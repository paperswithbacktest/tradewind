"""Similarity review of Tradewind against a reference corpus, without ever showing the corpus.

Clean-room rule 4 (``CLEANROOM.md``) asks for a similarity review against backtrader's source
before every public release. This tool runs it while keeping the reviewer blind to that
source: the reference corpus is reduced to **hashes** as it is read, and nothing derived from
its text -- no line, no token, no identifier, no file name, no line number, no exception
message -- is ever stored, printed or written. The only outputs are scores and locations in
the **target** tree (Tradewind), whose code the reviewer may then read alone.

Two independent measures:

* **tokens** -- each file is tokenised with :mod:`tokenize`; comments, docstrings and line
  breaks inside brackets are dropped; every identifier becomes ``V``, every number ``N``,
  every string ``S``; keywords, operators and the statement structure (NEWLINE, INDENT,
  DEDENT) are kept. Every run of ``k`` normalised tokens is hashed. A target file's score is
  the share of its winnowed fingerprints (minimum hash of each window of ``w`` k-grams,
  Schleimer, Wilkerson & Aiken 2003) found among the reference k-grams; its matching spans
  are the maximal runs of consecutive target k-grams present in the reference, reported as
  line ranges of the target with their length in tokens.
* **ast** -- every statement of the target is serialised with identifiers, attribute names
  and constants erased (constants keep their type); a statement (compound statements carry
  their whole body) whose serialisation also occurs in the reference matches. A target
  file's score is the share of its AST nodes covered by maximal matching statements of at
  least ``--min-nodes`` nodes; whole functions whose bodies match are listed apart.

Usage::

    python -I tools/similarity.py --ref /path/to/reference --target src/tradewind \\
        [--base /path/to/unrelated/corpus] [--k 20] [--w 5] [--min-tokens 41] \\
        [--min-nodes 40] [--json report.json]

    python tools/similarity.py --self-test-control src/tradewind/broker.py

    # release gate: fails on any range of --min-tokens or more missing from the allow-list
    python -I tools/similarity.py --ref ... --base ... --target src/tradewind \\
        --allowlist spec/similarity-allowlist.json

Run with ``python -I`` when the reference is a downloaded package, so that nothing in it can
shadow a module this tool imports. Calibrate ``--min-tokens`` and ``--min-nodes`` against an
unrelated reference (the standard library) and a renamed copy (``--self-test-control``)
before reading the spans; ``spec/similarity-review-0.0.2.md`` records how.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import keyword
import os
import random
import sys
import tokenize
import warnings
from dataclasses import dataclass, field

_SKIP = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING, tokenize.ENDMARKER}
_FSTRING_START = getattr(tokenize, "FSTRING_START", None)
_FSTRING_END = getattr(tokenize, "FSTRING_END", None)
_STRUCT = {tokenize.NEWLINE: "<nl>", tokenize.INDENT: "<in>", tokenize.DEDENT: "<de>"}
_KEYWORDS = set(keyword.kwlist)
_STRUCT_NAMES = set(_STRUCT.values())
_IDENT_FIELDS = {"id", "arg", "attr", "name", "module", "asname"}


def _h(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


# ---------------------------------------------------------------- token measure


def normalised_tokens(source: str) -> list[tuple[str, int]]:
    """``(normalised token, source line)`` pairs of a module. Raises on untokenisable input;
    the caller decides what to say about it."""
    return [(t, line) for t, line, _ in _tokens(source, keep_text=False)]


def _tokens(source: str, keep_text: bool) -> list[tuple[str, int, str]]:
    """``(normalised token, source line, original text)``; the original text is kept only on
    the target side (``keep_text``), where it fingerprints a span for the allow-list."""
    raw = [t for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type not in _SKIP]
    out: list[tuple[str, int, str]] = []
    i, n = 0, len(raw)
    while i < n:
        tok = raw[i]
        if _FSTRING_START is not None and tok.type == _FSTRING_START:
            depth, first = 0, i
            while i < n:
                if raw[i].type == _FSTRING_START:
                    depth += 1
                elif raw[i].type == _FSTRING_END:
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            text = "".join(t.string for t in raw[first : i + 1]) if keep_text else ""
            out.append(("S", tok.start[0], text))
            i += 1
            continue
        text = tok.string if keep_text else ""
        if tok.type == tokenize.STRING:
            prev = out[-1][0] if out else "<nl>"
            nxt = raw[i + 1].type if i + 1 < n else tokenize.NEWLINE
            if prev in ("<nl>", "<in>", "<de>") and nxt == tokenize.NEWLINE:
                i += 2  # a bare string statement: a docstring, dropped with its NEWLINE
                continue
            if out and out[-1][0] == "S":
                if keep_text:  # implicit concatenation is one literal
                    out[-1] = (out[-1][0], out[-1][1], out[-1][2] + " " + text)
                i += 1
                continue
            out.append(("S", tok.start[0], text))
        elif tok.type == tokenize.NUMBER:
            out.append(("N", tok.start[0], text))
        elif tok.type == tokenize.NAME:
            out.append((tok.string if tok.string in _KEYWORDS else "V", tok.start[0], text))
        elif tok.type in _STRUCT:
            out.append((_STRUCT[tok.type], tok.start[0], text))
        else:
            out.append((tok.string, tok.start[0], text))
        i += 1
    return out


def text_digest(parts: list[str]) -> str:
    """Fingerprint of a **target** range for the allow-list: its exact tokens, names
    included, whitespace, comments, line structure and line numbers excluded, so that the
    entry survives a range moving inside its file but not an edit of the range itself."""
    return hashlib.blake2b("\x1f".join(parts).encode(), digest_size=8).hexdigest()


def kgram_hashes(tokens: list, k: int) -> list[int]:
    words = [t[0] for t in tokens]
    return [_h("\x1f".join(words[i : i + k])) for i in range(len(words) - k + 1)]


def winnow(hashes: list[int], w: int) -> set[int]:
    """Positions selected by winnowing (rightmost minimum of each window)."""
    if len(hashes) <= w:
        return {min(range(len(hashes)), key=lambda j: (hashes[j], -j))} if hashes else set()
    picked = set()
    for s in range(len(hashes) - w + 1):
        window = range(s, s + w)
        picked.add(min(window, key=lambda j: (hashes[j], -j)))
    return picked


# ------------------------------------------------------------------ AST measure


def _strip_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]


def _ser(node, counter: list[int]) -> str:
    if isinstance(node, ast.AST):
        if isinstance(node, (ast.expr_context,)):
            return ""
        counter[0] += 1
        if isinstance(node, ast.Constant):
            return f"C:{type(node.value).__name__}"
        parts = [type(node).__name__]
        for name, value in ast.iter_fields(node):
            if name in _IDENT_FIELDS and (isinstance(value, str) or value is None):
                parts.append("_")
            elif name == "type_comment":
                continue
            else:
                parts.append(_ser(value, counter))
        return "(" + " ".join(parts) + ")"
    if isinstance(node, list):
        return "[" + ",".join(_ser(v, counter) for v in node) + "]"
    if isinstance(node, str):
        return "_"
    return repr(type(node).__name__)


@dataclass
class StmtUnit:
    digest: int
    nodes: int
    first: int
    last: int
    is_function: bool
    name: str  # target-side only; never filled for the reference
    fingerprint: str = ""  # target-side only: the statement with its names, for the allow-list
    children: list[int] = field(default_factory=list)


def statement_units(source: str, keep_names: bool) -> list[StmtUnit]:
    """Every statement of a module, serialised with names and constants erased."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(source, filename="<input>")
    _strip_docstrings(tree)
    units: list[StmtUnit] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        counter = [0]
        text = _ser(node, counter)
        is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        units.append(
            StmtUnit(
                digest=_h(text),
                nodes=counter[0],
                first=node.lineno if keep_names else 0,
                last=(node.end_lineno or node.lineno) if keep_names else 0,
                is_function=is_fn,
                name=(node.name if is_fn and keep_names else ""),
                fingerprint=(text_digest([ast.dump(node)]) if keep_names else ""),
            )
        )
    return units


