"""Point-in-time universes and corporate actions from symbol tables (spec/05 § 14).

Synthetic bars throughout; the dates and amounts of the splits and dividends are public
facts (Apple's 4-for-1 split of 2020-08-31, its $0.82 dividend of August 2020), not rows of
any dataset. The last test reads PWB's own tables on the Hub when asked to
(``TRADEWIND_PWB_SAMPLE=1`` and a token), with a bounded read.
"""

from __future__ import annotations

import math
import os

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.clock import UniverseChange
from tradewind.corporate import SymbolCorporateActions, dividend_table, split_table
from tradewind.models import Delisting, Dividend, OracleCorporateActions, Split, SymbolChange
from tradewind.universe import IntervalUniverse, MembershipUniverse, feeds


def frame(days, close, open_=None):
    close = np.asarray(close, dtype=float)
    open_ = close if open_ is None else np.asarray(open_, dtype=float)
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close), "low": np.minimum(open_, close),
                         "close": close, "volume": np.full(len(close), 1e6)}, index=days)


def engine(frames, normalization=None, cash=100_000.0):
    e = tw.Engine()
    for name, f in frames.items():
        e.adddata(tw.feeds.PandasData(dataname=f), name=name,
                  normalization=(normalization or {}).get(name))
    e.broker.setcash(cash)
    return e


def run_values(e, strategy, **params):
    values = []

    class Recorder(tw.Analyzer):
        def next(self):
            values.append(self.strategy.broker.getvalue())

    e.addstrategy(strategy, **params)
    e.addanalyzer(Recorder)
    strat = e.run()[0]
    return strat, values


DAYS = pd.bdate_range("2021-01-04", periods=20)


def membership(rows):
    """``{symbol: (first, last)}`` day indices -> a daily member table, one row per day."""
    out = []
    for sym, (a, b) in rows.items():
        out += [(DAYS[k], sym) for k in range(a, b + 1)]
    return pd.DataFrame(out, columns=["date", "symbol"])


class Rotate(tw.Strategy):
    """Buys each name the day it enters, records every universe change."""

    params = (("size", 10),)

    def __init__(self):
        self.changes = []
        self.member_log = []

    def notify_universe(self, change):
        added, removed = feeds(self, change.added), feeds(self, change.removed)
        self.changes.append((pd.Timestamp(int(change.time)).date(), [d._name for d in added],
                             [d._name for d in removed]))
        for d in added:
            self.buy(data=d, size=self.p.size)

    def next(self):
        self.member_log.append(sorted(d._name for d in self.engine.universe.members))


# -- universe -----------------------------------------------------------------------------


def test_membership_universe_changes_and_hook():
    frames = {s: frame(DAYS, np.linspace(100, 120, 20)) for s in ("A", "B", "C")}
    frames["C"] = frame(DAYS[8:], np.linspace(50, 60, 12))       # lists on day 8
    frames["SPY"] = frame(DAYS, np.full(20, 400.0))
    e = engine(frames)
    e.set_universe(MembershipUniverse(membership({"A": (0, 19), "B": (5, 9), "C": (8, 19), "Z": (0, 3)})))
    e.addstrategy(Rotate)
    strat = e.run()[0]
    days = [d.date() for d in DAYS]
    assert strat.changes == [(days[0], ["A"], []), (days[5], ["B"], []), (days[8], ["C"], []),
                             (days[10], [], ["B"])]
    # next ran from the first bar: C (listed on day 8) did not hold the strategy back
    assert len(strat.member_log) == 20
    assert strat.member_log[4] == ["A"] and strat.member_log[9] == ["A", "B", "C"]
    sec = {d._name: d.security for d in e.datas}
    assert not sec["B"].in_universe and sec["A"].in_universe and sec["SPY"].in_universe
    assert not sec["B"].is_delisted
    assert e.universe.unresolved == ["Z"]
    # keep (the default): B's position stays after it left
    assert strat.getposition(e.datas[1]).size == 10
    # the gate is restored after the run
    assert all(getattr(d, "_tw_gate", True) for d in e.datas)


def test_removal_liquidates_at_next_open():
    opens = np.arange(100.0, 120.0)
    frames = {"A": frame(DAYS, opens + 0.5, opens), "B": frame(DAYS, opens + 0.5, opens)}
    e = engine(frames)
    e.set_universe(MembershipUniverse(membership({"A": (0, 19), "B": (2, 6)})), on_removal="liquidate")
    e.addstrategy(Rotate)
    strat = e.run()[0]
    b = e.datas[1]
    assert strat.getposition(b).size == 0
    sells = [o for o in e.broker.orders if o.data is b and o.size < 0]
    assert len(sells) == 1 and sells[0].status == sells[0].Completed
    # removed on day 7 (the first day out), sold at the open of day 8
    assert sells[0].executed.price == opens[8]


