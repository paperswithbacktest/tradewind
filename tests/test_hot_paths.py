"""The speed paths give the general paths' floats, bit for bit (spec/05 § 7.3).

Each shortcut of the engine -- one cursor per feed, indicators following their clock's
cursor, notifications built only when someone listens, the default fill / fee /
settlement models called without their wrappers -- is checked here against the path it
short-circuits, on the same bars: every mark of the run compared as a hex float.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.analyzer import Analyzer
from tradewind.models import CommInfoFeeModel, ImmediateSettlement, NextBarFillModel


def frames(n_sym=12, n_bars=160, seed=7):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2020-01-01", periods=n_bars)
    out = []
    for j in range(n_sym):
        close = 40 * np.exp(np.cumsum(rng.normal(0.0002, 0.02, n_bars)))
        openp = close * (1 + rng.normal(0, 0.004, n_bars))
        high = np.maximum(openp, close) * 1.004
        low = np.minimum(openp, close) * 0.996
        late = 15 * (j % 4 == 3)               # some names list late
        out.append(pd.DataFrame({"open": openp[late:], "high": high[late:], "low": low[late:],
                                 "close": close[late:], "volume": 1e5}, index=index[late:]))
    return out


class Rotate(tw.Strategy):
    """Weekly into the names above their SMA(10), out of the others; a short leg and a
    commission make reversals, partial closes and refusals happen."""

    def __init__(self):
        self.sma = [tw.indicators.SMA(d, period=10) for d in self.datas]
        self.seen = []

    def next(self):
        self.seen.append(tuple((len(d), s[0], len(s)) for d, s in zip(self.datas, self.sma)))
        if len(self) % 5:
            return
        for i, (d, s) in enumerate(zip(self.datas, self.sma)):
            if not len(d) or s[0] != s[0]:
                continue
            if d.close[0] > s[0]:
                self.order_target_percent(d, target=0.15)
            elif i % 3 == 0:
                self.order_target_percent(d, target=-0.05)
            else:
                self.order_target_percent(d, target=0.0)


class RotateListening(Rotate):
    def __init__(self):
        super().__init__()
        self.events = []

    def notify_order(self, order):
        self.events.append((order.data._name, order.status, float(order.executed.price).hex()))

    def notify_trade(self, trade):
        self.events.append(("trade", trade.isclosed))


def run(strategy, setup=None, commission=0.001, cash=100_000, **runkw):
    engine = tw.Engine(stdstats=False)
    for i, f in enumerate(frames()):
        engine.adddata(tw.feeds.PandasData(dataname=f), name=f"S{i}")
    engine.broker.set_cash(cash)
    engine.broker.setcommission(commission=commission)
    if setup is not None:
        setup(engine)
    marks = []
    mark = type(engine.broker).mark

    def recording(self, s):
        mark(self, s)
        marks.append(float(self._value).hex())

    engine.broker.mark = recording.__get__(engine.broker)
    engine.addstrategy(strategy)
    strat = engine.run(**runkw)[0]
    orders = [(o.status, float(o.executed.size).hex(), float(o.executed.price).hex(),
               float(o.executed.comm).hex()) for o in engine.broker.orders]
    return marks, orders, strat, engine


def test_a_feeds_lines_share_one_cursor_moved_once_per_bar():
    seen = {}

    class Probe(tw.Strategy):
        def next(self):
            d = self.data
            cursors = {id(line._c) for line in d.lines}
            seen.setdefault("cursors", cursors)
            assert len(d) == len(d.close) == len(d.datetime) == d.lines.datetime.idx + 1
            assert d.close[0] == d.close.array[len(d) - 1]

    _, _, strat, _ = run(Probe)
    assert len(seen["cursors"]) == 1
    assert len(strat.data) == strat.data.buflen()


def test_indicators_follow_their_clock_without_being_stepped():
    _, _, strat, engine = run(Rotate)
    for d, s in zip(strat.datas, strat.sma):
        assert s.lines.sma._c is d.lines.close._c
    assert engine._tw_syncs == []
    # every bar read the indicator's value at its feed's current bar
    for row in strat.seen:
        for (n, value, n_ind), s in zip(row, strat.sma):
            assert n_ind == n
            expected = float(s.lines.sma.array[n - 1] if n else s.lines.sma.array[-1])
            assert repr(value) == repr(expected)


def test_live_indicators_are_still_stepped():
    _, _, strat, engine = run(Rotate, runonce=False)
    # built-ins are vectorised (``once``): they follow the clock even with runonce off,
    # since their sync would only copy the cursor; nothing is stepped
    assert all(not s._tw_steps for s in strat.sma)

    class Stepped(tw.Indicator):
        lines = ("x",)

        def next(self):
            self.lines.x[0] = self.data.close[0] * 2.0

    class UsesStepped(tw.Strategy):
        def __init__(self):
            self.ind = Stepped(self.data)
            self.rows = []

        def next(self):
            self.rows.append((self.ind[0], self.data.close[0]))

    _, _, strat, engine = run(UsesStepped, runonce=False)
    assert engine._tw_syncs == [strat.ind]
    assert all(x == 2.0 * c for x, c in strat.rows)


def test_quiet_run_gives_the_listening_runs_floats():
    quiet_marks, quiet_orders, _, quiet_engine = run(Rotate)
    loud_marks, loud_orders, loud, _ = run(RotateListening)
    assert loud.events, "the listening strategy heard its orders"
    assert quiet_marks == loud_marks
    assert quiet_orders == loud_orders
    # reset after the run: a later notify is published as usual
    assert quiet_engine.broker._tw_quiet_orders is False
    assert quiet_engine.broker._tw_quiet_trades is False


def test_quiet_run_builds_no_event():
    published = []

    def setup(engine):
        bus = engine.broker.bus
        publish = bus.publish

        def counting(event):
            published.append(type(event).__name__)
            publish(event)

        bus.publish = counting

    run(Rotate, setup=setup)
    assert published == []
    run(RotateListening, setup=setup)
    assert "OrderEvent" in published and "TradeEvent" in published


@pytest.mark.parametrize("hook", ["notify_order", "notify_trade"])
def test_an_analyzer_that_listens_gets_every_notification(hook):
    class Counter(Analyzer):
        def start(self):
            self.n = 0

        def get_analysis(self):
            return {"n": self.n}

    def count(self, item):
        self.n += 1

    setattr(Counter, hook, count)

    class WithCounter(Rotate):
        pass

    def setup(engine):
        engine.addanalyzer(Counter, _name="counter")

    _, _, strat, _ = run(WithCounter, setup=setup)
    assert strat.analyzers.counter.get_analysis()["n"] > 0


def test_a_journal_keeps_the_events():
    def setup(engine):
        engine.broker.bus.journal = []

    _, _, _, engine = run(Rotate, setup=setup)
    assert any(type(e).__name__ == "OrderEvent" for e in engine.broker.bus.journal)


class _GeneralFill(NextBarFillModel):
    """The same model, not the same type: the broker takes the general path."""

    __slots__ = ()


class _GeneralFee(CommInfoFeeModel):
    __slots__ = ()


class _GeneralSettlement(ImmediateSettlement):
    __slots__ = ()


@pytest.mark.parametrize("commission", [0.0, 0.001])
def test_default_model_shortcuts_equal_the_general_path(commission):
    def general(engine):
        for d in engine.datas:
            engine.broker.set_models(d, fill=_GeneralFill(), fee=_GeneralFee(),
                                     settlement=_GeneralSettlement())

    fast = run(RotateListening, commission=commission)
    slow = run(RotateListening, setup=general, commission=commission)
    assert fast[0] == slow[0]
    assert fast[1] == slow[1]
    assert fast[2].events == slow[2].events
    assert any(status == tw.Order.Margin for status, *_ in fast[1]) or commission == 0.0


def test_cheat_on_close_and_slippage_keep_their_paths():
    def coc(engine):
        engine.broker.set_coc(True)

    def slip(engine):
        engine.broker.set_slippage_perc(0.001, slip_open=True)

    for setup in (coc, slip):
        def general(engine, setup=setup):
            setup(engine)
            for d in engine.datas:
                engine.broker.set_models(d, fill=_GeneralFill(engine.broker.models.fill.coc),
                                         slippage=engine.broker.models.slippage)

        assert run(Rotate, setup=setup)[:2] == run(Rotate, setup=general)[:2]
