"""The typed strategy API: annotated classes over the same engine (spec/08).

A ``TypedStrategy`` is a plain Python class -- no metaclass, no ``params`` tuple, no line
objects in its signatures -- whose parameters are a dataclass and whose hooks receive typed
events::

    @dataclass(frozen=True)
    class Params:
        lookback: int = 60

    class Momentum(tw.TypedStrategy[Params]):
        def on_bar(self, bar: tw.BarEvent) -> None:
            if self.bars["SPY"].close[0] > self.bars["SPY"].close[-self.params.lookback]:
                self.order_target_weight("SPY", 1.0)

It does not run on a path of its own. ``Engine.addstrategy`` takes it like any strategy;
the engine builds a compatible ``Strategy`` (a *host*) that owns the typed instance and
forwards to it: ``next`` calls ``on_bar``, ``notify_order`` calls ``on_fill``, and
``order_target_weight`` is the host's ``order_target_percent``. Orders, fills, the cash
check and the mark are the engine's, so a strategy written both ways gives the same NAV
bit for bit (``tests/test_typed.py``).

The shape follows QuantConnect Lean's algorithm API (``SetHoldings``, ``OnData``,
``OnOrderEvent``, ``OnSecuritiesChanged``; Apache 2.0, see ``NOTICE``), read as a
specification; the Python classes are Tradewind's own.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import typing
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, TypeVar

import numpy as np
import numpy.typing as npt

from .dateutils import num2date
from .order import Order
from .strategy import Strategy
from .timer import Timer

if TYPE_CHECKING:  # pragma: no cover
    from .securities import Security

__all__ = [
    "BarEvent",
    "BarSeries",
    "Fill",
    "Holding",
    "NoParams",
    "OrderStatus",
    "OrderTicket",
    "OrderUpdate",
    "Portfolio",
    "SecurityChanges",
    "Series",
    "TimerEvent",
    "TypedStrategy",
]


@dataclasses.dataclass(frozen=True)
class NoParams:
    """The parameters of a strategy that declares none."""


P = TypeVar("P")


class OrderStatus(enum.IntEnum):
    """An order's state; the values are the compatible ``Order`` constants."""

    CREATED = Order.Created
    SUBMITTED = Order.Submitted
    ACCEPTED = Order.Accepted
    PARTIAL = Order.Partial
    COMPLETED = Order.Completed
    CANCELED = Order.Canceled
    EXPIRED = Order.Expired
    MARGIN = Order.Margin
    REJECTED = Order.Rejected

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.CREATED, OrderStatus.SUBMITTED, OrderStatus.ACCEPTED,
                        OrderStatus.PARTIAL)


# -- market data ------------------------------------------------------------------------------
class Series:
    """One line of a feed or of an indicator, read relative to the current bar.

    ``series[0]`` is the current value, ``series[-k]`` the value ``k`` bars back. A positive
    index (a bar not arrived yet) raises ``IndexError``: the compatible API keeps that
    look-ahead for backtrader's sake, the typed API does not.
    """

    __slots__ = ("_line",)

    def __init__(self, line: Any) -> None:
        self._line = line

    def __getitem__(self, ago: int) -> float:
        if ago > 0:
            raise IndexError(f"[{ago}] reads a bar that has not arrived (look-ahead); "
                             "use 0 for the current bar and negative indices for the past")
        if -ago >= len(self._line):
            raise IndexError(f"[{ago}] is before the first bar ({len(self._line)} bars so far)")
        return float(self._line[ago])

    def __len__(self) -> int:
        return len(self._line)

    def history(self, size: int) -> npt.NDArray[np.float64]:
        """The last ``size`` values, oldest first (fewer at the start of the run)."""
        size = min(int(size), len(self._line))
        if size <= 0:
            return np.empty(0)
        return np.asarray(self._line.get(size=size), dtype=np.float64)

    @property
    def line(self) -> Any:
        """The compatible line behind this series (to build a compatible indicator on)."""
        return self._line

    def __repr__(self) -> str:
        return f"Series({getattr(self._line, '_name', '')!r}, len={len(self)})"


