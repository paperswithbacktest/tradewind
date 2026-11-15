"""The names nine corpus strategies read without a guard and the engine lacked:
``broker._get_value``, ``bt.ind.SumN`` / ``Mean`` / ``PctChange``, ``bt.DivByZero``,
``buy_bracket`` / ``sell_bracket``, ``positionbyname``. Behaviour fixed on the oracle
(probes ``private_get_value``, ``sumn_divbyzero``, ``mean_pctchange``, ``bracket_orders``,
``bracket_batch``, ``position_by_name`` in ``tests/oracle/strategies.py``); here, the parts
that need no oracle."""

import math

import numpy as np
import pandas as pd
import pytest

import tradewind as bt


def frame(n_bars=60, seed=5, volume=None):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2022-01-03", periods=n_bars)
    close = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, n_bars)))
    vol = np.full(n_bars, 1e4) if volume is None else volume
    return pd.DataFrame({"open": close * 0.998, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": vol}, index=index)


def run(cls, f=None, cash=10_000.0):
    c = bt.Cerebro(stdstats=False)
    c.adddata(bt.feeds.PandasData(dataname=frame() if f is None else f), name="A")
    c.addstrategy(cls)
    c.broker.setcash(cash)
    return c.run()[0]


def test_names_exist():
    for name in ("SumN", "Mean", "Average", "ArithmeticMean", "PctChange", "PercentChange"):
        assert hasattr(bt.ind, name)
    assert hasattr(bt, "DivByZero")
    assert hasattr(bt.Strategy, "buy_bracket") and hasattr(bt.Strategy, "sell_bracket")
    assert hasattr(bt.BackBroker, "_get_value")


def test_sum_mean_pctchange_values():
    vol = np.where(np.arange(60) % 7 < 3, 0.0, 1e4)
    f = frame(volume=vol)

    class S(bt.Strategy):
        def __init__(self):
            c = self.data.close
            self.s = bt.ind.SumN(c, period=4)
            self.m = bt.ind.Mean(bt.ind.ROC(c, period=2), bt.ind.ROC(c, period=9))
            self.pc = bt.ind.PctChange(c, period=3)
            self.d = bt.DivByZero(c, bt.ind.SumN(self.data.volume, period=2), -1.0)
            self.rows = []

        def next(self):
            self.rows.append((self.s[0], self.m[0], self.pc[0], self.d[0]))

    s = run(S, f)
    c = f["close"].to_numpy()
    first = 10  # the largest minimum period: ROC(9)
    assert len(s.rows) == len(c) - first + 1
    for k, (sm, mn, pc, dv) in enumerate(s.rows):
        i = k + first - 1
        assert sm == sum(c[i - 3:i + 1].tolist())  # Python floats: the compensated sum
        assert mn == (c[i] - c[i - 2]) / c[i - 2]  # the first input only
        assert pc == c[i] / c[i - 3] - 1.0
        v = vol[i - 1] + vol[i]
        assert dv == (-1.0 if v == 0 else c[i] / v)


def test_divbyzero_nan_zero_and_default_periods():
    class S(bt.Strategy):
        def __init__(self):
            self.d = bt.DivByZero(self.data.close, self.data.close * 0.0, float("nan"))
            self.one = bt.ind.SumN(self.data.close)
            self.avg = bt.ind.Average(self.data.close)
            self.pct = bt.ind.PctChange(self.data.close)
            self.rows = []

        def next(self):
            self.rows.append((self.d[0], self.one[0], self.avg[0], self.pct[0], self.data.close[0]))

    s = run(S)
    assert all(math.isnan(r[0]) for r in s.rows)
    assert all(r[1] == r[4] == r[2] for r in s.rows)
    assert len(s.rows) == 60 - 30  # PctChange: period 30 by default


def test_positionbyname_is_the_first_position():
    class S(bt.Strategy):
        def next(self):
            if len(self) == 2:
                self.buy(size=3)
            if len(self) == 5:
                p = self.positionbyname
                self.same = p is self.position and p.size == 3
                with pytest.raises(TypeError):
                    p("A")
                self.byname = self.positionsbyname["A"].size

    s = run(S)
    assert s.same and s.byname == 3


def test_get_value_lands_additions():
    class S(bt.Strategy):
        def next(self):
            if len(self) == 3:
                before = self.broker.getvalue()
                self.broker.add_cash(100.0)
                self.queued = self.broker.getcash()
                self.settled = self.broker._get_value()
                self.after = (self.broker.getcash(), self.broker.getvalue(), before)

    s = run(S)
    cash, value, before = s.after
    assert s.queued == 10_000.0 and cash == 10_100.0
    assert s.settled == value == before + 100.0


def test_bracket_take_profit_cancels_stop():
    log = []

    class S(bt.Strategy):
        def next(self):
            if len(self) == 2:
                c = self.data.close[0]
                self.br = self.buy_bracket(size=10, exectype=bt.Order.Market, limitprice=c * 1.005,
                                           stopprice=c * 0.5)

        def notify_order(self, order):
            if order.status in (order.Completed, order.Canceled):
                log.append(([o.ref for o in self.br].index(order.ref), order.getstatusname(), len(self)))

    s = run(S)
    parent, stop, limit = s.br
    assert (parent.parent, stop.parent, limit.parent) == (None, parent, parent)
    assert (parent.transmit, stop.transmit, limit.transmit) == (False, False, True)
    assert (stop.exectype, limit.exectype) == (bt.Order.Stop, bt.Order.Limit)
    assert log[0] == (0, "Completed", 3)
    # a child never fills on its entry's bar; the other child is cancelled right after
    assert log[1][:2] == (2, "Completed") and log[1][2] > 3
    assert log[2] == (1, "Canceled", log[1][2])
    assert s.getposition().size == 0


def test_bracket_cancel_parent_cancels_children():
    class S(bt.Strategy):
        def next(self):
            if len(self) == 2:
                c = self.data.close[0]
                self.br = self.sell_bracket(size=5, price=c * 2, limitprice=c, stopprice=c * 3)
            if len(self) == 6:
                self.cancel(self.br[0])

    s = run(S)
    assert [o.getstatusname() for o in s.br] == ["Canceled"] * 3
    assert [o.isbuy() for o in s.br] == [False, True, True]
