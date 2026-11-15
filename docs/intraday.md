# Intraday data

Minute bars run on the same engine as daily bars: the clock is the union of the feeds'
timestamps, whatever their spacing. This page covers three things a minute backtest needs:
bars of several sizes at once, loading a bounded window, and stock splits on prices that are
not adjusted for them. The design is in [`spec/06`](https://github.com/paperswithbacktest/tradewind/blob/main/spec/06-intraday-and-options.md).

## Timestamps: bars are stamped at their end

Tradewind takes a bar's timestamp as the **end** of the interval it covers: the minute bar
stamped 09:31 New York is 09:30-09:31, the first of the regular session. Many minute files
stamp the start instead; `load_minute_bars` shifts them (below). Give the engine
timezone-aware timestamps (UTC or local); naive ones are read as UTC.

## Consolidated bars

```python
import datetime as dt
import tradewind as tw

RTH = (dt.time(9, 30), dt.time(16, 0))            # the regular session, New York time

engine = tw.Engine()
minute = engine.adddata(tw.feeds.PandasData(dataname=minute_frame), name="SPY")
five = engine.consolidate(minute, "5min", tz="America/New_York", session=RTH)
hour = engine.consolidate(minute, "1h", tz="America/New_York", session=RTH)
day = engine.consolidate(minute, "D", tz="America/New_York", session=RTH)
```

Each call adds a feed (`SPY_5min`, `SPY_1h`, `SPY_D`) that the strategy reads like any other
(`self.datas[1].close[0]`, indicators on it, orders on the minute feed). The rules:

* a bin is `(end - period, end]` in the place's wall-clock time; with a session, bins start at
  the open and the last one stops at the close (hourly bars end 10:30, 11:30, ... 15:30 and
  16:00), and pre- and post-market bars are left out;
* a consolidated bar is stamped at its **bin end**: the 5-minute bar of 09:30-09:35 arrives at
  09:35, together with the minute bar stamped 09:35, so at 09:37 `five.close[0]` is that
  completed bar and nothing later. A daily bar arrives at the close (16:00);
* `hours="NYSE"` (or any `tw.calendars` calendar) instead of `tz` and `session` takes the
  sessions from the exchange's calendar: holidays, and early closes such as 13:00 the day
  after Thanksgiving;
* without `session`, bins start at local midnight and every bar is kept (crypto, or extended
  hours);
* `count=N` instead of a period groups every N bars.

`next()` starts once every feed has delivered a bar: with an hourly feed, the first `next()`
of the run is at 10:30.

For consolidation inside a strategy, or on a live stream, `tw.consolidators.TimeBarConsolidator`
does the same bar by bar (`update(time, o, h, l, c, v)` then `scan(now)`), and
`tw.consolidators.consolidate` works on plain arrays.

## Loading a window of minute bars

A month of minute bars for every US stock is a 300-400 MB file; sixteen years is 75 GB.
`load_minute_bars` reads a window for a few symbols and nothing else:

```python
from tradewind.intraday import load_minute_bars, month_files, hub_month_files

files = month_files("/data/Stocks-1Min-Price/data")              # train-YYYY-MM.parquet files
# or, from the Hugging Face Hub, one month at a time (pip install huggingface_hub):
# files = hub_month_files("paperswithbacktest/Stocks-1Min-Price", token=os.environ["HF_TOKEN"])

bars = load_minute_bars(files, ["SPY", "QQQ"], "2024-06-03", "2024-06-28",
                        tz="America/New_York", session=RTH)
spy = bars["SPY"]          # UTC index, end-stamped, open high low close volume
```

It opens only the months the window touches, filters symbols and dates inside the Parquet
reader, and raises `MemoryError` past `max_rows` (5 million by default). The defaults read
PWB's stock minute files (`adj_open` ... `volume`, naive New York times stamped at the start);
`columns=` maps another layout, `stamped="end"` keeps stamps that already mark the end.

Memory: the engine keeps every loaded bar, about 56 bytes per bar and feed. One symbol over a
year of regular sessions is 98 000 bars, 5.5 MB; 500 symbols over a year, 2.7 GB. Choose the
window and the symbols accordingly.

## Splits on unadjusted minute prices

Minute prices are often quoted as they traded, splits included: NVDA's minute series drops
90 % overnight on 2024-06-10. Two remedies, from a split table (`symbol`, `date` = ex-date,
`split_from`, `split_to`, the layout of PWB's `Stocks-Daily-Splits`):

```python
from tradewind.intraday import read_splits, adjust_for_splits, split_actions
from tradewind.models import StaticCorporateActions

splits = read_splits(split_table, "NVDA")                 # ex_date, ratio (10.0)

# 1. adjust the prices backward, as a daily adjusted feed would be
nvda = adjust_for_splits(bars["NVDA"], splits, tz="America/New_York")

# 2. or keep the quoted prices and let the engine apply each split to the position
data = engine.adddata(tw.feeds.PandasData(dataname=bars["NVDA"]), name="NVDA")
engine.add_corporate_actions(StaticCorporateActions(split_actions(data, splits)))
```

With (2) a position of 10 shares becomes 100 at the first bar of 2024-06-10, its average
price is divided by 10, and open orders are rescaled, before any fill of that bar; the
portfolio value does not jump. `detect_splits(frame)` lists the overnight jumps that look
like a split, to check a table against the prices.

## Example

[`examples/07_opening_range.py`](examples/07_opening_range.py): an opening-range breakout on
synthetic minute bars, with a 30-minute consolidated feed.
