"""Bar consolidators: minute bars into N-minute, hourly and daily bars, in the place's time.

Read as a specification from QuantConnect Lean (Apache 2.0, see ``NOTICE``): its
``TradeBarConsolidator`` (by period and by count) and the convention that a bar is stamped
at its **end** and is complete when the clock reaches that end (``Scan``). The code is
Tradewind's own (spec/06 § 2).

Conventions, the same for the vectorised and the streaming form:

* input bars are stamped at their end (int64 ns UTC); a one-minute bar stamped 09:31 New
  York covers 09:30-09:31;
* a time bin is ``(end - period, end]`` in the place's wall-clock time. Bins are anchored at
  the session open when a session is given (09:30, 09:35, ... for 5-minute bars on a
  09:30-16:00 session; the hourly bars are 09:30-10:30 ... 15:30-16:00, the last one clipped
  at the close), at local midnight otherwise (Lean's ``RoundDown``);
* a consolidated bar is stamped at its **bin end**, never at its last input bar: a strategy
  that sees it at that time knows nothing it could not know then. A daily bar is stamped at
  the session close (early closes included when ``hours`` knows them) or, without a session,
  at the next local midnight;
* bars outside the session are dropped when a session is given (extended hours), kept
  otherwise;
* open = first, high = max, low = min, close = last, volume = sum, of the input bars in the
  bin. A bin without input bars produces no bar (no fill-forward: the clock of the engine
  repeats the last bar, as for any feed).
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from typing import NamedTuple

import numpy as np
import pandas as pd

from .feeds import DataBase

NS_PER_MIN = 60_000_000_000
NS_PER_DAY = 86_400_000_000_000
OHLCV = ("open", "high", "low", "close", "volume")


def period_ns(period) -> int | None:
    """``"5min"``, ``"1h"``, ``pd.Timedelta``, ``datetime.timedelta`` or minutes (int) -> ns;
    ``"D"`` / ``"1D"`` / ``"session"`` / ``"daily"`` -> ``None`` (one bar per session)."""
    if isinstance(period, str) and period.strip().lower() in ("d", "1d", "day", "daily", "session"):
        return None
    if isinstance(period, (int, np.integer)) and not isinstance(period, bool):
        ns = int(period) * NS_PER_MIN
    else:
        ns = int(pd.Timedelta(period).as_unit("ns").value)
    if ns <= 0:
        raise ValueError(f"a consolidation period must be positive, got {period!r}")
    if ns >= NS_PER_DAY:
        if ns == NS_PER_DAY:
            return None
        raise ValueError("periods longer than a day are not consolidated here (use a daily bar)")
    return ns


def _time_ns(t: _dt.time) -> int:
    return ((t.hour * 60 + t.minute) * 60 + t.second) * 1_000_000_000 + t.microsecond * 1000


def _local_ns(t_utc: np.ndarray, tz) -> np.ndarray:
    """Wall-clock time of ``tz`` as int64 ns (UTC ns + the zone's offset at that instant)."""
    if tz is None or str(tz) == "UTC":
        return t_utc.copy()
    idx = pd.DatetimeIndex(t_utc.astype("datetime64[ns]")).tz_localize("UTC").tz_convert(tz)
    return idx.tz_localize(None).as_unit("ns").asi8


class _Sessions:
    """Per local day: the session's open and close as local wall-clock ns, or none."""

    def __init__(self, tz, session=None, hours=None):
        self.tz = tz
        self.hours = hours
        self.fixed = None
        if session is not None:
            open_t, close_t = session
            self.fixed = (_time_ns(open_t), _time_ns(close_t))
        self._cache: dict[int, tuple[int, int] | None] = {}

    @property
    def anchored(self) -> bool:
        return self.fixed is not None or self.hours is not None

    def bounds(self, day: int) -> tuple[int, int] | None:
        """``day`` = local midnight in ns; (open, close) local ns, or None (no session)."""
        if self.fixed is not None:
            return day + self.fixed[0], day + self.fixed[1]
        if self.hours is None:
            return day, day + NS_PER_DAY
        got = self._cache.get(day, False)
        if got is not False:
            return got
        date64 = np.datetime64(day, "ns").astype("datetime64[D]")
        s = self.hours.session(date64)
        if s is None:
            out = None
        else:
            o, c = (int(x) for x in s)
            # the session's instants as wall-clock ns of the place
            lo = _local_ns(np.array([o, c], dtype=np.int64), self.tz)
            out = (int(lo[0]), int(lo[1]))
        self._cache[day] = out
        return out


def bin_ends(t_end: np.ndarray, period, tz="UTC", session=None, hours=None) -> np.ndarray:
    """The bin end (int64 ns UTC) of each end-stamped bar; ``-1`` for a bar outside every
    session (dropped). ``period`` as in ``period_ns``."""
    t_end = np.asarray(t_end, dtype=np.int64)
    pns = period_ns(period)
    if isinstance(hours, str):
        from .calendars import get_calendar

        hours = get_calendar(hours)
    if tz is None and hours is not None:
        tz = getattr(hours, "tz", None)
    tz = "UTC" if tz is None else tz
    sessions = _Sessions(tz, session, hours)
    local = _local_ns(t_end, tz)
    # an end-stamped bar ending at midnight belongs to the day before
    day = ((local - 1) // NS_PER_DAY) * NS_PER_DAY
    out = np.full(len(t_end), -1, dtype=np.int64)
    if not len(t_end):
        return out
    uniq, inverse = np.unique(day, return_inverse=True)
    opens = np.empty(len(uniq), dtype=np.int64)
    closes = np.empty(len(uniq), dtype=np.int64)
    has = np.ones(len(uniq), dtype=bool)
    for k, d in enumerate(uniq.tolist()):
        b = sessions.bounds(d)
        if b is None:
            has[k] = False
            opens[k] = closes[k] = 0
        else:
            opens[k], closes[k] = b
    o, c, h = opens[inverse], closes[inverse], has[inverse]
    inside = h & (local > o) & (local <= c)
    if pns is None:
        end_local = c
    else:
        k = -((o - local) // pns)                    # ceil((local - open) / period)
        end_local = np.minimum(o + k * pns, c)
    # back to UTC with the offset of the bar itself (exact unless the zone changes its
    # offset inside the bin: a 02:00 bin on a DST night, never inside an exchange session)
    ends = t_end + (end_local - local)
    out[inside] = ends[inside]
    return out


class BarArrays(NamedTuple):
    timestamps: np.ndarray        # int64 ns UTC, the bin ends
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    count: np.ndarray             # input bars per output bar


def _aggregate(t_out: np.ndarray, cols: Mapping[str, np.ndarray], starts: np.ndarray,
               lasts: np.ndarray) -> BarArrays:
    vol = np.asarray(cols.get("volume", np.zeros(len(cols["close"]))), dtype=np.float64)
    vol = np.where(np.isnan(vol), 0.0, vol)
    return BarArrays(
        t_out,
        np.asarray(cols["open"], dtype=np.float64)[starts],
        np.maximum.reduceat(np.asarray(cols["high"], dtype=np.float64), starts),
        np.minimum.reduceat(np.asarray(cols["low"], dtype=np.float64), starts),
        np.asarray(cols["close"], dtype=np.float64)[lasts],
        np.add.reduceat(vol, starts),
        (lasts - starts + 1).astype(np.int64),
    )


def consolidate(timestamps: np.ndarray, columns: Mapping[str, np.ndarray], period="5min",
                tz="UTC", session=None, hours=None) -> BarArrays:
    """Vectorised time consolidation of end-stamped bars (sorted, unique timestamps).

    ``columns`` holds ``open high low close`` (``volume`` optional). ``session`` is a pair of
    ``datetime.time`` (open, close) in ``tz``; ``hours`` an ``ExchangeHours`` (spec/05 § 6.1)
    whose ``session(day)`` gives each day's open and close, early closes and holidays
    included -- it takes precedence over ``session``. With neither, bins are anchored at
    local midnight and every bar is kept.
    """
    t = np.asarray(timestamps, dtype=np.int64)
    ends = bin_ends(t, period, tz, session, hours)
    keep = np.flatnonzero(ends >= 0)
    if not len(keep):
        empty = np.empty(0)
        return BarArrays(np.empty(0, dtype=np.int64), empty, empty, empty, empty, empty,
                         np.empty(0, dtype=np.int64))
    kept_ends = ends[keep]
    cols = {k: np.asarray(v)[keep] for k, v in columns.items()}
    change = np.flatnonzero(np.diff(kept_ends)) + 1
    starts = np.concatenate(([0], change)).astype(np.int64)
    lasts = np.concatenate((change - 1, [len(kept_ends) - 1])).astype(np.int64)
    return _aggregate(kept_ends[starts], cols, starts, lasts)


def consolidate_count(timestamps: np.ndarray, columns: Mapping[str, np.ndarray], count: int) -> BarArrays:
    """Every ``count`` consecutive bars into one, stamped at its last bar (complete when that
    bar arrives); a trailing incomplete group is dropped (Lean's count consolidator)."""
    if count < 1:
        raise ValueError("count must be >= 1")
    t = np.asarray(timestamps, dtype=np.int64)
    n = (len(t) // count) * count
    starts = np.arange(0, n, count, dtype=np.int64)
    lasts = starts + count - 1
    cols = {k: np.asarray(v)[:n] for k, v in columns.items()}
    return _aggregate(t[lasts], cols, starts, lasts) if n else consolidate(t[:0], cols)


class Bar(NamedTuple):
    time: int          # ns UTC, the bin end
    open: float
    high: float
    low: float
    close: float
    volume: float
    count: int


class TimeBarConsolidator:
    """The streaming form, bar by bar (live, or a strategy that consolidates by itself).

    ``update(time, open, high, low, close, volume)`` takes one end-stamped bar and returns the
    bars it completes (zero or one); ``scan(now)`` returns the bar whose bin ended at or before
    ``now`` (the clock passed its end with no later input). In a slice, ``update`` with the
    slice's bar comes first, then ``scan`` with the slice's time. Every completed bar is also
    handed to ``on_bar`` when given. Over the same input bars, scanning at each input time,
    it produces exactly the bars of ``consolidate`` (tested).
    """

    def __init__(self, period="5min", tz="UTC", session=None, hours=None, on_bar=None):
        self.period, self.tz, self.session, self.hours = period, tz, session, hours
        self.on_bar = on_bar
        self._end = None
        self._o = self._h = self._l = self._c = self._v = 0.0
        self._n = 0

    def _emit(self) -> list[Bar]:
        bar = Bar(int(self._end), self._o, self._h, self._l, self._c, self._v, self._n)
        self._end = None
        if self.on_bar is not None:
            self.on_bar(bar)
        return [bar]

    def update(self, time, open, high, low, close, volume=0.0) -> list[Bar]:
        end = int(bin_ends(np.array([time], dtype=np.int64), self.period, self.tz, self.session,
                           self.hours)[0])
        out: list[Bar] = []
        if self._end is not None and end != self._end:
            out += self._emit()
        if end < 0:
            return out
        v = 0.0 if volume is None or np.isnan(volume) else float(volume)
        if self._end is None:
            self._end = end
            self._o, self._h, self._l, self._c, self._v, self._n = (float(open), float(high), float(low),
                                                                    float(close), v, 1)
        else:
            self._h = max(self._h, float(high))
            self._l = min(self._l, float(low))
            self._c = float(close)
            self._v += v
            self._n += 1
        if int(time) >= self._end:          # the bar that ends the bin completes it
            out += self._emit()
        return out

    def scan(self, now) -> list[Bar]:
        if self._end is not None and int(now) >= self._end:
            return self._emit()
        return []


def frame_from_bars(bars: BarArrays) -> pd.DataFrame:
    """A ``PandasData``-ready frame (UTC index, OHLCV) from consolidated arrays."""
    index = pd.DatetimeIndex(bars.timestamps.astype("datetime64[ns]")).tz_localize("UTC")
    return pd.DataFrame({k: getattr(bars, k) for k in OHLCV}, index=index)


__all__ = ["BarArrays", "TimeBarConsolidator", "bin_ends", "consolidate", "consolidate_count",
           "frame_from_bars", "period_ns"]


# -- the consolidated feed ---------------------------------------------------------------------

class ConsolidatedData(DataBase):
    """A feed whose bars are another feed's bars consolidated (``Engine.consolidate``).

    Its bars are computed when the run starts, from the source feed's whole series, and are
    stamped at their bin end: on the engine's clock (the union of the feeds' timestamps) the
    5-minute bar ending 09:35 arrives in the same slice as the minute bar stamped 09:35, so
    ``data5.close[0]`` read at 09:37 is the 09:30-09:35 bar -- complete, nothing ahead. A bin
    is a pure function of the bars inside it, which is why it may be computed before the run
    (spec/05 § 7.4 allows this for indicators; no portfolio state is involved).
    """

    params = (
        ("source", None),
        ("period", "5min"),
        ("count", None),
        ("tz", "UTC"),
        ("session", None),
        ("hours", None),
    )

    def _load_arrays(self):
        from .clock import ns2num
        from .dateutils import num2ns

        src = self.p.source
        if src is None:
            raise ValueError("ConsolidatedData needs a source feed")
        if not src._started:
            src._start()
        t = num2ns(src._dtnum)
        cols = {k: src._arrays[k] for k in OHLCV}
        if self.p.count:
            bars = consolidate_count(t, cols, int(self.p.count))
        else:
            bars = consolidate(t, cols, self.p.period, self.p.tz, self.p.session, self.p.hours)
        n = len(bars.timestamps)
        arrays = {k: getattr(bars, k) for k in OHLCV}
        arrays["openinterest"] = np.full(n, np.nan)
        return ns2num(bars.timestamps), arrays


__all__ += ["ConsolidatedData"]
