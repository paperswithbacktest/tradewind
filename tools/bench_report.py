"""Markdown table of a reference benchmark: backtrader against Tradewind, µs per security-bar.

    python tools/bench_report.py bench/<run>/backtrader.json bench/<run>/tradewind.json

Exit 1 if a scenario's final value differs between the engines (relative 1e-9): the two runs
must be the same backtest before their times are compared.
"""

from __future__ import annotations

import json
import sys


def main():
    with open(sys.argv[1]) as fh:
        bt = json.load(fh)
    with open(sys.argv[2]) as fh:
        tw = json.load(fh)
    m, mt = bt["machine"], tw["machine"]
    print(f"{bt['symbols']} symbols x {bt['bars']} bars on {m['cpu']} ({m['os']}), Python {m['python']}; "
          f"backtrader {m['engine_version']} (numpy {m['numpy']}, pandas {m['pandas']}, best of {bt['repeat']}), "
          f"Tradewind {mt['engine_version']} (numpy {mt['numpy']}, pandas {mt['pandas']}, best of {tw['repeat']})\n")
    print("| scenario | backtrader µs | Tradewind µs | speed-up | same final value |")
    print("|---|---|---|---|---|")
    bad = []
    for k in ("idle", "reader", "rebalance", "notified", "indicators"):
        if k not in bt or k not in tw:
            continue
        a, b = bt[k], tw[k]
        va, vb = float(a["final_value"]), float(b["final_value"])
        same = abs(va - vb) <= 1e-9 * max(1.0, abs(va))
        if not same:
            bad.append(k)
        print(f"| `{k}` | {a['us_per_security_bar']:.2f} | {b['us_per_security_bar']:.3f} | "
              f"×{a['us_per_security_bar'] / b['us_per_security_bar']:.1f} | {'yes' if same else f'NO ({va!r} vs {vb!r})'} |")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
