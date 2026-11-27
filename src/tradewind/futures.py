"""Futures: contracts, margin, daily settlement, expiry, explicit rolls, continuous series
(spec/09).

Read as a specification from QuantConnect Lean's public documentation (Apache 2.0, see
``NOTICE``): ``Future`` securities with a contract multiplier and a tick size, a margin
model that asks an initial margin per contract to open and a maintenance margin per contract
to hold, the expiry of a contract on its last trading date, and continuous contracts
back-adjusted by ratio or by difference for the signals. The code and the names are
Tradewind's own.

* ``FutureContract``: root, symbol, last trading date, multiplier, tick, initial and
  maintenance margin per contract, quote currency;
* ``FutureData``: the daily bars of one contract (its close is the settlement price);
* ``FuturesMargin``: the buying-power model of a contract: opening asks the initial margin,
  a fill never moves the notional, only the variation against the settlement reference;
* ``FuturesBook``: the broker's ledger of the open contracts -- the settlement reference of
  each position, the daily variation margin, the margin calls;
* ``FutureExpiry`` / ``FutureExpiries``: no delivery -- a position still open after its
  last trading date stops the run (``FutureExpiryError``), or, under ``at_expiry="close"``,
  is closed at that day's settlement (a forced close, fee charged);
* ``FutureChain``, ``RollOnDate``, ``RollOnVolume``, ``Roller``: which contract to hold, and
  the roll as two real orders (close the old contract, open the next), each paying its fee
  and slippage;
* ``continuous``: the back-adjusted series of a chain for the signals (``ratio``,
  ``difference`` or ``none``); ``ContinuousData``, its feed, which cannot be traded;
* ``add_futures``: one ``FutureData`` per contract of a daily table, the expiries handled by
  the engine, the chain and, on request, the continuous feed.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .comminfo import CommInfoBase
from .dateutils import date2num, num2date
from .feeds import PandasData, TimeFrame
from .models import BatchState, BuyingPowerCheck, FillProposal
from .securities import ACCOUNT_CURRENCY, SecurityId, SymbolProperties, Timestamp

RATIO, DIFFERENCE, NONE = "ratio", "difference", "none"
LIQUIDATE, RECORD = "liquidate", "record"
RAISE, CLOSE = "raise", "close"


INTRADAY_REFUSED = (
    "intraday bars on futures are not supported yet (spec/09 § 10.3): the rule is that a "
    "contract settles once a day, at the close of its session on the exchange calendar, and "
    "that settlement is not modelled for intraday feeds; use daily bars")


class FutureExpiryError(RuntimeError):
    """A contract reached its last trading date with a position still open, under
    ``at_expiry="raise"`` (the default of ``add_futures``, spec/09 § 5): the run stops
    instead of closing it silently. Roll before (``Roller``), close before, or pass
    ``at_expiry="close"`` to close it by force at its last settlement."""


def _as_date(day) -> _dt.date:
    if isinstance(day, _dt.datetime):
        return day.date()
    if isinstance(day, _dt.date):
        return day
    return pd.Timestamp(day).date()


# -- the contract ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FutureContract:
    """One delivery month of a futures root.

    ``multiplier``: money per point of price (50 for an equity index mini, 1 000 for crude
    oil). ``tick_size``: the price increment; fill prices are rounded to it (0: no
    rounding). ``initial_margin`` / ``maintenance_margin``: money per contract, posted from
    cash to open and to hold (maintenance defaults to the initial margin), fixed for the
    life of the contract; every reader goes through ``margin`` (the extension point of a
    dated schedule). No delivery is modelled: a position still open after
    ``last_trade_date`` stops the run, or is closed at that day's settlement under
    ``at_expiry="close"`` (spec/09 § 5)."""

    root: str
    symbol: str
    last_trade_date: _dt.date
    multiplier: float
    tick_size: float = 0.0
    initial_margin: float = 0.0
    maintenance_margin: float | None = None
    currency: str = ACCOUNT_CURRENCY
    first_trade_date: _dt.date | None = None

    def __post_init__(self):
        object.__setattr__(self, "last_trade_date", _as_date(self.last_trade_date))
        if self.first_trade_date is not None:
            object.__setattr__(self, "first_trade_date", _as_date(self.first_trade_date))
        if not self.multiplier > 0:
            raise ValueError(f"{self.symbol}: the multiplier must be positive")
        if self.tick_size < 0 or self.initial_margin < 0:
            raise ValueError(f"{self.symbol}: tick size and margins are not negative")
        if self.maintenance_margin is None:
            object.__setattr__(self, "maintenance_margin", float(self.initial_margin))
        if self.maintenance_margin > self.initial_margin:
            raise ValueError(f"{self.symbol}: the maintenance margin exceeds the initial margin")

    def margin(self, kind: str = "initial", day=None) -> float:
        """Margin per contract, ``"initial"`` or ``"maintenance"``. Fixed per root for now:
        ``day`` is not read. It is the one place every margin read goes through, so a dated
        schedule per root (exchanges move margins: crude's initial margin roughly doubled in
        March 2020) replaces this body with a lookup at ``day`` once the reference tables
        carry one (spec/09 § 10.2)."""
        if kind == "initial":
            return self.initial_margin
        if kind == "maintenance":
            return self.maintenance_margin
        raise ValueError("margin kind is 'initial' or 'maintenance'")

    def days_to_last_trade(self, day) -> int:
        return (self.last_trade_date - _as_date(day)).days

    def round_price(self, price: float) -> float:
        """``price`` on the contract's tick grid (nearest tick; unchanged without a tick)."""
        tick = self.tick_size
        if not tick or math.isnan(price):
            return price
        return round(round(price / tick) * tick, 10)

    def symbol_properties(self) -> SymbolProperties:
        return SymbolProperties(description=f"{self.root} {self.last_trade_date}", quote_currency=self.currency,
                                contract_multiplier=float(self.multiplier),
                                minimum_price_variation=float(self.tick_size), market_ticker=self.symbol)


