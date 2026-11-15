"""The realistic profile: measured spreads per symbol and year, volume-capped fills, a Reg T
margin account, borrow fees on shorts, T+1/T+2 settlement -- against the oracle defaults.

The cost table has PWB's shape (``Stocks-Yearly-TransactionCostParams``: one spread per
symbol and year, in basis points); here it is written by hand so that the example runs
without a download. With the real one: ``CostTable.from_frame(frame)``.
"""

import pandas as pd
from _data import bars

import tradewind as tw
from tradewind.realistic import BorrowRates, CostTable

COSTS = CostTable.from_frame(pd.DataFrame({
    "symbol": ["LIQ", "LIQ", "THIN", "THIN"],
    "year": [2015, 2020, 2015, 2020],
    "spread_bps": [4.0, 2.0, 120.0, 60.0],       # full spread; the fee is half of it per side
}))


class LongShort(tw.Strategy):
    """Long the liquid name, short the thin one, rebalanced every 21 bars."""

    def next(self):
        if len(self) % 21 == 1:
            self.order_target_percent(self.datas[0], target=0.8)
            self.order_target_percent(self.datas[1], target=-0.3)


def run(profile: str | None) -> tw.Engine:
    engine = tw.Engine()
    # the thin name trades 20 000 shares a day: a 10 % participation cap spreads its orders
    thin = bars("THIN", seed=22)
    thin["volume"] = 20_000.0
    engine.adddata(tw.feeds.PandasData(dataname=bars("LIQ", seed=21)), name="LIQ")
    engine.adddata(tw.feeds.PandasData(dataname=thin), name="THIN")
    engine.addstrategy(LongShort)
    engine.broker.setcash(1_000_000)
    if profile == "realistic":
        engine.broker.set_profile("realistic", costs=COSTS, participation=0.1,
                                  borrow=BorrowRates(rate=0.0025, rates={"THIN": 0.05}))
    engine.run()
    return engine


oracle, realistic = run(None), run("realistic")
thin = realistic.datas[1].security.holding
print(f"final value oracle {oracle.broker.getvalue():,.2f}, realistic {realistic.broker.getvalue():,.2f}")
print(f"THIN under the realistic profile: fees {thin.total_fees:,.2f}, borrow {thin.total_borrow_fees:,.2f}")