def test_removal_leaves_the_strategy_exit_alone():
    class Exit(Rotate):
        def notify_universe(self, change):
            super().notify_universe(change)
            for d in feeds(self, change.removed):
                self.close(data=d)

    frames = {"A": frame(DAYS, np.full(20, 10.0)), "B": frame(DAYS, np.full(20, 10.0))}
    e = engine(frames)
    e.set_universe(MembershipUniverse(membership({"A": (0, 19), "B": (2, 6)})), on_removal="liquidate")
    e.addstrategy(Exit)
    strat = e.run()[0]
    b = e.datas[1]
    assert strat.getposition(b).size == 0
    assert len([o for o in e.broker.orders if o.data is b and o.size < 0]) == 1


def test_full_universe_with_gate_is_the_plain_run():
    """A universe that holds every feed from the start, gated, changes no value."""
    rng = np.random.default_rng(3)
    frames = {s: frame(DAYS, 100 * np.exp(np.cumsum(rng.normal(0, 0.02, 20)))) for s in "ABC"}

    class EW(tw.Strategy):
        def next(self):
            for d in self.datas:
                self.order_target_percent(data=d, target=0.3)

    _, plain = run_values(engine(frames), EW)
    e = engine(frames)
    e.set_universe(MembershipUniverse(membership({s: (0, 19) for s in "ABC"})), gate=True)
    _, with_universe = run_values(e, EW)
    assert [v.hex() for v in plain] == [v.hex() for v in with_universe]


def test_membership_tolerance_lag_and_top():
    class Sec:
        def __init__(self, i, s):
            self.id, self.symbol, self.feed = i, s, s

    secs = [Sec(0, "A"), Sec(1, "B")]
    table = pd.DataFrame({"date": ["2021-01-04", "2021-01-04", "2021-01-11"], "symbol": ["A", "B", "A"],
                          "rank": [1, 2, 1]})
    def ns(d):
        return np.int64(pd.Timestamp(d).value)

    u = MembershipUniverse(table, tolerance="3D")
    u.bind(secs)
    assert u.change(ns("2021-01-04")) == UniverseChange(ns("2021-01-04"), (0, 1), ())
    assert u.change(ns("2021-01-06")) is None
    assert u.change(ns("2021-01-08")) == UniverseChange(ns("2021-01-08"), (), (0, 1))   # stale
    assert u.change(ns("2021-01-11")) == UniverseChange(ns("2021-01-11"), (0,), ())
    u = MembershipUniverse(table, top=1, lag="1D")
    u.bind(secs)
    assert u.change(ns("2021-01-04")) is None
    assert u.change(ns("2021-01-05")).added == (0,)
    assert u.managed == frozenset({0})


def test_interval_universe_from_listing_dates():
    frames = {s: frame(DAYS, np.full(20, 10.0)) for s in ("OLD", "NEW", "ALL")}
    listing = pd.DataFrame({"symbol": ["OLD", "NEW", "ALL"],
                            "ipoDate": ["1990-01-01", str(DAYS[6].date()), "1990-01-01"],
                            "delistingDate": [str(DAYS[4].date()), "None", None]})
    e = engine(frames)
    e.set_universe(IntervalUniverse(listing, start="ipoDate", end="delistingDate"))
    e.addstrategy(Rotate)
    strat = e.run()[0]
    days = [d.date() for d in DAYS]
    assert strat.changes == [(days[0], ["OLD", "ALL"], []), (days[4], [], ["OLD"]), (days[6], ["NEW"], [])]


# -- corporate actions ----------------------------------------------------------------------

SPLIT_DAYS = pd.bdate_range("2020-08-24", "2020-09-04")      # AAPL 4-for-1, ex-date 2020-08-31
AAPL_SPLIT = pd.DataFrame({"symbol": ["AAPL"], "date": ["2020-08-31"], "split_from": [1.0], "split_to": [4.0]})


class BuyOnce(tw.Strategy):
    params = (("size", 10), ("short", False))

    def __init__(self):
        self.actions = []
        self.trades = []

    def notify_corporate_action(self, action):
        self.actions.append(action)

    def notify_trade(self, trade):
        if trade.isclosed:
            self.trades.append(trade)

    def next(self):
        if len(self) == 1:
            if self.p.short:
                self.sell(size=self.p.size)
            else:
                self.buy(size=self.p.size)


