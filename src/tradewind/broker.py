"""The simulated broker: cash, positions, order execution, commissions, notifications.

``BackBroker`` (also ``SimulatedBroker``) implements the ``Broker`` protocol of spec/05 § 6.5
as phases 3 to 8 of a slice -- corporate actions, accruals, acceptance, fills, transfers,
mark -- and delegates every reality decision to the ``ModelSet`` of the security concerned
(spec/05 § 5): the fee model prices, the fill model (with its slippage model) proposes, the
buying-power model accepts and re-checks, the settlement model moves the cash. With
``ModelSet.oracle()``, the default, the behaviour is the compat one of
``spec/02-semantics.md``:

an order accepted during bar *t* executes against bar *t+1* of its feed; a market order at
that bar's open, ``Order.Close`` at its close, limit and stop orders against its range with
the open as the cap when the price gapped through. With ``set_coc(True)`` a market order
fills at the close of the bar it was placed on. An order whose cost the cash cannot cover
is ``Margin`` and never fills.

Notifications are events on the broker's ``EventBus``, delivered at phase 12.
"""

from __future__ import annotations

import collections
import math
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import numpy as np

from .cashbook import CashBook
from .clock import ALWAYS_OPEN, TimeSlice
from .comminfo import CommInfoBase, CommissionInfo
from .events import CorporateActionEvent, EventBus, OrderEvent, TradeEvent
from .market import MarketState, PortfolioState
from .models import (
    BatchState,
    FillParameters,
    FillProposal,
    ImmediateSettlement,
    ModelSet,
    NextBarFillModel,
    confirm_fee,
    order_fee,
    slipped,
)
from .order import BuyOrder, Order, SellOrder
from .params import ParamsBase
from .position import Position
from .securities import ACCOUNT_CURRENCY, Bar, CashAmount, Security, SecurityHolding, Timestamp
from .trade import Trade

_MARKET = Order.Market


@runtime_checkable
class Broker(Protocol):
    """What the engine needs from a broker, simulated or live (spec/05 § 6.5)."""

    def submit(self, order: Order) -> Order: ...
    def cancel(self, order: Order) -> bool: ...
    def on_slice(self, s: TimeSlice) -> None: ...          # phases 3 to 8
    @property
    def cash(self) -> float: ...                            # the account currency's balance
    def value(self) -> float: ...
    def holding(self, security: int) -> SecurityHolding: ...
    def open_orders(self) -> Sequence[Order]: ...


class BrokerBase(ParamsBase):
    params = (("commission", None),)

    def __init__(self):
        self.comminfo = {None: self.p.commission or CommInfoBase()}
        self._comminfo_version = 0

    def getcommissioninfo(self, data):
        name = getattr(data, "_name", None)
        if name in self.comminfo:
            return self.comminfo[name]
        return self.comminfo[None]

    def setcommission(self, commission=0.0, margin=None, mult=1.0, commtype=None, percabs=True,
                      stocklike=False, interest=0.0, interest_long=False, leverage=1.0,
                      automargin=False, name=None):
        comm = CommissionInfo(commission=commission, margin=margin, mult=mult, commtype=commtype,
                              stocklike=stocklike, percabs=percabs, interest=interest,
                              interest_long=interest_long, leverage=leverage, automargin=automargin)
        self.comminfo[name] = comm
        self._comminfo_version += 1

    def addcommissioninfo(self, comminfo, name=None):
        self.comminfo[name] = comminfo
        self._comminfo_version += 1


class _PositionBook(dict):
    """``broker.positions``: a ``Position`` per feed, created on first access, in the order
    the feeds were first touched -- the order the mark sums them in (the oracle's)."""

    def __init__(self, broker):
        super().__init__()
        self._broker = broker
        self.order: list[int] = []       # security ids, in insertion order

    def __missing__(self, data):
        pos = Position()
        security = self._broker._security(data)
        pos._attach(self._broker.portfolio, security.id)
        self[data] = pos
        self.order.append(security.id)
        return pos


def _plain_comminfo(ci) -> bool:
    """A stock-like scheme valued by the base formulas: the mark can vectorise it."""
    cls = type(ci)
    return bool(ci.stocklike and cls.getvalue is CommInfoBase.getvalue
                and cls.profitandloss is CommInfoBase.profitandloss)


def _mark_terms(comminfo, pos, price):
    """A position's terms in the portfolio value, added in this order to a running sum
    that starts at zero; the value is ``cash + sum`` (not ``cash + v1 + v2 ...``).

    Observed on the oracle (probes ``value_terms``, 893 of 904 marks bit for bit, against
    164 for the cash-first sequential sum): a long counts its market value ``size *
    close``; a short its entry value then its open P&L; a long under a leveraged
    stock-like scheme its margin ``size * entry / leverage`` then its open P&L (114 of 120).
    Shorts count ``size * close`` too (101 of 106 marks on a corpus long/short book,
    against 89 for entry value then P&L). The positions are taken in the order they were
    first touched (fewer last-bit differences on the corpus than the feeds' order).
    A scheme with its own ``getvalue`` gives one term. At a close of zero or below, the
    leveraged long counts ``size * close`` like any other (probe ``levered_negative_close``;
    corpus ``the_best_defensive_strategies…`` on 2020-04-20, crude oil at -37.63)."""
    if not _plain_comminfo(comminfo):
        return (comminfo.getvalue(pos, price), 0.0)
    size, entry = pos.size, pos.price
    lev = comminfo.get_leverage()
    if size < 0 or lev == 1.0 or not price > 0.0:
        return (size * price, 0.0)
    return (size * entry / lev, comminfo.profitandloss(size, entry, price))