class BarSeries:
    """The bars of one security: ``open`` ... ``volume`` as ``Series``, its time and state.

    ``name`` is the feed's name, the key of ``strategy.bars`` and of every typed event: it
    never changes during a run. ``symbol`` is the security's current ticker: the feed's name
    until a ``SymbolChange`` (FB -> META) renames the security, the new ticker after it."""

    __slots__ = ("close", "feed", "high", "low", "name", "open", "open_interest", "volume")

    def __init__(self, feed: Any) -> None:
        self.feed = feed
        self.name: str = feed._name
        lines = feed.lines
        self.open = Series(lines.open)
        self.high = Series(lines.high)
        self.low = Series(lines.low)
        self.close = Series(lines.close)
        self.volume = Series(lines.volume)
        self.open_interest = Series(lines.openinterest)

    def __len__(self) -> int:
        return len(self.feed)

    @property
    def time(self) -> _dt.datetime:
        """The time of this security's last bar (it may be older than the slice's)."""
        return typing.cast(_dt.datetime, self.feed.datetime.datetime(0))

    @property
    def has_bar(self) -> bool:
        """A bar of this security arrived at the current slice."""
        return bool(self.feed.security.has_bar)

    @property
    def security(self) -> Security:
        """The engine's record of the instrument (contract terms, hours, holding)."""
        return typing.cast("Security", self.feed.security)

    @property
    def symbol(self) -> str:
        """The current ticker (``security.symbol``): it follows ``SymbolChange`` actions, the
        key (``name``) does not."""
        return str(self.feed.security.symbol)

    def __repr__(self) -> str:
        return f"BarSeries({self.name!r}, len={len(self)})"


class Bars(Mapping[str, BarSeries]):
    """``strategy.bars["SPY"]``: the securities of the run by feed name, in ``adddata``
    order. The key stays the feed's name for the whole run, even after a ticker change."""

    def __init__(self, feeds: list[Any]) -> None:
        self._by_name = {feed._name: BarSeries(feed) for feed in feeds}

    def __getitem__(self, symbol: str) -> BarSeries:
        try:
            return self._by_name[symbol]
        except KeyError:
            raise KeyError(f"no security named {symbol!r} in this run") from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_name)

    def __len__(self) -> int:
        return len(self._by_name)


# -- portfolio ----------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Holding:
    """One position at the current slice: signed quantity, average price, and market value
    (quantity × last close × contract multiplier)."""

    symbol: str
    quantity: float
    average_price: float
    market_value: float

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0


class Portfolio(Mapping[str, Holding]):
    """The account: ``value``, ``cash`` and one ``Holding`` per security (``portfolio["SPY"]``).

    Iterating yields the symbols with an open position."""

    def __init__(self, host: Any) -> None:
        self._host = host

    @property
    def value(self) -> float:
        """The portfolio value marked at this slice's closes (the compatible ``getvalue()``)."""
        return float(self._host.broker.getvalue())

    @property
    def cash(self) -> float:
        return float(self._host.broker.getcash())

    def __getitem__(self, symbol: str) -> Holding:
        host = self._host
        data = host.dnames[symbol]
        pos = host.broker.getposition(data)
        size = float(pos.size)
        # quantity x last close x multiplier, read without ``get_value(datas=...)``, which
        # would land a pending ``add_cash`` (a compatible side effect, spec/02)
        value = size * float(data.close[0]) * data.security.properties.contract_multiplier \
            if size and len(data) else 0.0
        return Holding(symbol, size, float(pos.price), value)

    def __iter__(self) -> Iterator[str]:
        broker = self._host.broker
        for data in self._host.datas:
            pos = broker.positions.get(data)
            if pos is not None and pos.size:
                yield data._name

    def __len__(self) -> int:
        return sum(1 for _ in self)

    @property
    def invested(self) -> bool:
        return any(True for _ in self)


