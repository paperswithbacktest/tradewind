# 04. Design: modules and interfaces

Written before the code, from `01-scope.md` and `02-semantics.md`. The names a strategy
sees are backtrader's public names; the internals are Tradewind's own and carry no
compatibility promise.

## Principles applied

* **One event loop.** `Cerebro.run()` advances a clock over the union of bar dates, and for
  every date: (1) each feed loads its bar for that date or repeats its last one, (2) the
  broker executes the orders accepted on the previous bar against this bar's prices, (3)
  indicators advance, (4) the strategy's `prenext`/`nextstart`/`next` runs and may place
  orders, (5) notifications are delivered, (6) observers record. A live source will emit
  the same events; the strategy never sees the arrays.
* **Vectorise across symbols, never across time.** Feed data are numpy arrays indexed by
  bar; the per-bar step does one Python iteration and the broker settles all symbols of a
  bar together.
* **Interfaces first.** `Broker`, `FillModel`, `CommInfoBase`, `Clock`, `Feed` are small
  abstract classes with one default implementation each; the compat defaults reproduce
  the kept behaviours of `02-semantics.md`.

## Modules (`src/tradewind/`)

| module | public names (compat) | role |
|---|---|---|
| `lines.py` | `LineBuffer`: `[ago]`, `get(ago, size)`, `__len__`; `Lines` with named aliases | a line is a numpy array plus a cursor; `[0]` is the cursor, negative ago walks back |
| `dateutils.py` | `date2num`, `num2date` | the float-day encoding strategies serialise with `bt.num2date` |
| `feeds.py` | `DataBase` (`_name`, `datetime`, `open`, `high`, `low`, `close`, `volume`, `openinterest`, `lines`, `__len__`), `PandasData(dataname, datetime, open, ..., nocase)` | a feed owns its arrays and a cursor the clock moves |
| `order.py` | `Order` with `Market/Limit/Stop/StopLimit/Close`, statuses `Created … Expired`, `executed.price/size/value/comm`, `data`, `size`, `isbuy()`, `issell()`, `alive()` | plain object, no behaviour |
| `position.py` | `Position(size, price)` | |
| `comminfo.py` | `CommInfoBase(commission, margin, mult, commtype, percabs, stocklike)`, `getcommission`, `getvaluesize`, `getoperationcost`, `profitandloss` | the interface pwb-toolbox's `SpreadImpactCommission` already implements |
| `broker.py` | `BackBroker`: `getcash/setcash/add_cash`, `getvalue(datas=None)`, `getposition(data)`, `setcommission(...)`, `set_coc`, `set_coo`, `submit/cancel`; internal `FillModel` | fills at next open (compat), cash check → `Margin`, commission from `CommInfoBase` |
| `strategy.py` | `Strategy`: `params`/`p`, `datas/data/data0…/dnames/getdatabyname`, `broker`, `buy/sell/close`, `order_target_size/value/percent`, `getposition`, `notify_*`, `start/prenext/nextstart/next/stop`, `add_timer`, `len(self)` | the user contract; `params` resolved at construction into a namespace |
| `indicator.py` + `indicators/` | `Indicator` (declared `lines`, `params`, `next()`/`once()`, `addminperiod`), `SMA/SimpleMovingAverage`, `EMA`, `Highest`, `Lowest`, `ATR`, `ROC/RateOfChange`, `RSI`, `MACD`, `CrossOver` | implemented from their definitions, one `once()` vectorised path and a `next()` path |
| `timer.py` | `Timer.SESSION_START/SESSION_END`, `add_timer(when, monthdays, monthcarry, weekdays, weekcarry)` | monthly/weekly triggers delivered to `notify_timer` |
| `analyzer.py` | `Analyzer` (`start/next/stop/get_analysis`), `TimeReturn`, `Returns` | what `backtest_engine` adds |
| `cerebro.py` | `Cerebro(stdstats=…)`: `adddata(data, name)`, `addstrategy(cls, **kwargs)`, `addanalyzer(cls, _name=…)`, `addobserver`, `broker`, `run()` → `[strategy]` | the loop |

## Clock

`Clock` yields the sorted union of the feeds' dates (daily today; intraday later through a
market calendar). A feed with no bar at the clock's date keeps its last bar and does not
advance `len(feed)` — this is what keeps `data.close[-1]` meaning "the previous bar of
this feed", as the corpus assumes.

## Broker, compat fill model

Orders placed during bar *t* are `Submitted` then `Accepted` immediately, and executed at
bar *t+1*: a market order at `open[t+1]`; a limit order at the limit if `low[t+1] <= limit`
(buy) with a cap at the open when gapped through; a stop order at the stop if `high[t+1]
>= stop` (buy) or at the open if gapped through; `Close` at `close[t]`. With `set_coc(True)`
a market order fills at `close[t]`. The cash check uses the fill price plus commission; a
failure is `Margin`. Sizes are integers (`order_target_percent` truncates toward zero);
fractional sizes are allowed when the comminfo is not `stocklike`.

## Value

`getvalue()` = cash + Σ `position.size × close[t]` over feeds with a position, at the bar
being processed (the close the strategy sees in `next()`).

## Not in v0

Resample/replay, plotting, `optstrategy`, live stores, multi-currency, settlement,
borrow costs — the interfaces exist (`FillModel`, `CommInfoBase`), the models come with
chantier 4.
