# How a backtest runs

## The clock

Before the first bar, the engine merges the dates of every feed into one sorted **clock**.
Each entry of the clock is a *slice*: one point in time at which one or more feeds have a
bar. A feed with no bar on a slice (a holiday on its exchange, a symbol not listed yet, a
gap in the data) keeps its previous bar, and its length does not grow.

`len(self)` in a strategy counts the slices run so far; `len(self.data)` counts the bars
that feed has delivered. They differ as soon as one feed lists late.

## The sixteen phases of a slice

Every slice runs the same phases, in the same order. There is one code path: a backtest is
a source that replays history, and a live run will be a source that waits for the next bar.

| # | phase | what happens |
|---|---|---|
| 0 | clock | external events are pumped onto the event bus; the broker learns the time |
| 1 | universe | securities added or removed (when a universe provider is set) |
| 2 | market data | each feed with a bar on this slice moves to it |
| 3 | corporate actions | splits, dividends, delistings (when a provider is set) |
| 4 | accruals | settlement, borrow fees, margin interest (no-ops by default) |
| 5 | acceptance | the orders submitted during the **previous** slice are checked, as one batch, against the cash: `Accepted` or `Margin` |
| 6 | fills | accepted orders whose feed has a bar now are filled by the fill model |
| 7 | transfers | queued `add_cash` amounts land |
| 8 | mark | the portfolio value is computed at this slice's closes |
| 9 | margin call | the margin-call model may queue liquidations (none by default) |
| 10 | indicators | indicators that step bar by bar compute this slice |
| 11 | timers | timers set for the session start fire (`notify_timer`) |
| 12 | delivery | order notifications, then trade notifications (`notify_order`, `notify_trade`) |
| 13 | strategy | `prenext()`, `nextstart()` or `next()` |
| 14 | analyzers | analyzers step |
| 15 | timers | timers set for the session end fire |

Three consequences worth remembering:

- **An order placed in `next()` fills on a later slice.** It is submitted at phase 13,
  accepted at phase 5 of the next slice and, for a market order, filled at phase 6 of that
  same slice, at its open. With `engine.broker.set_coc(True)` the fill price becomes the
  close of the bar the order was placed on; everything else stays the same.
- **Inside `notify_order` you already see the fill bar.** Notifications are delivered at
  phase 12, after the feeds moved (phase 2) and the portfolio was marked (phase 8).
- **`getvalue()` is the value marked at phase 8**, once per slice. Orders placed during
  `next()` do not change it until they fill.

## Lines and `ago` indexing

Each feed exposes **lines**: `datetime`, `open`, `high`, `low`, `close`, `volume`,
`openinterest`. An indicator has its own lines. A line is read relative to the current bar:

```python
self.data.close[0]      # this bar's close
self.data.close[-1]     # the previous bar's
self.data.close[-20]    # twenty bars ago
self.data.datetime.date(0)       # this bar's date (a datetime.date)
self.data.datetime.datetime(0)   # with the time
self.data.close.get(size=20)     # the last 20 closes, oldest first, as a list
```

`self.data` is the first feed (`self.datas[0]`); `self.data0`, `self.data1`... and
`self.getdatabyname("SPY")` reach the others. `self.datetime` is the clock's own line: it
moves on every slice, even before `data0` has listed.

> **Look-ahead, kept for compatibility.** A line indexes the whole preloaded series. A
> *positive* `ago` (`close[1]`) reads a bar that has not arrived yet, and an `ago` before
> the first bar wraps around to the end of the series. backtrader behaves the same way and
> some published strategies rely on it (`datetime.date(1)` to spot the last session of a
> month). Do not use a positive `ago` in new code: a later version will make it raise by
> default. See [`spec/02-semantics.md`](../spec/02-semantics.md).

## When `next()` starts

`next()` runs once **every feed has delivered at least one bar** and **every indicator has
reached its minimum period**, each indicator counted on its own input. Until then the
strategy's `prenext()` runs; on the first eligible slice `nextstart()` runs (it calls
`next()` by default).

- An SMA(50) on `data0` holds the strategy in `prenext` for 49 bars.
- A feed that lists 30 bars after the others holds it for 30 bars; an SMA(5) on that late
  feed for 4 more.
- An indicator built on another one starts at the larger of the two warm-ups: SMA(20) of
  SMA(15) has its first value on bar 34.

A strategy that must act from the first bar, whatever the warm-ups, overrides `prenext()`
and does its work there too.

## Two stages: building and stepping

While a strategy's `__init__` runs, an operator applied to lines builds a new **line**,
computed over the whole series:

```python
def __init__(self):
    self.spread = self.data0.close - self.data1.close      # a line
    self.above = self.data.close > tw.ind.SMA(self.data.close, period=50)   # a line of 1.0/0.0
```

In `next()` the same operators work on the current values and return numbers or booleans:

```python
def next(self):
    if self.data.close[0] > self.data.open[0]:      # a bool
        ...
    if self.above[0]:                               # 1.0 or 0.0, read from the built line
        ...
```

A built line has no truth value: `if self.above:` in `__init__` raises `TypeError`, to stop
a condition that would be evaluated once instead of on every bar.

## The portfolio value

The value marked at phase 8 is the cash plus, for every open position, its size times this
slice's close (a long under a leveraged scheme counts its margin plus its open P&L). A
security with no bar on the slice is valued at its last close. Positions are summed in the
order they were first opened, then added to the cash: the same arithmetic as backtrader,
so the two engines produce the same values to the last bit.
