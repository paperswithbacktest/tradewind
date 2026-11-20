# Performance

## What the engine costs

Measured on an Apple M4 laptop, Python 3.13, synthetic daily bars, best of nine runs
(`tools/bench_engine.py --symbols 200 --bars 500`; the figure is the wall time of
`Engine.run`, loading the feeds included, divided by securities × bars):

| scenario | 0.0.2 | now |
|---|---|---|
| `idle`: empty `next()` -- clock, feeds, broker, mark | 0.22 µs | 0.11 µs |
| `reader`: `len(d)`, `d.close[0]`, `d.close[-1]` of every feed, every bar | 0.46 µs | 0.26 µs |
| `rebalance`: equal weight on every name each month (`order_target_percent`) | 0.93 µs | 0.53 µs |
| `notified`: the same with `notify_order` and `notify_trade` defined | 0.91 µs | 0.68 µs |
| `indicators`: an SMA(20) per feed read every bar, weekly rotation | 1.26 µs | 0.77 µs |

`daily` (`order_target_percent` on every name at every bar, the weights rotating so every
name trades) is the broker's stress case, outside the budget's scenarios.

On 1 000 names × 250 bars the same scenarios go from 0.29 / 0.50 / 0.95 / 0.95 / 1.30 µs
to 0.17 / 0.31 / 0.56 / 0.70 / 0.84 µs. The budget of `spec/05-interfaces.md` § 7.2 is
3 µs per security-bar on 4 000 names, about 0.6 µs of it for the engine; the figure on
4 000 names comes from

    python tools/bench_engine.py --symbols 4000 --years 3 --repeat 3 --json bench_4000.json

What a strategy's calls cost (`tools/bench_api.py`, per call):

| call | cost |
|---|---|
| `data.close[0]`, `data.close[-k]`, `broker.getvalue()`, `getposition(d).size` | 20 to 90 ns |
| `len(data)` | ~80 ns |
| `data.datetime.date(0)` | ~120 ns |
| `data.close.get(size=60)` | ~500 ns |
| `order_target_percent` (no trade) | ~900 ns |

### How the engine keeps its share small

* **One cursor per feed.** The seven lines of a feed (datetime, open, high, low, close,
  volume, open interest) are views on one array per field and share one cursor; moving to
  the next bar is one increment per feed. A line read is a memoryview index.
* **Indicators follow their feed.** An indicator computed before the run (every built-in,
  any `once()`, a line bound in `__init__`, a `next()`-only indicator under `runonce`) shares
  its feed's cursor: it costs nothing per bar. Only an indicator stepped live
  (`runonce=False` with a `next()`) is visited on every bar.
* **Notifications on demand.** An order or trade notification is a snapshot of the order
  at that moment. When no strategy or analyzer of the run defines `notify_order` (or
  `notify_trade`), none is built; defining one turns them all back on.
* **The default models without their wrappers.** With the default fill, fee and settlement
  models a market order fills at the open through the slippage model and the commission
  scheme directly; a model of your own, or a subclass of a default one, takes the general
  path through the interfaces of `spec/05-interfaces.md` § 5.
* **The broker works on the whole batch.** When a bar's batch holds at least 32 orders and
  every one of them is a plain market order under the default models (`ModelSet.oracle()`,
  no slippage at the open, a stock-like scheme without leverage, the account currency), the
  batch check and the fills run on arrays: prices gathered from the market state, fees and
  cash flows computed for the whole batch, the running cash summed left to right with
  `np.add.accumulate` -- the same float operations, in the same order, as one order at a
  time, so every result is identical bit for bit. A commission scheme with its own
  `_getcommission` (pwb-toolbox's `SpreadImpactCommission`) is still called once per
  order. Any other batch, or a bar with two orders on one security, goes one order at a
  time; an order whose cash re-check fails at the fill is handled one by one from the exact
  cash, then the batch resumes.
* **Orders stay in submission order.** The order is deterministic and it is the oracle's:
  the batch check walks the orders in the order they were placed, the fills in the order
  they were accepted, because each one moves the cash the next one is checked against. A
  sort by symbol (Lean's) would change which order a short cash refuses, so it is not used.

On 4 000 names with every name trading every bar (`daily`, the broker's worst case),
the batch path halves the acceptance phase (1.36 to 0.67 µs per order) and cuts the fill
phase by a quarter (4.3 to 3.4 µs per order); the rest of that scenario's 7.3 µs per
security-bar is the strategy's own `order_target_percent` calls (building the orders) and
the per-order bookkeeping of the fills (positions, executions, trades).

### Checking that a speed change changes nothing

Every speed change must leave every float of every run unchanged.
`tools/nav_parity.py` runs the oracle probes and the benchmark scenarios on a small
universe and records every mark, fill, status and commission as a hex float; two
checkouts are compared with

    TRADEWIND_SRC=/path/to/main/src python tools/nav_parity.py --out /tmp/nav_main.json
    python tools/nav_parity.py --out /tmp/nav_new.json
    python tools/nav_parity.py --compare /tmp/nav_main.json /tmp/nav_new.json

`tests/test_hot_paths.py` checks each shortcut against the path it skips, bit for bit.

## Against backtrader

The reference benchmark runs the same five scenarios, on the same seeded bars, with the same
strategy classes, on both engines and on the same machine; backtrader runs in its own
environment. Each scenario must end at the same final value on both engines before its
times are compared.

    TW_PY=<python> BT_PY=<python of an environment with backtrader> tools/bench_reference.sh small
    TW_PY=<python> BT_PY=<python of an environment with backtrader> tools/bench_reference.sh wide

**5 securities × 20 years** (5 040 daily bars), Apple M4, Python 3.13.12, numpy 2.2.4 and
pandas 2.3.1 in both environments, backtrader 1.9.78.125 (best of 3) against Tradewind 0.0.2
`main` (best of 9), 19 Nov 2026 (`bench/small-mac-mini-20261119/`):

| scenario | backtrader | Tradewind | speed-up | same final value |
|---|---|---|---|---|
| `idle` | 32.8 µs | 0.41 µs | ×80 | yes |
| `reader` | 33.4 µs | 0.57 µs | ×59 | yes |
| `rebalance` | 37.4 µs | 3.03 µs | ×12 | yes |
| `notified` | 37.0 µs | 3.23 µs | ×11 | yes |
| `indicators` | 38.8 µs | 3.10 µs | ×13 | yes |

With five names, Tradewind's fixed cost per bar is spread over few securities, so its
figure is higher than on a wide universe. backtrader's stays near 35 µs whatever the width.

**4 000 securities × 20 years**: pending. backtrader needs about an hour per scenario on this
case, so it runs once on an idle machine (the data host, Intel Xeon E-2386G, 62 GB) and its
results are committed under `bench/wide-*` and copied here. The small case also runs in CI on
every push; times on shared runners are informational only.

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
5. **Cache the date.** Call `self.datetime.date(0)` once per bar and keep it in a variable
   rather than once per feed.
6. **Drop the feeds you do not need.** Every feed is stepped on every slice.

## Comparing with backtrader

On about 800 strategies run on both engines, data loading included, Tradewind is 2.2 times
faster in total and 2.3 times at the median; the 10 % of strategies that gain least (1.0 to
1.3 times) are those whose time is in their own `next()`. The engine's own cost per
security and bar is 30 to 100 times lower than backtrader's (35 µs).
