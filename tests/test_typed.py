"""The typed API (spec/08 § 2): same engine, same NAV bit for bit as the compatible API."""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import sys

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.clock import UniverseChange as EngineUniverseChange

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "examples"))

from _data import bars

SYMBOLS = ("AAA", "BBB", "CCC")


def _frames(years=2.0):
    return {s: bars(s, seed=40 + i, years=years) for i, s in enumerate(SYMBOLS)}


def _engine(frames, commission=None):
    engine = tw.Engine(stdstats=False)
    for name, frame in frames.items():
        engine.adddata(tw.feeds.PandasData(dataname=frame), name=name)
    engine.broker.setcash(1_000_000)
    if commission is not None:
        engine.broker.setcommission(commission=commission)
    return engine


def _record(engine):
    """Every phase-8 mark as a hex float, and every order's statuses and execution."""
    broker = engine.broker
    marks = []
    mark = type(broker).mark

    def recording_mark(self, s):
        mark(self, s)
        marks.append(float(self._value).hex())

    broker.mark = recording_mark.__get__(broker)
    return marks


def _orders(broker):
    orders = sorted(broker.orders, key=lambda o: o.ref)
    base = orders[0].ref if orders else 0
    return [(o.ref - base, o.data._name, tuple(int(s) for s in o._status_log), float(o.executed.size).hex(),
             float(o.executed.price).hex(), float(o.executed.comm).hex()) for o in orders]


def _run(cls, frames, commission=None, **params):
    engine = _engine(frames, commission)
    marks = _record(engine)
    engine.addstrategy(cls, **params)
    strat = engine.run()[0]
    return marks, _orders(engine.broker), strat


# -- one strategy, written both ways ---------------------------------------------------------------
class Compat(tw.Strategy):
    params = (("period", 30), ("weight", 0.3), ("listen", True))

    def __init__(self):
        self.smas = {d._name: tw.ind.SMA(d.close, period=self.p.period) for d in self.datas}
        self.add_timer(when=tw.Timer.SESSION_START, monthdays=[1], monthcarry=True)
        self.fills = 0
        self.stopped = False

    def notify_timer(self, timer, when, *args, **kwargs):
        if len(self.smas["AAA"]) < self.p.period:
            return
        for d in self.datas:
            above = d.close[0] > self.smas[d._name][0]
            self.order_target_percent(d, target=self.p.weight if above else 0.0)

    def next(self):
        d = self.getdatabyname("CCC")
        if len(self) % 37 == 0 and not self.getposition(d).size:
            self.buy(d, size=50, price=d.close[0] * 0.99, exectype=tw.Order.Limit)
        if len(self) % 101 == 0:
            self.close(d)

    def notify_order(self, order):
        if order.status == order.Completed:
            self.fills += 1

    def stop(self):
        self.stopped = True


@dataclasses.dataclass(frozen=True)
class TrendParams:
    period: int = 30
    weight: float = 0.3


class TypedQuiet(tw.TypedStrategy[TrendParams]):
    """Without ``on_fill``: the engine does not build the order events."""

    def initialize(self) -> None:
        self.smas = {s: self.indicator(tw.ind.SMA, s, period=self.params.period) for s in self.symbols}
        self.schedule("rebalance", month_days=(1,))
        self.fills: list[tw.Fill] = []
        self.ended = False

    def on_timer(self, event: tw.TimerEvent) -> None:
        if len(self.smas["AAA"]) < self.params.period:
            return
        for s in self.symbols:
            above = self.bars[s].close[0] > self.smas[s][0]
            self.order_target_weight(s, self.params.weight if above else 0.0)

    def on_bar(self, bar: tw.BarEvent) -> None:
        ccc = self.bars["CCC"]
        if (bar.index + 1) % 37 == 0 and not self.portfolio["CCC"].quantity:
            self.limit_order("CCC", 50, ccc.close[0] * 0.99)
        if (bar.index + 1) % 101 == 0:
            self.liquidate("CCC")

    def on_end(self) -> None:
        self.ended = True


class Typed(TypedQuiet):
    def on_fill(self, fill: tw.Fill) -> None:
        self.fills.append(fill)


@pytest.mark.parametrize("commission", [None, 0.001])
def test_same_nav_bit_for_bit_both_ways(commission):
    frames = _frames()
    marks_c, orders_c, compat = _run(Compat, frames, commission)
    marks_t, orders_t, host = _run(Typed, frames, commission)
    marks_q, orders_q, _ = _run(TypedQuiet, frames, commission)
    assert len(marks_c) == len(frames["AAA"])
    assert marks_t == marks_c and marks_q == marks_c
    assert orders_t == orders_c and orders_q == orders_c
    assert len(orders_c) > 20
    assert compat.fills == len(host.typed.fills) > 0
    assert compat.stopped and host.typed.ended
    # without ``on_fill`` the host does not define ``notify_order``: no order event is built
    assert "notify_order" not in vars(TypedQuiet._tw_host_class())
    assert "notify_order" in vars(Typed._tw_host_class())


def test_parameters_from_kwargs_and_from_an_instance():
    frames = _frames(0.5)
    _, _, host = _run(Typed, frames, period=10)
    assert host.typed.params == TrendParams(period=10)
    _, _, host = _run(Typed, frames, params=TrendParams(period=12, weight=0.5), weight=0.4)
    assert host.typed.params == TrendParams(period=12, weight=0.4)
    with pytest.raises(TypeError):
        _run(Typed, frames, nope=1)
    with pytest.raises(TypeError):
        _run(Typed, frames, params={"period": 3})


