# 06. Intraday bars and listed options (chantier 8, first part)

**Status: proposal and first implementation, 2026-11-15** (Édouard's decision of 2026-11-15:
start chantier 8, "QuantConnect depth", on the data PWB already has). The questions for
Édouard are in § 8. Nothing here changes a default: a feed without a contract multiplier,
without corporate actions and without consolidation runs exactly as before (§ 7).

Sources: `05-interfaces.md` (Security, SymbolProperties, the phases of a slice, the
corporate-action provider, the feeds), and QuantConnect Lean (Apache 2.0, read as a
specification, attributed in `NOTICE`): `TradeBarConsolidator` and the consolidator
`Scan`, `QCAlgorithm.Consolidate`, `Option` securities and `OptionSymbolProperties`,
`OptionChain` and `OptionFilterUniverse`, the Black-Scholes price model and the exercise of
contracts at expiry. backtrader's source is not a source (`CLEANROOM.md`); its
`resampledata` stays out of scope (`01-scope.md`, Tier 3).

## 1. What this adds

| piece | module | what it does |
|---|---|---|
| consolidators | `consolidators.py` | minute bars into N-minute, hourly, daily bars in the place's wall-clock time; a vectorised form and a streaming form that give the same bars; `Engine.consolidate` adds the consolidated feed |
| minute loader | `intraday.py` | `load_minute_bars`: a window of a few symbols read from monthly Parquet files, bounded in months, rows and columns |
| splits on a minute stream | `intraday.py` | backward adjustment of unadjusted minute prices, or `Split` actions applied by phase 3 at the first bar of the ex-date |
| option contracts | `options.py` | `OptionContract` (underlying, expiry, strike, right, style, multiplier 100, settlement), OCC symbols |
| multiplier | `broker.py`, `models.py` | `SymbolProperties.contract_multiplier` in cash flows, marks, P&L, checks and sizing of stock-like securities |
| pricing | `options.py` | Black-Scholes-Merton price, Greeks, implied volatility, vectorised |
| chains | `options.py` | `OptionChain` filtered by expiry, strike rank, moneyness, delta, right; `add_options` adds one feed per contract of a daily option table |
| expiry | `options.py` | `OptionExpiry`: exercise, assignment, cash settlement, worthless expiry, at phase 3 |

## 2. Consolidators

### 2.1 Conventions

