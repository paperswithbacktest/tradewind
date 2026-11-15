"""The seam between the engine and a broker (spec/07 § 3): ``BrokerAdapter`` and ``BarSource``.

An adapter speaks its venue's protocol and nothing else: it turns an ``OrderRequest`` into
the venue's order, and the venue's callbacks into the messages of ``messages.py``, which it
queues. It never touches an ``Order`` object, a position or the cash: the ``LiveBroker``
does, at slice boundaries, from the queued messages -- which is what makes a session
replayable from its journal.

Single-threaded by contract: the engine calls every method from its own thread, and an
adapter whose library runs an event loop (ib_async) runs it inside ``idle()``, the only
place the engine waits. An adapter that has threads of its own must hand messages over
through a thread-safe queue and still return them from ``drain()``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple, Protocol, runtime_checkable

from .messages import AccountSnapshot, OrderRequest


class BarRecord(NamedTuple):
    """One complete bar of one symbol; ``time`` is the bar's stamp (a daily bar: its date at
    midnight UTC, as the compat feeds stamp it)."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class AdapterError(RuntimeError):
    """The adapter cannot do what was asked (not installed, not connected, not a paper
    account...). Business refusals of an order are ``Rejected`` messages, never this."""


@runtime_checkable
class BrokerAdapter(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def place(self, request: OrderRequest) -> None: ...
    def cancel(self, client_id: str) -> None: ...
    def snapshot(self, full_sync: bool = False) -> AccountSnapshot: ...
    def drain(self) -> list: ...                       # queued messages, oldest first
    def idle(self, seconds: float) -> None: ...        # wait, letting the venue's I/O run
    def now(self) -> int: ...                          # wall clock, ns UTC


@runtime_checkable
class BarSource(Protocol):
    def poll(self, symbol: str, after: int, now: int) -> Sequence[BarRecord]:
        """The complete bars of ``symbol`` stamped after ``after``, oldest first. A bar that
        is still forming is never returned."""
        ...


__all__ = ["AdapterError", "BarRecord", "BarSource", "BrokerAdapter"]
