"""Plots of a backtest: equity curve, drawdown, monthly returns, exposure and turnover.

matplotlib is an optional dependency (``pip install 'tradewind[plot]'``), imported only when
a plot is drawn. Every function takes the result of a run as ``source``:

* a ``tw.analyzers.Performance`` analyzer (or the strategy that carries one), which gives
  the NAV, the benchmark, the exposures and the turnover;
* or a ``pandas.Series`` of portfolio values indexed by time (equity, drawdown and monthly
  returns only).

Each ``plot_*`` function draws on ``ax`` (a new figure when None) and returns the axes;
``plot`` puts the four panels on one figure and returns it. The figures read their numbers
from ``tradewind.metrics`` (drawdown) and from :func:`monthly_returns`, so what a panel
prints is what those functions return.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from . import metrics

__all__ = [
    "drawdown_series",
    "monthly_returns",
    "plot",
    "plot_drawdown",
    "plot_equity",
    "plot_exposure",
    "plot_monthly_returns",
]

# Colours: one blue for the strategy, a muted grey for the reference, a red for losses, a
# blue-grey-red diverging scale for the monthly returns.
STRATEGY = "#2a78d6"
BENCHMARK = "#898781"
LOSS = "#e34948"
NET = "#eb6834"
TURNOVER = "#1baf7a"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e1e0d9"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _pyplot() -> Any:
    try:
        import matplotlib.pyplot as plt
    except ImportError as err:  # pragma: no cover - depends on the environment
        raise ImportError("plotting needs matplotlib: pip install 'tradewind[plot]'") from err
    return plt


def _performance(source: Any) -> Any:
    """The ``Performance`` analyzer behind ``source``, or None for a plain series."""
    from .performance import Performance

    if isinstance(source, Performance):
        return source
    analyzers = getattr(source, "analyzers", None)
    if analyzers is not None:
        for analyzer in analyzers:
            if isinstance(analyzer, Performance):
                return analyzer
        raise ValueError("the strategy has no Performance analyzer: "
                         "engine.addanalyzer(tw.analyzers.Performance) before engine.run()")
    return None


def _nav(source: Any) -> pd.Series:
    perf = _performance(source)
    if perf is not None:
        nav = perf.nav
    elif isinstance(source, pd.Series):
        nav = source.astype("float64")
    else:
        raise TypeError("source: a Performance analyzer, a strategy carrying one, "
                        "or a pandas Series of portfolio values")
    if len(nav) < 2:
        raise ValueError("a plot needs at least two values")
    return nav


def _axes(ax: Any, size: tuple[float, float]) -> Any:
    if ax is not None:
        return ax
    _, ax = _pyplot().subplots(figsize=size)
    return ax


def _style(ax: Any, title: str) -> None:
    ax.set_title(title, loc="left", fontsize=11, color=INK)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c3c2b7")
    ax.tick_params(colors=INK_2, labelsize=9)


def _percent(ax: Any, decimals: int = 0) -> None:
    from matplotlib.ticker import PercentFormatter

    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=decimals))


# -- numbers -----------------------------------------------------------------------------
def drawdown_series(source: Any) -> pd.Series:
    """The fall from the running peak at every time, as a negative fraction (0 at a peak)."""
    nav = _nav(source)
    return (nav / nav.cummax() - 1.0).rename("drawdown")


def monthly_returns(source: Any) -> pd.DataFrame:
    """The return of every calendar month, one row per year and one column per month
    (``Jan`` ... ``Dec``), plus ``Year``: the return of the calendar year. A month is the
    last value of the month over the last value of the month before; the first month and
    the first year start from the first value of the run. Months outside the run are NaN."""
    nav = _nav(source)
    ends = nav.groupby([nav.index.year, nav.index.month]).last()
    previous = ends.shift(1)
    previous.iloc[0] = nav.iloc[0]
    by_month = ends / previous - 1.0
    table = by_month.unstack()
    table = table.reindex(columns=range(1, 13))
    table.columns = MONTHS
    year_ends = nav.groupby(nav.index.year).last()
    year_start = year_ends.shift(1)
    year_start.iloc[0] = nav.iloc[0]
    table["Year"] = year_ends / year_start - 1.0
    table.index.name = "year"
    return table


# -- panels ------------------------------------------------------------------------------
def plot_equity(source: Any, benchmark: Any = None, log: bool = True, ax: Any = None,
                label: str = "strategy", benchmark_label: str | None = None) -> Any:
    """The portfolio value over time, on a log scale by default, against an optional
    reference rebased to the same starting value.

    ``benchmark``: a ``pandas.Series`` of prices or values; by default the closes of the
    ``Performance`` analyzer's own ``benchmark`` feed when it has one."""
    nav = _nav(source)
    ax = _axes(ax, (9, 4))
    if benchmark is None:
        perf = _performance(source)
        if perf is not None and perf.benchmark_nav is not None:
            benchmark = perf.benchmark_nav
            if benchmark_label is None:
                name = perf.p.benchmark
                benchmark_label = name if isinstance(name, str) else "benchmark"
    ax.plot(nav.index, nav.to_numpy(), color=STRATEGY, linewidth=1.6, label=label)
    if benchmark is not None:
        bench = benchmark.astype("float64").reindex(nav.index).ffill()
        first = bench.first_valid_index()
        if first is not None and bench[first] > 0:
            rebased = bench / bench[first] * nav[first]
            ax.plot(rebased.index, rebased.to_numpy(), color=BENCHMARK, linewidth=1.2,
                    label=benchmark_label or "benchmark")
    if log:
        ax.set_yscale("log")
        from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter

        ax.yaxis.set_major_locator(FixedLocator(_log_ticks(*ax.get_ylim())))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:,.0f}"))
        ax.yaxis.set_minor_formatter(NullFormatter())
    _style(ax, "Portfolio value" + (" (log scale)" if log else ""))
    ax.set_xlim(nav.index[0], nav.index[-1])
    if benchmark is not None:
        ax.legend(frameon=False, fontsize=9, loc="upper left")
    return ax


