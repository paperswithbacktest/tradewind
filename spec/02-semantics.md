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
| **runonce (default)**: a `next`-only indicator is computed in one pre-pass before the run, during which every feed that is **not one of its inputs sits at its last bar**; an indicator reading such a feed through a parameter sees one frozen value for the whole series. `runonce=False` steps it live. | kept, flagged | probe `foreign_feed`; corpus `a_study_in_portfolio_diversification_using_vix_options` read VIX = 15.86 on all 9 601 bars (until it set `_nextforce`, parity 0.1.54 below). **The reference NAVs embed this defect**; the corrected default (chantier 4) is to step `next`-only indicators live and regenerate those NAVs. An indicator or observer with `_nextforce` turns the pre-pass off for the whole run (parity 0.1.54) |

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
| `broker.setcash()` moves the portfolio value at once, before the run too (an analyzer's `start` reads it); during the run the value becomes the cash alone until the next mark (see 0.1.45 below) | kept | scratch probe; the first build returned the default 10 000 until the first bar |

Observed on 2026-11-06 while the event bus of `05-interfaces.md` was built (probes
`notify_sequence`, `cancel_timing`; the order and trade notifications of both engines
compared, nothing else printed):

| behaviour | kept / corrected | how it was seen |
|---|---|---|
| Within one bar the strategy receives **every order notification before any trade notification** (two fills: Completed, Completed, then trade, trade), not one fill's order then its trade | kept | probe `notify_sequence`; v0 interleaved them, the bus's (kind, sequence) drain matches the oracle |
| An order placed **inside `notify_order`** is first notified at the **next** bar (Submitted, Accepted, Completed together), not during the delivery that placed it | kept | probe `notify_sequence`; v0 delivered its Submitted at once |
| `cancel()` reaches only an **Accepted** order not yet filled. An order still waiting for its batch acceptance -- cancelled in the `next` that placed it, or placed and cancelled inside `notify_order` -- is accepted at the next bar and fills as if nothing had been asked; a cancelled Accepted order is notified `Canceled` at the next bar | kept, flagged | probe `cancel_timing` (four moments, a market order cancelled at once still executes). **A cancel that silently does nothing is a defect**: the corrected default (chantier 4) cancels a Submitted order at once |

Observed on 2026-11-06/08 during the parity pass on pwb-toolbox 0.1.45 (804 corpus
strategies: 11 errors and 12 divergences of `feat/interfaces` explained). Probes in
`tests/oracle/strategies.py`; order logs of both engines compared; oracle exceptions read
as type and message only:

| behaviour | kept / corrected | how it was seen |
|---|---|---|
| A strategy's own attributes may use any `_name` (`_ready`, `_resolve(sym, default)`, `_step`, `_indicators`, `_listed`, `_computed`...): the engine's internals of `Strategy`, `Indicator` and `Analyzer` are prefixed `_tw_` | kept | probe `private_names`; corpus `regimes_of_index…`, `volatility_spreads…` (`'bool' object is not callable`), `credit_informed…` (its `_resolve` replaced the engine's) |
| `strategy.datetime` is the **clock's own line**, the time of every slice run so far, not data0's: it moves while data0 has not listed, it is not preloaded (`date(1)` raises `IndexError`), an `ago` before its first slice wraps like a Python list; `len(strategy)` counts the slices | kept | probe `strategy_clock`; six corpus strategies read `self.datetime.date(0)` |
| `next` waits for every feed to list **and for every indicator's warm-up on its own input**: an SMA(5) on a late feed adds 4 bars, one on an early feed nothing | kept | probe `strategy_clock` |
| An indicator may only **bind** its declared lines in `__init__` (`self.lines.value = ROC(...)`, `self.l.b = SMA(...)`): the line carries the bound values bit for bit, NaN before their min period, and the indicator takes the bound min period; it needs neither `once` nor `next` | kept | probe `line_binding`; corpus `ROC5Signal`, `DualMomentumSignal` |
| `RateOfChange = (x - x[-p]) / x[-p]`; `StandardDeviation = sqrt(mean(x²) - mean(x)²)`, both means compensated window sums | kept | probe `indicator_bits` (0 of 342 and 0 of 111 values apart); the ratio form `x / x[-p] - 1` differs in the last bit on 95 % of values and reordered two equal-return ETFs in `abnormal_returns…`'s decile sort |
| `order_target_value` reads the position's value through `get_value(datas=[d])` **even when flat**, which lands a pending `add_cash`: a buy placed after a same-bar withdrawal is checked at its fill against the reduced cash | kept, flagged | probe `add_cash_flat`; `investing_in_the_s_p_index…` 2023-02-02 (monthly fee then re-entry: `Margin`). Same defect as the `get_value` side effect above |
| With `set_checksubmit(False)` an order is **accepted at once**: no `Submitted` notification, `Accepted` delivered with the next bar's, cancellable in the bar that placed it; the cash is checked only at the fill | kept | probe `checksubmit_off`; `robust_asset_allocation…` (the toolbox's retry decision counts notifications) |
| A stock-like scheme's `leverage` (set even in place, `comminfo.p.leverage = 2`) applies to **longs only**: buying pays `size × price / leverage`, the pre-check and the fill check count that, selling out of a long returns `(size × entry / leverage + P&L)`, the mark counts `size × entry / leverage` then the P&L; shorts move the full price; `getsize` multiplies by the leverage; `get_value(datas=[d])` still answers `size × close` | kept | probe `stock_leverage`; `structural_convexity…` (accepts a 2× book the unlevered check refused); 19 corpus files set a leverage |
| `setcash()` while holding makes `getvalue()` **the cash alone** until the next bar's mark | kept, flagged | probe `setcash_held`; `parameter_uncertainty…` credits a monthly rate with `setcash` and then sizes its book on that value. **A value without positions is a defect** |
| A fill is one execution **per part** -- the closing part, then the opening part --, each settled then charged its own fee; closing returns the **entry value plus the P&L**, `(size × entry + P&L)`, not the traded value; `order.executed.value` counts the closed part at the entry price, `executed.comm` is the sum of the parts' fees | kept | probes `reversal_fees`, `executed_values` (cash bit for bit on 406 reversal bars and 74 others; one fee on the whole size differs on most reversals) |
| `notify_trade` hears of a trade when it **opens and when it closes**, not when a fill resizes it | kept | probe `executed_values` |
| The marked value is **`cash + t`**, `t` the positions' values summed from zero in the order the positions were first touched: `size × close` for a long or a short (a leveraged long: margin then P&L). Queued `add_cash` amounts land **one after the other**, not summed first; a short's `getvalue` (what `order_target_value` reads) is `size × close` too | kept, residual | scratch probes `value_terms` (893 of 904 marks bit for bit, against 164 for `cash + v1 + v2...`); `high_dimensional_minimum_variance…` 2025-05-26 (a target of ~0 on a 1 204-share short: 1 204 shares on the oracle, 1 203 from entry + P&L); `profitable_mean_reversion…` 2010-03-12 (nine toolbox additions). **Residual**: a few marks in a hundred still differ in their last bit, cash identical; the first-touch order of the oracle's positions is the suspect (switching the feeds' order for it moved the count both ways) |
| A strategy that draws from `np.random` without a seed is not reproducible on either engine | n/a | `large_scale_portfolio_allocation…`: two oracle runs at the same hash seed end at 326 551 and 386 278; with `np.random.seed(0)` both engines end at 416 048.7958239279 |

