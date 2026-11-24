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
    assert offline.differential("GBP", "2026-11-20") == 0.0 and "no short rate for GBP" in offline.warnings[0]


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
