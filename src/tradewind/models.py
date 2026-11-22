"""The pluggable reality models (spec/05 § 5) and their oracle defaults.

Every model is a ``typing.Protocol`` with a scalar method -- one security, one order -- as
in QuantConnect Lean (Apache 2.0, read as a specification, see ``NOTICE``): ``IFeeModel``,
``IFillModel``, ``ISlippageModel``, ``IBuyingPowerModel``, ``ISettlementModel``,
``IShortableProvider``, ``IMarginInterestRateModel``. Only the per-bar models (borrow fees,
interest) get optional vector methods (spec/05 § 10, Q5).

``ModelSet.oracle()`` is the default profile (spec/05 § 10, Q1): each model is the behaviour
already implemented and observed on the oracle (``spec/02-semantics.md``), moved behind its
interface without changing one floating-point operation. The compat broker setters
(``setcommission``, ``set_slippage_*``, ``set_coc``, ``set_checksubmit``, ``set_shortcash``)
reconfigure these models.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol, runtime_checkable

import numpy as np

from .order import Order
from .securities import ACCOUNT_CURRENCY, Bar, CashAmount, SecurityId, Symbol, Timestamp

if TYPE_CHECKING:  # pragma: no cover
    from .cashbook import CashBook
    from .market import PortfolioState
    from .securities import Security

_MARKET, _CLOSE, _LIMIT, _STOP, _STOPLIMIT = (Order.Market, Order.Close, Order.Limit, Order.Stop,
                                              Order.StopLimit)

# -- 5.1 fees -------------------------------------------------------------------------------


class FeeParameters(NamedTuple):
    security: Any                       # Security
    order: Any                          # Order
    fill_quantity: float                # signed
    fill_price: float
    time: Timestamp


@runtime_checkable
class FeeModel(Protocol):
    def get_order_fee(self, p: FeeParameters) -> CashAmount: ...


class CommInfoFeeModel:
    """The oracle default: the ``CommInfoBase`` attached to the order at submission prices
    the fill (percent, fixed, or a subclass overriding ``_getcommission`` such as
    pwb-toolbox's ``SpreadImpactCommission``). The scheme reads the fill bar's date itself:
    phase 2 (market data) runs before phase 6 (fills)."""

    __slots__ = ()

    def get_order_fee(self, p: FeeParameters) -> CashAmount:
        return CashAmount(p.order.comminfo.getcommission(p.fill_quantity, p.fill_price), ACCOUNT_CURRENCY)

    def confirm_order_fee(self, p: FeeParameters) -> CashAmount:
        """The commission recorded on the order once the fill is final (the scheme's
        ``confirmexec``); the cash moves by ``get_order_fee``, as the oracle does."""
        return CashAmount(p.order.comminfo.confirmexec(p.fill_quantity, p.fill_price), ACCOUNT_CURRENCY)


# -- 5.2 slippage ---------------------------------------------------------------------------


@runtime_checkable
class SlippageModel(Protocol):
    def get_slippage(self, security: Security, order: Order, reference_price: float,
                     at_open: bool = False) -> float:
        """Adverse price move, in price units, added for a buy and subtracted for a sell.
        ``at_open``: the reference is the bar's open (a market order, a gapped stop)."""
        ...


class CompatSlippage:
    """The oracle default: the compat broker's ``slip_perc`` / ``slip_fixed`` parameters,
    zero unless set; a fill at the open slips only with ``slip_open``."""

    __slots__ = ("fixed", "perc", "slip_limit", "slip_match", "slip_open", "slip_out")

    def __init__(self, perc=0.0, fixed=0.0, slip_open=False, slip_limit=True, slip_match=True,
                 slip_out=False):
        self.perc = perc
        self.fixed = fixed
        self.slip_open = slip_open
        self.slip_limit = slip_limit
        self.slip_match = slip_match
        self.slip_out = slip_out

    def get_slippage(self, security, order, reference_price, at_open=False) -> float:
        if self.perc:
            adj = reference_price * self.perc
        elif self.fixed:
            adj = self.fixed
        else:
            return 0.0
        if at_open and not self.slip_open:
            return 0.0
        return adj


def slipped(slippage: SlippageModel, security, order, price: float, at_open: bool) -> float:
    """``price`` moved against the order by the slippage model; untouched when it is zero."""
    adj = slippage.get_slippage(security, order, price, at_open)
    if not adj:
        return price
    return price + adj if order.isbuy() else price - adj


def slipped_in_bar(slippage: SlippageModel, security, order, price: float, at_open: bool,
                   bar: Bar) -> float | None:
    """``slipped``, then held inside the bar and the order's limit as the compat broker's
    flags say; None: no match at this bar (the order waits).

    Observed on the oracle (probes ``slippage_*``): unless ``slip_out``, a slipped price
    beyond the bar's high or low, or beyond a limit order's own price, is brought back to
    it when ``slip_match`` (or, for a limit order, ``slip_limit``) is set, and otherwise
    the order is not matched at this bar; with ``slip_out`` the slipped price stands, even
    past a limit. A price the model did not move is left alone."""
    p = slipped(slippage, security, order, price, at_open)
    if p == price or not isinstance(slippage, CompatSlippage) or slippage.slip_out:
        return p
    is_limit = order.exectype == _LIMIT
    give = slippage.slip_match or (is_limit and slippage.slip_limit)
    if is_limit:
        lim = order.price
        if (p > lim) if order.isbuy() else (p < lim):
            if not give:
                return None
            p = lim
    if p > bar.high:
        if not give:
            return None
        p = bar.high
    elif p < bar.low and not is_limit:
        # (a limit order is held below the high only: a sell limit slipped under the
        # bar's low fills there, at or above its limit -- probes ``slippage_perc``,
        # ``slippage_fixed``, ``slippage_strict``)
        if not give:
            return None
        p = bar.low
    return p


# -- 5.3 fills ------------------------------------------------------------------------------


class FillParameters(NamedTuple):
    security: Any                       # Security
    order: Any                          # Order
    bar: Bar                            # the bar at ``time``
    placed_bar: Bar | None              # the feed's bar before it (cheat-on-close); None if unused
    time: Timestamp
    slippage: SlippageModel


class FillProposal(NamedTuple):
    quantity: float                     # signed; may be less than the remaining size
    price: float


@runtime_checkable
class FillModel(Protocol):
    def fill(self, p: FillParameters) -> FillProposal | None:
        """None: no fill at this bar (limit not reached); the order stays open."""
        ...


class NextBarFillModel:
    """The oracle default. Market at the bar's open (with cheat-on-close: at the close of
    the feed's previous bar, the one the order was placed on); limit at the limit price
    when the bar trades through it, at the open when it gaps through; stop at the stop when
    touched, at the open when gapped; ``Close`` at the close; the whole size in one fill.
    ``expired`` is checked by the broker first."""

    __slots__ = ("coc",)

    def __init__(self, coc: bool = False):
        self.coc = coc

    @property
    def needs_placed_bar(self) -> bool:
        return self.coc

    def expired(self, order: Order, bar: Bar) -> bool:
        return order.valid is not None and bar.time > order.valid

    def fill(self, p: FillParameters) -> FillProposal | None:
        order = p.order
        # ``coc=False`` given to ``buy``/``sell`` (kept in ``order.info``) turns the broker's
        # cheat-on-close off for that order; ``coc=True`` does not turn it on (observed on
        # the oracle, probe ``order_coc``)
        coc = self.coc and (not order.info or bool(order.info.get("coc", True)))
        if coc and order.exectype == _MARKET and p.placed_bar is not None:
            price = slipped(p.slippage, p.security, order, p.placed_bar.close, True)
        else:
            price = self.fill_price(order, p.bar, p.slippage, p.security)
        if price is None:
            return None
        return FillProposal(order.size, price)

    @staticmethod
    def fill_price(order: Order, bar: Bar, slippage: SlippageModel, security=None) -> float | None:
        o, h, lo, c = bar.open, bar.high, bar.low, bar.close
        et = order.exectype
        if et == _MARKET:
            return slipped_in_bar(slippage, security, order, o, True, bar)
        if et == _CLOSE:
            return c
        if et == _LIMIT:
            # a limit reached through the bar fills at its price, never slipped; one the
            # open already satisfies fills at the open, slipped like a market order
            # (probes ``slippage_*``)
            lim = order.price
            if order.isbuy():
                if o <= lim:
                    return slipped_in_bar(slippage, security, order, o, True, bar)
                return lim if lo <= lim else None
            if o >= lim:
                return slipped_in_bar(slippage, security, order, o, True, bar)
            return lim if h >= lim else None
        if et == _STOP:
            stp = order.price
            if order.isbuy():
                if o >= stp:
                    return slipped_in_bar(slippage, security, order, o, True, bar)
                return slipped_in_bar(slippage, security, order, stp, False, bar) if h >= stp else None
            if o <= stp:
                return slipped_in_bar(slippage, security, order, o, True, bar)
            return slipped_in_bar(slippage, security, order, stp, False, bar) if lo <= stp else None
        if et == _STOPLIMIT:
            stp, lim = order.price, order.pricelimit
            if not order.triggered:
                hit = (h >= stp) if order.isbuy() else (lo <= stp)
                if not hit:
                    return None
                order.triggered = True
            if order.isbuy():
                return lim if lo <= lim else None
            return lim if h >= lim else None
        raise NotImplementedError(f"execution type {order.getordername()} is not supported")


# -- 5.4 buying power -----------------------------------------------------------------------


@dataclass(slots=True)
class BatchState:
    running_cash: float                                  # carried through the batch by the broker
    pseudo_positions: dict[SecurityId, float]            # position sizes as the batch moves them
    time: Timestamp = 0
    scratch: dict | None = None                          # a model's own running state for the batch


class BuyingPowerCheck(NamedTuple):
    accepted: bool
    cash_consumed: float                # what the broker subtracts from running_cash
    reason: str = ""


@runtime_checkable
class BuyingPowerModel(Protocol):
    def check_order(self, security: Security, order: Order, check_price: float | None,
                    state: BatchState) -> BuyingPowerCheck: ...
    def check_fill(self, security: Security, order: Order, fill: FillProposal,
                   cash: float) -> FillProposal | None:
        """The part of the proposal the cash allows (None: Margin)."""
        ...
    def quantity_for_target_value(self, security: Security, target_value: float,
                                  current_value: float, price: float) -> float: ...
    def leverage(self, security: Security) -> float: ...
    def maintenance_margin(self, security: Security, quantity: float, price: float) -> float: ...
    def initial_margin(self, security: Security, quantity: float, price: float) -> float: ...


def leveraged_stock_flow(size, price, pos_size, pos_price, leverage, comminfo) -> float:
    """Cash flow, before fees, of trading ``size`` of a stock-like asset whose scheme has
    ``leverage`` != 1, against a position of ``pos_size`` entered at ``pos_price``.

    Observed on the oracle (probe ``stock_leverage``; corpus ``structural_convexity…``,
    whose strategy sets ``comminfo.p.leverage = 2.0``): leverage applies to **longs only**.
    Buying debits ``size * price / leverage``; selling out of a long credits back the
    margin at the entry price plus the P&L, ``(q * entry / leverage + P&L)`` grouped as
    written; shorts move the full ``size * price`` either way, as without leverage. A
    reversal is its closing part then its opening part.
    """
    flow = 0.0
    if size > 0:
        if pos_size < 0:                       # buying back a short: full price
            q = min(size, -pos_size)
            flow -= q * price
            size -= q
        if size > 0:                           # opening or adding to a long: the margin
            flow -= size * price / leverage
        return flow
    q = min(-size, pos_size) if pos_size > 0 else 0
    if q > 0:                                  # selling out of a long: margin back + P&L
        flow += (q * pos_price / leverage + comminfo.profitandloss(q, pos_price, price))
    rest = -size - q
    if rest > 0:                               # opening or adding to a short: full price
        flow += rest * price
    return flow


def order_fee(fee_model, security, order, size, price, time) -> float:
    """``fee_model.get_order_fee(...).amount``; with the oracle default, the scheme's
    ``getcommission`` called directly (the same call, without the two tuples)."""
    if type(fee_model) is CommInfoFeeModel:
        return order.comminfo.getcommission(size, price)
    return fee_model.get_order_fee(FeeParameters(security, order, size, price, time)).amount


def confirm_fee(fee_model, security, order, size, price) -> float:
    """The commission recorded on the order (``confirm_order_fee`` when the model has one)."""
    if type(fee_model) is CommInfoFeeModel:
        return order.comminfo.confirmexec(size, price)
    confirm = getattr(fee_model, "confirm_order_fee", None) or fee_model.get_order_fee
    return confirm(FeeParameters(security, order, size, price, 0)).amount
def _security_multiplier(security) -> float:
    """The contract multiplier of a security (1.0 for a plain feed)."""
    props = getattr(security, "properties", None)
    return 1.0 if props is None else props.contract_multiplier


def _multiplier(order) -> float:
    """The contract multiplier of the order's security."""
    return _security_multiplier(getattr(order.data, "security", None))


def _fee(security, order, size, price, time) -> float:
    return order_fee(security.models.fee, security, order, size, price, time)


class CompatCashBuyingPower:
    """The oracle default: the five observed rules of ``02-semantics``.

    * the batch is checked in submission order, each order priced at its submission bar's
      close (limit/stop at their own price);
    * ``cash_consumed`` is the order's cash flow **even when refused** (the observed deficit
      carry); an order is accepted iff the running cash stays >= 0 after it;
    * at the fill, a reducing or closing order always passes, an opening one -- a short
      sale included -- is re-checked at the fill price, a reversal fills its closing part and refuses its opening part when
      the cash left cannot cover it (Partial then Margin);
    * ``order_target_value`` truncates the value gap toward zero;
    * shorts need no margin (the oracle's ``shortcash``).
    """

    __slots__ = ("checksubmit", "shortcash")

    def __init__(self, checksubmit: bool = True, shortcash: bool = True):
        self.checksubmit = checksubmit
        self.shortcash = shortcash

    @staticmethod
    def cash_delta(order: Order, price: float, pos_size: float, fee: float,
                   pos_price: float = 0.0) -> float:
        """Cash flow of filling ``order`` at ``price`` against a position of ``pos_size``
        (negative when cash goes out), fee included."""
        comminfo = order.comminfo
        size = order.size
        if comminfo.stocklike:
            m = _multiplier(order)
            leverage = comminfo.get_leverage()
            if leverage != 1.0:
                flow = leveraged_stock_flow(size, price, pos_size, pos_price, leverage, comminfo)
                return (flow if m == 1.0 else flow * m) - fee
            if m != 1.0:
                return -size * price * m - fee
            return -size * price - fee
        opening = (pos_size == 0) or ((pos_size > 0) == (size > 0))
        if opening:
            return -comminfo.getoperationcost(size, price) - fee
        return -fee

    def check_order(self, security, order, check_price, state: BatchState) -> BuyingPowerCheck:
        sid = security.id
        pseudo = state.pseudo_positions
        pos_size = pseudo.get(sid)
        if pos_size is None:
            # the real position, read through the broker (which registers it, as a fill would)
            pos_size = pseudo[sid] = security._broker.getposition(security.feed).size
        if check_price is None:
            return BuyingPowerCheck(True, 0.0)
        fee = _fee(security, order, order.size, check_price, state.time)
        pos_price = security._broker.getposition(security.feed).price
        delta = self.cash_delta(order, check_price, pos_size, fee, pos_price)
        pseudo[sid] = pos_size + order.size
        if order._check and self.checksubmit and state.running_cash + delta < 0:
            return BuyingPowerCheck(False, -delta, "running cash below zero")
        return BuyingPowerCheck(True, -delta)

    def check_fill(self, security, order, fill: FillProposal, cash: float,
                   time: Timestamp = 0) -> FillProposal | None:
        comminfo = order.comminfo
        size, price = fill.quantity, fill.price
        position = security._broker.getposition(security.feed)
        pos_size = position.size
        opening = (pos_size == 0) or ((pos_size > 0) == (size > 0))
        if opening:
            # Observed on the oracle (probe ``short_fill_deficit``; corpus
            # ``investing_just_another_hype…`` 2024-01-02, LCID): an opening short sale is
            # re-checked too -- its proceeds less its fee must leave the cash >= 0, which
            # only fails when closing buys earlier in the same pass drove the cash below
            # zero (a closing or reducing order always fills).
            fee = _fee(security, order, size, price, time)
            if not cash + self.cash_delta(order, price, pos_size, fee, position.price) >= 0:
                return None
        reversing = pos_size and ((pos_size > 0) != (size > 0)) and abs(size) > abs(pos_size)
        if reversing and comminfo.stocklike and comminfo.get_leverage() != 1.0:
            closed = -pos_size
            opened = size - closed
            comm_closed = _fee(security, order, closed, price, time)
            comm_opened = _fee(security, order, opened, price, time)
            lev = comminfo.get_leverage()
            cash_after_close = cash + leveraged_stock_flow(closed, price, pos_size, position.price, lev,
                                                           comminfo) - comm_closed
            if cash_after_close + leveraged_stock_flow(opened, price, 0, 0.0, lev, comminfo) - comm_opened < 0:
                return FillProposal(closed, price)
            return fill
        if reversing and comminfo.stocklike:
            # observed (breaking_bad_trends, 1993-04-02): the closing part always fills; the
            # opening part only if the cash left after closing covers it
            closed = -pos_size
            opened = size - closed
            comm_closed = _fee(security, order, closed, price, time)
            comm_opened = _fee(security, order, opened, price, time)
            m = _security_multiplier(security)
            unit = price if m == 1.0 else price * m
            cash_after_close = cash - closed * unit - comm_closed
            if cash_after_close - opened * unit - comm_opened < 0:
                return FillProposal(closed, price)
        return fill

    def quantity_for_target_value(self, security, target_value, current_value, price) -> float:
        """Signed size closing the value gap, truncated toward zero by the scheme's
        ``getsize`` (observed: the gap is truncated, not the target size)."""
        comminfo = security.comminfo
        m = _security_multiplier(security)
        if m != 1.0:
            price = price * m                  # the value of one contract
        if target_value > current_value:
            return comminfo.getsize(price, target_value - current_value)
        if target_value < current_value:
            return -comminfo.getsize(price, current_value - target_value)
        return 0

    def leverage(self, security) -> float:
        return float(security.comminfo.get_leverage())

    def maintenance_margin(self, security, quantity, price) -> float:
        return 0.0

    def initial_margin(self, security, quantity, price) -> float:
        return float(security.comminfo.getoperationcost(quantity, price))


# -- 5.5 settlement -------------------------------------------------------------------------


@runtime_checkable
class SettlementModel(Protocol):
    def apply_funds(self, security: Security, amount: CashAmount, time: Timestamp,
                    cashbook: CashBook) -> None: ...
    def scan(self, time: Timestamp, cashbook: CashBook) -> None: ...
    def unsettled(self) -> CashAmount: ...


class ImmediateSettlement:
    """The oracle default: proceeds are available at the fill (T+0)."""

    __slots__ = ()
    noop_scan = True

    def apply_funds(self, security, amount: CashAmount, time, cashbook) -> None:
        cashbook.add(amount.currency, amount.amount)

    def scan(self, time, cashbook) -> None:
        return None

    def unsettled(self) -> CashAmount:
        return CashAmount(0.0, ACCOUNT_CURRENCY)


# -- 5.6 shortable --------------------------------------------------------------------------


@runtime_checkable
class ShortableProvider(Protocol):
    def shortable_quantity(self, symbol: Symbol, time: Timestamp) -> float | None: ...  # None: unlimited
    def fee_rate(self, symbol: Symbol, time: Timestamp) -> float: ...     # annual borrow fee
    def rebate_rate(self, symbol: Symbol, time: Timestamp) -> float: ...


class AlwaysShortable:
    """The oracle default: unlimited, zero fee, zero rebate."""

    __slots__ = ()
    noop_fees = True

    def shortable_quantity(self, symbol, time) -> float | None:
        return None

    def fee_rate(self, symbol, time) -> float:
        return 0.0

    def rebate_rate(self, symbol, time) -> float:
        return 0.0

    def fee_rates(self, ids: np.ndarray, time) -> np.ndarray:
        return np.zeros(len(ids))


# -- 5.7 margin interest --------------------------------------------------------------------


@runtime_checkable
class MarginInterestRateModel(Protocol):
    def accrue(self, cashbook: CashBook, portfolio: PortfolioState, time: Timestamp) -> float:
        """Interest charged since the last call (positive: paid by the account)."""
        ...


class NoInterest:
    """The oracle default: nothing accrues, negative cash costs nothing."""

    __slots__ = ()
    noop_accrue = True

    def accrue(self, cashbook, portfolio, time) -> float:
        return 0.0


# -- 5.8 corporate actions ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbolChange:
    security: SecurityId
    time: Timestamp
    old: Symbol
    new: Symbol
    rank = 0


@dataclass(frozen=True, slots=True)
class Split:
    security: SecurityId
    time: Timestamp
    ratio: float                        # new shares per old
    rank = 1


@dataclass(frozen=True, slots=True)
class Dividend:
    security: SecurityId
    time: Timestamp
    amount: float                       # cash per share
    pay_time: Timestamp | None = None
    rank = 2


@dataclass(frozen=True, slots=True)
class Delisting:
    security: SecurityId
    time: Timestamp
    last_price: float | None = None     # None: the security's last close
    liquidate: bool | None = None       # None: the model's policy ("carry" by default)
    haircut: float = 0.0                # fraction of the price lost at the delisting (0.3: -30 %)
    rank = 3


CorporateAction = Split | Dividend | SymbolChange | Delisting

#: How a feed's prices treat corporate actions (Lean ``DataNormalizationMode``), declared per
#: feed with ``Engine.adddata(..., normalization=...)``. It decides which actions move the
#: holdings: on ``"raw"`` prices a split scales the position and a dividend pays cash; on
#: ``"split_adjusted"`` prices the split is already in the series (only the dividend pays);
#: on ``"adjusted"`` prices (splits and dividends back-adjusted, a total-return series) both
#: are in the series and neither moves the holdings. ``None`` (undeclared) applies every
#: action, as a feed carrying events always did. Delistings and symbol changes apply in every
#: mode.
NORMALIZATIONS = ("raw", "split_adjusted", "adjusted")


@runtime_checkable
class CorporateActionProvider(Protocol):
    def events(self, time: Timestamp) -> Sequence[CorporateAction]: ...


class CombinedActions:
    """Several providers as one: their events at a time, in the providers' order."""

    def __init__(self, *providers: CorporateActionProvider):
        self.providers = list(providers)

    def events(self, time) -> Sequence[CorporateAction]:
        out: list = []
        for p in self.providers:
            out.extend(p.events(time))
        return out


class StaticCorporateActions:
    """A provider over a fixed list of actions, each attached to the slice of its time."""

    def __init__(self, actions: Sequence[CorporateAction] = ()):
        self._by_time: dict[int, list] = {}
        for a in actions:
            self._by_time.setdefault(int(a.time), []).append(a)

    def events(self, time) -> Sequence[CorporateAction]:
        return self._by_time.get(int(time), ())


@runtime_checkable
class CorporateActionModel(Protocol):
    def apply(self, action: CorporateAction, broker) -> None: ...


class OracleCorporateActions:
    """Applies an action to holdings and open orders at the start of its bar, before any
    fill (spec/05 § 10, Q2). With the oracle's data no action ever arrives (prices are
    adjusted upstream), so this only acts on a feed that carries events.

    * symbol change: the security takes its new symbol;
    * split of ratio ``r``: quantity × r, average price ÷ r, open orders' sizes × r and
      prices ÷ r;
    * dividend: cash per share × quantity credited on the ex-date through the security's
      settlement model;
    * delisting: ``"carry"`` (the oracle: the position stays, marked at its last close) or
      ``"liquidate"`` (sold at ``last_price`` or the last close, less the action's
      ``haircut``, without fee; open orders on it end; the trade closes); an action's own
      ``liquidate`` overrides the policy.

    ``dividends="pay_date"`` credits the cash on the dividend's ``pay_time`` instead of the
    ex-date: the entitlement is fixed on the ex-date and carried as a receivable, counted in
    the portfolio value, until it is paid (a dividend without ``pay_time`` pays on the
    ex-date). The feed's declared normalization (``NORMALIZATIONS``) decides whether a split
    or a dividend moves the holdings at all.
    """

    __slots__ = ("delisting", "dividends")

    def __init__(self, delisting: str = "carry", dividends: str = "ex_date"):
        if delisting not in ("carry", "liquidate"):
            raise ValueError("delisting policy is 'carry' or 'liquidate'")
        if dividends not in ("ex_date", "pay_date"):
            raise ValueError("dividends are paid on the 'ex_date' or the 'pay_date'")
        self.delisting = delisting
        self.dividends = dividends

    def apply(self, action, broker) -> None:
        security = broker.securities[action.security]
        if isinstance(action, SymbolChange):
            security.symbol = action.new
        elif isinstance(action, Split):
            if security.normalization in (None, "raw"):
                self._split(security, action.ratio, broker)
        elif isinstance(action, Dividend):
            if security.normalization != "adjusted":
                self._dividend(security, action, broker)
        elif isinstance(action, Delisting):
            self._delist(security, action, broker)
        elif hasattr(action, "apply_to"):
            # an action that knows how to apply itself (an option's expiry, spec/06 § 5.4)
            action.apply_to(broker)
        else:
            raise TypeError(f"unknown corporate action {action!r}")

    @staticmethod
    def _split(security, ratio, broker) -> None:
        pos = broker.getposition(security.feed)
        if pos.size:
            pos.set(pos.size * ratio, pos.price / ratio)
        for order in broker.open_orders():
            if order.data is not security.feed:
                continue
            order.size = order.size * ratio
            order.created.remsize = order.created.remsize * ratio
            order.executed.remsize = order.executed.remsize * ratio
            if order.price is not None:
                order.price = order.price / ratio
            if order.pricelimit is not None:
                order.pricelimit = order.plimit = order.pricelimit / ratio
        # the open trade counts the same holding in new shares: size x r, price / r
        trade = broker.trades.get(security.feed)
        if trade is not None and trade.isopen:
            trade.size = trade.size * ratio
            trade.price = trade.price / ratio

    def _dividend(self, security, action, broker) -> None:
        pos = broker.getposition(security.feed)
        if not pos.size:
            return
        amount = pos.size * action.amount * security.properties.contract_multiplier
        currency = security.properties.quote_currency
        broker.portfolio.total_dividends[security.id] += amount
        if self.dividends == "pay_date" and action.pay_time is not None and action.pay_time > action.time:
            broker._tw_receive(int(action.pay_time), CashAmount(amount, currency), security)
            return
        security.models.settlement.apply_funds(security, CashAmount(amount, currency), action.time,
                                               broker.cashbook)

    def _delist(self, security, action, broker) -> None:
        security._delisted = True
        liquidate = action.liquidate if action.liquidate is not None else self.delisting == "liquidate"
        if not liquidate:
            return
        feed = security.feed
        # orders left on the security end: an accepted one is cancelled, one still waiting
        # for its batch acceptance expires (there is no market left to fill it)
        for order in list(broker.pending):
            if order.data is feed:
                broker.cancel(order)
        for order in list(broker.submitted):
            if order.data is feed:
                broker.submitted.remove(order)
                order.expire()
                broker.notify(order)
        pos = broker.getposition(feed)
        q = pos.size
        if not q:
            return
        price = action.last_price if action.last_price is not None else security.price
        price = price * (1.0 - action.haircut)
        m = security.properties.contract_multiplier
        pnl = (price - pos.price) * q * m
        broker.cashbook.add(security.properties.quote_currency, q * price * m)
        broker.portfolio.realized_pnl[security.id] += pnl
        pos.set(0, 0.0)
        broker._tw_close_trade(feed, -q, price, pnl)


# -- the bundle -----------------------------------------------------------------------------


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
    def oracle(cls, coc: bool = False, checksubmit: bool = True, shortcash: bool = True,
               slip_perc: float = 0.0, slip_fixed: float = 0.0, slip_open: bool = False,
               slip_limit: bool = True, slip_match: bool = True, slip_out: bool = False) -> ModelSet:
        """Everything observed in ``02-semantics``: the default profile."""
        return cls(
            fee=CommInfoFeeModel(),
            fill=NextBarFillModel(coc=coc),
            slippage=CompatSlippage(slip_perc, slip_fixed, slip_open, slip_limit, slip_match, slip_out),
            buying_power=CompatCashBuyingPower(checksubmit=checksubmit, shortcash=shortcash),
            settlement=ImmediateSettlement(),
            shortable=AlwaysShortable(),
            margin_interest=NoInterest(),
            corporate_actions=OracleCorporateActions(),
        )

    @classmethod
    def realistic(cls, costs=None, **options) -> ModelSet:
        """The realistic profile of chantier 4 (``tradewind.realistic``): spread per
        security and year from ``costs`` (a ``CostTable``) charged as a fee, fills capped
        at 10 % of the bar's volume, a Reg T margin account (50 % / 25 %) without the
        refused-order carry, 0.25 % a year to borrow a short, US equity settlement (T+3,
        T+2, T+1). Every choice is an option of ``realistic_models``; opt-in, the default
        stays ``oracle()``."""
        from .realistic import realistic_models

        return realistic_models(cls, costs, **options)

    def replace(self, **changes) -> ModelSet:
        """A copy with some models swapped (the others stay the same instances)."""
        return _dc_replace(self, **changes)

    def __iter__(self):
        return (getattr(self, f.name) for f in fields(self))


__all__ = [
    "NORMALIZATIONS",
    "AlwaysShortable", "BatchState", "BuyingPowerCheck", "BuyingPowerModel", "CombinedActions",
    "CommInfoFeeModel",
    "CompatCashBuyingPower", "CompatSlippage", "CorporateAction", "CorporateActionModel",
    "CorporateActionProvider", "Delisting", "Dividend", "FeeModel", "FeeParameters", "FillModel",
    "FillParameters", "FillProposal", "ImmediateSettlement", "MarginInterestRateModel", "ModelSet",
    "NextBarFillModel", "NoInterest", "OracleCorporateActions", "SettlementModel", "ShortableProvider",
    "SlippageModel", "Split", "StaticCorporateActions", "SymbolChange", "slipped",
]
