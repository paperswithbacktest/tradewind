# 07. Live trading, first broker: Interactive Brokers (chantier 7, step 1)

**Status: proposal with a working implementation behind it (2026-11-15), to be validated by
Édouard (§ 15).** Édouard decided on 2026-11-15 that the first live broker is Interactive
Brokers. This document fixes how a Tradewind strategy runs against a broker account without
changing a line of the strategy, and what step 1 delivers: `LiveEngine`, `LiveBroker`,
`LiveClock`, `LiveFeed`, the append-only journal and its replay, the `BrokerAdapter` seam with
two adapters -- `IBAdapter` (TWS / IB Gateway through ib_async) and `FakeIB` (a simulated
venue every test runs against) -- the guards, and an example to run on a paper account.

Sources: `05-interfaces.md` § 6.3 (bus), § 6.4 (feeds), § 6.5 (broker), § 8 (live in one
paragraph), § 10 Q4 (indicators live); Interactive Brokers' public TWS API documentation
(order types and times in force, order statuses, message codes, pacing limits, paper
accounts); ib_async's public interface (its installed package was read for names and
signatures only). QuantConnect Lean (Apache 2.0, `NOTICE`) is followed for two concepts:
a brokerage behind one interface that the algorithm never sees, and a warm-up during which
orders are refused. backtrader's code, its IB store included, is not a source
(`CLEANROOM.md`).

## 1. Step 1: what is in, what is not

| in | not yet |
|---|---|
| daily bars, US stocks and ETFs (`Stock(symbol, "SMART", "USD")`), one strategy per session | intraday bars, futures, forex, options, multi-currency accounts |
| market, limit, stop, stop-limit, market-on-close; `valid` as DAY / GTC / GTD | trailing stops, brackets / OCO, order modification |
| executions, partial fills, cancellations, expiries, rejections, disconnections | corporate actions from the broker (splits are reconciled as position mismatches) |
| reconciliation of positions and cash at start and at every slice | margin and buying power read from the account (the local pre-check stays the gate) |
| journal, replay, resume after a stop or a crash | several strategies sharing one account (a sleeve per session is supported, § 7.3) |
| paper-only by default, read-only mode, risk limits, kill switch, IB pacing | a hosted, supervised deployment (§ 15, Q5) |

Nothing in `tradewind/live/` is imported by the backtest engine (`import tradewind` does not
load it; a test checks), and ib_async is an optional extra: `pip install 'tradewind[ibkr]'`.
The oracle NAVs are unchanged to the bit (`tools/run_oracle.py`, compared before and after).

## 2. Architecture

```
LiveEngine (Engine subclass: the same sixteen phases of spec/05 § 3)
 ├── LiveFeed ×N ──── PandasData + reserved rows; a closed bar is written into the next row
 ├── LiveClock ────── polls a BarSource, emits one slice per settled bar time
 ├── LiveBroker ───── BackBroker facade (getcash, getvalue, getposition, buy/sell/cancel,
 │     │              notifications); orders out at once, the venue's answers in at phase 5
 │     └── BrokerAdapter ── IBAdapter (ib_async → TWS / IB Gateway) | FakeIB (simulated)
 ├── Journal ──────── append-only JSONL: session header, venue messages, slices, commands
 └── Guards ───────── RiskLimits, KillSwitch, read-only, paper-only, RateLimiter
```

A session goes through three stages, every one of them through `Engine._slice`:

1. **Warm-up.** The history of each `LiveFeed` is stepped exactly as a backtest steps it, so
   indicators and the strategy's own state are warm. Orders placed here are refused without
   being sent or notified (`order.info["tw_reason"] == "warm-up"`), as Lean refuses orders
   during its warm-up.
2. **Replay** (only when the journal already holds a session). The journalled slices are
   stepped again with the journalled venue messages applied where they were; every command
   the strategy asks for is compared with the journalled one instead of being sent. Any
   difference raises `LiveDivergence` and the session does not start.
3. **Live.** The adapter connects, a full account snapshot is queued (reconciliation, § 7),
   then each bar time the clock settles is one slice: the venue's queued messages are
   journalled and applied (phase 5), the bars written and the cursors moved (phase 2), the
   strategy steps (phase 13) and the orders it places go out immediately.

