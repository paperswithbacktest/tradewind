# Typed API

Tradewind has two ways to write a strategy. The **compatible API** (`tw.Strategy`, a
`params` tuple, `self.data.close[0]`, `notify_order`) runs backtrader code unchanged. The
**typed API** is for new code: annotated classes, parameters in a dataclass, typed events,
completion and type checking in the IDE, no metaclass in sight.

Both run on the same engine, through the same orders, fills, cash checks and marks: a
strategy written both ways gives the same portfolio value on every bar, to the last bit
(`tests/test_typed.py` checks it).

```python
from dataclasses import dataclass

import tradewind as tw


@dataclass(frozen=True)
class TrendParams:
    period: int = 100
    weight: float = 0.45


class TrendFollowing(tw.TypedStrategy[TrendParams]):
    def initialize(self) -> None:
        self.trend = {s: self.indicator(tw.ind.SMA, s, period=self.params.period)
                      for s in self.symbols}
        self.schedule("rebalance", month_days=(1,))

    def on_timer(self, event: tw.TimerEvent) -> None:
        if self.is_warming_up:
            return
        for symbol in self.symbols:
            up = self.bars[symbol].close[0] > self.trend[symbol][0]
            self.order_target_weight(symbol, self.params.weight if up else 0.0)

    def on_fill(self, fill: tw.Fill) -> None:
        print(fill.time, fill.symbol, fill.quantity, fill.price)


engine = tw.Engine()
engine.adddata(tw.feeds.PandasData(dataname=spy_frame), name="SPY")
engine.adddata(tw.feeds.PandasData(dataname=tlt_frame), name="TLT")
engine.addstrategy(TrendFollowing, period=120)        # or params=TrendParams(period=120)
engine.addanalyzer(tw.analyzers.Performance, _name="performance", benchmark="SPY")
strategy = engine.run()[0]
print(strategy.analyzers.performance.report)          # see Analyzers
strategy.typed                                        # your TrendFollowing instance
```

The full script is [`examples/08_typed_api.py`](examples/08_typed_api.py).

## Parameters

Declare them as a dataclass (frozen is a good idea) and name it in the base class,
`tw.TypedStrategy[MyParams]`. `self.params` is then a `MyParams`: `self.params.period` is
an `int` for the type checker and the IDE. Values given to `addstrategy` by name override
the defaults; `addstrategy(MyStrategy, params=MyParams(...))` passes a whole instance (names
given next to it override its fields). An unknown name raises `TypeError`. A strategy
without parameters writes `tw.TypedStrategy` alone (`self.params` is `tw.NoParams()`); a
class can also set `params_type = MyParams` instead of the subscript.

## Hooks

| hook | when | compatible counterpart |
|---|---|---|
| `initialize()` | once, while the run is set up: create indicators and schedules here | `__init__` |
| `on_start()` | once, before the first slice | `start` |
| `on_warmup(bar)` | a slice before every feed has a bar and every indicator is warm | `prenext` |
| `on_bar(bar)` | every slice after that | `next` |
| `on_fill(fill)` | an execution of one of the strategy's orders | `notify_order` (Completed, Partial) |
| `on_order_event(update)` | any status change of one of its orders | `notify_order` |
| `on_timer(event)` | a schedule fired | `notify_timer` |
| `on_universe_change(change)` | securities entered or left the universe (with a universe provider) | none |
| `on_corporate_action(action)` | a split, dividend, delisting or option expiry was applied | none |
| `on_end()` | once, after the last slice | `stop` |

All are optional. The engine only builds what is listened to: a strategy without `on_fill`
or `on_order_event` costs no order event, as in the compatible API.

