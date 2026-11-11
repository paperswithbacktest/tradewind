"""Several feeds, a monthly equal-weight rebalance, and the order notifications."""

from _data import bars

import tradewind as tw


class EqualWeight(tw.Strategy):
    def __init__(self):
        self.month = None
        self.fills = 0

    def next(self):
        month = self.datetime.date(0).month
        if month == self.month:
            return
        self.month = month
        weight = 0.98 / len(self.datas)
        for data in self.datas:
            self.order_target_percent(data, target=weight)

    def notify_order(self, order):
        if order.status == order.Completed:
            self.fills += 1
        elif order.status in (order.Margin, order.Rejected):
            print(f"{self.datetime.date(0)} {order.data._name}: {order.getstatusname()}")


cerebro = tw.Cerebro()
for i, symbol in enumerate(["AAA", "BBB", "CCC", "DDD"]):
    cerebro.adddata(tw.feeds.PandasData(dataname=bars(symbol, seed=10 + i)), name=symbol)
cerebro.addstrategy(EqualWeight)
cerebro.addanalyzer(tw.analyzers.DrawDown, _name="dd")
cerebro.broker.setcash(1_000_000)
strategy = cerebro.run()[0]
print(f"fills {strategy.fills}, final value {cerebro.broker.getvalue():,.2f}, "
      f"max drawdown {strategy.analyzers.dd.get_analysis()['max']['drawdown']:.2f} %")
