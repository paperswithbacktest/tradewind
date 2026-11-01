r"""Static inventory of the backtrader surface a corpus of strategies uses.

Clean-room input number one: the engine is specified from what the strategies
*call*, read off their own source with ``ast``, never from backtrader's code.

For every ``strategy.py`` under the corpus roots (and every Python file of the
packages passed with ``--package``) it records:

* ``bt.<attr>`` and ``backtrader.<attr>`` chains (``bt.Strategy``,
  ``bt.indicators.SMA``, ``bt.Order.Market``, ``bt.feeds.PandasData`` ...);
* methods called on ``self`` inside a strategy class that the class does not
  define itself, i.e. inherited from the engine (``order_target_percent``,
  ``buy``, ``close``, ``getposition``, ``notify_order`` overrides ...);
* attribute chains on ``self.broker``, ``self.data*``, ``self.datas[...]``,
  ``self.p``/``self.params``, ``self.position``, ``order.*`` and ``trade.*``
  inside ``notify_*`` callbacks, ``cerebro.*`` and ``bt.Cerebro(...)`` kwargs;
* line indexing patterns: ``x.close[0]``, ``x.close[-k]`` (max lookback),
  ``x.datetime.date(0)``, ``.get(size=...)``;
* strategy ``params`` tuple keys, ``__init__`` presence, ``next``/``prenext``/
  ``nextstart``/``stop``/``start``/``notify_order``/``notify_trade``/
  ``notify_cashvalue``/``notify_fund``/``notify_timer`` overrides.

Output: a JSON with per-feature file counts and call counts, and a Markdown
table sorted by file count, written to ``spec/inventory/``.

    python tools/inventory.py --root ~/dev/pwb/papers --package ~/dev/pwb/pwb-toolbox/pwb_toolbox
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import re

STRATEGY_HOOKS = {
    "__init__", "start", "prenext", "nextstart", "next", "stop",
    "notify_order", "notify_trade", "notify_cashvalue", "notify_fund",
    "notify_timer", "notify_data", "notify_store",
}


def _chain(node):
    """``a.b.c`` -> ['a', 'b', 'c'] for Attribute/Name chains, else None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    if isinstance(node, ast.Subscript):
        inner = _chain(node.value)
        if inner is None:
            return None
        return inner + ["[]"] + parts[::-1]
    if isinstance(node, ast.Call):
        inner = _chain(node.func)
        if inner is None:
            return None
        return inner + ["()"] + parts[::-1]
    return None


