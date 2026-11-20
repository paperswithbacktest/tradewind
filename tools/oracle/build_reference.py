"""Build a versioned reference set (the corpus oracle) from a finished PWB measurement campaign.

    python tools/oracle/build_reference.py \\
        --campaign grand_recalc_final_20261118.parquet \\
        --hub-commits /tmp/oracle_v1/hub_commits.json \\
        --papers ~/dev/pwb/papers \\
        --replace-nav-dir /tmp/oracle_v1/host_bt/nav_night_parity \\
        --version v1 --out /tmp/oracle_v1/build

Nothing is re-run: the NAV paths the campaign wrote are packed as they are. Output:

* ``nav.parquet``: ``slug, date, value`` (float64, bit-exact copies of the campaign's JSON);
* ``manifest.parquet``: one row per strategy of the campaign -- whether it is a reference
  (``role``), what produced it (engine, pwb-toolbox, papers tree, strategy blob, hash seed,
  host, Python), when (UTC start and end), the data epoch and the datasets whose revision
  moved during the run, and the earlier Tradewind parity verdict;
* ``hub_commits.json``: the datasets' commit history, so the revision of any dataset at a
  run's epoch can be resolved (``common.HubHistory``);
* ``README.md``: the provenance, written from the same numbers.

``--replace-nav-dir``: rows whose campaign NAV was written by Tradewind (identical to the
oracle at 1e-12) take the oracle's own NAV from that directory instead, so every reference
is the reference engine's output; the 1e-12 identity is re-checked on the way.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HubHistory, compare, iso, sha256_file, started_at, to_utc

# what the campaign's environments held (checked on 2026-11-19 in each virtual environment by
# importing the module and printing its version -- the oracle's code is never read)
ENGINE = {"name": "backtrader", "distribution": "pwb-backtrader", "version": "1.9.78.125",
          "note": "the module `backtrader` of both campaign environments is the PWB fork 1.9.78.125"}
PYTHON = "3.13.12"


def blob_hashes(papers: str, tree: str) -> dict:
    out = subprocess.run(["git", "-C", papers, "ls-tree", "-r", tree, "--", "published/"],
                         capture_output=True, text=True, check=True).stdout
    h = {}
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        if path.endswith("/strategy.py"):
            h[path.split("/")[1]] = meta.split()[2]
    return h


def load_log(path):
    with open(path) as fh:
        return json.load(fh)["log"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--campaign", required=True, help="the campaign's merged table (parquet)")
    p.add_argument("--hub-commits", required=True)
    p.add_argument("--papers", required=True, help="a clone of the papers repository (read with git only)")
    p.add_argument("--replace-nav-dir", default=None)
    p.add_argument("--version", default="v1")
    p.add_argument("--label", default="grand recalcul du 18 nov. 2026")
    p.add_argument("--out", required=True)
    args = p.parse_args()
    import pandas as pd

    os.makedirs(args.out, exist_ok=True)
    camp = pd.read_parquet(args.campaign)
    hub = HubHistory.load(args.hub_commits)
    blobs = {t: blob_hashes(args.papers, t) for t in sorted(camp["tree"].dropna().unique())}

    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema([("slug", pa.string()), ("date", pa.date32()), ("value", pa.float64())])
    writer = pq.ParquetWriter(os.path.join(args.out, "nav.parquet"), schema, compression="zstd")
    n_bars_total = 0
    rows = []
    replaced = collections.Counter()
    for r in camp.sort_values("slug").itertuples(index=False):
        nav_path = r.nav_path if isinstance(r.nav_path, str) else None
        engine_of_nav = "backtrader"
        if isinstance(r.nav_kind, str) and r.nav_kind.startswith("tradewind"):
            engine_of_nav = "tradewind"
            alt = os.path.join(args.replace_nav_dir, r.slug + ".json") if args.replace_nav_dir else None
            if alt and os.path.exists(alt):
                c = compare(load_log(alt), load_log(nav_path), tol=1e-12)
                replaced["checked identical" if c["same"] else "NOT identical"] += 1
                if c["same"]:
                    nav_path, engine_of_nav = alt, "backtrader"
        ok = r.status == "ok" and bool(r.valid) and nav_path is not None and os.path.exists(nav_path)
        role = "reference" if ok and engine_of_nav == "backtrader" else "excluded"
        reason = None
        if not ok:
            reason = f"status {r.status}" if r.status != "ok" else ("not valid" if not r.valid else "no NAV")
        elif engine_of_nav != "backtrader":
            reason = "NAV written by Tradewind only"
        host = r.host if isinstance(r.host, str) else "mac"
        end = to_utc(r.measured_at, host) if isinstance(r.measured_at, str) and r.measured_at else None
        start = started_at(end, r.seconds) if end else None
        moved = hub.moved_between(start, end) if start else []
        n = None
        if role == "reference":
            log = load_log(nav_path)
            n = len(log)
            d = np.array([str(x["date"])[:10] for x in log], dtype="datetime64[D]")
            v = np.array([float(x["value"]) for x in log], dtype=np.float64)
            writer.write_table(pa.table({"slug": pa.array([r.slug] * n, pa.string()), "date": pa.array(d, pa.date32()),
                                         "value": pa.array(v, pa.float64())}, schema=schema))
            n_bars_total += n
        rows.append({
            "slug": r.slug, "role": role, "excluded_reason": reason, "status": r.status, "error": r.error or None,
            "engine": ENGINE["name"] if role == "reference" else None,
            "engine_distribution": f"{ENGINE['distribution']}=={ENGINE['version']}",
            "toolbox_version": r.toolbox_version, "papers_tree": r.tree,
            "strategy_blob": blobs.get(r.tree, {}).get(r.slug), "hash_seed": "0", "python": PYTHON,
            "host": host, "source": r.source, "started_at_utc": iso(start) if start else None,
            "measured_at_utc": iso(end) if end else None, "seconds": r.seconds, "max_rss_mb": r.max_rss_mb,
            "data_epoch_utc": iso(start) if start else None, "hub_moved_during_run": json.dumps(moved),
            "n_bars": n, "start": r.nav_start, "end": r.nav_end, "final_nav": r.final_nav,
            "nav_nonpositive": r.nav_nonpositive, "cost_mode": r.cost_mode,
            "tw_parity": r.parity_verdict if isinstance(r.parity_verdict, str) else "none",
            "tw_parity_source": r.parity_source,
            "tw_parity_on_this_measure": bool(r.parity_on_retained_measure), "tw_parity_max_rel": r.parity_max_rel,
            "nav_file": os.path.basename(os.path.dirname(nav_path)) if nav_path else None,
        })
    writer.close()
    man = pd.DataFrame(rows).sort_values("slug").reset_index(drop=True)
    man.to_parquet(os.path.join(args.out, "manifest.parquet"), compression="zstd", index=False)
    with open(args.hub_commits) as src, open(os.path.join(args.out, "hub_commits.json"), "w") as dst:
        dst.write(src.read())

    ref = man[man.role == "reference"]
    stats = {
        "version": args.version, "label": args.label, "built_at": iso(datetime.now(timezone.utc)),
        "engine": ENGINE, "python": PYTHON, "n_rows": len(man), "n_reference": len(ref),
        "n_excluded": int((man.role != "reference").sum()),
        "excluded_reasons": {str(k): int(v) for k, v in man[man.role != "reference"].excluded_reason.value_counts().items()},
        "n_bars": int(n_bars_total), "toolbox_versions": {str(k): int(v) for k, v in ref.toolbox_version.value_counts().items()},
        "papers_trees": {str(k): int(v) for k, v in ref.papers_tree.value_counts().items()},
        "hosts": {str(k): int(v) for k, v in ref.host.value_counts().items()},
        "tw_parity": {str(k): int(v) for k, v in ref.tw_parity.value_counts().items()},
        "measured_utc": [str(ref.started_at_utc.min()), str(ref.measured_at_utc.max())],
        "runs_with_hub_moves": int((ref.hub_moved_during_run != "[]").sum()),
        "tradewind_navs_replaced": dict(replaced),
    }
    with open(os.path.join(args.out, "stats.json"), "w") as fh:
        json.dump(stats, fh, indent=1)
    files = {f: {"sha256": sha256_file(os.path.join(args.out, f)), "bytes": os.path.getsize(os.path.join(args.out, f))}
             for f in ("nav.parquet", "manifest.parquet", "hub_commits.json", "stats.json")}
    with open(os.path.join(args.out, "files.json"), "w") as fh:
        json.dump(files, fh, indent=1)
    print(json.dumps(stats, indent=1))
    print(json.dumps(files, indent=1))


if __name__ == "__main__":
    main()
