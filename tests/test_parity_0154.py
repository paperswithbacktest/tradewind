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
