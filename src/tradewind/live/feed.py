"""The live feed (spec/05 § 6.4 "Live", spec/07 § 4): a ``PandasData`` that grows by append.

The feed is built from a warm-up history (a DataFrame, as for a backtest) and reserves
``capacity`` empty rows after it. A bar that closes during the session is written into the
next free row -- into the same memory ``MarketState`` and the strategy read, since their line
buffers are views on one array -- and the clock then moves the cursor onto it, exactly as in
a backtest. Nothing is ever reallocated during a run: a session that fills the capacity
stops with ``CapacityError`` and resumes from its journal with a fresh one.

What a strategy can see differs from a backtest in one way only: the rows after the current
bar are empty (NaN), so a look-ahead read (``data.close[1]``, ``datetime.date(1)``) that a
backtest silently allows gets NaN or raises.
"""

from __future__ import annotations

import hashlib

import numpy as np

from ..feeds import LINE_ALIASES, PandasData

_NS_PER_DAY = 86_400.0 * 1e9


def ns2num(ns: int) -> float:
    """int64 ns since the Unix epoch -> the compat float day, with the arithmetic of
    ``stamps2num`` (so a live bar's date has the bits a backtest gives the same date)."""
    return 719163.0 + float(np.float64(ns)) / _NS_PER_DAY


class CapacityError(RuntimeError):
    """The feed's reserved rows are full: resume the session from its journal."""


class LiveFeed(PandasData):
    params = (
        ("symbol", None),        # the venue's symbol; the feed's name by default
        ("capacity", 5000),      # live bars the session can hold (20 years of daily bars)
    )

    def _load_arrays(self):
        dtnum, arrays = super()._load_arrays()
        n = len(dtnum)
        cap = int(self.p.capacity)
        self._tw_history_rows = n
        pad = np.full(cap, np.nan)
        dtnum = np.concatenate((dtnum, pad))
        arrays = {k: np.concatenate((v, pad)) for k, v in arrays.items()}
        return dtnum, arrays

    def _start(self):
        super()._start()
        if not hasattr(self, "_tw_rows"):
            self._tw_rows = self._tw_history_rows

    # -- what the session knows ------------------------------------------------------------
    @property
    def symbol(self) -> str:
        return self.p.symbol or self._name

    @property
    def rows(self) -> int:
        """Rows filled: the history plus the live bars appended."""
        return self._tw_rows

    @property
    def history_rows(self) -> int:
        return self._tw_history_rows

    def last_time_ns(self) -> int | None:
        """The stamp of the last row filled (ns), None when the feed is empty."""
        if not self._tw_rows:
            return None
        num = float(self.lines.datetime.array[self._tw_rows - 1])
        return round((num - 719163.0) * _NS_PER_DAY)

    def history_digest(self) -> str:
        h = hashlib.sha256()
        n = self._tw_history_rows
        for buf in self.lines:
            h.update(np.ascontiguousarray(buf.array[:n]).tobytes())
        return h.hexdigest()

    def history_columns(self) -> dict:
        """The warm-up history as plain lists (what the journal's header keeps)."""
        n = self._tw_history_rows
        out = {"time": [round((float(x) - 719163.0) * _NS_PER_DAY) for x in self.lines.datetime.array[:n]]}
        for k, alias in enumerate(LINE_ALIASES):
            if k:
                out[alias] = self.lines[k].array[:n].tolist()
        return out

    # -- growth ----------------------------------------------------------------------------
    def append(self, time_ns: int, open_: float, high: float, low: float, close: float,
               volume: float = 0.0, openinterest: float = float("nan")) -> int:
        """Write one closed bar into the next free row; return the row."""
        row = self._tw_rows
        if row >= self.buflen():
            raise CapacityError(f"feed {self._name!r}: {self.buflen()} rows full; resume from the journal")
        dtnum = ns2num(time_ns)
        if row and not dtnum > self.lines.datetime.array[row - 1]:
            raise ValueError(f"feed {self._name!r}: bar at {time_ns} is not after the last one")
        values = (dtnum, open_, high, low, close, volume, openinterest)
        for buf, value in zip(self.lines, values):
            buf.array[row] = value
        self._tw_rows = row + 1
        return row


def history_frame(columns: dict):
    """The DataFrame a ``LiveFeed`` takes, from ``LiveFeed.history_columns`` (journal header)."""
    import pandas as pd

    index = pd.to_datetime(np.asarray(columns["time"], dtype=np.int64), unit="ns")
    data = {k: np.asarray(v, dtype=np.float64) for k, v in columns.items() if k != "time"}
    return pd.DataFrame(data, index=index)


__all__ = ["CapacityError", "LiveFeed", "history_frame", "ns2num"]