def test_split_on_raw_prices_scales_the_position():
    raw = np.where(SPLIT_DAYS < "2020-08-31", 500.0, 125.0)
    e = engine({"AAPL": frame(SPLIT_DAYS, raw)}, normalization={"AAPL": "raw"})
    e.set_corporate_actions(SymbolCorporateActions(splits=AAPL_SPLIT))
    strat, values = run_values(e, BuyOnce)
    pos = strat.getposition(e.datas[0])
    assert pos.size == 40 and pos.price == 125.0
    assert len(set(values)) == 1                                     # flat price, flat value
    assert [type(a) for a in strat.actions] == [Split]
    assert pd.Timestamp(int(strat.actions[0].time)) == pd.Timestamp("2020-08-31")
    assert e.broker.trades[e.datas[0]].size == 40


def test_split_on_adjusted_prices_is_ignored():
    adjusted = np.full(len(SPLIT_DAYS), 125.0)
    e = engine({"AAPL": frame(SPLIT_DAYS, adjusted)}, normalization={"AAPL": "adjusted"})
    e.set_corporate_actions(SymbolCorporateActions(splits=AAPL_SPLIT))
    strat, values = run_values(e, BuyOnce)
    assert strat.getposition(e.datas[0]).size == 10
    assert len(set(values)) == 1
    assert len(strat.actions) == 1                                   # still delivered


def test_split_without_the_action_breaks_the_value():
    raw = np.where(SPLIT_DAYS < "2020-08-31", 500.0, 125.0)
    _, values = run_values(engine({"AAPL": frame(SPLIT_DAYS, raw)}), BuyOnce)
    assert min(values) < 0.97 * max(values)


DIV_DAYS = pd.bdate_range("2020-08-03", "2020-08-19")
AAPL_DIV = pd.DataFrame({"symbol": ["AAPL"], "date": ["2020-08-07"], "pay_date": ["2020-08-13"],
                         "cash_amount": [0.82], "split_adjusted_cash_amount": [0.205], "currency": ["USD"]})


def test_dividend_on_ex_date():
    e = engine({"AAPL": frame(DIV_DAYS, np.full(len(DIV_DAYS), 400.0))}, normalization={"AAPL": "raw"})
    e.set_corporate_actions(SymbolCorporateActions(dividends=AAPL_DIV))
    strat, values = run_values(e, BuyOnce)
    k = list(DIV_DAYS).index(pd.Timestamp("2020-08-07"))
    assert values[k] - values[k - 1] == pytest.approx(8.2)
    assert e.datas[0].security.holding.total_dividends == pytest.approx(8.2)
    assert isinstance(strat.actions[0], Dividend) and strat.actions[0].amount == 0.82


def test_dividend_on_pay_date_is_a_receivable():
    e = engine({"AAPL": frame(DIV_DAYS, np.full(len(DIV_DAYS), 400.0))}, normalization={"AAPL": "raw"})
    e.set_corporate_actions(SymbolCorporateActions(dividends=AAPL_DIV))
    e.broker.set_models(e.datas[0], corporate_actions=OracleCorporateActions(dividends="pay_date"))
    cash = []

    class Cash(BuyOnce):
        def next(self):
            super().next()
            cash.append(self.broker.getcash())

    _, values = run_values(e, Cash)
    days = list(DIV_DAYS)
    ex, pay = days.index(pd.Timestamp("2020-08-07")), days.index(pd.Timestamp("2020-08-13"))
    assert values[ex] - values[ex - 1] == pytest.approx(8.2)        # in the value from the ex-date
    assert cash[ex] == cash[ex - 1] and cash[pay] - cash[pay - 1] == pytest.approx(8.2)
    assert values[pay] == pytest.approx(values[ex]) and e.broker.receivables() == 0.0


def test_dividend_split_adjusted_amount_and_short():
    e = engine({"AAPL": frame(DIV_DAYS, np.full(len(DIV_DAYS), 100.0))}, normalization={"AAPL": "split_adjusted"})
    e.set_corporate_actions(SymbolCorporateActions(dividends=AAPL_DIV))
    strat, values = run_values(e, BuyOnce, size=40, short=True)
    k = list(DIV_DAYS).index(pd.Timestamp("2020-08-07"))
    assert strat.actions[0].amount == 0.205
    assert values[k] - values[k - 1] == pytest.approx(-8.2)          # a short pays it