def test_no_params_and_explicit_params_type():
    class Bare(tw.TypedStrategy):
        pass

    @dataclasses.dataclass
    class Q:
        x: int = 1

    class Explicit(Bare):
        params_type = Q

    frames = {"AAA": bars("AAA", seed=1, years=0.2)}
    assert _run(Bare, frames)[2].typed.params == tw.NoParams()
    assert _run(Explicit, frames, x=3)[2].typed.params == Q(3)


def test_series_reads_the_past_only():
    seen = {}

    class S(tw.TypedStrategy):
        def on_bar(self, bar):
            close = self.bars["AAA"].close
            if bar.index == 5:
                seen["now"] = close[0]
                seen["prev"] = close[-1]
                seen["hist"] = close.history(3)
                seen["len"] = len(close)
                for bad in (1, -6):
                    with pytest.raises(IndexError):
                        close[bad]
                seen["symbols"] = bar.symbols
                seen["time"] = bar.time

    frame = bars("AAA", seed=2, years=0.1)
    _run(S, {"AAA": frame})
    assert seen["now"] == frame["close"].iloc[5] and seen["prev"] == frame["close"].iloc[4]
    assert list(seen["hist"]) == list(frame["close"].iloc[3:6]) and seen["len"] == 6
    assert seen["symbols"] == ("AAA",) and seen["time"] == frame.index[5].to_pydatetime()


def test_portfolio_tickets_and_order_events():
    log = {"updates": [], "fills": []}

    class S(tw.TypedStrategy):
        def on_bar(self, bar):
            if bar.index == 0:
                log["ticket"] = self.market_order("AAA", 10)
                assert self.market_order("AAA", 0) is None
            if bar.index == 2:
                holding = self.portfolio["AAA"]
                log["holding"] = holding
                log["value"] = self.portfolio.value
                log["cash"] = self.portfolio.cash
                log["held"] = list(self.portfolio)
                log["bbb"] = self.portfolio["BBB"]
                log["sell"] = self.market_order("AAA", -4)
            if bar.index == 3:
                log["margin"] = self.market_order("BBB", 1e9)

        def on_order_event(self, update):
            log["updates"].append((update.symbol, update.status))

        def on_fill(self, fill):
            log["fills"].append((fill.symbol, fill.quantity, fill.price))

    frames = {"AAA": bars("AAA", seed=3, years=0.1), "BBB": bars("BBB", seed=4, years=0.1)}
    _run(S, frames)
    ticket = log["ticket"]
    assert ticket.status is tw.OrderStatus.COMPLETED and ticket.filled_quantity == 10
    assert ticket.average_fill_price == frames["AAA"]["open"].iloc[1]
    h = log["holding"]
    assert h.quantity == 10 and h.is_long and h.average_price == ticket.average_fill_price
    assert h.market_value == pytest.approx(10 * frames["AAA"]["close"].iloc[2])
    assert log["value"] == pytest.approx(log["cash"] + h.market_value)
    assert log["held"] == ["AAA"] and log["bbb"].quantity == 0
    assert log["sell"].quantity == -4 and log["sell"].status is tw.OrderStatus.COMPLETED
    assert log["margin"].status is tw.OrderStatus.MARGIN
    S_, A, C, M = (tw.OrderStatus.SUBMITTED, tw.OrderStatus.ACCEPTED, tw.OrderStatus.COMPLETED,
                   tw.OrderStatus.MARGIN)
    assert log["updates"] == [("AAA", S_), ("AAA", A), ("AAA", C), ("AAA", S_), ("AAA", A), ("AAA", C),
                              ("BBB", S_), ("BBB", M)]
    assert log["fills"] == [("AAA", 10.0, frames["AAA"]["open"].iloc[1]),
                            ("AAA", -4.0, frames["AAA"]["open"].iloc[3])]


def test_warmup_then_bars_and_securities():
    calls = []

    class S(tw.TypedStrategy):
        def initialize(self):
            self.sma = self.indicator(tw.ind.SMA, self.bars["AAA"].close, period=5)

        def on_warmup(self, bar):
            calls.append(("warmup", bar.index, self.is_warming_up))

        def on_bar(self, bar):
            calls.append(("bar", bar.index, self.is_warming_up))

    _run(S, {"AAA": bars("AAA", seed=5, years=0.05)})
    assert calls[:5] == [("warmup", i, True) for i in range(4)] + [("bar", 4, False)]


def test_universe_and_fingerprint():
    changes = []

    class Provider:
        def change(self, time):
            day = pd.Timestamp(int(time)).date() if isinstance(time, (int, np.integer)) else None
            if day == dt.date(2015, 1, 7):
                return EngineUniverseChange(time, added=(1,), removed=(0,))
            return None

    class S(tw.TypedStrategy[TrendParams]):
        def on_universe_change(self, change):
            changes.append(change)

    frames = {"AAA": bars("AAA", seed=6, years=0.05), "BBB": bars("BBB", seed=7, years=0.05)}
    engine = _engine(frames)
    engine.set_universe(Provider())
    engine.addstrategy(S, period=7)
    engine.run()
    assert [(c.added, c.removed) for c in changes] == [(("BBB",), ("AAA",))]
    assert changes[0].time.date() == dt.date(2015, 1, 7)
    entry = engine.fingerprint.strategies[0]
    assert entry["class"].endswith("test_universe_and_fingerprint.<locals>.S")
    assert entry["params"] == {"period": 7, "weight": 0.3}


def test_typed_strategy_has_no_metaclass_and_the_package_is_typed():
    assert type(tw.TypedStrategy) is type and type(Typed) is type
    root = os.path.dirname(tw.__file__)
    assert os.path.exists(os.path.join(root, "py.typed"))
