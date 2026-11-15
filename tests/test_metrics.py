"""``tradewind.metrics`` and ``tw.analyzers.Performance`` (spec/08 § 3).

The reference formulas below are pwb-toolbox's (``pwb_toolbox/performance/metrics.py``,
PWB's own code, the catalogue's definitions), written out so that the comparison runs
without the toolbox installed; when it is installed the same comparison runs against it.
"""

from __future__ import annotations

import math
import os
import sys
from math import sqrt
from statistics import NormalDist

import numpy as np
import pytest

import tradewind as tw
from tradewind import metrics

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "examples"))

from _data import bars


# -- the toolbox's definitions, as references ----------------------------------------------------
def tb_rets(p):
    return [p[i] / p[i - 1] - 1 for i in range(1, len(p))]


def tb_cagr(p, ppy=252):
    years = (len(p) - 1) / ppy
    return (p[-1] / p[0]) ** (1 / years) - 1


def tb_vol(p, ppy=252):
    rets = tb_rets(p)
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return sqrt(var) * sqrt(ppy)


def tb_sharpe(p, rf=0.0, ppy=252):
    rf_per = rf / ppy
    rets = [p[i] / p[i - 1] - 1 - rf_per for i in range(1, len(p)) if p[i - 1] > 0]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return mean / sqrt(var) * sqrt(ppy)


def tb_sortino(p, rf=0.0, ppy=252):
    rf_per = rf / ppy
    rets = tb_rets(p)
    mean_excess = sum(r - rf_per for r in rets) / len(rets)
    downside = [min(0.0, r - rf_per) for r in rets]
    var = sum(d**2 for d in downside) / len(rets)
    return mean_excess / sqrt(var) * sqrt(ppy)


def tb_alpha_beta(p, b):
    n = min(len(p), len(b))
    strat = [p[i] / p[i - 1] - 1 for i in range(1, n)]
    bench = [b[i] / b[i - 1] - 1 for i in range(1, n)]
    mean_x = sum(bench) / len(bench)
    mean_y = sum(strat) / len(strat)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(bench, strat)) / len(bench)
    var_x = sum((x - mean_x) ** 2 for x in bench) / len(bench)
    beta = cov / var_x
    return mean_y - beta * mean_x, beta


def tb_max_drawdown(p):
    peak, depth, duration, cur = p[0], 0.0, 0, 0
    for price in p:
        if price > peak:
            peak, cur = price, 0
        else:
            cur += 1
            depth = min(depth, price / peak - 1)
        duration = max(duration, cur)
    return depth, duration


def _nav(seed=0, n=600, drift=0.0004, vol=0.012):
    rng = np.random.default_rng(seed)
    return list(100_000 * np.cumprod(1 + rng.normal(drift, vol, n)))


NAVS = [_nav(0), _nav(1, drift=-0.0002), _nav(2, n=40, vol=0.03)]


@pytest.mark.parametrize("nav", NAVS)
def test_same_floats_as_the_toolbox(nav):
    bench = _nav(9, n=len(nav))
    assert metrics.cagr(nav) == tb_cagr(nav)
    assert metrics.annualized_volatility(nav) == tb_vol(nav)
    assert metrics.sharpe_ratio(nav) == tb_sharpe(nav)
    assert metrics.sharpe_ratio(nav, 0.03, 52) == tb_sharpe(nav, 0.03, 52)
    assert metrics.sortino_ratio(nav) == tb_sortino(nav)
    assert metrics.sortino_ratio(nav, 0.02) == tb_sortino(nav, 0.02)
    ab = metrics.alpha_beta(nav, bench)
    assert (ab.alpha_per_period, ab.beta) == tb_alpha_beta(nav, bench)
    assert ab.alpha == ab.alpha_per_period * 252
    depth, _ = tb_max_drawdown(nav)
    assert metrics.max_drawdown(nav).depth == pytest.approx(-depth, rel=1e-15, abs=0)


def test_against_the_installed_toolbox():
    tb = pytest.importorskip("pwb_toolbox.performance.metrics")
    for nav in NAVS:
        bench = _nav(9, n=len(nav))
        assert metrics.cagr(nav) == tb.cagr(nav)
        assert metrics.annualized_volatility(nav) == tb.annualized_volatility(nav)
        assert metrics.sharpe_ratio(nav) == tb.sharpe_ratio(nav)
        assert metrics.sortino_ratio(nav) == tb.sortino_ratio(nav)
        assert metrics.skewness(nav) == tb.skewness(nav)
        assert metrics.kurtosis(nav) == tb.kurtosis(nav)
        ab = metrics.alpha_beta(nav, bench)
        assert (ab.alpha_per_period, ab.beta) == tb.capm_alpha_beta(nav, bench)


def test_the_catalogue_sharpe_is_population_sqrt_252_no_rate():
    nav = NAVS[0]
    r = np.diff(nav) / np.asarray(nav[:-1])
    assert metrics.sharpe_ratio(nav) == pytest.approx(r.mean() / r.std(ddof=0) * math.sqrt(252), rel=1e-12)


def test_drawdown_depth_duration_and_recovery():
    dd = metrics.max_drawdown([100, 110, 99, 105, 88, 112, 112, 111, 120])
    assert dd.depth == pytest.approx(0.2) and (dd.peak, dd.trough, dd.recovered) == (1, 4, 5)
    assert dd.duration == 3                                  # 99, 105, 88 below 110
    assert metrics.max_drawdown([1, 2, 3]) == metrics.Drawdown(0.0, 0, 0, None, 0)
    assert tb_max_drawdown([1, 2, 3])[1] == 1                # the toolbox counts the first bar
    assert metrics.max_drawdown([100, 90, 80]).recovered is None


