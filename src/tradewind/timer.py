"""Timers: monthly and weekly triggers delivered to ``Strategy.notify_timer``.

``when=Timer.SESSION_START`` fires before the strategy's ``next`` on the first bar whose
date satisfies the rule; ``monthdays=[1]`` fires on the 1st, and with ``monthcarry`` on
the first bar after it when the 1st has no bar (weekend, holiday). ``weekdays`` likewise
(Monday is 1), with ``weekcarry``.

Under an ``ExchangeClock`` (opt-in, spec/05 § 6.1) timers run in the place's time instead:
``SESSION_START`` fires at the session's open plus ``offset``, ``SESSION_END`` at its close
plus ``offset``, a ``datetime.time`` at that time of the trade date on the place's wall
clock (or in ``tzdata``), each at the first slice at or after that instant and at most once
per session; ``repeat`` fires again every ``repeat`` until the close. ``monthdays`` /
``weekdays`` / ``allow`` filter on the trade date. ``SESSION_START`` and time-of-day timers
fire before the strategy's ``next`` (phase 11), ``SESSION_END`` after it (phase 15).
"""

from __future__ import annotations

import datetime as _dt


class Timer:
    SESSION_TIME, SESSION_START, SESSION_END = 0, 1, 2

    def __init__(self, tid, owner, strats=False, when=None, offset=_dt.timedelta(), repeat=_dt.timedelta(),
                 weekdays=(), weekcarry=False, monthdays=(), monthcarry=True, allow=None,
                 tzdata=None, cheat=False, *args, **kwargs):
        self.tid = tid
        self.owner = owner
        self.strats = strats
        self.when = when if when is not None else Timer.SESSION_START
        self.offset, self.repeat = offset, repeat
        self.weekdays = list(weekdays or ())
        self.weekcarry = weekcarry
        self.monthdays = list(monthdays or ())
        self.monthcarry = monthcarry
        self.allow = allow
        self.tzdata = tzdata
        self.cheat = cheat
        self.args, self.kwargs = args, kwargs
        self._last_month = None   # (year, month) of the last firing
        self._last_week = None    # (isoyear, isoweek) of the last firing
        self._pending_carry = False
        self._session = None      # exchange time: the session index last seen
        self._fired = 0           # exchange time: firings in that session
        self._allowed = None      # exchange time: the date filter's verdict for it

    def check(self, date: _dt.date) -> bool:
        """Does the timer fire on a bar dated ``date``?"""
        if self.allow is not None and not self.allow(date):
            return False
        fire = False
        if self.monthdays:
            key = (date.year, date.month)
            due = date.day in self.monthdays or (
                self.monthcarry and any(date.day > d for d in self.monthdays)
            )
            if key != self._last_month and due:
                fire = True
            if fire:
                self._last_month = key
        elif self.weekdays:
            iso = date.isocalendar()
            key = (iso[0], iso[1])
            wd = date.isoweekday()
            due = wd in self.weekdays or (self.weekcarry and any(wd > d for d in self.weekdays))
            if key != self._last_week and due:
                fire = True
            if fire:
                self._last_week = key
        else:
            fire = True  # every bar
        return fire

    # -- exchange time (ExchangeClock) ---------------------------------------------------------
    def fires_end(self) -> bool:
        """Fires after the strategy's ``next`` (phase 15) rather than before it."""
        return self.when == Timer.SESSION_END

    def _target_ns(self, clock, k: int) -> int:
        table = clock.sessions
        offset = _td_ns(self.offset)
        if self.when == Timer.SESSION_END:
            return int(table.closes[k]) + offset
        if isinstance(self.when, _dt.time):
            day = table.days[k].astype(object)
            tz = self.tzdata
            if tz is None:
                return clock.hours.to_ns(day, self.when) + offset
            from .calendars import _to_ns, _zone

            return _to_ns(_dt.datetime.combine(day, self.when, tzinfo=_zone(tz))) + offset
        return int(table.opens[k]) + offset

    def check_at(self, clock, position: int, now: int) -> bool:
        """Does the timer fire at slice ``position`` (time ``now``, ns UTC) of an
        ``ExchangeClock``?"""
        k = clock.session_at(position)
        if k < 0:
            return False
        if k != self._session:
            self._session, self._fired, self._allowed = k, 0, None
        if self._allowed is False:
            return False
        repeat = _td_ns(self.repeat)
        if clock.daily:
            # a daily bar stands for its whole session: every timer of the session is due
            now, repeat = int(clock.sessions.closes[k]) + _td_ns(self.offset) + 1, 0
        if self._fired and not repeat:
            return False
        first = self._target_ns(clock, k)
        due = first + self._fired * repeat
        if now < due:
            return False
        if self._fired and due > int(clock.sessions.closes[k]):
            return False
        if self._allowed is None:
            self._allowed = self.check(clock.sessions.days[k].astype(object))
            if not self._allowed:
                return False
        # one firing per slice; a slice that comes late counts the targets it passed
        self._fired = (now - first) // repeat + 1 if repeat else 1
        return True


def _td_ns(td) -> int:
    if not td:
        return 0
    return (td.days * 86_400 + td.seconds) * 1_000_000_000 + td.microseconds * 1_000
