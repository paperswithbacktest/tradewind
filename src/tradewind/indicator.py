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

from typing import ClassVar

import numpy as np

from .lines import LineBuffer, Lines
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


def _owner_line(source) -> LineBuffer:
    if isinstance(source, LineBuffer):
        return source
    if hasattr(source, "lines") and hasattr(source, "_dtnum"):
        return source.lines.close
    if isinstance(source, Indicator):
        return source.lines[0]
    raise TypeError(f"cannot bind to {type(source).__name__}")


class Indicator(ParamsBase):
    lines: tuple = ()
    plotinfo: ClassVar[dict] = {}
    plotlines: ClassVar[dict] = {}

    def _before_init(self):
        self._minperiod = 1
        self._computed = False
        self._clock = None
        self._strategy = None

    def _consume_args(self, *inputs):
        """Bind the input lines before the user's ``__init__`` runs; nothing is passed on,
        so a corpus indicator written as ``def __init__(self)`` works."""
        self.datas = list(inputs)
        self.data = self.datas[0] if self.datas else None
        for i, d in enumerate(self.datas):
            setattr(self, f"data{i}", d)
        self._clock = _owner_line(self.data) if self.data is not None else None
        n = self._clock.buflen() if self._clock is not None else 0
        names = tuple(self.lines) if not isinstance(self.lines, str) else (self.lines,)
        buffers = [LineBuffer(np.full(n, np.nan), name, owner=self) for name in names]
        self.lines = Lines(names, buffers)
        self.l = self.lines
        for name, buf in zip(names, buffers):
            setattr(self, name, buf)
        self._register()
        return ()

    def __init__(self):
        pass

    def _register(self):
        """Attach to the running strategy so the engine syncs the cursor and reads the
        min period. Found through the feed's environment."""
        source = self.data
        if isinstance(source, LineBuffer):
            source = source._owner
        env = getattr(source, "_env", None)
        self._env = env
        strat = getattr(env, "_building", None) if env is not None else None
        if strat is not None:
            strat._indicators.append(self)
            self._strategy = strat
            strat._minperiod = max(strat._minperiod, self._minperiod)

    def addminperiod(self, n: int):
        self._minperiod = max(self._minperiod, int(n))
        if self._strategy is not None:
            self._strategy._minperiod = max(self._strategy._minperiod, self._minperiod)

    def updateminperiod(self, n: int):
        self.addminperiod(n)

    def __len__(self):
        return len(self.lines[0])

    def __getitem__(self, ago):
        return self.lines[0][ago]

    def __call__(self, ago=0):
        return self.lines[0][ago]

    def __float__(self):
        return self.lines[0][0]

    def __lt__(self, other):
        return self.lines[0][0] < float(other)

    def __gt__(self, other):
        return self.lines[0][0] > float(other)

    def __le__(self, other):
        return self.lines[0][0] <= float(other)

    def __ge__(self, other):
        return self.lines[0][0] >= float(other)

    def __sub__(self, other):
        return self.lines[0][0] - float(other)

    def __rsub__(self, other):
        return float(other) - self.lines[0][0]

    def __add__(self, other):
        return self.lines[0][0] + float(other)

    def __radd__(self, other):
        return float(other) + self.lines[0][0]

    def __mul__(self, other):
        return self.lines[0][0] * float(other)

    def __truediv__(self, other):
        return self.lines[0][0] / float(other)

    # -- computation ------------------------------------------------------------------
    def _input_lines(self):
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
        inputs = self._input_lines()
        saved = [buf.idx for buf in inputs]
        try:
            for i in range(start, end):
                for buf in self.lines:
                    buf.idx = i
                for buf in inputs:
                    buf.idx = i
                if i + 1 < self._minperiod:
                    continue
                self.next()
        finally:
            for buf, idx in zip(inputs, saved):
                buf.idx = idx

    def next(self):
        raise NotImplementedError(f"{type(self).__name__} defines neither once() nor next()")

    @property
    def _vectorised(self) -> bool:
        """Does the class provide its own ``once``? Then it is computed in one pass at
        start. Otherwise it is stepped bar by bar with the strategy, like the oracle
        runs a ``next``-only indicator, so its ``next()`` may read any feed of the run
        (a corpus indicator reads a VIX feed it was not given as input)."""
        return type(self).once is not Indicator.once

    def _runonce(self) -> bool:
        env = self._env
        return bool(getattr(getattr(env, "p", None), "runonce", True)) if env is not None else True

    def _compute(self):
        if self._computed:
            return
        for inp in self.datas:
            if isinstance(inp, Indicator):
                inp._compute()
        n = self._clock.buflen()
        if self._vectorised:
            self.once(0, n)
            self.lines.home()
        elif self._runonce():
            # Observed on the oracle (runonce, its default): a next-only indicator is
            # computed in one pre-pass before the run, during which every feed that is
            # not one of its inputs sits at its LAST bar. An indicator reading such a
            # feed through a parameter therefore sees one frozen value for the whole
            # series (a corpus VIX-gated allocation reads VIX = 15.86 on every bar).
            # Reproduced here because the reference NAVs embed it; ``runonce=False``
            # steps the indicator live instead.
            foreign = []
            inputs = {id(b) for b in self._input_lines()}
            for d in getattr(self._env, "datas", []):
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
        self._computed = True
        self._stepped = -1

    def _sync(self):
        """Align the cursor with the clock feed's cursor; step a live next-only indicator."""
        if not self._computed:
            self._compute()
        idx = self._clock.idx
        for buf in self.lines:
            buf.idx = idx
        if not self._vectorised and not self._runonce() and idx > self._stepped:
            self._stepped = idx
            if idx + 1 >= self._minperiod:
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
