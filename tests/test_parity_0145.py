"""The semantics fixed by the 0.1.45 parity pass (spec/02, "parity 0.1.45"), by hand.

The oracle probes of ``tests/oracle/strategies.py`` hold the same behaviours against the
oracle; these tests pin them without it.
"""

import numpy as np
import pandas as pd
import pytest

import tradewind as tw


def frames(n_bars=40, late=None, seed=3):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2021-01-04", periods=n_bars)
    out = {}
    for i in range(3):
        close = 80 * np.exp(np.cumsum(rng.normal(0, 0.015, n_bars)))
        openp = close * (1 + rng.normal(0, 0.004, n_bars))
        out[f"S{i}"] = pd.DataFrame({"open": openp, "high": np.maximum(openp, close) * 1.01,
                                     "low": np.minimum(openp, close) * 0.99, "close": close, "volume": 1e5},
                                    index=index)
    if late:
        out["L"] = out["S0"].iloc[late:].copy()
    return out


def run(cls, fr, cash=100_000.0, names=None):
    cerebro = tw.Cerebro(stdstats=False)
    for name in names or fr:
        cerebro.adddata(tw.feeds.PandasData(dataname=fr[name]), name=name)
    cerebro.addstrategy(cls)
    cerebro.broker.set_cash(cash)
    return cerebro.run()[0], cerebro.broker


def test_strategy_private_names_do_not_shadow_the_engine():
    class S(tw.Strategy):
        def __init__(self):
            self._ready = False
            self._step = 0
            self._indicators = {}
            self._listed = set()
            self._orders = "user"

        def _resolve(self, name, default=None):
            return self.getdatabyname(name) if name in self.getdatanames() else default

        def next(self):
            self._step += 1
            if self._step == 5:
                self.order_target_value(self._resolve("S1"), target=10_000)

    strat, broker = run(S, frames())
    assert strat._step == 40
    assert broker.getposition(strat.datas[1]).size > 0


def test_strategy_datetime_is_the_clock_and_len_counts_slices():
    seen = []

    class S(tw.Strategy):
        def prenext(self):
            seen.append(("pre", len(self), self.datetime.date(0), len(self.datas[0])))

        def next(self):
            seen.append(("next", len(self), self.datetime.date(0), len(self.datas[0])))
            if len(self) == 12:
                with pytest.raises(IndexError):
                    self.datetime.date(1)

    fr = frames(late=10)
    run(S, fr, names=["L", "S0", "S1"])
    first = seen[0]
    assert first[0] == "pre" and first[1] == 1 and first[3] == 0           # data0 not listed yet
    assert first[2] == fr["S0"].index[0].date()
    start = next(r for r in seen if r[0] == "next")
    assert start[1] == 11 and start[3] == 1


def test_warm_up_is_counted_on_each_indicator_clock():
    starts = {}
    for on_late in (False, True):
        class S(tw.Strategy):
            params = (("on_late", on_late),)

            def __init__(self):
                self.sma = tw.indicators.SMA(self.datas[0 if self.p.on_late else 1].close, period=5)

            def next(self):
                starts.setdefault(self.p.on_late, len(self))

        run(S, frames(late=10), names=["L", "S0"])
    assert starts == {False: 11, True: 15}


def test_bound_lines_need_neither_once_nor_next():
    class Bound(tw.Indicator):
        lines = ("value",)

        def __init__(self):
            self.lines.value = tw.indicators.RateOfChange(self.data.close, period=5)

    rows = []

    class S(tw.Strategy):
        def __init__(self):
            self.b = Bound(self.data)
            self.r = tw.indicators.RateOfChange(self.data.close, period=5)
            self.n = tw.indicators.SMA(Bound(self.data), period=3)

        def nextstart(self):
            rows.append(len(self))
            self.next()

        def next(self):
            assert self.b.value[0] == self.r[0]

    run(S, frames(), names=["S0"])
    assert rows == [8]          # SMA(3) of a line bound to ROC(5): 6 + 3 - 1


