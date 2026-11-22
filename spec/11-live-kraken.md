# 11. Live trading, second broker: Kraken (crypto spot)

**Status: proposal with a working implementation behind it (2026-11-22), to be validated by
Édouard (§ 12).** Édouard agreed on 2026-11-22 to integrate Kraken the way Interactive Brokers
was integrated (spec/07). This note maps Kraken's spot API onto the seam spec/07 already
defines -- nothing in `LiveEngine`, `LiveBroker`, the journal or the guards changes -- and
fixes what is specific to crypto: no close, fractional quantities, per-pair minimums, two
quote currencies, a multi-asset wallet.

Sources: Kraken's official API documentation at docs.kraken.com only (spot REST: the
authentication guide and its worked signature example, the rate-limit guide, `AssetPairs`,
`OHLC`, `Ticker`, `BalanceEx`, `AddOrder`, `CancelOrder`, `OpenOrders`, `QueryOrders`,
`QueryTrades`, `TradesHistory`; WebSocket v2: the `executions` channel); spec/07; the PWB
dataset `Cryptocurrencies-Daily-Price` read locally (symbols and date convention). No
client library: the adapter speaks REST with the standard library (`urllib`, `hmac`,
`hashlib`), so the `kraken` path adds no dependency.

## 1. What is in, what is not

| in | not yet |
|---|---|
| spot pairs against USD or EUR, one quote currency per session | margin, futures, staking, transfers, withdrawals (never) |
| market, limit, stop-loss, stop-loss-limit; GTC, GTD, "DAY" | trailing stops, iceberg, conditional close, amend |
| `KrakenAdapter` (a `BrokerAdapter`) for `LiveEngine`; `KrakenBars` (a `BarSource`) | WebSocket v2 streaming (REST polling suffices for daily bars, § 6) |
| `KrakenAccount` for the daily runner (`--broker kraken`) | a 7-day schedule for the daily runner (it runs on NYSE weekdays, § 3) |
| fresh crypto closes from Kraken's public OHLC (`--fresh-prices kraken`) | intraday bars |
| **validation mode by default**: `AddOrder` with `validate=true` | -- |

## 2. Correspondence with the broker seam (spec/07 § 3)

| seam | Kraken |
|---|---|
| `connect` | credentials from the environment (§ 7), one `BalanceEx` call to check them, `AssetPairs` loaded |
| `place(OrderRequest)` | `AddOrder` (§ 4); a synchronous `error` is a `Rejected`; a `txid` is an `Ack`; in validation mode Kraken's answer (no `txid`) is a `Rejected` whose reason starts with `validated` |
| `cancel(client_id)` | `CancelOrder` with the order's `txid` |
| `snapshot` | `BalanceEx` (cash = the quote currency's balance; positions = every other asset's balance, in base units), `OpenOrders` (ours: those carrying our `cl_ord_id`), and on a full sync `TradesHistory` since the session started (missed executions) |
| `drain` / `idle` | while one of our orders is working, `QueryOrders` (+ `QueryTrades` for new fills) at most every `poll_every` seconds (10 by default) |
| order id | Kraken's `cl_ord_id` (32 hex characters = an MD5 of Tradewind's `client_id`: deterministic, so a replay gives the same id); the `cl_ord_id` -> `client_id` table is kept in an optional JSON state file for a restart (no secret in it) |
| status | `open` -> `Ack`; each trade -> `Execution` (signed `vol`, `price`, `fee`, `time`; deduplicated on the trade id); `canceled` -> `Canceled`, or `Rejected` when nothing ever executed and nobody asked; `expired` -> `Canceled(expired=True)`; `closed` -> nothing (the executions carry it) |
| commission | the trade's `fee`, in the quote currency: every order is sent with `oflags=fciq` (fee in quote), so a position in base units is exactly what was bought or sold |
| average cost | Kraken reports none for spot balances: positions are adopted at the last trade price (`Ticker` `c`), which is what a sleeve's starting cash is computed from |

The engine never sees Kraken: `LiveBroker`, its reconciliation, the journal and the replay
are spec/07's, unchanged.

## 3. Crypto specifics

**No close; the daily decision is at 00:00 UTC.** Kraken's daily OHLC bar (interval 1440)
is stamped at its 00:00 UTC open; PWB's `Cryptocurrencies-Daily-Price` follows the same
day (a bar's open is the previous bar's close). A bar dated D is complete at D+1 00:00 UTC;
`KrakenBars` passes it on after a grace (5 minutes) and never passes the last entry of an
`OHLC` answer, which Kraken documents as the still-forming one. A market order placed on
bar D's close executes at once, in the first minutes of D+1: the price the backtest fills
at (the next open) give or take those minutes. `market_tif` (`OPG`) has no meaning here and
is ignored for a market order. Measured on 2026-11-22 (public `OHLC`, no key): over the
last 60 common days, Kraken's daily closes of BTC, ETH, SOL and DOGE differ from the
catalogue's by a median 0.005-0.02 % -- the same 00:00 UTC day; the largest gaps (2-3 %) are
the catalogue's last, partial bar, which the fresh-price splice already replaces. 318 of the
catalogue's 673 crypto symbols have a Kraken USD pair. The daily runner keeps its NYSE weekday schedule: run after
the US close, the crypto bar of the same date is already complete (00:00 UTC is 19:00 or
20:00 New York); weekends are not traded until a 7-day schedule is decided (§ 12, Q3).

