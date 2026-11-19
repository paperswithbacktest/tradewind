# Live trading (paper): the daily run of a meta-portfolio

!!! warning "Paper accounts only"
    `tradewind.live.daily` trades a simulated account or an Interactive Brokers **paper**
    account. It refuses any port but the paper ports (TWS 7497, IB Gateway 4002) and any
    account whose id does not start with `DU`. Nothing is sent without `--send`.

[Live trading (IB)](live-trading.md) runs **one** strategy bar by bar. This page runs a
**meta-portfolio**, several strategies at given weights in one account, once a day after
the US close:

1. **Signals.** Each strategy runs as a backtest up to the last close. At that close it
   holds its positions and has just placed the orders a backtest fills at the next open.
   The two together are its *target book*, read by an analyzer, so the strategy is not
   modified. A catalogue strategy (`strategy.py`) runs through pwb-toolbox on Tradewind,
   one subprocess per strategy.
2. **Aggregation.** The meta-portfolio holds `k` *units* of each strategy, `k` times its
   shares. `k = weight × leverage × capital / strategy NAV`, reset on the rebalance schedule.
   The books are summed symbol by symbol. If the gross value of the sum exceeds
   `max_gross × capital` (×4, IB's limit), every unit is scaled down by the same factor.
   The result is rounded to lots, and orders below `min_order_notional` are dropped,
   except an order that closes a line.
3. **Orders.** The book is compared with the account's positions. The orders are written
   as a proposal (`orders/DATE.md` and `.csv`). With `--send` they are placed as
   market-on-open orders, which fill in the opening auction, the price the backtest
   fills at.
4. **Tracking.** Each day, the account's NAV is compared with the NAV the backtest
   expects. The gap is split into rounding, untraded, timing, costs and residual (see
   [below](#live-against-backtest)).

## Install

```bash
pip install 'tradewind[ibkr]'      # ib_async, for the IBKR account; matplotlib for the chart: [plot]
```

Catalogue strategies need an environment with pwb-toolbox (and the papers harness,
`backtester.py`). Name its Python in the meta-portfolio file (`python:`). The run injects
this Tradewind into that environment.

## The meta-portfolio file

YAML (with PyYAML installed), JSON or CSV. The Phase 1 selection CSV
(`selection_final_*.csv`) loads as is: its `slug` and `weight` columns are the sleeves,
and `leverage_meta` sets the leverage.

```yaml
leverage: 4.0              # every weight is multiplied by it
max_gross: 3.8             # cap on gross / NAV of the summed book (IB allows 4: keep a margin)
rebalance: monthly         # units reset: daily, weekly, monthly (first close of a month), never
lot: 1                     # round to whole shares (0: fractional)
min_order_notional: 200    # smaller orders are not sent
papers_dir: ~/dev/pwb/papers
python: ~/venvs/pwb/bin/python                # the environment with pwb-toolbox
exclude_symbols: []        # data series that are not instruments (a spot commodity, an index)
sleeves:
  - {slug: dynamic_versus_static_asset_allocation_from_theory_halfway_to_practice, weight: 0.128}
  - {slug: protective_asset_allocation_paa_a_simple_momentum_based_alternative_for_term_deposits, weight: 0.075}
  - slug: decomposing_the_price_earnings_ratio          # the concentrated 30-50 name book
    weight: 0.109
    path: ~/dev/pwb/phase1/concentrated/decomposing_the_price_earnings_ratio/strategy.py
```

`capital:` fixes the amount the weights apply to. Without it, the account's NAV is used.

## The daily command

```bash
# after the US close (22:00 Paris), once the PWB datasets hold the day's close
python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --propose
# read ~/pwb-paper/orders/DATE.md, then
python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --send
```

The second command reuses the books computed by the first (`books/DATE/`). The strategies
are not run again, and the day is not tracked twice. If a strategy's last close is older
than the others', the run stops: the data are behind. Run it again later. `--allow-stale`
trades anyway, with a warning. `--as-of DATE` trades from a given close.

## Procedure for the paper account

**Once:**

1. Open TWS (or IB Gateway) and log in with the **paper trading** login. The account id
   starts with `DU`.
2. *Global Configuration > API > Settings*: tick *Enable ActiveX and Socket Clients*; port
   **7497** (Gateway: 4002); *Trusted IPs* 127.0.0.1; untick *Read-Only API* (the propose
   run connects read-only on its own side, and sending needs it unticked).
3. Check the account's base currency. The weights apply to the NetLiquidation the account
   reports. If the base is not USD, set `capital:` in USD in the meta-portfolio file.
4. Dry run without TWS: `--broker sim --replay 20` replays the last 20 closes on a simulated
   account (below).

**Every trading day, evening:**

1. TWS paper is open and logged in (it logs out once a day: log in again if needed).
2. `--propose`, then read `orders/DATE.md`: the data's date, the NAV, the gross and net
   exposure, each sleeve's units, the orders, the warnings.
3. `--send` if it looks right. Market-on-open orders are accepted until shortly before the
   open (9:28 New York, 15:28 Paris). Send them in the evening or the next morning.
4. To stop everything: `touch ~/pwb-paper/KILL`. `--send` then refuses to send anything until
   the file is removed.

The proposal lists orders that IB will refuse (an unknown ticker, a short with no borrow),
and the send reports them as `rejected`. Tracking then shows them as rounding.

## Files

| file | what |
|---|---|
| `orders/DATE.md`, `orders/DATE.csv` | the proposal (and the send status of each order) |
| `tracking.csv` (`.parquet` with pyarrow) | one row per day: realised and expected NAV, gap and its terms, gross, net, positions |
| `positions.csv` | one row per day and symbol: held, exact target, rounded target, close |
| `tracking.png` | realised against expected NAV, and the cumulative terms of the gap |
| `state.json` | the units, the books and the account at each close: the next day's starting point |
| `books/DATE/SLUG.json` | each strategy's books, as its run gave them |
| `sim_account.json` | the simulated account (`--broker sim`) |

## Live against backtest

The **expected NAV** is a backtest of the meta-portfolio built from the strategies' own
backtests. It holds `k_s` units of each strategy, and like them it decides at a close and
trades at the next open. Over a day, it changes by `Σ k_s × (change of the strategy's NAV)`.
On the day after a reset, the old units are kept overnight. It does not depend on the
account. The **gap** is the account's change minus the expected change. It splits into
terms computed from the same prices, which add up to the gap when nothing else happens:

| term | what it measures |
|---|---|
| `rounding` | held book against the planned exact book: lots, orders under the minimum, orders not sent, not filled or rejected |
| `untraded` | planned book against the strategies' backtests: an order a strategy's backtest refused at the open (its cash check) that the account executed, and the excluded symbols |
| `timing` | execution against the open the backtest fills at: `Σ fills quantity × (open − fill price)` |
| `costs` | the backtests' costs at the units minus the commissions and spreads paid |
| `residual` | everything else: interest, dividends and corporate actions the account books, data revised between two runs, a strategy whose fills are not at the open |

On the simulated account with no costs and fractional shares, the gap is zero to the cent:
the tests check it over 20 closes. With lots, costs and slippage, the residual stays at
zero, and the other terms add up to the gap.

## Simulated account and replay

`--broker sim` keeps an account in `sim_account.json` and fills every order at the next
open. Costs follow the harness: `--sim-costs harness` (the default) charges each symbol the
half-spread the strategy's own backtest charges it, and `--sim-commission ibkr_tiered` adds
IBKR's tiered commission.

`--replay N` runs each strategy **once** up to the last close and replays the last N closes
from that run. A strategy is causal, so its book at a past close is the same as if the run
had stopped there. Only the prices are cut at `--as-of` (`get_pricing`). A strategy that
reads other datasets sees them in full, so a replay of past days is a check of the
mechanics, not a new backtest.

From Python, the same pieces are `MetaPortfolio`, `PwbProvider` / `EngineProvider` (Tradewind
strategies built by a function), `ReplayProvider`, `SimAccount` / `IBPaperAccount` and
`DailyRunner` ([reference](reference/live.md)).

## Limits of this first version

- Each strategy runs its full history every day. A small book takes seconds; a 2 000-name
  universe takes several minutes and a few GB. The strategies run one after the other.
- US stocks and ETFs only. A strategy that trades a series that is not an instrument
  (spot commodities, reconstructed bond prices, indices) needs a tradable variant first.
  Until then, exclude it or its symbols.
- The cap applies to the planned book at the close. Marked at the next close, the account
  drifts with prices, so keep `max_gross` below IB's limit.
- At-close and conditional orders (limit, stop) are listed in the proposal's warnings and
  are not part of the target.
