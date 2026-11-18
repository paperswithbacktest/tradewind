"""Data feeds: the bars of one instrument, as arrays behind cursors.

``PandasData`` is the feed the corpus and pwb-toolbox build (22 files + the toolbox):
a DataFrame with a ``DatetimeIndex`` (or a datetime column) and OHLCV columns. Column
mapping follows the public parameters: ``datetime=None`` means the index; each price
parameter is a column name, a column position, ``-1`` to find it by its own name
(case-insensitive with ``nocase``), or ``None`` when the column does not exist.
"""

from __future__ import annotations

import numbers
from typing import ClassVar

import numpy as np
import pandas as pd

from .dateutils import date2num, stamps2num
from .lines import Cursor, DatetimeLine, LineArithmetic, LineBuffer, Lines, in_build_stage
from .params import ParamsBase

LINE_ALIASES = ("datetime", "open", "high", "low", "close", "volume", "openinterest")


class TimeFrame:
    Ticks, MicroSeconds, Seconds, Minutes, Days, Weeks, Months, Years, NoTimeFrame = range(1, 10)
    Names: ClassVar[list] = ["", "Ticks", "MicroSeconds", "Seconds", "Minutes", "Days", "Weeks", "Months", "Years", "NoTimeFrame"]

    @classmethod
    def getname(cls, tf, compression=None):
        return cls.Names[tf]


class DataBase(ParamsBase):
    params = (
        ("dataname", None),
        ("name", ""),
        ("compression", 1),
        ("timeframe", TimeFrame.Days),
        ("fromdate", None),
        ("todate", None),
        ("sessionstart", None),
        ("sessionend", None),
        ("plot", True),
    )

    def __init__(self):
        self._name = self.p.name or ""
        self._dataname = self.p.dataname
        self._started = False
        self._tw_env = None
        self._arrays: dict[str, np.ndarray] = {}
        self._dtnum: np.ndarray | None = None
        self._cursor = Cursor()   # replaced at start by the one the lines share
        self.security = None   # set by the broker: the engine's record of this feed (read-only)
        self.provenance = None  # where the data comes from (fingerprint), e.g. {"revision": ...}

    # -- to be provided by subclasses -------------------------------------------------
    def _load_arrays(self) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        raise NotImplementedError

    # -- lifecycle ------------------------------------------------------------------------
    def _start(self):
        if self._started:
            self.lines.home()
            return
        dtnum, arrays = self._load_arrays()
        lo, hi = 0, len(dtnum)
        if self.p.fromdate is not None:
            lo = int(np.searchsorted(dtnum, date2num(self.p.fromdate), side="left"))
        if self.p.todate is not None:
            hi = int(np.searchsorted(dtnum, date2num(self.p.todate), side="right"))
        dtnum = dtnum[lo:hi]
        arrays = {k: v[lo:hi] for k, v in arrays.items()}
        self._dtnum = dtnum
        self._arrays = arrays
        # the seven lines move together: one cursor for all (phase 2 moves it once)
        self._cursor = cursor = Cursor()
        buffers = [DatetimeLine(dtnum, "datetime", owner=self, cursor=cursor)]
        for alias in LINE_ALIASES[1:]:
            buffers.append(LineBuffer(arrays[alias], alias, owner=self, cursor=cursor))
        self.lines = Lines(LINE_ALIASES, buffers)
        for alias, buf in zip(LINE_ALIASES, buffers):
            setattr(self, alias, buf)
        self.l = self.lines
        self._started = True

    def _advance_to(self, dtnum: float) -> bool:
        """Move to the bar dated ``dtnum`` if the feed has one; return whether it did."""
        nxt = self.lines.datetime.idx + 1
        if nxt < len(self._dtnum) and self._dtnum[nxt] == dtnum:
            self.lines.advance()
            return True
        return False

    def _peek_next_dtnum(self):
        nxt = self.lines.datetime.idx + 1
        return self._dtnum[nxt] if nxt < len(self._dtnum) else None

    def _peek_bar(self, dtnum: float):
        """The upcoming bar's (open, high, low, close, dtnum) if it is dated ``dtnum``.

        The broker reads it to execute the orders of the previous bar *before* the
        strategy sees the new bar -- observed on the oracle: inside ``notify_order`` the
        strategy still reads the previous bar's date and value.
        """
        nxt = self.lines.datetime.idx + 1
        if nxt < len(self._dtnum) and self._dtnum[nxt] == dtnum:
            a = self._arrays
            return (a["open"][nxt], a["high"][nxt], a["low"][nxt], a["close"][nxt], dtnum)
        return None

    # -- public surface ----------------------------------------------------------------------
    def __len__(self):
        return self._cursor.i + 1

    def buflen(self):
        return len(self._dtnum) if self._started else 0

    def getlinealiases(self):
        return LINE_ALIASES

    def num2date(self, num=None):
        from .dateutils import num2date

        return num2date(self.lines.datetime[0] if num is None else num)

    def date2num(self, dt):
        return date2num(dt)

    # -- DataFeed (spec/05 § 6.4) -------------------------------------------------------------
    def load(self):
        """The whole history as a ``FeedBlock`` (int64 ns timestamps, float64 columns)."""
        from .market import feed_block

        if not self._started:
            self._start()
        return feed_block(self._dtnum, self._arrays)

    def is_live(self) -> bool:
        return False

    def poll(self, until):
        return None

    def haslivedata(self):
        return False

    def islive(self):
        return False

    def __repr__(self):
        return f"{type(self).__name__}(name={self._name!r}, bars={self.buflen()})"

    # -- a feed as a number: its close ---------------------------------------------------------
    # Observed on the oracle (probes ``feed_equality``, ``feed_operators``): while the strategy
    # runs, a feed compared or combined with a number, a line or another feed stands for the
    # current value of its close, so two distinct feeds closing at the same price are equal
    # (``d in longs``, ``list.index(d)``, ``list.remove(d)`` follow); against anything else
    # (``None``, a string, an order, a strategy) it is equal only to itself. It still hashes
    # by identity, so dicts and sets keyed by feeds are unchanged. Before the run (a
    # strategy's ``__init__``) a feed compares by identity here; the oracle builds a line
    # there instead, whose truth value raises.
    def _tw_operand(self, other):
        if in_build_stage() or not self._started:
            return NotImplemented
        if isinstance(other, DataBase):
            if not other._started:
                return NotImplemented
            return other.close
        if isinstance(other, (numbers.Real, np.number, LineArithmetic)):
            return other
        return NotImplemented

    def _tw_as_number(self) -> float:
        """The current close: what a line operator reads when its other operand is a feed."""
        return self.close[0]

    def _tw_cmp(self, other, op):
        value = self._tw_operand(other)
        if value is NotImplemented:
            return NotImplemented
        return getattr(self.close, op)(value)

    def __eq__(self, other):
        return self._tw_cmp(other, "__eq__")

    def __ne__(self, other):
        return self._tw_cmp(other, "__ne__")

    __hash__ = object.__hash__

    def __lt__(self, other):
        return self._tw_cmp(other, "__lt__")

    def __le__(self, other):
        return self._tw_cmp(other, "__le__")

    def __gt__(self, other):
        return self._tw_cmp(other, "__gt__")

    def __ge__(self, other):
        return self._tw_cmp(other, "__ge__")

    def __add__(self, other):
        return self._tw_cmp(other, "__add__")

    def __radd__(self, other):
        return self._tw_cmp(other, "__radd__")

    def __sub__(self, other):
        return self._tw_cmp(other, "__sub__")

    def __rsub__(self, other):
        return self._tw_cmp(other, "__rsub__")

    def __mul__(self, other):
        return self._tw_cmp(other, "__mul__")

    def __rmul__(self, other):
        return self._tw_cmp(other, "__rmul__")

    def __truediv__(self, other):
        return self._tw_cmp(other, "__truediv__")

    def __rtruediv__(self, other):
        return self._tw_cmp(other, "__rtruediv__")