def test_rate_of_change_is_a_difference_over_the_old_value():
    x = frames()["S0"]["close"].to_numpy()
    out = []

    class S(tw.Strategy):
        def __init__(self):
            self.r = tw.indicators.RateOfChange(self.data.close, period=5)

        def next(self):
            out.append(self.r[0])

    run(S, frames(), names=["S0"])
    assert out[-1] == (x[-1] - x[-6]) / x[-6]


def test_order_target_value_on_a_flat_feed_lands_a_pending_withdrawal():
    statuses = []

    class S(tw.Strategy):
        def next(self):
            if len(self) == 5:
                cash = self.broker.getcash()
                self.broker.add_cash(-0.5 * cash)
                self.order_target_value(self.data, target=int(0.9 * cash / self.data.close[0]) * self.data.close[0])

        def notify_order(self, order):
            statuses.append(order.getstatusname())

    run(S, frames(), names=["S0"])
    assert statuses[-1] == "Margin"


def test_checksubmit_off_accepts_at_once_and_cancels_in_the_same_bar():
    statuses = []

    class S(tw.Strategy):
        def __init__(self):
            self.broker.set_checksubmit(False)

        def next(self):
            if len(self) == 5:
                self.cancel(self.buy(size=5))

        def notify_order(self, order):
            statuses.append(order.getstatusname())

    run(S, frames(), names=["S0"])
    assert statuses == ["Accepted", "Canceled"]


def test_setcash_makes_the_value_the_cash_alone_until_the_next_mark():
    rows = []

    class S(tw.Strategy):
        def next(self):
            if len(self) == 3:
                self.buy(size=100)
            if len(self) == 10:
                self.broker.setcash(self.broker.getcash() * 1.01)
                rows.append((self.broker.getvalue(), self.broker.getcash()))
            if len(self) == 11:
                rows.append((self.broker.getvalue(), self.broker.getcash()))

    run(S, frames(), names=["S0"])
    assert rows[0][0] == rows[0][1]
    assert rows[1][0] > rows[1][1]


def test_leveraged_long_pays_its_margin_and_a_short_the_full_price():
    fills = []

    class S(tw.Strategy):
        def __init__(self):
            for ci in self.broker.comminfo.values():
                ci.p.leverage = 2.0

        def next(self):
            if len(self) == 3:
                self.buy(data=self.datas[0], size=100)
                self.sell(data=self.datas[1], size=100)

        def notify_order(self, order):
            if order.status == order.Completed:
                fills.append((order.data._name, order.executed.price, self.broker.getcash()))

    run(S, frames(), names=["S0", "S1"])
    (_, p0, _), (_, p1, cash) = fills
    assert cash == 100_000.0 - 100 * p0 / 2.0 + 100 * p1


def test_a_reversal_is_two_executions_each_with_its_own_fee():
    cash = []

    class S(tw.Strategy):
        def __init__(self):
            self.broker.setcommission(commission=0.0013)

        def next(self):
            if len(self) == 3:
                self.buy(size=48)
            if len(self) == 6:
                self.sell(size=98)
            cash.append(self.broker.getcash())

    run(S, frames(), names=["S0"])
    o = frames()["S0"]["open"].to_numpy()
    p1, p2 = o[3], o[6]
    c = 100_000.0
    c += -(48 * p1)
    c -= 48 * 0.0013 * p1
    entry = 48 * p1                                   # closing the long: entry value + P&L
    c += entry + 48 * (p2 - p1)
    c -= 48 * 0.0013 * p2
    c += -(-50 * p2)
    c -= 50 * 0.0013 * p2
    assert cash[6] == c


def test_trades_are_notified_at_open_and_close_only():
    seen = []

    class S(tw.Strategy):
        def next(self):
            if len(self) in (3, 6):
                self.buy(size=10)
            if len(self) == 9:
                self.close()

        def notify_trade(self, trade):
            seen.append((len(self), trade.isclosed))

    run(S, frames(), names=["S0"])
    assert seen == [(4, False), (10, True)]
