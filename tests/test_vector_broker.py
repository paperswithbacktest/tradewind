"""The broker's batch path (phases 5 and 6 on arrays) against its per-order path.

``BackBroker._tw_vector_min`` decides which path a batch takes; the two must give the same
results bit for bit (spec/05 § 7.3): every mark, the cash, every notification (status,
executed size, price, value, commission, P&L), every trade, the positions and the
portfolio's arrays. The scenarios push the batch path through its branches: long/short
rotations (reductions, reversals), a cash shortage (refusals at acceptance, refusals and
partial reversals at the fill, the restart cap), feeds with missing bars, a late listing,
percentage, fixed and custom commission schemes, ``checksubmit`` off.
"""

from __future__ import annotations

import itertools
import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
import tradewind.order as tw_order
from tradewind.broker import BackBroker
from tradewind.comminfo import CommInfoBase

HERE = os.path.dirname(os.path.abspath(__file__))
ALWAYS, NEVER = 1, 10**9


def frames(n_sym=48, n_bars=70, seed=3):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2020-01-01", periods=n_bars)
    out = []
    for j in range(n_sym):
        close = 40 * np.exp(np.cumsum(rng.normal(0.0002, 0.025, n_bars)))
        openp = close * (1 + rng.normal(0, 0.01, n_bars))
        df = pd.DataFrame({"open": openp, "high": np.maximum(openp, close) * 1.01,
                           "low": np.minimum(openp, close) * 0.99, "close": close, "volume": 1e5},
                          index=index)
        if j % 7 == 3:
            df = df.drop(df.index[rng.choice(np.arange(5, n_bars), 6, replace=False)])  # missing bars
        if j == 5:
            df = df.iloc[12:]                                                          # a late listing
        out.append(df)
    return out


class Recorder(tw.Strategy):
    params = (("gross", 0.95), ("short", True))

    def __init__(self):
        self.k = 0
        self.log = []

    def notify_order(self, order):
        ex = order.executed
        self.log.append(("o", order.ref, order.status, ex.size, ex.price, ex.value, ex.comm, ex.pnl))

    def notify_trade(self, trade):
        self.log.append(("t", trade.data._name, trade.status, trade.size, trade.price, trade.pnl, trade.pnlcomm))

    def next(self):
        self.k += 1
        self.log.append(("v", len(self), self.broker.getvalue(), self.broker.getcash()))
        n = len(self.datas)
        w = self.p.gross / n
        for i, d in enumerate(self.datas):
            phase = (i + self.k) % 4
            if self.p.short and phase == 3:
                target = -w                               # a reversal every few bars
            else:
                target = w * (0.5 + 0.5 * phase)
            self.order_target_percent(d, target=target)


class SpreadLike(CommInfoBase):
    """A scheme of its own ``_getcommission`` (like pwb-toolbox's ``SpreadImpactCommission``)."""

    params = (("stocklike", True), ("commtype", CommInfoBase.COMM_PERC), ("percabs", True))

    def _getcommission(self, size, price, pseudoexec):
        return abs(size) * price * (0.0007 + 0.0001 * (abs(size) % 3))


def run(vector_min, fr, cash=1_000_000.0, gross=0.95, short=True, scheme=None, checksubmit=True):
    counts = {"accept": 0, "fill": 0}
    acc, fil = BackBroker._tw_accept_vector, BackBroker._tw_fill_vector

    def count_accept(self):
        r = acc(self)
        counts["accept"] += r
        return r

    def count_fill(self, has_bar):
        r = fil(self, has_bar)
        counts["fill"] += r
        return r

    old = BackBroker._tw_vector_min
    BackBroker._tw_vector_min = vector_min
    tw_order._refs = itertools.count(1)       # order references from 1 in both runs
    BackBroker._tw_accept_vector, BackBroker._tw_fill_vector = count_accept, count_fill
    try:
        engine = tw.Engine(stdstats=False)
        for i, f in enumerate(fr):
            engine.adddata(tw.feeds.PandasData(dataname=f), name=f"S{i:02d}")
        engine.addstrategy(Recorder, gross=gross, short=short)
        engine.broker.set_cash(cash)
        engine.broker.set_checksubmit(checksubmit)
        if scheme == "perc":
            engine.broker.setcommission(commission=0.001)
        elif scheme == "fixed":
            engine.broker.setcommission(commission=0.01, commtype=CommInfoBase.COMM_FIXED, stocklike=True)
        elif scheme == "custom":
            engine.broker.addcommissioninfo(SpreadLike())
        strat = engine.run()[0]
    finally:
        BackBroker._tw_vector_min = old
        BackBroker._tw_accept_vector, BackBroker._tw_fill_vector = acc, fil
    broker = engine.broker
    pf = broker.portfolio
    state = {
        "log": strat.log,
        "cash": broker.getcash(),
        "value": broker.getvalue(),
        "positions": [(d._name, p.size, p.price) for d, p in broker.positions.items()],
        "fees": pf.total_fees.tolist(),
        "pnl": pf.realized_pnl.tolist(),
    }
    return state, counts


def same(a, b):
    # repr keeps every bit of a float and tells -0.0 from 0.0
    return repr(a) == repr(b)


@pytest.mark.parametrize("case", [
    {},
    {"scheme": "perc"},
    {"scheme": "fixed"},
    {"scheme": "custom"},
    {"short": False, "scheme": "perc"},
    {"gross": 1.6, "scheme": "perc"},                      # cash shortage: refusals, partial reversals
    {"gross": 1.6, "scheme": "custom", "checksubmit": False},
    {"gross": 3.0, "cash": 50_000.0},                      # refusals at acceptance and at the fill
    {"gross": 3.0, "cash": 50_000.0, "checksubmit": False},   # many refusals in a pass: the restart cap
])
def test_batch_path_matches_per_order_path(case):
    fr = frames()
    scalar, c0 = run(NEVER, fr, **case)
    vector, c1 = run(ALWAYS, fr, **case)
    assert c0 == {"accept": 0, "fill": 0}
    assert c1["fill"] > 0 and (c1["accept"] > 0 or case.get("checksubmit") is False)
    assert len(scalar["log"]) == len(vector["log"])
    for x, y in zip(scalar["log"], vector["log"]):
        assert same(x, y), (x, y)
    for key in ("cash", "value", "positions", "fees", "pnl"):
        assert same(scalar[key], vector[key]), key


def test_default_threshold_takes_the_batch_path_on_a_wide_universe():
    fr = frames(n_sym=40, n_bars=30)
    default, counts = run(BackBroker._tw_vector_min, fr)
    scalar, _ = run(NEVER, fr)
    assert counts["accept"] > 0 and counts["fill"] > 0
    assert same(default["log"], scalar["log"]) and same(default["value"], scalar["value"])


def test_oracle_strategies_take_either_path_identically():
    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
    sys.path.insert(0, os.path.join(HERE, "oracle"))
    import run_oracle
    from strategies import build

    strategies = build(tw)
    fr = run_oracle.frames()
    old = BackBroker._tw_vector_min
    try:
        for name, cls in strategies.items():
            commission = 0.0005 if name == "commissioned" else None
            hook = getattr(cls, "oracle_frames", None)
            data = hook(fr) if hook else fr
            out = []
            for vector_min in (NEVER, ALWAYS):
                BackBroker._tw_vector_min = vector_min
                tw_order._refs = itertools.count(1)
                out.append(json.dumps(run_oracle.run_one(tw, cls, data, commission=commission), sort_keys=True))
            assert out[0] == out[1], name
    finally:
        BackBroker._tw_vector_min = old
