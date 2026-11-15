"""Live trading (spec/07-live-ibkr.md) against the simulated venue: no account, no network.

Every behaviour a real session depends on is provoked here through ``FakeIB``: the
live/backtest equivalence, warm-up, acknowledgements, partial fills, expiry, rejections, a
dropped connection, reconciliation, the guards and the kill switch, and the journal's replay
and resume. The Interactive Brokers adapter is tested on its pure mappings and against a
stub of ib_async's interface.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import types

import pandas as pd
import pytest

import tradewind as tw
from tradewind.live import (
    AccountSnapshot,
    Ack,
    BarRecord,
    BrokerOrder,
    Canceled,
    Execution,
    FakeIB,
    FakeMarket,
    IBAdapter,
    IBBars,
    Journal,
    KeyedInterval,
    KillSwitch,
    LiveClock,
    LiveDivergence,
    LiveEngine,
    LiveFeed,
    RateLimiter,
    Rejected,
    RiskLimits,
    order_request,
)
from tradewind.live.ibkr import ib_order_fields, status_message
from tradewind.live.messages import Notice, OrderRequest, decode, encode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "docs", "examples"))

from _data import bars

SPLIT = 300
HOUR = pd.Timedelta(hours=1)


class Trend(tw.Strategy):
    params = (("start", None), ("period", 20))

    def __init__(self):
        self.sma = tw.ind.SMA(self.data.close, period=self.p.period)
        self.log = []
        self.statuses = []

    def next(self):
        if self.p.start is not None and self.datetime.date(0) < self.p.start:
            return
        self.order_target_percent(target=0.9 if self.data.close[0] > self.sma[0] else 0.0)

    def notify_order(self, order):
        self.statuses.append((str(self.datetime.date(0)), order.getstatusname()))
        if order.status in (order.Completed, order.Margin, order.Rejected, order.Partial):
            self.log.append((str(self.datetime.date(0)), order.getstatusname(), order.executed.size,
                             order.executed.price))


class Scripted(tw.Strategy):
    """Runs ``script[k](self)`` on the k-th live ``next`` (warm-up bars excluded)."""

    params = (("script", None),)

    def __init__(self):
        self.k = 0
        self.statuses = []
        self.orders = []

    def next(self):
        if self.broker.suppress:
            return
        action = (self.p.script or {}).get(self.k)
        self.k += 1
        if action is not None:
            out = action(self)
            if isinstance(out, tw.Order):
                self.orders.append(out)

    def notify_order(self, order):
        self.statuses.append((self.k, order.getstatusname(), order.executed.size, order.info.get("tw_reason", "")))


def _frame(seed=1, years=2):
    return bars("A", seed=seed, years=years)


def _session(strategy=Trend, frames=None, split=SPLIT, cash=100_000.0, venue=None, market=None,
             journal=None, max_slices=None, fake=None, engine=None, **params):
    frames = frames or {"A": _frame()}
    first = next(iter(frames.values()))
    market = market or FakeMarket(frames, first.index[split - 1] + 21 * HOUR)
    venue = venue or FakeIB(market, cash=cash, **(fake or {}))
    live = LiveEngine(venue, journal=journal, poll_interval=6 * 3600, fsync=False, **(engine or {}))
    for name in frames:
        live.adddata(LiveFeed(dataname=market.history(name)), name=name)
    live.addstrategy(strategy, **params)
    strat = live.run(max_slices=max_slices)[0]
    return live, strat, venue, market


# -- the live loop reproduces the backtest -----------------------------------------------------------

def test_live_session_reproduces_the_backtest():
    frame = _frame()
    start = frame.index[SPLIT].date()
    bt = tw.Engine()
    bt.adddata(tw.feeds.PandasData(dataname=frame), name="A")
    bt.addstrategy(Trend, start=start)
    bt.broker.setcash(100_000)
    ref = bt.run(runonce=False)[0]
    live, strat, venue, _ = _session(frames={"A": frame}, start=start)
    assert live.stopped_by == "bar source finished"
    assert live.live_slices == len(frame) - SPLIT
    assert strat.log == ref.log and len(ref.log) > 50          # same orders, dates, sizes, prices
    assert live.broker.getvalue() == pytest.approx(bt.broker.getvalue(), rel=1e-12)
    # the account the venue keeps agrees with the strategy's book
    assert venue.positions["A"][0] == live.broker.getposition(live.datas[0]).size
    assert venue.cash == pytest.approx(live.broker.getcash(), rel=1e-12)


def test_warmup_sends_nothing_and_indicators_are_warm():
    _live, strat, venue, _ = _session(max_slices=1)
    assert len(strat.sma) > 0 and strat.sma[0] == strat.sma[0]   # computed on the first live bar
    sent = [c for c in venue.commands if c[0] == "place"]
    assert len(sent) == 1                                         # only the live slice's order
    assert all(s[0] >= str(_frame().index[SPLIT].date()) for s in strat.statuses)


def test_lookahead_reads_nothing_live():
    seen = []

    def peek(s):
        seen.append(s.data.close[1])

    _session(Scripted, max_slices=1, script={0: peek})
    assert seen and seen[0] != seen[0]                           # NaN: the next bar is not there


# -- the order life cycle ----------------------------------------------------------------------------

def test_partial_fills_across_bars():
    script = {0: lambda s: s.buy(size=100)}
    live, strat, _, _ = _session(Scripted, script=script, fake={"fill_fraction": 0.4},
                                     engine={"market_tif": "GTC"}, max_slices=6)
    names = [x[1] for x in strat.statuses]
    assert names == ["Submitted", "Accepted", "Partial", "Partial", "Completed"]
    assert [x[2] for x in strat.statuses[2:]] == [40, 80, 100]
    assert live.broker.getposition(live.datas[0]).size == 100


def test_partial_fill_then_expiry_of_an_opening_order():
    script = {0: lambda s: s.buy(size=100)}
    live, strat, _, _ = _session(Scripted, script=script, fake={"fill_fraction": 0.5}, max_slices=3)
    assert [x[1] for x in strat.statuses] == ["Submitted", "Accepted", "Partial", "Expired"]
    assert live.broker.getposition(live.datas[0]).size == 50
    assert live.broker.open_orders() == []


def test_rejection_is_notified_with_its_reason():
    def buy(s):
        s.broker.adapter.reject_next("Order rejected - insufficient permissions", 201)
        return s.buy(size=10)

    _, strat, _, _ = _session(Scripted, script={0: buy}, max_slices=2)
    assert [x[1] for x in strat.statuses] == ["Submitted", "Rejected"]
    assert "permissions" in strat.statuses[-1][3]


def test_cancel_goes_through_the_venue():
    def place(s):
        return s.buy(size=10, exectype=tw.Order.Limit, price=1.0)   # never reached

    def cancel(s):
        assert s.cancel(s.orders[0]) is True

    live, strat, venue, _ = _session(Scripted, script={0: place, 1: cancel}, max_slices=4)
    assert [x[1] for x in strat.statuses] == ["Submitted", "Accepted", "Canceled"]
    assert ("cancel", live.broker.client_id(strat.orders[0])) in venue.commands


def test_dropped_connection_refuses_orders_and_resyncs_missed_fills():
    def buy_then_drop(s):
        order = s.buy(size=10)
        s.broker.adapter.drop_connection()
        return order

    market_time_of_restore = []

    def while_down(s):
        assert not s.broker.connected
        s.buy(size=5)                      # refused locally: not connected
        market_time_of_restore.append(s.broker.adapter.now())
        s.broker.adapter.restore_connection()

    script = {0: buy_then_drop, 1: while_down}
    live, strat, venue, _ = _session(Scripted, script=script, max_slices=4)
    assert [x[1] for x in strat.statuses] == ["Submitted", "Accepted", "Submitted", "Rejected", "Completed"]
    assert "not connected" in strat.statuses[3][3]
    # the fill happened while the link was down; it arrives with the resync snapshot
    assert venue.executions[0].time < market_time_of_restore[0]
    assert live.broker.getposition(live.datas[0]).size == 10 == venue.positions["A"][0]


# -- reconciliation ----------------------------------------------------------------------------------

def test_initial_positions_are_adopted():
    seen = []
    frames = {"A": _frame()}
    market = FakeMarket(frames, frames["A"].index[SPLIT - 1] + 21 * HOUR)
    venue = FakeIB(market, cash=50_000, positions={"A": (100, 42.0), "ZZZ": (7, 1.0)})
    live, _, _, _ = _session(Scripted, venue=venue, market=market, max_slices=1,
                             script={0: lambda s: seen.append((s.position.size, s.position.price,
                                                               s.broker.getcash()))})
    assert seen == [(100, 42.0, 50_000.0)]
    assert live.broker.foreign_positions == {"ZZZ": 7.0}
    assert live.broker.reconciliations[0]["first"] is True


def test_manual_trade_is_adopted_by_default():
    def manual(s):
        s.broker.adapter.external_trade("A", 25, s.data.close[0])

    live, _strat, _, _ = _session(Scripted, script={0: manual}, max_slices=3)
    assert live.broker.getposition(live.datas[0]).size == 25
    assert any(r["positions"] and r["positions"][0]["broker"] == 25 for r in live.broker.reconciliations)


def test_mismatch_halts_under_the_halt_policy(tmp_path):
    def manual(s):
        s.buy(size=3, exectype=tw.Order.Limit, price=1.0)            # a working order to cancel
        s.broker.adapter.external_trade("A", 25, s.data.close[0])

    path = tmp_path / "j.jsonl"
    live, _, venue, _ = _session(Scripted, script={0: manual}, journal=str(path),
                                 engine={"reconcile": "halt"})
    assert live.stopped_by.startswith("kill switch") and "mismatch" in live.broker.killed
    assert live.live_slices == 2
    assert [c[0] for c in venue.commands] == ["place", "cancel"]
    kinds = [r["k"] for r in Journal.load(path)]
    assert "kill" in kinds and kinds[-1] == "end"


# -- guards ------------------------------------------------------------------------------------------

def test_risk_limits_refuse_locally():
    limits = RiskLimits(max_order_quantity=50, allowed_symbols=frozenset({"A"}))
    script = {0: lambda s: s.buy(size=60), 1: lambda s: s.buy(size=40)}
    _live, strat, venue, _ = _session(Scripted, script=script, engine={"limits": limits}, max_slices=3)
    assert strat.statuses[1][1] == "Rejected" and "max_order_quantity" in strat.statuses[1][3]
    assert len([c for c in venue.commands if c[0] == "place"]) == 1
    assert RiskLimits(allow_short=False).check("A", -5, 10.0, 0, 0)
    assert RiskLimits(max_position_notional=100).check("A", 20, 10.0, 0, 0)
    assert RiskLimits(max_order_notional=100).check("A", 20, 10.0, 0, 0)
    assert RiskLimits(max_orders_per_slice=1).check("A", 1, 10.0, 0, 1)
    assert not RiskLimits().check("A", 1e9, 1e9, 0, 10**6)


def test_read_only_sends_nothing():
    live, strat, venue, _ = _session(engine={"read_only": True}, max_slices=30)
    assert venue.commands == []
    assert {x[1] for x in strat.statuses} == {"Submitted", "Rejected"}
    assert live.broker.getposition(live.datas[0]).size == 0


def test_kill_file_stops_the_session_and_cancels(tmp_path):
    kill = tmp_path / "KILL"

    def place(s):
        return s.buy(size=10, exectype=tw.Order.Limit, price=1.0)

    def touch(s):
        kill.write_text("stop")

    live, strat, venue, _ = _session(Scripted, script={0: place, 1: touch},
                                     engine={"kill_switch": KillSwitch(kill)}, journal=str(tmp_path / "j.jsonl"))
    assert live.live_slices == 2 and live.stopped_by.startswith("kill switch")
    assert venue.commands[-1] == ("cancel", live.broker.client_id(strat.orders[0]))
    assert Journal.load(tmp_path / "j.jsonl")[-2]["k"] == "kill"


def test_kill_switch_trip_from_code():
    ks = KillSwitch()
    assert not ks.tripped
    ks.trip("operator")
    ks.trip("again")
    assert ks.tripped and ks.reason == "operator"


# -- journal, replay, resume -------------------------------------------------------------------------

def _uninterrupted(frame):
    live, strat, _venue, _ = _session(frames={"A": frame})
    return strat.log, live.broker.getvalue()


def test_resume_replays_without_sending_and_ends_like_an_uninterrupted_run(tmp_path):
    frame = _frame()
    ref_log, ref_value = _uninterrupted(frame)
    path = str(tmp_path / "session.jsonl")
    market = FakeMarket({"A": frame}, frame.index[SPLIT - 1] + 21 * HOUR)
    venue = FakeIB(market, cash=100_000)
    first, _s1, _, _ = _session(frames={"A": frame}, market=market, venue=venue, journal=path, max_slices=60)
    sent = len(venue.commands)
    assert first.stopped_by == "max_slices" and sent > 0
    # a new process: fresh engine and strategy; the venue (the account) carried on
    venue.disconnect()
    second, s2, _, _ = _session(frames={"A": frame}, market=market, venue=venue, journal=path)
    assert second.replayed_slices == 60
    assert venue.commands[:sent] == venue.commands[:sent] and len(venue.commands) > sent
    assert s2.log == ref_log
    assert second.broker.getvalue() == pytest.approx(ref_value, rel=1e-12)
    # the journal of both runs replays as one session
    records = Journal.load(path)
    assert records[0]["k"] == "session" and sum(r["k"] == "slice" for r in records) == len(frame) - SPLIT


def test_resume_after_a_gap_catches_up_without_trading_stale_bars(tmp_path):
    frame = _frame()
    path = str(tmp_path / "s.jsonl")
    market = FakeMarket({"A": frame}, frame.index[SPLIT - 1] + 21 * HOUR)
    venue = FakeIB(market, cash=100_000)
    _session(frames={"A": frame}, market=market, venue=venue, journal=path, max_slices=10)
    venue.disconnect()
    venue.idle(7 * 24 * 3600)                     # a week down
    before = len(venue.commands)
    second, _, _, _ = _session(frames={"A": frame}, market=market, venue=venue, journal=path, max_slices=5)
    slices = [r for r in Journal.load(path) if r["k"] == "slice"][10:]
    assert [r["stale"] for r in slices] == [True, True, True, True, False]   # five bars missed
    assert second.live_slices == 5 and len(venue.commands) - before <= 1      # only the last trades


def test_replay_detects_a_diverging_journal(tmp_path):
    path = tmp_path / "s.jsonl"
    market = FakeMarket({"A": _frame()}, _frame().index[SPLIT - 1] + 21 * HOUR)
    venue = FakeIB(market, cash=100_000)
    _session(market=market, venue=venue, journal=str(path), max_slices=40)
    lines = path.read_text().splitlines()
    k = next(i for i, line in enumerate(lines) if json.loads(line)["k"] == "cmd")
    record = json.loads(lines[k])
    record["payload"]["quantity"] += 1
    lines[k] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")
    venue.disconnect()
    with pytest.raises(LiveDivergence):
        _session(market=market, venue=venue, journal=str(path))


def test_journal_drops_a_torn_last_line(tmp_path):
    path = tmp_path / "j.jsonl"
    j = Journal(path, fsync=False)
    j.write("note", x=1)
    j.write("note", x=2.5)
    j.close()
    with open(path, "a") as fh:
        fh.write('{"k":"note","x":')
    assert [r["x"] for r in Journal.load(path)] == [1, 2.5]
    j = Journal(path, fsync=False)
    j.write("note", x=3)
    j.close()
    assert [r["x"] for r in Journal.load(path)] == [1, 2.5, 3]


def test_messages_round_trip_through_json():
    snap = AccountSnapshot(1000.5, 2000.25, "USD", (("A", 10.0, 1.1),), (BrokerOrder("c-1", "A", 5.0, 2.0),),
                           (Execution("c-1", "e1", "A", 2.0, 0.1 + 0.2, 1.0, 7),), True, 9)
    for message in (snap, Ack("c", "1", 3), Canceled("c", "x", True, 4), Rejected("c", "r", 201, 5),
                    Notice("n", 2104), OrderRequest("c", "A", -3.0, "LMT", 1.5, None, "GTC")):
        assert decode(json.loads(json.dumps(encode(message)))) == message


# -- the clock ---------------------------------------------------------------------------------------

class _Source:
    def __init__(self, bars_by_symbol, delay):
        self.bars = bars_by_symbol
        self.delay = delay

    def poll(self, symbol, after, now):
        return [b for b in self.bars[symbol] if after < b.time <= now - self.delay.get((symbol, b.time), 0)]


def test_clock_waits_for_every_feed_then_times_out():
    day = 86_400 * 10**9
    b = {s: [BarRecord(t * day, 1, 1, 1, 1) for t in (1, 2, 3)] for s in ("X", "Y")}
    b["Y"] = [x for x in b["Y"] if x.time != 2 * day]            # Y has a holiday on day 2
    now = [0]
    clock = LiveClock(["X", "Y"], _Source(b, {("X", 3 * day): 0, ("Y", 3 * day): 10 * day}), [0, 0],
                      now=lambda: now[0], idle=lambda s: now.__setitem__(0, now[0] + int(s * 1e9)),
                      poll_interval=3600, assemble_timeout=2 * 86_400, finished=lambda t: t > 20 * day)
    ticks = list(clock)
    assert [t.time // day for t in ticks] == [1, 2, 3]
    assert set(ticks[1].bars) == {0}                             # day 2: X only, once Y moved past it
    assert set(ticks[2].bars) == {0}                             # day 3: Y too late, timed out
    assert clock.late and clock.late[0][0] == "Y"


def test_clock_flags_all_but_the_last_settled_time_as_stale():
    day = 86_400 * 10**9
    b = {"X": [BarRecord(t * day, 1, 1, 1, 1) for t in (1, 2, 3)]}
    clock = LiveClock(["X"], _Source(b, {}), [0], now=lambda: 10 * day, idle=lambda s: None,
                      finished=lambda t: True)
    assert [t.stale for t in clock] == [True, True, False]


# -- pacing ------------------------------------------------------------------------------------------

def test_rate_limiter_sliding_window():
    t = [0.0]
    waits = []

    def sleep(s):
        waits.append(s)
        t[0] += s

    lim = RateLimiter(3, 10.0, now=lambda: t[0], sleep=sleep)
    assert [lim.acquire() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert lim.acquire() == pytest.approx(10.0)
    t[0] += 4
    assert lim.wait_time() == 0.0
    keyed = KeyedInterval(15.0, now=lambda: t[0], sleep=sleep)
    keyed.acquire("SPY")
    t[0] += 5
    assert keyed.acquire("SPY") == pytest.approx(10.0) and keyed.acquire("TLT") == 0.0


# -- order mapping -----------------------------------------------------------------------------------

def _order(exectype=None, size=10, price=None, plimit=None, valid=None):
    data = types.SimpleNamespace(_name="A")
    return tw.Order(None, data, size, price=price, plimit=plimit, exectype=exectype, valid=valid)


def test_order_requests_for_each_order_type():
    assert order_request(_order(), "c", "A") == OrderRequest("c", "A", 10.0, "MKT", tif="OPG")
    assert order_request(_order(size=-4), "c", "A", market_tif="DAY").tif == "DAY"
    lim = order_request(_order(tw.Order.Limit, price=9.5), "c", "A")
    assert (lim.order_type, lim.limit_price, lim.tif) == ("LMT", 9.5, "GTC")
    stp = order_request(_order(tw.Order.StopLimit, price=11, plimit=11.5, valid=dt.date(2026, 12, 1)), "c", "A")
    assert (stp.order_type, stp.stop_price, stp.limit_price, stp.tif) == ("STP LMT", 11.0, 11.5, "GTD")
    assert order_request(_order(tw.Order.Stop, price=8, valid=0), "c", "A").tif == "DAY"
    assert order_request(_order(tw.Order.Close), "c", "A").order_type == "MOC"
    assert order_request(_order(tw.Order.StopTrail), "c", "A") is None


def test_ib_order_fields():
    f = ib_order_fields(OrderRequest("tw-x-000001", "SPY", -12.0, "STP LMT", 99.5, 100.0, "GTD",
                                     int(pd.Timestamp("2026-12-01 21:00", tz="UTC").value)), account="DU123")
    assert f == {"action": "SELL", "totalQuantity": 12.0, "orderType": "STP LMT", "tif": "GTD",
                 "orderRef": "tw-x-000001", "outsideRth": False, "transmit": True, "lmtPrice": 99.5,
                 "auxPrice": 100.0, "goodTillDate": "20261201 21:00:00 UTC", "account": "DU123"}
    assert ib_order_fields(OrderRequest("c", "SPY", 3.0, "MKT", tif="OPG"))["action"] == "BUY"


def test_ib_status_mapping():
    kw = {"acked": False, "cancel_asked": False, "tif": "OPG"}
    assert isinstance(status_message("c", "PreSubmitted", **kw), Ack)
    assert status_message("c", "Submitted", **{**kw, "acked": True}) is None
    assert status_message("c", "PendingSubmit", **kw) is None and status_message("c", "Filled", **kw) is None
    rej = status_message("c", "Cancelled", **kw, reason="Error 201: no permission", code=201)
    assert isinstance(rej, Rejected) and rej.code == 201
    exp = status_message("c", "Cancelled", **{**kw, "acked": True})
    assert isinstance(exp, Canceled) and exp.expired
    asked = status_message("c", "Cancelled", **{**kw, "acked": True, "cancel_asked": True})
    assert isinstance(asked, Canceled) and not asked.expired
    assert not status_message("c", "Cancelled", **{**kw, "acked": True, "tif": "GTC"}).expired
    assert isinstance(status_message("c", "Inactive", **kw), Rejected)
    assert isinstance(status_message("c", "ValidationError", **kw), Notice)


# -- the IB adapter against a stub of ib_async's interface -------------------------------------------

class _Event:
    def __init__(self):
        self.handlers = []

    def connect(self, handler):
        self.handlers.append(handler)

    def emit(self, *args):
        for h in self.handlers:
            h(*args)


class _StubIB:
    def __init__(self, accounts=("DU1234567",)):
        for name in ("orderStatusEvent", "execDetailsEvent", "commissionReportEvent", "errorEvent",
                     "disconnectedEvent"):
            setattr(self, name, _Event())
        self.accounts = list(accounts)
        self.connected = False
        self.placed = []
        self.cancelled = []
        self.trades = []
        self.history = []

    def connect(self, host, port, clientId, readonly=False, account=""):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def isConnected(self):
        return self.connected

    def managedAccounts(self):
        return self.accounts

    def qualifyContracts(self, contract):
        return [contract]

    def placeOrder(self, contract, order):
        trade = types.SimpleNamespace(contract=contract, order=order, log=[],
                                      orderStatus=types.SimpleNamespace(status="PendingSubmit", filled=0.0))
        self.placed.append(trade)
        self.trades.append(trade)
        return trade

    def openTrades(self):
        return [t for t in self.trades if t.orderStatus.status not in ("Filled", "Cancelled")]

    def cancelOrder(self, order):
        self.cancelled.append(order.orderRef)

    def accountValues(self, account=""):
        V = types.SimpleNamespace
        return [V(tag="TotalCashValue", value="1234.5", currency="USD"),
                V(tag="NetLiquidation", value="9999", currency="USD"),
                V(tag="TotalCashValue", value="1", currency="EUR")]

    def positions(self, account=""):
        return [types.SimpleNamespace(contract=types.SimpleNamespace(symbol="SPY"), position=3.0, avgCost=400.0)]

    def fills(self):
        return []

    def reqHistoricalData(self, *args, **kwargs):
        self.history.append(args)
        return self.bars

    def sleep(self, seconds):
        pass


def _ib_module():
    return types.SimpleNamespace(Order=lambda **kw: types.SimpleNamespace(permId=0, orderId=1, **kw),
                                 Stock=lambda s, e, c: types.SimpleNamespace(symbol=s, exchange=e, currency=c))


def test_ib_adapter_refuses_anything_but_paper():
    with pytest.raises(tw.live.AdapterError):
        IBAdapter(port=7496, ib=_StubIB(), ib_module=_ib_module()).connect()
    stub = _StubIB(accounts=("U7654321",))
    with pytest.raises(tw.live.AdapterError):
        IBAdapter(ib=stub, ib_module=_ib_module()).connect()
    assert not stub.connected
    IBAdapter(ib=_StubIB(accounts=("U7654321",)), ib_module=_ib_module(), require_paper=False).connect()


def test_ib_adapter_turns_callbacks_into_messages():
    stub = _StubIB()
    adapter = IBAdapter(ib=stub, ib_module=_ib_module())
    adapter.connect()
    assert adapter.account == "DU1234567"
    adapter.place(OrderRequest("tw-s-000001", "SPY", 5.0, "MKT", tif="OPG"))
    trade = stub.placed[0]
    assert (trade.order.orderRef, trade.order.action, trade.order.account) == ("tw-s-000001", "BUY", "DU1234567")
    trade.orderStatus.status = "Submitted"
    stub.orderStatusEvent.emit(trade)
    fill = types.SimpleNamespace(contract=types.SimpleNamespace(symbol="SPY"), time=dt.datetime(2026, 11, 16, 14, 30,
                                                                                               tzinfo=dt.timezone.utc),
                                 execution=types.SimpleNamespace(orderRef="tw-s-000001", execId="0001.01", side="BOT",
                                                                 shares=5.0, price=600.25))
    stub.execDetailsEvent.emit(trade, fill)
    stub.commissionReportEvent.emit(trade, fill, types.SimpleNamespace(commission=1.0))
    trade.orderStatus.status = "Filled"
    stub.orderStatusEvent.emit(trade)
    stub.errorEvent.emit(-1, 2104, "Market data farm connection is OK", None)
    stub.errorEvent.emit(-1, 1100, "Connectivity between IB and TWS has been lost", None)
    out = adapter.drain()
    assert [type(m).__name__ for m in out] == ["Ack", "Execution", "Connection"]
    ex = out[1]
    assert (ex.client_id, ex.quantity, ex.price, ex.commission) == ("tw-s-000001", 5.0, 600.25, 1.0)
    assert ex.time == int(pd.Timestamp("2026-11-16 14:30", tz="UTC").value)
    # an order error before any acknowledgement: ib_async reports it as Cancelled
    adapter.place(OrderRequest("tw-s-000002", "SPY", -5.0, "LMT", 1.0, tif="GTC"))
    bad = stub.placed[1]
    bad.orderStatus.status = "Cancelled"
    bad.log.append(types.SimpleNamespace(message="Error 201, reqId 2: Order rejected", errorCode=201))
    stub.orderStatusEvent.emit(bad)
    stub.orderStatusEvent.emit(bad)                                # reported once
    (rej,) = adapter.drain()
    assert isinstance(rej, Rejected) and rej.code == 201
    snap = adapter.snapshot()
    assert (snap.cash, snap.net_liquidation, snap.positions) == (1234.5, 9999.0, (("SPY", 3.0, 400.0),))
    stub.disconnect()
    stub.disconnectedEvent.emit()
    adapter.place(OrderRequest("tw-s-000003", "SPY", 1.0))
    msgs = adapter.drain()
    assert [type(m).__name__ for m in msgs] == ["Connection", "Rejected"] and msgs[1].code == 504


def test_ib_bars_never_pass_a_forming_bar_and_pace_requests():
    stub = _StubIB()
    adapter = IBAdapter(ib=stub, ib_module=_ib_module())
    adapter.connect()
    t, waited = [0.0], []
    adapter.history_interval = KeyedInterval(15.0, now=lambda: t[0], sleep=waited.append)
    source = IBBars(adapter)
    B = types.SimpleNamespace
    stub.bars = [B(date=dt.date(2026, 11, d), open=1, high=2, low=0.5, close=1.5, volume=10) for d in (12, 13, 16)]
    ny = source.tz
    during = int(dt.datetime(2026, 11, 16, 12, 0, tzinfo=ny).timestamp() * 1e9)
    after = int(dt.datetime(2026, 11, 16, 16, 20, tzinfo=ny).timestamp() * 1e9)
    got = source.poll("SPY", 0, during)
    assert [dt.datetime.fromtimestamp(b.time / 1e9, dt.timezone.utc).day for b in got] == [12, 13]
    assert source.poll("SPY", got[-1].time, during) == []              # nothing due: no request
    assert len(stub.history) == 1
    got = source.poll("SPY", got[-1].time, after)
    assert [b.close for b in got] == [1.5] and len(stub.history) == 2
    assert waited == [15.0]                                        # IB: no identical request within 15 s
    assert source.due(after) == dt.date(2026, 11, 16)
    assert source.due(int(dt.datetime(2026, 11, 15, 12, tzinfo=ny).timestamp() * 1e9)) == dt.date(2026, 11, 13)


def test_ib_async_builds_the_mapped_order():
    ib_async = pytest.importorskip("ib_async")
    order = ib_async.Order(**ib_order_fields(OrderRequest("tw-x-1", "SPY", -3.0, "LMT", 500.0, tif="GTC"), "DU1"))
    assert (order.action, order.totalQuantity, order.orderType, order.lmtPrice, order.orderRef, order.account) == \
        ("SELL", 3.0, "LMT", 500.0, "tw-x-1", "DU1")
    adapter = IBAdapter(ib=ib_async.IB())                      # no connection: the events exist
    adapter._hook()
    assert adapter._hooked


# -- the backtest is untouched -----------------------------------------------------------------------

def test_the_engine_does_not_import_live():
    code = "import sys, tradewind; assert 'tradewind.live' not in sys.modules; assert 'ib_async' not in sys.modules"
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src") + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


def test_live_example_runs_on_the_fake_venue(capsys, monkeypatch):
    import runpy

    monkeypatch.chdir(os.path.join(ROOT, "docs", "examples"))
    monkeypatch.setattr(sys, "argv", ["live_ibkr_paper.py", "--fake"])
    runpy.run_path(os.path.join(ROOT, "docs", "examples", "live_ibkr_paper.py"), run_name="__main__")
    out = capsys.readouterr().out
    assert "bar source finished" in out and "final value" in out


def test_an_execution_after_the_final_status_moves_the_book_not_the_status():
    def place(s):
        return s.buy(size=10, exectype=tw.Order.Limit, price=1.0)

    def cancel_then_late_fill(s):
        order = s.orders[0]
        s.cancel(order)
        cid = s.broker.client_id(order)
        s.broker.adapter._book("A", 4.0, 1.0, 0.0)          # the venue's account has it too
        s.broker.adapter._queue.append(Execution(cid, "late.1", "A", 4.0, 1.0, 0.0, 0))

    live, strat, _, _ = _session(Scripted, script={0: place, 1: cancel_then_late_fill}, max_slices=4)
    assert [x[1] for x in strat.statuses] == ["Submitted", "Accepted", "Canceled"]
    assert strat.orders[0].executed.size == 4 and live.broker.getposition(live.datas[0]).size == 4
    assert live.broker.notices and "late.1" in live.broker.notices[0]
