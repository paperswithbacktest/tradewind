"""The bridge to pyfolio and alphalens (tradewind.tearsheets): skipped without the extra
``tradewind[tearsheets]``."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import tradewind as tw

pytest.importorskip("pyfolio")
pytest.importorskip("alphalens")
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _bars(seed: int, n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2020-01-01", periods=n)
    close = 50 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, n)))
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close,
                         "volume": 1e6}, index=index)


class _Rotation(tw.Strategy):
    def __init__(self):
        self.month = None

    def next(self):
        month = self.datetime.date(0).month
        if month == self.month:
            return
        self.month = month
        for i, data in enumerate(self.datas[:3]):
            self.order_target_percent(data, target=0.3 if (i + month) % 3 else 0.0)


@pytest.fixture(scope="module")
def run():
    engine = tw.Engine()
    for i, name in enumerate(["AAA", "BBB", "CCC", "SPY"]):
        engine.adddata(tw.feeds.PandasData(dataname=_bars(i)), name=name)
    engine.addstrategy(_Rotation)
    engine.addanalyzer(tw.analyzers.Performance, _name="performance", benchmark="SPY")
    engine.broker.setcash(100_000)
    strategy = engine.run()[0]
    return strategy, strategy.analyzers.performance


def test_to_pyfolio_shapes(run):
    _, perf = run
    returns, positions, transactions = perf.to_pyfolio()
    assert str(returns.index.tz) == "UTC" and len(returns) == len(perf.nav) - 1
    nav = perf.nav
    assert returns.iloc[-1] == pytest.approx(nav.iloc[-1] / nav.iloc[-2] - 1)
    assert positions.index.equals(returns.index) and "cash" in positions.columns
    assert list(transactions.columns) == ["amount", "price", "symbol"]
    assert len(transactions) == perf.report.fills
    # pyfolio's own reading of them: the gross leverage it computes is ours
    import pyfolio

    leverage = pyfolio.timeseries.gross_lev(positions)
    np.testing.assert_allclose(leverage.to_numpy(), perf.exposures["gross"].to_numpy()[1:],
                               rtol=1e-9)


def test_simple_tearsheet_draws(run):
    _, perf = run
    figures = perf.tearsheet("simple")
    assert figures and figures[0].axes
    plt.close("all")
    with pytest.raises(ValueError):
        perf.tearsheet("medium")


def test_to_alphalens(run):
    strategy, _ = run
    closes = tw.tearsheets.feed_closes(strategy)
    assert list(closes.columns) == ["AAA", "BBB", "CCC", "SPY"]
    factor = closes.pct_change(20).iloc[::21].dropna()
    data = tw.tearsheets.to_alphalens(factor, strategy, quantiles=2, periods=(1, 5),
                                      max_loss=1.0)
    assert {"1D", "5D", "factor", "factor_quantile"} <= set(data.columns)
    assert set(data["factor_quantile"].unique()) == {1, 2}
