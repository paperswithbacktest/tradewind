"""Engine cost per security-bar on a wide synthetic universe (spec/05 § 7.2).

    # the budget's case (spec/05 § 7.2): 4 000 names, about 3 years of daily bars
    python tools/bench_engine.py --symbols 4000 --years 3 --repeat 3 --json /tmp/bench_4000.json
    # the small case used while optimising (a few seconds)
    python tools/bench_engine.py --symbols 200 --bars 500

Scenarios, all on the same seeded bars (``--scenarios`` picks some):

* ``idle``: ``next`` does nothing -- the loop, the feeds, the broker pass and the mark;
* ``rebalance``: equal weight through ``order_target_percent`` on every name at each month's
  first bar -- the order path (batch acceptance, fills) on top;
* ``notified``: ``rebalance`` with ``notify_order`` and ``notify_trade`` defined, so every
  notification is built and delivered;
* ``reader``: ``next`` reads ``len(d)``, ``d.close[0]`` and ``d.close[-1]`` of every feed --
  what most strategies do on every bar;
* ``indicators``: an SMA(20) of every feed built in ``__init__``, read on every bar, and a
  weekly rotation into the names furthest above their average.

The figure is the wall time of ``Engine.run`` (feed start included, DataFrame building
excluded) divided by symbols × bars, best of ``--repeat``. The spec's budget is 3 µs per
security-bar on 4 000 names, about 0.6 µs of it for the engine.

``--engine backtrader`` times the reference engine on the same bars and the same strategy
classes, written against the compatible API (``Cerebro``, ``setcash``): run it **in the
oracle's own environment** (backtrader installed there, never in Tradewind's), for the
reference benchmark of spec/03 (``tools/bench_reference.sh``). Each scenario's final value
is recorded, so the two engines' runs can be checked equal as well as timed.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = "backtrader" if "--engine=backtrader" in sys.argv or (
    "--engine" in sys.argv[:-1] and sys.argv[sys.argv.index("--engine") + 1] == "backtrader") else "tradewind"
if ENGINE == "backtrader":
    # the reference engine, as a black box, in its own environment (clean-room rule 2)
    import backtrader as tw
else:
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


class Notified(Rebalance):
    def __init__(self):
        super().__init__()
        self.n_orders = 0
        self.n_trades = 0

    def notify_order(self, order):
        self.n_orders += 1

    def notify_trade(self, trade):
        self.n_trades += 1


class Reader(tw.Strategy):
    def next(self):
        total = 0.0
        for d in self.datas:
            if len(d) > 1:
                total += d.close[0] - d.close[-1]
        self.total = total


class Indicators(tw.Strategy):
    def __init__(self):
        self.sma = {d: tw.indicators.SMA(d, period=20) for d in self.datas}
        self.week = None

    def next(self):
        week = self.datetime.date(0).isocalendar()[1]
        scores = []
        for d in self.datas:
            m = self.sma[d][0]
            if not math.isnan(m) and len(d):
                scores.append((d.close[0] / m, d._name, d))
        if week == self.week or not scores:
            return
        self.week = week
        scores.sort(reverse=True)
        top = {id(d) for _, _, d in scores[: max(1, len(scores) // 10)]}
        w = 0.95 / max(1, len(scores) // 10)
        for _, _, d in scores:
            if id(d) in top:
                self.order_target_percent(d, target=w)
            elif self.getposition(d).size:
                self.order_target_percent(d, target=0.0)


SCENARIOS = {"idle": Idle, "rebalance": Rebalance, "notified": Notified, "reader": Reader,
             "indicators": Indicators}


def run_once(strategy, fr, profile=None):
    engine = getattr(tw, "Engine", None) or tw.Cerebro
    engine = engine(stdstats=False)
    for i, f in enumerate(fr):
        engine.adddata(tw.feeds.PandasData(dataname=f), name=f"S{i}")
    engine.addstrategy(strategy)
    engine.broker.setcash(10_000_000)
    if profile is not None:
        engine.broker.set_profile(profile)
    gc.collect()
    t0 = time.perf_counter()
    engine.run()
    elapsed = time.perf_counter() - t0
    return elapsed, engine.broker.getvalue()


def machine():
    """What the figures were measured on: they only compare within one machine."""
    import platform
    import subprocess

    cpu = platform.processor() or platform.machine()
    try:
        if sys.platform == "darwin":
            cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                                 text=True, check=False).stdout.strip() or cpu
        elif os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo") as fh:
                cpu = next((ln.split(":", 1)[1].strip() for ln in fh if ln.startswith("model name")), cpu)
    except OSError:
        pass
    try:
        version = tw.__version__
    except AttributeError:
        version = "?"
    return {"cpu": cpu, "cores": os.cpu_count(), "os": platform.platform(), "python": platform.python_version(),
            "numpy": np.__version__, "pandas": pd.__version__, "engine_version": str(version)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", type=int, default=4000)
    p.add_argument("--years", type=float, default=3.0)
    p.add_argument("--bars", type=int, default=None, help="bars per symbol (overrides --years)")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--scenarios", default=",".join(SCENARIOS), help="comma-separated, of: "
                   + ", ".join(SCENARIOS))
    p.add_argument("--json", default=None, help="write the figures there")
    p.add_argument("--profile", default=None, help="a model profile for the broker (``realistic``); "
                   "the default is the oracle's")
    p.add_argument("--engine", choices=("tradewind", "backtrader"), default="tradewind",
                   help="backtrader: the reference engine, in the oracle's environment only")
    args = p.parse_args()
    n_bars = args.bars or round(252 * args.years)
    fr = frames(args.symbols, n_bars)
    cells = args.symbols * n_bars
    out = {"symbols": args.symbols, "bars": n_bars, "engine": ENGINE, "repeat": args.repeat,
           "machine": machine()}
    for name in args.scenarios.split(","):
        cls = SCENARIOS[name]
        times, value = [], None
        for _ in range(args.repeat):
            t, value = run_once(cls, fr, args.profile)
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