def _log_ticks(low: float, high: float) -> list[float]:
    """Round values (1-2-5 per decade, finer on a short range) for a log axis."""
    if not (low > 0 and high > low):
        return []
    ticks: list[float] = []
    for mantissas in ((1.0,), (1.0, 2.0, 5.0), (1.0, 1.5, 2.0, 3.0, 5.0, 7.0),
                      (1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0)):
        ticks = [m * 10.0 ** k for k in range(int(np.floor(np.log10(low))),
                                              int(np.ceil(np.log10(high))) + 1)
                 for m in mantissas if low <= m * 10.0 ** k <= high]
        if len(ticks) >= 4:
            break
    return ticks


def plot_drawdown(source: Any, ax: Any = None) -> Any:
    """The drawdown from the running peak (an "underwater" chart), with the deepest one
    written on the panel: its depth (``tw.metrics.max_drawdown``) and its dates."""
    nav = _nav(source)
    dd = drawdown_series(nav)
    worst = metrics.max_drawdown(nav.to_list())
    ax = _axes(ax, (9, 2.5))
    ax.fill_between(dd.index, dd.to_numpy(), 0.0, color=LOSS, alpha=0.35, linewidth=0)
    ax.plot(dd.index, dd.to_numpy(), color=LOSS, linewidth=0.8)
    _percent(ax)
    _style(ax, "Drawdown")
    ax.set_xlim(nav.index[0], nav.index[-1])
    ax.set_ylim(min(-0.01, float(dd.min()) * 1.1), 0.0)
    if worst.depth > 0:
        peak, trough = nav.index[worst.peak], nav.index[worst.trough]
        text = (f"max drawdown {-worst.depth:.1%}  "
                f"({peak:%Y-%m-%d} to {trough:%Y-%m-%d})")
        ax.text(0.01, 0.06, text, transform=ax.transAxes, fontsize=9, color=INK,
                gid="max-drawdown")
    return ax


def _diverging() -> Any:
    from matplotlib.colors import LinearSegmentedColormap

    return LinearSegmentedColormap.from_list(
        "tw_diverging", ["#b42f2f", "#e34948", "#f0efec", "#2a78d6", "#1c5cab"])


