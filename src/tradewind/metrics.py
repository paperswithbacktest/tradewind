"""Performance statistics of a value series (a NAV), as plain typed functions.

The definitions are those of the Papers With Backtest catalogue and of pwb-toolbox's
``pwb_toolbox.performance.metrics`` (PWB's own code), so a figure computed here equals the
one the site shows for the same NAV: simple returns ``v[t] / v[t-1] - 1``, population
moments (divide by ``n``), annualisation by ``periods_per_year`` (252 for daily bars), no
risk-free rate unless one is given. The sums are Python's ``sum`` in the same order as the
toolbox, so the results agree to the last bit (``tests/test_metrics.py``).

Differences from the toolbox, all deliberate and listed in ``docs/analyzers.md``:

* ``max_drawdown`` returns the depth as a positive fraction (the catalogue's sign; the
  toolbox returns it negative) and its duration counts the bars spent **strictly below** a
  previous peak; the toolbox's counter also counts a bar equal to the peak and the first
  bar of the series, so it reads 1 on a series that never falls.
* ``alpha_beta`` returns the toolbox's per-period alpha and also an annualised one
  (× ``periods_per_year``), the figure Lean reports.
* ``probabilistic_sharpe_ratio`` and ``estimated_capacity`` have no toolbox counterpart:
  the first is Bailey and López de Prado's (2012), the second is Tradewind's own (Lean
  reports a capacity too, with a method of its own).

The catalogue's ``annualReturn`` (website ``lib/strategyRegistry.ts``) annualises over
``n`` points, ``(v[-1] / v[0]) ** (252 / n) - 1``; ``cagr`` here, like the toolbox, over the
``n - 1`` returns. The two differ by one bar in the exponent.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist

__all__ = [
    "AlphaBeta",
    "Drawdown",
    "alpha_beta",
    "annualized_volatility",
    "cagr",
    "estimated_capacity",
    "kurtosis",
    "max_drawdown",
    "probabilistic_sharpe_ratio",
    "returns",
    "sharpe_ratio",
    "skewness",
    "sortino_ratio",
    "total_return",
]


def returns(values: Sequence[float]) -> list[float]:
    """Simple returns ``v[t] / v[t-1] - 1``."""
    v = list(values)
    return [v[i] / v[i - 1] - 1 for i in range(1, len(v))]


def total_return(values: Sequence[float]) -> float:
    v = list(values)
    if not v:
        return 0.0
    return v[-1] / v[0] - 1


def cagr(values: Sequence[float], periods_per_year: float = 252) -> float:
    """Compound annual growth rate over ``len(values) - 1`` periods."""
    v = list(values)
    if len(v) < 2:
        return 0.0
    years = (len(v) - 1) / periods_per_year
    if years == 0:
        return 0.0
    ratio = v[-1] / v[0]
    if ratio <= 0:
        return -1.0          # a ruined account (the toolbox returns a complex number here)
    return float(ratio ** (1 / years) - 1)


def _moments(rets: list[float]) -> tuple[float, float]:
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return mean, var


def annualized_volatility(values: Sequence[float], periods_per_year: float = 252) -> float:
    """Population standard deviation of the returns × √``periods_per_year``."""
    v = list(values)
    if len(v) < 2:
        return 0.0
    _, var = _moments(returns(v))
    return math.sqrt(var) * math.sqrt(periods_per_year)


def sharpe_ratio(values: Sequence[float], risk_free_rate: float = 0.0,
                 periods_per_year: float = 252) -> float:
    """Mean over population deviation of the excess returns, × √``periods_per_year``.

    ``risk_free_rate`` is annual and taken per period as ``rate / periods_per_year``; a
    return whose previous value is not positive is left out (a ruined account); 0.0 when the
    returns do not vary. With the defaults this is the catalogue's Sharpe ratio."""
    v = list(values)
    if len(v) < 2:
        return 0.0
    rf_per = risk_free_rate / periods_per_year
    rets = [v[i] / v[i - 1] - 1 - rf_per for i in range(1, len(v)) if v[i - 1] > 0]
    if not rets:
        return 0.0
    mean, var = _moments(rets)
    if var == 0:
        return 0.0
    return mean / math.sqrt(var) * math.sqrt(periods_per_year)


def sortino_ratio(values: Sequence[float], risk_free_rate: float = 0.0,
                  periods_per_year: float = 252) -> float:
    """Mean excess return over the downside deviation, × √``periods_per_year``.

    The downside deviation is ``sqrt(sum(min(0, r - rf)²) / n)`` over **all** ``n`` returns
    (the target is the risk-free rate per period); 0.0 when no return falls below it."""
    v = list(values)
    if len(v) < 2:
        return 0.0
    rf_per = risk_free_rate / periods_per_year
    rets = returns(v)
    mean_excess = sum(r - rf_per for r in rets) / len(rets)
    downside = [min(0.0, r - rf_per) for r in rets]
    var = sum(d**2 for d in downside) / len(rets)
    if var == 0:
        return 0.0
    return mean_excess / math.sqrt(var) * math.sqrt(periods_per_year)


@dataclass(frozen=True)
class Drawdown:
    """The deepest fall from a running peak: ``depth`` (positive fraction of the peak),
    the bar indices of the ``peak`` and the ``trough``, the bar it ``recovered`` on (None
    if it had not by the end), and ``duration``: the longest run of consecutive bars spent
    strictly below a previous peak (any drawdown, not only the deepest)."""

    depth: float
    peak: int
    trough: int
    recovered: int | None
    duration: int


