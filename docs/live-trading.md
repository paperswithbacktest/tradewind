# Live trading on Interactive Brokers

!!! warning "Preview"
    `tradewind.live` is a preview: paper accounts first, daily bars, US stocks and ETFs. The
    interface may still change. The design document is
    [`spec/07-live-ibkr.md`](https://github.com/paperswithbacktest/tradewind/blob/main/spec/07-live-ibkr.md).

The same strategy class that backtests runs live, without a line changed: `LiveEngine` is
the backtest engine (the same sixteen phases of a slice) driven by a live clock, with a
broker that sends orders to an account instead of simulating them.

```bash
pip install 'tradewind[ibkr]'      # adds ib_async; the backtest never imports tradewind.live
```

## What step 1 covers

| in | not yet |
|---|---|
| daily bars, US stocks and ETFs, one strategy per session | intraday bars, futures, forex, options, multi-currency accounts |
| market, limit, stop, stop-limit, market-on-close; DAY / GTC / GTD | trailing stops, brackets / OCO, order modification |
| executions, partial fills, cancellations, expiries, rejections, disconnections | corporate actions from the broker (splits reconcile as position mismatches) |
| reconciliation of positions and cash at start and at every slice | margin and buying power read from the account |
| journal, replay, resume after a stop or a crash | several strategies sharing one account (a sleeve per session is supported) |
| paper-only by default, read-only mode, risk limits, kill switch, IB pacing | a hosted, supervised deployment |

## A session

```python
from tradewind.live import IBAdapter, IBBars, KillSwitch, LiveEngine, LiveFeed, RiskLimits

adapter = IBAdapter("127.0.0.1", 7497, client_id=17, readonly=True)
adapter.connect()                       # refuses a live port or a non-paper account
source = IBBars(adapter)
engine = LiveEngine(adapter, source, journal="live/paper.jsonl", read_only=True,
                    capital=100_000, kill_switch=KillSwitch("live/KILL"),
                    limits=RiskLimits(max_order_notional=60_000,
                                      allowed_symbols=frozenset(SYMBOLS), allow_short=False))
for s in SYMBOLS:
    engine.adddata(LiveFeed(dataname=source.history(s, "2 Y")), name=s)
engine.addstrategy(MyStrategy)          # the class you backtest
engine.run(connect=False)
```

A session goes through three stages, all through the engine's own loop:

1. **Warm-up.** Each feed's history is stepped exactly as a backtest steps it, so indicators
   and the strategy's state are warm. Orders placed here are refused without being sent.
2. **Replay**, when the journal already holds a session: the journalled slices are stepped
   again with the journalled broker messages; any difference between what the strategy asks
   and what was journalled raises `LiveDivergence` and the session does not start.
3. **Live.** Each bar the clock settles is one slice: the broker's messages are applied, the
   bar is written, the strategy steps, and its orders go out at once.

The full runnable script, with a simulated venue (`--fake`, no account, no network), is
[`live_ibkr_paper.py`](examples.md#paper-trading-on-interactive-brokers).

## Journal, replay and resume

Every session writes an append-only JSONL journal: a header with each feed's warm-up
history and its digest, every broker message at the slice it was applied, every slice, and
every command **before** it is sent (write-ahead). The strategy is a deterministic function
of the bars and messages it is given, and the journal holds both, so a live session is a
backtest whose feed is the journal: any divergence between a session and its replay is a bug
with a reproducer.

Restarting with the same journal resumes the session: the journal is replayed, then a full
account snapshot applies the executions missed while down, cancels orders the broker no
longer lists, and reconciles positions and cash.

## Reconciliation and sleeves

At start, the account's positions and cash are adopted. At every slice a snapshot is
compared with the book; a difference not explained by work in flight is journalled, then
either adopted (`reconcile="adopt"`, the default: a manual trade, a split) or trips the kill
switch (`reconcile="halt"`, the setting for real money).

`LiveEngine(capital=...)` runs the strategy as a **sleeve**: its cash starts at `capital`
and moves with its own executions only, so a strategy written for `order_target_percent`
of the portfolio trades 100 000 on an account that holds more.

## Guards

- **Paper by default**: `IBAdapter` refuses the live ports (TWS 7496, Gateway 4001) and any
  account that is not a paper account. Going live takes two deliberate changes.
- **Read-only**: `LiveEngine(read_only=True)` refuses every order locally and journals it;
  `IBAdapter(readonly=True)` connects with the API's read-only flag.
- **`RiskLimits`**: maximum quantity and notional per order, maximum position notional,
  maximum orders per slice, allowed symbols, no shorting.
- **Kill switch**: `KillSwitch(path)` trips from code or when the file appears
  (`touch live/KILL`): working orders are cancelled, nothing more is sent, the run ends.
- **Market-on-open by default**: a market order placed on a daily close executes in the next
  opening auction, the price a backtest fills at.
- **Pacing**: IB's documented message and historical-data limits are enforced by the adapter.

## What differs from a backtest

- Fills are the venue's: price, partial fills, commission. With market-on-open and the same
  bars, the simulated venue reproduces the backtest's orders and value (a test checks it).
- Look-ahead reads get NaN (`data.close[1]`) or raise (`datetime.date(1)`): a strategy that
  spots month ends with `date(1)` must change before going live.
- `add_cash` raises; `setcash` sets the sleeve's capital.
- Notifications arrive at the next slice, as in a backtest.

## Paper before real

1. `python docs/examples/live_ibkr_paper.py --fake`: the whole path against a simulated
   venue.
2. A paper account, **read-only**, for a few days: orders computed and journalled, nothing
   sent.
3. A paper account, trading (`--trade`), for several weeks, compared each month with the
   backtest of the same dates.
4. Real money only with a small `capital`, `reconcile="halt"`, tight `RiskLimits` and the
   kill file documented.

Setting up TWS or IB Gateway for the API (socket port, read-only flag, paper login) is
described in the example's docstring. Reference: [Live trading](reference/live.md).