class FutureData(PandasData):
    """The daily bars of one contract, prices in points (the security carries the
    multiplier). The close is read as the settlement price: ``add_futures`` puts the table's
    ``settlement`` there when it has one. A contract lists when it first trades and does not
    hold back a strategy's ``next`` until then."""

    params = (("contract", None),)

    def __init__(self):
        super().__init__()
        contract = self.p.contract
        if contract is None:
            raise ValueError("FutureData needs a contract")
        if self.p.timeframe < TimeFrame.Days or _intraday(self.p.dataname):
            raise ValueError(f"{contract.symbol}: {INTRADAY_REFUSED}")
        self.contract = contract
        self._tw_future = contract
        self.symbol_properties = contract.symbol_properties()
        self._tw_gate = False


def _intraday(frame) -> bool:
    """Whether a frame of bars carries a time of day (an intraday feed)."""
    index = getattr(frame, "index", None)
    if not isinstance(index, pd.DatetimeIndex) or not len(index):
        return False
    return bool((index != index.normalize()).any())


class FuturesCommission(CommInfoBase):
    """A fee per contract traded (``commission=2.25``: 2.25 per contract and side), integer
    sizes."""

    params = (
        ("commtype", CommInfoBase.COMM_FIXED),
        ("stocklike", True),
        ("percabs", True),
    )


# -- margin -------------------------------------------------------------------------------------


def _contract(security) -> FutureContract:
    return security.feed._tw_future


class FuturesMargin:
    """The buying-power model of a futures contract (spec/09 § 3).

    Nothing of the notional changes hands: opening contracts asks the **initial** margin per
    contract on top of the initial margin the open futures book already uses, posted from
    the cash; reducing or closing always passes. At the batch check (phase 5) the order is
    priced at its submission bar's close and the running cash carries the margin it blocks
    (or frees) plus its fee; at the fill (phase 6) the opening part is checked again against
    the cash left, and a reversal whose opening part does not fit fills its closing part
    only (Partial, then Margin). ``order_target_value`` and ``order_target_percent`` size on
    the notional, ``price x multiplier`` per contract. The margin is posted from **cash
    only**: stock positions in the same account are not collateral (spec/09 § 10.4)."""

    __slots__ = ()

    @staticmethod
    def _extra(contract, pos_size, size) -> float:
        """Initial margin blocked (+) or freed (-) by trading ``size`` against ``pos_size``."""
        return (abs(pos_size + size) - abs(pos_size)) * contract.margin("initial")

    def check_order(self, security, order, check_price, state: BatchState) -> BuyingPowerCheck:
        broker = security._broker
        sid = security.id
        pseudo = state.pseudo_positions
        pos_size = pseudo.get(sid)
        if pos_size is None:
            pos_size = pseudo[sid] = broker.getposition(security.feed).size
        if check_price is None:
            return BuyingPowerCheck(True, 0.0)
        if state.scratch is None:
            state.scratch = {}
        base = state.scratch.get("_tw_futures_initial")
        if base is None:
            base = state.scratch["_tw_futures_initial"] = broker.futures.requirement(broker, "initial")
        contract = _contract(security)
        fee = order_fee_of(security, order, order.size, check_price, state.time)
        extra = self._extra(contract, pos_size, order.size)
        pseudo[sid] = pos_size + order.size
        consumed = extra + fee
        if extra > 0 and order._check and state.running_cash - consumed < base:
            return BuyingPowerCheck(False, consumed, "initial margin not covered")
        return BuyingPowerCheck(True, consumed)

    def check_fill(self, security, order, fill: FillProposal, cash: float,
                   time: Timestamp = 0) -> FillProposal | None:
        broker = security._broker
        contract = _contract(security)
        size, price = fill.quantity, fill.price
        pos_size = broker.getposition(security.feed).size
        extra = self._extra(contract, pos_size, size)
        if extra <= 0:
            return fill
        required = broker.futures.requirement(broker, "initial")
        fee = order_fee_of(security, order, size, price, time)
        if cash - fee - extra >= required:
            return fill
        reversing = pos_size and ((pos_size > 0) != (size > 0)) and abs(size) > abs(pos_size)
        if reversing:
            return FillProposal(-pos_size, price)
        return None

    def quantity_for_target_value(self, security, target_value, current_value, price) -> float:
        notional = price * _contract(security).multiplier
        if not notional > 0:
            return 0
        return int((target_value - current_value) / notional)

    def leverage(self, security) -> float:
        return 1.0

    def maintenance_margin(self, security, quantity, price) -> float:
        return abs(quantity) * _contract(security).margin("maintenance")

    def initial_margin(self, security, quantity, price) -> float:
        return abs(quantity) * _contract(security).margin("initial")


