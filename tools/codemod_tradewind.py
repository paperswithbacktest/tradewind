"""Rewrite the engine and toolbox imports of the corpus' ``strategy.py`` files.

A dry run by default: counts the files, says what each rule would touch, lists the
patterns no rule handles and the strategies that read a backtrader surface Tradewind
does not cover, and shows a unified diff for a few examples. Nothing is written
without ``--write``; running it again on its own output changes nothing (idempotent).

    python tools/codemod_tradewind.py ~/dev/pwb/papers/published            # dry run
    python tools/codemod_tradewind.py --git ~/dev/pwb/papers --ref origin/main \\
        --paths published held --report /tmp/codemod.json                    # no checkout
    python tools/codemod_tradewind.py ~/dev/pwb/papers/published --show 5   # five diffs
    python tools/codemod_tradewind.py ~/dev/pwb/papers/published --write    # apply

``--git`` reads the ``strategy.py`` blobs of ``--ref`` straight from the repository
(``git ls-tree`` + ``git cat-file --batch``): no checkout, no working tree touched,
and ``--write`` is refused in that mode.

Rules, each on whole import lines so comments and indentation survive:

* ``--engine`` -- where ``bt`` comes from after the rewrite:

  - ``tradewind`` (default): ``import backtrader as bt`` becomes ``import tradewind as
    bt``; ``from backtrader import X`` and ``from backtrader.utils.date import
    num2date`` become their ``tradewind`` forms; ``backtrader.indicators`` /
    ``.feeds`` / ``.analyzers`` keep their sub-module;
  - ``toolbox``: the engine is taken from the toolbox instead (``from pwb.backtesting
    import bt``; ``from pwb.backtesting.bt import indicators as btind``), so the file
    never names an engine and runs on whichever ``pwb.engine`` selected -- the same
    file on backtrader for the parity pass and on Tradewind after the switch;
  - ``off``: engine imports left alone.
* ``--toolbox NAME`` (default ``pwb``; ``--toolbox off`` keeps it): ``pwb_toolbox``
  becomes ``NAME`` in every import line **and** wherever the code names the package
  (``pwb_toolbox.datasets.get_pricing(...)`` after an unaliased import); names inside
  strings and comments are not rewritten, they are reported.

Reported, never rewritten (``unsupported``): a backtrader import no rule knows (an
unknown sub-module), a dynamic import (``importlib.import_module("backtrader")``,
``__import__``), ``backtrader`` or ``pwb_toolbox`` inside a string, and a rewrite
that would bind the toolbox's new name over a name the file already uses.

``--surface`` (on with ``--report``) reads every file's engine surface with
``tools/inventory.py`` and checks it against Tradewind, imported from this checkout:
``bt.<chain>`` and base classes resolved attribute by attribute on the module;
inherited ``self.<method>()`` calls against ``tradewind.Strategy`` (and the toolbox's
``BaseStrategy`` when ``--base-strategy`` names its source), ``self.broker.<x>``,
``order.<x>`` in ``notify_order``, ``trade.<x>`` in ``notify_trade``,
``self.data*.<x>`` and ``cerebro.<x>`` against live objects of a two-bar Tradewind
run. Every name Tradewind lacks is listed with the files that read it and its
counts in ``spec/inventory/backtrader-surface.json`` (``-`` when the inventory, made
on an older corpus, never saw it).
"""

from __future__ import annotations

import argparse
import ast
import difflib
import io
import json
import os
import re
import subprocess
import sys
import tokenize
import warnings
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
INVENTORY_JSON = os.path.join(REPO, "spec", "inventory", "backtrader-surface.json")

SUBMODULES = "indicators|feeds|analyzers"

ENGINE_PATTERNS = [
    # (name, regex, replacement) -- applied to one line; the first that matches wins.
    ("import-as", re.compile(r"^(\s*)import backtrader as (\w+)(\s*(#.*)?)$"),
     r"\1import tradewind as \2\3"),
    ("import-bare", re.compile(r"^(\s*)import backtrader(\s*(#.*)?)$"),
     r"\1import tradewind as backtrader\2"),
    ("import-submodule",
     re.compile(rf"^(\s*)import backtrader\.({SUBMODULES}) as (\w+)(\s*(#.*)?)$"),
     r"\1import tradewind.\2 as \3\4"),
    ("from-date", re.compile(r"^(\s*)from backtrader\.utils\.date import (.+)$"),
     r"\1from tradewind import \2"),
    ("from-submodule", re.compile(rf"^(\s*)from backtrader\.({SUBMODULES}) import (.+)$"),
     r"\1from tradewind.\2 import \3"),
    ("from-root", re.compile(r"^(\s*)from backtrader import (.+)$"),
     r"\1from tradewind import \2"),
]