`bar` is a `tw.BarEvent`: `bar.time` (a `datetime`), `bar.date`, `bar.index` (the slice's
position in the run, from 0) and `bar.symbols` (the securities whose bar is at this slice).
`fill` is a `tw.Fill`: `time`, `symbol`, `quantity` (signed: positive bought), `price`,
`fee`, `ticket`. `update` is a `tw.OrderUpdate`: `time`, `symbol`, `status`, `ticket`.
`change` is a `tw.SecurityChanges` with `added` and `removed` tuples of feed names (see
[Names and ticker changes](#names-and-ticker-changes)).

## Data

| expression | type | meaning |
|---|---|---|
| `self.bars["SPY"]` | `BarSeries` | one security's bars |
| `self.bars["SPY"].close[0]`, `[-1]`, `[-k]` | `float` | this bar's close, the previous, `k` bars back |
| `.open`, `.high`, `.low`, `.close`, `.volume`, `.open_interest` | `Series` | the lines |
| `self.bars["SPY"].close.history(20)` | `numpy.ndarray` | the last 20 closes, oldest first |
| `len(self.bars["SPY"])` | `int` | bars that security has delivered |
| `self.bars["SPY"].time`, `.has_bar`, `.security` | | its last bar's time, a bar at this slice, the engine's record |
| `self.bars["SPY"].name`, `.symbol` | `str` | the feed's name (the key), the current ticker |
| `self.symbols` | `tuple[str, ...]` | every security, in `adddata` order |
| `self.time` | `datetime` | the current slice |
| `self.securities["SPY"]` | `Security` | contract terms, hours, holding (read-only) |

A `Series` reads the present and the past only: `close[1]` (tomorrow's bar, which the
compatible API serves for backtrader's sake) raises `IndexError`, and so does an index
before the first bar.

Indicators: `self.indicator(factory, source, **params)` builds any compatible indicator
(`tw.ind.SMA`, `tw.ind.RSI`, your own `tw.Indicator`) on a security (its name or its
`BarSeries`: the indicator reads the closes) or on a `Series`, and returns its output line
as a `Series` (`line="signal"` picks another of its lines). Build them in `initialize`:
the strategy then stays in `on_warmup` until they are warm, exactly as a compatible one.

### Names and ticker changes

A security is named by its feed's name, the `name` given to `adddata`, everywhere in the
typed API: the keys of `self.bars`, `self.securities` and `self.portfolio`, the `symbol`
argument of the order methods, `Fill.symbol`, `OrderUpdate.symbol`, `OrderTicket.symbol`
and the names in `SecurityChanges`. That name is stable for the whole run. When a
`SymbolChange` renames the security (FB became META on 2021-10-28, see [Universes and
corporate actions](universe-and-corporate-actions.md)), `self.bars["FB"]` still works and
the new ticker is an attribute: `self.bars["FB"].symbol == "META"` and
`self.securities["FB"].symbol == "META"`, while `self.bars["FB"].name` stays `"FB"`. A
strategy that keeps state per security keyed by name keeps it across the change.

## Orders and the portfolio

| method | what it does |
|---|---|
| `order_target_weight(symbol, weight)` | trade to a position worth `weight` × the portfolio value (Lean's `SetHoldings`; the compatible `order_target_percent`) |
| `order_target_value(symbol, value)` | trade to a position worth `value` |
| `order_target_quantity(symbol, quantity)` | trade to a position of `quantity` units |
| `market_order(symbol, quantity)` | buy (positive) or sell (negative) at the next bar's open |
| `limit_order(symbol, quantity, limit_price, valid=None)` | at `limit_price` or better |
| `stop_order(symbol, quantity, stop_price, valid=None)` | a market order once the price trades through `stop_price` |
| `liquidate(symbol=None)` | close one position, or all of them |
| `cancel(ticket)` | cancel an order not filled yet |

Each returns a `tw.OrderTicket` (or `None` when there is nothing to trade): `id`,
`symbol`, `quantity` (signed), `status` (a `tw.OrderStatus`), `filled_quantity`,
`average_fill_price`, `fees`; its fields follow the order as it fills. The semantics are
the compatible ones: whole units rounding toward zero for the target methods, the batch
cash check in placement order, fills at the next bar's open (see
[Orders and the broker](orders-and-broker.md)).

`self.portfolio.value` and `self.portfolio.cash` are the account; `self.portfolio["SPY"]`
is a `tw.Holding` (`quantity`, `average_price`, `market_value`, `is_long`, `is_short`);
iterating `self.portfolio` yields the symbols held.

## Schedules

`self.schedule("name", month_days=(1,))` calls `on_timer` on the first bar of every month
(`week_days=(1,)`: every Monday; `at="close"`: after `on_bar` rather than before;
`carry=False`: skip a day that has no bar instead of firing on the next one). It is the
compatible `add_timer` and follows the exchange clock when one is set.

## How it runs

`addstrategy` sees a `TypedStrategy` and builds a compatible strategy around it (a *host*,
generated once per class): the host's `next` calls `on_bar`, its `notify_order` calls
`on_fill`, and `order_target_weight` calls its `order_target_percent`. Nothing else in the
engine knows about the typed API. What `engine.run()` returns is that host: its
`analyzers` are the run's, and `host.typed` is your strategy object. The run's fingerprint
records your class, its source and its parameters.

The package ships `py.typed`, and CI runs mypy in strict mode on the typed modules
(`tradewind.typed`, `tradewind.metrics`, `tradewind.performance`) and on a user's view of
them (`tests/typing/typed_usage.py`), including lines that must *fail* to type-check.

## Coming from Lean

| Lean | typed API |
|---|---|
| `class MyAlgo(QCAlgorithm)`, `Initialize()` | `class MyStrategy(tw.TypedStrategy[P])`, `initialize()` |
| `GetParameter("period")` | a field of the parameters dataclass |
| `OnData(slice)` | `on_bar(bar)` |
| `OnOrderEvent(e)` with `e.Status == OrderStatus.Filled` | `on_fill(fill)`, or `on_order_event(update)` for every status |
| `OnSecuritiesChanged(changes)` | `on_universe_change(change)` |
| `OnEndOfAlgorithm()` | `on_end()` |
| `SetHoldings("SPY", 0.5)` | `self.order_target_weight("SPY", 0.5)` |
| `MarketOrder`, `LimitOrder`, `StopMarketOrder`, `Liquidate` | `market_order`, `limit_order`, `stop_order`, `liquidate` |
| `Securities["SPY"].Price` | `self.bars["SPY"].close[0]` |
| `History("SPY", 20)` | `self.bars["SPY"].close.history(20)` |
| `Portfolio["SPY"].Quantity`, `Portfolio.TotalPortfolioValue` | `self.portfolio["SPY"].quantity`, `self.portfolio.value` |
| `IsWarmingUp` | `self.is_warming_up` |
| `Schedule.On(DateRules.MonthStart(), ...)` | `self.schedule("name", month_days=(1,))` |
| `self.SMA("SPY", 50)` | `self.indicator(tw.ind.SMA, "SPY", period=50)` |

Unlike Lean's Python API, which is a binding over C# classes (PascalCase names, .NET
types, little the Python type checker can see), every name here is a Python class with
annotations: `self.params.period` is checked, `self.bars["SPY"].close[0]` is a `float`,
and a wrong argument is a type error before the backtest runs.