def order_fee_of(security, order, size, price, time) -> float:
    from .models import order_fee

    return order_fee(security.models.fee, security, order, size, price, time)


class NotTradable:
    """The buying-power model of a continuous series: every order is refused (Margin). A
    back-adjusted level is not a price anyone trades at."""

    __slots__ = ()

    def check_order(self, security, order, check_price, state) -> BuyingPowerCheck:
        return BuyingPowerCheck(False, 0.0, "a continuous futures series is not tradable")

    def check_fill(self, security, order, fill, cash, time=0):
        return None

    def quantity_for_target_value(self, security, target_value, current_value, price) -> float:
        return 0

    def leverage(self, security) -> float:
        return 1.0

    def maintenance_margin(self, security, quantity, price) -> float:
        return 0.0

    def initial_margin(self, security, quantity, price) -> float:
        return 0.0


# -- the broker's ledger -----------------------------------------------------------------------------


@dataclass(slots=True)
class VariationMargin:
    """One daily settlement of one position: ``amount`` = quantity x (settlement - reference)
    x multiplier, credited (+) or debited (-) to the cash."""

    time: Timestamp
    security: SecurityId
    quantity: float
    reference: float
    settlement: float
    amount: float


@dataclass(slots=True)
class MarginCall:
    """Equity under the maintenance margin of the futures book after a settlement.
    ``deficit`` is what brings the equity back to the **initial** margin; ``orders`` the
    liquidation orders placed (policy ``"liquidate"``)."""

    time: Timestamp
    equity: float
    maintenance: float
    initial: float
    deficit: float
    orders: list = field(default_factory=list)


