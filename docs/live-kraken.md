# Live trading on Kraken (crypto spot)

!!! warning "Preview, validation mode by default"
    The Kraken adapter sends every order to `AddOrder` with `validate=true`: Kraken checks it
    against the account and **does not trade it**. Orders trade only with two keys turned:
    `live=True` (or `--kraken-live`) **and** `TRADEWIND_KRAKEN_LIVE=1` in the environment.
    The design document is
    [`spec/11-live-kraken.md`](https://github.com/paperswithbacktest/tradewind/blob/main/spec/11-live-kraken.md).

The adapter plugs into the same seam as Interactive Brokers ([live trading](live-trading.md)):
`LiveEngine`, `LiveBroker`, the journal, the replay, the guards and the kill switch are the
same. It speaks Kraken's REST API with the standard library: nothing to install.

## The key

Create it in Kraken under *Settings > API* with these permissions only:

| permission | why |
|---|---|
| Query Funds | balances (`BalanceEx`) |
| Query Open Orders & Trades | working orders, fills |
| Query Closed Orders & Trades | fills after a restart (`TradesHistory`) |
| Create & Modify Orders | `AddOrder` (also needed for `validate=true`) |
| Cancel/Close Orders | `CancelOrder`, the kill switch |
| **Withdraw Funds** | **never** |

Export it in the shell that runs the session -- never in a file of a repository:

```bash
export KRAKEN_API_KEY=...        # the public key
export KRAKEN_API_SECRET=...     # the private key, base64, as Kraken shows it
```

Tradewind reads these two variables only, at `connect`. The key is never written to the
journal, the state file, a proposal or a log; error messages are scrubbed of it. Use one key
per process: Kraken's nonce must increase per key, and two processes sharing a key would
race.

## A session

```python
from tradewind.live import KillSwitch, LiveEngine, LiveFeed, RiskLimits
from tradewind.live.kraken import KrakenAdapter

limits = RiskLimits(max_order_notional=500, allowed_symbols=frozenset({"BTC", "ETH"}), allow_short=False)
adapter = KrakenAdapter(quote="USD", symbols=["BTC", "ETH"], limits=limits,
                        state_path="live/kraken_state.json")      # validation mode
adapter.connect()
source = adapter.bar_source()                                    # public daily bars, 00:00 UTC
engine = LiveEngine(adapter, source, journal="live/kraken.jsonl", capital=1_000,
                    kill_switch=KillSwitch("live/KILL"), limits=limits)
engine.broker.addcommissioninfo(Fractional())                    # fractional sizes (see the example)
for s in ("BTC", "ETH"):
    engine.adddata(LiveFeed(dataname=source.history(s)), name=s)
engine.addstrategy(MyStrategy)
engine.run(connect=False)
```

The runnable script is [`docs/examples/live_kraken.py`](https://github.com/paperswithbacktest/tradewind/blob/main/docs/examples/live_kraken.py):
`--public` reads Kraken's public bars only (no key), the default runs the account in
validation mode, `--live` asks for real orders (and still needs `TRADEWIND_KRAKEN_LIVE=1`).

## What is specific to crypto

* **No close.** A daily bar is stamped at its 00:00 UTC open, as in the catalogue's
  `Cryptocurrencies-Daily-Price`; the bar of day D is complete at D+1 00:00 UTC and is used
  five minutes later. The last row of Kraken's answer (the forming bar) is never used. A
  market order executes at once, in the first minutes of the next day.
* **Fractional sizes.** Each pair's `lot_decimals` cuts the volume toward zero; an order under
  `ordermin` or `costmin` is refused before any call. A strategy needs fractional sizing
  (the example's `Fractional` scheme): the default stock scheme sizes in whole units.
* **Symbols.** The catalogue's bare tickers (`BTC`, `ETH`, `DOGE`) or `BTCUSD`; Kraken's
  `XBT` and `XDG` are resolved through `AssetPairs`.
* **Quote currency.** `quote="USD"` or `"EUR"`: cash is that currency's balance; other assets
  are positions (in base units) when they have a pair against it, foreign otherwise; staked
  and rewards balances are ignored.
* **Fees.** Every order asks for the fee in the quote currency (`oflags=fciq`), so a position
  is exactly what was traded; the fee charged is each trade's own.
* **No shorting.** A sell over the available balance is refused locally.

| request | Kraken `AddOrder` |
|---|---|
| market | `market` |
| limit | `limit`, `price` |
| stop | `stop-loss`, `price` = trigger |
| stop-limit | `stop-loss-limit`, `price` = trigger, `price2` = limit |
| market-on-close | refused (no closing auction) |
| GTC / GTD / DAY | `GTC` / `GTD` + `expiretm` / `GTD` to the next 00:00 UTC |

## The daily run

The daily runner of a meta-portfolio ([live-paper.md](live-paper.md)) takes a Kraken account:

```bash
# propose only: reads the account, never calls AddOrder
python -m tradewind.live.daily --meta crypto.yaml --broker kraken --kraken-quote EUR \
    --fresh-prices kraken --lot 0.00000001
# orders checked by Kraken (validate=true), nothing trades
python -m tradewind.live.daily --meta crypto.yaml --broker kraken --send --kraken-allowed BTC,ETH \
    --kraken-max-order 500 --fresh-prices kraken --lot 0.00000001
```

`--fresh-prices kraken` completes the catalogue's crypto symbols up to the session with
Kraken's public daily bars (no key). `--lot` must be fractional for crypto. The runner keeps
its US weekday schedule; weekend days are traded at the next weekday run.

## Before a first real order

1. The tests and `python live_kraken.py --public`.
2. The key (no withdrawal), validation mode, for a few weeks: every order is checked by
   Kraken, the journal and the proposals show what would have traded.
3. Édouard's sign-off; then a small `capital`, tight `RiskLimits`, `reconcile="halt"`, and both
   keys: `--live` / `live=True` and `TRADEWIND_KRAKEN_LIVE=1`.
