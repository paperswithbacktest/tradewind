"""Replay corpus strategies against the versioned reference set (the corpus oracle).

    # the CI subset, one process, Tradewind from this checkout
    python tools/oracle/run.py --subset ci --papers ~/dev/pwb/papers \\
        --python <env with pwb-toolbox 0.1.51> --tradewind-src src --out-dir /tmp/oracle_ci

    # nightly: every reference, on the data host (tools/oracle/nightly.sh)
    python tools/oracle/run.py --subset all --jobs 4 --timeout 4800 ...

The reference set is fetched from the Hub at the revision ``oracle/<version>.lock.json``
names, and its files are checked against the lock's sha256 (``--reference DIR`` reads a
local copy instead). For each strategy:

1. its file is taken from the papers repository at the tree the reference ran
   (``git archive``, nothing checked out), with the papers harness next to it;
2. ``measure_one.py`` runs it in ``--python`` (an environment holding the pwb-toolbox of the
   reference: ``tools/oracle/env/``), hash seed fixed, every dataset pinned to its revision at
   the reference's epoch (``pinned_hub.py``);
3. its NAV is compared bar by bar to the reference (same dates, relative ``--tol``).

Verdicts: ``identical``; ``divergent`` / ``error`` (``expected`` when the reference set
already records that verdict for Tradewind); ``leaked`` when a backtrader module was loaded in
a Tradewind run. Exit 1 if any verdict is unexpected. Results: ``results.jsonl`` and
``summary.json`` in ``--out-dir``; the run is resumable (slugs already in the JSONL are skipped).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DEFAULT_TOL, HERE, LOCK_DIR, SUBSET_DIR, compare, read_lines, sha256_file

CACHE = os.path.expanduser(os.environ.get("TW_ORACLE_CACHE", "~/.cache/tradewind-oracle"))
REF_FILES = ("nav.parquet", "manifest.parquet", "hub_commits.json", "stats.json")
CLEAR_ENV = ("PWB_API_KEY", "PYTHONPATH", "PWB_BACKTEST_ENGINE", "PWB_HF_OFFLINE", "HF_HUB_OFFLINE",
             "HF_DATASETS_OFFLINE", "TW_ORACLE_EPOCH")


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# --- the reference set ------------------------------------------------------------------

def load_lock(version: str) -> dict:
    with open(os.path.join(LOCK_DIR, f"{version}.lock.json")) as fh:
        return json.load(fh)


def fetch_reference(lock: dict) -> str:
    """Download the lock's files at its revision (cached) and check their sha256."""
    from huggingface_hub import hf_hub_download

    dest = os.path.join(CACHE, "reference", lock["version"])
    os.makedirs(dest, exist_ok=True)
    for name, meta in lock["files"].items():
        path = os.path.join(dest, name)
        if not (os.path.exists(path) and sha256_file(path) == meta["sha256"]):
            src = hf_hub_download(lock["repo"], f"{lock['version']}/{name}", repo_type="dataset",
                                  revision=lock["revision"])
            shutil.copyfile(src, path)
        got = sha256_file(path)
        if got != meta["sha256"]:
            raise SystemExit(f"{name}: sha256 {got} differs from the lock's {meta['sha256']}")
    return dest


def check_reference(ref_dir: str, lock: dict | None):
    if lock is None:
        return
    for name, meta in lock["files"].items():
        got = sha256_file(os.path.join(ref_dir, name))
        if got != meta["sha256"]:
            raise SystemExit(f"{ref_dir}/{name}: sha256 {got} differs from the lock's {meta['sha256']}")


class ReferenceNavs:
    """The reference NAV of one strategy at a time, read by row group (one per strategy)."""

    def __init__(self, ref_dir: str):
        import pyarrow.parquet as pq

        self.pf = pq.ParquetFile(os.path.join(ref_dir, "nav.parquet"))
        self.groups = collections.defaultdict(list)
        md = self.pf.metadata
        for i in range(md.num_row_groups):
            st = md.row_group(i).column(0).statistics
            if st is None or st.min != st.max:
                raise SystemExit("nav.parquet: expected one strategy per row group")
            self.groups[st.min].append(i)
        self.lock = __import__("threading").Lock()

    def get(self, slug: str):
        if slug not in self.groups:
            return None
        with self.lock:
            t = self.pf.read_row_groups(self.groups[slug], columns=["date", "value"])
        return [{"date": str(d), "value": v} for d, v in zip(t.column("date").to_pylist(), t.column("value").to_pylist())]


# --- the papers trees ---------------------------------------------------------------------

SKIP_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".md")


def tree_entries(papers: str, tree: str, paths: list) -> list:
    """``(blob id, path)`` of the files under ``paths`` at ``tree`` (no blob is read)."""
    out = subprocess.run(["git", "-C", papers, "ls-tree", "-r", tree, "--", *paths], capture_output=True,
                         text=True, check=True).stdout
    entries = []
    for line in out.splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, oid = meta.split()
        if kind == "blob" and not path.lower().endswith(SKIP_EXT):
            entries.append((oid, path, mode))
    return entries