class FuturesBook:
    """The broker's ledger of futures positions (created on the first ``FutureData``).

    Each open position carries a **settlement reference**: its entry price until its first
    settlement, the last settlement price after. Its unsettled P&L is ``quantity x (price -
    reference) x multiplier``; that is what it adds to the portfolio value (the notional is
    never in the cash). At each bar of the contract's feed the broker settles it (phase 8):
    the unsettled P&L moves to the cash and the reference becomes the settlement price. A
    fill closing contracts moves only their unsettled P&L; one opening contracts moves
    nothing but its fee. Over the life of a position the cash flows add up to its realised
    P&L, ``(exit - average entry) x quantity x multiplier``.

    After the settlement (phase 9), an equity below the maintenance margin of the book is a
    ``MarginCall``; with ``on_margin_call="liquidate"`` (default) every futures position is
    cut pro rata, by market orders flagged ``margin_call`` filled at the next open, so that
    the initial margin of what remains fits in the equity; ``"record"`` only records it.
    """

    def __init__(self, on_margin_call: str = LIQUIDATE, log_variation: bool = True):
        self.on_margin_call = on_margin_call
        self.log_variation = log_variation
        self.reference: dict[int, float] = {}
        self.variation: list[VariationMargin] = []
        self.margin_calls: list[MarginCall] = []
        self.expiries: list = []
        self.rolls: list = []
        self._open_call: list = []

    @property
    def on_margin_call(self) -> str:
        return self._policy

    @on_margin_call.setter
    def on_margin_call(self, value: str) -> None:
        if value not in (LIQUIDATE, RECORD):
            raise ValueError("on_margin_call is 'liquidate' or 'record'")
        self._policy = value

    # -- fills ------------------------------------------------------------------------------
    def fill_flow(self, sid: int, oldsize: float, closed: float, opened: float, newsize: float,
                  price: float, m: float) -> float:
        """The cash flow of a fill (fees aside) and the new reference of the position."""
        ref = self.reference.get(sid, price)
        flow = -closed * (price - ref) * m if closed else 0.0
        if not newsize:
            self.reference.pop(sid, None)
        elif opened:
            if newsize == opened:                       # a new position, or a reversal's rest
                self.reference[sid] = price
            else:                                       # adding: the references average in
                self.reference[sid] = ((newsize - opened) * ref + opened * price) / newsize
        return flow

    def unsettled(self, sid: int, quantity: float, price: float, m: float) -> float:
        return quantity * (price - self.reference.get(sid, price)) * m

    # -- phase 8 ------------------------------------------------------------------------------
    def settle(self, broker, s) -> None:
        """Daily settlement: the unsettled P&L of every position whose contract has a bar now
        moves to the cash; the reference becomes that bar's close (the settlement price)."""
        if not self.reference:
            return
        has_bar = s.has_bar
        securities = broker.securities
        pf = broker.portfolio
        for sid, ref in list(self.reference.items()):
            if sid >= len(has_bar) or not has_bar[sid]:
                continue
            security = securities[sid]
            q = float(pf.quantity[sid])
            if not q:
                continue
            price = float(security.feed.close[0])
            if math.isnan(price):
                continue
            m = security.properties.contract_multiplier
            amount = q * (price - ref) * m
            if amount:
                broker.cashbook.add(security.properties.quote_currency, amount)
            self.reference[sid] = price
            if self.log_variation:
                self.variation.append(VariationMargin(s.time, sid, q, ref, price, amount))

    # -- phase 9 ------------------------------------------------------------------------------
    def requirement(self, broker, kind: str = "initial") -> float:
        """The initial or maintenance margin of the open futures positions, in the account
        currency."""
        total = 0.0
        securities = broker.securities
        pf = broker.portfolio
        for sid in self.reference:
            security = securities[sid]
            q = float(pf.quantity[sid])
            c = security.feed._tw_future
            per = c.margin("initial" if kind == "initial" else "maintenance")
            amount = abs(q) * per
            if broker._foreign and security.properties.quote_currency != broker.cashbook.account_currency:
                amount *= float(broker.fx_rates([sid])[0])
            total += amount
        return total

    def check(self, broker, s) -> None:
        if any(o.alive() for o in self._open_call):
            return
        self._open_call = []
        if not self.reference:
            return
        maintenance = self.requirement(broker, "maintenance")
        equity = broker._value
        if maintenance <= 0 or equity >= maintenance:
            return
        initial = self.requirement(broker, "initial")
        call = MarginCall(s.time if s is not None else broker._now, equity, maintenance, initial,
                          initial - equity)
        self.margin_calls.append(call)
        if self.on_margin_call != LIQUIDATE or broker.margin_call_model is not None:
            # a profile's own margin-call model (``set_profile``) already cuts the book,
            # futures included (``FuturesMargin.maintenance_margin``): recorded only
            return
        keep = max(0.0, equity / initial) if initial > 0 else 0.0
        pf = broker.portfolio
        for sid in list(self.reference):
            security = broker.securities[sid]
            q = float(pf.quantity[sid])
            if not q or security.is_delisted:
                continue
            cut = min(abs(q), math.ceil(abs(q) * (1.0 - keep) - 1e-9))
            if cut <= 0:
                continue
            side = broker.sell if q > 0 else broker.buy
            order = side(None, security.feed, cut, margin_call=True)
            call.orders.append(order)
            self._open_call.append(order)


# -- expiry -----------------------------------------------------------------------------------------


@dataclass(slots=True)
class FutureExpiry:
    """The end of one contract, applied at phase 3 of the first slice after its last trading
    date (before that slice's fills). Open orders on it end. A position still open there
    stops the run with ``FutureExpiryError`` (``at_expiry="raise"``, the default): no
    delivery is modelled, and a strategy that meant to stay exposed should have rolled.
    Under ``at_expiry="close"`` it is closed at the last settlement on or before the last
    trading date: its unsettled P&L is paid, its fee charged, its trade closed. Filled in
    by ``apply_to``."""

    security: SecurityId
    time: Timestamp
    settle_price: float = float("nan")
    quantity: float = 0.0               # contracts still held (signed): closed by force
    forced: bool = False
    cash: float = 0.0                   # variation paid at the forced close, fee deducted
    fee: float = 0.0
    at_expiry: str = RAISE
    rank = 4                            # after symbol change, split, dividend, delisting

    def apply_to(self, broker) -> None:
        security = broker.securities[self.security]
        feed = security.feed
        for order in list(broker.pending):
            if order.data is feed:
                broker.cancel(order)
        for order in list(broker.submitted):
            if order.data is feed:
                broker.submitted.remove(order)
                order.expire()
                broker.notify(order)
        security._delisted = True
        contract: FutureContract = feed._tw_future
        price = _last_close(feed, contract.last_trade_date)
        self.settle_price = price
        pos = broker.getposition(feed)
        q = pos.size
        self.quantity = q
        book = broker.futures
        if not q:
            book.expiries.append(self)
            return
        if self.at_expiry != CLOSE:
            raise FutureExpiryError(
                f"{contract.symbol}: {q:g} contract(s) still held after the last trading date "
                f"{contract.last_trade_date} (no delivery is modelled). Roll before it with a Roller "
                f"(whose rule must leave a session before that date), close the position, or pass "
                f"add_futures(..., at_expiry='close') to close it by force at its last settlement "
                f"({price:g}).")
        m = security.properties.contract_multiplier
        ccy = security.properties.quote_currency
        variation = book.unsettled(security.id, q, price, m)
        # the forced close is a trade out of the contract: it pays the scheme's fee
        fee = float(broker.getcommissioninfo(feed).getcommission(-q, price))
        pnl = (price - pos.price) * q * m
        broker.cashbook.add(ccy, variation - fee)
        broker.portfolio.realized_pnl[security.id] += pnl
        broker.portfolio.total_fees[security.id] += fee
        book.reference.pop(security.id, None)
        pos.set(0, 0.0)
        broker._tw_close_trade(feed, -q, price, pnl)
        self.forced = True
        self.cash = variation - fee
        self.fee = fee
        book.expiries.append(self)