The engine stays synchronous and single-threaded (`05` § 8): the adapter's library runs its
event loop inside `adapter.idle(seconds)`, the only place the engine waits, so every broker
callback lands between two slices and becomes a queued message.

`LiveEngine` prepares a run through the engine's own path (`Engine._tw_open_run`: feeds,
market state, strategies, the phase-10 indicator list, the bus) with its warm-up clock in
place of the backtest's, and closes it through `Engine._tw_stop_run`. One difference: a live
run always publishes its order and trade notifications (the backtest skips building them
when no strategy listens), so an operator's subscriber or a later `notify_order` never
misses one; the book itself never depends on them.

## 3. The adapter seam (`live/adapter.py`, `live/messages.py`)

```python
class BrokerAdapter(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def place(self, request: OrderRequest) -> None: ...   # never raises for a refusal: queues Rejected
    def cancel(self, client_id: str) -> None: ...
    def snapshot(self, full_sync: bool = False) -> AccountSnapshot: ...
    def drain(self) -> list: ...                          # queued messages, oldest first
    def idle(self, seconds: float) -> None: ...           # wait, running the venue's I/O
    def now(self) -> int: ...                             # wall clock, ns UTC

class BarSource(Protocol):
    def poll(self, symbol: str, after: int, now: int) -> Sequence[BarRecord]: ...  # complete bars only
```

An adapter never touches an `Order`, a position or the cash: it translates. Engine → adapter:
`OrderRequest(client_id, symbol, quantity, order_type, limit_price, stop_price, tif,
good_till)`, and `cancel(client_id)`. Adapter → engine, queued: `Ack`, `Execution(client_id,
exec_id, symbol, quantity, price, commission, time)`, `Canceled(reason, expired)`,
`Rejected(reason, code)`, `Connection(connected, code)`, `AccountSnapshot(cash,
net_liquidation, positions, open_orders, executions, full_sync)`, `Notice`, `Kill`. All are
frozen records of plain values, written to and read from the journal without loss (floats
through `repr`).

`client_id` is the engine's order id, `tw-<session>-<n>`: the session id comes from the
journal header and `n` counts the orders the strategy sent, so a replay gives every order
the id it had. IB carries it in the order's `orderRef`, which TWS keeps and reports on
executions, open orders and after a reconnect: it is what ties an IB order to a Tradewind
order across processes.

## 4. Clock, feeds, indicators

**`LiveFeed`** is a `PandasData` built from a warm-up history plus `capacity` empty rows
(default 5 000: twenty years of daily bars). `append(time, o, h, l, c, v)` writes a closed bar
into the next row of the very arrays `MarketState` and the strategy read (their line
buffers are views on one array, `05` § 11), and phase 2 then moves the cursor as in a
backtest. Nothing is reallocated during a session; a full feed raises `CapacityError` and the
session resumes from its journal. A live bar's float day is computed with `stamps2num`'s
arithmetic, so a date has the same bits live and in a backtest.

**`LiveClock`** polls the `BarSource` every `poll_interval` seconds (60 by default) and emits a
bar time once it is settled: every feed delivered it; or every feed missing it has already
delivered a later bar (its exchange was closed); or `assemble_timeout` (600 s) passed since the
first bar of that time arrived. Times are emitted in increasing order; a bar arriving for a
time already emitted is kept in `late` and never replayed into the past. When several times
settle at once (a restart after a gap), all but the last are **stale**: the strategy steps
through them so its state is right, but no order placed on a stale slice is sent
(`tw_reason == "catch-up"`), since it would trade yesterday's decision at today's prices.

**Daily bar completeness (`IBBars`).** IB returns the bar still forming during the session.
A bar dated D is passed on only once `16:00 America/New_York + grace` (15 min) of D has
passed. No request is made while no new bar can be complete; while one is due, one request
per symbol at most every `retry` minutes (15), under IB's pacing (§ 9). Exchange holidays
are not modelled: on one, the bar simply does not come and the clock waits.

**Indicators.** A live engine runs with `runonce=False` (spec/05 Q4: live steps bar by
bar): next-only indicators step at phase 10. Vectorised indicators (`once`) are recomputed
over the grown buffer after each new bar: `once` is a pure, causal function of its inputs,
so the rows up to the new bar get exactly the values a backtest over the same bars gives.
The cost is O(rows) per indicator per bar, nothing for daily bars.

## 5. `LiveBroker` (`live/broker.py`)

