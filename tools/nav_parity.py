"""Bit-level parity of two Tradewind checkouts: every mark, fill and status, as hex floats.

    # the reference checkout (``main``), then the candidate, on the same seeded bars
    TRADEWIND_SRC=/path/to/main/src python tools/nav_parity.py --out /tmp/nav_main.json
    python tools/nav_parity.py --out /tmp/nav_new.json
    python tools/nav_parity.py --compare /tmp/nav_main.json /tmp/nav_new.json

A speed change must leave every float of every run unchanged: the portfolio value marked
at each slice (``float.hex``, no rounding), the final cash, and for every order its status
sequence, executed size, price and commission. The runs are the oracle probes of
``tests/oracle/strategies.py`` (on ``tools/run_oracle.py``'s bars) and the scenarios of
``tools/bench_engine.py`` on a small universe, which exercise the paths no probe does: a
wide book, strategies that listen to no notification, indicators on every feed. Synthetic
data only; a few seconds.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.environ.get("TRADEWIND_SRC") or os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests", "oracle"))
sys.path.insert(0, HERE)

import tradewind as tw


def _h(x):
    try:
        return float(x).hex()
    except (TypeError, ValueError):
        return repr(x)


def _record(engine):
    """Every mark of the run, in order, read from the broker right after phase 8."""
    broker = engine.broker
    marks = []
    mark = type(broker).mark

    def recording_mark(self, s):
        mark(self, s)
        marks.append(_h(self._value))

    broker.mark = recording_mark.__get__(broker)
    return marks


def _orders(broker):
    orders = sorted(broker.orders, key=lambda o: o.ref)
    base = orders[0].ref if orders else 0
    out = []
    for o in orders:
        ex = o.executed
        out.append([o.ref - base, getattr(o.data, "_name", ""), [int(s) for s in o._status_log],
                    _h(ex.size), _h(ex.price), _h(ex.comm), _h(ex.value)])
    return out


def run(engine, strategy_cls):
    marks = _record(engine)
    engine.addstrategy(strategy_cls)
    engine.run()
    broker = engine.broker
    return {"marks": marks, "cash": _h(broker.getcash()), "value": _h(broker.getvalue()),
            "orders": _orders(broker)}


def oracle_runs():
    import run_oracle
    from strategies import build

    fr = run_oracle.frames()
    out = {}
    for name, cls in build(tw).items():
        engine = tw.Engine(stdstats=False)
        for fname, f in fr.items():
            engine.adddata(tw.feeds.PandasData(dataname=f), name=fname)
        engine.broker.set_cash(100_000)
        if name == "commissioned":
            engine.broker.setcommission(commission=0.0005)
        hook = getattr(cls, "oracle_analyzers", None)
        for aname, ancls, kwargs in (hook(tw) if hook else []):
            engine.addanalyzer(ancls, _name=aname, **kwargs)
        out[f"oracle/{name}"] = run(engine, cls)
    return out


def bench_runs(symbols=60, bars=300):
    import bench_engine

    fr = bench_engine.frames(symbols, bars)
    out = {}
    for name, cls in bench_engine.SCENARIOS.items():
        for commission in (None, 0.001):
            engine = tw.Engine(stdstats=False)
            for i, f in enumerate(fr):
                engine.adddata(tw.feeds.PandasData(dataname=f), name=f"S{i}")
            engine.broker.set_cash(10_000_000)
            if commission is not None:
                engine.broker.setcommission(commission=commission)
            key = f"bench/{name}" + ("" if commission is None else "/comm")
            out[key] = run(engine, cls)
    return out


def compare(a_path, b_path) -> int:
    with open(a_path) as fh:
        a = json.load(fh)["runs"]
    with open(b_path) as fh:
        b = json.load(fh)["runs"]
    bad = 0
    for name in sorted(set(a) | set(b)):
        if name not in a or name not in b:
            print(f"{name}: missing on one side")
            bad += 1
            continue
        ra, rb = a[name], b[name]
        if ra == rb:
            continue
        bad += 1
        for key in ("marks", "orders"):
            for i, (x, y) in enumerate(zip(ra[key], rb[key])):
                if x != y:
                    print(f"{name}: {key}[{i}] {x} vs {y}")
                    break
            else:
                if len(ra[key]) != len(rb[key]):
                    print(f"{name}: {key} length {len(ra[key])} vs {len(rb[key])}")
        for key in ("cash", "value"):
            if ra[key] != rb[key]:
                print(f"{name}: {key} {ra[key]} vs {rb[key]}")
    n_marks = sum(len(r["marks"]) for r in a.values())
    n_orders = sum(len(r["orders"]) for r in a.values())
    print(f"{len(a)} runs, {n_marks} marks, {n_orders} orders: "
          + ("identical bit for bit" if not bad else f"{bad} runs differ"))
    return 1 if bad else 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out")
    p.add_argument("--compare", nargs=2, metavar=("A", "B"))
    p.add_argument("--symbols", type=int, default=60)
    p.add_argument("--bars", type=int, default=300)
    args = p.parse_args()
    if args.compare:
        sys.exit(compare(*args.compare))
    runs = oracle_runs()
    runs.update(bench_runs(args.symbols, args.bars))
    with open(args.out, "w") as fh:
        json.dump({"src": tw.__file__, "runs": runs}, fh)
    print(f"{len(runs)} runs written to {args.out}")


if __name__ == "__main__":
    main()