def module_nodes(source: str) -> int:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(source)
    _strip_docstrings(tree)
    return sum(1 for n in ast.walk(tree) if not isinstance(n, ast.expr_context))


# ------------------------------------------------------------- reference index


@dataclass
class ReferenceIndex:
    """Hashes only. Nothing here can be turned back into the reference's text."""

    k: int
    kgrams: set[int] = field(default_factory=set)
    stmts: set[int] = field(default_factory=set)
    files: int = 0
    tokens: int = 0
    skipped_tokenise: int = 0
    skipped_parse: int = 0
    base_kgrams_removed: int = 0
    base_stmts_removed: int = 0

    def discount(self, base: ReferenceIndex) -> None:
        """Drop what an unrelated corpus also contains: generic Python, not evidence."""
        before_k, before_s = len(self.kgrams), len(self.stmts)
        self.kgrams -= base.kgrams
        self.stmts -= base.stmts
        self.base_kgrams_removed = before_k - len(self.kgrams)
        self.base_stmts_removed = before_s - len(self.stmts)


def _python_files(root: str) -> list[str]:
    if os.path.isfile(root):
        return [root]
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git"))
        found += [os.path.join(base, f) for f in sorted(files) if f.endswith(".py")]
    return found


def _read(path: str) -> str | None:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def build_reference(roots: list[str], k: int, sources: list[str] | None = None) -> ReferenceIndex:
    """Index the reference corpus. Failures are counted, never described: an exception
    message from :mod:`tokenize` or :mod:`ast` can quote the line it failed on."""
    idx = ReferenceIndex(k=k)
    texts = list(sources or [])
    for root in roots:
        for path in _python_files(root):
            text = _read(path)
            if text is not None:
                texts.append(text)
    for text in texts:
        idx.files += 1
        try:
            toks = normalised_tokens(text)
        except Exception:  # noqa: BLE001 -- never echo it
            idx.skipped_tokenise += 1
            toks = []
        idx.tokens += len(toks)
        idx.kgrams.update(kgram_hashes(toks, k))
        try:
            for unit in statement_units(text, keep_names=False):
                idx.stmts.add(unit.digest)
        except Exception:  # noqa: BLE001 -- never echo it
            idx.skipped_parse += 1
    return idx


