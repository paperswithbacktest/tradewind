# The daily run of a crypto meta-portfolio on Kraken

!!! warning "Validation mode by default; weekdays only"
    `--broker kraken` reads a Kraken spot account and, with `--send`, sends every order with
    `validate=true`: Kraken checks it and **does not trade it**. Orders trade only with
    `--send --kraken-live` **and** `TRADEWIND_KRAKEN_LIVE=1` in the environment.

    The runner follows the **NYSE weekdays**: it trades Monday to Friday evenings, never on
    Saturday, Sunday or a US holiday, while crypto trades every day and the catalogue's crypto
    strategies are measured on seven days a week. **A seven-day calendar is required before
    real money for any crypto strategy measured on seven days** (spec/11 § 12): until then
    the live account rebalances later than its backtest after each weekend and holiday, and
    the gap to the backtest carries it.

[Live trading on Kraken](live-kraken.md) runs **one** strategy bar by bar, seven days a week.
This page runs a **meta-portfolio** of catalogue strategies on a Kraken account, once a day,
with the runner of [the daily paper run](live-paper.md): the same signals, aggregation,
proposal, tracking, scheduled day, alerts and files. What follows is what changes for
crypto; everything not said here is as on that page.

1. **Signals.** Each strategy runs as a backtest up to the last complete crypto bar (00:00
   UTC) and gives its target book, in coins.
2. **Aggregation.** `k` units of each strategy, summed, capped at `max_gross × capital`,
   rounded to `lot` (0: fractional), orders under `min_order_notional` dropped.
3. **Orders.** Compared with the wallet; written as a proposal; with `--send`, market orders,
   sells first, each checked locally and then sent to Kraken -- validated, or traded once
   armed. They execute at once: there is no opening auction.
4. **Tracking.** The account's NAV against the NAV the backtest expects, the gap split into
   rounding, untraded, timing, costs, fx and residual.

## Install

```bash
pip install tradewind          # nothing more for Kraken: REST over the standard library
```