def _last_close(feed, day: _dt.date) -> float:
    n = len(feed)
    if not n:
        return float("nan")
    cutoff = date2num(day) + 1.0
    dt = feed._dtnum[:n]
    k = int(np.searchsorted(dt, cutoff, side="left")) - 1
    return float(feed._arrays["close"][k]) if k >= 0 else float("nan")


class FutureExpiries:
    """The ``CorporateActionProvider`` of a set of futures feeds: each contract's
    ``FutureExpiry`` is handed to the first slice at or after the day following its last
    trading date, with the policy ``at_expiry`` (``"raise"`` or ``"close"``)."""

    def __init__(self, at_expiry: str = RAISE):
        if at_expiry not in (RAISE, CLOSE):
            raise ValueError("at_expiry is 'raise' or 'close'")
        self.at_expiry = at_expiry
        self._pending: list[tuple[float, int]] = []
        self._sorted = True

    def add(self, feed) -> None:
        cutoff = date2num(feed._tw_future.last_trade_date) + 1.0
        self._pending.append((cutoff, feed.security.id))
        self._sorted = False

    def events(self, time) -> Sequence[FutureExpiry]:
        if not self._pending:
            return ()
        if not self._sorted:
            self._pending.sort()
            self._sorted = True
        from .clock import ns2num

        now = float(ns2num(np.int64(time)))
        out = []
        while self._pending and self._pending[0][0] <= now + 1e-9:
            _, sid = self._pending.pop(0)
            out.append(FutureExpiry(sid, np.int64(time), at_expiry=self.at_expiry))
        return out


# -- chains and rolls ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RollOnDate:
    """Hold the nearest contract whose last trading date is more than ``days_before``
    calendar days away (the rule of the rolled series of the catalogue: 7 days)."""

    days_before: int = 7

    def choose(self, chain: FutureChain, day: _dt.date, current: FutureContract | None) -> FutureContract | None:
        for c in chain.contracts:
            if (c.last_trade_date - day).days > self.days_before and (
                    c.first_trade_date is None or c.first_trade_date <= day):
                return c
        return None


@dataclass(frozen=True, slots=True)
class RollOnVolume:
    """Move to the next contract on the first bar its volume exceeds the held contract's
    (read on that bar's close, traded at the next open; never back), and in any case once
    the held contract is within ``days_before`` calendar days of its last trading date.
    The default, 5, leaves a session to fill the roll before the last trading date across a
    weekend and a holiday; a rule that decides on the last trading day itself fills after it,
    and the position meets its expiry (``FutureExpiryError`` by default)."""

    days_before: int = 5

    def choose(self, chain: FutureChain, day: _dt.date, current: FutureContract | None) -> FutureContract | None:
        live = [c for c in chain.contracts if (c.last_trade_date - day).days > self.days_before
                and (c.first_trade_date is None or c.first_trade_date <= day)]
        if not live:
            return None
        if current is None or current not in live:
            # start (or a forced move): the most traded of the two nearest live contracts
            cands = live[:2]
            vols = [chain.volume(c, day) for c in cands]
            return cands[int(np.nanargmax(vols))] if not all(math.isnan(v) for v in vols) else cands[0]
        k = live.index(current)
        if k + 1 < len(live):
            nxt = live[k + 1]
            if chain.volume(nxt, day) > chain.volume(current, day):
                return nxt
        return current


class FutureChain:
    """The contracts of one root, ordered by last trading date, with their feeds."""

    def __init__(self, root: str, contracts: Sequence[FutureContract], feeds: Mapping[str, Any] | None = None):
        self.root = root
        self.contracts = sorted(contracts, key=lambda c: (c.last_trade_date, c.symbol))
        self.feeds = dict(feeds or {})
        self.continuous = None

    def __len__(self):
        return len(self.contracts)

    def __iter__(self):
        return iter(self.contracts)

    def __repr__(self):
        return f"FutureChain({self.root!r}, {len(self.contracts)} contracts)"

    def contract(self, symbol: str) -> FutureContract:
        for c in self.contracts:
            if c.symbol == symbol:
                return c
        raise KeyError(symbol)

    def feed(self, contract) -> Any:
        symbol = contract.symbol if isinstance(contract, FutureContract) else str(contract)
        return self.feeds[symbol]

    def volume(self, contract: FutureContract, day: _dt.date) -> float:
        """The contract's volume on ``day`` (NaN when its feed has no bar that day)."""
        feed = self.feeds.get(contract.symbol)
        if feed is None or not len(feed):
            return float("nan")
        if num2date(feed.datetime[0]).date() != day:
            return float("nan")
        return float(feed.volume[0])

    def front(self, day, days_before: int = 0) -> FutureContract | None:
        return RollOnDate(days_before).choose(self, _as_date(day), None)