# ---------------------------------------------------------------- target side


@dataclass
class Span:
    first: int
    last: int
    tokens: int
    digest: str = ""


@dataclass
class FileReport:
    path: str
    tokens: int
    fingerprints: int
    fingerprint_hits: int
    spans: list[Span]
    ast_nodes: int
    ast_covered: int
    ast_matches: list[dict]
    function_matches: list[dict]
    key: str = ""  # path relative to the target root: what the allow-list names

    @property
    def token_score(self) -> float:
        return self.fingerprint_hits / self.fingerprints if self.fingerprints else 0.0

    @property
    def ast_score(self) -> float:
        return self.ast_covered / self.ast_nodes if self.ast_nodes else 0.0


def analyse_file(
    path: str, display: str, idx: ReferenceIndex, w: int, min_nodes: int
) -> FileReport:
    text = _read(path) or ""
    toks = _tokens(text, keep_text=True)
    k = idx.k
    hashes = kgram_hashes(toks, k)
    present = [h in idx.kgrams for h in hashes]
    picked = winnow(hashes, w)
    hits = sum(1 for j in picked if present[j])
    spans: list[Span] = []
    j = 0
    while j < len(present):
        if not present[j]:
            j += 1
            continue
        s = j
        while j + 1 < len(present) and present[j + 1]:
            j += 1
        last_tok = j + k - 1
        # NEWLINE / INDENT / DEDENT left out: a span may gain one at its edge when the code
        # next to it changes (a function added above a class ends with a DEDENT), and they
        # carry no text a reviewer reads
        digest = text_digest([t[2] for t in toks[s : last_tok + 1] if t[0] not in _STRUCT_NAMES])
        spans.append(Span(toks[s][1], toks[last_tok][1], last_tok - s + 1, digest))
        j += 1

    units = statement_units(text, keep_names=True)
    matched = [u for u in units if u.digest in idx.stmts and u.nodes >= min_nodes]
    # keep maximal matches only (a matching compound statement contains matching children)
    matched.sort(key=lambda u: (u.first, -u.last))
    maximal: list[StmtUnit] = []
    for u in matched:
        if maximal and u.first >= maximal[-1].first and u.last <= maximal[-1].last:
            continue
        maximal.append(u)
    covered = sum(u.nodes for u in maximal)
    return FileReport(
        path=display,
        tokens=len(toks),
        fingerprints=len(picked),
        fingerprint_hits=hits,
        spans=spans,
        ast_nodes=module_nodes(text),
        ast_covered=covered,
        ast_matches=[
            {
                "lines": [u.first, u.last],
                "nodes": u.nodes,
                "function": u.name or None,
                "digest": u.fingerprint,
            }
            for u in maximal
        ],
        function_matches=[
            {"lines": [u.first, u.last], "nodes": u.nodes, "function": u.name}
            for u in units
            if u.is_function and u.digest in idx.stmts
        ],
    )


