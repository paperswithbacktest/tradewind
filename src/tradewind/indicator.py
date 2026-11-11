"""Indicators: lines computed from a feed's lines (or from other indicators).

The corpus declares 33 ``Indicator`` subclasses and uses nine built-ins. An indicator
owns one ``LineBuffer`` per declared line, computed over the whole input at ``start``
by ``once(start, end)`` -- the vectorised path -- or bar by bar by ``next()`` when a
subclass only provides that. Both are reproducible bar by bar: ``once`` may only use
inputs up to and including the bar it fills. The cursor of every line follows the
clock feed's cursor, so ``ind[0]`` and ``ind.lines.x[-k]`` read like a feed's lines.

``minperiod`` is the number of bars an indicator needs before its first valid value;
the strategy's own min period is the largest among its indicators, and ``next`` is not
called before it is reached (``prenext`` is).
"""

from __future__ import annotations

import operator
from typing import ClassVar

import numpy as np

from .lines import LineArithmetic, LineBuffer, Lines, _lazy, in_build_stage
from .params import ParamsBase


def _as_array(source) -> np.ndarray:
    """The full series behind a line, a feed (its close) or an indicator (first line)."""
    if isinstance(source, LineBuffer):
        return source.array
    if hasattr(source, "lines") and hasattr(source, "_dtnum"):  # a feed
        return source.lines.close.array
    if isinstance(source, Indicator):
        return source.lines[0].array
    if hasattr(source, "array"):
        return source.array
    raise TypeError(f"cannot read a line from {type(source).__name__}")


def _minperiod_of(source) -> int:
    """The minimum period an input imposes: an indicator's own, a feed's 1."""
    owner = source._owner if isinstance(source, LineBuffer) else source
    if isinstance(owner, Indicator):
        return owner._tw_minperiod
    return 1


def _owner_line(source) -> LineBuffer:
    if isinstance(source, LineBuffer):
        return source
    if hasattr(source, "lines") and hasattr(source, "_dtnum"):
        return source.lines.close
    if isinstance(source, Indicator):
        return source.lines[0]
    raise TypeError(f"cannot bind to {type(source).__name__}")


def _bindable(obj) -> bool:
    """A line, an indicator (its first line) or a feed (its close): what a declared line
    of an indicator can be bound to."""
    return isinstance(obj, (LineBuffer, Indicator)) or (hasattr(obj, "lines") and hasattr(obj, "_dtnum"))


class _IndicatorLines(Lines):
    """An indicator's lines. Assigning a line to a declared line **binds** it, as the
    public documentation of indicator development shows (``self.lines.sma = SMA(...)``
    in ``__init__``): the declared line then carries the bound line's values, and the
    indicator needs neither ``once()`` nor ``next()``. Observed on the oracle (probe
    ``line_binding``): the values are the bound line's bit for bit, NaN before its min
    period, and the indicator's min period becomes the bound line's (an indicator bound
    to ``RateOfChange(period=5)`` first delivers at bar 6; an SMA(3) of it at bar 8)."""

    def __init__(self, names, buffers, owner):
        object.__setattr__(self, "_tw_owner", owner)
        object.__setattr__(self, "_tw_open", False)
        super().__init__(names, buffers)
        object.__setattr__(self, "_tw_open", True)

    def __setattr__(self, name, value):
        if self._tw_open and name in self._names and _bindable(value):
            self._tw_owner._tw_bind_line(self._names.index(name), value)
            return
        object.__setattr__(self, name, value)

    def __setitem__(self, i, value):
        if not _bindable(value):
            raise TypeError(f"cannot bind {type(value).__name__} to line {i}")
        self._tw_owner._tw_bind_line(range(len(self._names))[i], value)


# The indicators whose ``__init__`` is running, innermost last: an indicator built while
# another one's ``__init__`` runs is that one's *child* (``self.rsi = RSI(...)`` inside a
# corpus indicator). Cleared by Cerebro around a strategy's construction.
_CONSTRUCTING: list = []


def reset_construction() -> None:
    _CONSTRUCTING.clear()