class Visitor(ast.NodeVisitor):
    def __init__(self, bt_aliases):
        self.bt = bt_aliases
        self.feat = collections.Counter()      # feature -> occurrences in this file
        self.class_methods: dict[str, set] = {}
        self.cur_class = None
        self.cur_func = None
        self.max_lookback = 0
        self.params_keys = set()

    # -- classes -----------------------------------------------------------
    def visit_ClassDef(self, node):
        bases = [".".join(_chain(b) or ["?"]) for b in node.bases]
        for b in bases:
            root = b.split(".")[0]
            if root in self.bt:
                self.feat[f"class_base:bt.{'.'.join(b.split('.')[1:])}"] += 1
            elif b in ("BaseStrategy", "pwb_bt.BaseStrategy", "pwb.BaseStrategy"):
                self.feat["class_base:BaseStrategy"] += 1
        own = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.class_methods[node.name] = own
        for hook in own & STRATEGY_HOOKS:
            self.feat[f"hook:{hook}"] += 1
        for stmt in node.body:
            if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "params" for t in stmt.targets
            ):
                self.feat["strategy_params_declared"] += 1
                val = stmt.value
                items = val.elts if isinstance(val, (ast.Tuple, ast.List)) else (
                    val.keys if isinstance(val, ast.Dict) else []
                )
                for it in items:
                    if isinstance(it, (ast.Tuple, ast.List)) and it.elts and isinstance(it.elts[0], ast.Constant):
                        self.params_keys.add(str(it.elts[0].value))
                    elif isinstance(it, ast.Constant):
                        self.params_keys.add(str(it.value))
        prev = self.cur_class
        self.cur_class = node.name
        self.generic_visit(node)
        self.cur_class = prev

    def visit_FunctionDef(self, node):
        prev = self.cur_func
        self.cur_func = node.name
        self.generic_visit(node)
        self.cur_func = prev

    # -- attribute chains ---------------------------------------------------
    def visit_Attribute(self, node):
        chain = _chain(node)
        if chain:
            root = chain[0]
            if root in self.bt and len(chain) >= 2:
                self.feat["bt:" + ".".join(chain[1:4])] += 1
            elif root == "self" and self.cur_class and len(chain) >= 2:
                second = chain[1]
                if second in ("broker",):
                    self.feat["self.broker:" + ".".join(chain[2:3])] += 1
                elif second in ("p", "params"):
                    self.feat["self.params"] += 1
                elif second in ("position", "positions", "positionsbyname"):
                    self.feat["self." + ".".join(chain[1:3])] += 1
                elif second in ("data", "datas", "data0", "data1", "dnames", "getdatanames", "getdatabyname") or re.match(r"data\d+$", second):
                    rest = [c for c in chain[2:] if c != "[]"]
                    self.feat["self.data:" + ".".join(rest[:2])] += 1
                elif second in ("cerebro", "env", "analyzers", "observers", "stats", "lines", "l", "line", "orders", "_orders", "_name", "_trades"):
                    self.feat["self." + ".".join(chain[1:3])] += 1
            elif root in ("order", "o", "ord") and self.cur_func == "notify_order" and len(chain) >= 2:
                self.feat["order:" + ".".join(chain[1:3])] += 1
            elif root in ("trade", "t") and self.cur_func == "notify_trade" and len(chain) >= 2:
                self.feat["trade:" + ".".join(chain[1:3])] += 1
            elif root == "cerebro" and len(chain) >= 2:
                self.feat["cerebro:" + chain[1]] += 1
            elif root in ("data", "d", "dat", "feed") and len(chain) >= 2 and self.cur_class:
                self.feat["data_obj:" + ".".join(chain[1:3])] += 1
        self.generic_visit(node)

    # -- calls --------------------------------------------------------------
    def visit_Call(self, node):
        chain = _chain(node.func)
        if chain:
            if chain[0] == "self" and len(chain) == 2 and self.cur_class:
                own = self.class_methods.get(self.cur_class, set())
                if chain[1] not in own:
                    self.feat["self.call:" + chain[1]] += 1
                    for kw in node.keywords:
                        self.feat[f"self.call:{chain[1]}(kw={kw.arg})"] += 1
            if chain[0] in self.bt:
                self.feat["bt_call:" + ".".join(chain[1:4])] += 1
                if chain[1:2] == ["Cerebro"]:
                    for kw in node.keywords:
                        self.feat[f"cerebro_kw:{kw.arg}"] += 1
            if chain[0] == "cerebro":
                for kw in node.keywords:
                    self.feat[f"cerebro:{chain[1]}(kw={kw.arg})"] += 1
            if chain[-1] in ("date", "datetime", "time") and len(chain) >= 2 and chain[-2] == "datetime":
                self.feat["lines:datetime." + chain[-1] + "()"] += 1
            if chain[-1] == "get" and len(chain) >= 2 and chain[-2] in ("close", "open", "high", "low", "volume", "openinterest", "lines"):
                self.feat["lines:" + chain[-2] + ".get()"] += 1
                for kw in node.keywords:
                    self.feat[f"lines:get(kw={kw.arg})"] += 1
        self.generic_visit(node)

    # -- line indexing ------------------------------------------------------
    def visit_Subscript(self, node):
        chain = _chain(node.value)
        if chain and chain[-1] in ("close", "open", "high", "low", "volume", "openinterest", "datetime", "lines", "array") or (
            chain and len(chain) >= 2 and chain[-2] in ("lines", "l")
        ):
            idx = node.slice
            if isinstance(idx, ast.Constant) and isinstance(idx.value, int):
                k = idx.value
                self.feat["lines:index[0]" if k == 0 else ("lines:index[-k]" if k < 0 else "lines:index[+k]")] += 1
                self.max_lookback = max(self.max_lookback, -k)
            elif isinstance(idx, ast.UnaryOp) and isinstance(idx.op, ast.USub) and isinstance(idx.operand, ast.Constant):
                self.feat["lines:index[-k]"] += 1
                self.max_lookback = max(self.max_lookback, int(idx.operand.value))
            elif isinstance(idx, ast.Slice):
                self.feat["lines:slice"] += 1
            else:
                self.feat["lines:index[expr]"] += 1
        self.generic_visit(node)


