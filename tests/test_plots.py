"""The native plots (tradewind.plots) and the run's pandas views (Performance)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import tradewind as tw

matplotlib = pytest.importorskip("matplotlib")
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _bars(seed: int, n: int = 600) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2019-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n)))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1e6}, index=index)


class _Monthly(tw.Strategy):
    def __init__(self):
        self.month = None

    def next(self):
        month = self.datetime.date(0).month
        if month == self.month:
            return
        self.month = month
        sign = 1 if month % 2 else -1          # a short in even months: net differs from gross
        self.order_target_percent(self.datas[0], target=0.5)
        self.order_target_percent(self.datas[1], target=0.3 * sign)


@pytest.fixture(scope="module")
def run():
    engine = tw.Engine()
    for i, name in enumerate(["AAA", "BBB", "SPY"]):
        engine.adddata(tw.feeds.PandasData(dataname=_bars(i)), name=name)
    engine.addstrategy(_Monthly)
    engine.addanalyzer(tw.analyzers.Performance, _name="performance", benchmark="SPY")
    engine.broker.setcash(100_000)
    strategy = engine.run()[0]
    return strategy, strategy.analyzers.performance


@pytest.fixture(autouse=True)
def _close_figures():
    yield
    plt.close("all")


def test_pandas_views_agree_with_the_run(run):
    _, perf = run
    nav = perf.nav
    assert len(nav) == perf.report.bars and nav.iloc[-1] == perf.values[-1]
    assert perf.returns.iloc[0] == pytest.approx(nav.iloc[1] / nav.iloc[0] - 1)
    positions = perf.positions
    assert set(positions.columns) == {"AAA", "BBB", "cash"}
    # positions + cash = the portfolio value (shorts are negative market values)
    np.testing.assert_allclose(positions.sum(axis=1).to_numpy(), nav.to_numpy(), rtol=1e-9)
    assert (positions["BBB"] < 0).any() and (positions["BBB"] > 0).any()
    assert len(perf.transactions) == perf.report.fills
    assert list(perf.transactions.columns) == ["symbol", "amount", "price", "commission"]
    exposures = perf.exposures
    assert exposures["gross"].mean() == pytest.approx(perf.report.gross_exposure)
    assert exposures["net"].mean() == pytest.approx(perf.report.net_exposure)
    assert perf.benchmark_nav.iloc[-1] == pytest.approx(_bars(2)["close"].iloc[-1])


def test_full_figure_builds(run):
    strategy, perf = run
    fig = perf.plot(title="test")
    assert len(fig.axes) == 4
    assert tw.plots.plot(strategy).axes      # the strategy carrying the analyzer works too
    fig_nav = tw.plots.plot(perf.nav)          # a bare NAV: no exposure panel
    assert len(fig_nav.axes) == 3


def test_functions_draw_on_the_given_axes(run):
    _, perf = run
    _, axes = plt.subplots(4)
    assert tw.plots.plot_equity(perf, ax=axes[0]) is axes[0]
    assert tw.plots.plot_drawdown(perf, ax=axes[1]) is axes[1]
    assert tw.plots.plot_monthly_returns(perf, ax=axes[2]) is axes[2]
    assert tw.plots.plot_exposure(perf, ax=axes[3]) is axes[3]
    labels = [line.get_label() for line in axes[0].get_lines()]
    assert labels == ["strategy", "SPY"]
    # the benchmark is rebased to the strategy's first value
    assert axes[0].get_lines()[1].get_ydata()[0] == pytest.approx(perf.nav.iloc[0])
    assert axes[0].get_yscale() == "log"


def test_the_drawdown_written_is_the_deepest():
    nav = pd.Series([100, 120, 90, 95, 130, 104, 140],
                    index=pd.bdate_range("2024-01-01", periods=7), dtype=float)
    ax = tw.plots.plot_drawdown(nav)
    (text,) = [t.get_text() for t in ax.texts if t.get_gid() == "max-drawdown"]
    # 120 -> 90 is -25 %, deeper than 130 -> 104 (-20 %)
    assert text == "max drawdown -25.0%  (2024-01-02 to 2024-01-03)"
    np.testing.assert_allclose(tw.plots.drawdown_series(nav).to_numpy(),
                               [0, 0, -0.25, 95 / 120 - 1, 0, -0.2, 0], atol=1e-15)
    assert tw.metrics.max_drawdown(nav.to_list()).depth == pytest.approx(0.25)


def test_monthly_returns_are_exact():
    index = pd.to_datetime(["2023-12-28", "2023-12-29", "2024-01-15", "2024-01-31",
                            "2024-02-29", "2024-03-01", "2024-03-29"])
    nav = pd.Series([100.0, 110.0, 99.0, 121.0, 108.9, 120.0, 130.68], index=index)
    table = tw.plots.monthly_returns(nav)
    assert list(table.index) == [2023, 2024]
    assert table.loc[2023, "Dec"] == pytest.approx(0.10)            # from the first value
    assert table.loc[2024, "Jan"] == pytest.approx(121 / 110 - 1)   # month end over month end
    assert table.loc[2024, "Feb"] == pytest.approx(-0.10)
    assert table.loc[2024, "Mar"] == pytest.approx(130.68 / 108.9 - 1)
    assert np.isnan(table.loc[2024, "Apr"]) and np.isnan(table.loc[2023, "Jan"])
    assert table.loc[2023, "Year"] == pytest.approx(0.10)
    assert table.loc[2024, "Year"] == pytest.approx(130.68 / 110 - 1)
    ax = tw.plots.plot_monthly_returns(nav)
    cells = {t.get_gid(): t.get_text() for t in ax.texts}
    assert cells["cell-0-11"] == "10.0"
    assert cells["cell-1-0"] == f"{(121 / 110 - 1) * 100:.1f}"
    assert cells["cell-1-1"] == "-10.0"
    assert cells["cell-1-12"] == f"{(130.68 / 110 - 1) * 100:.1f}"
    assert "cell-1-3" not in cells                                   # no April: no text


def test_monthly_returns_of_a_run_match_its_nav(run):
    _, perf = run
    table = tw.plots.monthly_returns(perf)
    nav = perf.nav
    month_ends = nav.resample("ME").last().dropna()
    expected = month_ends.pct_change()
    expected.iloc[0] = month_ends.iloc[0] / nav.iloc[0] - 1
    got = table.drop(columns="Year").stack().dropna()
    np.testing.assert_allclose(got.to_numpy(), expected.to_numpy(), rtol=1e-12)


def test_errors_say_what_is_missing(run):
    _, _ = run
    with pytest.raises(TypeError):
        tw.plots.plot_exposure(pd.Series([1.0, 2.0], index=pd.bdate_range("2024-01-01", periods=2)))
    with pytest.raises(TypeError):
        tw.plots.plot_equity([1.0, 2.0])

    class Bare(tw.Strategy):
        pass

    engine = tw.Engine()
    engine.adddata(tw.feeds.PandasData(dataname=_bars(5, 30)), name="X")
    engine.addstrategy(Bare)
    bare = engine.run()[0]
    with pytest.raises(ValueError, match="Performance"):
        tw.plots.plot(bare)
