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
                 universe: UniverseProvider | None = None,
                 extra_times: np.ndarray | None = None):
        arrays = list(dtnums)
        if extra_times is not None and len(extra_times):
            arrays.append(np.asarray(extra_times, dtype=np.float64))
        times = np.unique(np.concatenate(arrays)) if arrays else np.empty(0)
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


def ns2num(ns) -> np.ndarray:
    """int64 ns UTC -> float days, the formula feeds use (``dateutils.stamps2num``), so a
    time built here and the same instant read from a feed are the same float."""
    return 719163.0 + np.asarray(ns, dtype=np.int64).astype(np.float64) / (86400.0 * 1e9)


class ExchangeClock(UnionClock):
    """A clock in the hours of one place (spec/05 § 6.1, chantier 4).

    ``ExchangeClock(NYSE)`` keeps the union of the feeds' timestamps -- bars are never
    dropped or moved -- and adds what the place's calendar knows:

    * ``interval`` with ``fill_forward=True``: one slice at the end of every bar of that
      length in every session between the first and the last feed timestamp (Lean's
      fill-forward), so minute strategies and timers run on minutes no feed traded;
    * a session table (``sessions``) and, per slice, the session it belongs to
      (``session_at``), which timers use to fire in the place's time (``Timer``:
      ``SESSION_START`` / ``SESSION_END`` plus ``offset``, a ``datetime.time`` in the
      place's zone or in ``tzdata``, ``repeat``).

    Bars are expected stamped at their end (Lean's convention): a minute bar at 09:31 New
    York is the first of the NYSE session. Feeds whose stamps are all midnight UTC are taken
    as daily bars: each belongs to the session of its date (the last session before it when
    the date does not trade).

    The engine builds it with ``bind`` (``Engine.set_clock``); the default stays
    ``UnionClock`` with ``AlwaysOpen`` hours.
    """

    def __init__(self, hours, interval=None, fill_forward: bool = False):
        self.hours = hours
        self.interval = interval
        self.fill_forward = bool(fill_forward)
        if self.fill_forward and interval is None:
            raise ValueError("fill_forward needs an interval ('1min', '5min', ...)")
        self._bound = False

    def describe(self) -> dict:
        return {"clock": type(self).__name__, "hours": getattr(self.hours, "name", repr(self.hours)),
                "interval": None if self.interval is None else str(self.interval),
                "fill_forward": self.fill_forward}

    def bind(self, dtnums: Sequence[np.ndarray], actions: CorporateActionProvider | None = None,
             universe: UniverseProvider | None = None) -> ExchangeClock:
        """Build the slices for these feeds (called by ``Engine.run``)."""
        from .calendars import _interval_ns

        stamps = [np.asarray(d, dtype=np.float64) for d in dtnums if len(d)]
        extra = None
        self.daily = (not self.fill_forward and bool(stamps)
                      and all(np.all(d == np.floor(d)) for d in stamps))
        if stamps:
            lo = min(float(d[0]) for d in stamps)
            hi = max(float(d[-1]) for d in stamps)
            lo_ns, hi_ns = int(num2ns(lo)), int(num2ns(hi))
            day0 = np.datetime64(lo_ns, "ns").astype("datetime64[D]") - np.timedelta64(7, "D")
            day1 = np.datetime64(hi_ns, "ns").astype("datetime64[D]") + np.timedelta64(7, "D")
            self.sessions = self.hours.sessions(day0, day1)
            if self.fill_forward:
                grid = self.hours.bar_times(day0, day1, self.interval)
                grid = grid[(grid >= lo_ns) & (grid <= hi_ns)]
                extra = ns2num(grid)
        else:
            from .calendars import SessionTable

            empty = np.empty(0, dtype=np.int64)
            self.sessions = SessionTable(np.empty(0, dtype="datetime64[D]"), empty, empty)
        if self.interval is not None:
            _interval_ns(self.interval)   # validate early
        UnionClock.__init__(self, dtnums, actions=actions, universe=universe, extra_times=extra)
        table = self.sessions
        if self.daily:
            days = self.times_ns.astype("datetime64[ns]").astype("datetime64[D]")
            index = np.searchsorted(table.days, days, side="right") - 1
        else:
            index = np.searchsorted(table.opens, self.times_ns, side="right") - 1
        self.session_index = index.astype(np.int64)
        ok = index >= 0
        closes = np.where(ok, table.closes[np.clip(index, 0, None)] if len(table) else 0, 0)
        self.in_session = ok & (self.daily | (self.times_ns <= closes))
        self._bound = True
        return self

    def session_at(self, position: int) -> int:
        """The index in ``sessions`` of the session slice ``position`` belongs to (the last
        one opened at or before it; -1 before the first)."""
        return int(self.session_index[position])

    def local_time(self, position: int):
        """The slice's time on the place's wall clock (a naive datetime); for daily bars,
        the bar's date at midnight, as the default clock gives it."""
        ns = int(self.times_ns[position])
        if self.daily:
            from .dateutils import num2date

            return num2date(float(self.times[position]))
        return self.hours.local(ns)

    def __iter__(self) -> Iterator[TimeSlice]:
        if not self._bound:
            raise RuntimeError("ExchangeClock.bind(dtnums) must run before iterating")
        return UnionClock.__iter__(self)
