# 02. Semantics: kept and corrected

**Status: validated by Édouard on 2026-11-01** (PR #1 merged). Later additions to the
"observed" table are proposed through their own PR. Each line is a
behaviour the corpus relies on, described from backtrader's public documentation and from
what the oracle shows (NAV paths, order logs), never from its source. "Kept" means
Tradewind reproduces it bit for bit in compatibility mode; "corrected" means the default
changes, the compatibility switch restores it, and the oracle is regenerated with a
documented delta. The "kept" lines are implemented and verified (9/9 synthetic oracle
strategies, 3/3 corpus strategies identical); the "corrected" lines are proposals and
nothing in the engine depends on them yet.

## Observed on the oracle and reproduced (2026-11-01)

Four behaviours the public documentation does not state, learned from order logs:

| behaviour | kept / corrected | how it was seen |
|---|---|---|
| Orders are `Submitted` at placement and `Accepted` **as a batch when the next bar arrives**; a bar's notifications arrive as all Submitted, then all Accepted, then the fills; an order placed on the last bar is never notified | kept | status sequences of the oracle strategies |
| The submitted orders of a bar are checked **in submission order against a running cash**: a sell earlier in the batch funds a buy later in it; each is Accepted or `Margin` before any fill | kept | `a_volatility_driven_asset_allocation_vdaa`: a 168-share buy accepted by the oracle, refused by the first Tradewind build |
| That running cash takes the cash flow of **every order of the batch, refused ones included**, and an order is Accepted iff the running cash is non-negative after it. The deficit of a refused buy therefore weighs on the orders after it, and a sell -- even one reducing a long -- is `Margin` while the running cash stays below zero after its proceeds | kept, flagged | `a_new_anomaly…` 2020-05-28: 48 orders, 20 refusals, reproduced only by this rule among the variants searched; probe `deficit_carry`. **A refused order that still spends is a defect**: the corrected default (chantier 4) drops the carry and re-checks each order against the cash the accepted ones leave |
| An order the cash cannot cover goes `Submitted → Margin`, never `Accepted` | kept | `add_cash_and_margin` |
| `order_target_value` truncates the **value gap** to a size, not the target size: `getsize(price, target - position_value)` | kept | one share of difference on a third of the rebalances of `monthly_equal_weight` |
| Inside `notify_order` the strategy reads the **fill bar's** date and the portfolio value marked at that bar's close | kept | values and dates recorded in the oracle statuses |
| At fill time the **closing part of a reversing order always fills**; its opening part fills only if the cash left after closing covers it, otherwise the order ends `Partial` (closing part executed) then `Margin`, the opening part untouched | kept | `breaking_bad_trends` 1993-04-02: short 1 998, buy 3 947 at a gapped-up open; probe `reverse_shortfall` |
| A line indexes its **preloaded buffer as a plain sequence**: a positive `ago` reads a bar that has not arrived, an `ago` before the first bar wraps to the end of the buffer, only a position outside the buffer raises; `line.array` holds the whole series during the run | kept, flagged | `long_term_investing_in_triple_leveraged_etfs` reads `datetime.date(1)` to spot a month's last session; `regime_detection…` reads `close.array` and dates back to the first bar; probe `line_edges`. **Look-ahead by construction**: the corrected default (chantier 4) raises on a positive `ago` and exposes only the bars delivered so far |
| `next` runs only once **every feed has delivered a bar**; `prenext` until then (a feed listing 30 bars late holds the strategy in `prenext` for 30 bars) | kept | oracle harness feed `S3`, all probes |
| With `set_coc(True)` only the **fill price** of a market order changes, to the close of the bar it was placed on; batch acceptance, the pre-check at that same close and the notification at the next bar are unchanged | kept | probe `cheat_on_close`; corpus `profitable_mean_reversion_after_large_price_drops…` |
| `add_cash` is **queued**: invisible to `getcash` in the bar that asks, invisible to the next bar's batch check and fills, landed at the end of that pass | kept | probe `add_cash_timing`; corpus `consumer_protection…pension_product`; thirty one-step scenarios on the oracle |
| `get_value(datas=…)` returns the value of **those positions alone** (no cash) and **applies any pending `add_cash` on the spot**; the plain `get_value()` is the value **marked once per bar** after fills and additions, unmoved by a same-bar `add_cash` or by that side effect | kept, flagged | same probe: an `order_target_value` after `add_cash` gets its whole batch checked against the new cash, a plain `sell` does not. **A read with a side effect is a defect**: the corrected default (chantier 4) applies `add_cash` at a single, documented point and makes `get_value` pure |
| The batch pre-check prices a market order at the **close of its submission bar**, a limit or stop order at its own price; the fill is then re-checked at the fill price (an order Accepted at the pre-check can still end `Margin`) | kept | `a_new_anomaly…`: order logs, 2020-04-08 |
| **runonce (default)**: a `next`-only indicator is computed in one pre-pass before the run, during which every feed that is **not one of its inputs sits at its last bar**; an indicator reading such a feed through a parameter sees one frozen value for the whole series. `runonce=False` steps it live. | kept, flagged | probe `foreign_feed`; corpus `a_study_in_portfolio_diversification_using_vix_options` reads VIX = 15.86 on all 9 601 bars. **The reference NAVs embed this defect**; the corrected default (chantier 4) is to step `next`-only indicators live and regenerate those NAVs. |

Observed on 2026-11-03 for the `bt.*` names the codemod found missing (probes `line_ops`,
`minperiod_add`, `minperiod_chain`, `analyzers`; scratch probes with no trade, an open trade
only, a reversal, a commission, weekly bars, eleven `SharpeRatio` parameter sets):

| behaviour | kept / corrected | how it was seen |
|---|---|---|
| **Two stages**: in a strategy's `__init__` an operator on lines (`h - data.low`, `h > 0.0`) and `bt.If(cond, a, b)` build a *line*; in `next()` the same operator returns a float or a bool. A built line has no truth value (`bool()` raises `TypeError`) | kept | probe `line_ops`; corpus `fixing_the_vix_an_indicator_to_beat_fear` |
| A built line takes the **largest minimum period of its operands**, counts towards the strategy's (`If` over `Highest(20)`: `next` from bar 20) and holds **NaN before it** -- `If(nan > 0, x, 0.0)` is NaN there, not 0.0. A comparison is 1.0 / 0.0; `If` takes a non-zero (or NaN) condition as true; a division by zero raises `ZeroDivisionError` when the line is computed | kept | probe `line_ops` |
| Minimum periods **chain**: an indicator starts from the largest of its inputs' (SMA(20) of SMA(15): bar 34); `addminperiod(n)` **adds** `n - 1` (20 then 14: bar 33), `updateminperiod(n)` raises to `n` | kept | probes `minperiod_add`, `minperiod_chain` |
| `bt.LineRoot` / `bt.LineIterator`: a feed, a line, a built line, an indicator and a strategy are `LineRoot`; only an indicator and a strategy are `LineIterator`; an analyzer and a feed's `lines` container are neither. `bt.get` does not exist (the corpus' `bt.get(sym)` is a local dict) | kept | probe `line_ops` |
| `TradeAnalyzer`: `{total: {total: 0}}` without trades, `{total: {total, open}}` with open trades only, the full tree from the first closed trade; a **reversing fill closes one trade and opens another**; won = net P&L > 0; `lost.pnl.max` is the largest loss; empty long/short length buckets start `min` at `sys.maxsize`, `len.won` / `len.lost` have no `min` until a trade lands there. A missing key raises `KeyError`, as an attribute too | kept | probe `analyzers` |
| `DrawDown`: percent of the running peak, money, bars since the peak, and their maxima, over every bar including `prenext` | kept | probe `analyzers` |
| `SharpeRatio`: per-period returns (value at the period's last bar over the previous period's, the first over the starting cash); `convertrate` turns `riskfreerate` into `(1+rf)^(1/factor)-1` and `annualize` multiplies by `sqrt(factor)`, else returns are annualised and `annualize` is moot; factor 252/52/12/1; population deviation unless `stddev_sample`; `None` on a zero deviation. Its `get_analysis()` is a **plain `OrderedDict`** (`["sharperatio"]`, no attribute access) | kept | probe `analyzers` |
| `broker.setcash()` moves the portfolio value at once, before the run too (an analyzer's `start` reads it) | kept | scratch probe; the first build returned the default 10 000 until the first bar |

Observed on 2026-11-06 while the event bus of `05-interfaces.md` was built (probes
`notify_sequence`, `cancel_timing`; the order and trade notifications of both engines
compared, nothing else printed):

| behaviour | kept / corrected | how it was seen |
|---|---|---|
| Within one bar the strategy receives **every order notification before any trade notification** (two fills: Completed, Completed, then trade, trade), not one fill's order then its trade | kept | probe `notify_sequence`; v0 interleaved them, the bus's (kind, sequence) drain matches the oracle |
| An order placed **inside `notify_order`** is first notified at the **next** bar (Submitted, Accepted, Completed together), not during the delivery that placed it | kept | probe `notify_sequence`; v0 delivered its Submitted at once |
| `cancel()` reaches only an **Accepted** order not yet filled. An order still waiting for its batch acceptance -- cancelled in the `next` that placed it, or placed and cancelled inside `notify_order` -- is accepted at the next bar and fills as if nothing had been asked; a cancelled Accepted order is notified `Canceled` at the next bar | kept, flagged | probe `cancel_timing` (four moments, a market order cancelled at once still executes). **A cancel that silently does nothing is a defect**: the corrected default (chantier 4) cancels a Submitted order at once |

Seen and **not** reproduced yet: in `runonce` mode a `next`-only indicator whose input is
another indicator reads `self.data[0]` as NaN on every bar (it reads the value with
`runonce=False`). No corpus strategy is known to depend on it; to be probed before the
4 200-NAV pass.

## Clock and bars

| behaviour | kept / corrected | note |
|---|---|---|
| One `next()` per bar of the clock feed (`datas[0]`); other feeds are aligned on it and a feed with no bar at that date repeats its last bar | kept | the corpus relies on `len(data)` growing only when a bar exists; Lean calls this fill-forward |
| `prenext()` while any feed has fewer bars than its minimum period, `nextstart()` once, then `next()` | kept | 0 files override them; kept for compatibility |
| `datetime.date(0)` is the bar's date; `[−k]` looks back k bars **of that feed** | kept | |
| Market calendar, time zones, early closes | corrected (new) | daily feeds carry dates only today; the calendar becomes explicit (Lean `MarketHoursDatabase`) and is a no-op on date-only data |

## Orders and execution

| behaviour | kept / corrected | note |
|---|---|---|
| A market order placed in `next()` fills at the **next bar's open** | kept | the harness tax measured in pwb-toolbox; `set_coc(True)` (29 files) fills at the current close instead |
| `order_target_percent(target)` sizes on `broker.getvalue()` at the current close, integer shares, rounding toward zero | kept | the oracle's NAVs depend on this rounding |
| `order_target_size` / `order_target_value` as differences to the current position | kept | |
| `close(data)` = market order for the opposite of the position | kept | |
| Limit and stop orders checked against the next bars' high/low, filled at the limit price (limit) or at the open if gapped through (stop) | kept | 7 files; to be confirmed on the oracle |
| `Order.Close` execution type fills at the bar's close | kept | 9 files |
| Partial fills, volume participation | corrected (new) | backtrader fills any size in one bar; Lean's `IFillModel` with a volume cap becomes the default, the compatibility fill model restores all-or-nothing |
| Cash check: an order the cash cannot cover is `Margin` and never fills | kept | 74 files test `order.Margin` |
| Shorting always allowed, no borrow cost | corrected (new) | an `IShortableProvider` with a default that allows everything (compat) and a cost model to plug |
| Commission: percentage or fixed per share via `setcommission`; pwb-toolbox's `SpreadImpactCommission` as a `CommInfoBase` | kept | the interface pwb-toolbox implements is part of the contract |
| Settlement: cash is available the instant a fill happens | corrected (new) | T+n settlement model, default T+0 in compat |

## Notifications

| behaviour | kept / corrected | note |
|---|---|---|
| `notify_order` on each status change: Submitted, Accepted, Partial, Completed, Canceled, Margin, Rejected, Expired | kept | names and order of states |
| `notify_trade` with `trade.isclosed`, `pnl`, `pnlcomm` | kept | |
| `notify_timer` with `add_timer(when, monthdays, monthcarry)` | kept | used by the eleven toolbox strategies |
| `notify_cashvalue`, `notify_fund` | kept, low priority | 0 files |

## Portfolio accounting

| behaviour | kept / corrected | note |
|---|---|---|
| `getvalue()` = cash + Σ position × close of the bar being processed | kept | 70 % of files log it in `next()`; it is the NAV the oracle compares |
| Single currency (USD) | corrected (new) | Lean `CashBook`; default single-currency, identical results |
| Corporate actions | corrected (new) | prices are adjusted upstream today; dividends as cash and splits as quantity changes become possible when the feed carries them |
| Delisting | corrected (new) | a feed that ends liquidates at its last close (today the position is carried at a stale price) |

## Numbers

Float64 everywhere; the oracle tolerance is 1e-9 relative on the NAV path for "kept"
behaviours. Measured on 2026-11-01: 9/9 synthetic strategies and 3/3 corpus strategies
(9 545 daily bars each) identical, worst relative error 4e-16.

Indicator arithmetic is reproduced **bit for bit**, not within a tolerance, because a
strategy compares an indicator to a price with `>` and 1e-14 is enough to flip it on a
flat series -- and every late-listed symbol has one, the flat prefix `run_strategy`
back-fills. Observed on 2026-11-03 (periods 3, 20, 200; 14 400 SMA, 9 602 EMA, 4 801
ATR and 4 801 RSI values, zero mismatches afterwards):

| behaviour | kept / corrected | note |
|---|---|---|
| A window average is the window summed by Python's compensated `sum` (Neumaier, Python ≥ 3.12), divided by the period; exact on a constant series | kept | `_window_sum`, vectorised across windows; a cumulative-sum difference is *not* equivalent |
| An exponential or Wilder smoothing step is `prev * (1 - a) + x * a` | kept | `prev + a * (x - prev)` rounds differently on half the bars |
| `RSI = 100 - 100 / (1 + au / ad)` | kept | `100 * au / (au + ad)` rounds differently |

## Decisions requested

1. The "kept" table above and in the sections: agreed as the compatibility contract?
2. Each "corrected" line: agreed as a *default* change (with a compat switch), to be
   delivered in chantier 4 with a regenerated oracle, or to be kept as is?
3. Partial fills and volume participation: agree to make them the default, knowing the
   published NAVs move for thin names?
4. Delisting liquidation at the last close: agree, knowing it changes the NAVs of the
   survivor-biased universes the most?
5. Anything in Tier 3 of `01-scope.md` that must become a commitment?