**Fractional quantities and per-pair minimums** (`AssetPairs`): a volume is cut toward zero
to `lot_decimals`; an order under `ordermin` (base units) or under `costmin` (quote units, at
the reference price) is refused locally, before any call; prices are rounded to
`pair_decimals`. A pair whose `status` is not `online` refuses every order.

**Symbols.** The catalogue's crypto symbols are bare base tickers (`BTC`, `ETH`, `DOGE`;
`BTCUSD` is accepted too). Kraken names some assets differently: `BTC` is `XBT`, `DOGE` is
`XDG`. A symbol resolves to the `AssetPairs` entry whose `wsname` is `<base>/<quote>`
(`XBT/USD`), after the aliases. A position or execution comes back under the name the
strategy used. Staked or rewards balances (`.S`, `.F`, `.M`, `.B`, `.T`) are not tradable and
are listed as ignored, never as positions.

**EUR and USD.** A session has one quote currency (`quote="USD"` or `"EUR"`). The books of
the catalogue are in USD: in the daily runner an EUR account is an account whose currency
is EUR (spec of the runner: the rate comes from PWB's EURUSD or `--base-fx-rate`), and the
order's quantity in base units is the same whichever quote it is sent against. Positions in
assets with no pair against the quote are listed as foreign.

**Multi-asset wallet.** Cash is the quote currency's balance only; any other fiat balance
is a foreign position. "Available" is `balance - hold_trade` (`BalanceEx`).

**Fees.** Taken from each trade. `AssetPairs`' `fees` table is deprecated (always empty
since 2026-09-08), so the pre-check uses a configurable taker rate (`taker_fee`, 0.40 % by
default, the documented entry tier; to set from the account's own schedule).

**No shorting.** Spot only: a sell larger than the available base balance is refused.

## 4. Order mapping, request -> `AddOrder`

| request | `AddOrder` |
|---|---|
| `MKT`, any tif | `ordertype=market` |
| `LMT` | `ordertype=limit`, `price` |
| `STP` | `ordertype=stop-loss`, `price` = trigger |
| `STP LMT` | `ordertype=stop-loss-limit`, `price` = trigger, `price2` = limit |
| `MOC` | refused locally (no closing auction) |
| tif `GTC` / `OPG` | `timeinforce=GTC` |
| tif `GTD` | `timeinforce=GTD`, `expiretm` = the Unix second (Kraken: at most a month ahead) |
| tif `DAY` | `timeinforce=GTD`, `expiretm` = the next 00:00 UTC |
| every order | `type` buy/sell from the sign, `volume` = abs, `oflags=fciq`, `cl_ord_id`, `validate=true` unless armed (§ 5) |

## 5. Validation mode (the default) and arming

Every `AddOrder` carries `validate=true` -- Kraken checks the order and does not trade it --
unless **both** `live=True` is passed to the adapter (or `--kraken-live` to the daily
runner) **and** the environment has `TRADEWIND_KRAKEN_LIVE=1`. One without the other stays
in validation mode, says so in a `Notice`, and journals it. A validated order comes back as
`Rejected("validated by Kraken (validate=true), not sent: <Kraken's description>")`, so a
strategy runs end to end, every order is checked by Kraken against the account, and
nothing trades. The guards stay on in both modes: `RiskLimits` (`allowed_symbols` as the
pair whitelist, `max_order_notional`, `max_position_notional`, `allow_short=False`), the
kill switch, and a local balance check (a buy's cost plus the taker fee must be covered by
the available quote balance, a sell by the available base balance), all before any call.

## 6. Market data, rate limits, nonce

**Public data, no key.** `KrakenBars` and the daily runner's fresh closes use `OHLC`
(interval 1440, the 720 most recent days) and `Ticker`. At most one public call a second.

**Private counter.** Kraken counts private REST calls per key: +1 per call, +2 for ledger
and trade history calls, decaying at 0.33 a second (Starter tier, maximum 15), 0.5
(Intermediate, 20) or 1 (Pro, 20); `AddOrder` and `CancelOrder` are counted by the
matching engine instead. `KrakenREST` keeps its own copy of that counter (`tier`, Starter by
default) and waits before a call would overflow it. `EAPI:Rate limit exceeded` and
`EService:Throttled` are retried with back-off on read calls only; `AddOrder` is never
retried (a duplicate would be a second order).

**Nonce.** A millisecond Unix timestamp, strictly increasing within the process (+1 when two
calls fall in the same millisecond). One process per key: two processes on one key would
race their nonces (Kraken: `EAPI:Invalid nonce`; repeated invalid nonces can ban the key
temporarily). Use a separate key per process.

**Signature.** `API-Sign = base64(HMAC-SHA512(base64decode(secret), uri_path +
SHA256(nonce + postdata)))`, `API-Key` the public key, the body form-encoded. The test suite
reproduces the documentation's worked example to the character.

**WebSocket v2** (`wss://ws-auth.kraken.com/v2`, channel `executions`, a token from
`GetWebSocketsToken`) would push order and fill events instead of polling. Not needed for
daily bars; the seam allows it later without touching the engine.

## 7. Secrets

The key and secret are read **only** from `KRAKEN_API_KEY` and `KRAKEN_API_SECRET`, at
`connect`, never from a file. They are never written (journal, state file, logs, proposals),
never printed: `repr` of the credentials is redacted, and every error message the client
raises is scrubbed of both strings. A test runs a session with a sentinel key and checks
that it appears in no journal line, no log, no state file and no exception.

**The key to create** (Kraken > Settings > API): *Query Funds*, *Query Open Orders &
Trades*, *Query Closed Orders & Trades*, *Create & Modify Orders*, *Cancel/Close Orders*.
**Never** *Withdraw Funds*, *Deposit*, *Export Data* or *Access WebSockets API* (not used).
Optional: restrict the key to the machine's IP address.

## 8. Daily runner (`live/daily`)

`KrakenAccount` answers the runner's four calls like `IBPaperAccount`: `settle` (the trades
since the previous session, from `TradesHistory`), `snapshot` (cash in the quote currency,
positions in the books' symbols, NAV at the books' closes), `place` (the day's orders,
market, validation mode unless armed), `close`. `send=False` (propose) never calls
`AddOrder`. `--fresh-prices kraken` completes the crypto symbols of
`Cryptocurrencies-Daily-Price` up to the session with Kraken's daily bars, asked before the other
fresh-price sources listed (`kraken_prices.py`, a per-session cache like
`ib_prices.py`: `CACHE/SESSION/KRAKEN_OHLC/SYMBOL.json`).

