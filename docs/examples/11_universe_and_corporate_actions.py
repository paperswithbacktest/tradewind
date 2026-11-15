"""An equal-weight book over a point-in-time universe, on raw prices with corporate actions.

Six names, each with a feed; a member table says which of them are in the universe on
each date (the layout of a point-in-time universe: one row per member and date). The
strategy rebalances each month over the members of the day; a name that leaves is sold at
the next open (``on_removal="liquidate"``). The prices are **raw** (not adjusted): a 2-for-1
split halves one name's price, and the engine doubles the shares instead; a dividend pays
cash on its ex-date; a name delisted mid-run is liquidated at its last close.

Everything is synthetic. On PWB's data the same three tables come from
``tradewind.corporate.pwb_corporate_actions(symbols, start, end)`` and the universe from
``tradewind.corporate.pwb_liquid_universe(start, end, top=500)`` (Hub access needed); PWB's
daily prices are back-adjusted, so declare ``normalization="adjusted"`` there: delistings
still apply, splits and dividends are already in the prices.
"""

import numpy as np
import pandas as pd
from _data import bars

import tradewind as tw
from tradewind.corporate import SymbolCorporateActions
from tradewind.universe import MembershipUniverse

NAMES = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
START = "2018-01-01"


def raw_prices():
    """Synthetic raw bars: CCC splits 2-for-1 on 2019-06-03, EEE stops trading on 2020-03-13."""
    frames = {name: bars(name, start=START, years=4, seed=i) for i, name in enumerate(NAMES)}
    ccc = frames["CCC"]
    after = ccc.index >= "2019-06-03"
    ccc.loc[after, ["open", "high", "low", "close"]] /= 2.0
    ccc.loc[after, "volume"] *= 2.0
    frames["EEE"] = frames["EEE"].loc[:"2020-03-13"]
    return frames


def member_table(frames):
    """Each month, the four names with the largest median dollar volume over the previous
    month: one row per member and trading day, as a point-in-time universe table has."""
    rows = []
    days = frames["AAA"].index
    for month_start in pd.date_range(START, periods=48, freq="MS"):
        window = slice(month_start - pd.offsets.MonthBegin(1), month_start - pd.Timedelta(days=1))
        dv = {n: (f.loc[window, "close"] * f.loc[window, "volume"]).median() for n, f in frames.items()}
        top = sorted((n for n in dv if np.isfinite(dv[n])), key=dv.get, reverse=True)[:4]
        month_days = days[(days >= month_start) & (days < month_start + pd.offsets.MonthBegin(1))]
        rows += [(d, n) for d in month_days for n in top if d <= frames[n].index[-1]]
    return pd.DataFrame(rows, columns=["date", "symbol"])


class EqualWeightUniverse(tw.Strategy):
    def __init__(self):
        self.month = None
        self.entered = self.left = 0
        self.events = []

    def notify_universe(self, change):
        # the engine's UniverseChange: SecurityIds (feeds(self, change.added) gives the feeds)
        self.entered += len(change.added)
        self.left += len(change.removed)

    def notify_corporate_action(self, action):
        self.events.append(f"{type(action).__name__} {self.broker.securities[action.security].symbol}")

    def next(self):
        today = self.datas[0].datetime.date(0)
        if today.month == self.month:
            return
        self.month = today.month
        members = [d for d in self.engine.universe.members if d.security.is_tradable]
        for d in members:
            self.order_target_percent(data=d, target=0.95 / len(members))


if __name__ == "__main__":
    frames = raw_prices()
    engine = tw.Engine()
    for name, frame in frames.items():
        engine.adddata(tw.feeds.PandasData(dataname=frame), name=name, normalization="raw")
    engine.broker.setcash(100_000)
    engine.set_universe(MembershipUniverse(member_table(frames)), on_removal="liquidate")
    engine.set_corporate_actions(SymbolCorporateActions(
        splits=pd.DataFrame({"symbol": ["CCC"], "date": ["2019-06-03"], "split_from": [1], "split_to": [2]}),
        dividends=pd.DataFrame({"symbol": ["AAA", "BBB"], "date": ["2019-03-08", "2019-09-06"],
                                "pay_date": ["2019-03-29", "2019-09-27"], "cash_amount": [0.80, 1.10]}),
        delistings=pd.DataFrame({"symbol": ["EEE"], "delistingDate": ["2020-03-16"]}),
    ))
    engine.addstrategy(EqualWeightUniverse)
    strat = engine.run()[0]
    print(f"universe: {strat.entered} entries, {strat.left} exits")
    print("corporate actions:", ", ".join(strat.events))
    print(f"final value {engine.broker.getvalue():,.2f}")
