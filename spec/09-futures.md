# 09. Futures: contracts, margin, daily settlement, expiry, explicit rolls, continuous series

Proposal and first implementation, 2026-11-27 (`src/tradewind/futures.py`,
`tests/test_futures.py`). Concepts read from QuantConnect Lean's public documentation
(Future securities, their margin model, continuous contracts and their adjustment modes);
the code is Tradewind's own. No real chain is published yet: every test runs on synthetic
contracts. Édouard's answers of 2026-11-27 to the questions of § 10 are applied (§ 10 now
records them as decisions).

## 1. What this adds

A futures position is not a stock position with a multiplier. No notional changes hands;
the account posts a margin to open and to hold, and the exchange settles the position every
day. A contract expires, and a strategy that wants to stay exposed must **roll** into the
next one, paying for it. Signals are computed on a continuous series that a backtest must
never trade. Everything below fires only for a `FutureData` feed or a `ContinuousData`
feed: `tools/nav_parity.py` (82 runs, 12 008 marks, 40 173 orders) is identical bit for bit
to `main`, and the similarity gate passes unchanged (19 allowed, 0 new).

## 2. The contract and its feed

`FutureContract(root, symbol, last_trade_date, multiplier, tick_size, initial_margin,
maintenance_margin=None, currency, first_trade_date=None)`. Margins are money per contract
(exchanges publish them that way); maintenance defaults to the initial margin and may not
exceed it. `FutureData` is a `PandasData` of the contract's daily bars in points, carrying
the contract and its `SymbolProperties` (multiplier, tick). Its **close is the settlement
price**: `future_table` puts the table's `settlement` column there when it has one (the
traded close is kept as `last`). A contract lists when it first trades and does not hold a
strategy in `prenext` (`_tw_gate = False`). Daily bars only: a `FutureData` whose bars
carry a time of day, or whose `timeframe` is under a day, is refused (`ValueError`, § 10.3). `FuturesCommission(commission=c)` charges `c`
per contract and side. Fill prices are rounded to the tick (after slippage).

## 3. Fills and margin (`FuturesMargin`, the contract's buying-power model)

* **Cash flows.** A fill moves no notional. Each position carries a *settlement
  reference*: its entry price until its first settlement, then the last settlement price
  (averaged in when contracts are added). Closing `k` contracts moves their unsettled P&L,
  `k × (fill − reference) × multiplier`; opening moves only the fee. A reversal is its
  closing part then its opening part, each with its own fee. The realised P&L on the trade
  and on `portfolio.realized_pnl` is the full `(exit − average entry) × q × multiplier`;
  `executed.value` is the notional in money.
* **Initial margin.** Opening contracts requires `initial_margin` per contract on top of
  the initial margin of the open futures book, posted from the cash. At the batch check
  (phase 5) the order is priced at its submission bar's close and the running cash carries
  the margin it blocks (or frees) plus its fee; at the fill (phase 6) the opening part is
  checked again; a reversal that does not fit fills its closing part only (Partial, then
  Margin). Reducing or closing always passes.
* **Sizing.** `order_target_value` / `order_target_percent` size on the notional,
  `price × multiplier` per contract, truncated toward zero; `get_value(datas=[f])` is the
  notional too.

## 4. Daily settlement and margin calls (`FuturesBook`, `broker.futures`)

* **Settlement, phase 8.** At every bar of a contract's feed (daily bars: every day), the
  unsettled P&L `q × (settlement − reference) × multiplier` moves to the cash before the
  mark, and the reference becomes the settlement. The mark of a position is its unsettled
  P&L, so the portfolio value is unchanged by the settlement and equals the cash right after
  it in a futures-only account. Each settlement is logged as a `VariationMargin`.
  Over a position's life the flows add up to its realised P&L (tested).
* **Margin call, phase 9.** When the portfolio value falls below the maintenance margin of
  the futures book, a `MarginCall(equity, maintenance, initial, deficit)` is recorded,
  `deficit` being what brings the equity back to the *initial* margin.
  `on_margin_call="liquidate"` (default): every futures position is cut pro rata by market
  orders flagged `margin_call`, filled at the next open, so that the initial margin of what
  remains fits in the equity; no new call while they are open. `"record"`: the call is
  recorded at every settlement under the maintenance margin and nothing is traded. When a
  profile's own margin-call model is set (`set_profile(..., margin_call=True)`), it already
  cuts the book, futures included: the futures call is recorded only.

## 5. Expiry: no delivery; a position held to it stops the run (`FutureExpiry`)

At phase 3 of the first slice after the last trading date, like an option's expiry
(spec/06 § 5.4): open orders on the contract end (accepted: cancelled; waiting for
acceptance: expired) and the security is delisted. What happens to a position still open
is `add_futures(..., at_expiry=...)`:

* `"raise"` (**default**): the run stops with `FutureExpiryError`, naming the contract, the
  contracts held, the last trading date and the three ways out (roll with a `Roller`,
  close before, or `at_expiry="close"`). Whether a strategy will hold a contract to its
  end is only known when it does, so the error comes at the expiry, not at construction;
  it comes whether or not a `Roller` placed the position: a `Roller` whose rule decides
  on the last trading day itself fills after it (§ 6).
* `"close"`, an explicit choice: the position is **closed by force** at the last
  settlement on or before the last trading date: its unsettled P&L is paid, the scheme's
  fee charged, its trade closed (trade list, `notify_trade`). The strategy hears of it
  through the opt-in `notify_corporate_action` (`forced`, `quantity`, `settle_price`,
  `cash`, `fee`).

A position closed before its last trading date, or rolled, meets no expiry.