# -- orders ---------------------------------------------------------------------------------------
class OrderTicket:
    """A handle on an order placed by a typed strategy; its fields read the live order."""

    __slots__ = ("_order",)

    def __init__(self, order: Any) -> None:
        self._order = order

    @property
    def id(self) -> int:
        return int(self._order.ref)

    @property
    def symbol(self) -> str:
        return str(self._order.data._name)

    @property
    def quantity(self) -> float:
        """The quantity asked, signed: positive buys, negative sells."""
        return float(self._order.size)

    @property
    def status(self) -> OrderStatus:
        return OrderStatus(self._order.status)

    @property
    def filled_quantity(self) -> float:
        return float(self._order.executed.size)

    @property
    def average_fill_price(self) -> float:
        return float(self._order.executed.price)

    @property
    def fees(self) -> float:
        return float(self._order.executed.comm)

    @property
    def order(self) -> Any:
        """The compatible ``Order`` behind the ticket."""
        return self._order

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OrderTicket) and other._order.ref == self._order.ref

    def __hash__(self) -> int:
        return hash(self._order.ref)

    def __repr__(self) -> str:
        return (f"OrderTicket(id={self.id}, symbol={self.symbol!r}, quantity={self.quantity}, "
                f"status={self.status.name})")


# -- events ---------------------------------------------------------------------------------------
class BarEvent:
    """The slice being processed: its time, its position in the run, the securities with a
    bar at it. Built per slice and cheap: ``time`` and ``symbols`` are computed on access."""

    __slots__ = ("_host", "index")

    def __init__(self, host: Any, index: int) -> None:
        self._host = host
        self.index = index

    @property
    def time(self) -> _dt.datetime:
        return typing.cast(_dt.datetime, self._host.datetime.datetime(0))

    @property
    def date(self) -> _dt.date:
        return self.time.date()

    @property
    def symbols(self) -> tuple[str, ...]:
        """The securities whose bar is at this slice (the others keep their last bar)."""
        return tuple(d._name for d in self._host.datas if d.security.has_bar)

    def __repr__(self) -> str:
        return f"BarEvent(index={self.index}, time={self.time!s})"


@dataclasses.dataclass(frozen=True)
class Fill:
    """One execution: signed quantity (positive bought), price, fee, at the fill bar."""

    time: _dt.datetime
    symbol: str
    quantity: float
    price: float
    fee: float
    ticket: OrderTicket

    @property
    def order_id(self) -> int:
        return self.ticket.id

    @property
    def value(self) -> float:
        """quantity × price, signed (before any contract multiplier)."""
        return self.quantity * self.price


@dataclasses.dataclass(frozen=True)
class OrderUpdate:
    """A status change of an order, as it was when it changed."""

    time: _dt.datetime
    symbol: str
    status: OrderStatus
    ticket: OrderTicket


@dataclasses.dataclass(frozen=True)
class SecurityChanges:
    """Securities entering and leaving the universe at ``time`` (Lean ``SecurityChanges``),
    by feed name -- the keys of ``strategy.bars``, unchanged by a ticker change (the current
    ticker is ``strategy.bars[name].symbol``)."""

    time: _dt.datetime
    added: tuple[str, ...]
    removed: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class TimerEvent:
    """A scheduled call (``TypedStrategy.schedule``): its name and when it fired."""

    name: str
    time: _dt.date


