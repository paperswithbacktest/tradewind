"""The built-in indicators the corpus uses, from their definitions."""

from __future__ import annotations

import numpy as np

from ..indicator import (
    Indicator,
    _as_array,
    _ema,
    _mean,
    _rolling_extreme,
    _rolling_mean,
    _window_sum,
)


class SimpleMovingAverage(Indicator):
    lines = ("sma",)
    params = (("period", 30),)

    def __init__(self):
        super().__init__()
        self.addminperiod(self.p.period)

    def once(self, start, end):
        self.lines.sma.array[:] = _rolling_mean(_as_array(self.data), self.p.period)


SMA = MovingAverageSimple = SimpleMovingAverage


class ExponentialMovingAverage(Indicator):
    lines = ("ema",)
    params = (("period", 30), ("alpha", None))

    def __init__(self):
        super().__init__()
        self.addminperiod(self.p.period)

    def once(self, start, end):
        self.lines.ema.array[:] = _ema(_as_array(self.data), self.p.period, self.p.alpha)


EMA = MovingAverageExponential = ExponentialMovingAverage


class Highest(Indicator):
    lines = ("highest",)
    params = (("period", 1),)

    def __init__(self):
        super().__init__()
        self.addminperiod(self.p.period)

    def once(self, start, end):
        self.lines.highest.array[:] = _rolling_extreme(_as_array(self.data), self.p.period, np.max)


MaxN = Highest


class Lowest(Indicator):
    lines = ("lowest",)
    params = (("period", 1),)

    def __init__(self):
        super().__init__()
        self.addminperiod(self.p.period)

    def once(self, start, end):
        self.lines.lowest.array[:] = _rolling_extreme(_as_array(self.data), self.p.period, np.min)


MinN = Lowest


class RateOfChange(Indicator):
    """``(close - close[-period]) / close[-period]``.

    Written as a difference over the old value, not ``close / close[-period] - 1``: the
    two round differently, and only the former is the oracle's bit for bit (0 of 342
    values apart on periods 1, 5 and 12; the ratio form differs on 95 % of them). A last
    bit is enough to reorder two ETFs of equal return in a decile sort (probe
    ``roc_bits``, corpus ``abnormal_returns_with_momentum_contrarian…``)."""

    lines = ("roc",)
    params = (("period", 12),)

    def __init__(self):
        super().__init__()
        self.addminperiod(self.p.period + 1)

    def once(self, start, end):
        x = _as_array(self.data)
        p = self.p.period
        out = np.full(len(x), np.nan)
        if len(x) > p:
            out[p:] = (x[p:] - x[:-p]) / x[:-p]
        self.lines.roc.array[:] = out


ROC = RateOfChange


class TrueRange(Indicator):
    lines = ("tr",)

    def __init__(self):
        super().__init__()
        self.addminperiod(2)

    def once(self, start, end):
        d = self.data
        h, lo, c = d.lines.high.array, d.lines.low.array, d.lines.close.array
        out = np.full(len(c), np.nan)
        prev = c[:-1]
        out[1:] = np.maximum(h[1:] - lo[1:], np.maximum(np.abs(h[1:] - prev), np.abs(lo[1:] - prev)))
        self.lines.tr.array[:] = out


class AverageTrueRange(Indicator):
    """Smoothed true range (Wilder's smoothing, seeded with the simple average)."""

    lines = ("atr",)
    params = (("period", 14),)

    def __init__(self):
        super().__init__()
        self.addminperiod(self.p.period + 1)

    def once(self, start, end):
        d = self.data
        h, lo, c = d.lines.high.array, d.lines.low.array, d.lines.close.array
        tr = np.full(len(c), np.nan)
        prev = c[:-1]
        tr[1:] = np.maximum(h[1:] - lo[1:], np.maximum(np.abs(h[1:] - prev), np.abs(lo[1:] - prev)))
        p = self.p.period
        out = np.full(len(c), np.nan)
        if len(c) > p:
            out[p] = _mean(tr[1:p + 1])
            a = 1.0 / p
            keep = 1.0 - a  # the ``prev * (1 - a) + x * a`` form, as in ``_ema``
            for i in range(p + 1, len(c)):
                out[i] = out[i - 1] * keep + tr[i] * a
        self.lines.atr.array[:] = out


ATR = AverageTrueRange


class RelativeStrengthIndex(Indicator):
    """Wilder's RSI: 100 - 100 / (1 + smoothed up / smoothed down)."""

    lines = ("rsi",)
    params = (("period", 14), ("safediv", False), ("safehigh", 100.0), ("safelow", 50.0))

    def __init__(self):
        super().__init__()
        self.addminperiod(self.p.period + 1)

    def once(self, start, end):
        x = _as_array(self.data)
        p = self.p.period
        n = len(x)
        out = np.full(n, np.nan)
        if n > p:
            d = np.diff(x)
            up = np.where(d > 0, d, 0.0)
            dn = np.where(d < 0, -d, 0.0)
            au = np.full(n - 1, np.nan)
            ad = np.full(n - 1, np.nan)
            au[p - 1] = _mean(up[:p])
            ad[p - 1] = _mean(dn[:p])
            a = 1.0 / p
            keep = 1.0 - a  # the ``prev * (1 - a) + x * a`` form, as in ``_ema``
            for i in range(p, n - 1):
                au[i] = au[i - 1] * keep + up[i] * a
                ad[i] = ad[i - 1] * keep + dn[i] * a
            with np.errstate(divide="ignore", invalid="ignore"):
                rs = au / ad
                rsi = 100.0 - 100.0 / (1.0 + rs)
            if self.p.safediv:
                rsi = np.where(ad == 0, np.where(au == 0, self.p.safelow, self.p.safehigh), rsi)
            else:
                rsi = np.where(ad == 0, 100.0, rsi)
            out[1:] = rsi
        self.lines.rsi.array[:] = out


