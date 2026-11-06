"""Engine cost per security-bar on a wide synthetic universe (spec/05 § 7.2).

    python tools/bench_engine.py --symbols 4000 --years 3 --repeat 3

Two strategies, both on the same seeded bars:

* ``idle``: ``next`` does nothing -- the loop, the feeds, the broker pass and the mark;
* ``rebalance``: equal weight through ``order_target_percent`` on every name at each month's
  first bar -- the order path (batch acceptance, fills, notifications) on top.

The figure is the wall time of ``Cerebro.run`` (feed start included, DataFrame building
excluded) divided by symbols × bars, best of ``--repeat``. The spec's budget is about
0.6 µs of engine time per security-bar.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
# TRADEWIND_SRC points at another checkout's ``src`` to time it on the same bars
sys.path.insert(0, os.environ.get("TRADEWIND_SRC") or os.path.join(os.path.dirname(HERE), "src"))

import tradewind as tw


def frames(n_sym: int, n_bars: int, seed: int = 5):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2018-01-01", periods=n_bars)
    rets = rng.normal(0.0003, 0.02, (n_bars, n_sym))
    close = 50 * np.exp(np.cumsum(rets, axis=0))
    openp = close * (1 + rng.normal(0, 0.003, (n_bars, n_sym)))
    high = np.maximum(openp, close) * 1.005
    low = np.minimum(openp, close) * 0.995
    # one name in ten lists up to 20 bars late, so the feeds are not all aligned
    late = np.where(rng.random(n_sym) < 0.1, rng.integers(1, 21, n_sym), 0)
    out = []
    for j in range(n_sym):
        s = late[j]
        out.append(pd.DataFrame({"open": openp[s:, j], "high": high[s:, j], "low": low[s:, j],
                                 "close": close[s:, j], "volume": 1e5}, index=index[s:]))
    return out


class Idle(tw.Strategy):
    def next(self):
        pass


class Rebalance(tw.Strategy):
    def __init__(self):
        self.month = None

    def next(self):
        m = self.datas[0].datetime.date(0).month
        if m != self.month:
            self.month = m
            w = 0.95 / len(self.datas)
            for d in self.datas:
                self.order_target_percent(d, target=w)


def run_once(strategy, fr):
    cerebro = tw.Cerebro(stdstats=False)
    for i, f in enumerate(fr):
        cerebro.adddata(tw.feeds.PandasData(dataname=f), name=f"S{i}")
    cerebro.addstrategy(strategy)
    cerebro.broker.set_cash(10_000_000)
    gc.collect()
    t0 = time.perf_counter()
    cerebro.run()
    elapsed = time.perf_counter() - t0
    return elapsed, cerebro.broker.getvalue()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", type=int, default=4000)
    p.add_argument("--years", type=float, default=3.0)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--json", default=None, help="write the figures there")
    args = p.parse_args()
    n_bars = round(252 * args.years)
    fr = frames(args.symbols, n_bars)
    cells = args.symbols * n_bars
    out = {"symbols": args.symbols, "bars": n_bars}
    for name, cls in (("idle", Idle), ("rebalance", Rebalance)):
        times, value = [], None
        for _ in range(args.repeat):
            t, value = run_once(cls, fr)
            times.append(t)
        best = min(times)
        out[name] = {"seconds": best, "us_per_security_bar": best / cells * 1e6, "final_value": value}
        print(f"{name:10s} {args.symbols} x {n_bars}: {best:7.2f} s, "
              f"{best / cells * 1e6:.3f} us per security-bar, final value {value!r}")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)


if __name__ == "__main__":
    main()