def toolbox_engine_patterns(pkg: str):
    """``bt`` from the toolbox: ``pwb.backtesting.bt`` is the engine the toolbox chose
    (the toolbox files it in ``sys.modules`` with its sub-modules, so ``from
    pwb.backtesting.bt import indicators`` and ``from pwb.backtesting.bt.feeds import
    PandasData`` import)."""

    def import_as(m: re.Match) -> str:
        alias = m.group(2)
        suffix = "" if alias == "bt" else f" as {alias}"
        return f"{m.group(1)}from {pkg}.backtesting import bt{suffix}{m.group(3)}"

    return [
        ("import-as", re.compile(r"^(\s*)import backtrader as (\w+)(\s*(#.*)?)$"), import_as),
        ("import-bare", re.compile(r"^(\s*)import backtrader(\s*(#.*)?)$"),
         rf"\1from {pkg}.backtesting import bt as backtrader\2"),
        ("import-submodule",
         re.compile(rf"^(\s*)import backtrader\.({SUBMODULES}) as (\w+)(\s*(#.*)?)$"),
         rf"\1from {pkg}.backtesting.bt import \2 as \3\4"),
        ("from-date", re.compile(r"^(\s*)from backtrader\.utils\.date import (.+)$"),
         rf"\1from {pkg}.backtesting.bt import \2"),
        ("from-submodule", re.compile(rf"^(\s*)from backtrader\.({SUBMODULES}) import (.+)$"),
         rf"\1from {pkg}.backtesting.bt.\2 import \3"),
        ("from-root", re.compile(r"^(\s*)from backtrader import (.+)$"),
         rf"\1from {pkg}.backtesting.bt import \2"),
    ]


# Kept for callers of the first version: the toolbox rules under the old package name.
TOOLBOX_ENGINE_PATTERNS = toolbox_engine_patterns("pwb_toolbox")

ANY_BACKTRADER = re.compile(r"^\s*(import backtrader\b|from backtrader\b)")
ANY_TOOLBOX = re.compile(r"^\s*(import pwb_toolbox\b|from pwb_toolbox\b)")
# the names a file binds to the engine: ``import backtrader as bt``, ``import tradewind as
# bt``, ``from pwb_toolbox.backtesting import bt`` / ``from pwb.backtesting import bt``
ENGINE_ALIAS = re.compile(
    r"^\s*(?:import (?:backtrader|tradewind) as (\w+)"
    r"|from (?:pwb_toolbox|pwb)\.backtesting import bt(?: as (\w+))?"
    r"|import (backtrader))\s*(?:#.*)?$",
    re.MULTILINE,
)
DYNAMIC_IMPORT = re.compile(
    r"""(importlib\.import_module|__import__)\(\s*['"](backtrader|pwb_toolbox)\b"""
)


def engine_aliases(text: str) -> set[str]:
    out = set()
    for m in ENGINE_ALIAS.finditer(text):
        out.add(m.group(1) or m.group(3) or m.group(2) or "bt")
    return out


def toolbox_patterns(name: str):
    return [
        ("toolbox-import", re.compile(r"^(\s*)import pwb_toolbox(\.|\s|$)"), rf"\1import {name}\2"),
        ("toolbox-from", re.compile(r"^(\s*)from pwb_toolbox(\.|\s)"), rf"\1from {name}\2"),
    ]


def rewrite(text: str, rules, toolbox_rules=()) -> tuple[str, Counter, list[str]]:
    """The rewritten text, the count per rule, and the import lines no rule handled.

    On each line the first engine rule that matches applies, then the first toolbox
    rule, so ``from pwb_toolbox.backtesting import bt`` can be both."""
    hits: Counter = Counter()
    unhandled: list[str] = []
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        for group in (rules, toolbox_rules):
            for name, pattern, repl in group:
                if pattern.match(body):
                    body = pattern.sub(repl, body)
                    hits[name] += 1
                    break
        if ANY_BACKTRADER.match(body) and rules or ANY_TOOLBOX.match(body) and toolbox_rules:
            unhandled.append(body.strip())
        out.append(body + newline)
    return "".join(out), hits, unhandled


