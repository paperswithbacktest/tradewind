"""Lines: a numpy array behind a cursor, read with backtrader's ``ago`` indexing.

``line[0]`` is the bar being processed, ``line[-1]`` the one before, and so on.
``len(line)`` is how many bars have been processed. The array is the whole series of the
feed, known at start; the cursor is what the clock moves. A strategy never sees the array.
"""

from __future__ import annotations

import operator

import numpy as np

from .dateutils import num2date

# -- the two stages of a strategy ------------------------------------------------------------
#
# The public documentation of the compatible API describes two stages: while a strategy
# (and the indicators it builds) is constructed, an operator applied to lines builds a new
# *line* -- ``self.data.close - self.data.open`` is a series, ``bt.If(cond, a, b)`` a
# conditional series --; once the run steps the strategy, the same operator reads the
# current values and returns a plain number or bool. Observed on the oracle: a comparison
# built in ``__init__`` has no truth value (``bool()`` raises ``TypeError``), the same
# comparison in ``next()`` is a ``bool``.

_BUILDING = [0]


class building:
    """Context manager: the build stage, entered by the engine around a strategy's construction."""

    def __enter__(self):
        _BUILDING[0] += 1
        return self

    def __exit__(self, *exc):
        _BUILDING[0] -= 1
        return False


def in_build_stage() -> bool:
    return _BUILDING[0] > 0


def _lazy(fn, *operands):
    from .lineops import operate  # lineops builds on indicator, which builds on this module

    return operate(fn, *operands)


class LineArithmetic:
    """Operators of a line-like object (a line, an indicator): a line in the build stage,
    a number on the current value (``_tw_v()``) otherwise."""

    __slots__ = ()

    def _tw_v(self):
        raise NotImplementedError

    def _tw_bin(self, other, fn):
        if _BUILDING[0]:
            return _lazy(fn, self, other)
        return fn(self._tw_v(), float(other))

    def _tw_rbin(self, other, fn):
        if _BUILDING[0]:
            return _lazy(fn, other, self)
        return fn(float(other), self._tw_v())

    def __lt__(self, other):
        return self._tw_bin(other, operator.lt)

    def __le__(self, other):
        return self._tw_bin(other, operator.le)

    def __gt__(self, other):
        return self._tw_bin(other, operator.gt)

    def __ge__(self, other):
        return self._tw_bin(other, operator.ge)

    def __add__(self, other):
        return self._tw_bin(other, operator.add)

    def __radd__(self, other):
        return self._tw_rbin(other, operator.add)

    def __sub__(self, other):
        return self._tw_bin(other, operator.sub)

    def __rsub__(self, other):
        return self._tw_rbin(other, operator.sub)

    def __mul__(self, other):
        return self._tw_bin(other, operator.mul)

    def __rmul__(self, other):
        return self._tw_rbin(other, operator.mul)

    def __truediv__(self, other):
        return self._tw_bin(other, operator.truediv)

    def __rtruediv__(self, other):
        return self._tw_rbin(other, operator.truediv)

    def __floordiv__(self, other):
        return self._tw_bin(other, operator.floordiv)

    def __rfloordiv__(self, other):
        return self._tw_rbin(other, operator.floordiv)

    def __mod__(self, other):
        return self._tw_bin(other, operator.mod)

    def __rmod__(self, other):
        return self._tw_rbin(other, operator.mod)

    def __pow__(self, other):
        return self._tw_bin(other, operator.pow)

    def __rpow__(self, other):
        return self._tw_rbin(other, operator.pow)

    def __neg__(self):
        if _BUILDING[0]:
            return _lazy(operator.neg, self)
        return -self._tw_v()

    def __pos__(self):
        if _BUILDING[0]:
            return _lazy(operator.pos, self)
        return +self._tw_v()

    def __abs__(self):
        if _BUILDING[0]:
            return _lazy(operator.abs, self)
        return abs(self._tw_v())