def harness_paths(papers: str, tree: str) -> list:
    """The top-level files of the papers tree: the harness and what it reads."""
    out = subprocess.run(["git", "-C", papers, "ls-tree", tree], capture_output=True, text=True, check=True).stdout
    return [ln.split("\t", 1)[1] for ln in out.splitlines() if ln.split()[1] == "blob"]


def materialize(papers: str, tree: str, slugs: list) -> str:
    """``<cache>/papers/<tree>/`` holding the harness and ``published/<slug>/`` of each slug.

    Read with ``git ls-tree`` and one ``git cat-file --batch``: nothing is checked out, and in
    a partial clone (``--filter=blob:none``, as in CI) git fetches just these blobs, in one go.
    PDFs and images are skipped.
    """
    dest = os.path.join(CACHE, "papers", tree)
    os.makedirs(dest, exist_ok=True)
    paths = [] if os.path.exists(os.path.join(dest, "backtester.py")) else harness_paths(papers, tree)
    paths += [f"published/{s}" for s in slugs if not os.path.exists(os.path.join(dest, "published", s, "strategy.py"))]
    for i in range(0, len(paths), 200):
        entries = tree_entries(papers, tree, paths[i:i + 200])
        if not entries:
            continue
        data = subprocess.run(["git", "-C", papers, "cat-file", "--batch"], capture_output=True, check=True,
                              input="".join(f"{oid}\n" for oid, _, _ in entries).encode()).stdout
        k = 0
        for oid, path, mode in entries:
            nl = data.index(b"\n", k)
            size = int(data[k:nl].split()[2])
            blob = data[nl + 1: nl + 1 + size]
            k = nl + 1 + size + 1
            target = os.path.join(dest, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(blob)
    return dest


# --- one strategy ---------------------------------------------------------------------------

def run_one(row, args, ref_dir, papers_dir, nav_dir):
    slug = row["slug"]
    nav_out = os.path.join(nav_dir, slug + ".json")
    env = {k: v for k, v in os.environ.items() if k not in CLEAR_ENV}
    env.update(PYTHONHASHSEED=str(row["hash_seed"]), TQDM_DISABLE="1", MPLBACKEND="Agg",
               TW_ORACLE_EPOCH=row["data_epoch_utc"], TW_ORACLE_HUB_COMMITS=os.path.join(ref_dir, "hub_commits.json"))
    if args.tradewind_src:
        env["TRADEWIND_SRC"] = os.path.abspath(args.tradewind_src)
    cmd = [args.python, "-u", os.path.join(HERE, "measure_one.py"), f"published/{slug}/strategy.py",
           "--engine", args.engine, "--nav-out", nav_out]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=papers_dir, env=env, capture_output=True, text=True,
                           timeout=args.timeout, check=False)
        res = [ln for ln in p.stdout.splitlines() if ln.startswith("RESULT ")]
        rec = json.loads(res[-1][7:]) if res else {"status": "no-result", "rc": p.returncode,
                                                    "error": (p.stderr or "").strip().splitlines()[-1:]}
    except subprocess.TimeoutExpired:
        rec = {"status": "timeout", "error": f"timeout after {args.timeout}s"}
    rec["wall_seconds"] = round(time.time() - t0, 1)
    return rec, nav_out


def verdict(rec, cmpres, row, engine):
    if rec.get("status") != "ok":
        v = "error"
    elif engine == "tradewind" and rec.get("leaked_backtrader_modules"):
        return "leaked", False
    else:
        v = "identical" if cmpres.get("same") else "divergent"
    if v == "identical":
        return v, True
    # a reference set records the earlier Tradewind verdict of each strategy
    expected = engine == "tradewind" and row.get("tw_parity") == v and bool(row.get("tw_parity_on_this_measure"))
    return v, expected