def bt_aliases(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "backtrader":
                    names.add(a.asname or "backtrader")
                elif a.name.startswith("backtrader."):
                    names.add(a.asname or a.name.split(".")[-1])
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "backtrader":
            for a in node.names:
                names.add(a.asname or a.name)
    return names


def scan(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return None, f"syntax error: {exc}"
    aliases = bt_aliases(tree)
    v = Visitor(aliases)
    v.visit(tree)
    out = dict(v.feat)
    if v.max_lookback:
        out["lines:max_lookback"] = v.max_lookback
    for k in v.params_keys:
        out["param_key:" + k] = 1
    out["imports_backtrader"] = 1 if aliases else 0
    out["imports_pandas_in_file"] = int(bool(re.search(r"^\s*import pandas|^\s*from pandas", src, re.MULTILINE)))
    out["imports_numpy_in_file"] = int(bool(re.search(r"^\s*import numpy|^\s*from numpy", src, re.MULTILINE)))
    return out, None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", action="append", default=[], help="corpus root holding */strategy.py")
    p.add_argument("--package", action="append", default=[], help="a Python package to scan whole")
    p.add_argument("--out", default="spec/inventory")
    args = p.parse_args()

    files = []
    for root in args.root:
        for dirpath, _, names in os.walk(os.path.expanduser(root)):
            if "/.git" in dirpath:
                continue
            if "strategy.py" in names:
                files.append(("corpus", os.path.join(dirpath, "strategy.py")))
    for pkg in args.package:
        for dirpath, _, names in os.walk(os.path.expanduser(pkg)):
            for n in names:
                if n.endswith(".py"):
                    files.append(("package", os.path.join(dirpath, n)))

    per_group = {}
    for group, path in files:
        g = per_group.setdefault(group, {"files": 0, "errors": [], "file_count": collections.Counter(), "occurrences": collections.Counter(), "max_lookback": collections.Counter()})
        feats, err = scan(path)
        if err:
            g["errors"].append((path, err))
            continue
        g["files"] += 1
        for k, v in feats.items():
            if k == "lines:max_lookback":
                g["max_lookback"][v] += 1
                continue
            if v:
                g["file_count"][k] += 1
                g["occurrences"][k] += v

    os.makedirs(args.out, exist_ok=True)
    report = {}
    for group, g in per_group.items():
        report[group] = {
            "files": g["files"],
            "syntax_errors": len(g["errors"]),
            "features": {k: {"files": g["file_count"][k], "occurrences": g["occurrences"][k]} for k in sorted(g["file_count"])},
            "max_lookback_histogram": dict(sorted(g["max_lookback"].items())),
        }
    with open(os.path.join(args.out, "backtrader-surface.json"), "w") as fh:
        json.dump(report, fh, indent=1, sort_keys=True)

    lines = ["# backtrader surface used by the corpus\n",
             "Generated by `tools/inventory.py` from the strategies' own source (ast), never from backtrader.\n"]
    for group, g in per_group.items():
        n = g["files"]
        lines.append(f"\n## {group}: {n} files ({len(g['errors'])} unparsable)\n")
        lines.append("| feature | files | % files | occurrences |\n|---|---|---|---|")
        for k, c in g["file_count"].most_common():
            if k.startswith("param_key:"):
                continue
            lines.append(f"| `{k}` | {c} | {100*c/n:.1f} | {g['occurrences'][k]} |")
        if g["max_lookback"]:
            lines.append("\n### max lookback close[-k] per file\n")
            lines.append("| k | files |\n|---|---|")
            for k, c in sorted(g["max_lookback"].items()):
                lines.append(f"| {k} | {c} |")
        pk = [(k[len('param_key:'):], c) for k, c in g["file_count"].most_common() if k.startswith("param_key:")]
        if pk:
            lines.append("\n### strategy params keys (top 40)\n")
            lines.append("| key | files |\n|---|---|")
            for k, c in pk[:40]:
                lines.append(f"| `{k}` | {c} |")
    with open(os.path.join(args.out, "backtrader-surface.md"), "w") as fh:
        fh.write("\n".join(lines) + "\n")
    for group, g in per_group.items():
        print(f"{group}: {g['files']} files, {len(g['errors'])} unparsable, {len(g['file_count'])} distinct features")


if __name__ == "__main__":
    main()
