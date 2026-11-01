# 02. Semantics: kept and corrected (draft for validation)

Each line is a behaviour the corpus relies on, described from backtrader's public
documentation and from what the oracle NAVs show, never from its source. "Kept" means
Tradewind reproduces it bit for bit in compatibility mode; "corrected" means the default
changes, the compatibility switch restores it, and the oracle is regenerated with a
documented delta. **To be validated by Édouard before chantier 2 codes anything.**

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
behaviours (measured: identical to the cent on 18 of 20 strategies between PandasData and
the array feed, two non-deterministic because they call `random` without a seed).