`LiveBroker` subclasses the simulated broker and keeps its facade, its position book, its
trade bookkeeping and its notifications; it replaces where the truth comes from.

`submit` (strategy, phase 13): `Submitted` is notified as in a backtest; then, in order,
the kill switch, read-only mode, the connection state and the `RiskLimits` (§ 10) may refuse
the order (`Rejected`, reason in `order.info["tw_reason"]`, journalled as `guard`); then the
security's buying-power model pre-checks it against a running cash for the slice (the
batch rule of `02-semantics`; `Margin` if refused); then the request is journalled (`cmd`)
and sent. The venue's messages change orders, positions and cash only at phase 5 of the next
slice; phase 6 does nothing live (the venue is the fill model).

### 5.1 Order mapping, Tradewind → generic request → IB

| Tradewind | request | IB `Order` |
|---|---|---|
| `Market`, `valid=None` | `MKT`, tif `market_tif` (`OPG` by default) | `orderType="MKT"`, `tif="OPG"`: market-on-open, the next open a backtest fills at |
| `Market` with `valid` | `MKT`, tif from `valid` | `tif` `DAY` / `GTD` |
| `Limit` | `LMT`, `limit_price=price` | `lmtPrice` |
| `Stop` | `STP`, `stop_price=price` | `auxPrice` |
| `StopLimit` | `STP LMT`, `stop_price=price`, `limit_price=pricelimit` | `auxPrice`, `lmtPrice` |
| `Close` | `MOC`, tif `DAY` | `orderType="MOC"` |
| `valid=None` (non-market) | `GTC` | `tif="GTC"` |
| `valid=0` / `timedelta(0)` | `DAY` | `tif="DAY"` |
| `valid=date / datetime / float day` | `GTD`, `good_till` | `goodTillDate="YYYYMMDD HH:MM:SS UTC"` |
| `StopTrail`, `StopTrailLimit`, `Historical`, relative `timedelta` | none: refused locally | -- |
| every order | `client_id` | `orderRef`, `outsideRth=False`, `transmit=True`, `account` |

### 5.2 Status mapping, IB → message → Tradewind

| IB order status (ib_async `OrderStatus`) | message | Tradewind |
|---|---|---|
| `PendingSubmit`, `ApiPending`, `PendingCancel` | none | unchanged (`Submitted` / `Accepted`) |
| `PreSubmitted`, `Submitted` (first time) | `Ack` | `Accepted` |
| execution + commission report | `Execution` | `Partial`, then `Completed` when the executed size reaches the order's |
| `Filled` | none (the executions carry it) | -- |
| `Cancelled` / `ApiCancelled` before any acknowledgement | `Rejected(code)` (ib_async turns an order error into `Cancelled`, the code in the trade's log) | `Rejected` |
| `Cancelled` after a cancel the strategy asked for | `Canceled` | `Canceled` |
| `Cancelled` unasked, tif DAY / OPG / GTD | `Canceled(expired=True)` | `Expired` |
| `Inactive` | `Rejected` | `Rejected` |
| `ValidationError` (a warning on a live order) | `Notice` | unchanged |
| local guard or not connected | -- | `Rejected` |
| local cash pre-check | -- | `Margin` |

An execution is applied once (deduplicated on `exec_id`: IB repeats executions on
`reqExecutions` and after a reconnect). Its commission is the one IB reports (the fee model
only prices the pre-check); an execution whose commission report does not come within 10 s
is passed on with zero commission and a `Notice`. An execution arriving after its order's
final status moves the book but not the status. The cash moves by `-quantity × price -
commission` and is then replaced by the account's figure at reconciliation (§ 7).

## 6. Journal and replay (spec/05 § 8)

One JSON object per line, flushed and fsynced at once (`live/journal.py`):

| kind | written when | used by the replay |
|---|---|---|
| `session` | first line: session id, symbols, digest and full columns of each feed's warm-up history | rebuilds the feeds (the history given to a resumed session is ignored) |
| `evt` | a venue message, at the slice boundary where it is applied, in that order | applied at the same slice |
| `slice` | after its messages: time, position, `stale`, the bars that closed | the slice itself |
| `cmd` | a command, **before** it is sent: `place` (the request) or `cancel`, with the slice position | compared with what the strategy asks |
| `recon`, `guard`, `kill`, `note`, `end` | what reconciliation, a guard, the kill switch did; the end of a run | informational (recomputed) |

Write-ahead: a command is journalled before the socket sees it, so a crash can leave a
journalled command that never reached IB, never the opposite; reconciliation settles the
first case (§ 8). A torn last line (a crash mid-write) is dropped on load and cut before
appending.

Why a replay is exact: the strategy is a deterministic function of the bars and of the
messages it is given, and the journal holds both, in the order and at the slice they were
applied. Hence `05` § 8: a live session is a backtest whose feed is the journal, and any
divergence between a session and its replay is a bug with a reproducer. The test suite
stops a session after 60 slices, resumes it in a fresh engine, and gets the orders and
the final value of an uninterrupted run.

## 7. Reconciliation

### 7.1 At start

The live stage begins with `snapshot(full_sync=True)`: cash, positions with average cost,
the working orders and the session's executions as the broker lists them. The first
snapshot of a session is always adopted -- it is the account the strategy starts from:
positions are set on the strategy's feeds (with a `Trade` opened at the average cost, so a
later sale closes a trade), positions on symbols the strategy does not trade are listed in
`foreign_positions` and ignored, and the cash becomes the account's (or the sleeve's, § 7.3).