class LineBuffer(LineArithmetic):
    __slots__ = ("_name", "_owner", "array", "idx")

    def __init__(self, array, name: str = "", owner=None):
        self.array = np.ascontiguousarray(array, dtype=np.float64)
        self.idx = -1
        self._name = name
        self._owner = owner  # the feed or indicator this line belongs to

    # -- cursor ---------------------------------------------------------------
    def __len__(self) -> int:
        return self.idx + 1

    def buflen(self) -> int:
        return len(self.array)

    def advance(self, n: int = 1) -> None:
        self.idx += n

    def home(self) -> None:
        self.idx = -1

    # -- reads -----------------------------------------------------------------
    def __getitem__(self, ago: int) -> float:
        # Plain sequence indexing on the preloaded buffer, as the oracle does: a positive
        # ``ago`` reads a bar that has not arrived yet (a documented look-ahead the corpus
        # relies on, e.g. ``datetime.date(1)`` to spot a month's last session), and an
        # ``ago`` reaching before the first bar wraps around to the end of the buffer.
        # Only a position outside the buffer raises.
        try:
            return float(self.array[self.idx + ago])
        except IndexError:
            raise IndexError(f"line {self._name!r}: ago={ago} at len={len(self)} of {self.buflen()}") from None

    def __call__(self, ago: int = 0) -> float:
        return self[ago]

    def get(self, ago: int = 0, size: int = 1) -> list:
        """The ``size`` values ending at ``ago`` (inclusive), oldest first."""
        end = self.idx + ago + 1
        start = end - size
        if start < 0 or end > self.idx + 1:
            return []
        return self.array[start:end].tolist()

    def __iter__(self):
        return iter(self.array[: self.idx + 1].tolist())

    # -- writes (indicators) ----------------------------------------------------
    def __setitem__(self, ago: int, value: float) -> None:
        self.array[self.idx + ago] = value

    def __bool__(self) -> bool:
        return True

    def __float__(self) -> float:
        return self[0]

    def __eq__(self, other):
        if _BUILDING[0]:
            return _lazy(operator.eq, self, other)
        return self._tw_v() == float(other)

    def __ne__(self, other):
        if _BUILDING[0]:
            return _lazy(operator.ne, self, other)
        return self._tw_v() != float(other)

    def __hash__(self):
        return id(self)

    # arithmetic: on the current value in ``next()`` (``data.close > data.open`` is a
    # bool), on the whole line while a strategy is being built (see ``LineArithmetic``)
    def _tw_v(self):
        return self[0]


class DatetimeLine(LineBuffer):
    """The ``datetime`` line: float days, plus the ``date()``/``datetime()`` readers."""

    __slots__ = ()

    def datetime(self, ago: int = 0, tz=None, naive: bool = True):
        return num2date(self[ago], tz=tz)

    def date(self, ago: int = 0, tz=None, naive: bool = True):
        return num2date(self[ago], tz=tz).date()

    def time(self, ago: int = 0, tz=None, naive: bool = True):
        return num2date(self[ago], tz=tz).time()

    def dt(self, ago: int = 0):
        return self.date(ago)

    def tm(self, ago: int = 0):
        return self.time(ago)


class Lines:
    """The named lines of a feed or an indicator, by alias or by position."""

    def __init__(self, names, buffers):
        self._names = tuple(names)
        self._buffers = tuple(buffers)
        for name, buf in zip(self._names, self._buffers):
            setattr(self, name, buf)

    def __len__(self):
        return len(self._buffers[0])

    def __getitem__(self, i):
        return self._buffers[i]

    def __iter__(self):
        return iter(self._buffers)

    def size(self):
        return len(self._buffers)

    def getlinealiases(self):
        return self._names

    def advance(self, n: int = 1):
        for buf in self._buffers:
            buf.advance(n)

    def home(self):
        for buf in self._buffers:
            buf.home()
