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
exclude_symbols: []        # data series that are not instruments (a spot commodity)
substitutes:               # indices are not tradable (below); SPX -> SPY is the default
  VIX: VIXY
notes:                     # repeated in the day's warnings while the symbol is held or targeted
  ONDS: hard to borrow (IB shortable 2.0 on 2026-11-20)
borrow_check: false        # IBKR: read the shortable tick of every short before sending
borrow_source: auto        # the shorts' fee and availability (below); auto is the default at IBKR
borrow_fee_alert: 1.0      # % a year: a short whose fee is at or above it is hard to borrow
sleeves:
  - {slug: dynamic_versus_static_asset_allocation_from_theory_halfway_to_practice, weight: 0.128}
  - {slug: protective_asset_allocation_paa_a_simple_momentum_based_alternative_for_term_deposits, weight: 0.075}
  - slug: decomposing_the_price_earnings_ratio          # the concentrated 30-50 name book
    weight: 0.109
    path: ~/dev/pwb/phase1/concentrated/decomposing_the_price_earnings_ratio/strategy.py
```

`capital:` fixes the amount the weights apply to. Without it, the account's NAV is used.

### The account's currency

The strategies' books are in USD; the account can be in any currency (the IBKR paper account
used for Phase 1 is in EUR). The run never assumes it:

- **Read off the account.** At IBKR, the currency of `NetLiquidation` / `TotalCashValue`
  (`$LEDGER` tags accepted): EUR, USD, GBP, CHF ... A simulated account is in USD unless
  `--sim-currency` (or `--account-currency`) says otherwise; its `--sim-cash` is in that
  currency.
- **`account_currency:` / `--account-currency`** is a check: when given and different from
  the account's, the run stops (`account currency: ...`, exit 2) instead of sizing wrongly.
- **`capital:`** is in the account's currency, or in USD with `capital_currency: USD`.
- **The day's rate** (USD per unit of the account's currency), in this order: IB's own
  (`ExchangeRate`, published once the account holds USD), PWB's forex series `<CUR>USD`
  (a sleeve's close, or the PWB datasets, completed by `--fresh-prices`), then
  `--base-fx-rate`. None: the run stops with an explicit error. The books are sized in USD at
  that rate.
- **Reported in both currencies**: the proposal and the day's report give the currency, how
  it was known (`detected`, `forced`, `default`, `simulated account`), the rate and its source,
  the NAV, cash and gap in the account's currency; `tracking.csv` has `account_currency`,
  `fx_rate`, `fx_source`, `nav_base`, `gap_day_base`, `gap_cum_base`.
- **The fx term of the gap.** The cash held in the account's currency (IB's `CashBalance` in
  that currency) is worth more or fewer USD as the rate moves; the tracking isolates it,
  `fx = base_cash(t-1) x (rate(t) - rate(t-1))`, instead of leaving it in the residual. A USD
  account has no fx term: its run is unchanged.

### Spot forex (IDEALPRO)

A strategy that trades PWB's forex series (`AUDUSD`, `JPYUSD` ...: the USD price of one unit
of the currency, the book in units of that currency) is traded on IBKR's IDEALPRO spot pairs.
The run itself (targets, gross, tracking) stays in the strategy's units, whose notional is in
USD. The IBKR account converts at the broker:

- **IB's direction.** The pair's base is the first of EUR, GBP, AUD, NZD, USD it contains:
  `AUDUSD` is `AUD.USD` (same quantity), `JPYUSD` is `USD.JPY`, inverted: buying `q` JPY
  sells `q × price` USD. So are CAD, CHF, NOK, SEK, PLN, CZK, TRY, MXN, ZAR, KRW.
- **Orders** are in units of the pair's base currency, rounded to one unit, market and good
  till cancelled (IDEALPRO has no opening auction: the order fills when it is sent, or at the
  reopening on Sunday evening).
- **Positions and fills** come back in the strategy's units: an inverted position of `x` USD
  at an average cost of `S` JPY per USD holds `-x × S` JPY, the yen actually exchanged. Its
  difference with the plan (the move between the reference close and the fill) is tolerated up
  to 3 % by the reconciliation.
- **Never traded**, with the reason in the warnings: BRL, CLP, ISK, INR, IDR (no IDEALPRO pair:
  non-deliverable currencies), RUB (USD.RUB is listed, its trading is restricted), and any
  currency Tradewind does not know on IDEALPRO. A series quoted the other way (`USDJPY`, a
  price in JPY) is not traded either: its backtest values the position in JPY as if it were USD.
- **KRW** has a pair (USD.KRW) but is not freely convertible: it is traded with a warning;
  try a paper order before relying on it.
- **Odd lots.** IDEALPRO fills orders from about 25,000 USD at the interbank quote; a
  smaller order is an odd lot, at a wider price. Each one is named in the warnings
  (`fx_odd_lot:` sets the threshold). With a forex sleeve of about 30,000 USD at leverage 4,
  most of its orders are odd lots.
- Commission (proposal estimate and simulated account): 0.20 bp of the trade value, at
  least 2 USD.
- The carry of the strategy's series (the forward points, `.FXR` series) is not traded: the
  account earns or pays the interest of its currency balances instead.

### Indices and their substitutes

An index (`SPX`, `VIX`, `NDX`, `RUT`, a symbol ending in `.IDX` ...; `indices:` adds more) cannot
be bought. The meta-portfolio says what is traded instead:

```yaml
substitutes:
  SPX: SPY                 # the default: SPY at the same notional
  VIX: VIXY                # an ETP at the same notional: a proxy, VIXY is not the VIX
  # SPX: null              # no substitute: SPX is refused
  # SPX: {symbol: MES, kind: future, multiplier: 5, expiry: "20261218", exchange: CME}