Observed on 2026-11-11 during the parity pass on pwb-toolbox 0.1.47 (grand remeasure of
2026-11-10: two errors and two divergences of Tradewind 0.0.2). Probes in
`tests/oracle/strategies.py`; order logs of both engines compared through
`tools/run_corpus_strategy.py --fills`; oracle exceptions read as type and message only:

| behaviour | kept / corrected | how it was seen |
|---|---|---|
| `addminperiod(n)` / `updateminperiod(n)` on a **strategy** are accepted and change nothing: before or after its indicators, with `n` past the end of the data, the first `next` stays where the feeds and the indicators put it | kept | probe `strategy_minperiod`; corpus `finding_consistent_trends…` (`AttributeError` on 0.0.2) |
| An indicator built inside another indicator's `__init__` is its **child**: complete before its parent computes (a `once` reading `self.child.lines.x.array` sees every value), walked bar by bar with a `next`-only parent (its `next` reads `self.child[-k]` at the bar being computed); the parent's min period is the **maximum** of its own (`addminperiod` included, in any order) and its children's -- a child SMA(20) with `addminperiod(3)` starts at bar 20, a child SMA(5) with `addminperiod(30)` at bar 30 -- while an input's adds; a child still holds the strategy in `prenext` until its own warm-up | kept | probe `child_indicators`; corpus `RSIBullRangeMomentum` |
| In `runonce` mode the strategy **does not move a child's cursor**: read from the strategy, a child of a `next`-only indicator sits on the last bar (`len` = the whole series), one of a `once` or binding indicator before the first (`len` 0); both read the series' **last value** at `[0]` | kept, flagged | probe `child_indicators`. **Look-ahead**: a strategy reading `self.ind.child[0]` sees the last bar of the run; the corrected default (chantier 4) keeps children on the clock. Live mode (`runonce=False`) keeps them on the clock, not probed |
| `RSI_Safe` is the RSI with `safediv` on: `safelow` (50) where both smoothed moves are zero, `safehigh` (100) where only the down move is | kept | probe `child_indicators` (flat and rising stretches, bit for bit). The plain RSI raises `ZeroDivisionError` on a flat window in the oracle; Tradewind 0.0.2 returns 100 there -- **seen, not reproduced** (see below) |
| An indicator that defines neither `once` nor `next` and binds no line is **inert**: no error, lines NaN, `len` the clock's; its own methods (`compute()`) are the strategy's to call | kept | probe `inert_indicator`; corpus `DecileMomentumSignal` (`NotImplementedError` on 0.0.2) |
| `TrueRange` (and the range `ATR` smooths) is `max(high, close[-1]) - min(low, close[-1])`: on a bar whose **high is below its low** it differs from `max(h - l, |h - c1|, |l - c1|)` (0 when the previous close lies between them); the two agree bit for bit when high >= low | kept | probe `true_range_inverted`; corpus `following_the_trend` 1994-08-12: GB1Y (an inverted series, high/low swapped) short of 6 397.94 on the oracle against 5 030.61, the ATR(100) differing |
| At the fill an **opening short sale is re-checked** like any opening order: its proceeds less its fee must leave the cash non-negative. It fails only when the cash is already below zero -- closing or reducing orders always fill, so a buy covering a short earlier in the same pass can take the cash below zero -- and the next short large enough to bring it back fills | kept | probes `short_fill_deficit` (cash checked at the fill only) and `short_fill_deficit_checked`; corpus `investing_just_another_hype…` 2024-01-02: covering A and MRNA took the cash to -1 522.60, then LCID's 5-share short (208.48 net) was `Margin` on the oracle and filled on 0.0.2 |