### 7.2 Continuously

Every slice (`reconcile_every=1`) a snapshot is queued after the venue's messages and
compared with the book, symbol by symbol. A difference is ignored when it is explained by
work in flight: an execution applied in the same batch, or a gap within the quantity the
working orders of that symbol still have to execute. Anything else is a mismatch, journalled
as `recon`, then:

* `reconcile="adopt"` (default): the broker's quantity and average cost replace the book's
  (a manual trade in TWS, a split, a corporate action, an assignment);
* `reconcile="halt"`: the kill switch trips (§ 10) -- the right setting with real money.

With no order working, the cash is set to the account's `TotalCashValue` and the drift
journalled (fees, interest, dividends are not modelled live).

### 7.3 Sleeve mode

`LiveEngine(capital=...)` (or `broker.setcash(...)` before the run) runs the strategy as a
sleeve: its cash starts at `capital` minus the cost of its positions and then moves with its
own executions only; the account's cash is not followed. That is how a catalogue strategy
written for `order_target_percent` of the portfolio trades 100 000 on a paper account
that holds 1 000 000.

## 8. Restart and resume

Restarting with the same journal path resumes the session: the warm-up history comes from
the journal header (its digest is checked), the journal is replayed (§ 6), then the adapter
connects and the full snapshot reconciles three things:

* **executions missed while down**: the snapshot's executions (ib_async's `fills()`, filled
  at connection) are applied if their `exec_id` is new;
* **orphans**: an order the book thinks working that the broker does not list (sent and lost
  in a crash, or done while away without a visible execution) becomes `Canceled` with the
  reason "not working at the broker after a resync";
* **positions and cash**, as in § 7.2.

The bars missed while down arrive at the first poll and are stepped as stale slices
(§ 4): the strategy's state catches up, only the latest bar can trade. IB reports the
executions of the current day by default (TWS can be set to keep more days): a session
down over a night with executions is caught up by the position reconciliation, not by its
executions -- to check on the paper account (§ 15, Q7).

## 9. Disconnections and pacing

| event | IB code | what happens |
|---|---|---|
| TWS ↔ IB servers lost | 1100, 2110 | `Connection(False)`: new orders are refused locally ("not connected"); working orders stay at IB |
| restored, data maintained / lost | 1102 / 1101 | `Connection(True)` and a full snapshot (missed executions, orphans, positions) |
| API socket closed (TWS or Gateway restarting, nightly restart) | ib_async `disconnectedEvent` | `Connection(False)`; `idle` reconnects with back-off 2, 5, 10, 30, 60 s, then queues `Connection(True)` and a full snapshot |
| cannot connect / not connected | 502, 504 | `Notice`; a `place` while down queues `Rejected(504)` |
| farm status and informational | 2100-2199 | ignored |
| any other code | | `Notice`, journalled |

Pacing, enforced by the adapter (`guards.RateLimiter`, `KeyedInterval`), on IB's documented
limits: at most 45 messages a second to TWS (IB: 50); at most 60 historical-data requests
in any ten minutes; never the same historical request twice within 15 seconds. A daily
session makes a handful of requests a day; the limits matter at start (history of every
feed) and with many symbols.