* **Bars are stamped at their end.** A one-minute bar stamped 09:31 New York covers
  09:30-09:31 (Lean's `EndTime`). The minute files PWB serves are stamped at the start; the
  loader shifts them (§ 3).
* **A time bin is `(end - period, end]`** in the wall-clock time of the place (`tz`). With a
  session (`session=(open, close)` or `hours`, an `ExchangeHours` of spec/05 § 6.1) bins
  are anchored at the open and the last one is clipped at the close: 5-minute bars end
  09:35 ... 16:00, hourly bars 10:30 ... 15:30 and a 30-minute 16:00; bars outside the
  session (pre- and post-market) are dropped. Without a session bins are anchored at local
  midnight (Lean's `RoundDown`) and every bar is kept.
* **A consolidated bar is stamped at its bin end**, not at its last input bar. A daily bar
  is stamped at the session close (13:00 on an early close when `hours` knows it), or at the
  next local midnight without a session. Stamping at the last input bar would tell the
  strategy at 15:59 that no 16:00 bar is coming: a look-ahead on completeness.
* Aggregation: open first, high max, low min, close last, volume sum (NaN volume counts 0).
  An empty bin produces no bar; the engine's clock repeats the last bar as for any feed.
* Time zones: the offset is taken at each input bar, so the hourly bars of a New York
  session keep their wall-clock hours across the daylight-saving change (tested on
  2024-03-08 / 03-11). A bin straddling a change of offset (a 02:00 bin on a DST night,
  never inside an exchange session) would be one hour off.
* Count consolidation (`count=N`): every N consecutive bars, stamped at the N-th (complete
  when it arrives); a trailing incomplete group is dropped.

### 2.2 Two forms, one result

`consolidate(timestamps, columns, period, tz, session, hours)` computes every bar of a
series at once (numpy `reduceat`); `TimeBarConsolidator.update(bar)` then `scan(now)` does it
bar by bar, the form a live run needs. Over the same input, updating then scanning at every
input time and at pulses between them, the streaming form returns exactly the vectorised
bars, field by field (`test_streaming_equals_vectorised`, five periods, 30 % of the minutes
missing).

### 2.3 On the engine

`engine.consolidate(data, "5min", tz="America/New_York", session=(time(9, 30), time(16)))`
adds a `ConsolidatedData` feed computed from `data` when the run starts. A bin is a pure
function of the bars in it, so computing it before the run is the same exception spec/05
§ 7.4 makes for indicators: nothing carrying portfolio state is computed across time. On the
clock (the union of the feeds' stamps), the 5-minute bar ending 09:35 arrives in the same
slice as the minute bar stamped 09:35: read at 09:37, `data5.close[0]` is the 09:30-09:35 bar
(`test_consolidated_feed_sees_only_completed_bins`, every slice of two days checked). The
strategy's readiness gate applies: `next` starts once the consolidated feed has a bar.
`resampledata` keeps raising, with a pointer to `consolidate`: its semantics would have to be
observed on the oracle before Tradewind claims them.

## 3. The bounded minute loader

`load_minute_bars(files, symbols, start, end, tz=..., session=..., max_rows=...)` returns one
frame per symbol (UTC index, end-stamped, `open high low close volume`):

* it opens only the monthly files the window touches, plus one day of margin on each side
  (PWB's files are cut by UTC month while their stamps are New York wall-clock times: the
  crypto file of 2020-06 starts at 2020-05-31 20:00);
* it pushes the symbol filter and the date window (ISO strings compare chronologically) down
  to Parquet and reads seven columns;
* it raises `MemoryError` before it holds more than `max_rows` rows (5 million by default,
  about 300 MB in the frames).

`files` is a resolver `month -> path`: `month_files(directory)` for a local mirror,
`hub_month_files(repo_id, token)` to download one month at a time through
`huggingface_hub` (an optional dependency, imported only there).

The engine holds the loaded window in memory, about 56 bytes per bar and feed (seven
float64 lines): one symbol over sixteen years of regular sessions is 1.6 million bars, 90 MB;
500 symbols over one year, 2.5 GB. Choosing the window is what bounds a minute backtest.
Streaming a run longer than memory needs the `poll` path of `DataFeed` (spec/05 § 6.4,
live) to grow the line buffers window by window: not done here (§ 8, Q6).

Layout of the PWB minute files, measured 2026-11-15: `Stocks-1Min-Price` holds `symbol,
datetime, adj_open, adj_high, adj_low, adj_close, volume`, `datetime` a naive string in New
York time, stamped at the bar's start, extended hours 04:00 to 19:59 (391 stamps from 09:30
to 16:00: the 16:00 one is the first post-market minute); the `adj_` prices are **not**
adjusted for splits. `Cryptocurrencies-1Min-Price` has `open ... close`, the same New York
wall-clock stamps, and an integer volume. `PWB_MINUTE_COLUMNS` maps the stock layout; pass
`columns=` for the other.

## 4. Splits on an unadjusted minute stream

Two ways, both from a split table with one row per split (`read_splits`: `symbol`, `date` =
ex-date, `split_from`, `split_to`; ratio = `split_to / split_from`, the layout of PWB's
`Stocks-Daily-Splits`):

* `adjust_for_splits(frame, splits, tz)`: prices divided and volume multiplied by the
  product of the ratios of the splits after each bar's trading day (in `tz`): the series in
  today's shares, what an adjusted daily feed gives.
* `split_actions(data, splits, tz)`: the prices stay as quoted and each split becomes a
  `Split` action at the feed's **first bar of the ex-date** (the pre-market minute when the
  feed has it), which phase 3 applies before any fill (spec/05 § 5.8, decision Q2): quantity
  × ratio, average price ÷ ratio, open orders' sizes × ratio and prices ÷ ratio.
  `engine.add_corporate_actions(StaticCorporateActions(actions))`.

Checked on real data (NVDA, regular sessions 2024-06-05 to 06-12, `Stocks-1Min-Price` and
`Stocks-Daily-Splits`): the raw overnight return into the 10-for-1 split of 2024-06-10 is
−90.04 %, adjusted −0.41 %; `detect_splits` finds exactly that split (jump 0.0996, ratio
10). A buy-and-hold of 10 shares on the raw minute stream with the split action ends with 100
shares and a value that moves at most 0.24 % from one minute to the next over the week; the
same run without the action loses 10.9 % in one minute on the ex-date.

## 5. Listed options

### 5.1 The contract and its security

`OptionContract(underlying, expiry, strike, right, style="american", multiplier=100,
settlement="physical")` carries an OCC symbol (`SPY190118P00250000`; `parse_occ` reads it,
padded or after a `PREFIX:`). Its `SymbolProperties`: `contract_multiplier` = 100, tick 0.01.
An `OptionData` feed (a `PandasData` of the contract's daily bars, prices per share) carries
the contract and its properties; the broker gives them to the feed's `Security` when the
feed is added.

### 5.2 The multiplier in the broker

For a stock-like security whose `contract_multiplier` m is not 1: the cash flow of a fill is
`size × price × m` (opening) and `entry value × m + P&L × m` (closing), the mark is
`quantity × price × m`, `get_value(datas)` is `getvalue × m`, the batch pre-check and the
fill re-check use the same flows, `order_target_value` sizes on `price × m` (1 050 of value at
2.00 gives 5 contracts), `executed.value` is in money. Every one of these is guarded by
`m != 1.0`, so a plain feed runs the code it ran before: the 50 runs of `tools/nav_parity.py`
(the oracle probes and the benchmark scenarios, 7 800 marks, 5 007 orders) are identical bit
for bit to `main`. A `CommInfoBase` with `mult` keeps its own (futures-like) meaning;
commissions are the scheme's, so an option's commission is set per contract (`COMM_FIXED`),
a percentage scheme reading the per-share premium.

### 5.3 Pricing

European Black-Scholes-Merton with a continuous dividend yield `q`, vectorised:
`bsm_price`, `bsm_greeks` (`delta`, `gamma`, `vega` per 1.00 of volatility, `theta` per
year, `rho` per 1.00 of rate: divide by 100 and 365 for the per-point and per-day
conventions of quote screens), `implied_volatility` (bisection on [1e-4, 5], 80 steps, NaN
outside the no-arbitrage bounds). The normal CDF is `erfc` from the standard library (no
SciPy). Tests: the textbook 10.4506 / 5.5735, put-call parity at 1e-12, every Greek against
a finite difference, IV round trips at 1e-10. On PWB's daily option table (SPY, 2019, 25 253
puts with volume ≥ 100, 20 to 45 days, strike within 5 % of the spot), the European IV from
the close with a flat r = 2 % and q = 1.8 % differs from the table's own (American, discrete
inputs) by a median of 0.01 volatility point, 0.08 in absolute value. American early
exercise is not priced: the bisection gives a European volatility.

