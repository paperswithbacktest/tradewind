"""Lines: a numpy array behind a cursor, read with backtrader's ``ago`` indexing.

``line[0]`` is the bar being processed, ``line[-1]`` the one before, and so on.
``len(line)`` is how many bars have been processed. The array is the whole series of the
feed, known at start; the cursor is what the clock moves. A strategy never sees the array.
"""

from __future__ import annotations

import numpy as np

from .dateutils import num2date


class LineBuffer:
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

    # arithmetic on the current value, so ``data.close > data.open`` reads naturally
    def _v(self):
        return self[0]

    def __lt__(self, other):
        return self._v() < float(other)

    def __le__(self, other):
        return self._v() <= float(other)

    def __gt__(self, other):
        return self._v() > float(other)

    def __ge__(self, other):
        return self._v() >= float(other)

    def __eq__(self, other):
        return self._v() == float(other)

    def __hash__(self):
        return id(self)

    def __add__(self, other):
        return self._v() + float(other)

    def __radd__(self, other):
        return float(other) + self._v()

    def __sub__(self, other):
        return self._v() - float(other)

    def __rsub__(self, other):
        return float(other) - self._v()

    def __mul__(self, other):
        return self._v() * float(other)

    def __rmul__(self, other):
        return float(other) * self._v()

    def __truediv__(self, other):
        return self._v() / float(other)

    def __rtruediv__(self, other):
        return float(other) / self._v()

    def __neg__(self):
        return -self._v()

    def __abs__(self):
        return abs(self._v())


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
