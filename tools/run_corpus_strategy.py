"""Run one corpus strategy through the pwb-toolbox harness on Tradewind and compare its
NAV with the reference produced by the oracle campaign.

The toolbox and the harness still ``import backtrader``: a module alias makes that name
resolve to Tradewind for this process only. Nothing of the reference engine is loaded.

    PWB_HF_OFFLINE=1 python tools/run_corpus_strategy.py <slug> [--nav-dir DIR] [--papers DIR]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
import types


def install_alias():
    import tradewind

    sys.modules["backtrader"] = tradewind
    utils = types.ModuleType("backtrader.utils")
    date = types.ModuleType("backtrader.utils.date")
    date.date2num, date.num2date, date.num2dt = tradewind.date2num, tradewind.num2date, tradewind.num2dt
    utils.date = date
    sys.modules["backtrader.utils"] = utils
    sys.modules["backtrader.utils.date"] = date
    sys.modules["backtrader.feeds"] = tradewind.feeds
    sys.modules["backtrader.indicators"] = tradewind.indicators
    return tradewind


def main():
    p = argparse.ArgumentParser()
    p.add_argument("slug")
    p.add_argument("--papers", default=os.environ.get("PAPERS_DIR", "/Users/edarchimbaud/dev/pwb/papers-window"))
    p.add_argument("--nav-dir", default=os.path.expanduser(
        "~/.claude/projects/-Users-edarchimbaud-dev-pwb/handover-scripts/nav_0138"))
    p.add_argument("--out", default=None)
    p.add_argument("--engine", choices=["tradewind", "backtrader"], default="tradewind",
                   help="backtrader: no alias, run the reference engine (in its own environment)")
    p.add_argument("--fills", default=None, help="dump every order notification to this JSON")
    args = p.parse_args()

    os.environ.setdefault("TQDM_DISABLE", "1")
    if args.engine == "tradewind":
        tradewind = install_alias()
    else:
        import backtrader as tradewind
    sys.path.insert(0, args.papers)
    os.chdir(args.papers)
    import matplotlib

    matplotlib.use("Agg")
    import backtester as harness  # the papers harness

    path = None
    for root in ("published", "strategies", "rejected", "."):
        cand = os.path.join(args.papers, root, args.slug, "strategy.py")
        if os.path.exists(cand):
            path = cand
            break
    if path is None:
        raise SystemExit(f"no strategy.py for {args.slug}")

    fills = []
    if args.fills:
        import pwb_toolbox.backtesting as pwb_bt

        original = pwb_bt.BaseStrategy.notify_order

        def recording(self, order):
            fills.append({
                "date": str(self.datas[0].datetime.date(0)), "ref": order.ref, "data": order.data._name,
                "status": order.getstatusname(), "size": float(order.size),
                "exec_size": float(order.executed.size), "exec_price": float(order.executed.price),
                "cash": round(float(self.broker.getcash()), 4), "value": round(float(self.broker.getvalue()), 4),
                "pos": float(self.getposition(order.data).size),
                "close_prev": float(order.data.close[-1]) if len(order.data) > 1 else None,
                "open_now": float(order.data.open[0]),
            })
            return original(self, order)

        pwb_bt.BaseStrategy.notify_order = recording

    t0 = time.time()
    buf = io.StringIO()
    with open(path, encoding="utf-8") as fh:
        code = fh.read()
    b = harness.Backtester()
    with contextlib.redirect_stdout(buf):
        b.backtest(code)
    log = b.strategy.log_data
    seconds = time.time() - t0
    print(f"{args.engine} {tradewind.__version__}: {len(log)} bars in {seconds:.1f} s, "
          f"final {log[-1]['value']:.2f}, sharpe {float(b.metrics.get('sharpeRatio', float('nan'))):.4f}")
    if args.fills:
        with open(args.fills, "w") as fh:
            json.dump(fills, fh)
        print(f"{len(fills)} order notifications -> {args.fills}")
    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"slug": args.slug, "log": [{"date": str(x["date"]), "value": float(x["value"])} for x in log]}, fh)

    ref_path = os.path.join(args.nav_dir, args.slug + ".json")
    if os.path.exists(ref_path):
        with open(ref_path) as fh:
            ref = json.load(fh)["log"]
        same_dates = len(ref) == len(log) and all(str(a["date"]) == str(x["date"]) for a, x in zip(ref, log))
        if not same_dates:
            print(f"DATES DIFFER: reference {len(ref)} bars {ref[0]['date']}..{ref[-1]['date']}, "
                  f"tradewind {len(log)} bars {log[0]['date']}..{log[-1]['date']}")
            n = min(len(ref), len(log))
            k = next((i for i in range(n) if str(ref[i]["date"]) != str(log[i]["date"])), n)
            print(f"  first date mismatch at #{k}: {ref[k]['date'] if k < len(ref) else None} vs {log[k]['date'] if k < len(log) else None}")
        else:
            scale = max(abs(a["value"]) for a in ref)
            errs = [abs(a["value"] - x["value"]) / max(1.0, scale) for a, x in zip(ref, log)]
            worst = max(errs)
            first = next((i for i, e in enumerate(errs) if e > 1e-9), None)
            if first is None:
                print(f"IDENTICAL to the oracle NAV on {len(ref)} bars (worst rel {worst:.2e})")
            else:
                print(f"DIFFERS from the oracle: first at #{first} {ref[first]['date']} "
                      f"ref {ref[first]['value']:.4f} vs {log[first]['value']:.4f}; worst rel {worst:.3e}; "
                      f"final ref {ref[-1]['value']:.2f} vs {log[-1]['value']:.2f}")
    text = buf.getvalue()
    for line in text.splitlines():
        if "[costs]" in line or "Era cut" in line or "Refused" in line:
            print("  harness:", line.strip()[:160])


if __name__ == "__main__":
    main()
