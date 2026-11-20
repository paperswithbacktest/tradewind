"""Choose the CI subset of the reference set: fast, deterministic, covering the corpus' idioms.

    python tools/oracle/select_subset.py --reference /tmp/oracle_v1/build --papers ~/dev/pwb/papers \\
        --n 40 --max-seconds 12 --out tools/oracle/subsets/ci.txt

Candidates: references Tradewind already reproduced on the very same measurement
(``tw_parity == identical``), measured on one papers tree (one environment), in at most
``--max-seconds`` on the reference machine, with no dataset republished during the run, no
non-positive NAV, no unseeded randomness, no options or intraday data and no import outside
``ALLOWED_IMPORTS``. Each idiom of ``IDIOMS`` found among the candidates is then covered at
least ``--per-idiom`` times (rarest idiom first, fastest strategy first), and the list is
filled up to ``--n`` with the fastest of the rest. The strategy text is read from git.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

IDIOMS = {
    "order_target_percent": r"\.order_target_percent\(",
    "order_target_size": r"\.order_target_size\(",
    "order_target_value": r"\.order_target_value\(",
    "buy_sell": r"\.(buy|sell)\(",
    "close_position": r"self\.close\(",
    "close_lag": r"\.close\[-\d+\]",
    "notify_order": r"def notify_order\(",
    "bt_indicators": r"bt\.(indicators|ind)\.",
    "indicator_subclass": r"\(bt\.Indicator\)",
    "limit_stop": r"exectype\s*=",
    "broker_value": r"broker\.get_?value\(",
    "add_cash": r"add_cash\(",
    "timer": r"add_timer\(",
    "stocks": r"Stocks-Daily-Price",
    "etfs": r"ETFs-Daily-Price",
    "forex": r"Forex-Daily-Price",
    "crypto": r"Cryptocurrencies-Daily-Price",
    "commodities": r"Commodities-Daily-Price",
    "bonds": r"Bonds-Daily-Price",
    "indices": r"Indices-Daily-Price",
    "fundamentals": r"Stocks-Quarterly-",
    "economic": r"CoreEconomicData|FRED",
    "pit_universe": r"sp500|liquid1000|universe",
}
EXCLUDE = r"Options-|1Min|Hourly|random\.|np\.random|default_rng"
ALLOWED_IMPORTS = {"numpy", "pandas", "scipy", "sklearn", "statsmodels", "math", "datetime", "collections",
                   "itertools", "functools", "typing", "warnings", "backtrader", "pwb_toolbox", "pypfopt",
                   "dataclasses", "enum", "calendar", "operator", "bisect", "heapq", "statistics", "os", "re",
                   "copy", "json", "__future__", "decimal", "dateutil"}


def imports(code: str) -> set:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(code)
    except SyntaxError:
        return {"<unparsable>"}
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module.split(".")[0])
    return out


def read_blobs(papers: str, specs: list) -> list:
    """Contents of ``<tree>:<path>`` for each spec, through one ``git cat-file --batch``."""
    p = subprocess.run(["git", "-C", papers, "cat-file", "--batch"], input="\n".join(specs).encode() + b"\n",
                       capture_output=True, check=True)
    data, out, i = p.stdout, [], 0
    for _ in specs:
        nl = data.index(b"\n", i)
        header = data[i:nl].split()
        if header[-1] == b"missing":
            out.append(None)
            i = nl + 1
            continue
        size = int(header[2])
        out.append(data[nl + 1: nl + 1 + size].decode("utf-8", "replace"))
        i = nl + 1 + size + 1
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reference", required=True)
    p.add_argument("--papers", required=True)
    p.add_argument("--tree", default="51945b907e5c6f34a0bcab3708141a17ebca83e8")
    p.add_argument("--n", type=int, default=40)
    p.add_argument("--per-idiom", type=int, default=2)
    p.add_argument("--max-seconds", type=float, default=12.0)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    import pandas as pd

    man = pd.read_parquet(os.path.join(args.reference, "manifest.parquet"))
    c = man[(man.role == "reference") & (man.tw_parity == "identical") & man.tw_parity_on_this_measure
            & (man.papers_tree == args.tree) & (man.seconds <= args.max_seconds)
            & (man.hub_moved_during_run == "[]") & (man.nav_nonpositive.astype(str) != "True")]
    c = c.sort_values(["seconds", "slug"])
    codes = read_blobs(args.papers, [f"{args.tree}:published/{s}/strategy.py" for s in c.slug])
    feats = {}
    for slug, code in zip(c.slug, codes):
        if code is None or re.search(EXCLUDE, code) or not imports(code) <= ALLOWED_IMPORTS:
            continue
        feats[slug] = {k for k, rx in IDIOMS.items() if re.search(rx, code)}
    secs = dict(zip(c.slug, c.seconds))
    pool = sorted(feats, key=lambda s: (secs[s], s))
    chosen = []
    counts = {k: sum(k in f for f in feats.values()) for k in IDIOMS}
    for idiom in sorted((k for k in IDIOMS if counts[k]), key=lambda k: (counts[k], k)):
        have = sum(idiom in feats[s] for s in chosen)
        for s in pool:
            if have >= args.per_idiom:
                break
            if s not in chosen and idiom in feats[s]:
                chosen.append(s)
                have += 1
    for s in pool:
        if len(chosen) >= args.n:
            break
        if s not in chosen:
            chosen.append(s)
    cover = {k: sum(k in feats[s] for s in chosen) for k in IDIOMS}
    with open(args.out, "w") as fh:
        fh.write(f"# CI subset of the corpus oracle: {len(chosen)} strategies, papers tree {args.tree[:10]},\n"
                 f"# reference time <= {args.max_seconds:g} s each (sum {sum(secs[s] for s in chosen):.0f} s on the\n"
                 f"# reference machines). Written by tools/oracle/select_subset.py; idioms covered:\n")
        fh.write("# " + ", ".join(f"{k} {v}" for k, v in cover.items()) + "\n")
        for s in chosen:
            fh.write(s + "\n")
    print(f"{len(c)} candidates by manifest, {len(feats)} after the text filters; chose {len(chosen)}, "
          f"reference seconds {sum(secs[s] for s in chosen):.0f}")
    print("idioms among candidates:", counts)
    print("idioms in the subset:   ", cover)


if __name__ == "__main__":
    main()