def test_dividend_amount_from_later_splits_when_no_adjusted_amount():
    div = AAPL_DIV.drop(columns=["split_adjusted_cash_amount"])
    days = pd.bdate_range("2020-08-03", "2020-09-04")
    e = engine({"AAPL": frame(days, np.full(len(days), 100.0))}, normalization={"AAPL": "split_adjusted"})
    p = SymbolCorporateActions(splits=AAPL_SPLIT, dividends=div)
    e.set_corporate_actions(p)
    strat, _ = run_values(e, BuyOnce)
    assert [a.amount for a in strat.actions if isinstance(a, Dividend)] == [0.205]
    assert strat.getposition(e.datas[0]).size == 10                 # the split is in the prices


def test_delisting_liquidates_at_last_close():
    days = pd.bdate_range("2021-01-04", periods=15)
    gone = frame(days[:10], np.linspace(20, 11, 10))               # last bar on day 9, close 11
    other = frame(days, np.full(15, 50.0))
    e = engine({"X": gone, "Y": other})
    e.set_corporate_actions(SymbolCorporateActions(
        delistings=pd.DataFrame({"symbol": ["X"], "delistingDate": [str(days[12].date())]}), haircut=0.3))

    class Hold(BuyOnce):
        def next(self):
            super().next()
            if len(self) == 9:
                self.buy(data=self.datas[0], size=5, exectype=tw.Order.Limit, price=1.0)

    strat, values = run_values(e, Hold)
    x = e.datas[0]
    assert strat.getposition(x).size == 0 and x.security.is_delisted
    assert strat.trades and strat.trades[0].pnl == pytest.approx(10 * (11 * 0.7 - 19))
    assert values[-1] == pytest.approx(100_000 + 10 * (11 * 0.7 - 19))
    limit = next(o for o in e.broker.orders if o.data is x and o.exectype == tw.Order.Limit)
    assert limit.status in (limit.Canceled, limit.Expired)
    assert isinstance(strat.actions[0], Delisting)
    assert pd.Timestamp(int(strat.actions[0].time)) == days[12]


def test_delisting_of_a_reused_ticker_is_set_aside():
    days = pd.bdate_range("2021-01-04", periods=40)
    e = engine({"X": frame(days, np.full(40, 10.0))})
    p = SymbolCorporateActions(delistings=pd.DataFrame({"symbol": ["X"], "delistingDate": [str(days[5].date())]}))
    e.set_corporate_actions(p)
    strat, _ = run_values(e, BuyOnce)
    assert strat.getposition(e.datas[0]).size == 10 and p.conflicts and not p.actions


def test_delist_ended_feeds_and_unresolved():
    days = pd.bdate_range("2021-01-04", periods=12)
    e = engine({"X": frame(days[:6], np.full(6, 10.0)), "Y": frame(days, np.full(12, 10.0))})
    p = SymbolCorporateActions(splits=AAPL_SPLIT, delist_ended=True)
    e.set_corporate_actions(p)
    strat, _ = run_values(e, BuyOnce)
    assert strat.getposition(e.datas[0]).size == 0
    assert [type(a) for a in p.actions] == [Delisting]
    assert pd.Timestamp(int(p.actions[0].time)) == days[6]
    assert p.unresolved == ["AAPL"]


def test_symbol_change():
    days = pd.bdate_range("2021-10-25", periods=10)
    e = engine({"FB": frame(days, np.full(10, 300.0))})
    e.set_corporate_actions(SymbolCorporateActions(
        symbol_changes=pd.DataFrame({"date": ["2021-10-28"], "old": ["FB"], "new": ["META"]})))
    strat, _ = run_values(e, BuyOnce)
    assert e.datas[0].security.symbol == "META" and e.datas[0]._name == "FB"
    assert isinstance(strat.actions[0], SymbolChange)
    assert strat.getposition(e.datas[0]).size == 10


def test_tables_are_read_idempotently():
    s = split_table(AAPL_SPLIT)
    assert s["ratio"].tolist() == [4.0] and split_table(s).equals(s)
    d = dividend_table(AAPL_DIV)
    assert dividend_table(d).equals(d)
    jpy = AAPL_DIV.assign(currency="JPY")
    assert dividend_table(jpy).empty and len(dividend_table(jpy, currency=None)) == 1


def test_normalization_is_checked():
    e = tw.Engine()
    with pytest.raises(ValueError):
        e.adddata(tw.feeds.PandasData(dataname=frame(DAYS, np.ones(20))), normalization="total")
    with pytest.raises(ValueError):
        e.set_universe(None, on_removal="sell")


# -- PWB's tables on the Hub (opt-in) -------------------------------------------------------