def _module_name_string(token: str, root: str) -> str | None:
    """The string's value when it is a dotted module name under ``root`` (a mock target,
    a ``sys.modules`` key, an ``import_module`` argument), else ``None``: prose that only
    mentions the name (a docstring, a message) needs no rewrite."""
    try:
        value = ast.literal_eval(token)
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, str) and re.fullmatch(rf"{root}(\.\w+)*", value):
        return value
    return None


def rename_code_names(text: str, old: str, new: str) -> tuple[str, int, list[str]]:
    """``old`` -> ``new`` where the code names it (NAME tokens), not in strings or
    comments; the strings holding a module name under ``old`` are returned to be
    reported."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text, 0, []
    lines = text.splitlines(keepends=True)
    edits = []  # (row, col) of NAME tokens to replace, left to right
    strings = []
    for tok in tokens:
        if tok.type == tokenize.NAME and tok.string == old:
            edits.append(tok.start)
        elif tok.type == tokenize.STRING and _module_name_string(tok.string, old):
            strings.append(tok.string)
    for row, col in reversed(edits):
        line = lines[row - 1]
        lines[row - 1] = line[:col] + new + line[col + len(old):]
    return "".join(lines), len(edits), strings


def _bindings_of(tree: ast.AST, name: str) -> list[str]:
    """How the file binds ``name`` other than by an unaliased ``import name.x``."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                if a.asname == name or (a.asname is None and isinstance(node, ast.ImportFrom)
                                        and a.name == name):
                    out.append(f"import ... as {name} (line {node.lineno})")
        elif isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store):
            out.append(f"assignment (line {node.lineno})")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == name:
            out.append(f"def/class (line {node.lineno})")
        elif isinstance(node, ast.arg) and node.arg == name:
            out.append(f"argument (line {node.lineno})")
    return out


def _binds_package(tree: ast.AST, name: str) -> bool:
    return any(
        isinstance(node, ast.Import)
        and any(a.asname is None and (a.name == name or a.name.startswith(name + "."))
                for a in node.names)
        for node in ast.walk(tree)
    )


def transform(text: str, engine_rules, toolbox: str | None):
    """Rewrite one file. Returns (new text, rule hits, unsupported patterns)."""
    toolbox_rules = toolbox_patterns(toolbox) if toolbox else []
    after, hits, unhandled = rewrite(text, engine_rules, toolbox_rules)
    unsupported = [f"import line: {line}" for line in unhandled]
    if toolbox:
        renamed, n, strings = rename_code_names(after, "pwb_toolbox", toolbox)
        if n:
            hits["toolbox-code-name"] += n
            after = renamed
        unsupported += [f"module name in a string: {s}" for s in strings]
    for m in DYNAMIC_IMPORT.finditer(after):
        unsupported.append(f"dynamic import: {m.group(0)}...")
    # ``backtrader`` named in a string (a mock target, a sys.modules key): left alone
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(after).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        tokens = []
    for tok in tokens:
        if engine_rules and tok.type == tokenize.STRING \
                and _module_name_string(tok.string, "backtrader"):
            unsupported.append(f"module name in a string: {tok.string}")
    if after != text:
        try:
            tree = ast.parse(after)
        except SyntaxError as exc:
            unsupported.append(f"rewrite does not parse: {exc.msg} (line {exc.lineno})")
        else:
            if toolbox and _binds_package(tree, toolbox):
                clash = _bindings_of(tree, toolbox)
                if clash:
                    unsupported.append(
                        f"name clash: an import now binds {toolbox!r}, which the file also "
                        f"binds by {', '.join(clash[:3])}"
                    )
    return after, hits, unsupported


# --------------------------------------------------------------------------------------
# reading the corpus


