"""Opening-range breakout on minute bars: buy when a minute closes above the high of the first
30-minute bar of the session, flat at 15:55. The 30-minute bars come from
``engine.consolidate``; the minute bars are synthetic (no download).
"""

import datetime as dt

import numpy as np
import pandas as pd

import tradewind as tw

NY = "America/New_York"
RTH = (dt.time(9, 30), dt.time(16, 0))


def minute_bars(days: int = 20, seed: int = 3) -> pd.DataFrame:
    """Regular-session minute bars, stamped at their end (09:31 ... 16:00 New York)."""
    rng = np.random.default_rng(seed)
    index = []
    for day in pd.bdate_range("2024-03-04", periods=days):
        index += list(pd.date_range(f"{day.date()} 09:31", f"{day.date()} 16:00", freq="1min", tz=NY))
    index = pd.DatetimeIndex(index).tz_convert("UTC")
    close = 100 * np.exp(np.cumsum(rng.normal(0, 6e-4, len(index))))
    open_ = np.concatenate(([close[0]], close[:-1]))
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.0003,
                         "low": np.minimum(open_, close) * 0.9997, "close": close,
                         "volume": rng.integers(100, 5_000, len(index)).astype(float)}, index=index)


class OpeningRange(tw.Strategy):
    def __init__(self):
        self.minute, self.half_hour = self.datas[0], self.datas[1]
        self.trades = 0

    def next(self):
        now = pd.Timestamp(self.minute.datetime.datetime(0), tz="UTC").tz_convert(NY)
        if now.time() >= dt.time(15, 55):
            if self.getposition(self.minute).size:
                self.close(self.minute)
            return
        opening = self.half_hour
        # the current 30-minute bar is today's first (09:30-10:00) from 10:00 to 10:29
        stamp = pd.Timestamp(opening.datetime.datetime(0), tz="UTC").tz_convert(NY)
        is_opening_range = stamp.date() == now.date() and stamp.time() == dt.time(10, 0)
        flat = not self.getposition(self.minute).size
        if is_opening_range and flat and self.minute.close[0] > opening.high[0]:
            self.order_target_percent(self.minute, target=0.95)
            self.trades += 1


engine = tw.Engine()
minute = engine.adddata(tw.feeds.PandasData(dataname=minute_bars()), name="XYZ")
engine.consolidate(minute, "30min", tz=NY, session=RTH)
engine.addstrategy(OpeningRange)
engine.broker.setcash(100_000)
strategy = engine.run()[0]
print(f"{strategy.trades} entries on {len(minute)} minute bars")
print(f"final value {engine.broker.getvalue():,.2f}")
