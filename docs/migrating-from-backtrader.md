# Migrating from backtrader

Tradewind implements the backtrader API: a strategy written for backtrader runs unchanged,
and gives the same results. The engine is a new implementation, written from the public
documentation and from black-box tests; it shares no code with backtrader, which is why it
can be released under Apache 2.0.

## The change

```python
import backtrader as bt          # before
import tradewind as bt           # after
```

Or, to run existing code without editing it, alias the module before it is imported:

```python
import sys
import tradewind
sys.modules["backtrader"] = tradewind
sys.modules["backtrader.feeds"] = tradewind.feeds
sys.modules["backtrader.indicators"] = tradewind.indicators
```

`bt.Cerebro()` keeps working: in Tradewind the class is called `Engine`, and `Cerebro` is a
permanent alias of it (so is `self.cerebro` inside a strategy, next to `self.engine`). The rest
of this documentation writes `tw.Engine()`.

## How "the same results" is checked

Every release runs a set of probe strategies on both engines and compares, bar by bar, the
portfolio value, every fill and every order state (CI job `oracle`, `tools/run_oracle.py`).
Beyond the probes, about 1 000 published strategies have been run on both engines with
identical value paths, to a relative tolerance of 1e-12 and in most cases bit for bit.

The behaviours that match are listed one by one in
[`spec/02-semantics.md`](../spec/02-semantics.md), with how each was observed.

## What is supported

- `Cerebro` (Tradewind's `Engine`, the same class under backtrader's name): `adddata`, `addstrategy`, `addanalyzer`, `broker`, `run`, `runonce`.
- `Strategy`: `params`, `__init__`, `start`, `prenext`, `nextstart`, `next`, `stop`,
  `notify_order`, `notify_trade`, `notify_timer`, `buy`, `sell`, `close`, `cancel`,
  `order_target_size/value/percent`, `getposition`, `positions`, `getdatabyname`,
  `add_timer`.
- Lines: `[ago]`, `get(ago, size)`, `datetime.date()`/`datetime()`/`time()`, `array`,
  arithmetic and comparisons in `__init__`, `bt.If`.
- Feeds: `PandasData` and `DataBase` subclasses with `fromdate`/`todate`.
- Orders: `Market`, `Close`, `Limit`, `Stop`, `StopLimit`; the full status machine.
- Broker: `setcash`, `getcash`, `getvalue`, `get_value(datas=)`, `add_cash`,
  `setcommission`, `addcommissioninfo`, `CommInfoBase` subclasses, `set_coc`,
  `set_checksubmit`, `set_shortcash`, `set_slippage_perc/fixed`, leverage.
- Indicators: SMA, EMA, Highest, Lowest, ROC, TrueRange, ATR, RSI, MACD, CrossOver,
  CrossUp, CrossDown, StandardDeviation; `bt.Indicator` subclasses with `next` or `once`,
  `addminperiod`, `updateminperiod`, line binding.
- Analyzers: `TradeAnalyzer`, `DrawDown`, `SharpeRatio`, `TimeReturn`, and `bt.Analyzer`
  subclasses.
- `bt.num2date`, `bt.date2num`, `bt.TimeFrame`, `bt.LineRoot`, `bt.LineIterator`.

## What is not supported (yet)

| feature | status |
|---|---|
| `resampledata`, `replaydata` | raises `NotImplementedError`; resample with pandas first |
| `optstrategy` | raises; run the parameter grid in a loop or a process pool |
| `plot` | raises; plot the values you record |
| observers (`stdstats`) | accepted and ignored; record the value in `next()` or use `TimeReturn` |
| sizers (`addsizer`) | accepted and ignored: give `size=` or use `order_target_*` |
| cheat-on-open (`cheat_on_open=True`, `set_coo`) | accepted and ignored: orders still fill at the next bar's open |
| `StopTrail`, `StopTrailLimit`, brackets, OCO, order expiry (`valid`) | not yet |
| `add_signal`, writers, live stores, CSV feeds other than through pandas | not yet |
| indicators beyond the list above | write them as `bt.Indicator` subclasses (see [Indicators](indicators.md)) |

A strategy that uses one of these fails loudly (an exception at the call) or is
unaffected (the ignored options); it never runs silently with a different meaning.

## Behaviours worth knowing

These are backtrader's behaviours, kept so results match. Some are surprising; four are
listed as defects to correct in a later version, behind a switch that will keep today's
results available.

- **Batch cash check, in placement order, with the refused orders' cost carried.** See
  [The cash check](orders-and-broker.md#the-cash-check). *(to be corrected)*
- **`cancel()` does nothing on an order not yet accepted** (one placed during the same
  `next()`): it is accepted and filled on the next bar. *(to be corrected)*
- **Positive `ago` reads the future**, and `line.array` holds the whole series.
  *(to be corrected)*
- **`get_value(datas=[d])` lands a pending `add_cash`** as a side effect.
  *(to be corrected)*
- **`runonce` freezes other feeds**: a `next`-only indicator is computed before the run, and
  a feed that is not one of its inputs sits at its last bar meanwhile. An indicator that
  reads another feed through a parameter sees one constant value. Pass that feed as an
  input, or run with `runonce=False`.
- **`setcash()` during a run** makes `getvalue()` the cash alone until the next bar.
- **Iteration over a `set`** in a strategy depends on `PYTHONHASHSEED`, on both engines.

## Speed

On the corpus, the median strategy runs 2.3 times faster than on backtrader, data loading
included, and the engine's own cost per security and bar is 30 to 100 times lower. A
strategy whose time goes into its own pandas or numpy work in `next()` gains less: see
[Performance](performance.md).