# -- the strategy ---------------------------------------------------------------------------------
class TypedStrategy(Generic[P]):
    """Base class of a typed strategy. Subclass it as ``TypedStrategy[MyParams]``.

    Hooks, all optional: ``initialize`` (build indicators and schedules; called once while
    the run is set up), ``on_start``, ``on_warmup`` (a slice before every feed has a bar and
    every indicator is warm), ``on_bar``, ``on_fill``, ``on_order_event``, ``on_timer``,
    ``on_universe_change``, ``on_corporate_action``, ``on_end``.
    """

    #: The parameters' class; found from ``TypedStrategy[MyParams]`` when not set.
    params_type: ClassVar[type[Any] | None] = None

    params: P
    bars: Bars
    portfolio: Portfolio
    _tw_host: Any

    # -- hooks -------------------------------------------------------------------------------
    def initialize(self) -> None:
        """Once, before the run: create indicators (``self.indicator``) and schedules."""

    def on_start(self) -> None:
        """Once, just before the first slice."""

    def on_warmup(self, bar: BarEvent) -> None:
        """A slice before every feed has delivered a bar and every indicator is warm."""

    def on_bar(self, bar: BarEvent) -> None:
        """A slice once the strategy is warm: decide and place orders."""

    def on_fill(self, fill: Fill) -> None:
        """An execution of one of this strategy's orders (delivered on the fill slice)."""

    def on_order_event(self, update: OrderUpdate) -> None:
        """Any status change of one of this strategy's orders (submitted, accepted, filled,
        refused for margin, canceled, expired)."""

    def on_timer(self, event: TimerEvent) -> None:
        """A schedule created with ``schedule`` fired."""

    def on_universe_change(self, change: SecurityChanges) -> None:
        """Securities entered or left the universe (with a universe provider)."""

    def on_corporate_action(self, action: Any) -> None:
        """A split, dividend, delisting or option expiry was applied (with a provider)."""

    def on_end(self) -> None:
        """Once, after the last slice."""

    # -- state -------------------------------------------------------------------------------
    @property
    def time(self) -> _dt.datetime:
        """The time of the current slice."""
        return typing.cast(_dt.datetime, self._tw_host.datetime.datetime(0))

    @property
    def symbols(self) -> tuple[str, ...]:
        """Every security of the run, in ``adddata`` order."""
        return tuple(self.bars)

    @property
    def securities(self) -> Mapping[str, Security]:
        """The engine's record of each security, by name, read-only (spec/05 § 10, Q6)."""
        return {name: series.security for name, series in self.bars.items()}

    @property
    def is_warming_up(self) -> bool:
        return not self._tw_host._tw_ready()

    @property
    def analyzers(self) -> Any:
        """The analyzers added to the engine (``strategy.analyzers.<name>``)."""
        return self._tw_host.analyzers

    @property
    def engine(self) -> Any:
        return self._tw_host.engine

    # -- indicators and schedules ---------------------------------------------------------------
    def indicator(self, factory: Any, source: str | BarSeries | Series, *args: Any,
                  line: int | str = 0, **params: Any) -> Series:
        """Build a compatible indicator (``tw.ind.SMA``, a custom ``tw.Indicator``...) on a
        security (its name or ``BarSeries``: the indicator reads its closes) or on a
        ``Series``, and return its output line as a ``Series``. Call it in ``initialize``:
        the strategy then waits for the indicator's warm-up, like a compatible one."""
        if isinstance(source, str):
            source = self.bars[source]
        raw = source.feed if isinstance(source, BarSeries) else source.line
        ind = factory(raw, *args, **params)
        out = getattr(ind.lines, line) if isinstance(line, str) else ind.lines[line]
        return Series(out)

    def schedule(self, name: str = "", *, at: Literal["open", "close"] = "open",
                 month_days: tuple[int, ...] = (), week_days: tuple[int, ...] = (),
                 carry: bool = True) -> None:
        """Call ``on_timer`` on the given days of the month (``month_days=(1,)``) or of the
        week (``week_days=(1,)``, Monday is 1), at the session's start (before ``on_bar``)
        or end (after it). With ``carry`` a day without a bar fires on the next bar. Call it
        in ``initialize``; it is the compatible ``add_timer``."""
        when = Timer.SESSION_START if at == "open" else Timer.SESSION_END
        self._tw_host.add_timer(when=when, monthdays=list(month_days), monthcarry=carry,
                                weekdays=list(week_days), weekcarry=carry, _tw_name=name)

    # -- orders ------------------------------------------------------------------------------
    def order_target_weight(self, symbol: str, weight: float) -> OrderTicket | None:
        """Trade to a position worth ``weight`` × the portfolio value (Lean's
        ``SetHoldings``; the compatible ``order_target_percent``). Whole units, toward
        zero; ``None`` when there is nothing to trade."""
        host = self._tw_host
        return _ticket(host.order_target_percent(host.dnames[symbol], target=weight))

    def order_target_value(self, symbol: str, value: float) -> OrderTicket | None:
        """Trade to a position worth ``value``."""
        host = self._tw_host
        return _ticket(host.order_target_value(host.dnames[symbol], target=value))

    def order_target_quantity(self, symbol: str, quantity: float) -> OrderTicket | None:
        """Trade to a position of ``quantity`` units (signed)."""
        host = self._tw_host
        return _ticket(host.order_target_size(host.dnames[symbol], target=quantity))

    def market_order(self, symbol: str, quantity: float) -> OrderTicket | None:
        """Buy (``quantity`` > 0) or sell (< 0) at the next bar's open."""
        return self._order(symbol, quantity, Order.Market, None, None)

    def limit_order(self, symbol: str, quantity: float, limit_price: float, *,
                    valid: _dt.date | _dt.timedelta | None = None) -> OrderTicket | None:
        """Buy or sell at ``limit_price`` or better, from the next bar on, until ``valid``."""
        return self._order(symbol, quantity, Order.Limit, limit_price, valid)

    def stop_order(self, symbol: str, quantity: float, stop_price: float, *,
                   valid: _dt.date | _dt.timedelta | None = None) -> OrderTicket | None:
        """A market order triggered when the price trades through ``stop_price``."""
        return self._order(symbol, quantity, Order.Stop, stop_price, valid)

    def liquidate(self, symbol: str | None = None) -> list[OrderTicket]:
        """Close the position in ``symbol``, or every open position (in ``adddata`` order)."""
        host = self._tw_host
        datas = [host.dnames[symbol]] if symbol is not None else host.datas
        tickets = []
        for data in datas:
            ticket = _ticket(host.close(data))
            if ticket is not None:
                tickets.append(ticket)
        return tickets

    def cancel(self, ticket: OrderTicket) -> None:
        self._tw_host.cancel(ticket.order)

    def _order(self, symbol: str, quantity: float, exectype: int, price: float | None,
               valid: Any) -> OrderTicket | None:
        host = self._tw_host
        data = host.dnames[symbol]
        if quantity > 0:
            return _ticket(host.buy(data, size=quantity, price=price, exectype=exectype, valid=valid))
        if quantity < 0:
            return _ticket(host.sell(data, size=-quantity, price=price, exectype=exectype, valid=valid))
        return None

    # -- construction (engine side) -------------------------------------------------------------
    @classmethod
    def _tw_params_type(cls) -> type[Any]:
        if cls.params_type is not None:
            return cls.params_type
        for klass in cls.__mro__:
            for base in getattr(klass, "__orig_bases__", ()):
                origin = typing.get_origin(base)
                if isinstance(origin, type) and issubclass(origin, TypedStrategy):
                    args = typing.get_args(base)
                    if args and isinstance(args[0], type):
                        return args[0]
        return NoParams

    @classmethod
    def _tw_make_params(cls, kwargs: dict[str, Any]) -> Any:
        ptype = cls._tw_params_type()
        given = kwargs.pop("params", None)
        if given is not None:
            if not isinstance(given, ptype):
                raise TypeError(f"{cls.__name__} takes params of type {ptype.__name__}, "
                                f"not {type(given).__name__}")
            return dataclasses.replace(given, **kwargs) if kwargs else given
        return ptype(**kwargs)

    @classmethod
    def _tw_host_class(cls) -> type[Strategy]:
        """The compatible strategy class that runs this typed one (one per class)."""
        host = cls.__dict__.get("_tw_host_cls")
        if host is None:
            host = _make_host(cls)
            cls._tw_host_cls = host  # type: ignore[attr-defined]
        return typing.cast(type[Strategy], host)