def find_strategies(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if "strategy.py" in filenames:
            found.append(os.path.join(dirpath, "strategy.py"))
    return sorted(found)


def read_git(repo: str, ref: str, paths: list[str]) -> list[tuple[str, str]]:
    """``(path, text)`` of every ``<path>/<slug>/strategy.py`` blob of ``ref``."""
    listing = subprocess.run(
        ["git", "-C", repo, "ls-tree", "-r", "--format=%(objectname) %(path)", ref, "--", *paths],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    wanted = []
    for row in listing:
        sha, path = row.split(" ", 1)
        parts = path.split("/")
        if len(parts) == 3 and parts[2] == "strategy.py":
            wanted.append((sha, path))
    proc = subprocess.run(
        ["git", "-C", repo, "cat-file", "--batch"],
        input="".join(f"{sha}\n" for sha, _ in wanted).encode(),
        capture_output=True, check=True,
    )
    data, pos, out = proc.stdout, 0, []
    for _, path in wanted:
        header_end = data.index(b"\n", pos)
        size = int(data[pos:header_end].split()[2])
        start = header_end + 1
        out.append((path, data[start:start + size].decode("utf-8", errors="replace")))
        pos = start + size + 1
    return out


# --------------------------------------------------------------------------------------
# surface coverage


def _probe_objects():
    """Live Tradewind objects from a two-bar run: strategy, broker, order, trade, data,
    cerebro -- the attributes they carry at run time, not only their classes'."""
    import numpy as np
    import pandas as pd

    import tradewind as bt

    seen: dict = {}

    class Probe(bt.Strategy):
        def next(self):
            seen.setdefault("data", self.datas[0])
            if len(self) == 1:
                self.buy(size=1)
            elif len(self) == 3:
                self.close()

        def notify_order(self, order):
            seen.setdefault("order", order)

        def notify_trade(self, trade):
            seen.setdefault("trade", trade)

    idx = pd.bdate_range("2020-01-01", periods=5)
    px = np.linspace(10.0, 11.0, len(idx))
    frame = pd.DataFrame({"open": px, "high": px + 0.5, "low": px - 0.5, "close": px,
                          "volume": 1000.0}, index=idx)
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.adddata(bt.feeds.PandasData(dataname=frame), name="X")
    cerebro.addstrategy(Probe)
    strat = cerebro.run()[0]
    seen.update(strategy=strat, broker=cerebro.broker, cerebro=cerebro)
    return bt, seen


def _resolve(obj, chain: list[str]) -> bool:
    for part in chain:
        if part in ("()", "[]"):
            return True  # a call or an index: the attribute before it exists, stop there
        try:
            obj = getattr(obj, part)
        except Exception:  # noqa: BLE001 - a property may raise outside a run
            return False
    return True


def default_base_strategy() -> str | None:
    """The installed toolbox's ``backtesting/base_strategy.py``, found without importing
    the toolbox (importing it would bind an engine)."""
    import importlib.util

    for package in ("pwb", "pwb_toolbox"):
        try:
            spec = importlib.util.find_spec(package)
        except (ImportError, ValueError):
            continue
        if spec and spec.origin:
            path = os.path.join(os.path.dirname(spec.origin), "backtesting", "base_strategy.py")
            if os.path.isfile(path):
                return path
    return None


def _methods_of_source(path: str | None) -> set[str]:
    if not path or not os.path.isfile(path):
        return set()
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    return {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef,
                                                              ast.AsyncFunctionDef))}


