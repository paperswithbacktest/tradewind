"""The live clock (spec/05 § 6.1 "Live", spec/07 § 4).

``LiveClock`` polls a ``BarSource`` on the wall clock and emits one slice per bar time once
that time is settled: every feed has delivered its bar, or every feed that has not has
already delivered a later one (a holiday on its exchange), or ``assemble_timeout`` seconds
went by since the first bar of that time arrived. Times are emitted in increasing order;
a bar that arrives for a time already emitted is late and is kept aside (``late``), never
replayed into the past.

When several times are settled at once -- a session restarting after a gap, a feed that
caught up -- all but the last are flagged ``stale``: the engine steps the strategy through
them so its state is right, but no order placed on a stale slice is sent (it would trade on
yesterday's decision at today's prices).

The clock waits with the adapter's ``idle`` (which keeps the venue's I/O running) and reads
the time from the adapter's ``now``, so a simulated adapter drives a whole session in
milliseconds.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import NamedTuple

from .adapter import BarRecord, BarSource


class LiveTick(NamedTuple):
    time: int                               # the bar time, ns
    bars: dict[int, BarRecord]              # feed index -> its bar at that time
    stale: bool                             # a later time was settled in the same poll
    wall: int                               # wall clock when it was emitted


class LiveClock:
    def __init__(self, symbols: Sequence[str], source: BarSource, last: Sequence[int | None], *,
                 now: Callable[[], int], idle: Callable[[float], None], poll_interval: float = 60.0,
                 assemble_timeout: float = 600.0, stop: Callable[[], bool] | None = None,
                 finished: Callable[[int], bool] | None = None):
        self.symbols = list(symbols)
        self.source = source
        self.polled = [(-1 << 62) if t is None else int(t) for t in last]
        self.emitted = max(self.polled) if self.polled else -(1 << 62)
        self.now = now
        self.idle = idle
        self.poll_interval = float(poll_interval)
        self.assemble_ns = int(assemble_timeout * 1e9)
        self.stop = stop
        self.finished = finished
        self.late: list[tuple[str, BarRecord]] = []
        self._pending: dict[int, dict[int, BarRecord]] = {}
        self._first_seen: dict[int, int] = {}

    def _poll(self, t: int) -> None:
        pending, first = self._pending, self._first_seen
        for j, symbol in enumerate(self.symbols):
            for bar in self.source.poll(symbol, self.polled[j], t):
                bar = BarRecord(*bar)
                if bar.time <= self.polled[j]:
                    continue
                self.polled[j] = bar.time
                if bar.time <= self.emitted:
                    self.late.append((symbol, bar))
                    continue
                pending.setdefault(bar.time, {})[j] = bar
                first.setdefault(bar.time, t)

    def _settled(self, ts: int, t: int) -> bool:
        bars = self._pending[ts]
        if len(bars) == len(self.symbols):
            return True
        if all(j in bars or self.polled[j] > ts for j in range(len(self.symbols))):
            return True
        return t - self._first_seen[ts] >= self.assemble_ns

    def __iter__(self) -> Iterator[LiveTick]:
        while True:
            if self.stop is not None and self.stop():
                return
            t = self.now()
            self._poll(t)
            ready = []
            for ts in sorted(self._pending):
                if not self._settled(ts, t):
                    break
                ready.append(ts)
            for k, ts in enumerate(ready):
                bars = self._pending.pop(ts)
                self._first_seen.pop(ts, None)
                self.emitted = ts
                yield LiveTick(ts, bars, k < len(ready) - 1, t)
                if self.stop is not None and self.stop():
                    return
            if not ready:
                if self.finished is not None and not self._pending and self.finished(t):
                    return
                self.idle(self.poll_interval)


__all__ = ["LiveClock", "LiveTick"]