class BackBroker(BrokerBase):
    params = (
        ("cash", 10000.0),
        ("checksubmit", True),
        ("coc", False),
        ("coo", False),
        ("slip_perc", 0.0),
        ("slip_fixed", 0.0),
        ("slip_open", False),
        ("slip_match", True),
        ("slip_limit", True),
        ("slip_out", False),
        ("shortcash", True),
        ("fundstartval", 100.0),
        ("fundmode", False),
    )

    def __init__(self):
        super().__init__()
        p = self.p
        self.cashbook = CashBook(ACCOUNT_CURRENCY, float(p.cash))
        self.startingcash = float(p.cash)
        self.models = ModelSet.oracle(coc=p.coc, checksubmit=p.checksubmit, shortcash=p.shortcash,
                                      slip_perc=p.slip_perc, slip_fixed=p.slip_fixed, slip_open=p.slip_open,
                                      slip_limit=p.slip_limit, slip_match=p.slip_match, slip_out=p.slip_out)
        self.hours = ALWAYS_OPEN
        self.securities: list[Security] = []
        self._model_overrides: dict = {}
        self.portfolio = PortfolioState(0)
        self.market: MarketState | None = None
        self.bus = EventBus()
        self.positions = _PositionBook(self)
        self.orders: list[Order] = []          # every order ever submitted
        self.submitted: collections.deque = collections.deque()  # submitted, accepted on the next bar
        self.pending: collections.deque = collections.deque()  # accepted, not yet executed
        self.trades: dict = {}
        self.margin_call_model = None
        self._cash_addition = []
        self._coo = p.coo
        self._tw_env = None
        self._now: Timestamp = np.int64(0)
        self._dtnum = 0.0
        self._accruals = False
        self._prev_accrual = None
        self._mark_cache = None
        self._value = self.cash
        self._tw_receivables: list = []    # (pay time, CashAmount, security): dividends due
        # set by the engine for the length of a run when nobody listens (``Engine.run``):
        # the order / trade events are then not built at all
        self._tw_quiet_orders = False
        self._tw_quiet_trades = False
        # multi-currency and delayed settlement (``set_conversion``, ``set_properties``,
        # ``set_profile``): off by default, and then the mark is the oracle's
        self._fx_sources: dict = {}          # currency -> (feed, invert)
        self._foreign = False                # a security is quoted outside the account currency
        self._multi_cash = False             # the mark adds foreign balances and unsettled cash

    # -- cash (the account currency's balance of the cash book) -------------------------
    @property
    def cash(self) -> float:
        return self.cashbook.account.amount

    @cash.setter
    def cash(self, value: float) -> None:
        self.cashbook.account.amount = value

    def value(self) -> float:
        return self.get_value()

    def holding(self, security: int) -> SecurityHolding:
        return self.securities[security].holding

    def open_orders(self) -> list[Order]:
        return list(self.submitted) + list(self.pending)

    # -- securities ---------------------------------------------------------------------
    def register(self, data) -> Security:
        """The security of ``data``, created (next ``SecurityId``) on first sight."""
        security = getattr(data, "security", None)
        if security is not None and security._broker is self:
            return security
        sid = len(self.securities)
        models = self._model_overrides.get(id(data), self.models)
        name = getattr(data, "_name", "") or f"data{sid}"
        # a feed may carry its contract terms (an option's multiplier of 100, spec/06 § 5)
        props = getattr(data, "symbol_properties", None)
        security = Security(sid, name, data, models, self.hours, properties=props, broker=self)
        self.securities.append(security)
        self.portfolio.grow(len(self.securities))
        self.portfolio.multiplier[sid] = security.properties.contract_multiplier
        security._portfolio = self.portfolio
        security._market = self.market
        data.security = security
        self._mark_cache = None
        if security.properties.quote_currency != self.cashbook.account_currency:
            self.cashbook.ensure(security.properties.quote_currency)
            self._refresh_cash_flags()
        return security

    _security = register

    def set_models(self, data, models: ModelSet | None = None, **changes) -> ModelSet:
        """Give ``data``'s security its own models: a whole ``ModelSet`` or some models
        swapped in the broker's default set (spec/05 § 5: any single model can be
        overridden per security)."""
        base = models if models is not None else self.models
        chosen = base.replace(**changes) if changes else base
        self._model_overrides[id(data)] = chosen
        security = getattr(data, "security", None)
        if security is not None and security._broker is self:
            security.models = chosen
        self._accruals = self._needs_accruals()
        self._refresh_cash_flags()
        return chosen

    def set_profile(self, profile="realistic", margin_call: bool | None = None, **options) -> ModelSet:
        """Install a model profile as the default of every security without its own models:
        ``"realistic"`` (``ModelSet.realistic(**options)``), ``"oracle"``, or a ``ModelSet``.
        ``margin_call`` (default: when the buying-power model has a maintenance margin, as
        ``MarginAccount`` does) adds the phase 9 liquidation model. Call it before ``run``;
        the compat setters (``set_coc``, ``set_checksubmit``...) still reach the new set."""
        if isinstance(profile, ModelSet):
            models = profile
        elif profile == "realistic":
            models = ModelSet.realistic(**options)
        elif profile == "oracle":
            models = ModelSet.oracle(**options)
        else:
            raise ValueError("profile is 'realistic', 'oracle' or a ModelSet")
        old = self.models
        self.models = models
        for security in self.securities:
            if security.models is old and id(security.feed) not in self._model_overrides:
                security.models = models
        from .realistic import MarginAccount, MarginCallModel

        if margin_call is None:
            margin_call = isinstance(models.buying_power, MarginAccount)
        if margin_call:
            self.margin_call_model = MarginCallModel()
        else:
            self.margin_call_model = None
        self._accruals = self._needs_accruals()
        self._refresh_cash_flags()
        return models

    # -- currencies ---------------------------------------------------------------------
    def set_account_currency(self, currency: str) -> None:
        """The currency of the cash, the value and ``getcash()`` (USD by default). Call it
        before ``run``; the starting cash keeps its amount, now in ``currency``."""
        if currency == self.cashbook.account_currency:
            return
        old = self.cashbook
        book = CashBook(currency, old.account.amount)
        for cash in old:
            if cash is not old.account:
                book.ensure(cash.currency, cash.conversion_rate, cash.conversion_source).amount += cash.amount
        self.cashbook = book
        for security in self.securities:
            if security.properties.quote_currency != currency:
                book.ensure(security.properties.quote_currency)
        self._refresh_cash_flags()

    def set_properties(self, data, properties) -> None:
        """Contract terms of ``data``'s security (``SymbolProperties``: quote currency,
        multiplier, lot...). A quote currency other than the account's needs a conversion
        (``set_conversion``) for the value to be marked."""
        data.symbol_properties = properties
        security = getattr(data, "security", None)
        if security is not None and security._broker is self:
            security.properties = properties
            self.portfolio.multiplier[security.id] = properties.contract_multiplier
            if properties.quote_currency != self.cashbook.account_currency:
                self.cashbook.ensure(properties.quote_currency)
        self._mark_cache = None
        self._refresh_cash_flags()

    def set_conversion(self, currency: str, data, invert: bool = False) -> None:
        """Price ``currency`` in the account currency from the closes of the feed ``data``
        (added to the engine like any other): its close is the value of one unit of
        ``currency`` in the account currency (``EURUSD`` for EUR in a USD account), or its
        inverse with ``invert`` (``USDJPY`` for JPY). The rate moves at each slice (phase 2),
        to the feed's last close."""
        cash = self.cashbook.ensure(currency)
        self._fx_sources[currency] = (data, bool(invert))
        security = getattr(data, "security", None)
        cash.conversion_source = security.id if security is not None and security._broker is self else None
        self._refresh_cash_flags()

    def _refresh_cash_flags(self) -> None:
        account = self.cashbook.account_currency
        props = [s.properties for s in self.securities]
        self._foreign = bool(self._fx_sources) or any(p.quote_currency != account for p in props) or \
            len(self.cashbook) > 1
        sets = [s.models for s in self.securities] or [self.models]
        self._multi_cash = self._foreign or any(type(m.settlement) is not ImmediateSettlement for m in sets)

    def _update_rates(self) -> None:
        """Phase 2, broker side: each converted currency at its feed's last close."""
        book = self.cashbook
        for currency, (data, invert) in self._fx_sources.items():
            if len(data):
                rate = data.close[0]
                book[currency].conversion_rate = 1.0 / rate if invert else rate

    def fx_rates(self, ids) -> np.ndarray:
        """The conversion rate of each security's quote currency (1.0 for the account's)."""
        book = self.cashbook
        account = book.account_currency
        out = np.ones(len(ids))
        securities = self.securities
        for k, sid in enumerate(np.asarray(ids).tolist()):
            ccy = securities[sid].properties.quote_currency
            if ccy != account:
                out[k] = book[ccy].conversion_rate
        return out

    def _cash_total(self) -> float:
        """Every balance in the account currency plus the unsettled proceeds."""
        book = self.cashbook
        total = book.total_in_account_currency()
        if book._unsettled:
            total += book.unsettled().amount
        return total

    def start(self, datas, market: MarketState) -> None:
        """Bind the run's feeds (``SecurityId`` = adddata order) and its market state."""
        for data in datas:
            self.register(data)
        self.market = market
        for security in self.securities:
            security._market = market
        self._accruals = self._needs_accruals()
        self._refresh_cash_flags()
        self._mark_cache = None
        if self._multi_cash:
            # the settlement models' unsettled cash counts in the value from the first mark
            seen = set()
            for security in self.securities:
                m = security.models.settlement
                if id(m) not in seen and not isinstance(m, ImmediateSettlement):
                    seen.add(id(m))
                    self.cashbook.register_unsettled(m.unsettled)

    def _needs_accruals(self) -> bool:
        sets = [s.models for s in self.securities] or [self.models]
        return any(not getattr(m.settlement, "noop_scan", False) or not getattr(m.shortable, "noop_fees", False)
                   or not getattr(m.margin_interest, "noop_accrue", False) for m in sets)

    # -- configuration -----------------------------------------------------------------
    def set_cash(self, cash):
        # Observed on the oracle: the portfolio value follows at once, before the run (an
        # analyzer's ``start`` reads it) and during it -- and it becomes the cash alone, the
        # open positions left out until the next bar's mark (probe ``setcash_held``; corpus
        # ``parameter_uncertainty…`` credits a monthly rate with ``setcash`` and then sizes
        # its book on ``getvalue()``, which reads that cash).
        self.cash = self.startingcash = float(cash)
        self._value = self.cash

    setcash = set_cash

    def get_cash(self):
        return self.cash

    getcash = get_cash

    def add_cash(self, cash):
        # Observed on the oracle (probe ``add_cash_timing``): the addition is not visible to
        # ``getcash``/``getvalue`` in the bar that requests it, the next bar's batch check and
        # fills still run on the old cash, and it lands at the end of that pass (phase 7) --
        # before the notifications of the bar are read. Several requests are summed first.
        self._cash_addition.append(float(cash))

    @property
    def _coc(self):
        return self.models.fill.coc

    def set_coc(self, coc):
        self.models.fill.coc = bool(coc)

    def set_coo(self, coo):
        self._coo = bool(coo)

    # Observed on the oracle (probes ``slippage_*``): the flags passed here replace the
    # broker's, ``slip_open`` included -- called with the amount alone, a market order
    # filled at the open slips (corpus ``the_profitability_of_technical_analysis…``,
    # ``price_overreactions_in_the_cryptocurrency_market``)
    def set_slippage_perc(self, perc, slip_open=True, slip_limit=True, slip_match=True, slip_out=False):
        slip = self._compat_slippage()
        slip.perc, slip.fixed = float(perc), 0.0
        self._compat_slip_flags(slip, slip_open, slip_limit, slip_match, slip_out)

    def set_slippage_fixed(self, fixed, slip_open=True, slip_limit=True, slip_match=True, slip_out=False):
        slip = self._compat_slippage()
        slip.fixed, slip.perc = float(fixed), 0.0
        self._compat_slip_flags(slip, slip_open, slip_limit, slip_match, slip_out)

    @staticmethod
    def _compat_slip_flags(slip, slip_open, slip_limit, slip_match, slip_out):
        slip.slip_open, slip.slip_limit = bool(slip_open), bool(slip_limit)
        slip.slip_match, slip.slip_out = bool(slip_match), bool(slip_out)

    def _compat_slippage(self):
        slip = self.models.slippage
        if not hasattr(slip, "perc"):
            raise TypeError(f"the broker's slippage model is {type(slip).__name__}, not the compatible one: "
                            "replace it with set_models(data, slippage=...) or set_profile(...)")
        return slip

    def set_checksubmit(self, checksubmit):
        self.p.checksubmit = checksubmit
        self.models.buying_power.checksubmit = checksubmit

    def set_shortcash(self, shortcash):
        self.p.shortcash = shortcash
        self.models.buying_power.shortcash = shortcash

    def set_fundmode(self, fundmode, fundstartval=None):
        self.p.fundmode = fundmode

    def get_fundmode(self):
        return self.p.fundmode

    def set_filler(self, filler):
        self._filler = filler

    # -- state ---------------------------------------------------------------------------
    def getposition(self, data):
        return self.positions[data]

    def get_value(self, datas=None, mkt=False, lever=False):
        # Observed on the oracle: with ``datas`` the result is the value of those
        # positions alone, without the cash -- and asking for it applies any pending
        # ``add_cash`` on the spot (``getcash`` reflects it from then on), which is how a
        # same-bar ``order_target_value`` after ``add_cash`` gets its batch checked against
        # the new cash while a plain ``sell`` does not (probe ``add_cash_timing``).
        if datas is None:
            # Observed on the oracle: the plain portfolio value is the one marked once per
            # bar, after that bar's fills and cash additions -- a same-bar ``add_cash`` or a
            # ``get_value(datas=...)`` that applied one does not move it until the next bar.
            return self.cash if self._value is None else self._value
        if self._cash_addition:
            self._land_additions()
        value = 0.0
        if isinstance(datas, (list, tuple)) and len(datas) == 1:
            # the common read (``order_target_value``): one position, no scan of the book
            data = datas[0]
            pos = self.positions.get(data)
            if pos is not None and pos.size and len(data):
                value += self.getcommissioninfo(data).getvalue(pos, data.close[0])
                m = data.security.properties.contract_multiplier
                if m != 1.0:
                    value = value * m
                if self._foreign:
                    value = value * self.fx_rates([data.security.id])[0]
            return value
        wanted = {id(d) for d in datas}    # by identity: a feed compares equal by its close
        for data, pos in self.positions.items():
            if id(data) not in wanted:
                continue
            if not pos.size:
                continue
            if len(data) == 0:
                continue
            comminfo = self.getcommissioninfo(data)
            m = self._security(data).properties.contract_multiplier
            v = comminfo.getvalue(pos, data.close[0])
            v = v if m == 1.0 else v * m
            if self._foreign:
                v = v * self.fx_rates([data.security.id])[0]
            value += v
        return value

    getvalue = get_value

    def _get_value(self, datas=None, lever=False):
        """The broker's own revaluation, which four published corpus strategies call right
        after ``add_cash`` to settle it inside the bar. Observed on the oracle (probe
        ``private_get_value``): every queued addition lands at once (``getcash`` shows it
        from then on, so do the next bar's batch check and fills), the portfolio is marked
        again, and that value is both returned and what the plain ``getvalue()`` reads for
        the rest of the bar; with nothing queued the mark is unchanged."""
        if datas is not None:
            return self.get_value(datas=datas, lever=lever)
        if self._cash_addition:
            self._land_additions()
        self.mark(None)
        return self._value

    def _mark_value(self):
        """``cash + t``, ``t`` the terms of every open position (``_mark_terms``) summed left
        to right from zero, in the order the positions were first touched."""
        market = self.market
        if market is None:
            terms = 0.0
            for data, pos in self.positions.items():
                if not pos.size or len(data) == 0:
                    continue
                e, u = _mark_terms(self.getcommissioninfo(data), pos, data.close[0])
                m = self._security(data).properties.contract_multiplier
                if m != 1.0:
                    e, u = e * m, u * m
                if self._foreign:
                    r = self.fx_rates([data.security.id])[0]
                    e, u = e * r, u * r
                terms += e
                terms += u
            return (self._cash_total() if self._multi_cash else self.cash) + terms
        order = self.positions.order
        cash = self._cash_total() if self._multi_cash else self.cashbook.account.amount
        if not order:
            return cash
        cache = self._mark_cache
        if cache is None or cache[0] != len(order) or cache[1] != self._comminfo_key():
            cache = self._mark_cache = self._build_mark_cache()
        _, _, ids, mult, lev, scalar_idx, cmult = cache
        pf = self.portfolio
        q = pf.quantity[ids]
        live = (q != 0) & (market.length[ids] > 0)
        if not live.any():
            return cash
        price = market.flat["close"][market.offsets[ids] + market.length[ids] - 1]
        avg = pf.average_price[ids]
        # the entry value then the open P&L of each position (``_mark_terms``), the same
        # float operations element by element
        levered = (q > 0) & (lev != 1.0) & (price > 0.0)   # at a close <= 0: size x close
        entry = np.where(levered, q * avg / lev, q * price)
        pnl = np.where(levered, q * (price - avg) * mult, 0.0)
        if scalar_idx:
            securities = self.securities
            for k in scalar_idx:
                if live[k]:
                    data = securities[ids[k]].feed
                    entry[k], pnl[k] = _mark_terms(self.getcommissioninfo(data), self.positions[data],
                                                   data.close[0])
        if cmult is not None:
            # contract multipliers (options): value = quantity x price x multiplier
            entry = entry * cmult
            pnl = pnl * cmult
        if self._foreign:
            # securities quoted in another currency, valued in the account currency
            rates = self.fx_rates(ids)
            entry = entry * rates
            pnl = pnl * rates
        terms = np.column_stack((entry[live], pnl[live])).ravel()
        # sequential, not pairwise: ``add.accumulate`` adds left to right like the loop
        return cash + float(np.add.accumulate(np.concatenate(((0.0,), terms)))[-1])

    def _build_mark_cache(self):
        order = self.positions.order
        ids = np.array(order, dtype=np.int64)
        securities = self.securities
        comminfos = [self.getcommissioninfo(securities[i].feed) for i in order]
        plain = np.array([_plain_comminfo(ci) for ci in comminfos], dtype=bool)
        mult = np.array([float(ci.p.mult) for ci in comminfos])
        lev = np.array([float(ci.get_leverage()) for ci in comminfos])
        scalar_idx = np.flatnonzero(~plain).tolist()
        cmult = np.array([securities[i].properties.contract_multiplier for i in order], dtype=np.float64)
        return (len(order), self._comminfo_key(), ids, mult, lev, scalar_idx,
                cmult if (cmult != 1.0).any() else None)

    def _comminfo_key(self):
        """Changes whenever a scheme is set, added or replaced (even in the dict directly)."""
        # leverage too: a strategy may set ``comminfo.p.leverage`` on a scheme in place
        return (self._comminfo_version, tuple(map(id, self.comminfo.values())),
                tuple(ci.p.leverage for ci in self.comminfo.values()))

    def get_orders_open(self, safe=False):
        return [o for o in self.pending]

    def get_notification(self):
        """The oldest undelivered notification (an order as it was, or a trade)."""
        event = self.bus.pop()
        if event is None:
            return None
        return event.order if isinstance(event, OrderEvent) else getattr(event, "trade", event)

    def notify(self, order):
        # A notification carries the status at the moment it was raised: the strategy
        # sees Submitted then Accepted, not Accepted twice, even though the live order
        # object has already moved on; ``executed`` is frozen with it.
        if self._tw_quiet_orders:
            return
        clone = order._snapshot()
        security = getattr(order.data, "security", None)
        ex = order.executed
        self.bus.publish(OrderEvent(self._now, 0, order.ref, security.id if security is not None else None,
                                    order.status, ex.size, ex.price, ex.comm, "", clone))

    # -- order entry -------------------------------------------------------------------------
    def buy(self, owner, data, size, price=None, plimit=None, exectype=None, valid=None,
            tradeid=0, oco=None, trailamount=None, trailpercent=None, parent=None, transmit=True,
            histnotify=False, _checksubmit=True, **kwargs):
        order = BuyOrder(owner=owner, data=data, size=abs(size), price=price, plimit=plimit,
                         exectype=exectype, valid=valid, tradeid=tradeid, **kwargs)
        return self.submit(order, check=_checksubmit)

    def sell(self, owner, data, size, price=None, plimit=None, exectype=None, valid=None,
             tradeid=0, oco=None, trailamount=None, trailpercent=None, parent=None, transmit=True,
             histnotify=False, _checksubmit=True, **kwargs):
        order = SellOrder(owner=owner, data=data, size=-abs(size), price=price, plimit=plimit,
                          exectype=exectype, valid=valid, tradeid=tradeid, **kwargs)
        return self.submit(order, check=_checksubmit)

    def submit(self, order, check=True):
        """Submitted now; accepted, as a batch, at phase 5 of the next slice.

        Observed on the oracle: the notifications of a bar arrive as all the Submitted,
        then all the Accepted, then the fills -- acceptance is a broker step at the next
        bar, not part of submission. With ``checksubmit`` the order is pre-checked
        against the cash at the current close and goes straight to ``Margin`` when it
        cannot be afforded, without ever being Accepted.
        """
        security = self._security(order.data)
        order.comminfo = self.getcommissioninfo(order.data)
        order.submit(self)
        self.orders.append(order)
        order._check = check
        if not getattr(security.models.buying_power, "checksubmit", True):
            # Observed on the oracle (probe ``checksubmit_off``): without the pre-check an
            # order is accepted at once -- no Submitted notification, an Accepted one
            # delivered with the next bar's -- and so ``cancel()`` in the bar that placed
            # it does cancel it. The cash is only checked at the fill (Margin there).
            order.accept(self)
            self.notify(order)
            self.pending.append(order)
            return order
        self.notify(order)
        # Cheat-on-close changes nothing here: observed on the oracle, the batch is still
        # accepted and notified when the next bar arrives; only the fill price of a market
        # order becomes the close of the bar it was placed on (see the fill model).
        self.submitted.append(order)
        return order

    def cancel(self, order, bracket=False):
        # Observed on the oracle (probe ``cancel_timing``): an order still waiting for its
        # batch acceptance (``checksubmit``, the default) cannot be cancelled -- in the ``next`` that placed it, or inside
        # the ``notify_order`` that placed it -- and goes on to be accepted and filled as if
        # nothing had been asked. Only an accepted order not yet filled is cancelled.
        if order in self.pending:
            self.pending.remove(order)
            order.cancel()
            self.notify(order)
            if order._tw_bracket is not None:
                self._tw_end_bracket(order)
            return True
        return False

    # -- brackets ----------------------------------------------------------------------------
    def _tw_link_bracket(self, legs):
        """Tie the orders of a bracket together, as observed on the oracle (probes
        ``bracket_orders``, ``bracket_batch``): the children name the entry as their
        ``parent``; only the last order carries ``transmit``; a child cannot fill before
        the bar after the entry's fill."""
        for leg in legs:
            leg._tw_bracket = legs
            leg.transmit = False
        legs[-1].transmit = True
        for child in legs[1:]:
            child.parent = legs[0]
            child._tw_waiting = True

    def _tw_end_bracket(self, order, remaining=None):
        """``order`` filled as a child, or ended without filling: every other live order of
        its bracket ends now, in bracket order -- ``Canceled`` once accepted, ``Rejected``
        while still waiting for the batch check (observed on the oracle, probe
        ``bracket_batch``: a refused entry rejects its children; a refused child cancels the
        accepted entry and rejects its sibling)."""
        for other in order._tw_bracket:
            if other is order or not other.alive():
                continue
            if other in self.submitted:
                self.submitted.remove(other)
                other.reject(self)
                self.notify(other)
                continue
            for queue in (self.pending, remaining):
                if queue is not None and other in queue:
                    queue.remove(other)
            other.cancel()
            self.notify(other)

    def _tw_bracket_step(self, order, remaining, activate):
        """After ``order`` of a bracket went through the fill phase."""
        if order.status == Order.Completed:
            if order.parent is None:
                activate.extend(order._tw_bracket[1:])
            else:
                self._tw_end_bracket(order, remaining)
        elif not order.alive():
            self._tw_end_bracket(order, remaining)

    # -- the slice: phases 3 to 8 --------------------------------------------------------------
    def on_slice(self, s: TimeSlice) -> None:
        self.set_time(s)
        self.corporate_actions(s)
        self.accruals(s)
        self.acceptance(s)
        self.fills(s)
        self.transfers(s)
        self.mark(s)

    def set_time(self, s: TimeSlice) -> None:
        """Phase 0, broker side: the time events and fees are stamped with."""
        self._now = s.time
        self._dtnum = s.dtnum

    def next(self, upcoming=None):
        """Compat entry: phases 5 to 8 against the bars the feeds have just moved to."""
        self.acceptance(None)
        self.fills(None)
        self.transfers(None)
        self.mark(None)

    # phase 3
    def corporate_actions(self, s: TimeSlice | None) -> None:
        """Splits, dividends, symbol changes and delistings effective now, applied to
        holdings and open orders before any fill (spec/05 § 10, Q2); none with the
        oracle's data."""
        if s is None:
            return
        if self._tw_receivables:
            self._tw_pay_receivables(s.time)
        if not s.actions:
            return
        securities = self.securities
        for action in sorted(s.actions, key=lambda a: (a.security, a.rank)):
            securities[action.security].models.corporate_actions.apply(action, self)
            self.bus.publish(CorporateActionEvent(self._now, 0, action))
        self._mark_cache = None

    def _tw_receive(self, pay_time: int, amount: CashAmount, security) -> None:
        """A dividend entitled on its ex-date and paid on ``pay_time``: carried until then
        (counted in the marked value), credited at phase 3 of the first slice at or after
        it, through the security's settlement model."""
        self._tw_receivables.append((pay_time, amount, security))

    def _tw_pay_receivables(self, now) -> None:
        due = [r for r in self._tw_receivables if r[0] <= now]
        if not due:
            return
        self._tw_receivables = [r for r in self._tw_receivables if r[0] > now]
        for _, amount, security in due:
            security.models.settlement.apply_funds(security, amount, now, self.cashbook)

    def receivables(self) -> float:
        """Dividends entitled and not yet paid, in the account currency."""
        total = 0.0
        for _, amount, _ in self._tw_receivables:
            total += self.cashbook.convert(amount.amount, amount.currency, self.cashbook.account_currency)
        return total

    def _tw_close_trade(self, data, size, price, pnl) -> None:
        """Close ``data``'s open trade outside an order (a delisting liquidated): the trade
        list, ``TradeAnalyzer`` and ``notify_trade`` see it closed at ``price``."""
        trade = self.trades.get(data)
        if trade is None or not trade.isopen:
            return
        trade.update(None, size, price, 0.0, size, pnl, 0.0, self._dtnum, 0)
        if self._tw_quiet_trades:
            return
        owner = None
        for order in reversed(self.orders):
            if order.data is data:
                owner = order.owner
                break
        security = getattr(data, "security", None)
        self.bus.publish(TradeEvent(self._now, 0, security.id if security is not None else None, trade,
                                    owner))

    # phase 4
    def accruals(self, s: TimeSlice | None) -> None:
        """Settlement scan, borrow fees on shorts, margin interest; skipped entirely while
        every security has the no-op oracle models."""
        if self._fx_sources:
            self._update_rates()
        if not self._accruals or s is None:
            return
        now = s.time
        seen = set()
        for security in self.securities:
            m = security.models.settlement
            if id(m) not in seen:
                seen.add(id(m))
                self.cashbook.register_unsettled(m.unsettled)
                m.scan(now, self.cashbook)
        self._borrow_fees(s)
        seen.clear()
        for security in self.securities:
            m = security.models.margin_interest
            if id(m) not in seen:
                seen.add(id(m))
                charged = m.accrue(self.cashbook, self.portfolio, now)
                if charged:
                    self.cashbook.add(self.cashbook.account_currency, -charged)
        self._prev_accrual = now

    def _borrow_fees(self, s: TimeSlice) -> None:
        prev = self._prev_accrual
        if prev is None or self.market is None:
            return
        years = float(s.time - prev) / (365.0 * 86_400e9)
        pf = self.portfolio
        groups: dict[int, tuple] = {}
        for security in self.securities:
            m = security.models.shortable
            if getattr(m, "noop_fees", False):
                continue
            groups.setdefault(id(m), (m, []))[1].append(security.id)
        for m, sids in groups.values():
            ids = np.array(sids, dtype=np.int64)
            short = pf.quantity[ids] < 0
            if not short.any():
                continue
            ids = ids[short]
            if hasattr(m, "fee_rates"):
                rates = np.asarray(m.fee_rates(ids, s.time), dtype=np.float64)
            else:
                rates = np.array([m.fee_rate(self.securities[i].symbol, s.time) for i in ids])
            price = self.market.gather("close", ids)
            fees = -pf.quantity[ids] * price * pf.multiplier[ids] * rates * years
            for sid, fee in zip(ids.tolist(), fees.tolist()):
                if fee:
                    self.cashbook.add(self.securities[sid].properties.quote_currency, -fee)
                    pf.total_borrow_fees[sid] += fee

    # phase 5
    def acceptance(self, s: TimeSlice | None) -> None:
        """The orders submitted during the previous slice, checked as one batch in
        submission order by each security's buying-power model.

        Observed on the oracle: the submitted orders of a bar are checked as a batch, in
        submission order, against a *running* cash -- a sell earlier in the batch funds a
        buy later in it -- and each is Accepted or Margin before any fill happens. The
        running cash takes the cash flow of **every** order, refused ones included: an
        order is Accepted when the running cash is still non-negative after it, Margin
        otherwise (``a_new_anomaly…``, 2020-05-28: 48 orders, 20 refusals).
        """
        submitted = self.submitted
        if not submitted:
            return
        state = BatchState(self.cash, {}, self._now)
        pending = self.pending
        while submitted:
            order = submitted.popleft()
            security = order.data.security
            # The pre-check prices the order at what was known when it was placed: the
            # close of the submission bar for a market order, the limit or stop price
            # otherwise -- not at the fill price of the bar that just arrived.
            check = security.models.buying_power.check_order(security, order, self._check_price(order, order.data),
                                                             state)
            state.running_cash -= check.cash_consumed
            if not check.accepted:
                order.margin()
                self.notify(order)
                if order._tw_bracket is not None:
                    self._tw_end_bracket(order)
                continue
            order.accept(self)
            self.notify(order)
            pending.append(order)

    @staticmethod
    def _check_price(order, data):
        et = order.exectype
        if et in (Order.Limit, Order.StopLimit) and order.pricelimit is not None:
            return order.pricelimit if et == Order.StopLimit else order.price
        if et in (Order.Stop,) and order.price is not None:
            return order.price
        if et == Order.Limit and order.price is not None:
            return order.price
        n = len(data)
        if n >= 2:
            return data.close[-1]
        if n == 1:
            return data.close[0]
        return None

    # phase 6
    def fills(self, s: TimeSlice | None) -> None:
        """Each accepted, still-open order whose feed has a bar now: the fill model
        proposes, the buying-power model re-checks at the fill price, the fee model prices,
        the settlement model moves the cash."""
        pending = self.pending
        if not pending:
            return
        has_bar = self.market.has_bar if (s is None and self.market is not None) else (
            s.has_bar if s is not None else None)
        remaining = collections.deque()
        activate: list = []
        while pending:
            order = pending.popleft()
            if order._tw_bracket is not None:
                if order._tw_waiting:
                    remaining.append(order)
                    continue
                self._tw_fill_one(order, has_bar, remaining)
                self._tw_bracket_step(order, remaining, activate)
                continue
            data = order.data
            security = data.security
            if has_bar is not None and (security.id >= len(has_bar) or not has_bar[security.id]):
                remaining.append(order)
                continue
            models = security.models
            fill_model = models.fill
            if (type(fill_model) is NextBarFillModel and order.exectype == _MARKET and order.valid is None
                    and not fill_model.coc):
                # the oracle default on a plain market order: never expires, fills whole at
                # this bar's open moved by the slippage model -- what ``fill`` computes,
                # without building the bar and the parameters (a slipped open takes the
                # general path, which holds it inside the bar)
                o = data.open[0]
                price = slipped(models.slippage, security, order, o, True)
                if price == o or math.isnan(price):
                    if self._execute(security, order, FillProposal(order.size, price), self._dtnum):
                        remaining.append(order)
                    continue
            bar = Bar(self._dtnum, data.open[0], data.high[0], data.low[0], data.close[0], data.volume[0])
            if fill_model.expired(order, bar):
                order.expire()
                self.notify(order)
                continue
            placed = None
            if getattr(fill_model, "needs_placed_bar", True) and len(data) >= 2:
                placed = Bar(data.datetime[-1], data.open[-1], data.high[-1], data.low[-1], data.close[-1],
                             data.volume[-1])
            proposal = fill_model.fill(FillParameters(security, order, bar, placed, self._now, models.slippage))
            if proposal is None:
                remaining.append(order)
                continue
            if self._execute(security, order, proposal, self._dtnum):
                remaining.append(order)    # a part filled, the rest waits for the next bar
        self.pending = remaining
        for child in activate:
            child._tw_waiting = False

    def _tw_fill_one(self, order, has_bar, remaining) -> None:
        """The fill phase of one order, by the general path (a bracket's orders)."""
        data = order.data
        security = data.security
        if has_bar is not None and (security.id >= len(has_bar) or not has_bar[security.id]):
            remaining.append(order)
            return
        fill_model = security.models.fill
        bar = Bar(self._dtnum, data.open[0], data.high[0], data.low[0], data.close[0], data.volume[0])
        if fill_model.expired(order, bar):
            order.expire()
            self.notify(order)
            return
        placed = None
        if getattr(fill_model, "needs_placed_bar", True) and len(data) >= 2:
            placed = Bar(data.datetime[-1], data.open[-1], data.high[-1], data.low[-1], data.close[-1],
                         data.volume[-1])
        proposal = fill_model.fill(FillParameters(security, order, bar, placed, self._now,
                                                  security.models.slippage))
        if proposal is None or self._execute(security, order, proposal, self._dtnum):
            remaining.append(order)

    def _execute(self, security, order, proposal, dt) -> bool:
        """Fill ``proposal``; True when the order stays open (a part filled, the rest
        left for the next bars by a volume-capped fill model)."""
        data = order.data
        comminfo = order.comminfo
        models = security.models
        size, price = proposal.quantity, proposal.price
        pos = self.positions[data]
        now = self._now
        fee = models.fee
        comm = order_fee(fee, security, order, size, price, now)
        allowed = models.buying_power.check_fill(security, order, proposal, self.cash, now)
        if allowed is None:
            order.margin()
            self.notify(order)
            return False
        settlement = models.settlement
        cashbook = self.cashbook
        # every cash flow of a security is in its quote currency (the account's by default)
        ccy = security.properties.quote_currency
        if type(settlement) is ImmediateSettlement:
            # the oracle default: the funds land at once (``apply_funds`` is this add)
            def settle(security, flow, now, cashbook):
                cashbook.add(ccy, flow)
        else:
            def settle(security, flow, now, cashbook):
                settlement.apply_funds(security, CashAmount(flow, ccy), now, cashbook)
        oldprice, oldsize = pos.price, pos.size
        partial = allowed.quantity != size
        if partial:
            # the closing part of a reversal: filled, then the rest refused (Partial, Margin)
            size = allowed.quantity
            comm = order_fee(fee, security, order, size, price, now)
        newsize, _newprice, opened, closed = pos.update(size, price)
        pnl = comminfo.profitandloss(-closed, oldprice, price) if closed else 0.0
        m = security.properties.contract_multiplier
        if comminfo.stocklike:
            if m != 1.0:
                pnl = pnl * m
            # Observed on the oracle (probes ``reversal_fees``, ``close_cash``): a fill is
            # one execution per part -- the closing part, then the opening part --, each
            # settled then charged its own fee. Closing gives back the position's entry
            # value plus the P&L, ``(entry value + P&L)``, not the traded value: the two
            # differ in the last bits. A leveraged long's entry value is its margin,
            # ``size * entry / leverage``; opening a long pays ``size * price /
            # leverage``. Shorts are never leveraged.
            lev = comminfo.get_leverage()
            parts = []
            if closed:
                entry = -closed * oldprice / lev if oldsize > 0 else -closed * oldprice
                if m != 1.0:
                    entry = entry * m
                parts.append((closed, entry + pnl))
            if opened:
                flow = -(opened * price) / lev if opened > 0 else -(opened * price)
                parts.append((opened, flow if m == 1.0 else flow * m))
            fees = []
            for part, flow in parts:
                part_fee = comm if len(parts) == 1 else order_fee(fee, security, order, part, price, now)
                settle(security, flow, now, cashbook)
                cashbook.add(ccy, -part_fee)
                fees.append(confirm_fee(fee, security, order, part, price))
            closedcomm = fees[0] if closed else 0.0
            openedcomm = fees[-1] if opened else 0.0
            comm_final = closedcomm + openedcomm if closed and opened else fees[0]
            trade_parts = (closedcomm, openedcomm) if closed and opened else None
        else:
            # futures-like: only the P&L moves the cash
            settle(security, pnl, now, cashbook)
            cashbook.add(ccy, -comm)
            comm_final = confirm_fee(fee, security, order, size, price)
            closedcomm = comm_final if closed else 0.0
            openedcomm = comm_final if not closed else 0.0
            trade_parts = None
        # observed on the oracle (probe ``executed_values``): the closed part is valued at
        # the position's entry price, the opened part at the fill price
        closed_value, opened_value = -closed * oldprice, opened * price
        if m != 1.0 and comminfo.stocklike:
            closed_value, opened_value = closed_value * m, opened_value * m
        order.execute(dt, size, price, closed, closed_value, closedcomm, opened, opened_value,
                      openedcomm, 0.0, pnl)
        self._account(security, comm_final, pnl)
        alive = False
        if partial:
            order.partial()
            self.notify(order)
            order.margin()
        elif order.executed.remsize:
            order.partial()                # volume-capped: the rest stays open
            alive = True
        else:
            order.completed()
        self.notify(order)
        self._update_trade(order, data, size, price, opened, closed, pnl, comm_final, dt, newsize,
                           parts=trade_parts)
        return alive

    @staticmethod
    def _confirm_fee(fee_model, security, order, size, price):
        return confirm_fee(fee_model, security, order, size, price)

    def _account(self, security, fee, pnl):
        pf = self.portfolio
        sid = security.id
        pf.total_fees[sid] += fee
        if pnl:
            pf.realized_pnl[sid] += pnl

    # phase 7
    def _land_additions(self) -> None:
        """Queued ``add_cash`` requests land one after the other, in request order --
        observed on the oracle: summing them first rounds differently (corpus
        ``profitable_mean_reversion…`` 2010-03-12, nine additions of the toolbox's
        membership marks)."""
        cash = self.cash
        for amount in self._cash_addition:
            cash += amount
        self.cash = cash
        self._cash_addition = []

    def transfers(self, s: TimeSlice | None) -> None:
        """Queued ``add_cash`` requests land, one after the other."""
        if self._cash_addition:
            self._land_additions()

    # phase 8
    def mark(self, s: TimeSlice | None) -> None:
        self._value = self._mark_value()
        if self._tw_receivables:
            # dividends entitled and not yet paid (``dividends="pay_date"``)
            self._value += self.receivables()

    # phase 9
    def margin_call(self, s: TimeSlice | None) -> None:
        """The margin-call model may place liquidation orders, handled from phase 5 of the
        next slice; there is none by default (the oracle never calls)."""
        model = self.margin_call_model
        if model is not None:
            model.check(self, s)

    def _update_trade(self, order, data, size, price, opened, closed, pnl, comm, dt, newsize, parts=None):
        if opened and closed:
            # Observed on the oracle (probe ``analyzers``): a reversing fill closes the
            # trade with its closing part and opens a new one with the rest, each part
            # carrying its own fee (probe ``reversal_fees``) -- a share of the whole fee
            # for a scheme that does not price the parts.
            if parts is None:
                share = abs(closed) / (abs(closed) + abs(opened))
                parts = (comm * share, comm - comm * share)
            self._update_trade(order, data, closed, price, 0.0, closed, pnl, parts[0], dt, 0.0)
            self._update_trade(order, data, opened, price, opened, 0.0, 0.0, parts[1], dt, newsize)
            return
        trade = self.trades.get(data)
        if trade is None or trade.isclosed:
            trade = Trade(data=data, tradeid=order.tradeid)
            self.trades[data] = trade
        trade.update(order, size, price, opened, closed, pnl, comm, dt, newsize)
        if not (trade.justopened or trade.isclosed):
            # observed on the oracle (probe ``executed_values``): ``notify_trade`` hears of a
            # trade when it opens and when it closes, not when a fill only resizes it
            return
        if self._tw_quiet_trades:
            return
        security = getattr(data, "security", None)
        self.bus.publish(TradeEvent(self._now, 0, security.id if security is not None else None, trade,
                                    order.owner))


SimulatedBroker = BackBroker
