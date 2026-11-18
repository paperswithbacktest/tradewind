"""A bridge to Quantopian's tear sheets: pyfolio for a backtest, alphalens for a factor.

The libraries are optional (``pip install 'tradewind[tearsheets]'``: pyfolio-reloaded,
alphalens-reloaded and empyrical-reloaded, Apache 2.0, maintained by Stefan Jansen; see
``NOTICE``) and imported only when a tear sheet is drawn. Tradewind does not ship or copy
any of their code: this module only reshapes a run into the inputs their documented
functions take, then calls them.

* :func:`to_pyfolio` gives ``(returns, positions, transactions)`` from a
  ``tw.analyzers.Performance`` analyzer (or the strategy that carries one): daily returns,
  daily end-of-day position values with a ``cash`` column, one row per fill.
* :func:`tearsheet` draws pyfolio's simple or full tear sheet from them.
* :func:`to_alphalens` turns a factor (a dates × assets table) and prices (a table, or the
  closes of a run's feeds) into alphalens' factor data.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

__all__ = ["feed_closes", "tearsheet", "to_alphalens", "to_pyfolio"]


def _require(module: str) -> Any:
    import importlib

    try:
        return importlib.import_module(module)
    except ImportError as err:
        raise ImportError(f"{module} is not installed: pip install 'tradewind[tearsheets]'") from err


def _daily(index: pd.DatetimeIndex, tz: str | None) -> pd.DatetimeIndex:
    days = index.normalize()
    if tz is not None:
        days = days.tz_localize(tz) if days.tz is None else days.tz_convert(tz)
    return days


def to_pyfolio(source: Any, tz: str | None = "UTC") -> tuple[pd.Series, pd.DataFrame, pd.DataFrame]:
    """``(returns, positions, transactions)`` in the shapes pyfolio documents.

    ``returns``: the daily simple return of the portfolio value (the last value of each
    day over the last value of the day before; a run on minute bars is reduced to days).
    ``positions``: the market value of every position at the end of each day, one column
    per symbol, plus ``cash``. ``transactions``: one row per fill, indexed by its time,
    with ``amount`` (signed quantity), ``price`` and ``symbol``. Indexes are localised to
    ``tz`` (UTC by default, what pyfolio expects; None keeps them naive)."""
    from .plots import _performance

    perf = _performance(source)
    if perf is None:
        raise TypeError("to_pyfolio needs a Performance analyzer (or the strategy carrying one)")
    nav = perf.nav
    nav = nav.groupby(_daily(nav.index, tz)).last()
    returns = nav.pct_change().iloc[1:].rename("returns")
    positions = perf.positions
    positions = positions.groupby(_daily(positions.index, tz)).last()
    positions = positions.loc[returns.index]
    positions.index.name = None
    fills = perf.transactions
    transactions = fills[["amount", "price", "symbol"]].copy()
    index = pd.DatetimeIndex(transactions.index)
    if tz is not None:
        index = index.tz_localize(tz) if index.tz is None else index.tz_convert(tz)
    transactions.index = index
    transactions.index.name = None
    return returns, positions, transactions


def tearsheet(source: Any, kind: str = "simple", benchmark: Any = None, **kwargs: Any) -> list[Any]:
    """Draw pyfolio's ``simple`` or ``full`` tear sheet of a run and return the matplotlib
    figures it made. ``benchmark``: a series of benchmark prices or values (default: the
    closes of the ``Performance`` analyzer's ``benchmark`` feed, when it has one); its
    daily returns are passed as ``benchmark_rets``. Other keyword arguments go to pyfolio
    (``live_start_date``, ``round_trips``...)."""
    if kind not in ("simple", "full"):
        raise ValueError("kind: 'simple' or 'full'")
    pyfolio = _require("pyfolio")
    import matplotlib.pyplot as plt

    from .plots import _performance

    returns, positions, transactions = to_pyfolio(source)
    if benchmark is None:
        perf = _performance(source)
        benchmark = perf.benchmark_nav if perf is not None else None
    if benchmark is not None:
        bench = benchmark.astype("float64")
        bench = bench.groupby(_daily(pd.DatetimeIndex(bench.index), "UTC")).last()
        kwargs.setdefault("benchmark_rets",
                          bench.pct_change().reindex(returns.index).fillna(0.0))
    kwargs.setdefault("estimate_intraday", False)   # positions are exact end-of-day values
    before = set(plt.get_fignums())
    draw = (pyfolio.create_simple_tear_sheet if kind == "simple"
            else pyfolio.create_full_tear_sheet)
    draw(returns, positions=positions, transactions=transactions, **kwargs)
    return [plt.figure(n) for n in plt.get_fignums() if n not in before]


def feed_closes(strategy: Any) -> pd.DataFrame:
    """The closes of every feed of a run, one column per feed name, indexed by bar time
    (NaN where a feed has no bar): the ``prices`` alphalens takes."""
    from .dateutils import num2date

    columns = {}
    for data in strategy.datas:
        n = len(data)
        times = [num2date(x) for x in data.datetime.get(size=n)]
        closes = np.asarray(data.close.get(size=n), dtype=np.float64)
        columns[data._name] = pd.Series(closes, index=pd.DatetimeIndex(times))
    return pd.DataFrame(columns).sort_index()


def to_alphalens(factor: Any, prices: Any, tz: str | None = "UTC", **kwargs: Any) -> pd.DataFrame:
    """alphalens' factor data (``get_clean_factor_and_forward_returns``) from a factor and
    prices.

    ``factor``: a dates × assets ``DataFrame`` of factor values (what a strategy computed
    on each rebalance date, say), or a ``Series`` indexed by (date, asset). ``prices``: a
    dates × assets ``DataFrame``, or a strategy after its run (the closes of its feeds,
    :func:`feed_closes`). Both are localised to ``tz``. Keyword arguments go to alphalens
    (``quantiles``, ``periods``, ``max_loss``...)."""
    utils = _require("alphalens.utils")
    if not isinstance(prices, pd.DataFrame):
        prices = feed_closes(prices)
    if isinstance(factor, pd.DataFrame):
        stacked = factor.stack()
        stacked.index = stacked.index.set_names(["date", "asset"])
        factor = stacked.dropna()

    def localise(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
        if tz is None:
            return index
        return index.tz_localize(tz) if index.tz is None else index.tz_convert(tz)

    prices = prices.copy()
    prices.index = localise(pd.DatetimeIndex(prices.index))
    dates = localise(pd.DatetimeIndex(factor.index.get_level_values(0)))
    factor = pd.Series(factor.to_numpy(), name="factor", index=pd.MultiIndex.from_arrays(
        [dates, factor.index.get_level_values(1)], names=["date", "asset"]))
    return utils.get_clean_factor_and_forward_returns(factor, prices, **kwargs)