@dataclass(slots=True)
class Roll:
    """A roll the ``Roller`` placed: ``quantity`` contracts out of ``old`` and into ``new``,
    as two market orders (each pays its fee and its slippage)."""

    time: float
    old: str
    new: str
    quantity: float
    orders: tuple = ()


class Roller:
    """Holds a target number of contracts of a root in the contract its rule picks, and
    rolls **explicitly**: when the rule moves to the next contract, the position in the old
    one is closed and the same position opened in the new one, by two market orders placed
    on this bar and filled at the next open -- each pays its fee and slippage; the cost of
    the roll is what those two fills cost, not an adjustment of the series.

    In ``next()``: ``self.roller.target(3)`` (long 3), ``target(-2)`` (short 2), ``target(0)``
    (flat). ``rolls`` lists the rolls placed."""

    def __init__(self, strategy, chain: FutureChain, rule=None):
        self.strategy = strategy
        self.chain = chain
        self.rule = rule if rule is not None else RollOnDate()
        self.current: FutureContract | None = None
        self.rolls: list[Roll] = []
        self._orders: list = []

    def active(self) -> FutureContract | None:
        """The contract the rule picks on the strategy's current bar."""
        day = num2date(self.strategy.broker._dtnum).date()
        return self.rule.choose(self.chain, day, self.current)

    def held(self) -> dict[str, float]:
        """Non-zero positions of the chain, by symbol."""
        broker = self.strategy.broker
        out = {}
        for c in self.chain.contracts:
            feed = self.chain.feeds.get(c.symbol)
            if feed is None:
                continue
            q = broker.getposition(feed).size
            if q:
                out[c.symbol] = q
        return out

    def target(self, contracts: float):
        """Place what it takes to hold ``contracts`` in the active contract; returns the
        orders placed (none while earlier ones are still open)."""
        if any(o.alive() for o in self._orders):
            return []
        self._orders = []
        strat = self.strategy
        new = self.active()
        if new is not None and not len(self.chain.feed(new)):
            new = None                                   # not listed yet
        held = self.held()
        placed = []
        rolled_from = None
        for symbol, q in held.items():
            if new is not None and symbol == new.symbol:
                continue
            feed = self.chain.feeds[symbol]
            if feed.security.is_delisted:
                continue
            placed.append(strat.close(data=feed))
            rolled_from = (symbol, q)
        if new is not None:
            self.current = new
            feed = self.chain.feed(new)
            have = held.get(new.symbol, 0.0)
            if contracts != have:
                order = strat.buy(data=feed, size=contracts - have) if contracts > have else \
                    strat.sell(data=feed, size=have - contracts)
                placed.append(order)
                if rolled_from is not None and contracts:
                    self._record(rolled_from[0], new.symbol, contracts, placed)
        placed = [o for o in placed if o is not None]
        self._orders = placed
        return placed

    def _record(self, old, new, quantity, orders):
        strat = self.strategy
        roll = Roll(float(strat.broker._dtnum), old, new, quantity, tuple(orders))
        self.rolls.append(roll)
        book = getattr(strat.broker, "_tw_futures", None)
        if book is not None:
            book.rolls.append(roll)


# -- continuous series ----------------------------------------------------------------------------------


def future_table(frame: pd.DataFrame) -> pd.DataFrame:
    """A daily futures table in the engine's layout: one row per contract and session, with
    ``date``, ``symbol``, ``root``, ``open``, ``high``, ``low``, ``close``, ``volume``,
    ``last_trade_date`` and, when present, ``settlement`` and ``first_trade_date``. The close
    becomes the settlement where one is given (kept as ``last`` the traded close). A table
    with a time of day, or more than one row per contract and session, is refused
    (intraday futures: spec/09 § 10.3)."""
    t = frame.copy()
    t.columns = [str(c).lower() for c in t.columns]
    missing = {"date", "symbol", "root", "close", "last_trade_date"} - set(t.columns)
    if missing:
        raise ValueError(f"futures table: missing columns {sorted(missing)}")
    date = pd.to_datetime(t["date"])
    if (date != date.dt.normalize()).any():
        raise ValueError(f"futures table: {INTRADAY_REFUSED}")
    t["date"] = date
    if t.duplicated(["symbol", "date"]).any():
        raise ValueError(f"futures table: more than one row per contract and session; {INTRADAY_REFUSED}")
    t["last_trade_date"] = pd.to_datetime(t["last_trade_date"]).dt.normalize()
    if "first_trade_date" in t:
        t["first_trade_date"] = pd.to_datetime(t["first_trade_date"]).dt.normalize()
    if "settlement" in t:
        t["last"] = t["close"]
        t["close"] = t["settlement"].where(t["settlement"].notna(), t["close"])
    for col in ("open", "high", "low"):
        if col not in t:
            t[col] = t["close"]
        t[col] = t[col].where(t[col].notna(), t["close"])
    if "volume" not in t:
        t["volume"] = 0.0
    t = t[t["close"].notna()]
    t = t[t["date"] <= t["last_trade_date"]]
    return t.sort_values(["root", "date", "last_trade_date", "symbol"], ignore_index=True)


