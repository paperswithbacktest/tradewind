"""The semantics fixed by the parity pass on pwb-toolbox 0.1.51 (spec/02, "parity 0.1.51"),
pinned without the oracle; the probes of ``tests/oracle/strategies.py`` hold them against it."""

import numpy as np
import pandas as pd

import tradewind as tw


def frame(close, open_=None, high=None, low=None):
    n = len(close)
    close = np.asarray(close, dtype=float)
    open_ = close if open_ is None else np.asarray(open_, dtype=float)
    high = np.maximum(open_, close) + 1.0 if high is None else np.asarray(high, dtype=float)
    low = np.minimum(open_, close) - 1.0 if low is None else np.asarray(low, dtype=float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1e5},
                        index=pd.bdate_range("2021-01-04", periods=n))


def run(cls, frames, cash=100_000.0, setup=None):
    engine = tw.Engine(stdstats=False)
    for name, f in frames.items():
        engine.adddata(tw.feeds.PandasData(dataname=f), name=name)
    engine.addstrategy(cls)
    engine.broker.set_cash(cash)
    if setup:
        setup(engine.broker)
    return engine.run()[0], engine.broker


def test_set_slippage_perc_alone_slips_at_the_open_inside_the_bar():
    fills = []

    class S(tw.Strategy):
        def next(self):
            if len(self) == 1:
                self.buy(size=10)
            if len(self) == 3:
                self.sell(size=10)

        def notify_order(self, order):
            if order.status == order.Completed:
                fills.append(order.executed.price)

    f = frame([100.0] * 5, open_=[100.0] * 5, high=[100.5] * 5, low=[95.0] * 5)
    run(S, {"A": f}, setup=lambda b: b.set_slippage_perc(0.01))
    assert fills == [100.5, 99.0]             # held at the high; 1 % under the open


def test_slip_match_off_leaves_the_order_waiting():
    fills = []

    class S(tw.Strategy):
        def next(self):
            if len(self) == 1:
                self.buy(size=10)

        def notify_order(self, order):
            if order.status == order.Completed:
                fills.append((len(self), order.executed.price))

    f = frame([100.0] * 5, high=[100.5, 100.5, 100.5, 102.0, 102.0], low=[99.0] * 5)
    run(S, {"A": f}, setup=lambda b: b.set_slippage_perc(0.01, slip_match=False))
    assert fills == [(4, 101.0)]


def test_notified_order_equals_the_placed_one():
    seen = []

    class S(tw.Strategy):
        def next(self):
            if len(self) == 1:
                self.placed = [self.buy(size=1)]

        def notify_order(self, order):
            seen.append((order is self.placed[0], order == self.placed[0], order in self.placed))

    run(S, {"A": frame([10.0] * 4)})
    assert seen and all(row == (False, True, True) for row in seen)


def test_indicator_owner_is_the_builder():
    owners = []

    class Kid(tw.Indicator):
        lines = ("x",)

        def __init__(self):
            owners.append(self._owner)
            self.lines.x = self.data

    class Parent(tw.Indicator):
        lines = ("y",)

        def __init__(self):
            owners.append(self._owner)
            self.kid = Kid(self.data)
            self.lines.y = self.kid.x

    class S(tw.Strategy):
        def __init__(self):
            self.p1 = Parent(self.data)

    strat, _ = run(S, {"A": frame([10.0] * 4)})
    assert owners == [strat, strat.p1]


def test_line_built_from_own_bound_lines():
    class DC(tw.Indicator):
        lines = ("up", "lo", "mid")

        def __init__(self):
            self.lines.up = tw.indicators.Highest(self.data.high, period=3)
            self.lines.lo = tw.indicators.Lowest(self.data.low, period=3)
            self.lines.mid = (self.lines.up + self.lines.lo) / 2.0

    rows = []

    class S(tw.Strategy):
        def __init__(self):
            self.dc = DC(self.data)

        def next(self):
            rows.append((self.dc.up[0], self.dc.lo[0], self.dc.mid[0]))

    run(S, {"A": frame([10.0, 12.0, 11.0, 15.0, 9.0])})
    assert rows and all(m == (u + lo) / 2.0 for u, lo, m in rows)


