# 05. Interfaces of the engine (chantier 2)

**Status: proposal, to be validated by Édouard before any code.** Nothing in `src/` depends
on this document yet. It fixes the seams of the engine that chantier 2 builds and chantier 4
fills: `Security` (currency, multiplier, tick size, lot), `Broker`, the pluggable reality
models, the clock, the event bus and the feeds. The questions that need a decision are at the
end (§ 10).

Sources: `01-scope.md`, `02-semantics.md` (the observed behaviour every default must
reproduce), `04-design.md` (the modules as built), and QuantConnect Lean (Apache 2.0, read as
a specification, attributed in `NOTICE`): `Security`, `SymbolProperties`, `SecurityHolding`,
`IFeeModel`, `IFillModel`, `ISlippageModel`, `IBuyingPowerModel`, `ISettlementModel`,
`IShortableProvider`, `IMarginInterestRateModel`, `ISecurityPortfolioModel`, `CashBook`,
`SecurityChanges`, `SecurityExchangeHours` and the time loop of `AlgorithmManager.Run`.
backtrader's source is not a source (`CLEANROOM.md`).

## 1. Constraints and how they are met

| constraint (card) | how this design meets it |
|---|---|
| **One event loop** | `Engine.run` iterates one `Clock`; every state change happens in a numbered phase of one `TimeSlice` (§ 3). No model runs outside a phase, no callback is asynchronous. |
| **Vectorise across securities, never across time** | Market data, holdings and per-bar accruals live in struct-of-arrays indexed by `SecurityId` (§ 7); each phase does O(1) numpy calls over all securities, then Python only for sparse things (orders, fills, events). Nothing that carries portfolio state (cash, holdings, orders) is computed for several bars at once. |
| **Live-compatible from the start** | The clock, the feeds and the broker are interfaces with a backtest and a live implementation; models take the time as an argument and never read an array beyond the current bar; external events (fills from a real broker, new bars) enter through one inbox drained at slice boundaries and are journalled, so a live session replays as a backtest (§ 8). |
| **BaseStrategy contract frozen** | Nothing a strategy calls changes name, signature or meaning (§ 9). New capabilities are opt-in hooks and read-only accessors; with the default models they are never triggered. |
| **Defaults reproduce the oracle** | Every interface ships a `Compat*` default that is the behaviour already implemented and observed (`02-semantics.md`; 17/17 synthetic probes, 21/21 with PR #5; 169/170 corpus NAVs). The `oracle` model set is the default until the 4 200-NAV pass is green and Édouard switches it (§ 10, Q1). |
| **Speed budget** | ≤ 3 µs of engine time per security-bar on 4 000 securities (backtrader 35 µs; Tradewind today 1.5 to 3x faster than backtrader). The budget per phase is in § 7. |

## 2. Object map

```
Engine (Cerebro facade)
 ├── Clock ──────────── yields TimeSlice(time, has_bar mask, events)
 ├── Feeds ─────────── DataFeed / PanelFeed  ──►  MarketState (arrays, one row per security)
 ├── Universe ──────── UniverseProvider        ──►  UniverseChange events
 ├── CorporateActions ─ CorporateActionProvider ──►  Split / Dividend / SymbolChange / Delisting
 ├── Securities ────── Security[id]: SymbolProperties, ExchangeHours, ModelSet, holding view
 ├── Broker ────────── SimulatedBroker (backtest) | LiveBroker (adapter)
 │     ├── Portfolio ─ PortfolioState (arrays) + CashBook
 │     └── per security: FeeModel, FillModel, SlippageModel, BuyingPowerModel,
 │                       SettlementModel, ShortableProvider, MarginInterestRateModel
 ├── EventBus ──────── ordered queue of OrderEvent, TradeEvent, CashEvent, CorporateActionEvent,
 │                     UniverseEvent, TimerEvent; drained at fixed phases
 └── Strategy (BaseStrategy, unchanged) + Analyzers + Observers, fed by the bus
```

A `Security` is the engine's record of one tradable instrument. The compat feed object a
strategy holds (`self.datas[i]`, a `PandasData`) stays the strategy's handle; the engine maps
each feed to one `SecurityId` and exposes the security read-only as `data.security` (§ 10,
Q6).

## 3. The bar: deterministic order of events

One `TimeSlice` per clock time. Phases run in this order, always; inside a phase the order is
the one given in the last column, so two runs on the same inputs produce the same sequence of
events and the same floats.

| # | phase | what happens | Lean counterpart | order inside the phase |
|---|---|---|---|---|
| 0 | clock | `now = slice.time`; past scheduled timers whose time had no bar are caught up | `ScanPastEvents`, `SetDateTime` | timer creation order |
| 1 | universe | additions and removals effective at `now` registered; a removed security keeps its holding until phase 3 or a sell | `SecurityChanges` (engine side) | `SecurityId` (adddata order) |
| 2 | market data | each feed with a bar at `now` loads it into `MarketState`; a feed without one keeps its last bar and its length does not grow; `CashBook` conversion rates updated | `security.Update`, `cash.Update` | vectorised; no order |
| 3 | corporate actions | effective at `now`, applied to holdings and open orders **before any fill**: symbol change, split (quantity × r, average price ÷ r, open order sizes and prices adjusted), dividend (cash per share credited on the ex-date through the settlement model), delisting (per `DelistingPolicy`) | `HandleSplits`, `HandleDividends`, delisting handling (Lean runs them after the fills of the slice, because its daily events sit in the slice before the open; ours sit on the same daily bar, hence before the fills -- § 10, Q2) | `SecurityId`, then symbol change → split → dividend → delisting |
| 4 | accruals | settlement scan (unsettled cash maturing at `now` becomes available); borrow fees on shorts; margin interest | `SettlementModel.Scan`, `ApplyMarginInterestRate` (hourly in Lean, at each slice here) | vectorised |
| 5 | acceptance | the orders submitted during the previous slice are checked as one batch, in submission order, by each security's `BuyingPowerModel`; each becomes `Accepted` or `Margin` | `HasSufficientBuyingPowerForOrder` at submission | order `ref` |
| 6 | fills | each accepted, still-open order with a bar at `now`: `FillModel` (which calls `SlippageModel`) proposes fills, the `BuyingPowerModel` re-checks them at the fill price, `FeeModel` prices them, the portfolio applies them (`SettlementModel.apply_funds` for the cash) | `ProcessSynchronousEvents` → `IFillModel.Fill` → `ISecurityPortfolioModel.ProcessFill` | order `ref` |
| 7 | transfers | queued cash additions (`add_cash`) land | — (Lean has none) | request order |
| 8 | mark | portfolio value marked once: cash in account currency + Σ holdings × last price × multiplier | `TotalPortfolioValue` | order-preserving sum over `SecurityId` (§ 7.3) |
| 9 | margin call | the margin-call model may emit liquidation orders (placed now, handled from phase 5 of the next slice) | `MarginCallModel` | `SecurityId` |
| 10 | indicators | next-only indicators step; precomputed ones move their cursor | consolidators | creation order |
| 11 | timers (start) | `notify_timer` for `SESSION_START` timers due at `now` | scheduled events | timer creation order |
| 12 | delivery | the bus is drained to the strategy and analyzers: order events, then trade events, then cash/value, then corporate-action and universe events | `OnOrderEvent`, `OnSecuritiesChanged`, `OnSplits`, `OnDividends`, `OnDelistings` | (event kind, sequence number) |
| 13 | strategy | `prenext` / `nextstart` / `next`; orders placed here are `Submitted` (event queued, delivered at phase 12 of the next slice) | `OnData` | — |
| 14 | analyzers, observers | `prenext` / `next` | `results.Sample` | creation order |
| 15 | timers (end) | `notify_timer` for `SESSION_END` timers | scheduled events | timer creation order |

What this reproduces of the oracle (compat): fills at the next bar's open happen in phase 6
of the slice after the order; the commission scheme reads the fill bar's date because phase 2
precedes phase 6; `add_cash` lands at the end of the next pass (phase 7) and is invisible to
that pass's checks and fills; the value is marked once per bar after fills and additions
(phase 8); notifications arrive at the start of the next bar, Submitted then Accepted then
fills (phase 12, kinds then sequence); an order placed on the last bar is never notified;
`next` waits until every feed has delivered a bar (phase 13 gate). Phases 1, 3, 4 and 9 do
nothing with the default models: today's feeds carry no events and the defaults are no-ops.

## 4. Securities

### 4.1 Identifiers, money, time

```python
SecurityId = int            # dense index 0..n-1 in registration (adddata) order; array row
Symbol = str                # the feed's name ("AAPL", "EURUSD", "BTC.CRP")
Currency = str              # ISO-like code, "USD"
Timestamp = np.int64        # ns since epoch, UTC; dates are midnight UTC (compat feeds)

@dataclass(frozen=True, slots=True)
class CashAmount:
    amount: float
    currency: Currency
```

Money is `float64` (§ 10, Q3), as the oracle computes it: decimal arithmetic would move every
NAV by rounding and break bit-level parity.

### 4.2 `SymbolProperties` (Lean `SymbolProperties`)

```python
@dataclass(frozen=True, slots=True)
class SymbolProperties:
    description: str = ""
    quote_currency: Currency = "USD"
    contract_multiplier: float = 1.0        # value of one unit = price × multiplier
    minimum_price_variation: float = 0.0    # tick size; 0 = prices are not rounded
    lot_size: float = 1.0                   # order sizes are multiples of it in the sizing helpers
    minimum_order_size: float | None = None
    price_magnifier: float = 1.0            # quoted price / real price (pence quotes)
    market_ticker: str | None = None
```

* **Default (= oracle)**: USD, multiplier 1, no tick rounding, lot 1 applied **only** where the
  oracle truncates (`order_target_percent` / `_value` / `_size` round toward zero to an
  integer); a raw `buy(size=0.5)` keeps its size, as today. A `CommInfoBase` with `mult` keeps
  its own multiplier (compat path).
* **Chantier 4**: a `SymbolPropertiesDatabase` keyed by symbol and market (futures multipliers,
  forex lots, crypto fractional lots, pence-quoted LSE lines) loaded from a CSV shipped with the
  package; tick rounding of limit/stop prices in the fill model.
* **Live**: the broker adapter can override lot and tick from the venue's contract details.

### 4.3 `Security`

```python
class Security:
    id: SecurityId
    symbol: Symbol
    properties: SymbolProperties
    hours: ExchangeHours
    models: ModelSet                    # § 5; per security, shared instances by default
    feed: DataFeed                      # the compat feed object the strategy holds
    # read-only views on MarketState / PortfolioState rows (no per-bar Python update)
    @property
    def price(self) -> float: ...       # last close
    @property
    def bar(self) -> Bar: ...           # open, high, low, close, volume, time of the current bar
    @property
    def has_bar(self) -> bool: ...      # a bar arrived at this slice
    @property
    def holding(self) -> SecurityHolding: ...
    @property
    def is_tradable(self) -> bool: ...  # listed, not delisted, has delivered a bar
    @property
    def is_delisted(self) -> bool: ...
```

`Security` is a thin object over array rows; the engine never loops over `Security` objects
in a phase (§ 7). Lean's per-security data filter, volatility model and option models are not
part of chantier 2.

### 4.4 `SecurityHolding` (Lean `SecurityHolding`) and the compat `Position`

```python
class SecurityHolding:                  # a view on PortfolioState row `id`
    quantity: float
    average_price: float
    total_fees: float
    realized_pnl: float
    total_dividends: float
    total_borrow_fees: float
    def value(self, price: float | None = None) -> float: ...   # quantity × price × multiplier
    def unrealized_pnl(self, price: float | None = None) -> float: ...
```

* **Default (= oracle)**: `Position(size, price)` stays the object `getposition()` returns;
  its `size` and `price` follow the update rule already reproduced (average price on increase,
  unchanged on reduction, reset on reversal). `SecurityHolding` adds the accumulators and is
  read through `data.security.holding`.
* **Chantier 4**: dividends and borrow fees accumulate here; `TradeBuilder`-style statistics
  for analyzers.
* **Live**: reconciled from the broker's positions at start and after each fill event.

### 4.5 `CashBook` (Lean `CashBook`, `Cash`)

```python
class Cash:
    currency: Currency
    amount: float
    conversion_rate: float              # value of 1 unit in the account currency
    conversion_source: SecurityId | None   # the forex security that prices it

class CashBook:
    account_currency: Currency          # "USD"
    def __getitem__(self, currency: Currency) -> Cash: ...
    def add(self, currency: Currency, amount: float) -> None: ...
    def convert(self, amount: float, src: Currency, dst: Currency) -> float: ...
    def total_in_account_currency(self) -> float: ...
    def unsettled(self) -> CashAmount: ...  # from the settlement models
```

* **Default (= oracle)**: one `Cash("USD")`; `getcash()` is its amount; conversion rate 1.0;
  every fill and fee is in USD. The mark of phase 8 keeps today's summation order, so the NAV
  is bit-identical.
* **Chantier 4**: securities quoted in another currency (the foreign indices PWB quotes in
  USD are already converted upstream; true multi-currency needs the raw forex pairs) get a
  `Cash` per quote currency, priced by a forex security added automatically (Lean does the
  same); `getvalue()` stays in the account currency.
* **Live**: balances per currency read from the broker account.

## 5. The pluggable reality models

Every model is a `typing.Protocol` with a **scalar** method, as in Lean (one security, one
order), and an **optional vector** method for the per-bar models, used by the engine when
present (§ 7, § 10 Q5). A `ModelSet` bundles them; `ModelSet.oracle()` is the default
profile, `ModelSet.realistic()` the profile chantier 4 assembles; any single model can be
overridden per security.

```python
@dataclass(slots=True)
class ModelSet:
    fee: FeeModel
    fill: FillModel
    slippage: SlippageModel
    buying_power: BuyingPowerModel
    settlement: SettlementModel
    shortable: ShortableProvider
    margin_interest: MarginInterestRateModel
    corporate_actions: CorporateActionModel

    @classmethod
    def oracle(cls) -> "ModelSet": ...       # everything observed in 02-semantics
    @classmethod
    def realistic(cls) -> "ModelSet": ...    # chantier 4
```

Compat wiring: `broker.setcommission(...)`, `broker.addcommissioninfo(...)`,
`set_slippage_perc/fixed`, `set_coc`, `set_checksubmit`, `set_shortcash` keep working: they
build or reconfigure the corresponding `Compat*` model of the securities they target.

### 5.1 `FeeModel` (Lean `IFeeModel.GetOrderFee(OrderFeeParameters) -> OrderFee`)

```python
@dataclass(frozen=True, slots=True)
class FeeParameters:
    security: Security
    order: Order
    fill_quantity: float                # signed
    fill_price: float
    time: Timestamp

class FeeModel(Protocol):
    def get_order_fee(self, p: FeeParameters) -> CashAmount: ...
```

* **Default (= oracle)**: `CommInfoFeeModel(comminfo)`: `comminfo.getcommission(size, price)`
  of the `CommInfoBase` attached to the feed (percent, fixed, or a subclass overriding
  `_getcommission`, such as pwb-toolbox's `SpreadImpactCommission`), charged at the fill.
  The bar date the scheme reads is the fill bar's (phase 2 before phase 6).
* **Chantier 4**: per-security, per-year spread and impact from the transaction-cost
  parameters dataset (Roll / Gibbs per name), exchange fee schedules, a minimum per order.
* **Live**: the fee reported by the broker's fill event replaces the modelled one; the model
  still prices the pre-check.

### 5.2 `SlippageModel` (Lean `ISlippageModel.GetSlippageApproximation(Security, Order)`)

```python
class SlippageModel(Protocol):
    def get_slippage(self, security: Security, order: Order, reference_price: float) -> float:
        """Adverse price move, in price units, added for a buy and subtracted for a sell."""
```

* **Default (= oracle)**: `CompatSlippage(perc, fixed, slip_open, slip_limit, slip_match,
  slip_out)`: the broker parameters of the compatible API, zero unless set.
* **Chantier 4**: volume-share impact (`k · σ · sqrt(q / ADV)`), half-spread from quotes.
* **Live**: not applied (the venue's price is the fill); kept for the pre-check.

### 5.3 `FillModel` (Lean `IFillModel.Fill(FillModelParameters) -> Fill`)

```python
@dataclass(frozen=True, slots=True)
class FillParameters:
    security: Security
    order: Order
    bar: Bar                            # the bar at `time` (open, high, low, close, volume)
    placed_bar: Bar                     # the bar the order was placed on (cheat-on-close)
    time: Timestamp
    slippage: SlippageModel

@dataclass(frozen=True, slots=True)
class FillProposal:
    quantity: float                     # signed; may be less than the remaining size
    price: float

class FillModel(Protocol):
    def fill(self, p: FillParameters) -> FillProposal | None:
        """None: no fill at this bar (limit not reached, no bar); the order stays open."""
```

* **Default (= oracle)**: `NextBarFillModel`: market at the bar's open (with cheat-on-close:
  at the placing bar's close); limit at the limit price when the bar trades through it, at the
  open when it gaps through; stop at the stop when touched, at the open when gapped; `Close`
  at the close; whole size in one fill; `valid` expiry checked first.
* **Chantier 4**: `VolumeShareFillModel` (partial fills capped at a share of the bar's volume,
  the rest carried; `02-semantics` decision 3), intrabar paths for limit/stop on minute data,
  auction prices for market-on-open/close.
* **Live**: replaced by the broker: proposals come from the venue's executions through the
  inbox (§ 8).

### 5.4 `BuyingPowerModel` (Lean `IBuyingPowerModel`)

The model that decides whether an order may proceed, at acceptance (phase 5) and at the fill
(phase 6), and how the sizing helpers turn a target into a quantity.

```python
@dataclass(slots=True)
class BatchState:
    running_cash: float                 # carried through the batch by the broker
    pseudo_positions: dict[SecurityId, float]

@dataclass(frozen=True, slots=True)
class BuyingPowerCheck:
    accepted: bool
    cash_consumed: float                # what the broker subtracts from running_cash
    reason: str = ""

class BuyingPowerModel(Protocol):
    def check_order(self, security: Security, order: Order, check_price: float,
                    state: BatchState) -> BuyingPowerCheck: ...
    def check_fill(self, security: Security, order: Order, fill: FillProposal,
                   cash: float) -> FillProposal | None:
        """The part of the proposal the cash allows (None: Margin)."""
    def quantity_for_target_value(self, security: Security, target_value: float,
                                  current_value: float, price: float) -> float: ...
    def leverage(self, security: Security) -> float: ...
    def maintenance_margin(self, security: Security, quantity: float, price: float) -> float: ...
    def initial_margin(self, security: Security, quantity: float, price: float) -> float: ...
```

* **Default (= oracle)**: `CompatCashBuyingPower`, i.e. the five observed rules: the batch is
  checked in submission order, each order priced at its submission bar's close (limit/stop
  at their own price); `cash_consumed` is the order's cash flow **even when refused** (the
  observed deficit carry), accepted iff the running cash stays ≥ 0; at the fill, a reducing
  or closing order always passes, an opening one is re-checked at the fill price, a reversal
  fills its closing part and refuses its opening part when the cash left cannot cover it
  (Partial then Margin); `order_target_value` truncates the value gap, `order_target_percent`
  sizes on the value marked at the current close, toward zero. Shorts need no margin (the
  oracle's `shortcash`).
* **Chantier 4**: `CashAccount` without the carry (a refused order consumes nothing: the
  corrected default flagged in `02-semantics`), `RegTMargin` (50 % initial, 25 %
  maintenance, short proceeds held), `PatternDayTrader` limits, futures initial/maintenance
  margins from the properties database; the margin-call model of phase 9.
* **Live**: the pre-check stays local (it is the strategy's risk gate); the broker's own
  rejection arrives as a `Rejected` order event.

### 5.5 `SettlementModel` (Lean `ISettlementModel`)

```python
class SettlementModel(Protocol):
    def apply_funds(self, security: Security, amount: CashAmount, time: Timestamp,
                    cashbook: CashBook) -> None: ...
    def scan(self, time: Timestamp, cashbook: CashBook) -> None: ...
    def unsettled(self) -> CashAmount: ...
```

* **Default (= oracle)**: `ImmediateSettlement`: proceeds are available at the fill (T+0),
  `unsettled()` is zero.
* **Chantier 4**: `DelayedSettlement(days=1)` for US equities since May 2024 (T+2 before),
  T+0 for crypto, dividends paid on the pay date.
* **Live**: settled and unsettled balances come from the broker; the model only forecasts.

### 5.6 `ShortableProvider` (Lean `IShortableProvider`)

```python
class ShortableProvider(Protocol):
    def shortable_quantity(self, symbol: Symbol, time: Timestamp) -> float | None: ...  # None: unlimited
    def fee_rate(self, symbol: Symbol, time: Timestamp) -> float: ...     # annual borrow fee
    def rebate_rate(self, symbol: Symbol, time: Timestamp) -> float: ...
    # optional vector form, used at phase 4
    def fee_rates(self, ids: np.ndarray, time: Timestamp) -> np.ndarray: ...
```

* **Default (= oracle)**: `AlwaysShortable`: unlimited, zero fee, zero rebate.
* **Chantier 4**: a short-volume / borrow dataset where one exists; an easy-to-borrow list
  (no short when not listed); a flat fee per year for hard-to-borrow names.
* **Live**: the broker's locate / shortable-shares endpoint.

### 5.7 `MarginInterestRateModel` (Lean `IMarginInterestRateModel`)

```python
class MarginInterestRateModel(Protocol):
    def accrue(self, cashbook: CashBook, portfolio: "PortfolioState", time: Timestamp) -> float: ...
```

* **Default (= oracle)**: `NoInterest`: nothing accrues, negative cash costs nothing.
* **Chantier 4**: interest on debit balances and on idle cash (a benchmark rate series), the
  short-proceeds rebate.
* **Live**: read from the account; the model forecasts only.

### 5.8 Corporate actions

```python
@dataclass(frozen=True, slots=True)
class Split:            security: SecurityId; time: Timestamp; ratio: float          # new shares per old
@dataclass(frozen=True, slots=True)
class Dividend:         security: SecurityId; time: Timestamp; amount: float; pay_time: Timestamp | None = None
@dataclass(frozen=True, slots=True)
class SymbolChange:     security: SecurityId; time: Timestamp; old: Symbol; new: Symbol
@dataclass(frozen=True, slots=True)
class Delisting:        security: SecurityId; time: Timestamp; last_price: float | None = None

CorporateAction = Split | Dividend | SymbolChange | Delisting

class CorporateActionProvider(Protocol):
    def events(self, time: Timestamp) -> Sequence[CorporateAction]: ...

class CorporateActionModel(Protocol):
    def apply(self, action: CorporateAction, broker: "Broker") -> None: ...
```

* **Default (= oracle)**: no provider, so no events: the corpus feeds are back-adjusted
  upstream and a delisted feed simply stops (its position is carried at its last close, as the
  oracle does). The model exists so that a feed carrying events is handled the moment it
  arrives.
* **Chantier 4**: providers from PWB's corporate-actions and listing-status data; splits as
  quantity changes on unadjusted prices, dividends as cash, delisting liquidated at the last
  close (`02-semantics` decision 4), `notify_corporate_action` delivered at phase 12.
* **Live**: the broker's corporate-action messages; the engine applies only what the broker
  confirms.

## 6. Clock, universe, bus, feeds, broker

### 6.1 Clock and exchange hours (Lean `SecurityExchangeHours`, `ITimeProvider`)

```python
@dataclass(frozen=True, slots=True)
class TimeSlice:
    time: Timestamp
    position: int                         # index of the slice in the run
    has_bar: np.ndarray                   # bool[n_securities]
    actions: tuple[CorporateAction, ...] = ()
    universe: "UniverseChange | None" = None

class Clock(Protocol):
    def __iter__(self) -> Iterator[TimeSlice]: ...
    def now(self) -> Timestamp: ...

class ExchangeHours(Protocol):
    def is_open(self, time: Timestamp) -> bool: ...
    def is_trading_day(self, day: np.datetime64) -> bool: ...
    def session(self, day: np.datetime64) -> tuple[Timestamp, Timestamp] | None: ...  # open, close
```

* **Default (= oracle)**: `UnionClock`: the sorted union of every feed's timestamps, as
  today; `AlwaysOpen` hours (daily bars carry dates only, so a calendar adds nothing and
  changes nothing).
* **Chantier 4**: a market-hours database per exchange (holidays, early closes, time zones);
  minute bars; timers in exchange time.
* **Live**: `LiveClock(hours, bar_interval)` emits a slice when a bar closes on the wall clock
  (or when a feed delivers it, whichever is later), plus pure time pulses for timers.

### 6.2 Universe (Lean `SecurityChanges`)

```python
@dataclass(frozen=True, slots=True)
class UniverseChange:
    time: Timestamp
    added: tuple[SecurityId, ...]
    removed: tuple[SecurityId, ...]

class UniverseProvider(Protocol):
    def change(self, time: Timestamp) -> UniverseChange | None: ...
```

* **Default (= oracle)**: no provider: the universe is the feeds added before `run()`, each
  "listed" from its first bar (`next` waits for every feed's first bar, observed).
* **Chantier 4**: point-in-time universes (PWB's PIT universe datasets) without one feed per
  ever-listed symbol: securities enter and leave, which is also how inactive feeds stop
  costing time (the speed lever "élagage des flux").
* **Live**: the same provider evaluated daily.

### 6.3 Event bus

```python
class Event(Protocol):
    time: Timestamp
    seq: int                              # global sequence number, assigned at publish

@dataclass(frozen=True, slots=True)
class OrderEvent:
    time: Timestamp; seq: int; order_ref: int; security: SecurityId
    status: OrderStatus                   # Submitted Accepted Partial Completed Canceled Expired Margin Rejected
    fill_quantity: float = 0.0; fill_price: float = 0.0; fee: float = 0.0; message: str = ""

class EventBus:
    def publish(self, event: Event) -> None: ...
    def drain(self) -> list[Event]: ...   # sorted by (kind rank, seq); called at phase 12
    def subscribe(self, kind: type, handler: Callable[[Event], None]) -> None: ...
    inbox: "queue.SimpleQueue[Event]"     # the only thread-safe entry, for live adapters
```

It is a deterministic, synchronous queue, not a pub/sub network: publishing never runs a
handler; handlers run when the bus is drained, in (kind, sequence) order. Kind ranks for
phase 12: `OrderEvent` < `TradeEvent` < `CashEvent` < `CorporateActionEvent` <
`UniverseEvent`. The compat adapter turns order events into `notify_order(order)` with the
status frozen at publish time (the clone semantics already implemented), trade events into
`notify_trade`, cash events into `notify_cashvalue` / `notify_fund`.

* **Default (= oracle)**: exactly today's notification order (Submitted batch, Accepted
  batch, fills, trades), at the start of the next bar.
* **Chantier 4**: `notify_corporate_action`, `notify_universe` (opt-in methods, absent from
  the compat contract); an event journal to Parquet for audits.
* **Live**: the broker adapter's thread puts events into `inbox`; the engine moves them onto
  the bus at the next slice boundary, stamps `seq` and journals them; replaying the journal
  through a backtest reproduces the session (§ 8).

### 6.4 Feeds

```python
@dataclass(frozen=True, slots=True)
class FeedBlock:
    timestamps: np.ndarray                # int64 ns, sorted, unique
    columns: Mapping[str, np.ndarray]     # "open" "high" "low" "close" "volume" "openinterest", float64

class DataFeed(Protocol):                 # one security (the compat PandasData is one)
    name: Symbol
    def load(self) -> FeedBlock: ...      # backtest: the whole history at start
    def is_live(self) -> bool: ...
    def poll(self, until: Timestamp) -> FeedBlock | None: ...   # live: bars closed since last poll

class PanelFeed(Protocol):                # many securities at once (T × N), the fast path
    symbols: Sequence[Symbol]
    def load(self) -> tuple[np.ndarray, Mapping[str, np.ndarray]]: ...   # timestamps, field -> T×N
```

* **Default (= oracle)**: `PandasData` (and pwb-toolbox's `ArrayPandasData`) unchanged for
  the strategy; at start the engine reads each into a `FeedBlock` and aligns all blocks on the
  clock into `MarketState` (T × N arrays plus a `has_bar` mask), keeping the observed rules: a
  missing bar repeats the last one without growing `len(data)`; a line indexes its buffer as a
  plain sequence (look-ahead readable, flagged); fundamentals and alternative data stay the
  strategy's pandas business.
* **Chantier 4**: `PanelFeed` from PWB's datasets directly (no per-symbol DataFrame), point-in-
  time fundamentals as feeds, the corrected line indexing (no positive `ago`).
* **Live**: `poll` from a vendor stream or the broker's bars; the line buffers grow by append.

### 6.5 Broker

```python
class Broker(Protocol):
    def submit(self, order: Order) -> Order: ...
    def cancel(self, order: Order) -> bool: ...
    def on_slice(self, s: TimeSlice) -> None: ...          # phases 3 to 8
    def cash(self) -> float: ...
    def value(self) -> float: ...
    def holding(self, security: SecurityId) -> SecurityHolding: ...
    def open_orders(self) -> Sequence[Order]: ...
```

* **Default (= oracle)**: `SimulatedBroker` composed of the `ModelSet` of each security, with
  the compatible `BackBroker` methods as a facade (`getcash`, `setcash`, `add_cash`,
  `getvalue(datas)` with its observed side effect, `getposition`, `setcommission`,
  `set_coc`, `set_coo`, `set_slippage_*`, `set_checksubmit`, `set_shortcash`).
* **Chantier 4**: nothing new in the interface; realism lives in the models.
* **Live (chantier 7)**: `LiveBroker(adapter)` implements the same protocol: `submit` sends,
  the adapter reports acknowledgements, fills and rejections into the bus inbox,
  `on_slice` reconciles positions and cash with the account. Strategies do not change.

## 7. Vectorisation and the speed budget

### 7.1 Where the state lives

```python
class MarketState:          # one row per SecurityId; one column per field
    open: np.ndarray; high: np.ndarray; low: np.ndarray; close: np.ndarray; volume: np.ndarray
    has_bar: np.ndarray     # bool, this slice
    length: np.ndarray      # int, bars delivered so far = len(data)
    cursor: np.ndarray      # int, row of the current bar in each feed's buffer

class PortfolioState:
    quantity: np.ndarray; average_price: np.ndarray; multiplier: np.ndarray
    currency: np.ndarray    # index into the CashBook
```

A feed's lines read `buffer[cursor[id] + ago]`: advancing every feed is one numpy assignment
from the precomputed T × N alignment, not one Python call per line per feed (today: seven
`LineBuffer` cursors per feed, the main per-bar cost left on wide universes).

### 7.2 Budget per slice (4 000 securities, 3 µs each = 12 ms)

| phase | work | target |
|---|---|---|
| 2 market data | gather T × N rows into `MarketState`, `length += has_bar`, cursors | ≤ 0.2 ms |
| 3, 4 corporate actions, accruals | O(events) Python; vector fee/interest with the default models skipped entirely | ≈ 0 with defaults |
| 5, 6 acceptance, fills | O(orders) Python: monthly rebalancing of 4 000 names is ~4 000 orders once a month, ≈ 0.2 ms per bar amortised | ≤ 1 ms |
| 8 mark | one vector product and an order-preserving sum (§ 7.3) | ≤ 0.1 ms |
| 10 indicators | cursor moves; next-only indicators stepped (strategy-defined cost) | ≤ 0.5 ms engine part |
| 12 delivery | O(events) | ≤ 0.5 ms |
| engine total | | **≤ 2.5 ms per slice, ≈ 0.6 µs per security-bar** |

The rest of the 3 µs is headroom for the strategy's own `next()`. The benchmark of
`03-oracle.md` (5 × 20 years and 4 000 × 20 years) gates it nightly.

### 7.3 Bit parity under vectorisation

The oracle accumulates sequentially in Python floats; numpy's `sum` is pairwise and does not
give the same bits. Every reduction whose result feeds a decision (the NAV that sizes
`order_target_percent`, the running cash of the batch) keeps the oracle's left-to-right
order: `np.cumsum(np.concatenate(([cash], quantity * price)))[-1]` is sequential and
vectorised. Indicator arithmetic keeps the forms fixed in `02-semantics` (compensated window
sums, `prev * (1 - a) + x * a`). A property test checks each vectorised reduction against
the scalar loop on random inputs, bit for bit.

### 7.4 Across time

Nothing that carries portfolio state is computed across time. Indicators are the one
exception kept for backtests: a vectorised indicator (`once()`) fills its whole line before
the run because it is a pure function of its inputs, and it must equal its bar-by-bar path
bit for bit (a test per built-in); live runs step the same indicators bar by bar (§ 10, Q4).

## 8. Live compatibility in one paragraph

The engine is synchronous and single-threaded. Everything that comes from outside -- bars
(live feeds), executions, rejections, corporate actions from a broker -- enters through the
bus inbox and is moved onto the bus at the next slice boundary with a sequence number, then
written to an append-only journal. Models receive `time` and never read beyond the current
bar. The live clock emits slices on bar close. Hence: a live session is a backtest whose feed
is the journal, and any divergence between a live session and its replay is a bug with a
reproducer.

## 9. What stays frozen

The BaseStrategy contract of `01-scope.md` Tier 1 and Tier 2, unchanged in name, signature
and meaning: `params` / `p`, `datas` / `data` / `dnames` / `getdatabyname`, line indexing,
`datetime.date(ago)`, `broker.getvalue()` / `getcash()` / `add_cash()` / `setcash()` /
`getposition()`, `buy` / `sell` / `close` / `order_target_size|value|percent`,
`notify_order` / `notify_trade` / `notify_timer`, `add_timer`, `prenext` / `nextstart` /
`next` / `start` / `stop`, `Indicator`, `Analyzer`, `CommInfoBase`, `Cerebro` entry points.
Mapping of the compatible names onto the interfaces:

| compatible name | interface behind it |
|---|---|
| `CommInfoBase`, `setcommission`, `addcommissioninfo` | `CommInfoFeeModel` (+ multiplier / margin read from it) |
| `set_slippage_perc`, `set_slippage_fixed` | `CompatSlippage` |
| `set_coc`, `set_coo`, `Order.Market/Limit/Stop/StopLimit/Close`, `valid` | `NextBarFillModel` |
| `set_checksubmit`, `Margin` status, `order_target_*` truncation | `CompatCashBuyingPower` |
| `add_cash` | phase 7 transfer |
| `getvalue()` / `getvalue(datas)` | phase 8 mark / compat side effect |
| `notify_order` / `notify_trade` / `notify_cashvalue` / `notify_fund` | bus drain adapter |
| `Timer.SESSION_START` / `SESSION_END` | phases 11 / 15 |
| `PandasData` and its column parameters | `DataFeed` → `MarketState` |

Order statuses: the compat names stay; Lean's map as `New`→`Created`, `Submitted`→`Submitted`
(and `Accepted` for the batch acceptance Lean does not have), `PartiallyFilled`→`Partial`,
`Filled`→`Completed`, `Canceled`→`Canceled`, `Invalid`→`Margin` or `Rejected` (with the
reason), `CancelPending`/`UpdateSubmitted`: live only.

## 10. Decisions to take (Édouard)

1. **Profiles.** One engine with a model set per security; `ModelSet.oracle()` is the default
   and reproduces backtrader; chantier 4 assembles `ModelSet.realistic()`; a strategy or the
   toolbox can override one model. *Recommendation*: yes; the default flips to `realistic`
   only after the 4 200-NAV pass on `oracle` and your sign-off, with a regenerated oracle per
   `03-oracle.md`.
2. **Corporate actions inside the bar.** Apply splits, dividends (ex-date) and delistings at
   the start of the bar, before its fills (phase 3), rather than after them as Lean's loop does
   for its own daily events. *Recommendation*: before the fills: on daily bars the open of the
   ex-date is already in post-split units, so a fill before the split would trade the wrong
   quantity; dividends credited on the ex-date, the pay date handled later by the settlement
   model.
3. **Money as float64**, not decimal as in Lean. *Recommendation*: float64 everywhere with
   order-preserving reductions (§ 7.3): it is what makes the 4 200 NAVs reproducible bit for
   bit, and decimal would cost several times the speed budget.
4. **Indicators computed across time in backtests.** Keep `once()` (whole-line pre-compute)
   as a backtest optimisation, with a test per built-in that it equals the bar-by-bar path bit
   for bit, live stepping bar by bar; or forbid it and step everything. *Recommendation*:
   keep it with the equality test; the "never across time" rule then applies to everything
   that carries portfolio state.
5. **Vector methods on models.** Lean's models are scalar (one security, one order). Make
   the scalar protocol the contract and add optional vector methods (`fee_rates`,
   `accrue` over arrays) for the per-bar models only. *Recommendation*: yes: per-order
   models stay scalar (orders are sparse), per-bar ones get a vector path or the 3 µs budget
   is out of reach on 4 000 names.
6. **Exposure of `Security`.** Keep it internal in chantier 2, reachable read-only as
   `data.security` (properties, holding, models), with no change to BaseStrategy; the public
   typed API (`self.securities`, `notify_corporate_action`, `notify_universe`) comes with
   chantier 5. *Recommendation*: yes, so that nothing in the corpus or the toolbox can come to
   depend on an interface before it is frozen.

Live concurrency is not a question: the synchronous loop with one inbox (§ 8) is the only
design that keeps runs deterministic and replayable; say so if you see it otherwise.