class Surface:
    """Which of a file's engine reads Tradewind does not cover."""

    def __init__(self, base_strategy_source: str | None = None):
        sys.path.insert(0, HERE)
        import inventory  # tools/inventory.py: the same extraction as spec/inventory

        self.inventory = inventory
        self.bt, self.obj = _probe_objects()
        self.toolbox_methods = _methods_of_source(base_strategy_source)
        self.cache: dict[str, bool] = {}

    def _covered(self, feature: str, own_names: set[str], strings: set[str]) -> bool | None:
        kind, _, rest = feature.partition(":")
        if not rest or "(kw=" in rest:
            return None
        chain = rest.split(".")
        if kind in ("bt", "bt_call"):
            return _resolve(self.bt, chain)
        if kind == "class_base" and rest.startswith("bt."):
            return _resolve(self.bt, chain[1:])
        if kind == "self.call":
            name = chain[0]
            return (name in own_names or name in self.toolbox_methods
                    or hasattr(self.obj["strategy"], name))
        if kind == "self.broker":
            return _resolve(self.obj["broker"], chain)
        if kind in ("order", "trade"):
            return _resolve(self.obj[kind], chain)
        if kind == "self.data":
            # a feed's own extra lines (``lines = ("pe",)``) are the file's, not the engine's;
            # ``self.datas.index(d)`` is the list's
            if chain[0] in strings or chain[0] in own_names or hasattr(list, chain[0]):
                return True
            return _resolve(self.obj["data"], chain)
        if kind == "cerebro":
            return _resolve(self.obj["cerebro"], chain)
        return None

    def missing(self, text: str) -> list[tuple[str, bool]]:
        """``(feature, guarded)`` for every engine read Tradewind lacks; ``guarded`` when
        every read of that name sits in a ``try`` body or under an ``if hasattr(...)`` /
        ``getattr(..., default)``: the strategy already copes with its absence."""
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        aliases = self.inventory.bt_aliases(tree)
        # the toolbox's engine handle counts as the engine too
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in (
                "pwb_toolbox.backtesting", "pwb.backtesting"
            ):
                aliases.update(a.asname or a.name for a in node.names if a.name == "bt")
        visitor = self.inventory.Visitor(aliases)
        visitor.visit(tree)
        own = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        assigned = {t.attr for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))
                    for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
                    if isinstance(t, ast.Attribute)}
        own |= assigned
        strings = {n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        # ``self.data = {...}``: the file's own container, not the engine's feed
        own_data = bool(assigned & {"data", "datas"})
        out = []
        for feature in visitor.feat:
            if feature.startswith("self.data") and own_data:
                continue
            local = feature.startswith(("self.call", "self.data"))
            if local or feature not in self.cache:
                verdict = self._covered(feature, own, strings)
                if not local:
                    self.cache[feature] = verdict
            else:
                verdict = self.cache[feature]
            if verdict is False:
                out.append((feature, _guarded(tree, feature)))
        return sorted(out)


def _guarded(tree: ast.AST, feature: str) -> bool:
    """Every read of the feature's last name is inside a ``try`` body or under a test that
    calls ``hasattr``/``getattr`` (or reads it through ``getattr(obj, name, default)``)."""
    kind, _, rest = feature.partition(":")
    name = rest.split(".")[-1]
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def owner(node: ast.Attribute) -> bool:
        # ``self.log(...)`` for self.call, ``self.broker.x`` for self.broker; ``np.log`` is
        # not a read of the strategy's ``log``
        value = node.value
        if kind == "self.call":
            return isinstance(value, ast.Name) and value.id == "self"
        if kind == "self.broker":
            return isinstance(value, ast.Attribute) and value.attr == "broker"
        return True

    reads = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == name and owner(n)]
    if not reads:
        return True  # read through getattr(obj, "name", ...) only

    def safe(node) -> bool:
        child = node
        while child in parents:
            parent = parents[child]
            if isinstance(parent, ast.Try) and child in parent.body:
                return True
            if isinstance(parent, (ast.If, ast.IfExp)) and child is not parent.test:
                test = ast.dump(parent.test)
                if "hasattr" in test or "getattr" in test:
                    return True
            child = parent
        return False

    return all(safe(n) for n in reads)


