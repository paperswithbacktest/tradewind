"""Compare two ``run_oracle.py`` dumps: NAV path, fills, statuses, per strategy."""

from __future__ import annotations

import json
import sys


def main(a_path, b_path, tol=1e-9):
    a = json.load(open(a_path))["results"]
    b = json.load(open(b_path))["results"]
    bad = 0
    for name in sorted(set(a) | set(b)):
        if name not in a or name not in b:
            print(f"{name}: missing on one side")
            bad += 1
            continue
        ra, rb = a[name], b[name]
        issues = []
        if len(ra["values"]) != len(rb["values"]):
            issues.append(f"nav length {len(ra['values'])} vs {len(rb['values'])}")
        else:
            worst = 0.0
            first_bad = None
            for (da, va), (db, vb) in zip(ra["values"], rb["values"]):
                if da != db:
                    issues.append(f"date mismatch {da} vs {db}")
                    break
                err = abs(va - vb) / max(1.0, abs(va))
                worst = max(worst, err)
                if err > tol and first_bad is None:
                    first_bad = (da, va, vb)
            if first_bad:
                issues.append(f"nav differs from {first_bad[0]}: {first_bad[1]} vs {first_bad[2]} (worst rel {worst:.2e})")
        if ra["fills"] != rb["fills"]:
            n = min(len(ra["fills"]), len(rb["fills"]))
            k = next((i for i in range(n) if ra["fills"][i] != rb["fills"][i]), n)
            issues.append(f"fills differ at #{k}: {ra['fills'][k] if k < len(ra['fills']) else None} vs "
                          f"{rb['fills'][k] if k < len(rb['fills']) else None} ({len(ra['fills'])} vs {len(rb['fills'])})")
        if [s[1] for s in ra["statuses"]] != [s[1] for s in rb["statuses"]]:
            issues.append(f"status sequence differs ({len(ra['statuses'])} vs {len(rb['statuses'])})")
        if issues:
            bad += 1
            print(f"FAIL {name}")
            for i in issues:
                print(f"     {i}")
        else:
            print(f"ok   {name}: {len(ra['values'])} bars, {len(ra['fills'])} fills, final {ra['final_value']}")
    print(f"\n{len(a) - bad}/{len(a)} strategies identical")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
