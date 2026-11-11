"""A monthly timer, a limit order placed under the close, and a trade notification."""

from _data import bars

import tradewind as tw


class BuyTheDipMonthly(tw.Strategy):
    def __init__(self):
        self.add_timer(when=tw.Timer.SESSION_START, monthdays=[1], monthcarry=True)

    def notify_timer(self, timer, when, *args, **kwargs):
        if self.getposition(self.data).size:
            self.close()
        # a limit 2 % under today's close, valid until it fills or is replaced
        self.buy(size=100, exectype=tw.Order.Limit, price=self.data.close[0] * 0.98)

    def notify_trade(self, trade):
        if trade.isclosed:
            print(f"{self.datetime.date(0)} trade closed, net P&L {trade.pnlcomm:,.2f}")


engine = tw.Engine()
engine.adddata(tw.feeds.PandasData(dataname=bars("AAA", years=2, seed=3)), name="AAA")
engine.addstrategy(BuyTheDipMonthly)
engine.broker.setcash(50_000)
engine.run()
print(f"final value {engine.broker.getvalue():,.2f}")