## 10. Guards

* **Paper by default.** `IBAdapter(require_paper=True)` refuses the live ports (TWS 7496,
  Gateway 4001) and any managed account that is not a paper account (`DU…`, `DF…`). Going
  live takes two deliberate changes: the port and `require_paper=False`.
* **Read-only.** `LiveEngine(read_only=True)` refuses every order locally (journalled as
  `guard`, notified `Rejected`), and `IBAdapter(readonly=True)` connects with the API's
  read-only flag: what the strategy would send is in the journal, nothing reaches IB. The
  example runs read-only unless `--trade`.
* **`RiskLimits`**: maximum quantity and notional per order, maximum position notional,
  maximum orders per slice, an allowed-symbols list, no shorting; a breach refuses the
  order, and with `trip_on_breach` trips the kill switch.
* **Kill switch.** `KillSwitch(path)` trips from code (`trip(reason)`) or when the file
  appears (`touch live/KILL`), checked between polls and between slices. When it trips,
  every working order is cancelled, nothing more is sent, the run ends, and the journal
  records it. The `halt` reconciliation policy trips it from inside a slice.
* **Market-on-open by default** (`market_tif="OPG"`): a market order placed on a daily
  close executes in the next opening auction, the price a backtest fills at, rather than at
  whatever the first minutes trade at.

## 11. Paper before real

1. **FakeIB**: `python docs/examples/live_ibkr_paper.py --fake` -- the whole path, no account.
2. **Paper, read-only**, a few days: orders computed and journalled against the paper
   account's real positions, nothing sent; check the journal's `guard` records.
3. **Paper, trading** (`--trade`), several weeks: compare each month the session with the
   backtest of the same strategy over the same dates (the journal's bars give the dates; fills
   differ by the opening auction's prices only).
4. **Real money** only after Édouard's sign-off, with a small `capital`, `reconcile="halt"`,
   tight `RiskLimits` and the kill file documented to whoever watches the session.

What Édouard sets up for step 2 (also in the example's docstring): a paper account (IB
Account Management > Settings > Paper Trading Account; it starts with `DU`); TWS or IB Gateway
logged into it; *Global Configuration > API > Settings*: "Enable ActiveX and Socket Clients"
ticked, socket port 7497 (TWS paper) or 4002 (Gateway paper), "Allow connections from
localhost only" ticked, "Read-Only API" ticked for step 2 and unticked for step 3; market data
on the paper account (it shares the live account's subscriptions once enabled in Account
Management) for the four ETFs; `pip install 'tradewind[ibkr]'`; then `python
live_ibkr_paper.py` from `docs/examples/`.

## 12. The client library: ib_async or IB's ibapi

| | ib_async | ibapi (IB's official Python client) |
|---|---|---|
| licence | BSD 2-Clause (LICENSE of the 2.1.0 wheel; its PyPI classifier says "Other/Proprietary", which the licence file contradicts); dependencies aeventkit and nest_asyncio (BSD), tzdata (Apache 2.0) | IB's own TWS API licence, not an OSI licence; distributed by IB with the TWS API download (and as `ibapi` on PyPI, a channel and versioning to verify) |
| what it is | an independent implementation of the TWS socket protocol, the community successor of ib_insync (unmaintained since 2024), at github.com/ib-api-reloaded | IB's reference client: `EClient` / `EWrapper` callbacks |
| state | keeps orders, trades, fills, positions, account values in sync (`openTrades()`, `fills()`, `positions()`), reconnects cleanly, turns order errors into statuses | none: every request and every callback is the user's to track, plus a reader thread to run |
| concurrency | asyncio loop run in `ib.sleep()`: fits the engine's single thread (`idle`) | a thread for the reader, so a queue and locks on our side |
| risk | a small volunteer team; IB protocol changes land after IB's own client | IB's release cadence; heavier code for the same features |
| packaging | `pip install ib_async`, Python ≥ 3.10, pure Python | download from IB or PyPI; versions tied to the TWS API release |

**Recommendation: ib_async**, as the optional extra `tradewind[ibkr]` (`ib_async>=2.0`),
never vendored: a licence compatible with Apache 2.0 as a dependency, the account state and
reconnection logic we would otherwise write, and a loop that fits the engine's
single-threaded design. The adapter seam limits the bet: an `ibapi` adapter is the same
`BrokerAdapter` (an estimated 400 lines with its reader thread) if ib_async stalls. ib_async
2.1.0 was installed in an isolated virtual environment of the worktree to check the names
and signatures `IBAdapter` uses (no connection made); the test that builds an ib_async
`Order` from our mapping runs where ib_async is installed and is skipped elsewhere.

## 13. What differs from a backtest

* Fills are the venue's: price, partial fills, commission. With `market_tif="OPG"` and the
  same bars, the simulated venue reproduces the backtest's orders and NAV (test
  `test_live_session_reproduces_the_backtest`: same orders, value within 1e-12).
* Look-ahead reads get NaN (`data.close[1]`) or raise (`datetime.date(1)`): a corpus strategy
  that spots month ends with `date(1)` must change before going live.
* `add_cash` raises (5.4 % of the corpus uses it for synthetic sleeves); `setcash` sets the
  sleeve's capital.
* Orders placed during warm-up and on stale slices are refused silently.
* `runonce=False`: a next-only indicator reading a feed it was not given steps on that
  feed's current bar, not on its last (the backtest quirk of `02-semantics` is not reproduced
  live, on purpose).