def _ticket(order: Any) -> OrderTicket | None:
    if order is None:
        return None
    ticket = OrderTicket(order)
    tickets = getattr(order.owner, "_tw_tickets", None)
    if tickets is not None:
        tickets[order.ref] = ticket
    return ticket


def _overrides(cls: type, name: str) -> bool:
    return getattr(cls, name) is not getattr(TypedStrategy, name)


class _TypedHost(Strategy):
    """The compatible strategy that runs a ``TypedStrategy``: built by the engine like any
    other, it forwards its hooks to the typed instance it owns (``self.typed``)."""

    _tw_typed_cls: ClassVar[type[TypedStrategy[Any]]]
    _tw_wants_fills: ClassVar[bool] = False
    _tw_wants_updates: ClassVar[bool] = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if args:
            raise TypeError("a TypedStrategy takes its parameters by name")
        cls = self._tw_typed_cls
        params = cls._tw_make_params(dict(kwargs))
        typed = cls.__new__(cls)
        typed._tw_host = self
        typed.params = params
        typed.bars = Bars(self.datas)
        typed.portfolio = Portfolio(self)
        self.typed = typed
        # what the run fingerprint records as this strategy's parameters
        if dataclasses.is_dataclass(params) and not isinstance(params, type):
            from .params import _Params

            self.p = _Params(dataclasses.asdict(params))
            object.__setattr__(self, "params", self.p)
        self._tw_seen_bits: dict[int, int] = {}
        self._tw_tickets: dict[int, OrderTicket] = {}
        type(typed).__init__(typed)
        typed.initialize()

    def start(self) -> None:
        self.typed.on_start()

    def prenext(self) -> None:
        self.typed.on_warmup(BarEvent(self, len(self) - 1))

    def next(self) -> None:
        self.typed.on_bar(BarEvent(self, len(self) - 1))

    def stop(self) -> None:
        self.typed.on_end()