def select(args, man):
    ref = man[man.role == "reference"]
    if args.subset == "all":
        slugs = list(ref.slug)
    else:
        path = args.subset if os.path.exists(args.subset) else os.path.join(SUBSET_DIR, f"{args.subset}.txt")
        slugs = read_lines(path)
        missing = sorted(set(slugs) - set(ref.slug))
        if missing:
            raise SystemExit(f"not references in this set: {missing[:5]}{'...' if len(missing) > 5 else ''}")
    if args.tree:
        slugs = [s for s in slugs if ref.set_index("slug").at[s, "papers_tree"].startswith(args.tree)]
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        slugs = [s for s in slugs if zlib.crc32(s.encode()) % n == k]
    if args.limit:
        slugs = slugs[: args.limit]
    return slugs


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--version", default="v1", help="reference set version (oracle/<version>.lock.json)")
    p.add_argument("--reference", default=None, help="a local copy of the set (checked against the lock)")
    p.add_argument("--subset", default="ci", help="ci, all, or a file of slugs")
    p.add_argument("--tree", default=None, help="keep the strategies measured on this papers tree")
    p.add_argument("--shard", default=None, help="K/N: the strategies whose crc32(slug) %% N == K (a nightly slice)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--papers", required=True, help="a clone of the papers repository holding the trees")
    p.add_argument("--python", default=sys.executable, help="the environment running the strategies")
    p.add_argument("--engine", choices=("tradewind", "backtrader"), default="tradewind")
    p.add_argument("--tradewind-src", default=None, help="Tradewind sources to put first on the path")
    p.add_argument("--tol", type=float, default=DEFAULT_TOL)
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--min-free-gb", type=float, default=10.0,
                   help="stop before a strategy when the Hub cache's disk has less free space")
    p.add_argument("--out-dir", required=True)
    args = p.parse_args()
    import pandas as pd

    lock = None
    if os.path.exists(os.path.join(LOCK_DIR, f"{args.version}.lock.json")):
        lock = load_lock(args.version)
    if args.reference:
        ref_dir = args.reference
        check_reference(ref_dir, lock)
    elif lock:
        ref_dir = fetch_reference(lock)
    else:
        raise SystemExit(f"no oracle/{args.version}.lock.json and no --reference")
    man = pd.read_parquet(os.path.join(ref_dir, "manifest.parquet"))
    rows = {r["slug"]: r for r in man.to_dict("records")}
    slugs = select(args, man)

    os.makedirs(args.out_dir, exist_ok=True)
    nav_dir = os.path.join(args.out_dir, f"nav_{args.engine}")
    results_path = os.path.join(args.out_dir, "results.jsonl")
    done = {}
    if os.path.exists(results_path):
        with open(results_path) as fh:
            for ln in fh:
                r = json.loads(ln)
                done[r["slug"]] = r
    todo = [s for s in slugs if s not in done]
    by_tree = collections.defaultdict(list)
    for s in todo:
        by_tree[rows[s]["papers_tree"]].append(s)
    trees = {t: materialize(args.papers, t, ss) for t, ss in by_tree.items()}
    refs = ReferenceNavs(ref_dir)
    log(f"{len(slugs)} strategies ({len(done)} already done), engine {args.engine}, jobs {args.jobs}, "
        f"tol {args.tol:g}, reference {args.version} ({ref_dir})")

    def disk_ok():
        from huggingface_hub.constants import HF_HUB_CACHE

        probe = HF_HUB_CACHE if os.path.exists(HF_HUB_CACHE) else os.path.expanduser("~")
        return shutil.disk_usage(probe).free / 1e9 >= args.min_free_gb

    def one(slug):
        row = rows[slug]
        if not disk_ok():
            log(f"STOP free disk under {args.min_free_gb:g} GB before {slug}; rerun to resume")
            return None
        rec, nav_out = run_one(row, args, ref_dir, trees[row["papers_tree"]], nav_dir)
        run_log = None
        if rec.get("status") == "ok":
            with open(nav_out) as fh:
                run_log = json.load(fh)["log"]
        c = compare(refs.get(slug), run_log, tol=args.tol) if run_log is not None else {}
        v, expected = verdict(rec, c, row, args.engine)
        out = {"slug": slug, "verdict": v, "expected": expected, "engine": args.engine, "cmp": c,
               "reference": {"tree": row["papers_tree"], "toolbox": row["toolbox_version"], "epoch": row["data_epoch_utc"],
                             "seconds": row["seconds"], "tw_parity": row["tw_parity"]},
               "run": {k: rec.get(k) for k in ("status", "error", "seconds", "wall_seconds", "toolbox_version",
                                               "tradewind_version", "tradewind_file", "leaked_backtrader_modules", "pinned", "unpinned")}}
        with open(results_path, "a") as fh:
            fh.write(json.dumps(out, default=str) + "\n")
        extra = ""
        if v == "divergent":
            extra = (f" first #{c.get('first_index')} {c.get('first_date')} max_rel {c.get('max_rel', float('nan')):.2e}"
                     if not c.get("dates_differ") else f" dates differ at #{c.get('first_index')} ({c.get('n_ref')} vs {c.get('n_run')} bars)")
        elif v == "error":
            extra = f" {rec.get('status')}: {rec.get('error')}"
        log(f"{v.upper():9s}{'' if v == 'identical' else (' (expected)' if expected else ' (NEW)')} {slug} "
            f"{rec.get('wall_seconds')}s{extra}")
        return out

    if args.jobs <= 1:
        for s in todo:
            out = one(s)
            if out is None:
                break
            done[s] = out
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(args.jobs) as ex:
            for out in ex.map(one, todo):
                if out is not None:
                    done[out["slug"]] = out

    res = [done[s] for s in slugs if s in done]
    summary = {"version": args.version, "engine": args.engine, "n": len(res), "selected": len(slugs),
               "verdicts": dict(collections.Counter(r["verdict"] for r in res)),
               "unexpected": sorted(r["slug"] for r in res if not r["expected"]),
               "wall_seconds": round(sum(r["run"].get("wall_seconds") or 0 for r in res), 1)}
    with open(os.path.join(args.out_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    log(f"SUMMARY {json.dumps(summary)}")
    sys.exit(1 if summary["unexpected"] or len(res) < len(slugs) else 0)


if __name__ == "__main__":
    main()