## 9. Paper before real

Kraken has no paper account for spot. The path is: (1) the tests (recorded answers, no
network); (2) the public-data test and `docs/examples/live_kraken.py --public` (bars only);
(3) the key, validation mode: a few weeks of daily runs where every order is checked by
Kraken and nothing trades; (4) armed, with a small capital, tight `RiskLimits`,
`reconcile="halt"`, after Édouard's sign-off.

## 10. Tests (`tests/test_live_kraken.py`, no network by default)

The documentation's signature example; nonce monotonicity; the private counter; parsing of
the documented `AssetPairs`, `OHLC`, `Ticker`, `BalanceEx`, `QueryOrders`, `TradesHistory`
answers (fixtures in `tests/fixtures/kraken/`); symbol resolution (`BTC` -> `XBT/USD`);
volume rounding and minimums; order mapping; validation mode by default and the two-key
arming; guards (whitelist, notional, balance, no short); executions and statuses from polls;
a `LiveEngine` session against a recorded-answer transport; `KrakenBars` never passing a
forming bar; the daily account; the fresh-price cache; the secret in no output. One network
test (`-m network`, skipped by default) calls public endpoints only.

## 11. Clean-room note

Written from Kraken's public documentation and spec/07; no third-party Kraken client was
read or installed. Logged in `CLEANROOM.md`.

## 12. Decisions (Édouard, 2026-11-22) and what is still to do

1. **Quote currency: USD** by default (`quote="USD"`, `--kraken-quote USD`); EUR stays
   available.
2. **Fees: 0.40 %** taker by default (`taker_fee`, `--kraken-taker-fee`,
   `FractionalCommInfo`) until the account's real tier is known.
3. **Weekends: the daily runner trades the NYSE weekdays for now**, documented as such
   (`docs/live-kraken-daily.md`). `LiveEngine` with `KrakenBars` already trades every day.
4. **Fractional sizing**: `FractionalCommInfo` (live/kraken.py) for `LiveEngine` and the
   backtest; `lot: 0` in a crypto meta-portfolio.
5. **Which sleeves**: long-only catalogue strategies on `Cryptocurrencies-Daily-Price` whose
   symbols have a Kraken pair; the meta-portfolio is still to be chosen.

**To do before real money** (open):

- [ ] **A seven-day calendar for the daily runner**, required for any crypto strategy
  measured on seven days: a session every day, decided on the 00:00 UTC bar (`CRYPTO` of
  `calendars.py` in place of `NYSE` in `session.closed_session`, `runner.last_session`,
  the send cut-off and the scheduling examples), the books, the tracking and the
  reconciliation run on calendar days. Until then the account trades the weekend's moves on
  Monday night and the tracking gap carries them (timing term).
- [ ] The first run with a key, validation mode: record what Kraken answers to
  `validate=true` (assumed: a description, no `txid`), that it accepts the 18-character
  `cl_ord_id`, and the form of `descr.pair` in `OpenOrders`.
- [ ] The account's fee tier, in place of 0.40 %.
- [ ] Several weeks of validation mode, then Édouard's sign-off, then both keys with small
  limits and `reconcile="halt"`.
