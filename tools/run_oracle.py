"""Run the oracle strategies under one engine and dump NAVs, fills and statuses.

    # the oracle, in the environment where backtrader is installed
    python tools/run_oracle.py --engine backtrader --out /tmp/oracle_bt.json
    # the candidate
    python tools/run_oracle.py --engine tradewind --out /tmp/oracle_tw.json
    python tools/oracle_diff.py /tmp/oracle_bt.json /tmp/oracle_tw.json

Synthetic data only (seeded), so the comparison needs no market data and runs in CI.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests", "oracle"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))


def frames(n_sym=3, n_bars=120, seed=11):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2021-01-04", periods=n_bars)
    out = {}
    for i in range(n_sym):
        close = 80 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, n_bars)))
        openp = close * (1 + rng.normal(0, 0.004, n_bars))
        high = np.maximum(openp, close) * (1 + np.abs(rng.normal(0, 0.006, n_bars)))
        low = np.minimum(openp, close) * (1 - np.abs(rng.normal(0, 0.006, n_bars)))
        out[f"S{i}"] = pd.DataFrame(
            {"open": openp, "high": high, "low": low, "close": close, "volume": 1e5}, index=index
        )
    # One feed that lists 30 bars after the others, for the probes that read a feed before
    # its first bar or place orders on it.
    out[f"S{n_sym}"] = out["S0"].iloc[30:].copy() * 1.5
    return out


def run_one(bt, cls, fr, cash=100_000, commission=None):
    cerebro = bt.Cerebro(stdstats=False)
    for name, f in fr.items():
        cerebro.adddata(bt.feeds.PandasData(dataname=f), name=name)
    cerebro.addstrategy(cls)
    cerebro.broker.set_cash(cash)
    if commission is not None:
        cerebro.broker.setcommission(commission=commission)
    hook = getattr(cls, "oracle_analyzers", None)
    names = []
    for name, ancls, kwargs in (hook(bt) if hook else []):
        cerebro.addanalyzer(ancls, _name=name, **kwargs)
        names.append(name)
    strat = cerebro.run()[0]
    out = {
        "values": strat.values,
        "fills": strat.fills,
        "statuses": strat.statuses,
        "final_cash": round(cerebro.broker.getcash(), 6),
        "final_value": round(cerebro.broker.getvalue(), 6),
    }
    extra = dict(getattr(strat, "extra", None) or {})
    if names:
        analyses = {n: getattr(strat.analyzers, n).get_analysis() for n in names}
        extra["analyzers"] = {n: _plain(a) for n, a in analyses.items()}
        extra["analysis_types"] = {n: type(a).__name__ for n, a in analyses.items()}
    if extra:
        out["extra"] = extra
    return out


def _plain(x):
    """An analysis as plain JSON: nested dicts with string keys."""
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    return x


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--engine", choices=["backtrader", "tradewind"], required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--only", default=None)
    args = p.parse_args()
    bt = importlib.import_module(args.engine)
    from strategies import build

    strategies = build(bt)
    fr = frames()
    results = {}
    for name, cls in strategies.items():
        if args.only and name != args.only:
            continue
        commission = 0.0005 if name == "commissioned" else None
        results[name] = run_one(bt, cls, fr, commission=commission)
        print(f"{args.engine:10s} {name:24s} final_value={results[name]['final_value']:.2f} "
              f"fills={len(results[name]['fills'])}")
    with open(args.out, "w") as fh:
        json.dump({"engine": args.engine, "results": results}, fh)


if __name__ == "__main__":
    main()