Catalogue strategies need an environment with pwb-toolbox (`python:` in the meta-portfolio
file), as for [the daily paper run](live-paper.md#install). The Kraken key (see
[the key](live-kraken.md#the-key): trading yes, **withdrawal never**) is read from
`KRAKEN_API_KEY` and `KRAKEN_API_SECRET` only. Give the daily run a key of its own: Kraken's
nonce must increase per key, and two processes on one key would race.

## The meta-portfolio file

The file is the daily paper run's ([fields](live-paper.md#the-meta-portfolio-file)), with
values for a spot crypto account: no leverage, no shorts, fractional quantities.

```yaml
leverage: 1.0              # spot: no margin
max_gross: 0.95            # keep cash for the fees (0.40 % a trade) and the price moves to the fill
rebalance: monthly         # units reset: daily, weekly, monthly, never
lot: 0                     # fractional: Kraken cuts each order to its pair's lot_decimals (8 for BTC)
min_order_notional: 10     # USD; above every pair's costmin (0.5 USD), below it orders are dust
papers_dir: ~/dev/pwb/papers
python: ~/venvs/pwb/bin/python
fresh_prices: kraken       # the crypto closes completed from Kraken's public daily bars (no key)
fresh_close_grace: {crypto: 5}   # minutes after 00:00 UTC before the day's bar is taken
stale_symbol_policy: hold
sleeve_error_policy: skip
borrow_source: none        # spot: no shorts, no borrow
sleeves:                   # long-only strategies on Cryptocurrencies-Daily-Price (illustrative slugs)
  - {slug: a_crypto_trend_strategy, weight: 0.5}
  - {slug: a_crypto_momentum_strategy, weight: 0.5}
```

**Which strategies.** Long-only strategies whose symbols have a Kraken pair. A short target
is refused (`refused`, "no short"); 318 of the catalogue's 673 crypto symbols have a Kraken
USD pair, and an order on a symbol without one is refused ("Kraken has no USD pair"). The
proposal lists them; exclude those symbols (`exclude_symbols:`) or choose other sleeves.

**Fractional sizing.** The books come from the strategies' own backtests (fractional unless a
strategy rounds itself); `lot: 0` keeps the summed book fractional, and the account then cuts
each order toward zero to the pair's `lot_decimals` and skips one under the pair's
`ordermin` or `costmin` (`skipped`, no alert). A Tradewind strategy run with `LiveEngine`
sizes fractionally with `FractionalCommInfo` ([sizing](live-kraken.md#crypto-is-not-a-stock-market)).

### The account's currency

The books are in USD and in coins. The account's currency is the quote currency of the pairs
it trades, `--kraken-quote USD` (the default) or `EUR`; the quantity in coins is the same
whichever quote the order is sent against (`BTC` 0.05 is `XBT/USD` or `XBT/EUR` 0.05).

- **USD** (the default): no conversion; cash is the USD balance.
- **EUR**: cash is the EUR balance, converted to USD at the day's rate, as for an IBKR
  account in EUR ([the account's currency](live-paper.md#the-accounts-currency)): PWB's
  `EURUSD` series, then `--base-fx-rate`. `capital:` is in EUR unless `capital_currency: USD`;
  the `fx` term of the gap isolates the EUR cash's move against USD.

Positions are every coin balance with a pair against the quote (or against USD), in coins,
under the books' symbols (`BTC`, or `BTCUSD` when that is the books' name). Another fiat
balance is not a position; staked and rewards balances (`.S`, `.F`, `.M`...) are ignored.

## The daily command

```bash
export KRAKEN_API_KEY=... KRAKEN_API_SECRET=...      # in the shell, never in a file of a repository

# propose only: reads the wallet, never calls AddOrder
python -m tradewind.live.daily --meta crypto.yaml --dir ~/pwb-crypto --broker kraken \
    --fresh-prices kraken
# send in validation mode: every order checked by Kraken (validate=true), nothing trades
python -m tradewind.live.daily --meta crypto.yaml --dir ~/pwb-crypto --broker kraken \
    --fresh-prices kraken --send --kraken-allowed BTC,ETH --kraken-max-order 500
# armed (after the sign-off): both keys
TRADEWIND_KRAKEN_LIVE=1 python -m tradewind.live.daily --meta crypto.yaml --dir ~/pwb-crypto \
    --broker kraken --fresh-prices kraken --send --kraken-live --kraken-allowed BTC,ETH \
    --kraken-max-order 500
```

| option | |
|---|---|
| `--broker kraken` | the Kraken spot account (`KrakenAccount`) |
| `--kraken-quote USD\|EUR` | the pairs' quote currency, the account's currency (default USD) |
| `--kraken-allowed BTC,ETH` | the pair whitelist, in the books' symbols: any other order is `refused` |
| `--kraken-max-order 500` | the largest order's notional, USD: a larger one is `refused` |
| `--kraken-taker-fee 0.004` | the fee the checks and the proposal assume (0.40 %, the entry tier, until the account's own is known) |
| `--kraken-live` | with `--send` and `TRADEWIND_KRAKEN_LIVE=1`, orders trade; otherwise validation |

The status of each order, in the proposal and the report:

| status | meaning | alert |
|---|---|---|
| `validated` | Kraken checked it (`validate=true`), nothing traded | none |
| `sent` | armed: Kraken took it (its `txid` is listed) | none |
| `skipped` | under the pair's lot, `ordermin` or `costmin` (dust) | none |
| `refused` | a local guard: whitelist, notional, balance, no short, no pair | `orders_rejected` |
| `rejected` | Kraken refused it (`EOrder:...`, `EGeneral:...`) | `orders_rejected` |

Sells go first, and the buys are checked against the quote balance plus the sells' proceeds
less the fee. Every check runs before any call: a refused order never reaches Kraken.

## Fresh prices

`Cryptocurrencies-Daily-Price` is refreshed once a month. `--fresh-prices kraken` (or
`fresh_prices: kraken`) completes the crypto symbols the strategies load up to the session,
from Kraken's public daily bars (no key, one call a second):

- the parent process asks Kraken once per session, before the strategies run, for the
  crypto symbols of the last sessions' caches and of the sleeves' last books, and writes
  `prices/SESSION/KRAKEN_OHLC/SYMBOL.json`; the strategies' processes read those files;
- a bar dated D is taken only when the file was written after D+1 00:00 UTC plus
  `fresh_close_grace: {crypto: 5}` minutes; the forming bar is never written. Crypto trades
  every day: the bar expected at session D is D's own;
- the series is continued the way the daily paper run continues any series
  ([fresh prices](live-paper.md#fresh-prices)): chained on the last common day before the
  dataset's last bar (that bar is often partial), and refused when the two sources disagree
  beyond the crypto tolerance (0.5 %). Kraken's daily closes and the catalogue's agree to a
  median 0.005-0.02 % (BTC, ETH, SOL, DOGE): they are the same 00:00 UTC day;
- a symbol Kraken does not list against USD, or whose file is missing or not final, falls
  back on the next source listed in `--fresh-prices`, or stays stale (`stale_symbol`).

The day's line says who served what: `fresh prices (kraken): 12 symbol(s), IB 0, Kraken 12
(100.0%), ...`.

## Procedure

**Once:**

1. Create the key ([permissions](live-kraken.md#the-key): no withdrawal), restricted to the
   machine's IP if possible, and keep it out of any repository: in the shell, or in the
   scheduler's env file (`~/.config/tradewind/daily.env`, `chmod 600`).
2. Fund the account in the quote currency (USD by default). Set `capital:` (in that
   currency) to run on part of it.
3. Dry run without a key: `--broker sim --replay 20` replays the last 20 sessions on a
   simulated account ([below](live-paper.md#simulated-account-and-replay)), with
   `lot: 0`.

**Every weekday, after 00:05 UTC (evening in New York, night in Paris):**

1. `--propose`, then read `orders/DATE.md`: the data's date (the session's own crypto bar),
   the NAV, each sleeve's units, the orders and their statuses, the warnings.
2. `--send`: in validation mode, every order comes back `validated` or `rejected` with
   Kraken's reason; after the sign-off, `--kraken-live` with `TRADEWIND_KRAKEN_LIVE=1`.
3. To stop everything: `touch ~/pwb-crypto/KILL`. `--send` then sends nothing until the file
   is removed.

## The scheduled day

`python -m tradewind.live.daily.session` runs the day in one command
([the scheduled day](live-paper.md#the-scheduled-day)), with the same options:

```bash
python -m tradewind.live.daily.session --meta crypto.yaml --dir ~/pwb-crypto --broker kraken \
    --fresh-prices kraken            # add --send for validation, --send --kraken-live armed
```

- **The session** is the last NYSE session whose close is 30 minutes behind: a run at 00:15
  UTC (19:15 or 20:15 New York) works on that day's session, whose crypto bar is final.
  Saturdays, Sundays and US holidays are not sessions: their bars are part of the next run's
  history, and nothing is traded for them.
- **Once per session**, one send per session, as on the paper run.
- **The send cut-off** is the next NYSE open (09:28 New York): a run later than that
  proposes only and raises `missed_open`. For Kraken, send in the night the bar completes.
- **Reconciliation:** the wallet against the previous session's wallet plus the orders
  traded then; a `validated`, `skipped`, `refused` or `rejected` order counts as nothing
  traded, so validation mode raises no `fills_mismatch`.

### Alerts and exit codes

The alerts and exit codes are the daily paper run's
([table](live-paper.md#alerts-and-exit-codes)); for Kraken:

| alert | on Kraken |
|---|---|
| `broker_unreachable` (exit 6) | the key is missing or wrong (`KRAKEN_API_KEY` / `KRAKEN_API_SECRET`), lacks *Query Funds*, Kraken is down, or `EAPI:Invalid nonce` (another process on the same key) |
| `orders_rejected` | an order `refused` by a guard or `rejected` by Kraken, with the reason |
| `stale_prices` / `stale_symbol` | the session's crypto bar is not final yet (a run before 00:05 UTC), or Kraken and the catalogue disagree on a symbol |
| `missed_open`, `kill_switch` | as on the paper run (the cut-off is the next NYSE open) |
| `fills_mismatch` | armed only: the wallet differs from the previous wallet plus the orders sent (a manual trade, a deposit or withdrawal of coins) |
| `tracking_gap`, `daily_loss` | as on the paper run; expect the timing term after weekends (below) |
| `short_borrow` | never (no shorts) |

| exit code | |
|---|---|
| 0 | done, no alert |
| 2 | usage |
| 3 | stale prices: nothing proposed; run again after 00:05 UTC |
| 4 | done, with alerts |
| 5 | the run failed |
| 6 | the account could not be opened or read |
| 7 | another run holds the lock |

### Scheduling

[`tradewind-daily-kraken.cron`](examples/schedule/tradewind-daily-kraken.cron) runs the
wrapper [`tradewind-daily.sh`](examples/schedule/tradewind-daily.sh) at **00:15 UTC, Tuesday
to Saturday**, with `BROKER=kraken FRESH_PRICES=kraken`:

```text
15 0 * * 2-6  BROKER=kraken FRESH_PRICES=kraken $HOME/pwb-crypto/tradewind-daily.sh
```

That is 19:15 or 20:15 in New York the evening before, Monday to Friday, after the crypto bar
is final, all year (the tests check every weekday of a year); 01:15 or 02:15 in Paris. With
`CRON_TZ=America/New_York`, `15 20 * * 1-5` is the same. On a Mac, a launchd job at 02:15
Paris time, Tuesday to Saturday, does it. The key goes into the wrapper's env file
(`KRAKEN_API_KEY=...`, `KRAKEN_API_SECRET=...`, `chmod 600`), never into the wrapper or a
repository; sending stays off unless `TRADEWIND_SEND=1`, and trading off unless the
arguments add `--kraken-live` and the env file `TRADEWIND_KRAKEN_LIVE=1`.

## Files

As on the paper run ([files](live-paper.md#files)): `orders/`, `tracking.csv`,
`positions.csv`, `state.json`, `books/`, `reports/`, `sessions/`, `prices/SESSION/KRAKEN_OHLC/`.
The Kraken account keeps no file of its own: the wallet is read from Kraken each run.

## Live against backtest

The expected NAV is the backtest of the meta-portfolio ([live against
backtest](live-paper.md#live-against-backtest)). On Kraken:

- **timing**: the backtest fills at the next bar's open (00:00 UTC); the account fills when
  the run sends, 15 minutes to an hour later, and after a weekend or a holiday, days later:
  the strategies' books moved on Saturday and Sunday, the account follows on Monday night.
  This is the term the seven-day calendar removes;
- **costs**: the fees paid (each trade's, in the quote currency) against the backtests'
  costs;
- **rounding**: the lot cut and the orders under the minimums;
- **fx**: the EUR cash's move against USD, for an EUR account.

## Limits of this first version

- **Weekdays only.** Crypto strategies measured on seven days are traded on five: a
  seven-day calendar (a session every 00:00 UTC) is required before real money for them.
- Long-only, no leverage, spot pairs against USD or EUR; a symbol without a Kraken pair is
  not traded.
- The symbols Kraken is asked for are those of the last sessions' price caches and of the
  sleeves' last books: on a sleeve's first run its symbols go to the next source listed (or
  stay stale), and Kraken serves them from the next session.
- The fee the checks assume is 0.40 % until the account's tier is set (`--kraken-taker-fee`).
- No average cost from Kraken: positions are valued at the books' closes, or at Kraken's
  last trade for a coin no book prices.
- What Kraken answers to `validate=true`, and whether it accepts the 18-character client order
  id, are checked on the first run with a key: the run reports both.
