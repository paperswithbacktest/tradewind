"""Point-in-time universes: securities that enter and leave during a run (spec/05 § 6.2).

A ``UniverseProvider`` answers, at each slice, which securities entered and which left the
universe (``clock.UniverseChange``). The engine registers the change at phase 1, marks each
security ``in_universe`` or not, and delivers it at phase 12 to a strategy that defines
``notify_universe(change)`` (opt-in, as Lean's ``OnSecuritiesChanged``, read as a
specification: Apache 2.0, see ``NOTICE``): the ``UniverseChange`` itself, SecurityIds that
``feeds(strategy, ids)`` turns into the strategy's feeds. ``Engine.set_universe(provider, on_removal=...)``
decides what happens to the position of a security that leaves.

The engine cannot add feeds during a run, so every symbol the universe ever holds needs a
feed added before ``run()``; the provider maps its symbols to those feeds by name when the
run starts (``bind``). Two providers:

* ``MembershipUniverse``: a member table of ``(date, symbol)`` rows, one snapshot per date
  (the layout of PWB's ``Stocks-Daily-LiquidUniverse``: one row per member and session,
  with a ``rank``). The snapshot of a date holds from that date until the next one.
* ``IntervalUniverse``: one ``[start, end)`` window per symbol (listing and delisting dates,
  index inclusion and exclusion).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from .clock import UniverseChange

NS_PER_DAY = 86_400_000_000_000


def feeds(strategy, ids) -> list:
    """The feeds of SecurityIds ``ids`` (a ``UniverseChange``'s ``added`` or ``removed``),
    as the strategy holds them: ``feeds(self, change.added)`` inside ``notify_universe``."""
    securities = strategy.broker.securities
    return [securities[i].feed for i in ids]


def _ns(values) -> np.ndarray:
    """Dates or datetimes to int64 ns UTC (naive values are UTC), whatever their unit."""
    idx = pd.DatetimeIndex(pd.to_datetime(values))
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return idx.as_unit("ns").asi8.astype(np.int64)


def _offset(lag) -> int:
    return 0 if lag is None else int(pd.Timedelta(lag).as_unit("ns").value)


class _SymbolResolver:
    """Symbol -> SecurityId of the run's securities, by name (the feed's name)."""

    @staticmethod
    def resolve(securities, symbols: Sequence[str]) -> tuple[np.ndarray, list[str]]:
        by_name = {}
        for security in securities:
            by_name.setdefault(str(security.symbol), security.id)
        ids = np.array([by_name.get(str(s), -1) for s in symbols], dtype=np.int64)
        missing = sorted({str(s) for s, i in zip(symbols, ids) if i < 0})
        return ids, missing


class MembershipUniverse:
    """A universe given as dated snapshots: the symbols listed at a date are the members
    from that date (plus ``lag``) until the next date of the table.

    ``table``: a DataFrame with a date column and a symbol column, one row per member and
    date (extra columns are ignored); ``rank`` and ``top`` keep the rows ranked ``top`` or
    better. ``tolerance`` (a ``Timedelta`` or a string such as ``"7D"``): a snapshot older
    than that is no longer in force and the universe is empty until the next one (None: a
    snapshot holds until the next, the last one forever -- pwb-toolbox uses seven days).
    ``lag``: a delay added to every date before it takes effect (``"1D"`` uses a date's
    membership from the next day on, when the table is computed from that day's close).

    Symbols of the table without a feed are listed in ``unresolved`` after the run starts;
    they are ignored. ``managed`` is the set of SecurityIds the table names: those start
    outside the universe, the other feeds are not touched (an index or a hedge added next
    to the universe stays a member all along).
    """

    def __init__(self, table: pd.DataFrame, *, date: str = "date", symbol: str = "symbol",
                 rank: str | None = None, top: int | None = None, tolerance=None, lag=None):
        f = table
        if top is not None:
            rank = rank or "rank"
            f = f[f[rank] <= top]
        self._dates = _ns(f[date]) + _offset(lag)
        self._symbols = f[symbol].astype(str).to_numpy()
        self.tolerance = None if tolerance is None else int(pd.Timedelta(tolerance).as_unit("ns").value)
        self.unresolved: list[str] = []
        self.managed: frozenset[int] = frozenset()
        self._matrix = None
        self._snap = np.empty(0, dtype=np.int64)
        self._feeds: list = []

    def describe(self) -> dict:
        from .fingerprint import table_digest

        return {"rows": len(self._symbols), "tolerance_ns": self.tolerance,
                "table_sha256": table_digest(self._dates, self._symbols)}

    def bind(self, securities) -> None:
        """Map the table's symbols onto the run's securities and build the snapshots: a
        boolean matrix, one row per date of the table, one column per security."""
        ids, self.unresolved = _SymbolResolver.resolve(securities, self._symbols)
        keep = ids >= 0
        dates, ids = self._dates[keep], ids[keep]
        snap, row = np.unique(dates, return_inverse=True)
        matrix = np.zeros((len(snap), len(securities)), dtype=bool)
        matrix[row, ids] = True
        if len(self._dates) and not len(snap):
            # no symbol of the table has a feed: the snapshots exist, all empty
            snap = np.unique(self._dates)
            matrix = np.zeros((len(snap), len(securities)), dtype=bool)
        self._snap, self._matrix = snap, matrix
        self.managed = frozenset(int(i) for i in np.unique(ids))
        self._feeds = [s.feed for s in securities]
        self._current = np.zeros(len(securities), dtype=bool)
        self._key = None

    def _row(self, time: int) -> tuple[Any, np.ndarray | None]:
        k = int(np.searchsorted(self._snap, time, side="right")) - 1
        if k < 0:
            return ("none",), None
        if self.tolerance is not None and time - int(self._snap[k]) > self.tolerance:
            return ("stale", k), None
        return k, self._matrix[k]

    def change(self, time) -> UniverseChange | None:
        if self._matrix is None:
            raise RuntimeError("MembershipUniverse is bound to the run's feeds by Engine.set_universe")
        time = int(time)
        key, row = self._row(time)
        if key == self._key:
            return None
        self._key = key
        new = row if row is not None else np.zeros_like(self._current)
        added = np.flatnonzero(new & ~self._current)
        removed = np.flatnonzero(self._current & ~new)
        self._current = new.copy()
        if not len(added) and not len(removed):
            return None
        return UniverseChange(np.int64(time), tuple(int(i) for i in added), tuple(int(i) for i in removed))

    # -- read by the strategy ------------------------------------------------------------
    @property
    def members(self) -> list:
        """The feeds in the universe now, in SecurityId order."""
        if self._matrix is None:
            return []
        return [self._feeds[i] for i in np.flatnonzero(self._current)]

    def is_member(self, data) -> bool:
        security = getattr(data, "security", None)
        return bool(security is not None and self._matrix is not None and self._current[security.id])


class IntervalUniverse:
    """A universe given as one window per row: ``symbol`` is a member from ``start``
    (included) to ``end`` (excluded; empty or NaT: still a member). Several rows per
    symbol give several windows. ``lag`` delays every boundary, as in
    ``MembershipUniverse``.

    Built from listing data -- ``IntervalUniverse(listing, start="ipoDate",
    end="delistingDate")`` on PWB's ListingStatus -- it keeps a strategy off a name before
    its listing and after its delisting.
    """

    def __init__(self, table: pd.DataFrame, *, symbol: str = "symbol", start: str = "start",
                 end: str = "end", lag=None):
        lag_ns = _offset(lag)
        self._symbols = table[symbol].astype(str).to_numpy()
        # missing dates ("None" strings included): a window open on that side
        starts = pd.Series(pd.to_datetime(table[start], errors="coerce"))
        ends = pd.Series(pd.to_datetime(table[end], errors="coerce"))
        start_ns = _ns(starts.fillna(pd.Timestamp("1970-01-01"))) + lag_ns
        start_ns[starts.isna().to_numpy()] = np.iinfo(np.int64).min
        end_ns = _ns(ends.fillna(pd.Timestamp("1970-01-01"))) + lag_ns
        end_ns[ends.isna().to_numpy()] = np.iinfo(np.int64).max
        self._start, self._end = start_ns, end_ns
        self.unresolved: list[str] = []
        self.managed: frozenset[int] = frozenset()
        self._events: list[tuple[int, int, int]] = []
        self._feeds: list = []
        self._count = None

    def describe(self) -> dict:
        from .fingerprint import table_digest

        return {"rows": len(self._symbols), "table_sha256": table_digest(self._symbols, self._start, self._end)}

    def bind(self, securities) -> None:
        ids, self.unresolved = _SymbolResolver.resolve(securities, self._symbols)
        events = []
        for sid, a, b in zip(ids, self._start, self._end):
            if sid < 0 or b <= a:
                continue
            events.append((int(a), 1, int(sid)))
            if b != np.iinfo(np.int64).max:
                events.append((int(b), -1, int(sid)))
        events.sort()
        self._events = events
        self._pos = 0
        self.managed = frozenset(int(i) for i in ids if i >= 0)
        self._feeds = [s.feed for s in securities]
        self._count = np.zeros(len(securities), dtype=np.int64)   # open windows per security

    def change(self, time) -> UniverseChange | None:
        if self._count is None:
            raise RuntimeError("IntervalUniverse is bound to the run's feeds by Engine.set_universe")
        time = int(time)
        events, pos = self._events, self._pos
        if pos >= len(events) or events[pos][0] > time:
            return None
        before = self._count > 0
        while pos < len(events) and events[pos][0] <= time:
            _, step, sid = events[pos]
            self._count[sid] += step
            pos += 1
        self._pos = pos
        after = self._count > 0
        added = np.flatnonzero(after & ~before)
        removed = np.flatnonzero(before & ~after)
        if not len(added) and not len(removed):
            return None
        return UniverseChange(np.int64(time), tuple(int(i) for i in added), tuple(int(i) for i in removed))

    @property
    def members(self) -> list:
        if self._count is None:
            return []
        return [self._feeds[i] for i in np.flatnonzero(self._count > 0)]

    def is_member(self, data) -> bool:
        security = getattr(data, "security", None)
        return bool(security is not None and self._count is not None and self._count[security.id] > 0)


__all__ = ["IntervalUniverse", "MembershipUniverse", "feeds"]