Seen and **not** reproduced yet: in `runonce` mode a `next`-only indicator whose input is
another indicator reads `self.data[0]` as NaN on every bar (it reads the value with
`runonce=False`). No corpus strategy is known to depend on it; to be probed before the
4 200-NAV pass. Also seen on 2026-11-11 and not reproduced: the plain `RSI` (`safediv`
off) raises `ZeroDivisionError` in the oracle when its window is flat (0/0); Tradewind
returns 100. A corpus strategy with a plain RSI on a back-filled flat prefix would end in
error on the oracle; none of the four strategies of the 0.1.47 pass does.


### Surface the catalogue reads without a guard (2026-11-15)

The codemod's dry run (PR #24) listed nine corpus strategies that read, outside any `try`
or `hasattr`, a name the oracle has and Tradewind lacked. Each name was then observed on the
oracle through black-box probes (`tests/oracle/strategies.py`: `private_get_value`,
`sumn_divbyzero`, `mean_pctchange`, `bracket_orders`, `bracket_batch`,
`bracket_batch_unchecked`, `position_by_name`), printing values (`repr`), dates, statuses
and type or exception names only; scratch probes for the default periods and for `Mean`
with several inputs printed values and lengths only. The public documentation gave the
names, the parameters and the order of the bracket list:

| behaviour | kept / corrected | how it was seen |
|---|---|---|
| `broker._get_value()` (private, called right after `add_cash` to settle it in the bar) **lands every queued addition at once** (`getcash` shows it, and so do the next bar's batch check and fills) and **marks the portfolio again**; the value is returned and is what the plain `getvalue()` reads for the rest of the bar; with nothing queued it changes nothing | kept | probe `private_get_value` (bit for bit); corpus `the_volatility_risk_premium…`, `time_varying_risk_aversion…`, `which_is_worse_heavy_tails…`, `yet_another_note_on_the_leland_s…` |
| `bt.ind.SumN(x, period)` is the window summed like the simple average's (Python's compensated `sum`), `period` 1 by default; a window of zeros is exactly 0.0 | kept | probe `sumn_divbyzero`; corpus `a_comparative_study_of_the_macd…`, `do_options_markets_anticipate…` (held) |
| `bt.DivByZero(x, y, zero)` is a line: `x / y`, or `zero` where `y` is 0 (a `nan` zero gives NaN), never raising | kept | same probe, a stretch of zero volumes |
| `bt.ind.Mean` (= `Average`, `ArithmeticMean`) averages **its first input only** over `period` bars, `period` **1** by default: `Mean(roc3, roc5, roc7, roc9)` is `roc3` bit for bit, the other inputs only raise the minimum period (`Mean(ROC(2), ROC(45))`: NaN to bar 45, then `ROC(2)`) | kept, flagged | probe `mean_pctchange` and a scratch probe; corpus `exploration_of_long_short_etf_momentum…` (published) means to average four momenta and gets the 3-month one. **The strategy's intent is not what runs**: a defect of the strategy, to be fixed in the corpus, not in the engine |
| `bt.ind.PctChange` (= `PercentChange`) is `x / x[-period] - 1.0`, `period` 30 by default -- the ratio form, which differs in the last bits from `RateOfChange`'s difference over the old value | kept | probe `mean_pctchange` |
| `buy_bracket` / `sell_bracket` return `[entry, stop, limit]`; the entry is a `Limit` by default; the children are on the other side, same size, `Stop` at `stopprice` and `Limit` at `limitprice`, with `parent` the entry; only the last order has `transmit` true. All three are `Submitted` at once and go through the batch check as ordinary orders, in that order (a child sell funds the orders after it, a child buy spends) | kept | probes `bracket_orders`, `bracket_batch`; corpus `double_bottom_country_trading…` (which reads the list as `[entry, tp, sl]`: only names, it cancels all three alike) |
| A child **cannot fill on the bar its entry fills**, even when that bar crosses its price; from the next bar it fills as any stop or limit | kept | probe `bracket_orders` (`default_exec`: the limit child's price inside the entry bar's range, filled two bars later) |
| When one order of a bracket fills as a child, or ends without filling (cancelled by the strategy, `Margin`, and by extension `Expired`), **every other live order of the bracket ends right after it**, in bracket order: `Canceled` once accepted, `Rejected` while still waiting for the batch check. An entry refused by the batch check rejects its children (which then neither spend nor fund the running cash); a refused child cancels the accepted entry and rejects its sibling; an entry refused at its fill (`checksubmit` off) cancels its children; cancelling a child before the entry fills cancels the entry and the other child | kept | probes `bracket_orders`, `bracket_batch`, `bracket_batch_unchecked` (notification order checked). Expiry of a bracket order not probed |
| `self.positionbyname` is an **attribute**: the very `Position` object of `self.position` (the first feed's); calling it, as `feasible_momentum_strategies…` does with a name, raises `TypeError`. `self.positionsbyname` maps every feed's name to its live position | kept, flagged | probe `position_by_name`. **The call is a crash on both engines** when the strategy reaches it (only when every cohort NAV is negative) |

Nine names the same report shows **guarded** (`set_cashinterest`, `setslippage_perc`,
`addcash`, `broker.Order`, `strategy.adddata`, `strategy.log`, `position_percent`,
`get_latest_weights`, `get_market_cap`) are absent from the oracle too (checked with
`hasattr` on live oracle objects): the guarded branch runs on both engines, nothing to add.

### Parity 0.1.51 (2026-11-16)

Grand remeasure on pwb-toolbox 0.1.51 (both environments aligned on 2026-11-15): 11
divergences and 2 errors of Tradewind `d1731cd` among 850 strategies. Order logs of both
engines through `tools/run_corpus_strategy.py --fills`, corpus strategy copies instrumented
in the strategy itself, per-bar marks dumped from both engines; probes in
`tests/oracle/strategies.py` (`slippage_perc`, `slippage_no_open`, `slippage_out`,
`slippage_no_limit`, `slippage_no_match`, `slippage_strict`, `slippage_fixed`,
`slippage_fixed_out`, `order_identity`, `indicator_owner`, `self_bound_lines`,
`levered_negative_close`); the public documentation of the slippage parameters
(`slip_open`, `slip_match`, `slip_limit`, `slip_out`):

| behaviour | kept / corrected | how it was seen |
|---|---|---|
| `set_slippage_perc(p)` / `set_slippage_fixed(f)` **set the four flags too**, with their own defaults (`slip_open` true): called with the amount alone, a market order filled at the open slips. The broker's default before any call keeps `slip_open` false | kept | probes `slippage_*`; corpus `the_profitability_of_technical_analysis…`, `price_overreactions_in_the_cryptocurrency_market` (Tradewind kept the flags of its default and never slipped the open) |
| A slipped price is **held inside the bar**: beyond the high (or under the low) it becomes the high (low) when `slip_match`, and otherwise the order is **not matched at this bar** and waits; `slip_out` lets it stand. Market orders, stops (at the open or at the stop price) alike | kept | probes `slippage_perc`, `slippage_no_match`, `slippage_out`, `slippage_fixed(_out)` |
| A limit order reached **through the bar** fills at its price, never slipped (even with `slip_out`); one the **open** satisfies fills at the open slipped like a market order, then brought back to its limit and to the bar's **high only** (a sell limit slipped under the low fills there) when `slip_match` or `slip_limit`, else it waits; with `slip_out` the slipped open stands even past the limit | kept, flagged | probes `slippage_perc`, `slippage_strict`, `slippage_no_limit`, `slippage_fixed`. **The missing low-side bound of a limit order** is an asymmetry of the oracle, reproduced because the reference NAVs embed it |
| The order a notification carries is a copy, yet **equal** to the one `buy`/`sell` returned: orders compare by reference, so `order in placed` and `placed.remove(order)` inside `notify_order` work. The oracle raises `AttributeError` comparing an order with a non-order and refuses to hash one; Tradewind answers unequal and hashes the reference | kept | probe `order_identity`; corpus `option_implied_currency_risk_premia` (its pending list never emptied on Tradewind: one rebalance, no other order for 36 years) |
| An indicator's **`_owner`** is the object whose `__init__` built it: the strategy, or the parent indicator for a child | kept | probe `indicator_owner`; corpus `when_to_own_stocks_and_when_to_own_gold` (`AttributeError` on Tradewind) |
| An indicator may bind a line to an **expression of its own bound lines** (`self.lines.mid = (self.lines.up + self.lines.lo) / 2`): the expression is computed once the lines it reads are bound | kept | probe `self_bound_lines`; corpus `analysis_of_trend_following_systems` (`RecursionError` on Tradewind) |
| A long under a **leveraged** stock-like scheme counts `size × close` in the marked value, not margin then P&L, when the **close is zero or below** | kept | probe `levered_negative_close`; corpus `the_best_defensive_strategies…` (3x scheme, crude oil at -37.63 on 2020-04-20: one mark 0.8 M apart) |

Seen and **not** reproduced: the last-bit residual of the marked value (spec/02, `cash + t`)
still differs on about one mark in twenty when a book holds shorts
(`is_downside_risk_priced_in_cryptocurrency_market`: 166 of 3 180 marks one or two units in
the last place; no order of the terms and none of the term forms tried fits them all). A
strategy can amplify it: `analysis_of_the_effect_of_covid…` compares `0.15 / 15` of a value
one ulp apart with a 1 % threshold and trades 12 more names on 2018-06-29. Two strategies of
the pass draw from `random` / `np.random` without a seed and are not reproducible on the
oracle itself (`a_hamiltonian_phase_space_approach…`, `allocation_to_industry_portfolios…`).

### Parity redo 0.1.51 (2026-11-18)

Parity redo of Tradewind `d77d526` against pwb-toolbox 0.1.51 (papers tree `51945b9`, seed
0): four strategies divergent. Order logs of both engines through
`tools/run_corpus_strategy.py --fills`, corpus strategy copies instrumented in the strategy
itself (cash, value, positions and closes printed as hex floats), probes `line_get` and
`feed_equality` in `tests/oracle/strategies.py`:

| behaviour | kept / corrected | how it was seen |
|---|---|---|
| `line.get(ago, size)` is a **plain slice of the preloaded buffer** ending at `ago`: a positive `ago` reads bars that have not arrived (the window is cut at the end of the buffer), and a window reaching before the first bar follows Python's slicing rules (empty, or wrapped to the end of the buffer when it ends before the first bar too) | kept, flagged | probe `line_get`; corpus `merton_s_portfolio_problem_revisited…` reads `close.get(ago=1, size=253)` -- the window ending at **tomorrow's** close -- and never traded on Tradewind (`get` returned `[]`). **Look-ahead by construction**, like the positive index of the same line |
| While the strategy runs, a **feed compared or combined with a number, a line or another feed stands for its current close**: two distinct feeds closing at the same price are equal, so `d in longs`, `list.index(d)` and `list.remove(d)` find the other one; against anything else (`None`, a string, an order, a strategy) a feed is equal only to itself; it still hashes by identity. `get_value(datas=[...])` still takes its positions by identity | kept, flagged | probes `feed_equality`, scratch probes on the oracle; corpus `chronological_return_ordering…`: `for d in self.datas: target = w_long if d in longs ...` bought `IRF` on 2003-11-03 because its close equalled a long's. Before the run (a strategy's `__init__`) the oracle builds a line from `d == other`, whose truth value raises; Tradewind compares by identity there |

