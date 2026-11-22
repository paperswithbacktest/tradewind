# Live trading on Kraken (crypto spot)

!!! warning "Preview, validation mode by default"
    `tradewind.live.kraken` is a preview: Kraken spot pairs against USD (the default) or EUR,
    daily bars. Every order goes to Kraken's `AddOrder` with `validate=true`: Kraken checks it
    against the account and **does not trade it**. Orders trade only with two keys turned,
    `live=True` **and** `TRADEWIND_KRAKEN_LIVE=1` in the environment. The design document is
    [`spec/11-live-kraken.md`](https://github.com/paperswithbacktest/tradewind/blob/main/spec/11-live-kraken.md).

Several strategies in one Kraken account, once a day: see
[the daily run of a meta-portfolio on Kraken](live-kraken-daily.md).

The same strategy class that backtests runs live, without a line changed: `LiveEngine` is
the backtest engine (the same sixteen phases of a slice) driven by a live clock, with a
broker that sends orders to an account instead of simulating them. Kraken plugs into the
same seam as [Interactive Brokers](live-trading.md): `LiveEngine`, `LiveBroker`, the journal,
the replay, the reconciliation, the guards and the kill switch are the same; only the
adapter (`KrakenAdapter`) and the bar source (`KrakenBars`) are Kraken's.

```bash
pip install tradewind      # nothing more: the adapter speaks Kraken's REST API with the standard library
```

## What this step covers

| in | not yet |
|---|---|
| daily bars stamped at 00:00 UTC, crypto spot pairs against USD or EUR, one strategy per session | intraday bars, margin, futures, staking, transfers (withdrawals: never) |
| market, limit, stop-loss, stop-loss-limit; GTC / GTD / DAY (to the next 00:00 UTC) | trailing stops, iceberg, conditional close, order amendment |
| executions, partial fills, cancellations, expiries, rejections | WebSocket streaming (REST polling is enough for daily bars) |
| reconciliation of the wallet and cash at start and at every slice | an average cost from Kraken (positions are adopted at the last trade price) |
| journal, replay, resume after a stop or a crash | several strategies sharing one key (one key per process) |
| validation mode by default, risk limits, balance check, kill switch, Kraken's rate limits | a hosted, supervised deployment |

## The key

Create it in Kraken under *Settings > API*, with these permissions and no others:

| permission | used for |
|---|---|
| Query Funds | balances (`BalanceEx`) |
| Query Open Orders & Trades | working orders and their fills |
| Query Closed Orders & Trades | fills missed while the session was down (`TradesHistory`) |
| Create & Modify Orders | `AddOrder`, also needed for `validate=true` |
| Cancel/Close Orders | `CancelOrder`, the kill switch |
| **Withdraw Funds**, Deposit, Export Data | **never** |

Restricting the key to the machine's IP address is recommended. Export it in the shell that
runs the session, never in a file of a repository:

```bash
export KRAKEN_API_KEY=...        # the public key
export KRAKEN_API_SECRET=...     # the private key, base64, as Kraken shows it
```

Tradewind reads these two variables only, at `connect`. The key is never written to the
journal, the state file, a proposal or a log, its `repr` is redacted, and every error message
is scrubbed of it.

**One key per process.** Kraken requires every private call to carry a nonce that increases
for the key; Tradewind uses millisecond timestamps, strictly increasing within its process.
Two processes on one key (a session and a daily run, or two sessions) would race their
nonces: Kraken answers `EAPI:Invalid nonce`, and repeated invalid nonces can ban the key for a
while. Give each process its own key.

## A session

```python
from tradewind.live import KillSwitch, LiveEngine, LiveFeed, RiskLimits
from tradewind.live.kraken import FractionalCommInfo, KrakenAdapter

SYMBOLS = ["BTC", "ETH"]
limits = RiskLimits(max_order_notional=500, allowed_symbols=frozenset(SYMBOLS), allow_short=False)
adapter = KrakenAdapter(quote="USD", symbols=SYMBOLS, limits=limits,
                        state_path="live/kraken_state.json")      # validation mode
adapter.connect()                       # reads the key from the environment and checks it
source = adapter.bar_source()           # public daily bars, no key
engine = LiveEngine(adapter, source, journal="live/kraken.jsonl", capital=1_000,
                    kill_switch=KillSwitch("live/KILL"), limits=limits)
engine.broker.addcommissioninfo(FractionalCommInfo())             # fractional sizes, 0.40 % fee
for s in SYMBOLS:
    engine.adddata(LiveFeed(dataname=source.history(s)), name=s)  # up to 720 daily bars
engine.addstrategy(MyStrategy)          # the class you backtest
engine.run(connect=False)
```

A session goes through three stages, all through the engine's own loop:

1. **Warm-up.** Each feed's history is stepped exactly as a backtest steps it, so indicators
   and the strategy's state are warm. Orders placed here are refused without being sent.
   Kraken keeps 720 daily bars: a strategy that needs a longer warm-up is given a longer
   history (the catalogue's `Cryptocurrencies-Daily-Price`, stamped the same way).
2. **Replay**, when the journal already holds a session: the journalled slices are stepped
   again with the journalled broker messages; any difference between what the strategy asks
   and what was journalled raises `LiveDivergence` and the session does not start.
3. **Live.** Each daily bar the clock settles is one slice: the broker's messages are
   applied, the bar is written, the strategy steps, and its orders go out at once.

The full runnable script is
[`live_kraken.py`](examples.md#crypto-on-kraken): `--public` reads Kraken's public bars only
(no key, no order), the default runs the account in validation mode, `--live` asks for real
orders and still needs `TRADEWIND_KRAKEN_LIVE=1`.

## Crypto is not a stock market

**A 24/7 market, a decision at 00:00 UTC.** There is no close. A daily bar is stamped at its
00:00 UTC open, as in the catalogue's `Cryptocurrencies-Daily-Price` (Kraken's closes and the
catalogue's agree to a median 0.005-0.02 % on BTC, ETH, SOL and DOGE). The bar of day D is
complete at D+1 00:00 UTC; `KrakenBars` passes it on five minutes later (`grace`) and never
passes the last row of Kraken's answer, the bar still forming. The strategy decides on bar D
and its market orders execute at once, in the first minutes of D+1: the price a backtest
fills at (the next open), give or take those minutes. There is no opening auction: the
market-on-open time in force of a stock session has no meaning and is ignored. The session
trades every day, weekends included.

**Fractional quantities and minimums.** The default commission scheme of the backtest sizes
`order_target_percent` in whole units (0 or 1 BTC). `FractionalCommInfo` sizes to 1e-8 of a
coin (`decimals`), toward zero, and charges `commission` of the traded value; install it on
the broker as above, in the backtest and live alike so both size the same way. Kraken then
cuts each order to its pair's `lot_decimals`, and refuses locally, before any call, an
order under the pair's `ordermin` (base units) or `costmin` (quote units at the reference
price). A remainder under one lot is reported as cancelled once the rest has executed.

**Symbols.** The catalogue's bare tickers (`BTC`, `ETH`, `DOGE`), or `BTCUSD`, `BTC/USD`.
Kraken names some assets otherwise (`XBT`, `XDG`); the pair is found in `AssetPairs`.
Positions come back under the name the strategy uses. 318 of the catalogue's 673 crypto
symbols have a Kraken USD pair.

**The quote currency.** `quote="USD"` (the default) or `"EUR"`. Cash is that currency's
balance; every other asset is a position, in base units, when it has a pair against it, and
a foreign position otherwise; staked and rewards balances (`.S`, `.F`, `.M`...) are ignored.

**Fees in the quote currency.** Every order asks for the fee in the quote currency
(`oflags=fciq`), so a position is exactly the quantity traded and the cash pays the fee. The
fee charged is each trade's own. The local checks assume 0.40 % (`taker_fee`), Kraken's
entry taker fee, until the account's own tier is known.

**No shorting, no leverage.** A sell over the available balance of the coin is refused
locally; a buy whose cost plus the fee exceeds the available quote balance too.

| Tradewind order | Kraken `AddOrder` |
|---|---|
| `Market` | `ordertype=market` |
| `Limit` | `limit`, `price` |
| `Stop` | `stop-loss`, `price` = trigger |
| `StopLimit` | `stop-loss-limit`, `price` = trigger, `price2` = limit |
| `Close` (market-on-close) | refused locally: no closing auction |
| `valid=None` / GTC | `timeinforce=GTC` |
| `valid=date` | `GTD`, `expiretm` (Kraken: at most a month ahead) |
| `valid=0` (DAY) | `GTD` to the next 00:00 UTC |
| every order | `type` from the sign, `volume` cut to the lot, `oflags=fciq`, `cl_ord_id`, `validate=true` unless armed |

## Validation mode, then armed

Until both keys are turned, every order is sent with `validate=true`. Kraken checks it -- the
pair, the volume, the price, the account's permissions -- and answers with its description
without trading it. The engine sees the order `Rejected` with the reason
`validated by Kraken (validate=true), not sent: buy 0.01000000 XBTUSD @ market`, so the
strategy runs end to end, every order is journalled and checked, and nothing trades.

| `live=` | `TRADEWIND_KRAKEN_LIVE` | mode |
|---|---|---|
| `False` (default) | anything | validation |
| `True` | unset, `0`, `true`... | validation, with a notice in the journal |
| `True` | `1` | **armed**: orders trade |

The mode is printed and journalled at `connect` (`adapter.mode`).

## Journal, replay and resume

Every session writes an append-only JSONL journal: a header with each feed's warm-up
history and its digest, every broker message at the slice it was applied, every slice, and
every command **before** it is sent (write-ahead). The strategy is a deterministic function
of the bars and messages it is given, and the journal holds both, so a live session is a
backtest whose feed is the journal.

Kraken knows our orders by a client order id (`cl_ord_id`), derived from Tradewind's own
order id, so a replay sends the same. `state_path` keeps the table from Kraken's ids back to
Tradewind's (no secret in it): give it to every session. Restarting with the same journal
and state file resumes the session: the journal is replayed, then a full snapshot applies
the executions missed while down (`TradesHistory` since a week before `connect`, `since=`),
cancels the orders Kraken no longer lists, and reconciles the wallet and cash.

## Reconciliation and sleeves

At start, the wallet and cash are adopted. Kraken reports no average cost for a spot
balance: a position is adopted at the last trade price (`Ticker`). At every slice a snapshot
is compared with the book; a difference not explained by work in flight is journalled, then
either adopted (`reconcile="adopt"`, the default: a manual trade, a deposit of coins) or
trips the kill switch (`reconcile="halt"`, the setting for real money).

`LiveEngine(capital=...)` runs the strategy as a **sleeve**: its cash starts at `capital`
minus the value of its positions and moves with its own executions only, so a strategy
written for `order_target_percent` of the portfolio trades 1 000 USD of an account that
holds more.

## Guards

- **Validation mode by default** (above): two keys to trade.
- **`RiskLimits`**: maximum quantity and notional per order, maximum position notional,
  maximum orders per slice, allowed symbols (the pair whitelist), no shorting. The adapter
  checks them too, so they hold even outside `LiveEngine`.
- **The balance check**: a buy's cost plus the taker fee against the available quote
  balance, a sell against the available coin balance, before any call.
- **The pair's state**: an order on a pair that is not `online` (`cancel_only`,
  `post_only`...) is refused.
- **Kill switch**: `KillSwitch(path)` trips from code or when the file appears
  (`touch live/KILL`): working orders are cancelled, nothing more is sent, the run ends.
- **Rate limits**: Kraken counts private calls per key (+1 a call, +2 for trade history,
  decaying by 0.33 a second up to 15 on a Starter account; `tier="intermediate"` or `"pro"`
  for 20). The client keeps its own copy of that counter and waits rather than overflow it,
  and paces public calls at one a second. A read answered `EAPI:Rate limit exceeded` or
  `EService:Throttled` is tried again after a back-off; an order never is (a second
  `AddOrder` would be a second order).

## What differs from a backtest

- Fills are Kraken's: price, partial fills, fees. A market order executes in the minutes
  after 00:00 UTC, not at the bar's open price.
- Fees are each trade's own, in the quote currency; the backtest's are its commission
  scheme's (`FractionalCommInfo`: 0.40 %).
- A position is cut to the pair's lot and an order under the pair's minimum is not sent: a
  small sleeve trades less than its backtest.
- Look-ahead reads get NaN (`data.close[1]`) or raise (`datetime.date(1)`).
- `add_cash` raises; `setcash` sets the sleeve's capital.
- Notifications arrive at the next slice, as in a backtest.

## Validation before real

Kraken has no paper account for spot trading. The path is:

1. The tests (`pytest tests/test_live_kraken.py`, recorded answers, no network) and
   `python docs/examples/live_kraken.py --public` (public bars only, no key).
2. The key (no withdrawal permission), **validation mode**, for several weeks: every order
   is checked by Kraken against the real account, the journal shows what would have traded.
3. Real money only after Édouard's sign-off, with a small `capital`, `reconcile="halt"`,
   tight `RiskLimits`, the kill file documented, and both keys: `live=True` and
   `TRADEWIND_KRAKEN_LIVE=1`.

Reference: [Live trading](reference/live.md).
