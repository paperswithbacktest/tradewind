# 08. The typed API and the run statistics (chantier 5)

**Status: implemented on 2026-11-15 (`src/tradewind/typed.py`, `metrics.py`,
`performance.py`); defaults unchanged.** It delivers the public typed API that spec/05
§ 10 (Q6) left to chantier 5 -- `self.securities`, `notify_universe`, typed events -- and a
statistics analyzer at the level of Lean's backtest report.

Sources: `spec/05-interfaces.md`; QuantConnect Lean (Apache 2.0, read as a specification
from knowledge of its public API and documentation, attributed in `NOTICE`):
`QCAlgorithm` (`Initialize`, `OnData`, `OnOrderEvent`, `OnSecuritiesChanged`,
`SetHoldings`, `Liquidate`, `IsWarmingUp`, `Schedule.On`) and the statistics of its
backtest report (`PortfolioStatistics`: Sharpe, Sortino, probabilistic Sharpe ratio,
alpha, beta, drawdown, turnover, estimated capacity); pwb-toolbox's
`pwb_toolbox/performance/metrics.py` and the website's `lib/strategyRegistry.ts` (PWB's
own code: the catalogue's formulas). Lean is also a counter-example: its Python API is a
binding over C# classes, with PascalCase names and .NET types the Python type checker
cannot see; the typed API here is Python classes with annotations.

## 1. Constraints

| constraint | how it is met |
|---|---|
| One engine, no second execution path | a `TypedStrategy` runs inside a compatible `Strategy` (the *host*) that the engine builds like any other; every order goes through the host's compatible methods |
| Same NAV bit for bit | `tests/test_typed.py`: one strategy written both ways (indicators, a monthly timer, target weights, limit orders, closes, with and without commission), every mark and order identical as hex floats |
| No metaclass exposed | `TypedStrategy` is a plain `Generic` class (`type(TypedStrategy) is type`); the parameters are a dataclass, not a `params` tuple |
| IDE completion, type checking | annotations everywhere on the public surface, `py.typed`, `mypy --strict` in CI on the typed modules and on `tests/typing/typed_usage.py` (including lines that must fail) |
| Defaults unchanged | the engine changes are opt-in (a typed class, a strategy defining `notify_universe`); `tools/nav_parity.py` identical to `main` |

## 2. The typed strategy

`addstrategy(cls, **kw)` with `cls` a `TypedStrategy` subclass: `Engine._build_strategy`
replaces it by `cls._tw_host_class()`, a `_TypedHost` subclass generated once per class.
The host's `__init__` (run inside the build stage, so indicators register for the warm-up)
builds the parameters (`P(**kw)`, or `dataclasses.replace(params, **kw)`), creates the typed
instance without calling the metaclass machinery, binds `bars`, `portfolio`, `params`, then
calls `initialize()`. Hooks map one to one:

| host (compatible) | typed |
|---|---|
| `__init__` | `initialize()` |
| `start` / `stop` | `on_start()` / `on_end()` |
| `prenext` / `next` (and `nextstart`) | `on_warmup(BarEvent)` / `on_bar(BarEvent)` |
| `notify_order` | `on_fill(Fill)` per new execution bit (Partial, Completed), `on_order_event(OrderUpdate)` per status |
| `notify_timer` | `on_timer(TimerEvent)` |
| `notify_universe` (new, opt-in) | `on_universe_change(SecurityChanges)` |
| `notify_corporate_action` (spec/06) | `on_corporate_action(action)` |

The host defines `notify_order`, `notify_timer`, `notify_universe` and
`notify_corporate_action` only when the typed class overrides the matching hook, so the
engine's "notifications on demand" (spec/05 § 11, chantier 3) still skips order events
nobody reads. Orders: `order_target_weight` = `order_target_percent`,
`order_target_value`, `order_target_quantity` = `order_target_size`, `market_order` /
`limit_order` / `stop_order` = `buy` / `sell` with the exec type, `liquidate` = `close`.

Data: `Series` wraps a line and refuses a positive `ago` (look-ahead) and an index before
the first bar; `BarSeries` groups a feed's lines; `Portfolio` reads the broker. The run's
fingerprint records the typed class (`_tw_fingerprint_class`) and its parameters.

**Engine change for universes.** `Engine` now subscribes a handler to `UniverseEvent` that
calls `notify_universe(change)` on the strategies defining it (the compatible contract has
no such method; the event was published but not delivered before).

## 3. Statistics

`tradewind.metrics`: pure functions on a value series, the catalogue's and the toolbox's
definitions with the same float operations in the same order (Python's `sum`), so equal to
the last bit (`tests/test_metrics.py`, and against the installed toolbox when present:
checked on 2026-11-15 against pwb-toolbox `origin/main` `da3464d`). PSR: Bailey and López
de Prado (2012) with population moments. Capacity: Tradewind's own method, the
value-weighted 5 % quantile over fills of `NAV × participation × ADV$ / |fill value|`.

`tw.analyzers.Performance`: records at phase 14 of every slice (warm-up included) the
marked value, the benchmark's close, the signed market value of every position
(vectorised on `PortfolioState` and `MarketState`, converted by the cash book when the
quote currency is not the account's), and from `notify_order` each execution bit's value
and the security's average dollar volume. The differences with the toolbox and the
catalogue are listed in `docs/analyzers.md`.

## 4. Not in this chantier

Typed consolidators, typed option chains and a typed live API (they keep their compatible
objects, reachable through `security`, `BarSeries.feed` and `Series.line`); a `Slice`
object holding every bar of the slice (Lean's `OnData(slice)`): `bar.symbols` and
`self.bars` cover it without copying the bars.