def test_levered_long_at_a_non_positive_close_counts_size_times_close():
    values = []

    class S(tw.Strategy):
        def next(self):
            if len(self) == 1:
                self.buy(size=100)
            values.append(self.broker.getvalue())

    f = frame([20.0, 18.0, 15.0, -37.63, 10.0], open_=[20.0, 19.0, 16.0, 14.0, 10.0])
    run(S, {"A": f}, cash=10_000.0, setup=lambda b: b.setcommission(leverage=3.0))
    cash = 10_000.0 - 100 * 19.0 / 3.0
    assert values[3] == cash + 100 * -37.63
    assert values[2] == cash + (100 * 19.0 / 3.0 + 100 * (15.0 - 19.0))


def test_line_get_slices_the_preloaded_buffer():
    """``get(ago, size)`` is a plain slice of the buffer ending at ``ago``: a positive ``ago``
    reads bars not yet delivered (cut at the end of the buffer), a window before the first bar
    follows Python's slicing rules. Probe ``line_get``; corpus ``merton_s_portfolio_problem…``
    reads ``close.get(ago=1, size=253)`` and never traded on Tradewind ``d77d526``."""
    seen = {}

    class S(tw.Strategy):
        def next(self):
            c = self.data.close
            seen[len(self)] = [c.get(ago=a, size=s) for a, s in
                               ((0, 1), (0, 3), (-1, 2), (1, 1), (1, 3), (2, 2), (-4, 3), (0, 10), (0, 0))]

    run(S, {"A": frame([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])})
    assert seen[1] == [[1.0], [], [], [2.0], [], [2.0, 3.0], [1.0, 2.0, 3.0], [1.0], []]
    assert seen[3] == [[3.0], [1.0, 2.0, 3.0], [1.0, 2.0], [4.0], [2.0, 3.0, 4.0], [4.0, 5.0], [3.0, 4.0, 5.0],
                       [1.0, 2.0, 3.0], []]
    assert seen[4] == [[4.0], [2.0, 3.0, 4.0], [2.0, 3.0], [5.0], [3.0, 4.0, 5.0], [5.0, 6.0], [],
                       [1.0, 2.0, 3.0, 4.0], []]
    assert seen[6] == [[6.0], [4.0, 5.0, 6.0], [4.0, 5.0], [], [5.0, 6.0], [], [], [3.0, 4.0, 5.0, 6.0], []]


def test_a_feed_compares_by_its_close_while_the_strategy_runs():
    """Two distinct feeds closing at the same price are equal (so ``d in longs`` and
    ``list.index(d)`` find the other one), a feed against a number or a line is its close,
    against ``None`` or a string it is equal only to itself, and it still hashes by identity.
    Probe ``feed_equality``; corpus ``chronological_return_ordering…`` bought every name
    whose close matched a long's (``IRF`` on 2003-11-03) on the oracle, not on Tradewind
    ``d77d526``."""
    seen = []

    class S(tw.Strategy):
        def next(self):
            a, b = self.datas
            seen.append((a == b, a != b, a in [b], [b, a].index(a), a == None, a == "x",
                         a == 50.0, b.close == a, a < b, a + 1.0, a in {b: 1}, len({a, b})))

    run(S, {"A": frame([50.0, 51.0]), "B": frame([50.0, 60.0])})
    assert seen[0] == (True, False, True, 0, False, False, True, True, False, 51.0, False, 2)
    assert seen[1] == (False, True, False, 1, False, False, False, False, True, 52.0, False, 2)


def test_get_value_of_listed_datas_keeps_identity():
    """``get_value(datas=[...])`` takes the positions of those feeds by identity, not by the
    feeds' equality: ``a`` closes at the same price as ``c`` and is not counted in ``[c, b]``.
    The difference of two such reads is compared, the oracle adding the cash to a read of
    several feeds (seen on the oracle, not reproduced here)."""
    out = []

    class S(tw.Strategy):
        def next(self):
            a, b, c = self.datas
            if len(self) == 1:
                self.buy(data=a, size=2)
                self.buy(data=b, size=3)
            if len(self) == 3:
                out.append(self.broker.get_value(datas=[a, c]) - self.broker.get_value(datas=[c, b]))

    run(S, {"A": frame([10.0] * 4), "B": frame([10.0] * 4), "C": frame([10.0] * 4)})
    assert out == [-10.0]