def _host_notify_order(self: _TypedHost, order: Any) -> None:
    typed = self.typed
    status = order.status
    # the ticket the strategy holds reads the live order; the notification's is a snapshot
    ticket = self._tw_tickets.get(order.ref) or OrderTicket(order)
    time = self.datetime.datetime(0)
    if status in (Order.Partial, Order.Completed) and self._tw_wants_fills:
        bits = order.executed.exbits
        seen = self._tw_seen_bits.get(order.ref, 0)
        if len(bits) > seen:
            self._tw_seen_bits[order.ref] = len(bits)
            sign = 1.0 if order.isbuy() else -1.0
            for bit in bits[seen:]:
                typed.on_fill(Fill(num2date(bit.dt) if isinstance(bit.dt, float) else time,
                                   order.data._name, sign * abs(float(bit.size)), float(bit.price),
                                   float(bit.comm), ticket))
    if self._tw_wants_updates:
        typed.on_order_event(OrderUpdate(time, order.data._name, OrderStatus(status), ticket))


def _host_notify_timer(self: _TypedHost, timer: Any, when: Any, *args: Any, **kwargs: Any) -> None:
    self.typed.on_timer(TimerEvent(str(timer.kwargs.get("_tw_name", "")), when))


def _host_notify_universe(self: _TypedHost, change: Any) -> None:
    securities = self.broker.securities
    self.typed.on_universe_change(SecurityChanges(
        num2date(self.datetime[0]),
        tuple(securities[i].feed._name for i in change.added),
        tuple(securities[i].feed._name for i in change.removed)))


def _host_notify_corporate_action(self: _TypedHost, action: Any) -> None:
    self.typed.on_corporate_action(action)


def _make_host(cls: type[TypedStrategy[Any]]) -> type[Strategy]:
    """A ``_TypedHost`` subclass for ``cls`` that defines only the notification methods the
    typed class listens to: the engine skips building order events nobody reads."""
    ns: dict[str, Any] = {"_tw_typed_cls": cls, "__module__": cls.__module__,
                          "__qualname__": cls.__qualname__, "_tw_fingerprint_class": cls}
    fills, updates = _overrides(cls, "on_fill"), _overrides(cls, "on_order_event")
    if fills or updates:
        ns["notify_order"] = _host_notify_order
        ns["_tw_wants_fills"] = fills
        ns["_tw_wants_updates"] = updates
    if _overrides(cls, "on_timer"):
        ns["notify_timer"] = _host_notify_timer
    if _overrides(cls, "on_universe_change"):
        ns["notify_universe"] = _host_notify_universe
    if _overrides(cls, "on_corporate_action"):
        ns["notify_corporate_action"] = _host_notify_corporate_action
    return type(cls.__name__, (_TypedHost,), ns)


def is_typed_strategy(cls: Any) -> bool:
    return isinstance(cls, type) and issubclass(cls, TypedStrategy)
