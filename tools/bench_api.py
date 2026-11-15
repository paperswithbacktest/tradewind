"""Cost of the calls a strategy makes on the engine, per call (ns), on a synthetic universe.

    python tools/bench_api.py --symbols 500 --years 4

The engine loop itself is timed by ``tools/bench_engine.py``; this one times what a corpus
``next()`` does through the API: reading lines (``close[0]``, ``close[-k]``, a window read
value by value, ``get(size=)``), dates, positions, the portfolio value and the order helpers.
Each case is a strategy whose ``next`` repeats one call; the figure is the run's wall time
minus the idle run's, divided by the number of calls, best of ``--repeat``.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("TRADEWIND_SRC") or os.path.join(os.path.dirname(HERE), "src"))

from bench_engine import frames

import tradewind as tw

WINDOW = 60


class Idle(tw.Strategy):
    calls_per_bar_per_data = 0

    def next(self):
        pass


def case(name, per_data, body):
    """A strategy class whose ``next`` runs ``body(self, d)`` on every feed."""

    def next(self):
        if len(self) <= WINDOW + 25:
            return
        for d in self.datas:
            body(self, d)

    return type(name, (tw.Strategy,), {"next": next, "calls_per_bar_per_data": per_data})


def _window(self, d):
    c = d.close
    return [c[-i] for i in range(WINDOW, 0, -1)]


CASES = [
    case("close[0]", 1, lambda self, d: d.close[0]),
    case("close[-1]", 1, lambda self, d: d.close[-1]),
    case("data.close[0] x4 fields", 4, lambda self, d: (d.open[0], d.high[0], d.low[0], d.close[0])),
    case(f"window {WINDOW} by close[-i]", WINDOW, _window),
    case(f"close.get(size={WINDOW})", 1, lambda self, d: d.close.get(size=WINDOW)),
    case("len(data)", 1, lambda self, d: len(d)),
    case("datetime.date(0)", 1, lambda self, d: d.datetime.date(0)),
    case("datetime.datetime(0)", 1, lambda self, d: d.datetime.datetime(0)),
    case("getposition(d).size", 1, lambda self, d: self.getposition(d).size),
    case("broker.getvalue()", 1, lambda self, d: self.broker.getvalue()),
    case("broker.getcash()", 1, lambda self, d: self.broker.getcash()),
    case("data._name", 1, lambda self, d: d._name),
    case("order_target_percent (no-op target)", 1, lambda self, d: self.order_target_percent(d, target=0.0)),
]


def run_once(strategy, fr):
    cerebro = tw.Cerebro(stdstats=False)
    for i, f in enumerate(fr):
        cerebro.adddata(tw.feeds.PandasData(dataname=f), name=f"S{i}")
    cerebro.addstrategy(strategy)
    cerebro.broker.set_cash(10_000_000)
    gc.collect()
    t0 = time.perf_counter()
    cerebro.run()
    return time.perf_counter() - t0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbols", type=int, default=500)
    p.add_argument("--years", type=float, default=4.0)
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--json", default=None)
    args = p.parse_args()
    n_bars = round(252 * args.years)
    fr = frames(args.symbols, n_bars)
    bars = n_bars - (WINDOW + 25) - 20            # bars on which the body runs (late listings)
    idle = min(run_once(Idle, fr) for _ in range(args.repeat))
    out = {"symbols": args.symbols, "bars": n_bars, "idle_seconds": idle, "cases": {}}
    print(f"idle run {args.symbols} x {n_bars}: {idle:.2f} s")
    # the loop over the feeds and the call of the body, to subtract
    loop = case("loop", 1, lambda self, d: None)
    t_loop = min(run_once(loop, fr) for _ in range(args.repeat)) - idle
    for cls in CASES:
        t = min(run_once(cls, fr) for _ in range(args.repeat)) - idle - t_loop
        calls = bars * args.symbols * cls.calls_per_bar_per_data
        ns = t / calls * 1e9
        out["cases"][cls.__name__] = ns
        print(f"  {cls.__name__:38s} {ns:8.1f} ns per call")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)


if __name__ == "__main__":
    main()