@pytest.mark.skipif(os.environ.get("TRADEWIND_PWB_SAMPLE") != "1", reason="reads PWB's datasets on the Hub")
def test_pwb_sample():
    pytest.importorskip("huggingface_hub")
    pytest.importorskip("pyarrow")
    from tradewind.corporate import load_hub_table, pwb_corporate_actions, pwb_liquid_universe

    p = pwb_corporate_actions(["AAPL", "NVDA", "YHOO"], "2017-01-01", "2024-12-31")
    sp = p.splits.set_index(["symbol", "ex_date"])["ratio"]
    assert sp[("AAPL", pd.Timestamp("2020-08-31"))] == 4.0
    assert sp[("NVDA", pd.Timestamp("2024-06-10"))] == 10.0
    dv = p.dividends[(p.dividends["symbol"] == "AAPL") & (p.dividends["ex_date"] == pd.Timestamp("2020-08-07"))]
    assert dv["amount"].tolist() == [0.82] and dv["pay_date"].tolist() == [pd.Timestamp("2020-08-13")]
    assert p.delistings["symbol"].tolist() == ["YHOO"]
    u = pwb_liquid_universe("2017-06-01", "2017-07-31", symbols=["YHOO", "AABA"])
    dates = pd.to_datetime(pd.Series(u._dates))
    assert len(dates) and dates.min() >= pd.Timestamp("2017-06-01")
    t = load_hub_table("Stocks-Daily-LiquidUniverse", symbols=["YHOO", "AABA"], start="2017-06-01",
                       end="2017-07-31", columns=["date", "symbol"])
    last = t.groupby("symbol")["date"].agg(["min", "max"])
    assert last.loc["YHOO", "max"] == "2017-06-16" and last.loc["AABA", "min"] == "2017-07-17"
    assert not math.isnan(len(t))


def test_fingerprint_records_providers_and_normalization():
    def fp(table):
        e = engine({"A": frame(DAYS, np.full(20, 10.0))}, normalization={"A": "raw"})
        e.set_universe(MembershipUniverse(table), on_removal="liquidate")
        e.set_corporate_actions(SymbolCorporateActions(splits=AAPL_SPLIT))
        e.addstrategy(BuyOnce)
        e.run()
        return e.fingerprint

    a, b = fp(membership({"A": (0, 19)})), fp(membership({"A": (0, 18)}))
    assert a.data[0]["normalization"] == "raw"
    assert a.broker["universe"]["on_removal"] == "liquidate"
    assert a.broker["corporate_actions"]["splits"]["rows"] == 1
    assert a.broker["universe"]["table_sha256"] != b.broker["universe"]["table_sha256"]
    assert a.digest != b.digest


def test_typed_strategy_keeps_feed_names_across_a_ticker_change():
    # decision of 2026-11-15: in the typed API the key of ``self.bars`` and the names of
    # ``SecurityChanges`` stay the feed's name for the whole run; the new ticker is an attribute
    days = pd.bdate_range("2021-10-25", periods=10)
    e = engine({"FB": frame(days, np.full(10, 300.0)), "SPY": frame(days, np.full(10, 400.0))})
    e.set_corporate_actions(SymbolCorporateActions(
        symbol_changes=pd.DataFrame({"date": ["2021-10-28"], "old": ["FB"], "new": ["META"]})))
    table = pd.DataFrame([(d, "SPY") for d in days] + [(d, "FB") for d in days[2:7]],
                         columns=["date", "symbol"])
    e.set_universe(MembershipUniverse(table))

    class Typed(tw.TypedStrategy):
        def initialize(self):
            self.seen = []
            self.changes = []

        def on_bar(self, bar):
            fb = self.bars["FB"]
            self.seen.append((bar.date, fb.name, fb.symbol, self.securities["FB"].symbol,
                              fb.close[0]))
            if fb.symbol == "META" and not self.portfolio.invested:
                self.market_order("FB", 5)

        def on_universe_change(self, change):
            self.changes.append((change.time.date(), change.added, change.removed))

    e.addstrategy(Typed)
    typed = e.run()[0].typed
    before = [s for s in typed.seen if s[0] < days[3].date()]
    after = [s for s in typed.seen if s[0] >= days[3].date()]
    assert before and after
    assert all(s[1:4] == ("FB", "FB", "FB") for s in before)
    assert all(s[1:4] == ("FB", "META", "META") for s in after)
    assert all(s[4] == 300.0 for s in typed.seen)
    assert "FB" in typed.bars and "META" not in typed.bars
    assert typed.portfolio["FB"].quantity == 5
    names = {n for _, added, removed in typed.changes for n in added + removed}
    assert names == {"SPY", "FB"}
    assert (days[7].date(), (), ("FB",)) in typed.changes
