"""A custom indicator with ``next``, another built from line arithmetic, and their warm-up."""

from _data import bars

import tradewind as tw


class Momentum(tw.Indicator):
    """Return over ``period`` bars, computed bar by bar."""

    lines = ("mom",)
    params = (("period", 20),)

    def __init__(self):
        self.addminperiod(self.p.period + 1)

    def next(self):
        self.lines.mom[0] = self.data[0] / self.data[-self.p.period] - 1.0


class Breakout(tw.Strategy):
    def __init__(self):
        self.mom = Momentum(self.data.close, period=60)
        high20 = tw.ind.Highest(self.data.high, period=20)
        # built once, in __init__: a line, not a number (see docs/indicators.md)
        self.near_high = self.data.close > high20 * 0.97

    def nextstart(self):
        print(f"first next() on {self.datetime.date(0)} (bar {len(self)})")
        self.next()

    def next(self):
        if self.mom[0] > 0 and self.near_high[0]:
            self.order_target_percent(target=0.95)
        elif self.mom[0] < 0:
            self.order_target_percent(target=0.0)


engine = tw.Engine()
engine.adddata(tw.feeds.PandasData(dataname=bars("AAA", seed=7)), name="AAA")
engine.addstrategy(Breakout)
engine.broker.setcash(100_000)
engine.run()
print(f"final value {engine.broker.getvalue():,.2f}")
