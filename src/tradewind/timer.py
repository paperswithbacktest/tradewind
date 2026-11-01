"""Timers: monthly and weekly triggers delivered to ``Strategy.notify_timer``.

``when=Timer.SESSION_START`` fires before the strategy's ``next`` on the first bar whose
date satisfies the rule; ``monthdays=[1]`` fires on the 1st, and with ``monthcarry`` on
the first bar after it when the 1st has no bar (weekend, holiday). ``weekdays`` likewise
(Monday is 1), with ``weekcarry``.
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
        self.cheat = cheat
        self.args, self.kwargs = args, kwargs
        self._last_month = None   # (year, month) of the last firing
        self._last_week = None    # (isoyear, isoweek) of the last firing
        self._pending_carry = False

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