class Indicator(LineArithmetic, ParamsBase):
    lines: tuple = ()
    plotinfo: ClassVar[dict] = {}
    plotlines: ClassVar[dict] = {}

    def _tw_before_init(self):
        self._tw_minperiod = 1
        self._tw_computed = False
        self._tw_clock = None
        self._tw_strategy = None
        self._tw_bindings = []
        self._tw_parent = None
        self._tw_children = []

    def _tw_consume_args(self, *inputs):
        """Bind the input lines before the user's ``__init__`` runs; nothing is passed on,
        so a corpus indicator written as ``def __init__(self)`` works."""
        self.datas = list(inputs)
        self.data = self.datas[0] if self.datas else None
        for i, d in enumerate(self.datas):
            setattr(self, f"data{i}", d)
        self._tw_clock = _owner_line(self.data) if self.data is not None else None
        # the clock is the feed's line at the root of the inputs: an indicator built on
        # another's line follows the feed, not that line's cursor (a child's stays put)
        clock_owner = getattr(self._tw_clock, "_owner", None)
        if isinstance(clock_owner, Indicator) and clock_owner._tw_clock is not None:
            self._tw_clock = clock_owner._tw_clock
        # Observed on the oracle: an indicator starts from the largest minimum period of its
        # inputs (an indicator, or a line of one; a feed counts 1) -- SMA(5) of SMA(3) first
        # delivers at bar 7, a line operation on Highest(20) at bar 20.
        self._tw_minperiod = max([1] + [_minperiod_of(d) for d in self.datas])
        n = self._tw_clock.buflen() if self._tw_clock is not None else 0
        names = tuple(self.lines) if not isinstance(self.lines, str) else (self.lines,)
        buffers = [LineBuffer(np.full(n, np.nan), name, owner=self) for name in names]
        self.lines = _IndicatorLines(names, buffers, self)
        self.l = self.lines
        for name, buf in zip(names, buffers):
            setattr(self, name, buf)
        self._tw_register()
        if _CONSTRUCTING:
            self._tw_parent = _CONSTRUCTING[-1]
            self._tw_parent._tw_children.append(self)
        _CONSTRUCTING.append(self)
        return ()

    def _tw_after_init(self):
        """Observed on the oracle (probe ``child_indicators``): the indicators an indicator
        builds in its ``__init__`` raise its minimum period to theirs by **maximum**, after
        any ``addminperiod`` of its own (a child SMA(20) and ``addminperiod(3)``, in either
        order: first ``next`` at bar 20; a child SMA(5) and ``addminperiod(30)``: bar 30) --
        unlike an input, whose period ``addminperiod`` adds to."""
        if _CONSTRUCTING and _CONSTRUCTING[-1] is self:
            _CONSTRUCTING.pop()
        if self._tw_children:
            self._tw_minperiod = max([self._tw_minperiod] + [c._tw_minperiod for c in self._tw_children])
            self._tw_push_minperiod()

    def __init__(self):
        pass

    def _tw_register(self):
        """Attach to the running strategy so the engine syncs the cursor and reads the
        min period. Found through the feed's environment."""
        source = self.data
        if isinstance(source, LineBuffer):
            source = source._owner
        env = getattr(source, "_tw_env", None)
        self._tw_env = env
        strat = getattr(env, "_building", None) if env is not None else None
        if strat is not None:
            strat._tw_indicators.append(self)
            self._tw_strategy = strat
            strat._tw_minperiod = max(strat._tw_minperiod, self._tw_minperiod)

    def _tw_bind_line(self, i: int, source):
        """``self.lines.<name> = source`` in ``__init__`` (see ``_IndicatorLines``)."""
        self._tw_bindings.append((i, source))
        self._tw_minperiod = max(self._tw_minperiod, _minperiod_of(source))
        self._tw_push_minperiod()

    def addminperiod(self, n: int):
        """Extend the minimum period by ``n - 1`` bars. Observed on the oracle: periods add
        up (``addminperiod(5)`` then ``addminperiod(3)`` on a feed: first value at bar 7;
        on an SMA(4) input: bar 10)."""
        self._tw_minperiod += int(n) - 1
        self._tw_push_minperiod()

    def updateminperiod(self, n: int):
        """Raise the minimum period to ``n`` if it is lower (observed: ``addminperiod(5)``
        then ``updateminperiod(3)`` keeps 5)."""
        self._tw_minperiod = max(self._tw_minperiod, int(n))
        self._tw_push_minperiod()

    def _tw_push_minperiod(self):
        if self._tw_strategy is not None:
            self._tw_strategy._tw_minperiod = max(self._tw_strategy._tw_minperiod, self._tw_minperiod)

    def __len__(self):
        return len(self.lines[0])

    def __getitem__(self, ago):
        return self.lines[0][ago]

    def __call__(self, ago=0):
        return self.lines[0][ago]

    def __float__(self):
        return self.lines[0][0]

    # operators come from ``LineArithmetic``: a new line in the build stage, a number on
    # the current value during the run
    def _tw_v(self):
        return self.lines[0][0]

    def __eq__(self, other):
        if in_build_stage():
            return _lazy(operator.eq, self, other)
        if isinstance(other, (int, float, np.floating, np.integer)) and not isinstance(other, bool):
            return self._tw_v() == float(other)
        return self is other  # identity otherwise: ``ind in some_list`` must not compare values

    def __ne__(self, other):
        if in_build_stage():
            return _lazy(operator.ne, self, other)
        eq = self.__eq__(other)
        return not eq

    def __hash__(self):
        return id(self)

    # -- computation ------------------------------------------------------------------
    def _tw_input_lines(self):
        """Every LineBuffer of every input (a feed's lines, an indicator's lines)."""
        out = []
        for inp in self.datas:
            owner = inp._owner if isinstance(inp, LineBuffer) else inp
            lines = getattr(owner, "lines", None)
            if lines is not None:
                out.extend(list(lines))
            elif isinstance(inp, LineBuffer):
                out.append(inp)
        return out

    def once(self, start: int, end: int):
        """Fill ``[start, end)`` of every line from the inputs; default: bar by bar.

        The inputs' cursors are moved along so that ``self.data.close[0]``,
        ``self.data.high[-1]`` or ``self.data.datetime.date(0)`` inside ``next()`` read
        the bar being computed, then restored.
        """
        inputs = self._tw_input_lines()
        saved = [buf.idx for buf in inputs]
        # The children (indicators built in ``__init__``) walk with the indicator, so
        # ``self.rsi[-k]`` in ``next()`` reads the bar being computed; observed on the
        # oracle, they are then left on the last bar (see ``_tw_sync``).
        # (only those on a clock as long as this one's: a child on another feed is not walked)
        n_own = self.lines[0].buflen() if len(self.lines._buffers) else 0
        kids = [buf for child in self._tw_children for buf in child.lines if buf.buflen() == n_own]
        try:
            for i in range(start, end):
                for buf in self.lines:
                    buf.idx = i
                for buf in inputs:
                    buf.idx = i
                for buf in kids:
                    buf.idx = i
                if i + 1 < self._tw_minperiod:
                    continue
                self.next()
        finally:
            for buf, idx in zip(inputs, saved):
                buf.idx = idx

    def next(self):
        """Nothing by default. Observed on the oracle (probe ``inert_indicator``): an
        indicator that defines neither ``once`` nor ``next`` and binds no line runs
        without error, its lines NaN on every bar (the corpus uses one as a holder of
        feeds and of a ``compute()`` method the strategy calls)."""

    @property
    def _tw_vectorised(self) -> bool:
        """Does the class provide its own ``once``? Then it is computed in one pass at
        start. Otherwise it is stepped bar by bar with the strategy, like the oracle
        runs a ``next``-only indicator, so its ``next()`` may read any feed of the run
        (a corpus indicator reads a VIX feed it was not given as input)."""
        return type(self).once is not Indicator.once

    @property
    def _tw_steps(self) -> bool:
        """Does the class compute anything bar by bar? An indicator whose lines are all
        bound in ``__init__`` defines neither ``once`` nor ``next`` and has nothing to step."""
        return not self._tw_vectorised and type(self).next is not Indicator.next

    def _tw_runonce(self) -> bool:
        env = self._tw_env
        return bool(getattr(getattr(env, "p", None), "runonce", True)) if env is not None else True

    def _tw_compute(self):
        if self._tw_computed:
            return
        for inp in self.datas:
            owner = inp._owner if isinstance(inp, LineBuffer) else inp
            if isinstance(owner, Indicator):
                owner._tw_compute()
        # observed on the oracle: a child is complete before its parent computes (a
        # ``once`` reading ``self.child.lines.x.array`` sees every value)
        for child in self._tw_children:
            child._tw_compute()
        n = self._tw_clock.buflen()
        for i, source in self._tw_bindings:
            owner = source._owner if isinstance(source, LineBuffer) else source
            if isinstance(owner, Indicator):
                owner._tw_compute()
            values = _as_array(source)
            if len(values) != n:
                raise NotImplementedError("binding a line of another clock (a feed of another length)")
            self.lines[i].array[:] = values
        if self._tw_vectorised:
            self.once(0, n)
            self.lines.home()
        elif self._tw_steps and self._tw_runonce():
            # Observed on the oracle (runonce, its default): a next-only indicator is
            # computed in one pre-pass before the run, during which every feed that is
            # not one of its inputs sits at its LAST bar. An indicator reading such a
            # feed through a parameter therefore sees one frozen value for the whole
            # series (a corpus VIX-gated allocation reads VIX = 15.86 on every bar).
            # Reproduced here because the reference NAVs embed it; ``runonce=False``
            # steps the indicator live instead.
            foreign = []
            inputs = {id(b) for b in self._tw_input_lines()}
            for d in getattr(self._tw_env, "datas", []):
                for buf in getattr(d, "lines", []):
                    if id(buf) not in inputs:
                        foreign.append((buf, buf.idx))
                        buf.idx = buf.buflen() - 1
            try:
                self.once(0, n)
            finally:
                for buf, idx in foreign:
                    buf.idx = idx
            self.lines.home()
        self._tw_computed = True
        self._tw_stepped = -1

    def _tw_sync(self):
        """Align the cursor with the clock feed's cursor; step a live next-only indicator."""
        if not self._tw_computed:
            self._tw_compute()
        live = not self._tw_runonce()
        if self._tw_parent is not None and not live:
            # Observed on the oracle (runonce, its default; probe ``child_indicators``): the
            # strategy does not move a child's cursor during the run. Read from the
            # strategy, a child of a ``next``-only indicator stays on the last bar (``len``
            # the whole series), one of a ``once`` or binding indicator before the first
            # (``len`` 0) -- both read the series' last value at ``[0]``.
            return
        if live:
            for child in self._tw_children:
                child._tw_sync()
        idx = self._tw_clock.idx
        for buf in self.lines:
            buf.idx = idx
        if self._tw_steps and live and idx > self._tw_stepped:
            self._tw_stepped = idx
            if idx + 1 >= self._tw_minperiod:
                self.next()