def analyse(target: str, idx: ReferenceIndex, w: int, min_nodes: int) -> list[FileReport]:
    reports = []
    root = target if os.path.isdir(target) else os.path.dirname(target)
    for path in _python_files(target):
        rep = analyse_file(path, os.path.relpath(path), idx, w, min_nodes)
        rep.key = os.path.relpath(path, root).replace(os.sep, "/")
        reports.append(rep)
    return reports


# ----------------------------------------------------------- positive control


def renamed_copy(source: str, seed: int = 0) -> str:
    """The same module with every non-keyword identifier renamed (consistently), every
    comment and docstring removed and every string literal changed: what a careless port
    with a find-and-replace pass would look like."""
    rng = random.Random(seed)
    names: dict[str, str] = {}
    keep = _KEYWORDS | {"__future__", "annotations"}
    toks: list[tokenize.TokenInfo] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        after_bang = bool(toks) and toks[-1].string == "!"  # an f-string conversion: !r, !s
        if tok.type == tokenize.NAME and tok.string not in keep and not after_bang:
            if tok.string not in names:
                names[tok.string] = f"x{rng.getrandbits(24):x}_{len(names)}"
            tok = tok._replace(string=names[tok.string])
        elif tok.type == tokenize.COMMENT:
            tok = tok._replace(string="#")
        elif tok.type == tokenize.STRING:
            tok = tok._replace(string=f'"s{rng.getrandbits(16)}"')
        toks.append(tok)
    return tokenize.untokenize(toks)


# ---------------------------------------------------------------------- output


def report_text(reports: list[FileReport], idx: ReferenceIndex, min_tokens: int) -> str:
    lines = [
        (
            f"reference: {idx.files} files, {idx.tokens} tokens, {len(idx.kgrams)} distinct "
            f"{idx.k}-grams, {len(idx.stmts)} distinct statements "
            f"(untokenisable {idx.skipped_tokenise}, unparsable {idx.skipped_parse}; "
            f"discounted as generic: {idx.base_kgrams_removed} k-grams, "
            f"{idx.base_stmts_removed} statements)"
        ),
        "",
        (
            f"{'target file':44s} {'tokens':>7s} {'fp':>6s} {'tok%':>6s} {'ast%':>6s} "
            f"{'longest':>7s} {'spans>=' + str(min_tokens):>10s}"
        ),
    ]
    tot_fp = tot_hit = tot_nodes = tot_cov = 0
    for r in sorted(reports, key=lambda r: -r.token_score):
        longest = max((s.tokens for s in r.spans), default=0)
        big = sum(1 for s in r.spans if s.tokens >= min_tokens)
        lines.append(
            f"{r.path:44s} {r.tokens:7d} {r.fingerprints:6d} {100 * r.token_score:6.1f} "
            f"{100 * r.ast_score:6.1f} {longest:7d} {big:10d}"
        )
        tot_fp += r.fingerprints
        tot_hit += r.fingerprint_hits
        tot_nodes += r.ast_nodes
        tot_cov += r.ast_covered
    lines.append(
        f"{'ALL':44s} {sum(r.tokens for r in reports):7d} {tot_fp:6d} "
        f"{100 * tot_hit / max(tot_fp, 1):6.1f} {100 * tot_cov / max(tot_nodes, 1):6.1f}"
    )
    lines.append("")
    lines.append(f"token spans of at least {min_tokens} tokens (target locations only):")
    for r in reports:
        for s in r.spans:
            if s.tokens >= min_tokens:
                lines.append(f"  {r.path}:{s.first}-{s.last}  {s.tokens} tokens")
    lines.append("")
    lines.append("AST statements matched (maximal, target locations only):")
    for r in reports:
        for m in r.ast_matches:
            fn = f"  def {m['function']}" if m["function"] else ""
            lines.append(f"  {r.path}:{m['lines'][0]}-{m['lines'][1]}  {m['nodes']} nodes{fn}")
    return "\n".join(lines)


