"""Exchange calendars: trading days, sessions, holidays, early closes and time zones.

An ``ExchangeCalendar`` implements the ``ExchangeHours`` protocol of spec/05 § 6.1 for one
place: which days trade, when each session opens and closes in the place's time zone, and
the instants (int64 ns since the epoch, UTC: the engine's ``Timestamp``) those are.

The rules are written here, from the exchanges' published holiday rules (no data file, no
network). The shape -- weekly sessions, a list of holidays, early closes per exchange --
follows QuantConnect Lean's ``MarketHoursDatabase`` / ``SecurityExchangeHours`` (Apache 2.0,
read as a specification, see ``NOTICE``); the code and the rules are Tradewind's own.

Four calendars ship:

* ``NYSE`` -- 09:30-16:00 America/New_York, 13:00 early closes. Holidays from 1971 (the
  Monday-holiday rules) with the special closures since then; early closes from 1993.
  Earlier dates follow the 1971 rules, which is not what the exchange did.
* ``NASDAQ`` -- the NYSE rules (the two have shared their holiday schedule since the 1970s;
  the few historical differences are not encoded).
* ``CME`` -- CME Globex equity index futures: the session of trade date D runs from 17:00
  (America/Chicago) the day before to 16:00 on D, with the daily hour of maintenance in
  between. Closed on New Year's Day, Good Friday and Christmas; the other US holidays halt
  at 12:00, the NYSE early-close days at 12:15. An approximation of the published
  schedules: the one-off changes (Good Fridays with a jobs report, days of mourning) are
  not encoded.
* ``CRYPTO`` -- every day, 00:00-24:00 UTC.

The engine's default stays ``clock.AlwaysOpen`` (the oracle's: daily bars carry dates only);
a calendar is opt-in through ``Engine.set_hours`` and ``clock.ExchangeClock``.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Callable, Iterable
from typing import NamedTuple

import numpy as np

try:  # Python >= 3.9; the time-zone rules come from the system or from ``tzdata``
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment,misc]

_UTC = _dt.timezone.utc
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_UTC)
_NS_PER_DAY = 86_400_000_000_000
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


# -- date rules ------------------------------------------------------------------------------
def nth_weekday(year: int, month: int, weekday: int, n: int) -> _dt.date:
    """The ``n``-th ``weekday`` (Monday = 0) of a month; ``n = -1`` is the last."""
    if n > 0:
        first = _dt.date(year, month, 1)
        return first + _dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    nxt = _dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - _dt.timedelta(days=1)
    return last - _dt.timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> _dt.date:
    """Easter Sunday of the Gregorian calendar (the anonymous computus)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return _dt.date(year, month, day + 1)


def observed(day: _dt.date, saturday: bool = True) -> _dt.date | None:
    """The weekday a fixed-date holiday is observed on: Sunday moves to Monday, Saturday to
    Friday (``saturday=False``: not observed at all, as the NYSE does for New Year's Day)."""
    wd = day.weekday()
    if wd == SUN:
        return day + _dt.timedelta(days=1)
    if wd == SAT:
        return day - _dt.timedelta(days=1) if saturday else None
    return day


def _us_market_holidays(year: int) -> dict[str, _dt.date]:
    """The regular full-day holidays of the US stock exchanges, by name (1971 rules)."""
    out: dict[str, _dt.date | None] = {
        "new_year": observed(_dt.date(year, 1, 1), saturday=False),
        "mlk": nth_weekday(year, 1, MON, 3) if year >= 1998 else None,
        "washington": nth_weekday(year, 2, MON, 3),
        "good_friday": easter(year) - _dt.timedelta(days=2),
        "memorial": nth_weekday(year, 5, MON, -1),
        "juneteenth": observed(_dt.date(year, 6, 19)) if year >= 2022 else None,
        "independence": observed(_dt.date(year, 7, 4)),
        "labor": nth_weekday(year, 9, MON, 1),
        "thanksgiving": nth_weekday(year, 11, THU, 4),
        "christmas": observed(_dt.date(year, 12, 25)),
    }
    if year in (1972, 1976, 1980):  # presidential election days, closed until 1980
        out["election"] = nth_weekday(year, 11, MON, 1) + _dt.timedelta(days=1)
    return {k: v for k, v in out.items() if v is not None}