def continuous(table: pd.DataFrame, rule=None, adjust: str = DIFFERENCE, root: str | None = None) -> pd.DataFrame:
    """The continuous series of one root for the signals, indexed by date: ``open``,
    ``high``, ``low``, ``close``, ``volume`` of the contract held, its ``symbol``, ``rolled``
    (the first day of a new contract) and the cumulative ``adjustment``.

    The contract of day *d* is the one ``rule`` picks on day *d - 1* (the roll is decided
    at the previous close, as the ``Roller`` trades it), so a level never borrows a price
    from a contract chosen later. ``adjust``:

    * ``"difference"``: at each roll, every earlier level moves by ``new - old`` (both
      closes of the decision day): the gaps between contracts vanish, point changes are the
      traded ones; works with negative or zero prices;
    * ``"ratio"``: every earlier level is multiplied by ``new / old``: percentage changes are
      kept; needs positive closes on every roll day (``ValueError`` otherwise: use
      ``"difference"``);
    * ``"none"``: the closes of the contracts held, spliced (the roll gaps stay).

    The last segment is never adjusted: the latest level is the current contract's close.
    """
    if adjust not in (RATIO, DIFFERENCE, NONE):
        raise ValueError("adjust is 'ratio', 'difference' or 'none'")
    t = future_table(table)
    roots = t["root"].unique()
    if root is None:
        if len(roots) != 1:
            raise ValueError(f"several roots in the table, pick one: {sorted(roots)}")
        root = roots[0]
    t = t[t["root"] == root]
    rule = rule if rule is not None else RollOnDate()
    chain = _TableChain(root, t)
    days = sorted(t["date"].unique())
    by_day = {d: g.set_index("symbol") for d, g in t.groupby("date")}
    rows = []
    held: FutureContract | None = None
    pending: FutureContract | None = None
    prev_day = None
    for d in days:
        bars = by_day[d]
        if pending is not None and pending.symbol in bars.index:
            switched = held is not None and pending.symbol != held.symbol
            gap_old = gap_new = float("nan")
            if switched and prev_day is not None:
                prev = by_day[prev_day]
                if held.symbol in prev.index and pending.symbol in prev.index:
                    gap_old = float(prev.at[held.symbol, "close"])
                    gap_new = float(prev.at[pending.symbol, "close"])
            if held is None or switched:
                held = pending
                if switched:
                    rows.append(("__roll__", gap_old, gap_new))
        if held is not None and held.symbol in bars.index:
            b = bars.loc[held.symbol]
            rows.append((d, held.symbol, float(b["open"]), float(b["high"]), float(b["low"]),
                         float(b["close"]), float(b["volume"])))
        chain.day = d
        pending = rule.choose(chain, _as_date(d), held)
        prev_day = d
    return _adjust(rows, adjust)


def _adjust(rows, adjust: str) -> pd.DataFrame:
    """Apply the roll gaps backward from the last segment."""
    records = []
    segment = 0
    gaps = []
    for r in rows:
        if r[0] == "__roll__":
            gaps.append((segment, r[1], r[2]))
            segment += 1
            continue
        records.append((segment,) + r)
    cols = ["segment", "date", "symbol", "open", "high", "low", "close", "volume"]
    out = pd.DataFrame(records, columns=cols)
    if out.empty:
        return out.drop(columns="segment").set_index("date")
    n_seg = segment + 1
    # the adjustment of segment k composes the gaps of every roll after it
    if adjust == RATIO:
        factor = np.ones(n_seg)
        for k, old, new in reversed(gaps):
            if math.isnan(old) or math.isnan(new):
                continue                       # no common day to measure the gap on
            if not (old > 0 and new > 0):
                raise ValueError(f"ratio adjustment needs positive closes at a roll (got {old} -> {new}): "
                                 "use adjust='difference'")
            factor[: k + 1] *= new / old
        f = factor[out["segment"].to_numpy()]
        for col in ("open", "high", "low", "close"):
            out[col] = out[col] * f
        out["adjustment"] = f
    elif adjust == DIFFERENCE:
        shift = np.zeros(n_seg)
        for k, old, new in reversed(gaps):
            if math.isnan(old) or math.isnan(new):
                continue                       # no common day to measure the gap on
            shift[: k + 1] += new - old
        sft = shift[out["segment"].to_numpy()]
        for col in ("open", "high", "low", "close"):
            out[col] = out[col] + sft
        out["adjustment"] = sft
    else:
        out["adjustment"] = 0.0
    seg = out["segment"].to_numpy()
    out["rolled"] = np.concatenate(([False], seg[1:] != seg[:-1]))
    return out.drop(columns="segment").set_index("date")


