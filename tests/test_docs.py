"""The documentation's examples and snippets run, and say what the pages say they do."""

from __future__ import annotations

import os
import runpy
import sys

import numpy as np
import pytest

import tradewind as tw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "docs", "examples")
sys.path.insert(0, EXAMPLES)

from _data import bars


@pytest.mark.parametrize("script", sorted(f for f in os.listdir(EXAMPLES) if f[:2].isdigit()))
def test_example_runs(script, capsys, monkeypatch):
    monkeypatch.chdir(EXAMPLES)
    runpy.run_path(os.path.join(EXAMPLES, script), run_name="__main__")
    assert "final value" in capsys.readouterr().out or script.startswith("05")


def _run(strategy, frames, cash=100_000, analyzers=(), setup=None, **params):
    cerebro = tw.Cerebro()
    for name, frame in frames.items():
        cerebro.adddata(tw.feeds.PandasData(dataname=frame), name=name)
    cerebro.addstrategy(strategy, **params)
    for cls, kwargs in analyzers:
        cerebro.addanalyzer(cls, **kwargs)
    cerebro.broker.setcash(cash)
    if setup:
        setup(cerebro)
    return cerebro, cerebro.run()[0]


class _Trend(tw.Strategy):
    def __init__(self):
        self.sma = tw.ind.SMA(self.data.close, period=50)
        self.first_next = None

    def next(self):
        if self.first_next is None:
            self.first_next = len(self)
        self.order_target_percent(target=0.95 if self.data.close[0] > self.sma[0] else 0.0)


def test_getting_started_analyzers_and_warmup():
    _, s = _run(_Trend, {"A": bars("A", seed=1)},
                analyzers=[(tw.analyzers.DrawDown, {"_name": "dd"}),
                           (tw.analyzers.SharpeRatio, {"_name": "sharpe", "timeframe": tw.TimeFrame.Days,
                                                       "annualize": True}),
                           (tw.analyzers.TradeAnalyzer, {"_name": "trades"})])
    assert s.first_next == 50                       # "for the first 49 bars ... prenext"
    assert s.analyzers.dd.get_analysis()["max"]["drawdown"] > 0
    assert isinstance(s.analyzers.sharpe.get_analysis()["sharperatio"], float)
    ta = s.analyzers.trades.get_analysis()
    assert ta.total.closed > 0 and ta.won.total + ta.lost.total == ta.total.closed
    ta.pnl.net.total, ta.won.pnl.average, ta.lost.pnl.max, ta.len.average  # noqa: B018


def test_commission_schemes():
    class SpreadCost(tw.CommInfoBase):
        params = (("half_spread_bps", 5.0), ("stocklike", True), ("commtype", tw.CommInfoBase.COMM_PERC))

        def _getcommission(self, size, price, pseudoexec):
            return abs(size) * price * self.p.half_spread_bps / 10_000

    values = []
    for setup in (lambda c: None,
                  lambda c: c.broker.setcommission(commission=0.001),
                  lambda c: c.broker.setcommission(commission=0.005, commtype=tw.CommInfoBase.COMM_FIXED,
                                                   stocklike=True),
                  lambda c: c.broker.addcommissioninfo(SpreadCost())):
        cerebro, _ = _run(_Trend, {"A": bars("A", seed=1)}, setup=setup)
        values.append(cerebro.broker.getvalue())
    assert values[0] > values[1] and values[0] > values[2] and values[0] > values[3]


def test_momentum_next_and_once_agree():
    class MomNext(tw.Indicator):
        lines = ("mom",)
        params = (("period", 20),)

        def __init__(self):
            self.addminperiod(self.p.period + 1)

        def next(self):
            self.lines.mom[0] = self.data[0] / self.data[-self.p.period] - 1.0

    class MomOnce(MomNext):
        def once(self, start, end):
            x = self.data.array
            p = self.p.period
            out = self.lines.mom.array
            out[:] = np.nan
            out[p:] = x[p:] / x[:-p] - 1.0

    class Spread(tw.Indicator):
        lines = ("spread",)

        def __init__(self):
            self.lines.spread = tw.ind.SMA(self.data0, period=5) - tw.ind.SMA(self.data1, period=5)

    class Read(tw.Strategy):
        def __init__(self):
            self.a = MomNext(self.data.close, period=20)
            self.b = MomOnce(self.data.close, period=20)
            self.s = Spread(self.data0.close, self.data1.close)
            self.sig = tw.If(self.data.close > tw.ind.SMA(self.data.close, period=30), 1.0, -1.0)
            self.rows = []

        def next(self):
            self.rows.append((self.a[0], self.b[0], self.s[0], self.sig[0]))

    _, s = _run(Read, {"A": bars("A", seed=2), "B": bars("B", seed=3)})
    rows = np.array(s.rows)
    assert np.array_equal(rows[:, 0], rows[:, 1])
    assert np.isfinite(rows).all() and set(np.unique(rows[:, 3])) <= {1.0, -1.0}


def test_custom_analyzer_and_numpy_feed():
    class Exposure(tw.Analyzer):
        def start(self):
            self.gross = []

        def next(self):
            value = self.strategy.broker.getvalue()
            held = sum(abs(self.strategy.broker.get_value(datas=[d])) for d in self.strategy.datas)
            self.gross.append(held / value if value else 0.0)

        def get_analysis(self):
            return {"average_gross": sum(self.gross) / max(len(self.gross), 1)}

    class NumpyFeed(tw.DataBase):
        params = (("arrays", None),)

        def _load_arrays(self):
            a = self.p.arrays
            n = len(a["datetime"])
            nan = np.full(n, np.nan)
            return a["datetime"], {k: a.get(k, nan) for k in
                                   ("open", "high", "low", "close", "volume", "openinterest")}

    frame = bars("A", seed=4)
    arrays = {"datetime": np.array([tw.date2num(t.to_pydatetime()) for t in frame.index])}
    arrays.update({k: frame[k].to_numpy() for k in ("open", "high", "low", "close", "volume")})
    cerebro = tw.Cerebro()
    cerebro.adddata(NumpyFeed(arrays=arrays), name="A")
    cerebro.addstrategy(_Trend)
    cerebro.addanalyzer(Exposure, _name="exposure")
    cerebro.broker.setcash(100_000)
    s = cerebro.run()[0]
    gross = s.analyzers.exposure.get_analysis()["average_gross"]
    assert 0.0 < gross < 1.0


def test_unsupported_features_fail_loudly():
    cerebro = tw.Cerebro()
    for call in (cerebro.resampledata, cerebro.replaydata, cerebro.optstrategy, cerebro.plot):
        with pytest.raises(NotImplementedError):
            call()
    with pytest.raises(NotImplementedError):
        tw.ModelSet.realistic()