Seen and **not** reproduced: the last-bit residual of the marked value (above) decides two
divergences of this pass, each amplified by the strategy. `optimal_portfolio_choice_with_wash_sale_constraints`
(long only) rebuilds a target from its current weight, `int(value * w / price)`, which flips
by one share when the value moves by an ulp (2008-10-08 rebalance, ITW one share);
`are_informed_traders_sensitive_to_regulatory_environments` tests `target_dollars == 0.0`
on a sum of cohort budgets that cancels exactly or not depending on the value's last bit
(F, 2004-02-11). On the first book, cash, sizes, entry prices and closes are identical on
both engines on 335 dumped bars and the value differs on 236; the oracle's value is matched
by `cash + t`, `t` summing **each long's entry value then its open P&L** in first-touched
order, on 307 of the 335 bars (99 for the current `size * close` terms) -- a lead, not
adopted: it changes the mark of every long book and needs the full corpus parity.

### Parity 0.1.54 (2026-11-22)

Campaign of the whole catalogue on pwb-toolbox 0.1.54, both engines in one environment, one
frozen papers tree, every dataset pinned to one revision, seed 0 (spec/12). Divergences
isolated with order logs of both engines (every notification, cash and value as hex floats)
and scratch probes on the oracle, then kept as probes in `tests/oracle/strategies.py`:

| behaviour | kept / corrected | how it was seen |
|---|---|---|
| `order.created.price` is the order's price, else the **close of its feed when the order is made** (market, close, target orders); `created.pricelimit` is `plimit`, else that same price | kept | probe `created_price`. pwb-toolbox's fill-gap audit divides the open by `created.price`: at 0.0 it gave no gap, the harness did not re-run the strategy at a reduced exposure as it did on the oracle (`absolute_momentum_a_simple_rule…`, `reconsidering_returns`, `how_to_use_gold_as_a_safe_haven` and three more: the first run identical, the published second run sized one share apart) |
| An indicator built with **no input** (`Konst(k=0.5)`) reads its owner's first feed -- the indicator building it, else the strategy -- and follows that feed's clock even when its `next` never reads it | kept | probe `indicator_no_input`; corpus `trend_following_algorithms_on_india_s_nifty_index` (`IndexError` on Tradewind: a constant cash-rate indicator with no data had no bars) |
| An indicator whose class sets **`_nextforce = True`** (or whose own `__init__` sets it on `self`), built anywhere in the run -- by the strategy, as another indicator's child, `once`-defining or line-binding alike -- turns the runonce pre-pass **off for the whole run**: every `next`-only indicator, forced or not, is stepped live and reads a foreign feed at the bar's date, as with `runonce=False`. An observer class with it does the same; on a strategy (class or instance) or an analyzer, or set on an indicator after it is built, it changes nothing | kept | probe `nextforce`; scratch probe of thirteen placements (each frozen or live on both engines). Corpus `a_study_in_portfolio_diversification_using_vix_options` (papers `fe3a5a8` sets it on its VIX-gated signal: Tradewind kept the pre-pass and read the last VIX on every bar, first NAV gap 1990-10-17, Sharpe 0.5051 against 0.5044), `when_to_own_stocks_and_when_to_own_gold`, `market_sentiment_and_an_overnight_anomaly` (the three catalogue strategies that set it) |

