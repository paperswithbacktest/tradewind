"""The float-day encoding of datetimes used on the ``datetime`` line.

A bar's datetime is stored as a float: the proleptic Gregorian ordinal of the day
(``datetime.date.toordinal``) plus the fraction of the day elapsed. ``date2num`` and
``num2date`` convert both ways. Strategies of the corpus serialise dates through
``num2date`` (30 files), so the encoding is part of the public surface.
"""

from __future__ import annotations

import datetime as _dt
import math

import numpy as np

_SECONDS_PER_DAY = 86400.0


def date2num(dt: _dt.datetime | _dt.date) -> float:
    """``datetime`` -> float days; a bare ``date`` is midnight."""
    if isinstance(dt, _dt.datetime):
        if dt.tzinfo is not None:
            dt = dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        day = dt.toordinal()
        secs = dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6
        return day + secs / _SECONDS_PER_DAY
    return float(dt.toordinal())


def num2date(num: float, tz=None) -> _dt.datetime:
    """float days -> naive ``datetime`` (rounded to the microsecond)."""
    day = math.floor(num)
    frac = num - day
    base = _dt.datetime.fromordinal(day)
    # A float day carries ~1e-10 day (~10 µs) of resolution on current dates, so the
    # seconds are rounded to the millisecond: exact for daily and minute bars.
    secs = round(frac * _SECONDS_PER_DAY, 3)
    out = base + _dt.timedelta(seconds=secs)
    if tz is not None:
        out = out.replace(tzinfo=_dt.timezone.utc).astimezone(tz)
    return out


def num2dt(num: float) -> _dt.datetime:
    return num2date(num)


def stamps2num(index) -> np.ndarray:
    """A pandas ``DatetimeIndex`` (or array of datetimes) -> float-day array."""
    import pandas as pd

    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    # ``asi8`` is in the index's own unit (s, ms, us or ns depending on how the frame was
    # built), so pin the unit before reading the integers.
    idx = idx.as_unit("ns")
    # Days since the proleptic ordinal epoch: ordinal(1970-01-01) == 719163.
    ns = idx.asi8.astype(np.float64)
    return 719163.0 + ns / (_SECONDS_PER_DAY * 1e9)
