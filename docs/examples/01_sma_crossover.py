"""A first strategy: long one instrument while its close is above a 50-day average."""

from _data import bars

import tradewind as tw


class SmaTrend(tw.Strategy):
    params = (("period", 50),)

    def __init__(self):
        self.sma = tw.ind.SMA(self.data.close, period=self.p.period)

    def next(self):
        if self.data.close[0] > self.sma[0]:
            self.order_target_percent(target=0.95)
        else:
            self.order_target_percent(target=0.0)


cerebro = tw.Cerebro()
cerebro.adddata(tw.feeds.PandasData(dataname=bars("AAA", seed=1)), name="AAA")
cerebro.addstrategy(SmaTrend, period=50)
cerebro.broker.setcash(100_000)
cerebro.broker.setcommission(commission=0.001)        # 0.1 % of each fill's value
strategy = cerebro.run()[0]
print(f"final value {cerebro.broker.getvalue():,.2f}")