### 5.4 Expiry: exercise, assignment, settlement

At **phase 3 of the first slice after the expiry date** (before that slice's fills), each
contract that expired produces an `OptionExpiry` action:

1. open orders on the contract end: an accepted one is cancelled, one still waiting for its
   batch acceptance expires (the contract has no bar left to fill on); the security is
   marked delisted;
2. the settlement price is the underlying's last close on or before the expiry date (PM
   settlement). AM-settled index options (monthly SPX) settle on the next morning's opening
   print, which a daily feed does not carry: they would need a settlement series (§ 8, Q3);
3. a position in the money by at least 0.01 (OCC's exercise-by-exception threshold) is
   exercised when long and assigned **in full** when short; otherwise it expires worthless.
   No early exercise or assignment is modelled;
4. the option leaves the books at its intrinsic value (realised P&L = (intrinsic − average
   price) × quantity × m); physical settlement moves `quantity × m` shares of the underlying
   at the strike (a long call or a short put receives them, cash − shares × strike; a
   covered call's assignment closes the shares, with their realised P&L); cash settlement
   pays `quantity × intrinsic × m`.

The value is conserved: just before the action the option is worth `q × m × intrinsic` at
the settlement price, just after, the shares and the cash paid for them are worth the same.
The strategy hears of it through an **opt-in** `notify_corporate_action(action)` (phase 12),
the action filled in with `settle_price`, `exercised`, `quantity`, `delivered`, `cash`. It is
not reported as an order or a trade (§ 8, Q2).

### 5.5 Chains and the option universe

`OptionChain` is the contracts of one underlying on one date against a spot. Every filter
returns a new chain: `calls_only`, `puts_only`, `expiration(min_days, max_days)`,
`front_month`, `standards_only` (third Fridays), `strikes(-n, +n)` (ranks around the strike
nearest the spot, Lean's `Strikes`), `moneyness(lo, hi)` (strike / spot), `delta(lo, hi)`
(the table's delta, or BSM at the implied volatility computed from the close), `liquid`,
`nearest(strike=, days=)`, `with_greeks`.

`add_options(engine, underlying_feed, table, select=...)` adds one `OptionData` per contract
of a daily option table (one row per contract and day it traded: `symbol, date, underlying,
expiration, strike, option_type, open, high, low, close, volume`, PWB's columns). The engine
cannot add feeds during a run, so `select` is applied to every date's chain before the run
and the **union** of the contracts it keeps gets a feed: the strategy can pick any of them
on any date, and a filter such as "puts, 0 to 62 days, 85 % to 102 % of the spot" keeps the
feed count in the thousands rather than the tens of thousands. The returned
`OptionUniverse.chain()` gives the strategy the current chain restricted to those feeds.
Option feeds carry `_tw_gate = False`: a contract lists when it first trades, and it does not
hold the strategy in `prenext` until then (any other feed still does, as observed on the
oracle). A contract that did not trade on a day repeats its last bar (the engine's rule);
`fill="model"` fills those days with a BSM price at its last implied volatility, volume 0.

Measured on the Mac (Apple Silicon, 2026-11-15): the cash-secured put example on SPY 2019
(685 682 rows, 12 047 contracts selected and added as feeds, 252 days) runs in 6.4 s with 1.2
GB of peak memory, loading included; it sells about four puts a month and ends at 104 470
for 100 000 (the underlying for that run is the raw SPY close, open = close: no OHLC raw
series was at hand).

## 6. With the calendars of chantier 4 (`calendars.py`, `clock.ExchangeClock`)

Rebased on the calendar work (PR #17) on 2026-11-15:

1. **Hours.** `consolidate(..., hours=...)` and `Engine.consolidate` take an
   `ExchangeHours` (`calendars.NYSE`, or its name `"NYSE"`): sessions, holidays and early
   closes come from `hours.session(day)`, the wall-clock zone from `hours.tz` (the
   `ExchangeCalendar` attribute; the spec/05 § 6.1 protocol itself has no zone: proposal, add
   it). Tested on Thanksgiving 2024 and its 13:00 close with the shipped NYSE calendar.
2. **End-stamped bars** on both sides: `ExchangeClock` takes bars as stamped at their end; a
   consolidated feed's stamps are ordinary feed stamps on it, and its fill-forward slices
   leave the consolidated bars unchanged.
3. **`ns2num`** is `clock.ns2num` (the duplicate in `dateutils.py` was removed).
4. **Still on dates, to move onto sessions:** the expiry cutoff of `OptionExpiries` (the day
   after expiry, midnight UTC: right for daily feeds; with an intraday underlying including
   winter post-market bars, 20:00 New York = 01:00 UTC, it would apply during the expiry
   day's last post-market hour; with hours, it becomes `hours.session(expiry)[1]`) and the
   time of `split_actions` (the first bar of the ex-date in `tz`).

## 7. What stays unchanged

No default changes. The new behaviours fire only for: a feed carrying `symbol_properties`
with a multiplier other than 1 (an `OptionData`), a feed flagged `_tw_gate = False`, a
corporate-action provider set by the user, a consolidated feed added by the user, a strategy
defining `notify_corporate_action`. `tools/nav_parity.py`: 50 runs identical
bit for bit to `main` (after #16, #17 and #18); the 169 tests of `main` pass unchanged, under pandas 2.3
and 3.1; the similarity gate passes
(19 allowed ranges, 0 new).

## 8. Questions for Édouard

1. **When an expiry applies.** At the first slice after the expiry date (proposed, phase 3,
   like a split), or at the end of the expiry day itself (a phase after the mark)? The first
   keeps one place for every corporate action and never touches a slice after its
   strategy step; the second shows the delivered shares in that day's NAV.
2. **Exercise and assignment as orders.** Reported only through the opt-in
   `notify_corporate_action` (proposed for now), or as synthetic orders and trades (Lean
   emits an `OrderEvent` per exercise), so that `notify_order`, `TradeAnalyzer` and the trade
   list see them? The second touches the order numbering of runs that hold options only.
3. **Settlement of AM-settled index options.** A settlement-price series (SOQ) per index, or
   the open of the next day as an approximation?
4. **Marks on days a contract did not trade.** Last trade (the engine's rule, proposed as
   the oracle-like default) or a model price (`fill="model"`) for the realistic profile?
5. **Early assignment** (calls before an ex-dividend date, deep in-the-money puts): model it
   in the realistic profile, or leave it to the strategy?
6. **Minute runs longer than memory.** The bounded window is enough for event studies and a
   few symbols over years; a wide universe at the minute needs the feeds to grow window by
   window (the live `poll` path). Worth doing before the live work of chantier 7, or with it?
7. **A public sample.** The examples run on synthetic data; the real-data checks above read
   PWB's own files on the Mac. Publish a small open sample (a month of SPY minutes, a year of
   one option chain) for the docs, or keep the examples synthetic?
