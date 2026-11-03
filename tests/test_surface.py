"""The ``bt.*`` names the corpus reads that the first engine lacked: ``If`` and line
arithmetic in ``__init__``, ``LineRoot`` / ``LineIterator``, ``analyzers``. Behaviour
fixed on the oracle (probes ``line_ops``, ``minperiod_add``, ``minperiod_chain``,
``analyzers`` in ``tests/oracle/strategies.py``); here, the parts that need no oracle."""

import math
import os
import sys
from typing import Dict, List  # noqa: UP035 -- written as the corpus writes it

import numpy as np
import pandas as pd
import pytest

import tradewind as bt

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))


def frames(n_bars=60, seed=3):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2022-01-03", periods=n_bars)
    close = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, n_bars)))
    return {"A": pd.DataFrame({"open": close * 0.998, "high": close * 1.01, "low": close * 0.99,
                               "close": close, "volume": 1e4}, index=index)}


def run(cls, analyzers=(), **kw):
    c = bt.Cerebro(stdstats=False)
    for name, f in frames().items():
        c.adddata(bt.feeds.PandasData(dataname=f), name=name)
    c.addstrategy(cls, **kw)
    c.broker.setcash(10_000.0)
    for name, acls, akw in analyzers:
        c.addanalyzer(acls, _name=name, **akw)
    return c.run()[0]


def test_engine_has_no_get():
    # ``bt.get(sym)`` in the corpus is a local dict named ``bt``; the oracle has no such name
    assert not hasattr(bt, "get")


def test_if_and_operators_build_lines_in_init_and_read_values_in_next():
    class S(bt.Strategy):
        def __init__(self):
            h = bt.ind.Highest(self.data.close, period=5)
            self.cond = h > 0.0
            self.vf = bt.If(self.cond, (h - self.data.low) / h * 100.0, 0.0)
            self.ma = bt.ind.SMA(self.vf, period=4)
            self.rows = []

        def next(self):
            cmp = self.data.close > self.data.open
            self.rows.append((len(self), self.vf[0], self.ma[0], type(cmp)))

    s = run(S)
    assert isinstance(s.vf, bt.LinesOperation)
    assert s.rows[0][0] == 8  # 5 + 4 - 1
    assert all(t is bool for *_, t in s.rows)
    d = s.datas[0]
    hi = d.close.array[:5].max()
    assert s.vf.lines[0].array[4] == (hi - d.low.array[4]) / hi * 100.0
    assert np.isnan(s.vf.lines[0].array[:4]).all()  # NaN before the min period, not the else-branch


def test_built_comparison_has_no_truth_value():
    class S(bt.Strategy):
        def __init__(self):
            with pytest.raises(TypeError):
                bool(self.data.close > self.data.open)

    run(S)


def test_division_by_zero_raises_like_python_floats():
    class S(bt.Strategy):
        def __init__(self):
            self.z = self.data.close / (self.data.close - self.data.close)

    with pytest.raises(ZeroDivisionError):
        run(S)


def test_minperiods_add_up_and_chain():
    class Two(bt.Indicator):
        lines = ("x",)

        def __init__(self):
            self.addminperiod(5)
            self.addminperiod(3)

        def next(self):
            self.lines.x[0] = self.data.close[0]

    class Upd(Two):
        def __init__(self):
            self.addminperiod(5)
            self.updateminperiod(3)

    assert run_minperiod(lambda s: Two(s.data)) == 7
    assert run_minperiod(lambda s: Upd(s.data)) == 5
    assert run_minperiod(lambda s: bt.ind.SMA(bt.ind.SMA(s.data.close, period=3), period=5)) == 7
    assert run_minperiod(lambda s: bt.ind.CrossOver(bt.ind.SMA(s.data.close, period=3),
                                                    bt.ind.SMA(s.data.close, period=5))) == 6
    assert run_minperiod(lambda s: 100.0 - bt.ind.SMA(s.data.close, period=4)) == 4


def run_minperiod(make):
    class S(bt.Strategy):
        def __init__(self):
            self.x = make(self)
            self.first = None

        def next(self):
            if self.first is None:
                self.first = len(self)

    return run(S).first


def test_line_markers_answer_like_the_oracle():
    seen = {}

    class S(bt.Strategy):
        def __init__(self):
            ind = bt.ind.SMA(self.data.close, period=3)
            built = self.data.close - self.data.open
            for k, o in {"feed": self.data, "line": self.data.close, "indicator": ind,
                         "built": built, "strategy": self, "lines": self.data.lines}.items():
                seen[k] = (isinstance(o, bt.LineRoot), isinstance(o, bt.LineIterator))
            # the corpus annotates with them
            self.meta: Dict[bt.LineRoot, dict] = {d: {} for d in self.datas}  # noqa: UP006
            self.side: List[bt.LineIterator] = []  # noqa: UP006

    s = run(S, analyzers=[("dd", bt.analyzers.DrawDown, {})])
    assert seen == {"feed": (True, False), "line": (True, False), "indicator": (True, True),
                    "built": (True, False), "strategy": (True, True), "lines": (False, False)}
    assert not isinstance(s.analyzers.dd, bt.LineRoot)


def test_analyses_shape():
    class S(bt.Strategy):
        def next(self):
            if len(self) == 2:
                self.buy(size=10)
            if len(self) == 20:
                self.close()

    s = run(S, analyzers=[("t", bt.analyzers.TradeAnalyzer, {}), ("dd", bt.analyzers.DrawDown, {}),
                          ("sr", bt.analyzers.SharpeRatio, {"timeframe": bt.TimeFrame.Days})])
    t = s.analyzers.t.get_analysis()
    assert t.total.closed == 1 and t.total.open == 0
    assert t.len.long.lost.min == sys.maxsize or t.len.long.won.min == sys.maxsize
    with pytest.raises(KeyError):
        _ = t.nothing
    dd = s.analyzers.dd.get_analysis()
    assert dd.max.drawdown >= dd.drawdown >= 0.0
    sr = s.analyzers.sr.get_analysis()
    assert type(sr).__name__ == "OrderedDict" and math.isfinite(sr["sharperatio"])


def test_trade_analyzer_without_trades():
    s = run(bt.Strategy, analyzers=[("t", bt.analyzers.TradeAnalyzer, {})])
    assert s.analyzers.t.get_analysis() == {"total": {"total": 0}}


def test_codemod_surface_ignores_a_local_bt():
    from codemod_tradewind import engine_aliases

    assert engine_aliases("import backtrader as bt\n") == {"bt"}
    assert engine_aliases("from pwb_toolbox.backtesting import bt\n") == {"bt"}
    assert engine_aliases("import pwb_toolbox.backtesting as pwb_bt\nbt = {}\nbt.get(1)\n") == set()
