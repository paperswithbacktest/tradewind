"""The daily paper run of a meta-portfolio (tradewind.live.daily), end to end on the simulated
account: three strategies, twenty closes, no account and no network.

What is checked: a strategy's target book read off its backtest is the book it holds at the
next open; the summed book respects the leverage cap and the lots; with no costs and
fractional shares the account follows its backtest to the cent, and with lots, costs and
slippage the gap is explained term by term; nothing is sent without ``send``; the IBKR
account refuses anything but a paper account.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import types

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.live import AdapterError, IBAdapter
from tradewind.live.daily import (
    DailyRunner,
    EngineProvider,
    IBPaperAccount,
    MetaPortfolio,
    ReplayProvider,
    SimAccount,
    Sleeve,
    StaleData,
    plan_day,
    record_books,
)
from tradewind.live.daily.aggregate import round_lot
from tradewind.live.daily.books import SleeveBook

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "docs", "examples"))

from _data import bars

SYMBOLS = ["AAA", "BBB", "CCC", "DDD"]
FRAMES = {s: bars(s, start="2024-01-01", years=1.5, seed=i + 11) for i, s in enumerate(SYMBOLS)}
UNTIL = str(FRAMES["AAA"].index[-1].date())


class WeeklyMomentum(tw.Strategy):
    """Long the two best of four over 20 days, rebalanced every Monday."""

    def next(self):
        if self.datetime.date(0).weekday() != 0 or len(self) < 21:
            return
        ranked = sorted(self.datas, key=lambda d: d.close[0] / d.close[-20], reverse=True)
        for d in ranked[2:]:                       # sells first: the buys are then funded
            self.order_target_percent(d, 0.0)
        for d in ranked[:2]:
            self.order_target_percent(d, 0.45)


class LongShort(tw.Strategy):
    """Long AAA short BBB when AAA is above its 10-day mean, the reverse otherwise; daily."""

    def __init__(self):
        self.sma = tw.indicators.SMA(self.datas[0].close, period=10)

    def next(self):
        sign = 1 if self.datas[0].close[0] > self.sma[0] else -1
        self.order_target_percent(self.datas[0], 0.4 * sign)
        self.order_target_percent(self.datas[1], -0.4 * sign)


class SixtyForty(tw.Strategy):
    """60 % CCC, 40 % DDD, back to target every day (small daily trades)."""

    def next(self):
        self.order_target_percent(self.datas[0], 0.6)
        self.order_target_percent(self.datas[1], 0.35)


def _builder(strategy, names, commission=0.0):
    def build(until):
        cerebro = tw.Cerebro()
        for name in names:
            frame = FRAMES[name] if until is None else FRAMES[name].loc[:until]
            cerebro.adddata(tw.feeds.PandasData(dataname=frame), name=name)
        cerebro.addstrategy(strategy)
        cerebro.broker.setcash(100_000.0)
        if commission:
            cerebro.broker.setcommission(commission=commission)
        return cerebro
    return build


def _provider(commission=0.0):
    return EngineProvider({"momentum": _builder(WeeklyMomentum, SYMBOLS),
                           "longshort": _builder(LongShort, ["AAA", "BBB"], commission),
                           "sixtyforty": _builder(SixtyForty, ["CCC", "DDD"])})


def _meta(**kw):
    opts = {"leverage": 2.0, "max_gross": 4.0, "rebalance": "monthly", "lot": 0.0, "min_order_notional": 0.0}
    opts.update(kw)
    return MetaPortfolio([Sleeve("momentum", 0.4), Sleeve("longshort", 0.3), Sleeve("sixtyforty", 0.3)], **opts)


def _replay(tmp_path, meta, account_kw=None, commission=0.0, n=20, name="run"):
    replay = ReplayProvider(_provider(commission), UNTIL, n + 1)
    directory = str(tmp_path / name)
    account = SimAccount(os.path.join(directory, "sim.json"), cash=110_000.0, **(account_kw or {}))
    runner = DailyRunner(meta, replay, account, directory, log=lambda *a: None)
    dates = replay.dates()[-n:]
    reports = runner.replay(dates)
    return runner, reports, pd.read_csv(os.path.join(directory, "tracking.csv"))


# -- the books ---------------------------------------------------------------------------------------
def test_target_book_is_what_the_strategy_holds_after_the_next_open():
    build = _builder(WeeklyMomentum, SYMBOLS)
    with record_books(since=None, last_n=400) as rec:
        build(None).run()
    books = rec.books
    dates = sorted(books)
    assert len(dates) > 300
    changed = 0
    for a, b in itertools.pairwise(dates):
        assert books[a].target == pytest.approx(books[b].positions), (a, b)
        changed += books[a].target != books[a].positions
    assert changed > 20                           # rebalances happened and were read as targets
    last = books[dates[-1]]
    assert set(last.close) == set(SYMBOLS) and last.nav > 0


class _HoldOutside(tw.Strategy):
    """Buys AAA on the first bar and holds it; AAA's feed is a flat line after ``CUT`` and the
    real closes are ``REAL``, as pwb-toolbox's harness does for a name that left a point-in-time
    universe (``pit_exit="hold"``)."""

    CUT = 0
    REAL: pd.Series = pd.Series(dtype=float)
    fail = False

    def next(self):
        if len(self) == 1:
            self.order_target_size(self.datas[0], 10)

    def _pwb_mark_price(self, name, data):
        if self.fail:
            raise KeyError(name)
        day = pd.Timestamp(data.datetime.date(0))
        return float(self.REAL.loc[day]) if name == "AAA" and day > self.CUT else float(data.close[0])


def _flat_after(frame, cut):
    flat = frame.copy()
    last = flat.loc[:cut, "close"].iloc[-1]
    for col in ("open", "high", "low", "close"):
        flat.loc[flat.index > cut, col] = last
    return flat


@pytest.mark.parametrize("fail", [False, True])
def test_a_held_name_outside_the_universe_is_booked_at_its_real_close(fail):
    real = FRAMES["AAA"]
    cut = real.index[-40]
    flat = _flat_after(real, cut)
    strategy = type("HoldOutside", (_HoldOutside,), {"CUT": cut, "REAL": real["close"], "fail": fail})
    cerebro = tw.Cerebro()
    cerebro.adddata(tw.feeds.PandasData(dataname=flat), name="AAA")
    cerebro.adddata(tw.feeds.PandasData(dataname=FRAMES["BBB"]), name="BBB")
    cerebro.addstrategy(strategy)
    cerebro.broker.setcash(100_000.0)
    with record_books(since=str(real.index[-50].date()), last_n=60) as rec:
        cerebro.run()
    books = rec.books
    last = books[max(books)]
    assert last.positions == {"AAA": 10.0}
    assert last.close["BBB"] == pytest.approx(FRAMES["BBB"]["close"].iloc[-1])
    if fail:                                   # a hook that cannot say: the feed's flat close
        assert last.close["AAA"] == pytest.approx(flat["close"].iloc[-1])
        return
    for day, book in books.items():
        ts = pd.Timestamp(day)
        assert book.close["AAA"] == pytest.approx(real["close"].loc[ts])
        if ts > cut:                           # the flat bar's open is the stale close: the mark
            assert book.open["AAA"] == book.close["AAA"]
        else:
            assert book.open["AAA"] == pytest.approx(real["open"].loc[ts])
    assert len({b.close["AAA"] for b in books.values()}) == len(books)


def test_books_cut_at_a_past_close_equal_the_full_runs():
    full = _provider().books(None, last_n=30)
    day = sorted(full["momentum"])[-10]
    cut = _provider().books(day, last_n=2)
    for slug in full:
        assert cut[slug][day].to_dict() == full[slug][day].to_dict()


# -- the plan ----------------------------------------------------------------------------------------
def _book(nav, target, close):
    return SleeveBook(date="2026-01-05", nav=nav, positions=dict(target), target=dict(target),
                      open=dict(close), close=dict(close))


def test_plan_sums_units_caps_gross_and_rounds_lots():
    books = {"a": _book(100_000, {"X": 500, "Y": -200}, {"X": 100.0, "Y": 50.0}),     # gross 60 000
             "b": _book(50_000, {"X": 100, "Z": 300}, {"X": 100.0, "Z": 120.0})}      # gross 46 000
    meta = MetaPortfolio([Sleeve("a", 0.5), Sleeve("b", 0.5)], leverage=6.0, max_gross=4.0, lot=10,
                         min_order_notional=500)
    plan = plan_day(meta, books, {"X": 1000, "W": 5}, 100_000.0, "2026-01-05")
    # units before the cap: a 0.5*6*100k/100k = 3, b 0.5*6*100k/50k = 6
    raw = {"X": 3 * 500 + 6 * 100, "Y": 3 * -200, "Z": 6 * 300}
    raw_gross = (abs(raw["X"]) * 100 + abs(raw["Y"]) * 50 + abs(raw["Z"]) * 120) / 100_000
    assert plan.cap_scale == pytest.approx(4.0 / raw_gross)
    assert plan.gross_exact == pytest.approx(4.0)
    assert plan.gross_target <= 4.0 + 1e-12
    assert all(q % 10 == 0 for q in plan.target.values())
    assert plan.units["a"] == pytest.approx(3 * plan.cap_scale)
    orders = {o.symbol: o for o in plan.orders}
    assert plan.missing_price == ["W"] and orders["W"].target == 0 and orders["W"].quantity == -5
    assert orders["X"].quantity == plan.target["X"] - 1000


def test_round_lot_and_minimum_order():
    assert round_lot(14.5, 1) == 15 and round_lot(-14.5, 1) == -15 and round_lot(0.4, 1) == 0
    assert round_lot(1.234, 0) == 1.234
    books = {"a": _book(100_000, {"X": 1000}, {"X": 10.0})}
    meta = MetaPortfolio([Sleeve("a", 1.0)], leverage=1.0, lot=1, min_order_notional=200)
    plan = plan_day(meta, books, {"X": 990}, 100_000.0, "2026-01-05")
    assert not plan.orders and plan.skipped[0].quantity == 10
    plan = plan_day(meta, {"a": _book(100_000, {}, {"X": 10.0})}, {"X": 5}, 100_000.0, "2026-01-05")
    assert plan.orders[0].quantity == -5             # closing a line is never under the minimum


# -- end to end on the simulated account --------------------------------------------------------------
def test_replay_without_costs_follows_the_backtest_exactly(tmp_path):
    runner, reports, journal = _replay(tmp_path, _meta())
    assert len(journal) == 20
    nav = journal["nav_real"].to_numpy()
    assert np.ptp(nav) > 100                       # the book moved
    assert np.abs(journal["gap_day"]).max() < 1e-6 * nav.max()
    assert np.abs(journal["gap_cum"]).max() < 1e-6 * nav.max()
    for col in ("rounding", "untraded", "timing", "costs", "residual"):
        assert np.abs(journal[col]).max() < 1e-6 * nav.max(), col
    assert journal["rebalanced"].astype(bool).sum() >= 2     # the first day and a month start
    assert (journal["gross"] <= 4.0 + 1e-9).all()
    assert sum(len(r.plan.orders) for r in reports) > 20
    assert os.path.exists(os.path.join(runner.dir, "orders", f"{reports[-1].date}.md"))
    if reports[-1].chart:
        assert os.path.exists(reports[-1].chart)


def test_replay_with_daily_resets_and_a_binding_cap_still_has_no_gap(tmp_path):
    _runner, reports, journal = _replay(tmp_path, _meta(leverage=6.0, rebalance="daily"), name="cap")
    nav = journal["nav_real"].to_numpy()
    assert any(r.plan.cap_scale < 1.0 for r in reports)
    assert max(r.plan.gross_target for r in reports) <= 4.0 + 1e-9
    # the account, marked at the next close, drifts with the prices around the capped plan
    assert journal["gross"].max() < 4.0 * 1.1
    assert np.abs(journal["gap_cum"]).max() < 1e-6 * nav.max()


def test_lots_costs_and_slippage_are_explained_term_by_term(tmp_path):
    meta = _meta(lot=1.0, min_order_notional=300.0)
    _runner, _reports, journal = _replay(
        tmp_path, meta, commission=0.0005, name="costs",
        account_kw={"half_spread": 0.0004, "commission": "ibkr_tiered", "slippage": 0.0002})
    nav = journal["nav_real"].to_numpy()
    gap = journal["gap_cum"].iloc[-1]
    assert abs(gap) > 1.0                                    # there is a gap to explain
    assert (journal["costs_paid"] > 0).any() and (journal["costs_backtest"] > 0).any()
    assert (journal["timing"] < 0).any()                    # slippage costs, relative to the open
    assert (journal["rounding"] != 0).any()
    assert np.abs(journal["residual"]).max() < 1e-6 * nav.max()
    explained = journal[["rounding", "untraded", "timing", "costs"]].sum().sum()
    assert explained == pytest.approx(gap, abs=1e-6 * nav.max())


def test_propose_sends_nothing_and_a_rerun_of_the_day_counts_nothing_twice(tmp_path):
    replay = ReplayProvider(_provider(), UNTIL, 4)
    dates = replay.dates()[-3:]
    directory = str(tmp_path / "p")
    account = SimAccount(os.path.join(directory, "sim.json"), cash=110_000.0)
    runner = DailyRunner(_meta(), replay, account, directory, log=lambda *a: None)
    runner.run_day(dates[0], send=True)
    first = runner.run_day(dates[1], send=False)
    assert first.plan.orders and not account.state["pending"]      # proposed, not queued
    again = runner.run_day(dates[1], send=True)
    assert len(account.state["pending"]) == len(again.plan.orders)
    assert again.row["gap_day"] == pytest.approx(first.row["gap_day"], abs=1e-9)
    third = runner.run_day(dates[2], send=False)
    assert abs(third.row["gap_day"]) < 1e-6 * third.row["nav_real"]
    journal = pd.read_csv(os.path.join(directory, "tracking.csv"))
    assert list(journal["date"]) == dates
    open(os.path.join(directory, "KILL"), "w").close()
    with pytest.raises(RuntimeError, match="kill switch"):
        runner.run_day(dates[2], send=True)


def test_a_sleeve_behind_the_others_stops_the_run(tmp_path):
    hist = _provider().books(None, last_n=3)
    hist["momentum"] = {d: b for d, b in hist["momentum"].items() if d < max(hist["momentum"])}

    class Fixed:
        def books(self, as_of=None, last_n=2):
            return hist

    account = SimAccount(str(tmp_path / "s.json"))
    runner = DailyRunner(_meta(), Fixed(), account, str(tmp_path / "st"), today=UNTIL, log=lambda *a: None)
    with pytest.raises(StaleData, match="momentum"):
        runner.run_day()
    report = DailyRunner(_meta(), Fixed(), account, str(tmp_path / "st2"), allow_stale=True, today=UNTIL,
                         log=lambda *a: None).run_day()
    assert any("last close" in w for w in report.warnings)
    # every sleeve up to date, but the data a week behind the calendar
    late = DailyRunner(_meta(), _provider(), SimAccount(str(tmp_path / "l.json")), str(tmp_path / "late"),
                       today=str((pd.Timestamp(UNTIL) + pd.Timedelta(days=7)).date()), log=lambda *a: None)
    with pytest.raises(StaleData, match="not up to date"):
        late.run_day()


def test_meta_portfolio_files(tmp_path):
    csv = tmp_path / "sel.csv"
    pd.DataFrame({"slug": ["a", "b"], "weight": [0.6, 0.4], "leverage_meta": [4.0, 4.0]}).to_csv(csv, index=False)
    meta = MetaPortfolio.load(str(csv), lot=0)
    assert meta.leverage == 4.0 and meta.weights == {"a": 0.6, "b": 0.4} and meta.lot == 0
    js = tmp_path / "m.json"
    js.write_text(json.dumps({"leverage": 3, "rebalance": "weekly",
                              "sleeves": [{"slug": "a", "weight": 1, "path": "~/x/strategy.py"}]}))
    meta = MetaPortfolio.load(str(js))
    assert meta.sleeves[0].strategy_path("/p") == os.path.expanduser("~/x/strategy.py")
    with pytest.raises(ValueError):
        MetaPortfolio.from_dict({"sleeves": [], "leverag": 2})


# -- the IBKR paper account -----------------------------------------------------------------------------
class _Event:
    def __init__(self):
        self.handlers = []

    def connect(self, h):
        self.handlers.append(h)


class _StubIB:
    def __init__(self, accounts=("DU1234567",)):
        for name in ("orderStatusEvent", "execDetailsEvent", "commissionReportEvent", "errorEvent",
                     "disconnectedEvent"):
            setattr(self, name, _Event())
        self.accounts = list(accounts)
        self.connected = False
        self.readonly = None
        self.placed = []

    def connect(self, host, port, clientId, readonly=False, account=""):
        self.connected, self.readonly = True, readonly

    def disconnect(self):
        self.connected = False

    def isConnected(self):
        return self.connected

    def managedAccounts(self):
        return self.accounts

    def qualifyContracts(self, contract):
        return [contract]

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))

    def openTrades(self):
        return []

    def accountValues(self, account=""):
        v = types.SimpleNamespace
        return [v(tag="TotalCashValue", value="10000", currency="USD"),
                v(tag="NetLiquidation", value="110000", currency="USD")]

    def positions(self, account=""):
        return [types.SimpleNamespace(contract=types.SimpleNamespace(symbol="SPY"), position=10.0, avgCost=500.0)]

    def fills(self):
        return []

    def sleep(self, seconds):
        pass


def _ibm():
    return types.SimpleNamespace(Order=lambda **kw: types.SimpleNamespace(**kw),
                                 Stock=lambda s, e, c: types.SimpleNamespace(symbol=s, exchange=e, currency=c))


def _adapter(stub, port=7497, readonly=True):
    return IBAdapter("127.0.0.1", port, 31, ib=stub, ib_module=_ibm(), readonly=readonly)


def test_ibkr_account_is_paper_only_and_read_only_when_proposing():
    for port in (7496, 4001, 7777):
        with pytest.raises(AdapterError, match="paper"):
            IBPaperAccount(port=port, adapter=_adapter(_StubIB(), port=port))
    with pytest.raises(AdapterError, match="paper"):
        IBPaperAccount(account="U1234567", adapter=_adapter(_StubIB()))
    stub = _StubIB(accounts=("U7654321",))
    with pytest.raises(AdapterError):
        IBPaperAccount(adapter=_adapter(stub))
    assert not stub.connected
    stub = _StubIB(accounts=("DU1111111", "DU2222222"))
    with pytest.raises(AdapterError, match="single paper account"):
        IBPaperAccount(adapter=_adapter(stub))

    stub = _StubIB()
    account = IBPaperAccount(adapter=_adapter(stub), send=False)
    assert stub.readonly is True and account.account == "DU1234567"
    snap = account.snapshot()
    assert snap.nav == 110_000 and snap.positions == {"SPY": 10.0}
    from tradewind.live.daily.aggregate import ProposedOrder
    with pytest.raises(AdapterError, match="read-only"):
        account.place([ProposedOrder("SPY", 5, 500.0, 10, 15, 15.0)], "2026-12-15")
    assert not stub.placed


def test_ibkr_account_with_a_non_usd_base_needs_a_rate():
    stub = _StubIB()
    v = types.SimpleNamespace
    stub.accountValues = lambda account="": [v(tag="TotalCashValue", value="1000000", currency="EUR"),
                                             v(tag="NetLiquidation", value="1000054", currency="EUR")]
    account = IBPaperAccount(adapter=_adapter(stub), send=False)
    with pytest.raises(AdapterError, match="base currency is EUR"):
        account.snapshot()
    stub2 = _StubIB()
    stub2.accountValues = stub.accountValues
    adapter = IBAdapter("127.0.0.1", 7497, 31, ib=stub2, ib_module=_ibm(), readonly=True, base_fx=1.15)
    snap = IBPaperAccount(adapter=adapter, send=False).snapshot()
    assert snap.nav == pytest.approx(1000054 * 1.15) and snap.cash == pytest.approx(1_150_000)


def test_ibkr_account_sends_market_on_open_orders():
    stub = _StubIB()
    account = IBPaperAccount(adapter=_adapter(stub, readonly=False), send=True, ack_wait=0.0)
    from tradewind.live.daily.aggregate import ProposedOrder
    out = account.place([ProposedOrder("SPY", -4, 500.0, 10, 6, 6.2)], "2026-12-15")
    assert out[0]["id"] == "twd-20261215-0001"
    _contract, order = stub.placed[0]
    assert (order.action, order.totalQuantity, order.orderType, order.tif) == ("SELL", 4.0, "MKT", "OPG")


# -- catalogue strategies through pwb-toolbox (opt-in: needs that environment and its data) ---------------
@pytest.mark.skipif(not os.environ.get("TW_PWB_PYTHON"), reason="set TW_PWB_PYTHON (a Python with pwb-toolbox) "
                    "and PAPERS_DIR to run catalogue strategies")
def test_catalogue_strategies_replay_through_pwb_toolbox(tmp_path):
    from tradewind.live.daily import PwbProvider

    meta = MetaPortfolio([
        Sleeve("a_general_model_of_dynamic_asset_allocation_with_incomplete_information_and_learning", 0.094),
        Sleeve("protective_asset_allocation_paa_a_simple_momentum_based_alternative_for_term_deposits", 0.075)],
        leverage=4.0, max_gross=4.0, lot=0.0, papers_dir=os.environ.get("PAPERS_DIR", "~/dev/pwb/papers"),
        python=os.environ["TW_PWB_PYTHON"])
    replay = ReplayProvider(PwbProvider(meta, str(tmp_path / "books"), log=lambda *a: None), None, 21)
    account = SimAccount(str(tmp_path / "sim.json"), cash=110_000.0)
    runner = DailyRunner(meta, replay, account, str(tmp_path / "run"), log=lambda *a: None)
    runner.replay(replay.dates()[-20:])
    journal = pd.read_csv(tmp_path / "run" / "tracking.csv")
    assert len(journal) == 20
    assert np.abs(journal["gap_cum"]).max() < 1e-6 * journal["nav_real"].max()