```

- **ETF** (`SPY`, `VIXY`, `VXX`): `q × index / ETF price` shares, at each close. The ETF's
  prices come from a sleeve that holds it, or else from the PWB datasets (completed by
  `--fresh-prices` like the strategies' loads). The sleeve's backtest stays on the index:
  what the substitute does differently (dividends, VIXY's roll) goes to the residual of the
  tracking.
- **Future** (an option, never on by default): `q / multiplier` contracts of the contract
  named (`expiry` `YYYYMMDD` or `YYYYMM`, `exchange`), priced at `multiplier × index` (the basis
  is ignored: fine for ES / MES on SPX, poor for VIX futures, which trade far from the spot).
  Nothing rolls: within `roll_days` (5) of the expiry the run stops until the next contract
  is written. Sent as a market order for the day (no opening auction on CME / CFE). Not yet
  tried on the paper account.
- **No substitute**: the index is refused, with its reason in the warnings, and the IBKR
  account refuses any batch of orders that holds one before sending anything.

### Names to watch

The day's warnings (and the session report) also say:

- `watch: KVUE: last price 2025-…` — a symbol held or targeted whose data stopped before the
  close (a delisted stock still in a book); `watch: X: close unchanged … over the last 5
  closes` — prices frozen (a week: fewer would flag the few days a monthly dataset is carried
  forward at its end);
- `watch: ONDS: hard to borrow (IB shortable 2.0)` — with `borrow_check: true` on an IBKR
  account, every short is checked on IB's shortable tick (236, delayed data accepted,
  read-only): 1.5-2.5 needs a locate, 1.5 or less is not available;
- `note: …` — the meta-portfolio's `notes:` while the symbol is held or targeted.

`watch:` lines raise the `symbol_watch` alert of the session; `note:` lines do not.

### The cost of borrowing the shorts

Every short of the day, proposed (a negative target) or held, gets the borrow fee and the shares
the broker can lend that day, and its cost of carry, in a *Short borrow* section of the proposal
and of the report of the day (`borrow` in `reports/SESSION.json`):

| column | |
|---|---|
| short (target / held) | the target's quantity and the account's; the short carried is the target's, or the held one when the target closes it (one more night) |
| short value | the short carried × the close, USD |
| fee bps/yr | IB's indicative borrow fee, in basis points a year |
| available | the shares IB can lend (`> 10,000,000` when IB caps the figure) |
| cost a day | fee × short value / 365, USD; the total a day and a year is above the table |

**Sources** (`borrow_source:` or `--borrow-source`):

- `hub`: IB's short-stock file (`ftp2.interactivebrokers.com`, user `shortstock`) as the private
  dataset `paperswithbacktest/InteractiveBrokers-Daily-ShortAvailability` keeps it, one snapshot
  a US business day after the close (the latest is used; a snapshot more than 4 days older than
  the session is used with a note). It works without TWS / IB Gateway; it needs
  `huggingface_hub`, `pyarrow` and a Hugging Face login that can read the dataset. The file lists
  every stock IB can lend: a short that is not in it has no availability.
- `ib`: IB's shortable tick (236) on the IBKR account, read-only, delayed data accepted. It gives
  a level, not a fee: the costs are then `n/a`.
- `auto` (the default on an IBKR account): `hub`, then `ib` when the dataset cannot be read; the
  note says why. `none` (the default on the simulated account): no section.

If no source answers, every short is `unknown`, with the reason in the notes; the run goes on.

**Flags.** A short is flagged, in the warnings (`borrow: …`) and with the session's alert
`short_borrow`, when it is

- `hard_to_borrow`: a fee at or above `borrow_fee_alert` (1 % a year by default; USD general
  collateral costs about 0.25 %, 25 bps, and IB's list had SPY at 0.26 % on 2026-11-20), or an IB
  shortable level of 2.5 or less;
- `no_availability`: absent from IB's list, 0 shares available, or a shortable level of 1.5 or
  less: the short will be refused;
- `insufficient`: fewer shares available than the short (when IB does not cap the figure).

The fee is IB's indicative rate for the day: the rate actually charged is set each day on the
position held, and the account also earns (or not) a rebate on the short proceeds, which the
report gives in the JSON (`rebate_rate`) but does not net.

## The daily command

```bash
# after the US close (22:00 Paris); the monthly datasets completed to the close (below)
export ALPHAVANTAGE_API_KEY=...
python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --propose --fresh-prices alphavantage
# read ~/pwb-paper/orders/DATE.md, then
python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --send --fresh-prices alphavantage
```

The second command reuses the books computed by the first (`books/DATE/`). The strategies
are not run again, and the day is not tracked twice. If a strategy's last close is older
than the others', the run stops: the data are behind. Run it again later. `--allow-stale`
trades anyway, with a warning. `--as-of DATE` trades from a given close.

## Fresh prices

The PWB daily price datasets are refreshed **once a month**, and the run refuses data more
than 4 days older than the last session. `--fresh-prices alphavantage` (or
`fresh_prices: alphavantage` in the meta-portfolio file) completes them up to the last
close before the strategies compute their signals. Each strategy's price loads
(`load_dataset` and so `get_pricing`) get the AlphaVantage bars that come after the dataset's
last day, only for the symbols that strategy loads.

| | |
|---|---|
| key | `ALPHAVANTAGE_API_KEY` in the environment (the name data-api uses). It is never written to a file or printed. Without it the run stops before any strategy runs. |
| rate | `ALPHAVANTAGE_RPM` requests a minute, 75 by default (the smallest premium plan). A throttling answer waits a minute and retries; a network error is retried four times. |
| sources | stocks and ETFs: `TIME_SERIES_DAILY_ADJUSTED`, `outputsize=compact` (100 sessions; `ALPHAVANTAGE_EQUITY_FUNCTION=TIME_SERIES_DAILY` for raw closes). Currency pairs: `FX_DAILY`. Crypto: `DIGITAL_CURRENCY_DAILY`. |
| not covered | indices, commodities, bonds. A live series of these datasets that is behind stops the run, listed as not covered. |
| cache | `DIR/prices/SESSION/FUNCTION/SYMBOL.json`: each answer is read once a session, shared by the strategies, the `--send` run and a rerun after an error. |

**The junction.** The anchor is the last day both sources have before the dataset's last
bar. That bar is skipped because the monthly source sometimes downloads it during the
session, as a partial bar. The chain factor `k` is the dataset's adjusted close divided
by AlphaVantage's on the anchor. The dataset's history up to the anchor is divided by `k`,
so its returns do not change. The bars after the anchor are AlphaVantage's own, so the last
close is the price the account trades at. A dividend or a split between the two cut-offs
then reads like an event the adjusted series has taken in. It is not "un-adjusted" at the
junction, and orders are not sized in pre-split shares.

**The check.** `k` must agree within 0.5 % on the anchor and the four common days before
it. Otherwise the two sources do not quote the same series (a reused ticker, a split one
of them missed), and the symbol is refused. A dataset last bar more than 0.5 % away from
AlphaVantage's is replaced and listed in the proposal's warnings.

**When the run stops.** A symbol is *live* when its last bar is within 45 days of the
session. Older ones are delisted names and are left as they are. The market's last close is
SPY's last AlphaVantage bar, so a holiday is not counted as a missing day. A live symbol
still ending before that close after the top-up is *stale*: unknown to AlphaVantage,
refused, not covered, or not read. Then the strategy stops, its books are not cached, and
the run exits with code 3, proposes nothing, and lists the symbols. Run it again: the
answers already read come from the cache.

Bars dated after the last session whose close (16:30 New York) has passed are never taken.
A run at 22:00 Paris uses the day's close. The books of a fresh run are cached apart from the
others (`books/latest-DATE-fresh/`).

## Procedure for the paper account

**Once:**

1. Open TWS (or IB Gateway) and log in with the **paper trading** login. The account id
   starts with `DU`.
2. *Global Configuration > API > Settings*: tick *Enable ActiveX and Socket Clients*; port
   **7497** (Gateway: 4002); *Trusted IPs* 127.0.0.1; untick *Read-Only API* (the propose
   run connects read-only on its own side, and sending needs it unticked).
3. The account's currency is read off the account ("The account's currency" above): set
   `capital:` in it (or `capital_currency: USD`), and `account_currency:` if you want the run
   to check it. Until IB publishes its own USD rate (once the account holds USD), the rate is
   PWB's `<CUR>USD` series, or `--base-fx-rate` (EURUSD: the price of 1 EUR in USD).
4. Dry run without TWS: `--broker sim --replay 20` replays the last 20 closes on a simulated
   account (below).

**Every trading day, evening:**

1. TWS paper is open and logged in (it logs out once a day: log in again if needed, or let
   [IBC](keeping-ib-gateway-running.md) do it).
2. `--propose`, then read `orders/DATE.md`: the data's date, the NAV, the gross and net
   exposure, each sleeve's units, the orders, the warnings.
3. `--send` if it looks right. Market-on-open orders are accepted until shortly before the
   open (9:28 New York, 15:28 Paris). Send them in the evening or the next morning.
4. To stop everything: `touch ~/pwb-paper/KILL`. `--send` then refuses to send anything until
   the file is removed.

The proposal lists orders that IB will refuse (an unknown ticker, a short with no borrow),
and the send reports them as `rejected`. With *Read-Only API* still ticked, every order is
refused (warning 321) and reported as `rejected`. Tracking then shows them as rounding.

## The scheduled day

`python -m tradewind.live.daily.session` is the day in one command, for a scheduler. It takes
the same options as `python -m tradewind.live.daily` (`--meta`, `--dir`, `--broker`,
`--fresh-prices`, `--send`...) and adds the calendar, the run-once rule, the report of the day
and the alerts:

```bash
python -m tradewind.live.daily.session --meta meta.yaml --dir ~/pwb-paper --broker sim \
    --fresh-prices alphavantage            # add --send to place the orders
