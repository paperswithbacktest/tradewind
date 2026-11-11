# Data feeds

A feed is one instrument's bars. Tradewind ships `PandasData`, which reads a pandas
DataFrame; the engine has no dependency on any data vendor, so anything you can load into a
DataFrame (a CSV, a parquet file, a database query, an API) can be backtested.

## `PandasData`

```python
frame = pd.read_parquet("spy.parquet")       # DatetimeIndex; open high low close volume
data = tw.feeds.PandasData(dataname=frame)
engine.adddata(data, name="SPY")
```

The index is the bar's time by default. Each line is mapped to a column:

| parameter | default | meaning |
|---|---|---|
| `datetime` | `None` | `None`: the index; a column name or position otherwise |
| `open`, `high`, `low`, `close`, `volume`, `openinterest` | `-1` | `-1`: the column of that name (case-insensitive with `nocase=True`); a name or a position; `None` when the column does not exist (the line is NaN) |
| `nocase` | `True` | match column names without regard to case |
| `fromdate`, `todate` | `None` | keep only the bars between these dates (inclusive) |
| `name` | `""` | the feed's name (also settable through `adddata(data, name=...)`) |

```python
# columns named differently, no volume
tw.feeds.PandasData(dataname=frame, open="Open", high="High", low="Low", close="Adj Close",
                    volume=None)
```

Rows out of order are sorted by time. A time-zone-aware index is converted to UTC.
A `close` column is required.

Time is stored on each bar as a float number of days (the proleptic Gregorian ordinal plus
the fraction of the day). `tw.num2date(x)` and `tw.date2num(dt)` convert both ways; the
encoding is the one backtrader and matplotlib use, so saved dates round-trip.

## Several instruments

Add one feed per instrument. The order of `adddata` calls is the order of `self.datas`.

```python
for symbol, frame in frames.items():
    engine.adddata(tw.feeds.PandasData(dataname=frame), name=symbol)
```

The feeds do not need the same dates. The run's clock is the union of all their dates
([How a backtest runs](concepts.md)); on a slice where a feed has no bar it keeps its last
one. That has three effects to keep in mind with a universe of many symbols:

- **Late listings hold `next()` back.** By default `next()` waits until every feed has
  delivered a bar. A universe that includes a symbol listing in 2015 runs `prenext()`
  until 2015. Either trim the universe to the symbols listed at the start, or do the work
  in `prenext()` as well (and guard on `len(data) > 0` before reading a feed).
- **Stale prices.** A symbol that stops trading (delisted, suspended) keeps its last close
  for the rest of the run, and a position in it is valued there.
- **Memory.** Every feed is preloaded. A feed costs about 60 bytes per bar (seven float64
  lines); 2 000 symbols over 20 years of daily bars is about 600 MB. Do not add feeds the
  strategy never trades or reads.

## Bar sizes

Bars can be of any size: the clock is built from the feeds' own timestamps and does not
assume days. Mixing bar sizes in one run is not supported. Resampling (building weekly bars from daily ones inside
the engine) and replaying are not available yet: resample with pandas before adding the
feed.

## Writing a feed

A feed is a subclass of `tw.DataBase` that returns the whole history as arrays:

```python
class NumpyFeed(tw.DataBase):
    params = (("arrays", None),)       # {"datetime": float days, "open": ..., "close": ...}

    def _load_arrays(self):
        a = self.p.arrays
        n = len(a["datetime"])
        nan = np.full(n, np.nan)
        return a["datetime"], {k: a.get(k, nan) for k in
                               ("open", "high", "low", "close", "volume", "openinterest")}
```

`_load_arrays` returns the float-day times (sorted, unique) and one float64 array per line.
Live feeds will use the same class with a `poll` method; see `DataFeed` in
[`spec/05-interfaces.md`](../spec/05-interfaces.md).