def load_inventory(path: str = INVENTORY_JSON) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh).get("corpus", {}).get("features", {})
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("root", nargs="?", help="a directory walked for strategy.py files, or one file")
    p.add_argument("--git", metavar="REPO", help="read the blobs of --ref from this repository")
    p.add_argument("--ref", default="origin/main", help="with --git (default origin/main)")
    p.add_argument("--paths", nargs="+", default=["published", "held"],
                   help="with --git: top-level directories (default published held)")
    p.add_argument("--engine", choices=["tradewind", "toolbox", "off"], default="tradewind",
                   help="where ``bt`` comes from after the rewrite (default tradewind)")
    p.add_argument("--toolbox", metavar="NAME", default="pwb",
                   help="new name of the pwb_toolbox package (default pwb; 'off' keeps it)")
    p.add_argument("--show", type=int, default=0, metavar="N", help="print N example diffs")
    p.add_argument("--surface", action="store_true",
                   help="list the engine reads Tradewind does not cover")
    p.add_argument("--base-strategy", metavar="PATH",
                   help="the toolbox's base_strategy.py: its methods count as covered "
                        "(default: the installed pwb, else pwb_toolbox)")
    p.add_argument("--report", metavar="JSON", help="write the full report here (implies --surface)")
    p.add_argument("--write", action="store_true", help="apply the rewrite (default: dry run)")
    args = p.parse_args(argv)
    warnings.simplefilter("ignore", SyntaxWarning)  # corpus regexes like "\\h" in plain strings

    toolbox = None if args.toolbox in (None, "", "off") else args.toolbox
    rules = []
    if args.engine == "tradewind":
        rules = ENGINE_PATTERNS
    elif args.engine == "toolbox":
        rules = toolbox_engine_patterns(toolbox or "pwb_toolbox")
    if not rules and not toolbox:
        p.error("nothing to do: --engine off and --toolbox off")
    if args.git:
        if args.write:
            p.error("--write needs a working tree; --git only reads blobs")
        files = read_git(args.git, args.ref, args.paths)
        source = f"{args.git}@{args.ref} ({' '.join(args.paths)})"
    elif args.root:
        files = []
        for path in find_strategies(args.root):
            with open(path, encoding="utf-8") as fh:
                files.append((path, fh.read()))
        source = args.root
    else:
        p.error("give a root, or --git REPO")

    surface = None
    if args.surface or args.report:
        base = args.base_strategy or default_base_strategy()
        print(f"toolbox BaseStrategy methods read from: {base or '(none found)'}")
        surface = Surface(base)
    changed: list[tuple[str, str, str]] = []
    per_rule: Counter = Counter()
    unsupported_by_file: dict[str, list[str]] = {}
    unsupported_kinds: Counter = Counter()
    missing_by_file: dict[str, list[str]] = {}
    missing_files: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    for path, before in files:
        after, hits, unsupported = transform(before, rules, toolbox)
        per_rule.update(hits)
        if unsupported:
            unsupported_by_file[path] = unsupported
            for item in unsupported:
                unsupported_kinds[item] += 1
        if after != before:
            changed.append((path, before, after))
            if args.write:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(after)
        if surface is not None:
            missing = surface.missing(before)
            if missing:
                missing_by_file[path] = [f"{f} (guarded)" if g else f for f, g in missing]
                for feature, guarded in missing:
                    missing_files[feature].append((path, guarded))

    mode = "written" if args.write else "would change"
    print(f"{len(files)} strategy.py scanned from {source}")
    print(f"{len(changed)} {mode}; {len(files) - len(changed)} untouched")
    for name, n in sorted(per_rule.items()):
        print(f"  {name:18s} {n} line(s)")
    print(f"{len(unsupported_by_file)} file(s) with a pattern no rule handles"
          f" ({sum(unsupported_kinds.values())} occurrence(s))")
    for item, n in unsupported_kinds.most_common(25):
        print(f"  {n:4d}  {item}")
    inventory = load_inventory()
    if surface is not None:
        unguarded = {path for rows in missing_files.values() for path, g in rows if not g}
        print(f"{len(missing_by_file)} file(s) read an engine surface Tradewind does not cover"
              f" ({len(missing_files)} distinct name(s)); {len(unguarded)} without a guard"
              f" (try / hasattr) around every read")
        print("  files  guarded  name                                     spec/inventory")
        for feature, rows in sorted(missing_files.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            inv = inventory.get(feature)
            seen = f"{inv['files']} file(s)" if inv else "- (not inventoried)"
            guarded = sum(g for _, g in rows)
            print(f"  {len(rows):5d}  {guarded:7d}  {feature:40s} {seen}")
    for path, before, after in changed[: args.show]:
        rel = os.path.relpath(path, args.root) if args.root and os.path.isdir(args.root) else path
        sys.stdout.writelines(
            difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile=f"a/{rel}", tofile=f"b/{rel}", n=1)
        )
        print()
    if args.report:
        report = {
            "source": source,
            "engine": args.engine,
            "toolbox": toolbox,
            "scanned": len(files),
            "changed": sorted(path for path, _, _ in changed),
            "rules": dict(sorted(per_rule.items())),
            "unsupported": unsupported_by_file,
            "unsupported_kinds": dict(unsupported_kinds.most_common()),
            "surface_missing": {
                feature: {"files": sorted(p for p, _ in rows),
                          "guarded": sorted(p for p, g in rows if g),
                          "inventory": inventory.get(feature)}
                for feature, rows in sorted(missing_files.items())
            },
            "surface_missing_unguarded_files": sorted(
                {p for rows in missing_files.values() for p, g in rows if not g}),
            "surface_missing_by_file": missing_by_file,
        }
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, sort_keys=True)
        print(f"report: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
