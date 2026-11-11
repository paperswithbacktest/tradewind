# Coming from QuantConnect Lean

Tradewind takes Lean as its design reference for what a backtester must model (securities,
reality models, the order of events in a bar) and keeps the backtrader programming model for
strategies. Both are Apache 2.0. Tradewind runs in plain Python, locally, with no .NET and no
container, on data you load yourself.

## Concept map

| Lean | Tradewind | notes |
|---|---|---|
| `QCAlgorithm` | `tw.Strategy` | |
| `Initialize()` | `__init__()` and `start()` | data is added outside the strategy, on `Cerebro` |
| `OnData(slice)` | `next()` | one call per slice of the clock; read bars through the feeds, not a `Slice` object |
| `OnWarmupFinished`, `SetWarmUp` | `nextstart()`; warm-up is implicit | `next()` waits for every indicator's minimum period |
| `AddEquity("SPY")` | `cerebro.adddata(tw.feeds.PandasData(dataname=df), name="SPY")` | you bring the data |
| `SetCash(100000)` | `cerebro.broker.setcash(100_000)` | |
| `SetStartDate`, `SetEndDate` | `fromdate=`, `todate=` on the feed | |
| `Securities["SPY"]` | `self.getdatabyname("SPY")`; `data.security` for the engine's record | |
| `self.Securities[s].Price` | `data.close[0]` | |
| `History(symbol, 20)` | `data.close.get(size=20)` | the last 20 values, oldest first |
| `Portfolio.TotalPortfolioValue` | `self.broker.getvalue()` | |
| `Portfolio.Cash` | `self.broker.getcash()` | |
| `Portfolio["SPY"].Quantity` | `self.getposition(data).size` | |
| `SetHoldings("SPY", 0.5)` | `self.order_target_percent(data, target=0.5)` | whole units, rounding toward zero |
| `Liquidate("SPY")` | `self.close(data)` | |
| `MarketOrder`, `LimitOrder`, `StopMarketOrder`, `StopLimitOrder`, `MarketOnCloseOrder` | `buy`/`sell` with `exectype=Market`, `Limit`, `Stop`, `StopLimit`, `Close` | |
| `OnOrderEvent(orderEvent)` | `notify_order(order)` | |
| `Schedule.On(DateRules.MonthStart(), TimeRules.AfterMarketOpen(...), fn)` | `add_timer(when=tw.Timer.SESSION_START, monthdays=[1])` + `notify_timer` | |
| `self.SMA(symbol, 50)` | `tw.ind.SMA(data.close, period=50)` in `__init__` | |
| `IFeeModel` | `FeeModel`, or a `CommInfoBase` subclass | |
| `IFillModel` | `FillModel` | default: next bar's open, whole size |
| `ISlippageModel` | `SlippageModel` | |
| `IBuyingPowerModel` | `BuyingPowerModel` | default: cash check in batches |
| `ISettlementModel` | `SettlementModel` | default: immediate |
| `IShortableProvider` | `ShortableProvider` | default: always shortable, no fee |
| `IMarginInterestRateModel` | `MarginInterestRateModel` | default: none |
| `security.SetFeeModel(...)` | `cerebro.broker.set_models(data, fee=...)` | any of the eight, per security |
| `CashBook` | `broker.cashbook` | single currency for now |
| Universe selection, `OnSecuritiesChanged` | `UniverseProvider` (interface only) | planned |
| Splits, dividends, delistings | `CorporateActionProvider` (interface only) | planned; today, adjust prices before loading |
| Consolidators | not yet | resample with pandas |
| `MarketHoursDatabase`, time zones | not yet | the clock is the union of the feeds' dates |
| Options and futures chains | not yet | |
| Alpha / Portfolio construction / Execution / Risk framework | not planned for now | write it in the strategy |
| Live brokerages | not yet | the interfaces (`Broker`, `DataFeed`) are designed for it |

## Order of events in a bar

Lean and Tradewind both process a time slice in a fixed order. Tradewind's sixteen phases
are listed in [How a backtest runs](concepts.md#the-sixteen-phases-of-a-slice). The main
difference from Lean's defaults: a market order placed in `next()` fills at the **next**
bar's open, not at the current bar's close or at the next tick.

## What to expect

- **Speed**: the engine loop costs 0.2 to 1 µs per security and bar in Python; a 2 000-name
  daily universe over three years runs in under two seconds when the strategy itself does
  little.
- **Data**: nothing is bundled. Any DataFrame works; the [Papers With
  Backtest](https://paperswithbacktest.com) datasets plug in through its SDK.
- **Fidelity**: the default models reproduce backtrader, not Lean. Volume-capped fills,
  T+1 settlement, borrow costs and margin calls come with the realistic model set.
