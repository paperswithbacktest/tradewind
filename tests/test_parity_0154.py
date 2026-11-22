"""The semantics fixed by the parity campaign on pwb-toolbox 0.1.54 (spec/02, "parity 0.1.54"),
pinned without the oracle; the probes of ``tests/oracle/strategies.py`` hold them against it."""

import numpy as np
import pandas as pd

import tradewind as tw


def frame(close):
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({"open": close + 0.5, "high": close + 2.0, "low": close - 2.0, "close": close,
                         "volume": 1e5}, index=pd.bdate_range("2021-01-04", periods=len(close)))


def test_created_price_is_the_close_when_the_order_has_no_price():
    rows = []

    class S(tw.Strategy):
        def next(self):
            n = len(self)
            if n == 1:
                self.buy(size=1)                                            # market
            if n == 2:
                self.buy(size=1, exectype=tw.Order.Limit, price=11.0)
            if n == 3:
                self.order_target_size(self.data, 5)
            if n == 4:
                self.buy(size=1, price=99.0)
            if n == 5:
                self.close()
            if n == 6:
                self.buy(size=1, exectype=tw.Order.StopLimit, price=20.0, plimit=21.0)

        def notify_order(self, order):
            if order.status == order.Submitted:
                rows.append((order.created.price, order.created.pricelimit))

    engine = tw.Engine(stdstats=False)
    engine.adddata(tw.feeds.PandasData(dataname=frame([10.5 + i for i in range(9)])), name="A")
    engine.addstrategy(S)
    engine.run()
    assert rows == [(10.5, 10.5), (11.0, 11.0), (12.5, 12.5), (99.0, 99.0), (14.5, 14.5), (20.0, 21.0)]


def test_an_indicator_without_input_follows_the_first_feed():
    rows = []

    class Konst(tw.Indicator):
        lines = ("r",)
        params = (("k", 0.25),)

        def next(self):
            self.lines.r[0] = self.p.k + len(self)

    class S(tw.Strategy):
        def __init__(self):
            self.konst = Konst(k=0.5)

        def next(self):
            rows.append((len(self), len(self.konst), self.konst.r[0], self.konst.data is self.datas[0]))

    engine = tw.Engine(stdstats=False)
    engine.adddata(tw.feeds.PandasData(dataname=frame([10.0, 11.0, 12.0])), name="A")
    engine.adddata(tw.feeds.PandasData(dataname=frame([5.0, 6.0])), name="B")
    engine.addstrategy(S)
    engine.run()
    assert rows == [(1, 1, 1.5, True), (2, 2, 2.5, True), (3, 3, 3.5, True)]


def _nextforce_run(where):
    """A next-only indicator reading a feed that is not its input, with ``_nextforce`` put
    at ``where``; returns what it read on each bar."""
    rows = []

    class Foreign(tw.Indicator):
        lines = ("x",)
        params = (("other", None),)

        def next(self):
            self.lines.x[0] = self.p.other.close[0]

    class Forced(Foreign):
        _nextforce = True

    class SelfForced(Foreign):
        def __init__(self):
            self._nextforce = True

    class ObsForced:
        _nextforce = True

    class S(tw.Strategy):
        def __init__(self):
            v = self.getdatabyname("V")
            self.f = Foreign(self.datas[0], other=v)
            if where == "class":
                Forced(self.datas[0], other=v)
            elif where == "init":
                SelfForced(self.datas[0], other=v)
            elif where == "after":
                Foreign(self.datas[0], other=v)._nextforce = True
            elif where == "strategy":
                self._nextforce = True

        def next(self):
            rows.append(float(self.f[0]))

    engine = tw.Engine(stdstats=False)
    engine.adddata(tw.feeds.PandasData(dataname=frame([10.0 + i for i in range(8)])), name="A")
    v = frame([100.0 + 10 * i for i in range(6)])
    v.index = pd.bdate_range("2021-01-06", periods=6)
    engine.adddata(tw.feeds.PandasData(dataname=v), name="V")
    engine.addstrategy(S)
    if where == "observer":
        engine.addobserver(ObsForced)
    engine.run()
    return rows


def test_nextforce_turns_the_runonce_prepass_off_for_the_whole_run():
    live, frozen = [100.0, 110.0, 120.0, 130.0, 140.0, 150.0], [150.0] * 6
    assert _nextforce_run(None) == frozen
    for where in ("class", "init", "observer"):
        assert _nextforce_run(where) == live, where
    for where in ("after", "strategy"):
        assert _nextforce_run(where) == frozen, where
