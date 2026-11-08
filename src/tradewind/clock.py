"""The clock, exchange hours, universe changes and the phases of a bar (spec/05 § 3, § 6.1-6.2).

One ``TimeSlice`` per clock time; every state change of the engine happens in one of the
sixteen numbered phases of that slice, always in the same order. Read as a specification
from QuantConnect Lean (Apache 2.0, see ``NOTICE``): ``ITimeProvider``,
``SecurityExchangeHours``, ``SecurityChanges`` and the time loop of ``AlgorithmManager.Run``.
"""

from __future__ import annotations

import enum
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

from .dateutils import num2ns
from .securities import SecurityId, Timestamp

if TYPE_CHECKING:  # pragma: no cover
    from .models import CorporateAction, CorporateActionProvider


class Phase(enum.IntEnum):
    """The deterministic order of one slice (spec/05 § 3)."""

    CLOCK = 0
    UNIVERSE = 1
    MARKET_DATA = 2
    CORPORATE_ACTIONS = 3
    ACCRUALS = 4
    ACCEPTANCE = 5
    FILLS = 6
    TRANSFERS = 7
    MARK = 8
    MARGIN_CALL = 9
    INDICATORS = 10
    TIMERS_START = 11
    DELIVERY = 12
    STRATEGY = 13
    ANALYZERS = 14
    TIMERS_END = 15


@dataclass(frozen=True, slots=True)
class UniverseChange:
    time: Timestamp
    added: tuple[SecurityId, ...] = ()
    removed: tuple[SecurityId, ...] = ()


@runtime_checkable
class UniverseProvider(Protocol):
    def change(self, time: Timestamp) -> UniverseChange | None: ...


@dataclass(frozen=True, slots=True)
class TimeSlice:
    time: Timestamp                       # ns since epoch, UTC
    position: int                         # index of the slice in the run
    has_bar: np.ndarray                   # bool[n_securities]: a bar arrives at this slice
    actions: tuple[CorporateAction, ...] = ()
    universe: UniverseChange | None = None
    dtnum: float = 0.0                    # the same time as the compat float day
    all_have_bar: bool = False            # every security has a bar (the common case)


@runtime_checkable
class Clock(Protocol):
    def __iter__(self) -> Iterator[TimeSlice]: ...
    def now(self) -> Timestamp: ...


@runtime_checkable
class ExchangeHours(Protocol):
    def is_open(self, time: Timestamp) -> bool: ...
    def is_trading_day(self, day: np.datetime64) -> bool: ...
    def session(self, day: np.datetime64) -> tuple[Timestamp, Timestamp] | None: ...


class AlwaysOpen:
    """The oracle default: daily bars carry dates only, so a calendar adds nothing."""

    def is_open(self, time: Timestamp) -> bool:
        return True

    def is_trading_day(self, day: np.datetime64) -> bool:
        return True

    def session(self, day: np.datetime64) -> tuple[Timestamp, Timestamp] | None:
        start = np.datetime64(day, "D").astype("datetime64[ns]").astype(np.int64)
        return np.int64(start), np.int64(start + 86_400_000_000_000 - 1)


ALWAYS_OPEN = AlwaysOpen()


def delivered_rows(dtnum: np.ndarray) -> int:
    """How many of a feed's rows the union clock delivers.

    A feed moves to its next row when that row's date is the clock's; its dates are sorted,
    so a repeated date stops it there (the clock never comes back to a date it passed).
    """
    n = len(dtnum)
    if n < 2:
        return n
    bad = np.flatnonzero(np.diff(dtnum) <= 0)
    return int(bad[0]) + 1 if len(bad) else n


class UnionClock:
    """The sorted union of every feed's timestamps (the oracle default).

    ``has_bar[t, j]`` is precomputed for the whole run: security ``j`` delivers a bar at
    slice ``t``. Corporate actions and universe changes, when providers are given, are
    attached to the slice of their time.
    """

    def __init__(self, dtnums: Sequence[np.ndarray],
                 actions: CorporateActionProvider | None = None,
                 universe: UniverseProvider | None = None):
        times = np.unique(np.concatenate(list(dtnums))) if len(dtnums) else np.empty(0)
        self.times = times
        self.times_ns = num2ns(times)
        n_t, n_s = len(times), len(dtnums)
        has_bar = np.zeros((n_t, n_s), dtype=bool)
        for j, dt in enumerate(dtnums):
            rows = delivered_rows(dt)
            if rows:
                has_bar[np.searchsorted(times, dt[:rows]), j] = True
        self.has_bar = has_bar
        self.all_have_bar = has_bar.all(axis=1) if n_s else np.ones(n_t, dtype=bool)
        self.actions_provider = actions
        self.universe_provider = universe
        self._now = np.int64(0)

    def __len__(self) -> int:
        return len(self.times)

    def now(self) -> Timestamp:
        return self._now

    def __iter__(self) -> Iterator[TimeSlice]:
        times, times_ns, has_bar, all_have = self.times, self.times_ns, self.has_bar, self.all_have_bar
        actions_provider, universe_provider = self.actions_provider, self.universe_provider
        for t in range(len(times)):
            now = times_ns[t]
            self._now = now
            actions = tuple(actions_provider.events(now)) if actions_provider is not None else ()
            universe = universe_provider.change(now) if universe_provider is not None else None
            yield TimeSlice(now, t, has_bar[t], actions, universe, float(times[t]), bool(all_have[t]))