# -- helpers for the built-ins ---------------------------------------------------------

def _window_sum(x: np.ndarray, period: int) -> np.ndarray:
    """The sum of every ``period``-long window, compensated the way Python's ``sum`` is.

    Observed on the oracle: its simple average of a window is the window's values
    summed by Python's ``sum`` (Neumaier-compensated since Python 3.12) divided by
    the period -- bit for bit on 14 400 values of periods 3, 20 and 200, and
    *exactly* the constant on a constant series. A cumulative-sum difference is
    not: it carries ~1e-14 of rounding, which is enough to flip ``close > sma`` on
    the flat back-filled prefix every late-listed symbol has. The compensation is
    vectorised across windows; each window is accumulated left to right.
    """
    n = len(x) - period + 1
    total = np.zeros(n)
    comp = np.zeros(n)
    for k in range(period):
        v = x[k:k + n]
        t = total + v
        big = np.abs(total) >= np.abs(v)
        comp += np.where(big, (total - t) + v, (v - t) + total)
        total = t
    return np.where(np.isfinite(comp) & (comp != 0.0), total + comp, total)


def _mean(x: np.ndarray) -> float:
    """Python's ``sum(x) / len(x)``, the oracle's average of a seed window."""
    return float(_window_sum(np.asarray(x, dtype=float), len(x))[0]) / len(x)


def _rolling_mean(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if period <= 0 or len(x) < period:
        return out
    out[period - 1:] = _window_sum(x, period) / period
    return out


def _rolling_extreme(x: np.ndarray, period: int, fn) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if period <= 0 or len(x) < period:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(x, period)
    out[period - 1:] = fn(windows, axis=1)
    return out


def _ema(x: np.ndarray, period: int, alpha: float | None = None) -> np.ndarray:
    """Exponential average seeded with the simple average of the first ``period`` bars."""
    out = np.full(len(x), np.nan)
    if len(x) < period:
        return out
    a = 2.0 / (period + 1.0) if alpha is None else alpha
    out[period - 1] = _mean(x[:period])
    # ``prev * (1 - a) + x * a``, not ``prev + a * (x - prev)``: the two round
    # differently, and only the former matches the oracle bit for bit (0 of 4 801
    # values apart on periods 12 and 50; the latter differs on half of them).
    keep = 1.0 - a
    for i in range(period, len(x)):
        out[i] = out[i - 1] * keep + x[i] * a
    return out