```

1. **The session** is the last NYSE session whose close is 30 minutes behind
   (`--close-margin`), read in New York time from Tradewind's NYSE calendar (holidays, the
   13:00 early closes, daylight saving). Before the close the run works on the previous
   session; on a holiday or a weekend it finds the last session done and does nothing.
2. **Once per session.** `daily.lock` keeps two runs apart (the second exits with 7), and
   `sessions/SESSION.json` records how far the day went. A session already proposed is not
   run again (`--force` proposes it again); a second run with `--send` sends a proposed
   session; a session sent, or whose send started, is never sent again. Any run after that
   prints the stored summary and returns the stored exit code.
3. Fresh prices, signals and the proposal, then the send (with `--send`), as
   [above](#the-daily-command), on the session's close: every sleeve must have it.
   Market-on-open orders are sent only before 09:28 New York of the next session; later, the
   day is proposed only and the alert `missed_open` is raised.
4. **Reconciliation:** the account's positions against the previous session's positions plus
   the orders sent then (`fills_mismatch`), and the orders still working at the broker
   before the day's orders (`orders_working`).
5. **The report of the day**, `reports/SESSION.md` and `reports/SESSION.json` (and
   `reports/latest.*`): a one-line summary, the portfolio's value and the day's change, the
   orders proposed, sent and filled at the day's open, the gap to the backtest and its terms,
   each sleeve's close, units and state (error, stale symbols), the stale symbols, the
   reconciliation, the shorts' borrow fee and cost of carry, the warnings and the alerts.

```text
2026-12-15 DONE | NAV 110,214 (+0.19%) | gap to backtest -38 (-0.03%) | orders 14 proposed, 14 sent, 12 filled | alerts: none
```

### Alerts and exit codes

| alert | raised when |
|---|---|
| `stale_prices` | a sleeve has no book at the session's close (data behind, symbols still stale after the fresh-price top-up); the report lists the symbols and the sleeves |
| `run_failed` | a strategy or the run failed; every sleeve is still run, and those in error are listed |
| `broker_unreachable` | the account cannot be opened or read (TWS / IB Gateway closed or logged out) |
| `tracking_gap` | the cumulative gap to the backtest is above `--alert-gap` of the NAV (1 %), or the day's gap above `--alert-gap-day` (0.5 %) |
| `daily_loss` | the day's loss is beyond `--alert-loss` of the previous NAV (3 %) |
| `orders_rejected` | the broker refused or cancelled orders sent |
| `missed_open`, `kill_switch` | `--send` asked but nothing sent: past the open's cut-off, or `KILL` present |
| `send_interrupted` | a send started and did not finish: check the broker's orders by hand |
| `fills_mismatch`, `orders_working` | the reconciliation above |
| `symbol_watch` | a held or targeted name whose data stopped or froze, or a hard-to-borrow short ("Names to watch") |
| `short_borrow` | a short with a fee at or above `borrow_fee_alert`, or with no or too few shares to borrow ("The cost of borrowing the shorts") |

| exit code | |
|---|---|
| 0 | done, no alert (or nothing to do) |
| 2 | usage: options, meta-portfolio file |
| 3 | stale prices: nothing proposed; run it again later |
| 4 | done, with alerts |
| 5 | the run failed |
| 6 | the broker is unreachable |
| 7 | another run holds the lock |

**Notification.** `--notify-cmd "COMMAND"` (or `TRADEWIND_NOTIFY_CMD`) runs a command of yours
when the day has an alert (`--notify always`: every day). It gets the summary on its
standard input, and `TW_DAILY_SESSION`, `TW_DAILY_STATUS`, `TW_DAILY_EXIT_CODE`,
`TW_DAILY_SUMMARY`, `TW_DAILY_REPORT_MD` and `TW_DAILY_REPORT_JSON` in its environment. It is
given a minute; its exit code goes into the JSON report. Tradewind sends nothing itself:
the command is where a mail, a chat message or a phone notification is plugged in.

### Scheduling

Examples in [`docs/examples/schedule/`](examples/schedule/tradewind-daily.sh) (nothing is
installed by Tradewind):

| file | for |
|---|---|
| [`tradewind-daily.sh`](examples/schedule/tradewind-daily.sh) | the wrapper the schedulers call: the paths, the options, the secrets read from `~/.config/tradewind/daily.env`, a log per day in `DIR/logs/`. Proposals only unless `TRADEWIND_SEND=1` |
| [`com.paperswithbacktest.tradewind-daily.plist`](examples/schedule/com.paperswithbacktest.tradewind-daily.plist) | launchd on a Mac: weekdays at 22:45 Paris time |
| [`tradewind-daily.cron`](examples/schedule/tradewind-daily.cron) | cron on a server: `CRON_TZ=America/New_York`, weekdays at 17:15 New York |
| [`tradewind-daily.service`](examples/schedule/tradewind-daily.service), [`.timer`](examples/schedule/tradewind-daily.timer) | systemd: `OnCalendar=Mon..Fri 17:15 America/New_York`, `Persistent=true` |

**Daylight saving.** The US and Europe change their clocks on different Sundays: New York is
6 hours behind Paris, and 5 during two to three weeks in March and in October-November. A
schedule in Paris time at 22:45 is 16:45 or 17:45 in New York, after the close all year (the
tests check every weekday of a year). On a server, schedule in New York time (`CRON_TZ`,
systemd's time zone in `OnCalendar`); with a cron that only knows UTC, run twice (21:45 and
22:45 UTC): the run decides from the NYSE calendar, and the second run finds the session done.

**Holidays** need nothing in the schedule: the run reads the NYSE calendar, and a holiday's
run finds the last session done. A run missed (the Mac asleep, the server down) happens at the
next wake or boot (launchd, `Persistent=true`): it does the session it missed, and sends only
if the next open has not passed.

**Install on the Mac** (by hand, after editing the paths):

```bash
cp docs/examples/schedule/tradewind-daily.sh ~/pwb-paper/ && chmod +x ~/pwb-paper/tradewind-daily.sh
mkdir -p ~/.config/tradewind && echo 'ALPHAVANTAGE_API_KEY=...' > ~/.config/tradewind/daily.env
chmod 600 ~/.config/tradewind/daily.env
cp docs/examples/schedule/com.paperswithbacktest.tradewind-daily.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.paperswithbacktest.tradewind-daily.plist
sudo pmset repeat wakeorpoweron MTWRF 22:40:00     # optional: wake the Mac for the run
```

For an IBKR paper account, TWS or IB Gateway must be running and logged in at the time of the
run (`BROKER=ibkr` in the wrapper); IB Gateway with its daily auto-restart is the usual choice
on a machine without a screen. [Keeping IB Gateway running](keeping-ib-gateway-running.md)
starts and logs it in unattended with IBC (launchd and systemd examples), and handles the
daily restart and the Sunday logout.

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
| `reports/SESSION.md`, `reports/SESSION.json` | the report of the day (`session`) |
| `sessions/SESSION.json`, `daily.lock` | how far each session went, and the lock of the run in progress |

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
- Fresh prices cost one request per live symbol a session. A strategy that loads a
  2 000-name universe makes up to ~2 000 requests, about 27 minutes at 75 a minute, the
  first time in a session. The next strategies and the `--send` run read the cache.
- US stocks and ETFs, IDEALPRO spot forex against USD, and indices through a substitute.
  A strategy that trades another series that is not an instrument (spot commodities,
  reconstructed bond prices) needs a tradable variant first. Until then, exclude it or its
  symbols.
- The cap applies to the planned book at the close. Marked at the next close, the account
  drifts with prices, so keep `max_gross` below IB's limit.
- At-close and conditional orders (limit, stop) are listed in the proposal's warnings and
  are not part of the target.
