"""spec/05 § 10, Q4: ``once()`` stays the backtest path of the built-in indicators, guarded
bit for bit against the bar-by-bar computation.

The bar-by-bar value at bar *t* is what a run that only knows bars ``0..t`` computes (a live
run, or a backtest stopped at *t*): the indicator is rebuilt on every prefix of the series
and its value at the last bar compared with the whole-series ``once()`` at *t*. Any
difference -- one ulp included -- means ``once()`` read the future or summed differently.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import tradewind as tw

N_BARS = 70


def _frame(seed=21):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2021-01-04", periods=N_BARS)
    close = 50 * np.exp(np.cumsum(rng.normal(0, 0.02, N_BARS)))
    close[30:36] = close[30]                     # a flat stretch: compensated sums matter there
    openp = close * (1 + rng.normal(0, 0.005, N_BARS))
    high = np.maximum(openp, close) * 1.004
    low = np.minimum(openp, close) * 0.996
    return pd.DataFrame({"open": openp, "high": high, "low": low, "close": close, "volume": 1e5}, index=index)


BUILT_INS = {
    "sma": lambda d: tw.ind.SMA(d.close, period=7),
    "ema": lambda d: tw.ind.EMA(d.close, period=9),
    "highest": lambda d: tw.ind.Highest(d.high, period=5),
    "lowest": lambda d: tw.ind.Lowest(d.low, period=5),
    "roc": lambda d: tw.ind.ROC(d.close, period=4),
    "tr": lambda d: tw.ind.TrueRange(d),
    "atr": lambda d: tw.ind.ATR(d, period=6),
    "rsi": lambda d: tw.ind.RSI(d.close, period=6),
    "macd": lambda d: tw.ind.MACD(d.close, period_me1=5, period_me2=11, period_signal=4),
    "crossover": lambda d: tw.ind.CrossOver(tw.ind.SMA(d.close, period=3), tw.ind.SMA(d.close, period=8)),
    "crossup": lambda d: tw.ind.CrossUp(tw.ind.SMA(d.close, period=3), tw.ind.SMA(d.close, period=8)),
    "crossdown": lambda d: tw.ind.CrossDown(tw.ind.SMA(d.close, period=3), tw.ind.SMA(d.close, period=8)),
    "stddev": lambda d: tw.ind.StdDev(d.close, period=6),
}


class Holder(tw.Strategy):
    def __init__(self):
        self.inds = {name: make(self.data) for name, make in BUILT_INS.items()}


def _lines(frame):
    """Every built-in's lines computed by ``once()`` on ``frame``, as float arrays."""
    cerebro = tw.Cerebro(stdstats=False)
    cerebro.adddata(tw.feeds.PandasData(dataname=frame), name="X")
    cerebro.addstrategy(Holder)
    strat = cerebro.run()[0]
    return {name: [np.array(buf.array, copy=True) for buf in ind.lines] for name, ind in strat.inds.items()}


def _same(a, b):
    return (a == b) or (np.isnan(a) and np.isnan(b))


def test_once_equals_the_bar_by_bar_values_bit_for_bit():
    frame = _frame()
    whole = _lines(frame)
    mismatches = []
    for t in range(N_BARS):
        prefix = _lines(frame.iloc[: t + 1])
        for name, lines in whole.items():
            for k, line in enumerate(lines):
                a, b = float(line[t]), float(prefix[name][k][t])
                if not _same(a, b):
                    mismatches.append((name, k, t, a, b))
    assert not mismatches, mismatches[:5]
    # every built-in produced values (the comparison is not vacuous)
    for name, lines in whole.items():
        assert np.isfinite(lines[0]).sum() > N_BARS // 3, name
