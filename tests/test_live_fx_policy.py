"""A portfolio's currency policy (spec/13, ``tradewind.live.daily.fxpolicy``): the base
currency, unhedged or hedged, the same rule for the runner's reference and for the account.

What is checked: unhedged, the reference's value is the USD value converted at the day's
rate; hedged, the dollar exposure is sold at each rebalance on both sides and the order is on
the base currency's pair; the carry and the fees are counted; on a simulated replay of an EUR
account the gap in EUR has no fx term left under either policy, its terms add up to it, and
``apply_fx_policy`` on the reference's USD series gives the runner's reference; a
meta-portfolio that declares nothing runs as before.
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

from tradewind.live.daily import (
    DailyRunner,
    FxBook,
    FxPolicy,
    MetaPortfolio,
    ReplayProvider,
    ShortRates,
    SimAccount,
    Sleeve,
    apply_fx_policy,
)
from tradewind.live.daily.runner import AccountCurrencyError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_live_daily import FRAMES, UNTIL, _provider

IDX = FRAMES["AAA"].index
#: EURUSD close and open (the open is not the previous close: the hedge fills somewhere else)
CLOSE = pd.Series(1.10 * np.exp(np.cumsum(np.random.default_rng(7).normal(0, 0.006, len(IDX)))), index=IDX)
OPEN = CLOSE.shift(1).fillna(CLOSE.iloc[0]) * (1 + 0.001 * np.sin(np.arange(len(IDX))))
RATES = {"EUR": 2.0, "USD": 4.5}


def _prices(symbols, start, end):
    out = {}
    for s in symbols:
        if s == "EURUSD":
            o, c = OPEN.loc[start:end], CLOSE.loc[start:end]
            out[s] = {str(d.date()): (float(o.loc[d]), float(c.loc[d])) for d in c.index}
    return out


def _meta(**kw):
    opts = {"leverage": 2.0, "max_gross": 4.0, "rebalance": "monthly", "lot": 0.0, "min_order_notional": 0.0}
    opts.update(kw)
    return MetaPortfolio([Sleeve("momentum", 0.4), Sleeve("longshort", 0.3), Sleeve("sixtyforty", 0.3)], **opts)


def _run(tmp_path, name, n=30, currency="EUR", **meta_kw):
    inner = _provider()
    inner._prices = _prices
    replay = ReplayProvider(inner, UNTIL, n + 1)
    directory = str(tmp_path / name)
    account = SimAccount(os.path.join(directory, "sim.json"), cash=100_000.0, currency=currency)
    runner = DailyRunner(_meta(**meta_kw), replay, account, directory, log=lambda *a: None,
                         rates=ShortRates(RATES))
    reports = runner.replay(replay.dates()[-n:])
    return runner, reports, pd.read_csv(os.path.join(directory, "tracking.csv"))


# -- the book, alone ----------------------------------------------------------------------------------
def _nav():
    return pd.Series(100_000.0 * np.exp(np.cumsum(np.random.default_rng(3).normal(0.0004, 0.01, len(IDX)))),
                     index=IDX)


def test_unhedged_value_is_the_usd_value_converted_at_the_days_rate():
    nav = _nav()
    policy = FxPolicy("EUR", "unhedged", half_spread=0.0)
    out = apply_fx_policy(nav, CLOSE, policy)
    fee = policy.fee(nav.iloc[0])                     # the first-day conversion: IDEALPRO's commission
    assert fee == pytest.approx(0.00002 * nav.iloc[0])
    assert out["order"].iloc[0] == pytest.approx(-nav.iloc[0] / CLOSE.iloc[0])
    assert (out["order"].iloc[1:] == 0).all()          # converted once, never again
    np.testing.assert_allclose(out["value"].iloc[1:], ((nav - fee) / CLOSE).iloc[1:], rtol=1e-12)
    assert np.abs(out["base"].iloc[1:]).max() < 1e-6


def test_hedged_exposure_is_sold_at_each_rebalance_and_only_the_months_pnl_is_exposed():
    nav = _nav()
    policy = FxPolicy("EUR", "hedged", "monthly", half_spread=0.0)
    out = apply_fx_policy(nav, CLOSE, policy)          # filled at the close it is decided at
    months = pd.Series(out.index.to_period("M"), index=out.index)
    starts = out.index[months.ne(months.shift(1))]
    assert set(out.index[out["order"] != 0]) <= set(starts)
    assert (out.loc[starts[1:], "order"] != 0).all()
    # the session after each order: what is left in USD is that session's P&L (and the fee)
    for d in starts[1:-1]:
        nxt = out.index[out.index.get_loc(d) + 1]
        pnl = nav.loc[nxt] - nav.loc[d]
        assert out.loc[nxt, "usd"] == pytest.approx(pnl - out.loc[nxt, "fee"], abs=1e-6)
    # unhedged exposure is the whole value; hedged it is at most a month of P&L
    assert np.abs(out["usd"]).max() < 0.2 * nav.max()


def test_carry_and_fees_are_counted():
    book = FxBook(base=100_000.0, usd=0.0, pending=50_000.0, last="2026-01-02")
    policy = FxPolicy("EUR", "hedged", half_spread=0.0001)
    info = book.step(policy, pnl_usd=0.0, open_rate=1.10, days=3, differential=-2.5)
    assert info["carry"] == pytest.approx(100_000.0 * -0.025 * 3 / 360)
    assert info["fee"] == pytest.approx(2.0 + 55_000.0 * 0.0001)   # commission (2 USD minimum) + half-spread
    assert book.base == pytest.approx(100_000.0 + info["carry"] + 50_000.0)
    assert book.usd == pytest.approx(-55_000.0 - info["fee"])
    # a whole year at a constant differential
    nav = pd.Series(100_000.0, index=pd.bdate_range("2025-01-01", "2025-12-31"))
    out = apply_fx_policy(nav, pd.Series(1.0, index=nav.index), FxPolicy("EUR", "hedged", half_spread=0.0),
                          rates=ShortRates(RATES))
    days = (nav.index[-1] - nav.index[0]).days
    assert out["value"].iloc[-1] / out["value"].iloc[0] == pytest.approx((1 + -0.025 / 360) ** days, rel=1e-3)


def test_short_rates_are_read_as_of_the_previous_day_from_public_csvs(tmp_path):
    fred = "observation_date,DFF\n2026-11-18,3.88\n2026-11-19,3.87\n2026-11-20,3.89\n"
    ecb = ("KEY,FREQ,TIME_PERIOD,OBS_VALUE,TITLE\nEST.B.X.WT,B,2026-11-18,2.443,a\n"
           "EST.B.X.WT,B,2026-11-20,2.444,a\n")
    urls = []

    def fetch(url):
        urls.append(url)
        return fred if "fred" in url else ecb

    rates = ShortRates({"EUR": "ecb:EST/B.X.WT"}, str(tmp_path / "rates"), fetch=fetch, log=lambda *a: None)
    assert rates.rate("USD", "2026-11-19") == 3.87
    assert rates.rate("EUR", "2026-11-19") == 2.443          # no observation that day: the one before
    assert rates.differential("EUR", "2026-11-20") == pytest.approx(2.444 - 3.89)
    assert rates.rate("USD", "2026-11-01") is None
    assert any("fredgraph.csv?id=DFF" in u for u in urls) and any("data-api.ecb.europa.eu" in u for u in urls)
    assert sorted(os.listdir(tmp_path / "rates")) == ["ecb_EST_B.X.WT.csv", "fred_DFF.csv"]
    offline = ShortRates({"GBP": "fred:NOPE"}, None, fetch=lambda url: 1 / 0, log=lambda *a: None)
    assert offline.differential("GBP", "2026-11-20") == 0.0
    assert "not downloaded" in offline.warnings[0] and "no short rate for GBP" in offline.warnings[-1]


# -- the runner ------------------------------------------------------------------------------------
def _assert_terms_add_up(journal):
    terms = ["rounding_base", "untraded_base", "timing_base", "costs_base", "carry_base", "fx_base",
             "residual_base"]
    np.testing.assert_allclose(journal[terms].sum(axis=1), journal["gap_day_base"], atol=1e-6)
    np.testing.assert_allclose(journal["gap_day_base"].cumsum(), journal["gap_cum_base"], atol=1e-6)


@pytest.mark.parametrize("policy", ["unhedged", "hedged"])
def test_an_eur_account_tracks_its_reference_in_eur_with_no_fx_term(tmp_path, policy):
    _runner, reports, journal = _run(tmp_path, policy, base_currency="EUR", fx_policy=policy, capital=100_000.0)
    nav = journal["nav_base"].to_numpy()
    _assert_terms_add_up(journal)
    # in USD the EUR cash (or the converted capital) moves with the rate; in EUR nothing does
    assert np.abs(journal["fx_base"]).max() < 1e-6 * nav.max()
    assert np.abs(journal["residual_base"]).max() < 1e-6 * nav.max()
    # what is left is the reference's carry (a simulated account books no interest) and its fee
    left = journal["gap_cum_base"] - (journal["carry_base"] + journal["costs_base"]).cumsum()
    assert np.abs(left).max() < 1e-6 * nav.max()
    assert abs(journal["costs_base"].sum()) < 1e-3 * nav.max()
    hedges = [(r.date, o) for r in reports for o in r.plan.orders if o.symbol == "EURUSD"]
    if policy == "unhedged":
        assert np.abs(journal["fx"]).max() > 10                   # the old term, in USD, is not zero
        assert len(hedges) == 1 and hedges[0][0] == reports[0].date
        assert hedges[0][1].quantity == pytest.approx(-100_000.0)  # the capital converted to USD
        assert np.abs(journal["carry_base"].iloc[2:]).max() < 0.01   # no EUR left after the conversion
        # the reference is the USD reference converted at the day's rate, after the capital is
        # converted at the next open (and the carry of the night it was still in EUR)
        o = OPEN.loc[pd.Timestamp(journal["date"].iloc[1])]
        fee = FxPolicy("EUR", "unhedged").fee(100_000.0 * o)
        usd = journal["nav_expected"] - journal["nav_expected"].iloc[0] + 100_000.0 * o - fee
        night = -journal["carry_base"].iloc[1]
        np.testing.assert_allclose(journal["nav_expected_base"].iloc[1:],
                                   (usd / journal["fx_rate"] + night).iloc[1:], rtol=1e-6)
    else:
        months = pd.to_datetime(journal["date"]).dt.to_period("M")
        starts = set(journal["date"][months.ne(months.shift(1))].iloc[1:])
        assert {d for d, _o in hedges} == starts and starts
        assert (journal["carry_base"] > 0).any()             # EUR below USD: holding EUR costs carry
        after = journal[journal["date"].isin(starts)].index + 1
        after = after[after < len(journal)]
        exposure = journal.loc[after, ["fx_exposure", "fx_exposure_expected"]].abs().to_numpy()
        daily_pnl = journal["real_change"].abs().max()
        assert exposure.max() < 2 * daily_pnl                 # sold: what is left is about a day of P&L
    with open(reports[-1].proposal_md, encoding="utf-8") as fh:
        assert "currency policy **" + policy + "**" in fh.read()


def test_the_backtest_helper_gives_the_runners_reference(tmp_path):
    _runner, _reports, journal = _run(tmp_path, "coherent", base_currency="EUR", fx_policy="hedged",
                                      capital=100_000.0)
    dates = pd.to_datetime(journal["date"])
    nav_usd = pd.Series(journal["nav_expected"].to_numpy(), index=dates)
    rate = pd.Series(journal["fx_rate"].to_numpy(), index=dates)
    out = apply_fx_policy(nav_usd, rate, FxPolicy("EUR", "hedged"), rates=ShortRates(RATES),
                          opens=OPEN.reindex(dates), capital_usd=100_000.0 * rate.iloc[0])
    np.testing.assert_allclose(out["value"].to_numpy(), journal["nav_expected_base"].to_numpy(), rtol=1e-12)


def test_nothing_declared_runs_as_before_and_wrong_declarations_stop(tmp_path):
    _runner, reports, journal = _run(tmp_path, "plain", n=6)
    assert journal["nav_expected_base"].isna().all() and journal["fx_base"].isna().all()
    assert not any(o.symbol == "EURUSD" for r in reports for o in r.plan.orders)
    assert "policy" not in reports[0].fx
    with pytest.raises(ValueError, match="needs a base_currency other than USD"):
        _meta(fx_policy="hedged")
    with pytest.raises(ValueError, match="fx_policy is one of"):
        _meta(base_currency="EUR", fx_policy="half")
    with pytest.raises(ValueError, match="fx_hedge_rebalance is one of"):
        _meta(base_currency="EUR", fx_policy="hedged", fx_hedge_rebalance="yearly")
    with pytest.raises(AccountCurrencyError, match="base_currency GBP"):
        _run(tmp_path, "gbp", n=2, base_currency="GBP")
    meta = MetaPortfolio.from_dict({"sleeves": [{"slug": "a", "weight": 1.0}], "base_currency": "eur",
                                    "fx_policy": "Hedged", "fx_rates": {"EUR": 2.0}})
    pol = meta.currency_policy()
    assert (pol.base_currency, pol.policy, pol.rebalance, pol.pair) == ("EUR", "hedged", "monthly", "EURUSD")


# -- the account's dollar exposure, read currency by currency (accrued interest is not dollars) -------
def _ib_account(values, positions):
    """An IB paper account (a stub of TWS) that reports ``values`` (tag, currency, value) and
    ``positions`` (symbol, quantity, average cost)."""
    import types

    from test_live_daily import _adapter, _StubIB

    from tradewind.live.daily import IBPaperAccount

    stub = _StubIB()
    v = types.SimpleNamespace
    stub.accountValues = lambda account="": [v(tag=t, value=str(x), currency=c) for t, c, x in values]
    stub.positions = lambda account="": [v(contract=v(symbol=s), position=q, avgCost=a) for s, q, a in positions]
    return IBPaperAccount(adapter=_adapter(stub), send=False)


#: the EUR paper account as IB reported it on 2026-11-24 (report fx_config_2026-11-24, n. 1): a NAV
#: of 1 000 271.69 EUR, a CashBalance of 1 000 000 EUR and 274.09 EUR of interest accrued, at
#: 1.09021 USD per EUR. The dollar side (one stock, the USD borrowed for it) is set so that the
#: items add up to that NAV: the account's dollar exposure is -2.40 EUR, not the +296 USD read then.
X_IB = 1.09021
IB_VALUES = [
    ("NetLiquidation", "EUR", 1_000_271.69), ("NetLiquidation", "BASE", 1_000_271.69),
    ("TotalCashValue", "EUR", 994_685.85),
    ("CashBalance", "EUR", 1_000_000.00), ("CashBalance", "USD", -5_002.616504), ("CashBalance", "BASE", 995_411.39),
    ("AccruedCash", "EUR", 274.09), ("AccruedCash", "BASE", 274.09),
    ("ExchangeRate", "EUR", 1.0), ("ExchangeRate", "USD", 1.0 / X_IB), ("ExchangeRate", "BASE", 1.0),
]
IB_POSITIONS = [("SPY", 10.0, 480.0), ("EUR.USD", 0.0, 0.0)]


def test_accrued_interest_in_the_base_currency_is_not_a_dollar_exposure():
    from tradewind.live.daily.fxpolicy import account_split

    snap = _ib_account(IB_VALUES, IB_POSITIONS).snapshot({"SPY": 500.0})
    assert snap.nav == pytest.approx(1_000_271.69 * X_IB)
    assert snap.balances == {"EUR": 1_000_000.0, "USD": -5_002.616504} and snap.accrued == {"EUR": 274.09}
    split = account_split(snap, "EUR", X_IB, pair="EURUSD", hedge_in_base_cash=True)
    # the old reading: the NAV less the EUR cash balance, the accrued interest taken for dollars
    assert snap.nav - snap.base_cash * X_IB == pytest.approx(296.2, abs=0.1)
    # by currency: EUR cash and EUR interest are base units, the dollars are the stock less the loan
    assert split.base_units == pytest.approx(1_000_274.09)
    assert split.usd == pytest.approx(-2.40 * X_IB, abs=0.01)
    assert abs(split.unexplained) < 0.01 and not split.notes
    # hedged, IB's minimum of 200 USD: no order (the old reading proposed +272 EUR, a ghost)
    policy = FxPolicy("EUR", "hedged", "monthly", min_notional=200.0)
    assert policy.order(split.usd, X_IB, day="2026-12-01", last="2026-11-24", capital_usd=0.0) == 0.0
    assert policy.order(296.2, X_IB, day="2026-12-01", last="2026-11-24", capital_usd=0.0) == pytest.approx(
        296.2 / X_IB)
    # IB gives the accrued interest only for BASE: it is the base currency's
    only_base = [x for x in IB_VALUES if not (x[0] == "AccruedCash" and x[1] == "EUR")]
    snap = _ib_account(only_base, IB_POSITIONS).snapshot({"SPY": 500.0})
    assert snap.accrued == {"EUR": 274.09}
    # USD interest accrued (the loan's) is dollars
    with_usd = IB_VALUES + [("AccruedCash", "USD", -3.0)]
    split = account_split(_ib_account(with_usd, IB_POSITIONS).snapshot({"SPY": 500.0}), "EUR", X_IB)
    assert split.usd == pytest.approx(-2.40 * X_IB - 3.0, abs=0.01)


def test_an_unexplained_nav_withholds_the_hedge_order():
    from tradewind.live.daily.fxpolicy import account_split

    # a position with no close to value it: the items do not add up to the NAV
    snap = _ib_account(IB_VALUES, IB_POSITIONS).snapshot({})
    split = account_split(snap, "EUR", X_IB)
    assert split.unexplained == pytest.approx(5_000.0, abs=0.01) and "SPY +10: no value" in split.notes


class _AccruingSim(SimAccount):
    """A simulated EUR account that accrues interest on its EUR cash as IB reports it: in the NAV
    (``AccruedCash``), not in the cash balance."""

    per_day = 27.4

    def snapshot(self, closes=None):
        snap = super().snapshot(closes)
        n = len(self.state.get("_days", []))
        if self.state.get("date") and self.state.get("date") not in self.state.setdefault("_days", []):
            self.state["_days"].append(self.state["date"])
            n += 1
        accrued = 274.09 + self.per_day * n
        snap.accrued = {self.currency: accrued}
        snap.nav += accrued * snap.fx_rate
        snap.nav_base = snap.nav / snap.fx_rate
        return snap


def _run_account(tmp_path, name, account_cls, n=30, cash=100_000.0, **meta_kw):
    inner = _provider()
    inner._prices = _prices
    replay = ReplayProvider(inner, UNTIL, n + 1)
    directory = str(tmp_path / name)
    account = account_cls(os.path.join(directory, "sim.json"), cash=cash, currency="EUR")
    runner = DailyRunner(_meta(**meta_kw), replay, account, directory, log=lambda *a: None, rates=ShortRates(RATES))
    reports = runner.replay(replay.dates()[-n:])
    return runner, reports, pd.read_csv(os.path.join(directory, "tracking.csv"))


def test_the_runner_does_not_hedge_accrued_base_interest(tmp_path):
    kw = {"base_currency": "EUR", "fx_policy": "hedged", "capital": 100_000.0, "min_order_notional": 200.0}
    _r, plain_reports, plain = _run_account(tmp_path, "plain", SimAccount, **kw)
    _r, reports, accruing = _run_account(tmp_path, "accruing", _AccruingSim, **kw)
    # the interest is in the NAV, and none of it is a dollar exposure
    assert (accruing["nav_real"] - plain["nav_real"]).iloc[-1] > 274.09 * 1.0
    np.testing.assert_allclose(accruing["fx_exposure"], plain["fx_exposure"], atol=1e-6)
    np.testing.assert_allclose(accruing["fx_unexplained"], 0.0, atol=1e-6)

    def hedges(rs):
        return [(r.date, round(o.quantity, 6)) for r in rs for o in r.plan.orders if o.symbol == "EURUSD"]

    assert hedges(reports) == hedges(plain_reports)
    assert reports[0].row["fx_hedge_order"] == 0.0           # the first day: no ghost order


# -- the carry is on the capital, not on the account's other cash --------------------------------------
def test_the_carry_is_on_the_capitals_base_part_not_on_the_whole_accounts_cash(tmp_path):
    _r, _reports, big = _run_account(tmp_path, "big", SimAccount, n=6, cash=1_000_000.0, base_currency="EUR",
                                     fx_policy="hedged", capital=110_000.0)
    days = (pd.to_datetime(big["date"]).diff().dt.days).iloc[1]
    diff = (RATES["EUR"] - RATES["USD"]) / 100.0
    # the first night: the capital (110 000 EUR) earns the differential, the other 890 000 EUR do not
    assert big["carry_base"].iloc[1] == pytest.approx(-110_000.0 * diff * days / 360.0, rel=1e-9)
    _r, _reports, small = _run_account(tmp_path, "small", SimAccount, n=6, cash=110_000.0, base_currency="EUR",
                                       fx_policy="hedged", capital=110_000.0)
    np.testing.assert_allclose(big["carry_base"], small["carry_base"], rtol=1e-6, atol=1e-6)
    # a reference saved before this fix (no "outside"): the capital's part is found again
    book = FxBook.from_dict({"base": 1_000_000.0, "usd": 0.0, "pending": 0.0, "last": "2026-11-24"})
    assert book.outside == 0.0 and book.carried == 1_000_000.0


# -- fx_hedge_rebalance: never -------------------------------------------------------------------------
def test_never_makes_no_currency_trade_and_leaves_no_fx_term(tmp_path):
    _runner, reports, journal = _run(tmp_path, "never", base_currency="EUR", fx_policy="hedged",
                                     fx_hedge_rebalance="never", capital=100_000.0)
    assert not [o for r in reports for o in r.plan.orders if o.symbol == "EURUSD"]
    assert (journal["fx_hedge_order"] == 0).all() and (journal["fx_hedge_order_expected"] == 0).all()
    _assert_terms_add_up(journal)
    nav = journal["nav_base"].to_numpy()
    assert np.abs(journal["fx_base"]).max() < 1e-6 * nav.max()
    # the dollar exposure is the strategies' P&L since the start, on both sides, never sold
    pnl = journal["expected_change"].cumsum()
    np.testing.assert_allclose(journal["fx_exposure_expected"], pnl, atol=1e-6)
    assert np.abs(journal["fx_exposure"] - journal["fx_exposure_expected"]).max() < 1e-6 * nav.max() * 1.1
    # the capital stays in EUR and earns the differential
    assert (journal["carry_base"].iloc[1:] > 0).all()
    with open(reports[-1].proposal_md, encoding="utf-8") as fh:
        assert "(never)" in fh.read()
    policy = FxPolicy("EUR", "hedged", "never")
    book = FxBook()
    assert book.decide(policy, 1.1, "2026-12-01", 0.0) == 0.0 and book.last == "2026-12-01"
    assert policy.order(1e9, 1.1, day="2027-01-04", last="2026-12-01", capital_usd=0.0) == 0.0
    assert policy.order(1e9, 1.1, day="2027-01-04", last=None, capital_usd=0.0) == 0.0
    with pytest.raises(ValueError, match="never goes with fx_policy hedged"):
        FxPolicy("EUR", "unhedged", "never")
    meta = MetaPortfolio.from_dict({"sleeves": [{"slug": "a", "weight": 1.0}], "base_currency": "EUR",
                                    "fx_policy": "hedged", "fx_hedge_rebalance": "never"})
    assert meta.currency_policy().trades_never


# -- the short rates: a descriptive agent, a timeout, and an alert when a series is not read ----------
class _FakeFred:
    """A local server that answers like FRED: only to a descriptive User-Agent; a bare name
    (or the ``SLOW`` series) hangs."""

    def __init__(self):
        import http.server
        import threading

        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                ua = self.headers.get("User-Agent", "")
                seen.append((self.path, ua))
                if "SLOW" in self.path or not (ua.startswith("tradewind/") and "(+https://" in ua):
                    import time

                    time.sleep(1.5)
                    return
                body = b"observation_date,DFF\n2026-11-20,3.89\n2026-11-23,3.88\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/graph/fredgraph.csv?id={{id}}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_fred(monkeypatch):
    from tradewind.live.daily import fxpolicy

    server = _FakeFred()
    monkeypatch.setattr(fxpolicy, "FRED_CSV", server.url)
    yield server
    server.close()


def test_short_rates_are_downloaded_with_a_descriptive_agent(tmp_path, fake_fred):
    import time
    import urllib.request

    import tradewind
    from tradewind.live.daily.fxpolicy import user_agent

    assert user_agent() == f"tradewind/{tradewind.__version__} (+https://github.com/paperswithbacktest/tradewind)"
    rates = ShortRates({"USD": "fred:DFF", "EUR": 2.0}, str(tmp_path / "rates"), log=lambda *a: None, timeout=1.0)
    assert rates.rate("USD", "2026-11-23") == 3.88 and not rates.warnings
    assert rates.differential("EUR", "2026-11-23") == pytest.approx(2.0 - 3.88)
    assert fake_fred.seen[0][1] == user_agent()
    # the agent this replaces gets no answer from the server
    req = urllib.request.Request(fake_fred.url.format(id="DFF"), headers={"User-Agent": "tradewind"})
    t0 = time.monotonic()
    with pytest.raises(OSError):
        urllib.request.urlopen(req, timeout=0.5)
    assert time.monotonic() - t0 < 1.4


def test_a_short_rate_not_read_is_an_explicit_warning_never_a_silent_zero(tmp_path, fake_fred):
    import time

    logged = []
    rates = ShortRates({"USD": "fred:SLOW", "EUR": 2.0}, str(tmp_path / "rates"), log=logged.append, timeout=0.5)
    t0 = time.monotonic()
    assert rates.differential("EUR", "2026-11-23") == 0.0
    assert time.monotonic() - t0 < 1.4                       # the timeout, not 30 s
    assert len(rates.warnings) == 2 and all(w.startswith("fx: ") for w in rates.warnings)
    assert "short rate USD (fred:SLOW) not downloaded" in rates.warnings[0] and "no cached copy" in rates.warnings[0]
    assert "no short rate for USD (fred:SLOW: " in rates.warnings[1] and "carry is taken as 0" in rates.warnings[1]
    assert logged
    # a copy cached on an earlier day is used, and said so
    with open(tmp_path / "rates" / "fred_SLOW.csv", "w") as fh:
        fh.write("observation_date,DFF\n2026-11-20,3.89\n")
    os.utime(tmp_path / "rates" / "fred_SLOW.csv", (0, 86_400 * 20_000))
    cached = ShortRates({"USD": "fred:SLOW", "EUR": 2.0}, str(tmp_path / "rates"), log=lambda *a: None, timeout=0.5)
    assert cached.differential("EUR", "2026-11-23") == pytest.approx(2.0 - 3.89)
    assert len(cached.warnings) == 1 and "the cached copy of 2024-" in cached.warnings[0]


def test_the_session_raises_an_fx_policy_alert_when_a_rate_is_not_read(tmp_path, fake_fred):
    import datetime as _dt

    from tradewind.calendars import NYSE
    from tradewind.live.daily.session import EXIT_ALERT, AlertConfig, DailySession

    inner = _provider()
    inner._prices = _prices
    replay = ReplayProvider(inner, UNTIL, 4)
    d1, d2 = [x for x in replay.dates() if NYSE.is_trading_day(x)][-2:]
    meta = _meta(base_currency="EUR", fx_policy="hedged", capital=100_000.0,
                 fx_rates={"USD": "fred:SLOW", "EUR": 2.0})
    directory = str(tmp_path / "paper")

    def account(send=False):
        return SimAccount(os.path.join(directory, "sim.json"), cash=100_000.0, currency="EUR")

    DailyRunner(meta, replay, account(), directory, log=lambda *a: None,
                rates=ShortRates(meta.fx_rates, timeout=0.5, log=lambda *a: None)).run_day(d1)
    out = DailySession(meta, directory, provider_factory=lambda: replay, account_factory=account, session=d2,
                       now=_dt.datetime.combine(_dt.date.fromisoformat(d2), _dt.time(23, 0), tzinfo=_dt.timezone.utc),
                       alerts=AlertConfig(max_gap=1.0, max_gap_day=1.0, max_loss=1.0), log=lambda *a: None).run()
    assert out.code == EXIT_ALERT
    fx_alerts = [a["message"] for a in out.report["alerts"] if a["code"] == "fx_policy"]
    assert any("not downloaded" in m for m in fx_alerts) and any("carry is taken as 0" in m for m in fx_alerts)
