# Performance

## What the engine costs

Measured on an Apple M4 laptop, Python 3.13, synthetic daily bars
(`tools/bench_engine.py --symbols 2000 --years 3`):

| run | per security and bar |
|---|---|
| empty `next()`: clock, feeds, broker, mark | 0.23 µs |
| equal-weight rebalance of 2 000 names every month | 1.06 µs |

A 2 000-name universe over 20 years of daily bars (10 million security-bars) costs the
engine 2 to 11 seconds.

What a strategy's calls cost (`tools/bench_api.py`, per call):

| call | cost |
|---|---|
| `data.close[0]`, `data.close[-k]`, `getposition(d).size`, `broker.getvalue()` | 20 to 75 ns |
| `len(data)` | ~100 ns |
| `data.datetime.date(0)` | ~500 ns |
| `data.close.get(size=60)` | ~500 ns |
| `order_target_percent` (no trade) | ~900 ns |

## Where the time goes in real strategies

On the slowest strategies of a 4 200-strategy corpus, profiled with a stack sampler, the
engine loop is close to 0 % of the run. The time is in the strategy's own code (58 %), in
the libraries it calls from `next()` (pandas, numpy, scipy, scikit-learn: 29 %), and in its
reads of lines and dates (9 %). Loading data and computing costs before the run often weigh
more than the run itself on short strategies.

So the levers are in the strategy:

1. **Do the work once, not on every bar.** A signal that only changes at month-end does not
   need recomputing daily: test the date first and return.
2. **Read a window as an array, not value by value.** `[d.close[-i] for i in range(252, 0,
   -1)]` makes 252 calls per feed per bar; `d.close.get(size=252)` makes one.
3. **Compute series indicators in `__init__`.** An SMA built in `__init__` is computed once
   over the whole series, vectorised; the same average recomputed in `next()` from a list
   costs a window's work on every bar.
4. **Avoid building DataFrames in `next()`.** Constructing a `pd.DataFrame` or calling
   `.rolling()` per bar dominates most slow strategies. Keep numpy arrays, or precompute the
   series before the run and look them up by date.
5. **Cache the date.** Call `self.datetime.date(0)` once per bar and keep it in a variable.
6. **Drop the feeds you do not need.** Every feed is stepped on every slice.

## Comparing with backtrader

On about 800 strategies run on both engines, data loading included, Tradewind is 2.2 times
faster in total and 2.3 times at the median; the 10 % of strategies that gain least (1.0 to
1.3 times) are those whose time is in their own `next()`. The engine's own cost per
security and bar is 30 to 100 times lower than backtrader's (35 µs).