def max_drawdown(values: Sequence[float]) -> Drawdown:
    v = list(values)
    if not v:
        return Drawdown(0.0, 0, 0, None, 0)
    peak_value = v[0]
    peak_at = 0
    depth = 0.0
    best = (0, 0)
    run = longest = 0
    for i, x in enumerate(v):
        if x >= peak_value:
            peak_value, peak_at = x, i
            run = 0
            continue
        run += 1
        longest = max(longest, run)
        if peak_value > 0:
            dd = 1 - x / peak_value
            if dd > depth:
                depth, best = dd, (peak_at, i)
    recovered = None
    if depth > 0:
        top = v[best[0]]
        recovered = next((i for i in range(best[1] + 1, len(v)) if v[i] >= top), None)
    return Drawdown(depth, best[0], best[1], recovered, longest)


def skewness(values: Sequence[float]) -> float:
    """Population skewness of the returns (0.0 for fewer than two returns or no variance)."""
    v = list(values)
    if len(v) < 3:
        return 0.0
    rets = returns(v)
    mean, var = _moments(rets)
    if var == 0:
        return 0.0
    m3 = sum((r - mean) ** 3 for r in rets) / len(rets)
    return m3 / (math.sqrt(var) ** 3)


def kurtosis(values: Sequence[float]) -> float:
    """Population kurtosis of the returns, not in excess (3 for a normal law)."""
    v = list(values)
    if len(v) < 3:
        return 0.0
    rets = returns(v)
    mean, var = _moments(rets)
    if var == 0:
        return 0.0
    m4 = sum((r - mean) ** 4 for r in rets) / len(rets)
    return m4 / (var**2)


def probabilistic_sharpe_ratio(values: Sequence[float], benchmark_sharpe: float = 0.0,
                               periods_per_year: float = 252) -> float:
    """The probability that the true Sharpe ratio exceeds ``benchmark_sharpe`` (annual),
    given the track record's length, skewness and kurtosis (Bailey and López de Prado,
    "The Sharpe Ratio Efficient Frontier", 2012)::

        PSR = Φ( (SR - SR*) √(n - 1) / √(1 - γ₃ SR + (γ₄ - 1) / 4 SR²) )

    with SR the per-period Sharpe ratio (no risk-free rate), SR* the benchmark per period
    (``benchmark_sharpe / √periods_per_year``), n the number of returns and γ₃, γ₄ the
    population skewness and kurtosis. 0.0 with fewer than three values or when the
    denominator is not positive."""
    v = list(values)
    if len(v) < 3:
        return 0.0
    rets = returns(v)
    n = len(rets)
    mean, var = _moments(rets)
    sr = mean / math.sqrt(var) if var > 0 else 0.0
    target = benchmark_sharpe / math.sqrt(periods_per_year)
    g3, g4 = skewness(v), kurtosis(v)
    denom = 1 - g3 * sr + (g4 - 1) / 4 * sr**2
    if denom <= 0:
        return 0.0
    return NormalDist().cdf((sr - target) * math.sqrt(n - 1) / math.sqrt(denom))


@dataclass(frozen=True)
class AlphaBeta:
    """CAPM regression of the strategy's returns on the benchmark's: ``beta`` (population
    covariance over variance), ``alpha_per_period`` (mean return minus beta times the
    benchmark's), ``alpha`` (that × ``periods_per_year``)."""

    alpha: float
    beta: float
    alpha_per_period: float


def alpha_beta(values: Sequence[float], benchmark: Sequence[float],
               periods_per_year: float = 252) -> AlphaBeta:
    """Alpha and beta of ``values`` against ``benchmark``, two value series of the same
    bars (the first ``min(len)`` values of each are used, as in the toolbox)."""
    v, b = list(values), list(benchmark)
    n = min(len(v), len(b))
    if n < 2:
        return AlphaBeta(0.0, 0.0, 0.0)
    strat = [v[i] / v[i - 1] - 1 for i in range(1, n)]
    bench = [b[i] / b[i - 1] - 1 for i in range(1, n)]
    mean_x = sum(bench) / len(bench)
    mean_y = sum(strat) / len(strat)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(bench, strat)) / len(bench)
    var_x = sum((x - mean_x) ** 2 for x in bench) / len(bench)
    beta = cov / var_x if var_x else 0.0
    alpha = mean_y - beta * mean_x
    return AlphaBeta(alpha * periods_per_year, beta, alpha)


def estimated_capacity(trades: Sequence[tuple[float, float, float]], participation: float = 0.01,
                       quantile: float = 0.05) -> float | None:
    """The portfolio value at which the strategy's trades would start to exceed
    ``participation`` of the securities' traded dollar volume.

    ``trades`` holds one ``(traded_value, portfolio_value, dollar_volume)`` per fill: the
    fill's absolute value, the portfolio's value when it filled, and the security's
    average dollar volume around it. A fill scales with the portfolio, so it reaches the
    limit at ``C = portfolio_value × participation × dollar_volume / traded_value``. The
    capacity is the value-weighted ``quantile`` of those ``C``: the value at which that
    share of the traded dollars (5 % by default) would be above the limit. None without a
    fill that has a positive volume."""
    rows = sorted((nav * participation * dv / tv, tv) for tv, nav, dv in trades
                  if tv > 0 and nav > 0 and dv > 0)
    if not rows:
        return None
    total = sum(tv for _, tv in rows)
    acc = 0.0
    for cap, tv in rows:
        acc += tv
        if acc >= quantile * total:
            return cap
    return rows[-1][0]
