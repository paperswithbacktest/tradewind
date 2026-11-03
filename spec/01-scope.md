# 01. Scope: the compatible surface

Source: `inventory/backtrader-surface.md`, generated on 2026-11-01 from the 4 333
`strategy.py` of the Papers With Backtest corpus (4 202 published, the rest rejected
drafts) and the 25 modules of pwb-toolbox. Percentages are files out of 4 333.

The corpus does not use backtrader; it uses **BaseStrategy and a dozen idioms**. That is
what Tradewind commits to first, and why a compatible engine is realistic.

## Tier 1, the contract (every strategy depends on it)

| idiom | files | Tradewind commitment |
|---|---|---|
| `class X(BaseStrategy)` with `params = (...)`, `__init__`, `next()` | 100 % | identical: `params` tuple of pairs or dict, `self.p` / `self.params` attribute access |
| `self.datas`, `self.data`, `self.data0`, `self.getdatabyname()`, `data._name` | 99.9 % | a list of feeds in `adddata` order, each with `_name` |
| `self.datas[0].datetime.date(0)` / `.datetime(0)` | 85.6 % / 5.7 % | `datetime.date(ago)` and `datetime.datetime(ago)` on the datetime line |
| `line[0]`, `line[-k]` on `close`/`open`/`high`/`low`/`volume` | 82.7 % / 46.2 % | ago-indexing: 0 is the current bar, negative is the past; k ≤ 2 in 97 % of files, max 252 |
| `self.broker.getvalue()` | 70.3 % | portfolio value marked at the current bar's close |
| `order_target_size(data, target)` / `order_target_percent(data, target)` / `order_target_value` | 62.7 % / 37.7 % / 3.5 % | same semantics: difference to the current position, executed at the **next bar's open**, percent of current value |
| `close(data)` | 57.2 % | flatten the position with a market order |
| `get_latest_positions()`, `is_tradable()` | 49.8 % / 2.1 % | provided by BaseStrategy (pwb-toolbox), unchanged |
| `getposition(data).size` / `self.broker.getposition()` | 20.1 % / 1.6 % | position object with `size` and `price` |
| `self.broker.add_cash()` / `getcash()` / `setcash()` | 5.4 % / 3.4 % / 0.6 % | same |
| `stop()`, `start()` hooks | 5.2 % / 0.6 % | same lifecycle |

Not in the table and present in the inventory: `pivot_table`, `groupby`, `dropna`, `columns`
and the like under `data_obj:*` are pandas calls on variables named `data`; `imports_pandas`
55 % and `imports_numpy` 55 % say what the strategies compute with. The engine is not involved.

## Tier 2, the long tail (1 to 3 % each, 150 files import backtrader directly)

| idiom | files | commitment |
|---|---|---|
| `buy(data, size, exectype)` / `sell(...)` | 103 / 82 | market, limit (4), stop (3), close (9) execution types |
| `notify_order(order)` with `order.status` in Completed / Canceled / Margin / Rejected / Submitted / Accepted; `order.executed.price`, `order.data` | 102 | same state machine and names |
| `bt.Indicator` subclasses with `lines`, `self.lines.x[0]` | 33 | a `Indicator` base with declared lines, `next()`/`once()` |
| `bt.indicators.*`: SMA (14), Highest (7), Lowest (5), ATR (3), RateOfChange / ROC (5), EMA (2), RSI (2), MACD (2), CrossOver (2) | 27 | reimplemented from their mathematical definition |
| `bt.num2date()` | 30 | same float-day encoding as matplotlib's `date2num`, so serialized dates round-trip |
| `bt.feeds.PandasData` | 22 | a pandas feed with the same column mapping parameters (`datetime`, `open`, ... `openinterest`) |
| `bt.Cerebro`, `adddata`, `addstrategy`, `addanalyzer`, `run`, `broker.setcommission`, `set_coc` | 15 / 60 / 29 | the harness entry points pwb-toolbox uses |
| `add_timer(when=bt.Timer.SESSION_START, monthdays=, monthcarry=)` + `notify_timer` | 5 + toolbox | monthly timers |
| `lines.get(size=, ago=)`, slices | 39 / 17 | array views of a line |
| `bt.Analyzer`, `bt.CommInfoBase` (toolbox) | toolbox | pluggable analyzers and commission schemes |

## Tier 3, not committed

Everything else in backtrader: resampling and replaying, live stores, plotting, `optstrategy`,
observers beyond the value curve, line internals. A strategy that needs one of these is ported,
not supported. (`bt.If` and line arithmetic in `__init__`, the `LineRoot` / `LineIterator` names
and `bt.analyzers` `TradeAnalyzer` / `DrawDown` / `SharpeRatio`, one corpus file each, moved
into Tier 2 on 2026-11-03; see `02-semantics.md`.)

## pwb-toolbox surface (25 modules)

Six modules import backtrader: `base_strategy` (the contract), `backtest_engine` (Cerebro,
adddata, addstrategy, addanalyzer, broker, run), `commission` (`CommInfoBase`),
`strategies` (eleven portfolio strategies: timers, `order_target_percent`, `getposition`),
`indicators` (one `bt.Indicator`), `feeds` (`PandasData` subclass). These are the first
six files the codemod of chantier 2 rewrites.