def plot_monthly_returns(source: Any, ax: Any = None, decimals: int = 1) -> Any:
    """The monthly returns as a years × months grid (the ``Year`` column is the calendar
    year's return), each cell written as a percentage: :func:`monthly_returns` drawn."""
    table = monthly_returns(source)
    ax = _axes(ax, (9, 0.32 * len(table) + 1.2))
    from matplotlib.colors import TwoSlopeNorm

    months = table[MONTHS].to_numpy(dtype=float)
    finite = np.abs(months[np.isfinite(months)])
    bound = float(np.quantile(finite, 0.95)) if finite.size else 0.01
    bound = max(bound, 1e-4)
    norm = TwoSlopeNorm(vcenter=0.0, vmin=-bound, vmax=bound)
    grid = np.ma.masked_invalid(table.to_numpy(dtype=float))
    shown = np.ma.masked_invalid(np.hstack([months, np.full((len(table), 1), np.nan)]))
    ax.imshow(shown, cmap=_diverging(), norm=norm, aspect="auto", interpolation="nearest")
    for r in range(grid.shape[0]):
        for c in range(grid.shape[1]):
            value = grid[r, c]
            if np.ma.is_masked(value):
                continue
            weight = "bold" if c == 12 else "normal"
            ax.text(c, r, f"{round(value * 100, decimals) + 0.0:.{decimals}f}", ha="center", va="center",
                    fontsize=7.5, color=INK, fontweight=weight, gid=f"cell-{r}-{c}")
    ax.set_xticks(range(13), MONTHS + ["Year"])
    ax.set_yticks(range(len(table)), [str(y) for y in table.index])
    ax.tick_params(length=0, colors=INK_2, labelsize=8.5)
    ax.xaxis.tick_top()
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.axvline(11.5, color="white", linewidth=3)
    ax.set_title("Monthly returns (%)", loc="left", fontsize=11, color=INK, pad=22)
    return ax


def plot_exposure(source: Any, ax: Any = None, window: int | None = None) -> Any:
    """Gross and net exposure (multiples of the portfolio value) and the turnover over a
    trailing year (traded value / (2 × portfolio value), summed over ``window`` bars,
    ``periods_per_year`` by default): one axis, all three in multiples of the NAV."""
    perf = _performance(source)
    if perf is None:
        raise TypeError("plot_exposure needs a Performance analyzer (or its strategy)")
    frame = perf.exposures
    if len(frame) < 2:
        raise ValueError("a plot needs at least two values")
    window = int(window or perf.p.periods_per_year)
    turnover = (frame["traded"] / 2).rolling(window, min_periods=window).sum()
    ax = _axes(ax, (9, 2.5))
    ax.plot(frame.index, frame["gross"].to_numpy(), color=STRATEGY, linewidth=1.2,
            label="gross exposure")
    ax.plot(frame.index, frame["net"].to_numpy(), color=NET, linewidth=1.2,
            label="net exposure")
    ax.plot(turnover.index, turnover.to_numpy(), color=TURNOVER, linewidth=1.2,
            label=f"turnover, trailing {window} bars")
    ax.axhline(0.0, color="#c3c2b7", linewidth=0.8)
    low = min(0.0, float(np.nanmin(frame["net"].to_numpy())))
    high = float(np.nanmax(np.r_[frame["gross"].to_numpy(), turnover.dropna().to_numpy(), 0.0]))
    ax.set_ylim(low - 0.05 * (high - low + 1e-9), high + 0.35 * (high - low + 1e-9))
    from matplotlib.ticker import FuncFormatter

    ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:.1f}×"))
    _style(ax, "Exposure and turnover (× portfolio value)")
    ax.set_xlim(frame.index[0], frame.index[-1])
    ax.legend(frameon=False, fontsize=8.5, loc="upper left", ncol=3)
    return ax


def plot(source: Any, benchmark: Any = None, log: bool = True, title: str | None = None,
         figsize: tuple[float, float] | None = None) -> Any:
    """Equity against the benchmark, drawdown, exposure and turnover (when ``source`` is
    a ``Performance`` analyzer or its strategy) and the monthly returns, on one figure,
    which is returned (``fig.savefig("run.png")`` to keep it)."""
    plt = _pyplot()
    nav = _nav(source)
    has_exposure = _performance(source) is not None
    years = len(monthly_returns(nav))
    heat = max(2.0, 0.26 * years + 0.9)
    ratios = [3.0, 1.3] + ([1.3] if has_exposure else []) + [heat]
    size = figsize or (10, sum(ratios) + 0.6 * len(ratios))
    fig = plt.figure(figsize=size, layout="constrained")
    grid = fig.add_gridspec(len(ratios), 1, height_ratios=ratios)
    top = fig.add_subplot(grid[0])
    plot_equity(source, benchmark=benchmark, log=log, ax=top)
    under = fig.add_subplot(grid[1], sharex=top)
    plot_drawdown(nav, ax=under)
    row = 2
    if has_exposure:
        plot_exposure(source, ax=fig.add_subplot(grid[2], sharex=top))
        row = 3
    plot_monthly_returns(nav, ax=fig.add_subplot(grid[row]))
    if title:
        fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    return fig