**The last-bit residual of the marked value: the written cause (spec/12, criterion 1).**
What is known. The oracle's `getvalue()` differs from `cash + size × close` (Tradewind's mark,
the `cash + t` row above) by one unit in the last place on a few marks in a hundred; cash, sizes,
entry prices and closes are identical on both engines on every dumped bar, so the gap is in the
**mark alone**, never in an order's size or a fill, unless a strategy turns the mark into a
decision. On the 0.1.54 campaign it accounts for every `within_tol` verdict (max relative gap
4.2e-16 on the first 440 strategies, none above 1e-15). Reproduction: a **single long** is enough
(scratch probe, eight seeds: 400 random-walk closes, one 291-share buy at 1 bp commission, held;
163 of 3 184 marks one ulp away -- 109 above, 54 below), so neither shorts nor the first-touch order
of several positions (the suspects of 0.1.45 and 0.1.51) are needed. Excluded, by exhaustive search
on those marks: any expression of at most four terms over cash, size, entry price, close, the
previous mark and the previous close (`+ - ×`, every parenthesisation), the previous mark plus the
day's P&L, entry value plus open P&L in either association (2 578 of 3 184 at best, against 3 021
for `cash + size × close`), five-term expressions adding the starting cash and the commission,
`math.fsum`, a constant hidden cash and exact (rational) rounding of `cash + size × close`. Not
known: the order or the intermediate quantity the oracle rounds through; Tradewind keeps
`cash + t`, which matches 95 % of the marks bit for bit. **Where it can change a result**: a
strategy that compares the value or the cash with a threshold, or truncates a size computed from it
(`int(value × w / price)`), and lands within an ulp of the edge (`optimal_portfolio_choice_with_wash_sale_constraints`,
`are_informed_traders_sensitive_to_regulatory_environments`, `analysis_of_the_effect_of_covid…`
above). A scan of the frozen catalogue (`fe3a5a8`, 4 954 strategies; report `parity_0154`) finds 6
strategies comparing `getvalue()`/`getcash()` directly, 908 more comparing a name derived from them
with a non-zero threshold and 1 751 with zero only (solvency guards); no divergence among the first
440 measured strategies is due to it. Such a divergence is listed by slug as "strategy decision on
a value one ulp apart", not fixed in the engine.

## Clock and bars

| behaviour | kept / corrected | note |
|---|---|---|
| One `next()` per date of the **union** of the feeds' dates (not of `datas[0]`'s alone); a feed with no bar at that date repeats its last bar and its `len` does not grow | kept | the corpus relies on `len(data)` growing only when a bar exists; Lean calls this fill-forward. Probe `extra_session` (2026-11-15): a weekday that **only one feed** has a bar on (the shape of a stray holiday row in one name of a panel) is a slice on the oracle too, for every feed, `datas[0]` included; the other feeds keep their bar and their length; a market order placed the bar before on a feed without that bar fills at its next own bar. Identical on both engines |
| `prenext()` while any feed has fewer bars than its minimum period, `nextstart()` once, then `next()` | kept | 0 files override them; kept for compatibility |
| `datetime.date(0)` is the bar's date; `[−k]` looks back k bars **of that feed** | kept | |
| Market calendar, time zones, early closes | corrected (new), opt-in | daily feeds carry dates only today; the calendar becomes explicit (Lean `MarketHoursDatabase`) and is a no-op on date-only data. Delivered 2026-11-15 as `calendars.py` (NYSE, NASDAQ, CME, CRYPTO) and `clock.ExchangeClock` (minute slices, timers in exchange time), off by default: `UnionClock` + `AlwaysOpen` stay the oracle's, NAVs unchanged (spec/05 § 12) |

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
| `notify_trade` with `trade.isclosed`, `pnl`, `pnlcomm`, at a trade's open and close only | kept | |
| `notify_timer` with `add_timer(when, monthdays, monthcarry)` | kept | used by the eleven toolbox strategies |
| `notify_cashvalue`, `notify_fund` | kept, low priority | 0 files |

## Portfolio accounting

| behaviour | kept / corrected | note |
|---|---|---|
| `getvalue()` = cash + Σ position × close of the bar being processed (the positions summed apart, then added to the cash; see 0.1.45 above) | kept | 70 % of files log it in `next()`; it is the NAV the oracle compares |
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