* Notifications arrive at the next slice, as in a backtest: an order placed on Monday's close
  and filled at Tuesday's open is notified `Accepted` and `Completed` on Tuesday's close.

## 14. Tests (`tests/test_live.py`, 34 tests, no account, no network)

Live/backtest equivalence; warm-up sends nothing; look-ahead reads NaN; partial fills over
several bars; partial fill then expiry (OPG); rejection with its reason; cancel through the
venue; dropped connection (orders refused while down, the missed fill applied at the
resync); initial positions adopted and foreign positions listed; manual trade adopted;
mismatch halts under `halt` and cancels the working order; risk limits; read-only sends
nothing; kill file stops and cancels; resume after a stop equals an uninterrupted run; resume
after a week's gap trades only the last bar; a modified journal raises `LiveDivergence`; a
torn journal line; message round trip through JSON; clock settling (holiday, timeout, late
bar) and stale flags; rate limiters; order and status mappings; the IB adapter against a stub
of ib_async's interface (paper-only refusal, callbacks to messages, rejection from a
`Cancelled` status, disconnection); `IBBars` never passing a forming bar and pacing its
requests; an ib_async `Order` built from the mapping (skipped without ib_async); `import
tradewind` does not load the live package; the example runs on `FakeIB`.

## 15. Questions for Édouard

1. **Market orders at the open.** Default `OPG` (market-on-open, the backtest's price) or
   `DAY` (a market order queued for the open, filled in the first seconds)? Proposed: `OPG`.
2. **One account, one strategy?** Step 1 assumes one strategy per session and runs it as a
   sleeve of the paper account (`capital`). Several catalogue strategies on one account need
   per-strategy books (IB model codes or sub-accounts). Proposed: one strategy per paper
   account for step 2, the multi-strategy book in a later step.
3. **Warm-up data.** The example warms up on IB's unadjusted `TRADES` daily bars, the same
   series the live bars extend; PWB's datasets are adjusted, so a backtest and a live session
   can differ after a dividend. Proposed: IB bars for live and for the comparison backtest of
   § 11, PWB data for research only.
4. **Reconciliation policy.** `adopt` by default for paper, `halt` required for real money?
   Proposed: yes.
5. **Where the session runs.** TWS / IB Gateway needs a login (and 2FA for a real account)
   and restarts nightly; this Mac or a server with IB Gateway under a supervisor (IBC)?
   Proposed: this Mac for paper, the server question at step 4.
6. **Strategies using `add_cash`** (5.4 % of the corpus): out of scope for live, or a
   documented rewrite? Proposed: out of scope.
7. **To check on the paper account at the first run**: the history of executions IB returns
   after a reconnect (current day or more), the market-data permissions for historical
   daily bars of the four ETFs (codes 162 / 354 otherwise), and how paper fills MOO orders.

## 16. Clean-room note

Written from `05-interfaces.md`, IB's public API documentation (order statuses, message
codes, pacing, paper accounts) and ib_async's public names; Lean concepts as attributed in
`NOTICE`. No backtrader source, IB store included, was opened. Logged in `CLEANROOM.md`.
