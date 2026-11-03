"""Compare two ``run_oracle.py`` dumps: NAV path, fills, statuses, per strategy."""

from __future__ import annotations

import json
import math
import sys


def _diff_extra(a, b, path, tol=1e-9):
    """The first path where two recorded structures differ (floats within ``tol``
    relative, NaN equal to NaN), or None."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k} (missing on one side)"
            where = _diff_extra(a[k], b[k], f"{path}.{k}", tol)
            if where:
                return where
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path} (length {len(a)} vs {len(b)})"
        for i, (x, y) in enumerate(zip(a, b)):
            where = _diff_extra(x, y, f"{path}[{i}]", tol)
            if where:
                return where
        return None
    if isinstance(a, float) or isinstance(b, float):
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
            if math.isnan(a) and math.isnan(b):
                return None
            if abs(a - b) <= tol * max(1.0, abs(a)):
                return None
        return f"{path}: {a!r} vs {b!r}"
    return None if a == b else f"{path}: {a!r} vs {b!r}"


def main(a_path, b_path, tol=1e-9):
    with open(a_path) as fh:
        a = json.load(fh)["results"]
    with open(b_path) as fh:
        b = json.load(fh)["results"]
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
        if [tuple(s[1:]) for s in ra["statuses"]] != [tuple(s[1:]) for s in rb["statuses"]]:
            n = min(len(ra["statuses"]), len(rb["statuses"]))
            k = next((i for i in range(n) if tuple(ra["statuses"][i][1:]) != tuple(rb["statuses"][i][1:])), n)
            issues.append(f"status/value/date sequence differs at #{k}: "
                          f"{ra['statuses'][k] if k < len(ra['statuses']) else None} vs "
                          f"{rb['statuses'][k] if k < len(rb['statuses']) else None} "
                          f"({len(ra['statuses'])} vs {len(rb['statuses'])})")
        if "extra" in ra or "extra" in rb:
            where = _diff_extra(ra.get("extra"), rb.get("extra"), "extra")
            if where:
                issues.append(f"recorded values differ at {where}")
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