#: One-off full closures of the NYSE since 1971 (funerals and days of mourning, weather,
#: the blackout of 1977, September 2001).
NYSE_SPECIAL_CLOSURES = frozenset(_dt.date.fromisoformat(d) for d in (
    "1972-12-28",                                      # President Truman's funeral
    "1973-01-25",                                      # President Johnson's funeral
    "1977-07-14",                                      # New York City blackout
    "1985-09-27",                                      # Hurricane Gloria
    "1994-04-27",                                      # President Nixon's funeral
    "2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14",  # September 11
    "2004-06-11",                                      # President Reagan's funeral
    "2007-01-02",                                      # President Ford's funeral
    "2012-10-29", "2012-10-30",                        # Hurricane Sandy
    "2018-12-05",                                      # President George H. W. Bush's funeral
    "2025-01-09",                                      # President Carter's funeral
))

#: One-off 13:00 closes of the NYSE outside the regular rules.
NYSE_SPECIAL_EARLY_CLOSES = frozenset(_dt.date.fromisoformat(d) for d in (
    "1997-12-26", "2002-07-05", "2003-12-26",
))

EARLY_CLOSE_RULES_FROM = 1993


def nyse_holidays(year: int) -> frozenset[_dt.date]:
    """The weekdays the NYSE is closed in ``year``."""
    days = set(_us_market_holidays(year).values())
    days.update(d for d in NYSE_SPECIAL_CLOSURES if d.year == year)
    return frozenset(d for d in days if d.weekday() < SAT)


def nyse_early_closes(year: int) -> dict[_dt.date, _dt.time]:
    """The NYSE's 13:00 closes in ``year``: the day before Independence Day when the 4th is
    Tuesday to Friday, the day after Thanksgiving, Christmas Eve from Monday to Thursday."""
    out: dict[_dt.date, _dt.time] = {}
    one = _dt.time(13, 0)
    if year >= EARLY_CLOSE_RULES_FROM:
        july4 = _dt.date(year, 7, 4)
        if TUE <= july4.weekday() <= FRI:
            out[july4 - _dt.timedelta(days=1)] = one
        out[nth_weekday(year, 11, THU, 4) + _dt.timedelta(days=1)] = one
        eve = _dt.date(year, 12, 24)
        if eve.weekday() <= THU:
            out[eve] = one
    for d in NYSE_SPECIAL_EARLY_CLOSES:
        if d.year == year:
            out[d] = one
    closed = nyse_holidays(year)
    return {d: t for d, t in out.items() if d not in closed}


def cme_holidays(year: int) -> frozenset[_dt.date]:
    """Trade dates CME Globex equity index futures do not have: New Year's Day, Good Friday,
    Christmas (as the NYSE observes them) and the week of September 11, 2001."""
    rules = _us_market_holidays(year)
    days = {rules[k] for k in ("new_year", "good_friday", "christmas")}
    days.update(d for d in NYSE_SPECIAL_CLOSURES if d.year == 2001 and year == 2001)
    return frozenset(d for d in days if d.weekday() < SAT)


def cme_early_closes(year: int) -> dict[_dt.date, _dt.time]:
    """CME equity index halts: 12:00 Chicago on the other US exchange holidays, 12:15 on
    the NYSE's early-close days."""
    closed = cme_holidays(year)
    out = {d: _dt.time(12, 15) for d in nyse_early_closes(year)}
    for name, d in _us_market_holidays(year).items():
        if name not in ("new_year", "good_friday", "christmas", "election") and d.weekday() < SAT:
            out[d] = _dt.time(12, 0)
    return {d: t for d, t in out.items() if d not in closed}


# -- the calendar ----------------------------------------------------------------------------
class SessionTable(NamedTuple):
    """The sessions of a range of trading days, as arrays (one row per trading day)."""

    days: np.ndarray        # datetime64[D], the trading (trade) dates
    opens: np.ndarray       # int64 ns UTC
    closes: np.ndarray      # int64 ns UTC

    def __len__(self) -> int:
        return len(self.days)


def _as_date(day) -> _dt.date:
    if isinstance(day, _dt.datetime):
        return day.date()
    if isinstance(day, _dt.date):
        return day
    if isinstance(day, str):
        return _dt.date.fromisoformat(day[:10])
    return np.datetime64(day, "D").astype(object)


def _as_ns(time) -> int:
    """A ``Timestamp`` (int ns UTC), ``np.datetime64`` or aware ``datetime`` -> int ns."""
    if isinstance(time, _dt.datetime):
        if time.tzinfo is None:
            raise ValueError("a naive datetime is ambiguous here: give it a time zone")
        return _to_ns(time)
    if isinstance(time, np.datetime64):
        return int(time.astype("datetime64[ns]").astype(np.int64))
    return int(time)