## 6. Explicit rolls (`FutureChain`, `RollOnDate`, `RollOnVolume`, `Roller`)

* `RollOnDate(days_before=7)`: hold the nearest contract whose last trading date is more
  than `days_before` calendar days away (the rule of the catalogue's rolled series,
  data-api #81).
* `RollOnVolume(days_before=5)`: move to the next contract on the first bar its volume
  exceeds the held one's, never back; in any case once the held contract is within
  `days_before` days of its last trading date. The days are calendar days and the roll
  fills at the next open: the default leaves a session before a Monday last trading date
  across a weekend and a holiday (2 days would decide on the Monday itself and fill after
  it, which § 5 refuses).
* `Roller(strategy, chain, rule).target(n)` in `next()`: hold `n` contracts (signed) in the
  contract the rule picks on this bar. When the pick changes, **two market orders** are
  placed on this bar and filled at the next open: close the old contract, open the new.
  Each pays its fee and its slippage; the cost of the roll is what those fills cost, never
  an adjustment of a price series. `Roller.rolls` (and `broker.futures.rolls`) list them.
  No new order while earlier ones are open.

## 7. Continuous series for the signals (`continuous`, `ContinuousData`)

`continuous(table, rule, adjust)` gives, per date, the bars of the contract held, its
`symbol`, `rolled` and the cumulative `adjustment`. The contract of day *d* is the one the
rule picks on day *d − 1* -- the roll is decided at the previous close, as the `Roller`
trades it, and no level borrows a price from a later choice. The gap of a roll is measured
on the decision day, on both contracts' closes. `adjust`:

* `"difference"`: earlier levels move by `new − old`; point changes are the traded ones;
  works through zero and negative prices;
* `"ratio"`: earlier levels are multiplied by `new / old`; percentage changes are kept;
  a roll at a non-positive close raises `ValueError` (WTI, 20 April 2020: -37.63) naming
  `"difference"`;
* `"none"`: the closes spliced, gaps kept.

The last segment is never adjusted. `add_futures(..., continuous_adjust=...)` adds it as a
`ContinuousData` feed `"<root>_cont"`, whose security refuses every order (`NotTradable`:
status Margin): a back-adjusted level is not a price anyone trades at.

## 8. Loading (`future_table`, `add_futures`)

`future_table` reads one row per contract and session -- `date`, `symbol`, `root`, OHLC,
`settlement` (optional), `volume`, `last_trade_date`, `first_trade_date` (optional), the
layout of data-api #81's daily table -- and drops rows after the last trading date. A date
with a time of day, or a second row for the same contract and session, is refused
(intraday, § 10.3). `add_futures(engine, table, {root: FutureSpec(multiplier, tick_size,
initial_margin, maintenance_margin, commission, currency)}, at_expiry="raise")` adds one
`FutureData` per contract, its commission scheme, the expiries under the `at_expiry`
policy (§ 5), and returns the `FutureChain` of each root.

## 9. Tests (synthetic, `tests/test_futures.py`, 16)

P&L with a multiplier of 1 000 and the daily variation adding up to the realised P&L;
the settlement column as the mark; the initial margin refusing a third contract; the margin
call after a settlement under both policies; notional sizing; tick rounding of a slipped
fill; a position held to its last trading date stopping the run by default
(`FutureExpiryError`, its message), one closed before running through, an unknown
`at_expiry` refused; the forced close under `at_expiry="close"` (fee, realised P&L, closed
trade, the late order expired); a date roll paid as two orders with fees and slippage, reconciled to
the cent; a volume roll decided on the right bar; ratio and difference series against the
contracts' own returns and point changes; the continuous feed refusing orders; WTI April
2020 -- a position held through -37.63 marked and closed by force without a NaN, the
difference series finite, the ratio series refused; intraday tables and feeds refused; the
`margin` reader.

## 10. Decisions (Édouard, 2026-11-27)

The four questions of the first draft, answered "recommendations 1 to 4: ok".

1. **No silent forced close.** A backtest whose strategy holds a contract to its last
   trading date is refused: `FutureExpiryError` at the expiry (§ 5), the earliest point at
   which it is known. The forced close stays, as the explicit option
   `add_futures(..., at_expiry="close")`. `RollOnVolume`'s default moved from 2 to 5
   calendar days so that its rolls fill before a Monday expiry (§ 6).
2. **Margins fixed per root, for now.** `FutureSpec` gives one initial and one maintenance
   margin per root, for the life of every contract. Extension point: every margin read
   (the batch check, the fill check, the book's requirement, the margin call, the
   profile's `maintenance_margin`) goes through `FutureContract.margin(kind, day)`; a dated
   schedule per root -- exchanges move margins: crude's initial margin roughly doubled in
   March 2020 -- replaces its body with a lookup at `day` once the reference tables carry
   one, and the callers pass the slice's day. Not built until such a table exists.
3. **Intraday: settlement at the session close; refused for now.** The rule, when intraday
   futures arrive: a contract settles once a day, at the close of its session on the
   exchange's calendar (`tradewind.calendars`, CME), not at every bar; margin calls follow
   that settlement. Implementing it needs the session close of each futures exchange and a
   settlement price distinct from the last minute bar, which no table carries yet. Until
   then intraday futures are refused: `future_table` rejects a date with a time of day or
   two rows for one contract and session, and `FutureData` rejects intraday bars or a
   timeframe under a day (`ValueError`).
4. **Margin from cash only.** The initial and maintenance margins are posted from cash;
   stock or option positions in the same account are not collateral (no cross-margining,
   no haircut table). Enough for the catalogue's futures strategies, which hold futures
   alone; documented on `FuturesMargin` and `add_futures`.