def test_degenerate_series():
    for f in (metrics.cagr, metrics.annualized_volatility, metrics.sharpe_ratio,
              metrics.sortino_ratio, metrics.probabilistic_sharpe_ratio):
        assert f([]) == 0.0 and f([5.0]) == 0.0
    assert metrics.sharpe_ratio([1.0, 1.0, 1.0]) == 0.0
    assert metrics.cagr([100.0, -5.0]) == -1.0
    assert metrics.total_return([2.0, 3.0]) == 0.5


def test_probabilistic_sharpe_ratio():
    nav = NAVS[0]
    rets = tb_rets(nav)
    n = len(rets)
    mean = sum(rets) / n
    sd = sqrt(sum((r - mean) ** 2 for r in rets) / n)
    sr = mean / sd
    g3 = sum((r - mean) ** 3 for r in rets) / n / sd**3
    g4 = sum((r - mean) ** 4 for r in rets) / n / sd**4
    for target in (0.0, 1.0):
        z = (sr - target / sqrt(252)) * sqrt(n - 1) / sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr**2)
        assert metrics.probabilistic_sharpe_ratio(nav, target) == pytest.approx(NormalDist().cdf(z), rel=1e-12)
    assert metrics.probabilistic_sharpe_ratio(nav, 0.0) > metrics.probabilistic_sharpe_ratio(nav, 1.0)


def test_estimated_capacity():
    # one fill of 1 000 on a 100 000 book in a 1 000 000 dollar-volume name: 1 % of the volume
    # is reached at 1 000 000 of capital
    assert metrics.estimated_capacity([(1_000.0, 100_000.0, 1_000_000.0)]) == pytest.approx(1_000_000)
    rows = [(1_000.0, 1e5, 1e6), (9_000.0, 1e5, 1e8), (10.0, 1e5, 0.0)]
    assert metrics.estimated_capacity(rows, quantile=0.05) == pytest.approx(1e6)
    assert metrics.estimated_capacity(rows, quantile=0.5) == pytest.approx(1e5 * 0.01 * 1e8 / 9_000)
    assert metrics.estimated_capacity([(10.0, 1e5, 0.0)]) is None


# -- the analyzer ------------------------------------------------------------------------------------
class Hold(tw.Strategy):
    params = (("weight", 1.0),)

    def next(self):
        if len(self) == 1 or len(self) % 63 == 0:
            self.order_target_percent(self.getdatabyname("SPY"), target=self.p.weight)


def _run(strategy=Hold, analyzer=True, **params):
    engine = tw.Engine(stdstats=False)
    engine.adddata(tw.feeds.PandasData(dataname=bars("SPY", seed=11, years=3)), name="SPY")
    engine.adddata(tw.feeds.PandasData(dataname=bars("QQQ", seed=12, years=3)), name="QQQ")
    engine.broker.setcash(1_000_000)
    marks = []
    mark = type(engine.broker).mark

    def recording_mark(self, s):
        mark(self, s)
        marks.append(self._value)

    engine.broker.mark = recording_mark.__get__(engine.broker)
    if analyzer:
        engine.addanalyzer(tw.analyzers.Performance, _name="perf", benchmark="SPY")
    engine.addstrategy(strategy, **params)
    return engine.run()[0], marks


def test_performance_reads_the_marks_and_changes_nothing():
    strat, marks = _run()
    _, marks_without = _run(analyzer=False)
    assert [m.hex() for m in marks] == [m.hex() for m in marks_without]
    perf = strat.analyzers.perf
    assert perf.values == marks
    report = perf.report
    assert report.bars == len(marks)
    assert report.sharpe == metrics.sharpe_ratio(marks)
    assert report.volatility == metrics.annualized_volatility(marks)
    assert report.sortino == metrics.sortino_ratio(marks)
    assert report.max_drawdown == metrics.max_drawdown(marks).depth
    assert perf.get_analysis()["sharpe"] == report.sharpe
    assert perf.get_analysis().beta == report.beta


def test_performance_benchmark_exposure_turnover_capacity():
    strat, _ = _run(weight=0.95)
    r = strat.analyzers.perf.report
    assert r.beta == pytest.approx(0.95, abs=0.03)          # held at ~95 % of SPY all along
    assert abs(r.alpha) < 0.02
    assert r.gross_exposure == pytest.approx(r.net_exposure) and 0.85 < r.net_exposure < 0.96
    assert r.short_exposure == 0.0 and r.long_exposure == r.gross_exposure
    assert r.exposure_by_currency == {"USD": pytest.approx(r.net_exposure)}
    assert r.exposure_by_asset_class == {"unclassified": pytest.approx(r.net_exposure)}
    assert r.fills > 1 and 0 < r.turnover < 3
    assert r.capacity is not None and r.capacity > 0
    assert 0.0 <= r.probabilistic_sharpe <= 1.0


def test_performance_long_short_and_asset_class():
    class LongShort(tw.Strategy):
        def __init__(self):
            self.getdatabyname("QQQ").asset_class = "equity"

        def next(self):
            if len(self) == 1:
                self.order_target_percent(self.getdatabyname("SPY"), target=0.5)
                self.order_target_percent(self.getdatabyname("QQQ"), target=-0.3)

    strat, _ = _run(LongShort)
    r = strat.analyzers.perf.report
    assert r.long_exposure > 0.3 and r.short_exposure > 0.1       # weights drift after the first bar
    assert r.gross_exposure == pytest.approx(r.long_exposure + r.short_exposure)
    assert r.net_exposure == pytest.approx(r.long_exposure - r.short_exposure)
    assert r.exposure_by_asset_class["equity"] < 0 < r.exposure_by_asset_class["unclassified"]