def _to_ns(aware: _dt.datetime) -> int:
    delta = aware - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _zone(tz):
    if isinstance(tz, _dt.tzinfo):
        return tz
    if tz in ("UTC", "utc", "Etc/UTC"):
        return _UTC
    if ZoneInfo is None:  # pragma: no cover
        raise RuntimeError("time zones need the zoneinfo module (Python >= 3.9)")
    return ZoneInfo(tz)


class ExchangeCalendar:
    """The hours of one place (spec/05 § 6.1 ``ExchangeHours``).

    ``open_time`` on the trade date plus ``open_day_offset`` days (CME: -1, the evening
    before) to ``close_time`` on the trade date plus ``close_day_offset`` days (24/7: +1,
    midnight). ``holidays(year)`` gives the weekdays without a session,
    ``early_closes(year)`` the trade dates whose session closes at another time.
    """

    def __init__(self, name: str, tz, open_time: _dt.time, close_time: _dt.time, *,
                 weekdays: Iterable[int] = (MON, TUE, WED, THU, FRI),
                 open_day_offset: int = 0, close_day_offset: int = 0,
                 holidays: Callable[[int], Iterable[_dt.date]] | None = None,
                 early_closes: Callable[[int], dict[_dt.date, _dt.time]] | None = None,
                 description: str = ""):
        self.name = name
        self.tz_name = tz if isinstance(tz, str) else str(tz)
        self.tz = _zone(tz)
        self.open_time = open_time
        self.close_time = close_time
        self.weekdays = frozenset(weekdays)
        self.open_day_offset = int(open_day_offset)
        self.close_day_offset = int(close_day_offset)
        self._holiday_rule = holidays
        self._early_rule = early_closes
        self.description = description
        self._holidays: dict[int, frozenset[_dt.date]] = {}
        self._early: dict[int, dict[_dt.date, _dt.time]] = {}

    # -- rules ---------------------------------------------------------------------------
    def holidays(self, year: int) -> frozenset[_dt.date]:
        """The weekdays of ``year`` without a session."""
        out = self._holidays.get(year)
        if out is None:
            out = frozenset(self._holiday_rule(year)) if self._holiday_rule else frozenset()
            self._holidays[year] = out
        return out

    def early_closes(self, year: int) -> dict[_dt.date, _dt.time]:
        """The trade dates of ``year`` whose session closes early, with the closing time."""
        out = self._early.get(year)
        if out is None:
            out = dict(self._early_rule(year)) if self._early_rule else {}
            self._early[year] = out
        return out

    # -- ExchangeHours ---------------------------------------------------------------------
    def is_trading_day(self, day) -> bool:
        d = _as_date(day)
        return d.weekday() in self.weekdays and d not in self.holidays(d.year)

    def session(self, day) -> tuple[np.int64, np.int64] | None:
        """(open, close) of the trade date ``day`` in ns UTC, or None when it does not trade."""
        d = _as_date(day)
        if not self.is_trading_day(d):
            return None
        return np.int64(self._open_ns(d)), np.int64(self._close_ns(d))

    def is_open(self, time) -> bool:
        """The place trades at ``time`` (int ns UTC, ``datetime64`` or aware datetime):
        ``open <= time < close`` for one of its sessions."""
        ns = _as_ns(time)
        local = self.local_date(ns)
        for k in (-1, 0, 1, 2):
            d = local + _dt.timedelta(days=k)
            if self.is_trading_day(d) and self._open_ns(d) <= ns < self._close_ns(d):
                return True
        return False

    # -- tables ------------------------------------------------------------------------------
    def trading_days(self, start, end) -> np.ndarray:
        """The trade dates from ``start`` to ``end`` included, as ``datetime64[D]``."""
        d0, d1 = _as_date(start), _as_date(end)
        out = []
        d = d0
        one = _dt.timedelta(days=1)
        while d <= d1:
            if self.is_trading_day(d):
                out.append(d)
            d += one
        return np.array(out, dtype="datetime64[D]")

    def sessions(self, start, end) -> SessionTable:
        """Every session whose trade date lies in ``[start, end]``."""
        days = self.trading_days(start, end)
        dates = days.astype(object)
        opens = np.fromiter((self._open_ns(d) for d in dates), dtype=np.int64, count=len(dates))
        closes = np.fromiter((self._close_ns(d) for d in dates), dtype=np.int64, count=len(dates))
        return SessionTable(days, opens, closes)

    def bar_times(self, start, end, interval) -> np.ndarray:
        """The end times (int64 ns UTC) of the bars of ``interval`` covering every session of
        ``[start, end]``: ``open + interval``, ``open + 2 interval`` ... up to the close (a
        last, shorter bar ends at the close when the session is not a multiple)."""
        step = _interval_ns(interval)
        table = self.sessions(start, end)
        parts = []
        for o, c in zip(table.opens.tolist(), table.closes.tolist()):
            ends = np.arange(o + step, c + 1, step, dtype=np.int64)
            if not len(ends) or ends[-1] != c:
                ends = np.append(ends, np.int64(c))
            parts.append(ends)
        return np.concatenate(parts) if parts else np.empty(0, dtype=np.int64)

    # -- time zone -----------------------------------------------------------------------------
    def local(self, time) -> _dt.datetime:
        """``time`` as a naive datetime on the place's wall clock."""
        ns = _as_ns(time)
        utc = _EPOCH + _dt.timedelta(microseconds=ns // 1_000)
        return utc.astimezone(self.tz).replace(tzinfo=None)

    def local_date(self, time) -> _dt.date:
        return self.local(time).date()

    def to_ns(self, day, at: _dt.time, day_offset: int = 0) -> int:
        """The instant (int ns UTC) of wall-clock time ``at`` on ``day`` (+ ``day_offset``)."""
        d = _as_date(day) + _dt.timedelta(days=day_offset)
        return _to_ns(_dt.datetime.combine(d, at, tzinfo=self.tz))

    def _open_ns(self, d: _dt.date) -> int:
        return self.to_ns(d, self.open_time, self.open_day_offset)

    def _close_ns(self, d: _dt.date) -> int:
        early = self.early_closes(d.year).get(d)
        if early is not None:
            return self.to_ns(d, early, 0)
        return self.to_ns(d, self.close_time, self.close_day_offset)

    def __repr__(self):
        return f"ExchangeCalendar({self.name!r}, {self.tz_name})"


def _interval_ns(interval) -> int:
    if isinstance(interval, _dt.timedelta):
        return (interval.days * 86_400 + interval.seconds) * 1_000_000_000 + interval.microseconds * 1_000
    if isinstance(interval, np.timedelta64):
        return int(interval.astype("timedelta64[ns]").astype(np.int64))
    if isinstance(interval, str):
        units = {"min": 60, "m": 60, "h": 3600, "s": 1, "d": 86_400}
        for suffix in ("min", "h", "s", "d", "m"):
            if interval.endswith(suffix):
                return int(float(interval[: -len(suffix)] or 1) * units[suffix] * 1_000_000_000)
        raise ValueError(f"unknown interval {interval!r}: use '1min', '5min', '1h', '1d'")
    return int(interval)


NYSE = ExchangeCalendar(
    "NYSE", "America/New_York", _dt.time(9, 30), _dt.time(16, 0),
    holidays=nyse_holidays, early_closes=nyse_early_closes,
    description="New York Stock Exchange, regular session",
)
NASDAQ = ExchangeCalendar(
    "NASDAQ", "America/New_York", _dt.time(9, 30), _dt.time(16, 0),
    holidays=nyse_holidays, early_closes=nyse_early_closes,
    description="Nasdaq, regular session (the NYSE holiday rules)",
)
CME = ExchangeCalendar(
    "CME", "America/Chicago", _dt.time(17, 0), _dt.time(16, 0), open_day_offset=-1,
    holidays=cme_holidays, early_closes=cme_early_closes,
    description="CME Globex equity index futures (approximate holiday schedule)",
)
CRYPTO = ExchangeCalendar(
    "CRYPTO", "UTC", _dt.time(0, 0), _dt.time(0, 0), close_day_offset=1,
    weekdays=range(7), description="every day, 00:00-24:00 UTC",
)

CALENDARS: dict[str, ExchangeCalendar] = {c.name: c for c in (NYSE, NASDAQ, CME, CRYPTO)}


def get_calendar(name: str) -> ExchangeCalendar:
    """A shipped calendar by name: ``NYSE``, ``NASDAQ``, ``CME``, ``CRYPTO``."""
    try:
        return CALENDARS[name.upper()]
    except KeyError:
        raise KeyError(f"no calendar {name!r}; shipped: {', '.join(CALENDARS)}") from None


__all__ = [
    "CALENDARS",
    "CME",
    "CRYPTO",
    "NASDAQ",
    "NYSE",
    "ExchangeCalendar",
    "SessionTable",
    "cme_early_closes",
    "cme_holidays",
    "easter",
    "get_calendar",
    "nth_weekday",
    "nyse_early_closes",
    "nyse_holidays",
    "observed",
]