def report_json(reports: list[FileReport], idx: ReferenceIndex, args) -> dict:
    return {
        "parameters": {
            "k": args.k,
            "w": args.w,
            "min_tokens": args.min_tokens,
            "min_nodes": args.min_nodes,
        },
        "reference": {
            "files": idx.files,
            "tokens": idx.tokens,
            "kgrams": len(idx.kgrams),
            "statements": len(idx.stmts),
            "untokenisable": idx.skipped_tokenise,
            "unparsable": idx.skipped_parse,
            "base_kgrams_removed": idx.base_kgrams_removed,
            "base_stmts_removed": idx.base_stmts_removed,
        },
        "files": [
            {
                "path": r.path,
                "tokens": r.tokens,
                "fingerprints": r.fingerprints,
                "fingerprint_hits": r.fingerprint_hits,
                "token_score": round(r.token_score, 4),
                "ast_nodes": r.ast_nodes,
                "ast_covered": r.ast_covered,
                "ast_score": round(r.ast_score, 4),
                "spans": [[s.first, s.last, s.tokens, s.digest] for s in r.spans],
                "ast_matches": r.ast_matches,
                "function_matches": r.function_matches,
            }
            for r in reports
        ],
    }


# ------------------------------------------------------------------------ gate


def findings(reports: list[FileReport], min_tokens: int) -> list[dict]:
    """Every range a review must classify, as target-side facts only: the token spans of at
    least ``min_tokens`` tokens and the maximal AST statements of at least ``--min-nodes``."""
    out = []
    for r in reports:
        for s in r.spans:
            if s.tokens >= min_tokens:
                out.append(
                    {"file": r.key, "kind": "tokens", "digest": s.digest, "size": s.tokens,
                     "lines": [s.first, s.last], "where": r.path}
                )
        for m in r.ast_matches:
            out.append(
                {"file": r.key, "kind": "ast", "digest": m["digest"], "size": m["nodes"],
                 "lines": list(m["lines"]), "where": r.path}
            )
    return out


