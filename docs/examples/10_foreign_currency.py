"""A security quoted in euros in a dollar account: cash per currency, the value in dollars.

The EUR/USD feed is an ordinary feed; ``set_conversion`` makes its close the value of one
euro in dollars at each bar. The purchase is paid in euros (borrowed, under the margin
account), the portfolio value is marked in dollars.
"""

import numpy as np
from _data import bars

import tradewind as tw
from tradewind.realistic import MarginAccount
from tradewind.securities import SymbolProperties


class BuyAndHold(tw.Strategy):
    def next(self):
        if len(self) == 1:
            self.order_target_percent(self.datas[0], target=0.9)


engine = tw.Engine()
stock = engine.adddata(tw.feeds.PandasData(dataname=bars("SAP", seed=31)), name="SAP")
fx = bars("EURUSD", seed=32, drift=0.0, vol=0.004)
fx[["open", "high", "low", "close"]] *= 1.10 / fx["close"].iloc[0]
fx["volume"] = np.nan
eurusd = engine.adddata(tw.feeds.PandasData(dataname=fx), name="EURUSD")

engine.broker.set_properties(stock, SymbolProperties(quote_currency="EUR"))
engine.broker.set_conversion("EUR", eurusd)               # EURUSD close = dollars per euro
engine.broker.set_models(stock, buying_power=MarginAccount())
engine.addstrategy(BuyAndHold)
engine.broker.setcash(100_000)
engine.run()

book = engine.broker.cashbook
print(f"USD cash {book['USD'].amount:,.2f}, EUR cash {book['EUR'].amount:,.2f} "
      f"at {book['EUR'].conversion_rate:.4f}")
print(f"final value {engine.broker.getvalue():,.2f} USD")
