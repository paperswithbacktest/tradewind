"""Lines built from other lines in a strategy's ``__init__``, and the line class markers.

The compatible API documents two stages. While a strategy is built, an operator applied
to lines (``highest - self.data.low``, ``close > 0.0``) builds a *line*, and ``If(cond,
a, b)`` a conditional line; while it runs, the same operators read current values. A line
built this way is an indicator of its own: it has a minimum period (the largest of its
operands'), it is computed in one vectorised pass before the run, it can feed another
indicator (``SMA(If(...), period=20)``), and it holds the strategy in ``prenext`` until
it delivers. Everything here is fixed from the public documentation and from black-box
probes on the oracle (``tests/oracle/strategies.py``, probe ``line_ops``):

* the operands' minimum periods combine by maximum, and a line built in ``__init__``
  counts towards the strategy's own (``If`` over ``Highest(20)``: ``next`` from bar 20;
  ``SMA(20)`` of it: bar 39);
* before its minimum period a built line holds NaN -- even ``If(nan > 0, x, 0.0)``, whose
  condition would be false, is NaN there, not 0.0;
* a comparison yields 1.0 or 0.0; ``If`` takes its first operand as true when it is
  non-zero (NaN included, as in Python);
* a division by zero raises ``ZeroDivisionError`` when the line is computed, as Python
  float arithmetic does;
* a built line has no truth value: ``bool()`` raises ``TypeError``.

``LineRoot`` and ``LineIterator`` are the names of two classes of the line hierarchy the
corpus reads in type annotations (``Dict[bt.LineRoot, ...]``, ``List[bt.LineIterator]``).
Tradewind has no such hierarchy; they are markers whose ``isinstance`` answers what the
oracle answers: a feed, a line, a built line, an indicator and a strategy are ``LineRoot``;
only an indicator and a strategy are ``LineIterator``; an analyzer and a feed's ``lines``
container are neither.
"""

from __future__ import annotations

import operator

import numpy as np

from .indicator import Indicator, _as_array, _minperiod_of
from .lines import LineBuffer

_DIVISIONS = {operator.truediv, operator.floordiv, operator.mod}

_UFUNCS = {
    operator.lt: np.less,
    operator.le: np.less_equal,
    operator.gt: np.greater,
    operator.ge: np.greater_equal,
    operator.eq: np.equal,
    operator.ne: np.not_equal,
    operator.add: np.add,
    operator.sub: np.subtract,
    operator.mul: np.multiply,
    operator.truediv: np.true_divide,
    operator.floordiv: np.floor_divide,
    operator.mod: np.mod,
    operator.pow: np.power,
    operator.neg: np.negative,
    operator.pos: np.positive,
    operator.abs: np.abs,
}


def _is_line(obj) -> bool:
    if isinstance(obj, (LineBuffer, Indicator)):
        return True
    return hasattr(obj, "lines") and hasattr(obj, "_dtnum")  # a feed: its close


def _if(cond, a, b):
    # Python truthiness of a float: non-zero, NaN included
    return np.where(cond != 0, a, b)


class LinesOperation(Indicator):
    """One line computed element-wise from line operands and scalars."""

    lines = ("op",)

    def __init__(self, _fn=None, _operands=()):
        self._fn = _fn
        self._operands = tuple(_operands)

    def __bool__(self):
        raise TypeError(
            "a line built in __init__ has no truth value; compare its current value in next() "
            "(e.g. ``self.cond[0]``)"
        )

    def once(self, start, end):
        n = self.lines[0].buflen()
        args = []
        for o in self._operands:
            if _is_line(o):
                arr = _as_array(o)
                if len(arr) != n:
                    raise NotImplementedError(
                        "a line operation across feeds of different lengths is not supported"
                    )
                args.append(arr)
            else:
                args.append(float(o))
        first = self._minperiod - 1
        if self._fn in _DIVISIONS:
            divisor = args[1]
            tail = divisor[first:] if isinstance(divisor, np.ndarray) else divisor
            if np.any(np.asarray(tail) == 0.0):
                raise ZeroDivisionError("float division by zero")
        fn = _UFUNCS.get(self._fn, self._fn)
        with np.errstate(all="ignore"):
            out = fn(*args)
        out = np.broadcast_to(np.asarray(out, dtype=np.float64), (n,)).copy()
        out[:first] = np.nan
        self.lines[0].array[:] = out


def operate(fn, *operands):
    """A ``LinesOperation`` of ``fn`` over ``operands`` (lines and scalars)."""
    inputs = [o for o in operands if _is_line(o)]
    if not inputs:
        raise TypeError("a line operation needs at least one line operand")
    op = LinesOperation(*inputs, _fn=fn, _operands=operands)
    # the inputs set the minimum period (``_consume_args``); a built line adds nothing
    op._minperiod = max(_minperiod_of(i) for i in inputs)
    op._push_minperiod()
    return op


def If(cond, a, b):
    """``a`` where ``cond`` is true, else ``b``: a line, built in a strategy's ``__init__``."""
    return operate(_if, cond, a, b)


# -- class markers ---------------------------------------------------------------------------


class _MarkerMeta(type):
    def __instancecheck__(cls, obj):
        return cls.__subclasscheck__(type(obj))

    def __subclasscheck__(cls, sub):
        if type.__subclasscheck__(cls, sub):
            return True
        return cls._matches(sub)


class LineRoot(metaclass=_MarkerMeta):
    """Marker: anything made of lines -- a feed, a line, a built line, an indicator, a strategy."""

    @classmethod
    def _matches(cls, sub) -> bool:
        from .feeds import DataBase
        from .strategy import Strategy

        return issubclass(sub, (LineBuffer, Indicator, Strategy, DataBase))


class LineIterator(LineRoot):
    """Marker: what the run steps bar by bar -- an indicator or a strategy, not a built line."""

    @classmethod
    def _matches(cls, sub) -> bool:
        from .strategy import Strategy

        if issubclass(sub, LinesOperation):
            return False
        return issubclass(sub, (Indicator, Strategy))