def gate(reports: list[FileReport], idx: ReferenceIndex, args) -> tuple[int, str]:
    """Compare the ranges found with a versioned allow-list of ranges a human has reviewed.

    An entry is ``(file relative to the target root, kind, digest)``: the digest is taken on
    Tradewind's own tokens (:func:`text_digest`), so an accepted range may move inside its
    file, while any edit of it, any growth (a span only grows when code next to it also
    matches) or any new range fails the gate. Exit status 1 on a new range, on parameters
    that differ from the allow-list's or on an empty reference (a vacuous pass)."""
    with open(args.allowlist, encoding="utf-8") as fh:
        allow = json.load(fh)
    problems = []
    for name, value in allow.get("parameters", {}).items():
        if getattr(args, name, value) != value:
            problems.append(f"--{name.replace('_', '-')} {getattr(args, name)} differs from the "
                            f"allow-list's {value}")
    if idx.files == 0 or not idx.kgrams:
        problems.append("the reference is empty: nothing was compared")
    entries = {(e["file"], e["kind"], e["digest"]): e for e in allow.get("entries", [])}
    lines = ["", f"gate: ranges to classify against {args.allowlist} (target locations only)"]
    seen, new = set(), []
    for f in findings(reports, args.min_tokens):
        key = (f["file"], f["kind"], f["digest"])
        unit = "tokens" if f["kind"] == "tokens" else "nodes"
        where = f["file"] if f["where"].startswith("..") else f["where"]
        loc = f"{where}:{f['lines'][0]}-{f['lines'][1]}  {f['kind']} {f['size']} {unit}  {f['digest']}"
        if key in entries:
            seen.add(key)
            lines.append(f"  allowed  {loc}  [{entries[key].get('class', '?')}]")
        else:
            new.append(f)
            lines.append(f"  NEW      {loc}")
    for key in entries:
        if key not in seen:
            lines.append(f"  unused allow-list entry (range edited or removed): {' '.join(key)}")
    for p in problems:
        lines.append(f"  ERROR    {p}")
    if new:
        lines += [
            "",
            f"{len(new)} range(s) not in the allow-list. Read each one in Tradewind only, classify",
            "it (API, idiom, semantics, suspect) and, if it is not a suspect, add its entry to the",
            "allow-list in a pull request reviewed by a human, with a line in CLEANROOM.md",
            "(procedure: spec/similarity-allowlist.md). Candidate entries:",
        ]
        for f in new:
            cand = {k: f[k] for k in ("file", "kind", "digest", "size")}
            cand.update({"lines_at_review": f["lines"], "class": "TODO", "what": "TODO",
                         "accepted": "TODO", "review": "TODO"})
            lines.append("  " + json.dumps(cand, ensure_ascii=False) + ",")
    ok = not new and not problems
    summary = (
        f"gate: {'PASS' if ok else 'FAIL'} ({len(seen)} allowed, {len(new)} new, "
        f"{len(entries) - len(seen)} unused entries, {len(problems)} errors)"
    )
    lines += ["", summary]
    return (0 if ok else 1), "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", action="append", default=[], help="reference file or directory")
    ap.add_argument(
        "--base",
        action="append",
        default=[],
        help="unrelated corpus whose k-grams and statements are discounted as generic Python",
    )
    ap.add_argument("--target", help="target file or directory (Tradewind)")
    ap.add_argument("--k", type=int, default=20, help="k-gram length in normalised tokens")
    ap.add_argument("--w", type=int, default=5, help="winnowing window in k-grams")
    ap.add_argument("--min-tokens", type=int, default=41, help="span length worth reviewing")
    ap.add_argument("--min-nodes", type=int, default=40, help="statement size worth reviewing")
    ap.add_argument("--json", help="write the full report (target locations only) here")
    ap.add_argument(
        "--allowlist",
        metavar="FILE",
        help="gate mode: exit 1 on any reviewable range missing from this versioned allow-list",
    )
    ap.add_argument(
        "--self-test-control",
        metavar="FILE",
        help="positive control: index a renamed copy of FILE, then analyse FILE against it",
    )
    args = ap.parse_args(argv)

    if args.self_test_control:
        with open(args.self_test_control, encoding="utf-8") as fh:
            copy = renamed_copy(fh.read())
        idx = build_reference([], args.k, sources=[copy])
        path = args.self_test_control
        reports = [analyse_file(path, path, idx, args.w, args.min_nodes)]
    else:
        if not args.ref or not args.target:
            ap.error("--ref and --target are required")
        idx = build_reference(args.ref, args.k)
        if args.base:
            idx.discount(build_reference(args.base, args.k))
        reports = analyse(args.target, idx, args.w, args.min_nodes)
    print(report_text(reports, idx, args.min_tokens))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report_json(reports, idx, args), fh, indent=1)
    if args.allowlist:
        status, text = gate(reports, idx, args)
        print(text)
        return status
    return 0


if __name__ == "__main__":
    sys.exit(main())