RSI = RelativeStrengthIndex


class MACD(Indicator):
    lines = ("macd", "signal")
    params = (("period_me1", 12), ("period_me2", 26), ("period_signal", 9))

    def __init__(self):
        super().__init__()
        self.addminperiod(self.p.period_me2 + self.p.period_signal - 1)

    def once(self, start, end):
        x = _as_array(self.data)
        me1 = _ema(x, self.p.period_me1)
        me2 = _ema(x, self.p.period_me2)
        macd = me1 - me2
        first = self.p.period_me2 - 1
        signal = np.full(len(x), np.nan)
        valid = macd[first:]
        if len(valid) >= self.p.period_signal:
            signal[first:] = _ema(valid, self.p.period_signal)
        self.lines.macd.array[:] = macd
        self.lines.signal.array[:] = signal


class CrossOver(Indicator):
    """+1 when the first input crosses above the second, -1 below, 0 otherwise."""

    lines = ("crossover",)

    def __init__(self):
        super().__init__()
        # one bar beyond the slower input (observed: CrossOver of SMA(3) and SMA(5) first
        # delivers at bar 6)
        self.addminperiod(2)

    def once(self, start, end):
        a = _as_array(self.datas[0])
        b = _as_array(self.datas[1]) if len(self.datas) > 1 else np.zeros_like(a)
        if np.ndim(b) == 0:
            b = np.full(len(a), float(b))
        diff = a - b
        out = np.zeros(len(a))
        up = (diff[1:] > 0) & (diff[:-1] <= 0)
        dn = (diff[1:] < 0) & (diff[:-1] >= 0)
        out[1:][up] = 1.0
        out[1:][dn] = -1.0
        out[np.isnan(diff)] = np.nan
        out[1:][np.isnan(diff[:-1])] = np.nan
        # the first line, whatever a subclass names it (CrossUp reads it back)
        self.lines[0].array[:] = out


class CrossUp(CrossOver):
    lines = ("crossup",)

    def once(self, start, end):
        super().once(start, end)
        arr = self.lines[0].array
        self.lines[0].array[:] = np.where(arr > 0, 1.0, np.where(np.isnan(arr), np.nan, 0.0))


class CrossDown(CrossOver):
    lines = ("crossdown",)

    def once(self, start, end):
        a = _as_array(self.datas[0])
        b = _as_array(self.datas[1]) if len(self.datas) > 1 else np.zeros_like(a)
        diff = a - b
        out = np.zeros(len(a))
        out[1:][(diff[1:] < 0) & (diff[:-1] >= 0)] = 1.0
        out[np.isnan(diff)] = np.nan
        self.lines[0].array[:] = out


class StandardDeviation(Indicator):
    """``sqrt(mean(x**2) - mean(x)**2)`` over the window, both means the simple average.

    The public documentation gives this definition (mean of the squares minus the squared
    mean); with the window sums compensated as the simple average's are (``_window_sum``),
    it is the oracle's bit for bit (probe ``indicator_bits``: 0 of 111 values apart, where
    a two-pass ``numpy.std`` differs on all of them). ``safepow`` takes the absolute value
    first, against a tiny negative difference."""

    lines = ("stddev",)
    params = (("period", 20), ("movav", None), ("safepow", True))

    def __init__(self):
        super().__init__()
        self.addminperiod(self.p.period)

    def once(self, start, end):
        x = _as_array(self.data)
        p = self.p.period
        out = np.full(len(x), np.nan)
        if len(x) >= p:
            meansq = _window_sum(x * x, p) / p
            mean = _window_sum(x, p) / p
            diff = meansq - mean * mean
            with np.errstate(invalid="ignore"):
                out[p - 1:] = np.sqrt(np.abs(diff) if self.p.safepow else diff)
        self.lines.stddev.array[:] = out


StdDev = StandardDeviation

__all__ = [
    "ATR",
    "EMA",
    "MACD",
    "ROC",
    "RSI",
    "SMA",
    "AverageTrueRange",
    "CrossDown",
    "CrossOver",
    "CrossUp",
    "ExponentialMovingAverage",
    "Highest",
    "Lowest",
    "MaxN",
    "MinN",
    "MovingAverageExponential",
    "MovingAverageSimple",
    "RateOfChange",
    "RelativeStrengthIndex",
    "SimpleMovingAverage",
    "StandardDeviation",
    "StdDev",
    "TrueRange",
]
