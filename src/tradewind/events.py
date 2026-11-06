"""The event bus: a deterministic, synchronous queue (spec/05 § 6.3).

Publishing never runs a handler. The bus is drained at phase 12 of a slice: the events
published since the last drain are handed out in (kind rank, sequence) order -- every order
event, then every trade event, then cash, corporate-action and universe events. An event
published while handlers run (an order placed inside ``notify_order``) waits for the next
drain. Both rules are the oracle's (probe ``notify_sequence``).

External events (a live broker's fills, a feed's bars) enter through ``inbox``, the only
thread-safe entry, and are moved onto the bus with a sequence number by ``pump()`` at a slice
boundary; with ``journal`` set, every published event is appended to it, so a session can be
replayed.
"""

from __future__ import annotations

import queue
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

from .securities import SecurityId, Timestamp


@runtime_checkable
class Event(Protocol):
    time: Timestamp
    seq: int


@dataclass(slots=True)
class OrderEvent:
    """A status change of one order. ``order`` is the order as it was at publish time
    (the compat ``notify_order`` argument)."""

    rank: ClassVar[int] = 0
    time: Timestamp
    seq: int
    order_ref: int
    security: SecurityId | None
    status: int                           # Order.Submitted ... Order.Rejected
    fill_quantity: float = 0.0
    fill_price: float = 0.0
    fee: float = 0.0
    message: str = ""
    order: Any = None


@dataclass(slots=True)
class TradeEvent:
    rank: ClassVar[int] = 1
    time: Timestamp
    seq: int
    security: SecurityId | None
    trade: Any = None
    owner: Any = None                     # the strategy of the order that moved the trade


@dataclass(slots=True)
class CashEvent:
    rank: ClassVar[int] = 2
    time: Timestamp
    seq: int
    cash: float = 0.0
    value: float = 0.0
    fundvalue: float = 0.0
    shares: float = 0.0


@dataclass(slots=True)
class CorporateActionEvent:
    rank: ClassVar[int] = 3
    time: Timestamp
    seq: int
    action: Any = None


@dataclass(slots=True)
class UniverseEvent:
    rank: ClassVar[int] = 4
    time: Timestamp
    seq: int
    change: Any = None


def _rank(event) -> int:
    return event.rank


class EventBus:
    def __init__(self, journal: list | None = None):
        self._queue: list = []
        self._seq = 0
        self._handlers: dict[type, list[Callable]] = {}
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.journal = journal

    def __len__(self) -> int:
        return len(self._queue)

    def publish(self, event) -> None:
        """Stamp ``event.seq`` and queue it; no handler runs."""
        self._seq += 1
        event.seq = self._seq
        self._queue.append(event)
        if self.journal is not None:
            self.journal.append(event)

    def pump(self) -> int:
        """Move what the inbox holds onto the bus (slice boundary); return how many."""
        n = 0
        while True:
            try:
                event = self.inbox.get_nowait()
            except queue.Empty:
                return n
            self.publish(event)
            n += 1

    def drain(self) -> list:
        """The events published since the last drain, sorted by (kind rank, seq)."""
        events, self._queue = self._queue, []
        if len(events) > 1:
            events.sort(key=_rank)   # stable: publish (seq) order inside a kind
        return events

    def pop(self):
        """The oldest queued event, without sorting (compat ``get_notification``)."""
        return self._queue.pop(0) if self._queue else None

    def subscribe(self, kind: type, handler: Callable[[Any], None]) -> None:
        self._handlers.setdefault(kind, []).append(handler)

    def unsubscribe(self, kind: type, handler: Callable[[Any], None]) -> None:
        handlers = self._handlers.get(kind)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def deliver(self) -> int:
        """Drain once and run each event's handlers, in (kind, seq) order."""
        events = self.drain()
        handlers = self._handlers
        for event in events:
            for handler in handlers.get(type(event), ()):
                handler(event)
        return len(events)