class PandasData(DataBase):
    params = (
        ("nocase", True),
        ("datetime", None),
        ("open", -1),
        ("high", -1),
        ("low", -1),
        ("close", -1),
        ("volume", -1),
        ("openinterest", -1),
    )

    def _column(self, frame: pd.DataFrame, alias: str, names: dict | None = None):
        """The column of ``alias`` as float64, or None. ``names`` caches the frame's
        column lookups across the aliases of one load."""
        spec = getattr(self.p, alias)
        if spec is None:
            return None
        if isinstance(spec, int) and not isinstance(spec, bool):
            if spec >= 0:
                return frame.iloc[:, spec].to_numpy(dtype=np.float64)
            # autodetect by name
            if names is None:
                names = {}
            cols = names.get("cols")
            if cols is None:
                cols = names["cols"] = set(frame.columns)
            if alias in cols:
                return frame[alias].to_numpy(dtype=np.float64)
            if self.p.nocase:
                lowered = names.get("lowered")
                if lowered is None:
                    lowered = names["lowered"] = {str(c).lower(): c for c in frame.columns}
                if alias in lowered:
                    return frame[lowered[alias]].to_numpy(dtype=np.float64)
            return None
        return frame[spec].to_numpy(dtype=np.float64)

    def _load_arrays(self):
        frame = self.p.dataname
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("PandasData needs a pandas DataFrame as dataname")
        if self.p.datetime is None:
            stamps = frame.index
        elif isinstance(self.p.datetime, int):
            stamps = frame.iloc[:, self.p.datetime]
        else:
            stamps = frame[self.p.datetime]
        dtnum = stamps2num(stamps)
        if len(dtnum) > 1 and not np.all(np.diff(dtnum) > 0):
            order = np.argsort(dtnum, kind="stable")
            frame = frame.iloc[order]
            dtnum = dtnum[order]
        n = len(dtnum)
        arrays = {}
        names: dict = {}
        for alias in LINE_ALIASES[1:]:
            col = self._column(frame, alias, names)
            arrays[alias] = np.full(n, np.nan) if col is None else np.ascontiguousarray(col)
        if np.isnan(arrays["close"]).all() and self.p.close is not None:
            raise ValueError("PandasData: no close column found")
        return dtnum, arrays


class feeds:
    PandasData = PandasData
    DataBase = DataBase