class _TableChain(FutureChain):
    """A chain read from a table (no feeds): volumes from the table's rows."""

    def __init__(self, root, t: pd.DataFrame):
        firsts = t.groupby("symbol")["date"].min()
        contracts = []
        for sym, g in t.groupby("symbol"):
            r0 = g.iloc[0]
            first = r0["first_trade_date"] if "first_trade_date" in g and pd.notna(
                r0.get("first_trade_date")) else firsts[sym]
            contracts.append(FutureContract(root, str(sym), r0["last_trade_date"], 1.0,
                                            first_trade_date=first))
        super().__init__(root, contracts)
        self._vol = t.set_index(["date", "symbol"])["volume"]
        self.day = None

    def volume(self, contract, day) -> float:
        try:
            return float(self._vol.loc[(pd.Timestamp(day), contract.symbol)])
        except KeyError:
            return float("nan")


class ContinuousData(PandasData):
    """The feed of a continuous series (``continuous``): for indicators and signals only.
    Its security refuses every order (``NotTradable``)."""

    def __init__(self):
        super().__init__()
        self._tw_continuous = True


# -- the engine side ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FutureSpec:
    """The terms shared by every contract of a root."""

    multiplier: float
    tick_size: float = 0.0
    initial_margin: float = 0.0
    maintenance_margin: float | None = None
    commission: float = 0.0             # per contract and side
    currency: str = ACCOUNT_CURRENCY


def add_futures(engine, table: pd.DataFrame, specs: Mapping[str, FutureSpec], roots: Sequence[str] | None = None,
                continuous_adjust: str | None = None, continuous_rule=None,
                on_margin_call: str = LIQUIDATE, at_expiry: str = RAISE) -> dict[str, FutureChain]:
    """Add one ``FutureData`` per contract of a daily futures table (``future_table``
    layout) for each root of ``specs`` (or of ``roots``), the per-contract commission, the
    margin model and the expiries; returns the chain of each root. With
    ``continuous_adjust`` (``"ratio"``, ``"difference"``, ``"none"``), the root's continuous
    series is added too, as a ``ContinuousData`` feed named ``"<root>_cont"`` (``chain.continuous``),
    rolled by ``continuous_rule`` (default ``RollOnDate()``).

    ``at_expiry``: what happens to a position still open after its contract's last trading
    date. ``"raise"`` (default) stops the run with ``FutureExpiryError`` -- a strategy holds
    futures through a ``Roller`` or closes them itself; ``"close"`` closes it by force at
    the last settlement, fee charged, as an explicit choice. Initial and maintenance margins
    are posted from cash only: other positions of the account are not collateral."""
    expiries = FutureExpiries(at_expiry)
    t = future_table(table)
    broker = engine.broker
    book = broker.futures
    book.on_margin_call = on_margin_call
    chains = {}
    for root in (roots if roots is not None else list(specs)):
        spec = specs[root]
        rows_root = t[t["root"] == root]
        if rows_root.empty:
            raise ValueError(f"no rows for root {root!r}")
        contracts, feeds = [], {}
        for sym, rows in rows_root.groupby("symbol", sort=False):
            r0 = rows.iloc[0]
            first = r0["first_trade_date"] if "first_trade_date" in rows and pd.notna(
                r0.get("first_trade_date")) else None
            contract = FutureContract(root, str(sym), r0["last_trade_date"], spec.multiplier, spec.tick_size,
                                      spec.initial_margin, spec.maintenance_margin, spec.currency, first)
            frame = rows.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
            feed = FutureData(dataname=frame, contract=contract)
            engine.adddata(feed, name=str(sym))
            broker.addcommissioninfo(FuturesCommission(commission=spec.commission), name=str(sym))
            expiries.add(feed)
            contracts.append(contract)
            feeds[str(sym)] = feed
        chain = FutureChain(root, contracts, feeds)
        if continuous_adjust is not None:
            cont = continuous(rows_root, rule=continuous_rule, adjust=continuous_adjust, root=root)
            feed = ContinuousData(dataname=cont[["open", "high", "low", "close", "volume"]])
            engine.adddata(feed, name=f"{root}_cont")
            chain.continuous = feed
        chains[root] = chain
    engine.add_corporate_actions(expiries)
    return chains
