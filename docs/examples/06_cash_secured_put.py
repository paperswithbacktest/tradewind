"""A monthly cash-secured put: sell one put about 5 % out of the money, 20 to 45 days out,
hold it to expiry, and if it is assigned, sell the shares the next day.

The option table is synthetic here (Black-Scholes-Merton prices on a random walk, from
``tradewind.options.synthetic_option_table``); any daily table with the same columns runs
the same way -- one row per contract and day it traded: ``symbol, date, underlying,
expiration, strike, option_type, open, high, low, close, volume`` (PWB's daily option
table has them). Pass its path to run on it: ``python 06_cash_secured_put.py table.parquet
underlying.parquet``, the second file holding the underlying's daily ``close`` (and
``open``, ``high``, ``low``) with a ``date`` column.
"""

import sys

import pandas as pd
from _data import bars

import tradewind as tw
from tradewind.options import add_options, synthetic_option_table


class CashSecuredPut(tw.Strategy):
    params = (("chain", None), ("otm", 0.95), ("min_days", 20), ("max_days", 45))

    def __init__(self):
        self.put = None            # the feed of the put we are short
        self.assignments = 0
        self.premium = 0.0

    def notify_corporate_action(self, action):
        # the engine's expiry of a contract (opt-in hook): exercised = assigned for a short
        if getattr(action, "exercised", False) and action.quantity < 0:
            self.assignments += 1

    def notify_order(self, order):
        if order.status == order.Completed and getattr(order.data, "contract", None) is not None:
            self.premium -= order.executed.size * order.executed.price * 100

    def next(self):
        stock = self.datas[0]
        if self.getposition(stock).size:              # assigned: sell the shares
            self.close(stock)
            return
        if self.put is not None and self.getposition(self.put).size:
            return                                    # the put is still on
        chain = (self.p.chain.chain().puts_only()
                 .expiration(self.p.min_days, self.p.max_days)
                 .standards_only()
                 .nearest(strike=stock.close[0] * self.p.otm))
        if not len(chain):
            return
        self.put = chain.feeds()[0]
        contracts = int(self.broker.getcash() // (self.put.contract.strike * 100))
        if contracts > 0 and len(self.put):
            self.sell(self.put, size=contracts)


def run(table: pd.DataFrame, underlying: pd.DataFrame, name: str, cash: float = 100_000):
    engine = tw.Engine()
    stock = engine.adddata(tw.feeds.PandasData(dataname=underlying), name=name)
    chain = add_options(engine, stock, table,
                        select=lambda c: c.puts_only().expiration(0, 62).moneyness(0.85, 1.02))
    engine.addstrategy(CashSecuredPut, chain=chain)
    engine.broker.setcash(cash)
    strat = engine.run()[0]
    return engine, strat, chain


if __name__ == "__main__":
    files = [a for a in sys.argv[1:] if a.endswith(".parquet")]
    if len(files) == 2:
        table = pd.read_parquet(files[0])
        und = pd.read_parquet(files[1])
        und = und.set_index(pd.to_datetime(und.pop("date")))
        name = str(table["underlying"].iloc[0])
    else:
        name = "XYZ"
        und = bars(name, start="2019-01-01", years=2, seed=7)
        table = synthetic_option_table(und, name, vol=0.22, seed=7)
    engine, strat, chain = run(table, und, name)
    print(f"{len(chain.feeds)} contracts loaded, {strat.assignments} assignments, "
          f"premium collected {strat.premium:,.2f}")
    print(f"final value {engine.broker.getvalue():,.2f}")
