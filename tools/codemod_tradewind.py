"""Rewrite the engine imports of the corpus' ``strategy.py`` files for Tradewind.

A dry run by default: counts the files, says what each rule would touch, and
shows a unified diff for a few examples. Nothing is written without ``--write``.

    python tools/codemod_tradewind.py ~/dev/pwb/papers/published            # dry run
    python tools/codemod_tradewind.py ~/dev/pwb/papers/published --show 5   # five diffs
    python tools/codemod_tradewind.py ~/dev/pwb/papers/published --write    # apply

Rules, each on whole lines so comments and indentation survive:

* ``--engine`` (on by default): ``import backtrader as bt`` becomes
  ``import tradewind as bt``; ``from backtrader import X`` and
  ``from backtrader.utils.date import num2date`` become their ``tradewind``
  forms; ``backtrader.indicators`` / ``.feeds`` keep their sub-module. With
  ``--engine toolbox`` the name is taken from pwb-toolbox instead
  (``from pwb_toolbox.backtesting import bt``), so a strategy runs on whichever
  engine ``pwb_toolbox.engine`` selected and the strategy file never names one.
* ``--toolbox NAME`` (off unless given): ``pwb_toolbox`` becomes ``NAME`` in
  every import line, for the day the package is renamed (``pwb``).

A ``backtrader`` sub-module the rules do not know is left untouched and listed,
so nothing is rewritten into an import that would fail. With ``--surface`` the
``bt.<name>`` attributes the strategies read are checked against what
``tradewind`` exports, when it is importable: the names it lacks are listed with
their counts, which is the to-do list before those files can run.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from collections import Counter

ENGINE_PATTERNS = [
    # (name, regex, replacement) -- applied to one line; the first that matches wins.
    ("import-as", re.compile(r"^(\s*)import backtrader as (\w+)(\s*(#.*)?)$"),
     r"\1import tradewind as \2\3"),
    ("import-bare", re.compile(r"^(\s*)import backtrader(\s*(#.*)?)$"),
     r"\1import tradewind as backtrader\2"),
    ("import-submodule", re.compile(r"^(\s*)import backtrader\.(indicators|feeds) as (\w+)(\s*(#.*)?)$"),
     r"\1import tradewind.\2 as \3\4"),
    ("from-date", re.compile(r"^(\s*)from backtrader\.utils\.date import (.+)$"),
     r"\1from tradewind import \2"),
    ("from-submodule", re.compile(r"^(\s*)from backtrader\.(indicators|feeds) import (.+)$"),
     r"\1from tradewind.\2 import \3"),
    ("from-root", re.compile(r"^(\s*)from backtrader import (.+)$"),
     r"\1from tradewind import \2"),
]


def _toolbox_import(m: re.Match) -> str:
    alias = m.group(2)
    suffix = "" if alias == "bt" else f" as {alias}"
    return f"{m.group(1)}from pwb_toolbox.backtesting import bt{suffix}{m.group(3)}"


TOOLBOX_ENGINE_PATTERNS = [
    ("import-as", re.compile(r"^(\s*)import backtrader as (\w+)(\s*(#.*)?)$"), _toolbox_import),
    ("import-bare", re.compile(r"^(\s*)import backtrader(\s*(#.*)?)$"),
     r"\1from pwb_toolbox.backtesting import bt as backtrader\2"),
    ("import-submodule", re.compile(r"^(\s*)import backtrader\.(indicators|feeds) as (\w+)(\s*(#.*)?)$"),
     r"\1from pwb_toolbox.backtesting.bt import \2 as \3\4"),
    ("from-date", re.compile(r"^(\s*)from backtrader\.utils\.date import (.+)$"),
     r"\1from pwb_toolbox.backtesting.bt import \2"),
] + ENGINE_PATTERNS[4:]
ANY_BACKTRADER = re.compile(r"^\s*(import backtrader\b|from backtrader\b)")
BT_ATTR = re.compile(r"\bbt\.([A-Za-z_][A-Za-z_0-9]*)")


def toolbox_patterns(name: str):
    return [
        ("toolbox-import", re.compile(r"^(\s*)import pwb_toolbox(\.|\s)"), rf"\1import {name}\2"),
        ("toolbox-from", re.compile(r"^(\s*)from pwb_toolbox(\.|\s)"), rf"\1from {name}\2"),
    ]


def rewrite(text: str, rules) -> tuple[str, Counter, list[str]]:
    """The rewritten text, the count per rule, and the backtrader lines left alone."""
    hits: Counter = Counter()
    unhandled: list[str] = []
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        done = False
        for name, pattern, repl in rules:
            if pattern.match(body):
                body = pattern.sub(repl, body)
                hits[name] += 1
                done = True
                break
        if not done and ANY_BACKTRADER.match(body):
            unhandled.append(body.strip())
        out.append(body + newline)
    return "".join(out), hits, unhandled


def find_strategies(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if "strategy.py" in filenames:
            found.append(os.path.join(dirpath, "strategy.py"))
    return sorted(found)


def surface(paths: list[str]) -> tuple[Counter, Counter]:
    """``bt.<name>`` reads across the files: those tradewind has, those it lacks."""
    try:
        import tradewind
    except ImportError:
        return Counter(), Counter({"(tradewind not importable)": 0})
    have, lack = Counter(), Counter()
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as fh:
            names = set(BT_ATTR.findall(fh.read()))
        for name in names:
            (have if hasattr(tradewind, name) else lack)[name] += 1
    return have, lack


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("root", help="a directory walked for strategy.py files, or one file")
    p.add_argument("--engine", choices=["tradewind", "toolbox", "off"], default="tradewind",
                   help="where ``bt`` comes from after the rewrite (default tradewind)")
    p.add_argument("--toolbox", metavar="NAME", default=None,
                   help="rename the pwb_toolbox package in imports (e.g. pwb); off by default")
    p.add_argument("--show", type=int, default=0, metavar="N", help="print N example diffs")
    p.add_argument("--surface", action="store_true",
                   help="list the bt.<name> attributes tradewind lacks")
    p.add_argument("--write", action="store_true", help="apply the rewrite (default: dry run)")
    args = p.parse_args(argv)

    rules = []
    if args.engine == "tradewind":
        rules += ENGINE_PATTERNS
    elif args.engine == "toolbox":
        rules += TOOLBOX_ENGINE_PATTERNS
    if args.toolbox:
        rules += toolbox_patterns(args.toolbox)
    if not rules:
        p.error("nothing to do: --engine off and no --toolbox")

    paths = find_strategies(args.root)
    changed: list[tuple[str, str, str]] = []
    per_rule: Counter = Counter()
    unhandled: Counter = Counter()
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            before = fh.read()
        after, hits, left = rewrite(before, rules)
        per_rule.update(hits)
        for line in left:
            unhandled[line] += 1
        if after != before:
            changed.append((path, before, after))
            if args.write:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(after)

    mode = "written" if args.write else "would change"
    print(f"{len(paths)} strategy.py scanned under {args.root}")
    print(f"{len(changed)} {mode}; {len(paths) - len(changed)} untouched")
    for name, n in sorted(per_rule.items()):
        print(f"  {name:16s} {n} line(s)")
    if unhandled:
        print(f"{sum(unhandled.values())} backtrader import line(s) left alone (no rule):")
        for line, n in unhandled.most_common():
            print(f"  {n:4d}  {line}")
    if args.surface:
        have, lack = surface(paths)
        print(f"bt.<name> reads: {len(have)} names tradewind exports, {len(lack)} it lacks")
        for name, n in lack.most_common():
            print(f"  missing bt.{name:24s} in {n} file(s)")
    for path, before, after in changed[: args.show]:
        rel = os.path.relpath(path, args.root) if os.path.isdir(args.root) else path
        sys.stdout.writelines(
            difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile=f"a/{rel}", tofile=f"b/{rel}", n=1)
        )
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
