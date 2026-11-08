"""``tools/similarity.py`` must find a renamed copy and never print the reference corpus.

The reference of a real review is code nobody on the project may read. These tests plant
canaries (identifiers, strings, comments, a path, a line that makes the parser warn, files
that fail to tokenise or to parse) in a synthetic reference and check that none of them
reaches stdout, stderr or the JSON report, while the copy hidden in the target is found."""

import ast
import json
import os
import re
import sys
import textwrap
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import similarity as sim

CANARY = "zqcanary"

REFERENCE_FUNCTION = textwrap.dedent(
    f'''
    def {CANARY}_sizer(self, {CANARY}_price, {CANARY}_cash):
        """{CANARY} docstring."""
        # {CANARY} comment
        if not self.{CANARY}_flag:
            total = 0
            for {CANARY}_i in range(len({CANARY}_cash)):
                total += {CANARY}_cash[{CANARY}_i] * {CANARY}_price / 7.5
                if total > 1000:
                    raise ValueError("{CANARY} too large")
            return int(self.{CANARY}_lev * (total // self.{CANARY}_margin({CANARY}_price)))
        return int(self.{CANARY}_lev * ({CANARY}_cash // {CANARY}_price))
    '''
)


def _reference(tmp_path):
    ref = tmp_path / f"{CANARY}_corpus"
    ref.mkdir()
    (ref / f"{CANARY}_module.py").write_text(
        f"class {CANARY.title()}Info:\n" + textwrap.indent(REFERENCE_FUNCTION, "    ")
    )
    # an invalid escape sequence makes the compiler warn with the source line attached
    (ref / f"{CANARY}_warns.py").write_text(f'{CANARY}_rx = "\\d+ {CANARY}"\n')
    # a file the tokenizer rejects and one the parser rejects, both quoting a canary
    (ref / f"{CANARY}_untok.py").write_text(f'x = """{CANARY} never closed\n')
    (ref / f"{CANARY}_unparse.py").write_text(f"{CANARY} = = 1\n")
    return ref


def _target(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "copied.py").write_text(
        "class Scheme:\n" + textwrap.indent(sim.renamed_copy(REFERENCE_FUNCTION, seed=4), "    ")
    )
    (target / "unrelated.py").write_text(
        textwrap.dedent(
            """
            import statistics

            def summary(values):
                return {"mean": statistics.fmean(values), "n": len(values)}
            """
        )
    )
    return target


def test_never_prints_the_reference(tmp_path, capsys):
    ref, target = _reference(tmp_path), _target(tmp_path)
    out_json = tmp_path / "report.json"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        rc = sim.main(["--ref", str(ref), "--target", str(target), "--k", "8", "--min-tokens", "10",
                       "--min-nodes", "10", "--json", str(out_json)])
    assert rc == 0
    captured = capsys.readouterr()
    report = out_json.read_text()
    warned = "\n".join(str(w.message) + str(w.filename) for w in caught)
    for channel in (captured.out, captured.err, report, warned):
        assert CANARY not in channel.lower()
    assert "untokenisable 1, unparsable 2" in captured.out

    data = json.loads(report)
    copied = next(f for f in data["files"] if f["path"].endswith("copied.py"))
    unrelated = next(f for f in data["files"] if f["path"].endswith("unrelated.py"))
    assert copied["token_score"] == 1.0
    assert max(s[2] for s in copied["spans"]) >= 60
    assert copied["function_matches"] and copied["ast_score"] > 0.9
    assert unrelated["token_score"] < 0.2 and not unrelated["function_matches"]


def test_reference_index_holds_hashes_only(tmp_path):
    idx = sim.build_reference([str(_reference(tmp_path))], k=8)
    assert all(isinstance(h, int) for h in idx.kgrams | idx.stmts)
    assert CANARY not in repr(vars(idx)).lower()


def test_base_discounts_generic_code(tmp_path):
    ref, target = _reference(tmp_path), _target(tmp_path)
    idx = sim.build_reference([str(ref)], k=8)
    idx.discount(sim.build_reference([str(ref)], k=8))
    assert not idx.kgrams and idx.base_kgrams_removed > 0
    rep = sim.analyse_file(str(target / "copied.py"), "copied.py", idx, w=4, min_nodes=10)
    assert rep.token_score == 0.0 and not rep.spans


def test_normalisation_ignores_names_comments_and_docstrings():
    a = 'def f(x):\n    """doc"""\n    # c\n    return x + 1\n'
    b = "def g(y):\n    return y + 2\n"
    assert [t for t, _ in sim.normalised_tokens(a)] == [t for t, _ in sim.normalised_tokens(b)]
    fs = 'def h(v):\n    return f"{v!r} and {v:>4}"\n'
    assert sim.normalised_tokens(fs)[-3][0] == "S"
    ast.parse(sim.renamed_copy(fs))  # the !r conversion is not renamed into a syntax error


# ------------------------------------------------------------------ release gate

GATE_ARGS = ["--k", "8", "--min-tokens", "10", "--min-nodes", "10"]
GATE_PARAMS = {"k": 8, "w": 5, "min_tokens": 10, "min_nodes": 10}


def _gate(ref, target, allow, capsys, tmp_path):
    out_json = tmp_path / "gate.json"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        rc = sim.main(["--ref", str(ref), "--target", str(target), "--allowlist", str(allow),
                       "--json", str(out_json), *GATE_ARGS])
    captured = capsys.readouterr()
    warned = "\n".join(str(w.message) + str(w.filename) for w in caught)
    for channel in (captured.out, captured.err, out_json.read_text(), warned):
        assert CANARY not in channel.lower()  # the gate prints Tradewind-side facts only
    return rc, captured.out


def _accept_everything(out: str, allow, params=GATE_PARAMS):
    """What a reviewer does after reading the ranges: paste the candidate entries."""
    cands = [json.loads(line.strip().rstrip(",")) for line in out.splitlines()
             if line.strip().startswith('{"file"')]
    for c in cands:
        c.update({"class": "API", "what": "test", "accepted": "2026-11-08", "review": "test"})
    allow.write_text(json.dumps({"parameters": params, "entries": cands}))
    return cands


def test_gate_fails_on_new_ranges_and_passes_once_reviewed(tmp_path, capsys):
    ref, target = _reference(tmp_path), _target(tmp_path)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"parameters": GATE_PARAMS, "entries": []}))

    rc, out = _gate(ref, target, allow, capsys, tmp_path)
    assert rc == 1 and "gate: FAIL" in out and "  NEW      " in out
    cands = _accept_everything(out, allow)
    assert cands and {c["file"] for c in cands} == {"copied.py"}
    assert {c["kind"] for c in cands} == {"tokens", "ast"}

    rc, out = _gate(ref, target, allow, capsys, tmp_path)
    assert rc == 0 and "gate: PASS" in out and "NEW" not in out


def test_gate_entries_survive_a_move_but_not_an_edit(tmp_path, capsys):
    ref, target = _reference(tmp_path), _target(tmp_path)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"parameters": GATE_PARAMS, "entries": []}))
    _accept_everything(_gate(ref, target, allow, capsys, tmp_path)[1], allow)

    copied = target / "copied.py"
    original = copied.read_text()
    # moved: new code (a function, whose DEDENT now precedes the range), comments and blank
    # lines above, reformatted spacing inside
    copied.write_text("import math\n\n# moved down\n\n\ndef rate():\n    return math.pi\n\n\n"
                      + original.replace(", ", ",  "))
    rc, out = _gate(ref, target, allow, capsys, tmp_path)
    assert rc == 0, out

    # edited: one name changed inside the accepted range (the normalised tokens, hence the
    # match with the reference, are unchanged; the reviewed text is not)
    lines = original.splitlines()
    i = next(j for j, line in enumerate(lines) if "return" in line)
    lines[i] = re.sub(r"x[0-9a-f]+_[0-9]+", "edited_name", lines[i], count=1)
    assert "edited_name" in lines[i]
    copied.write_text("\n".join(lines) + "\n")
    rc, out = _gate(ref, target, allow, capsys, tmp_path)
    assert rc == 1 and "NEW" in out and "unused allow-list entry" in out

    # copied elsewhere: the same text in another file is a new range
    copied.write_text(original)
    (target / "elsewhere.py").write_text(original)
    rc, out = _gate(ref, target, allow, capsys, tmp_path)
    assert rc == 1 and "elsewhere.py" in out


def test_gate_refuses_other_parameters_and_an_empty_reference(tmp_path, capsys):
    ref, target = _reference(tmp_path), _target(tmp_path)
    allow = tmp_path / "allow.json"
    allow.write_text(json.dumps({"parameters": GATE_PARAMS, "entries": []}))
    out = _gate(ref, target, allow, capsys, tmp_path)[1]
    _accept_everything(out, allow, params={**GATE_PARAMS, "min_tokens": 11})
    rc, out = _gate(ref, target, allow, capsys, tmp_path)
    assert rc == 1 and "--min-tokens 10 differs" in out and "NEW" not in out

    empty = tmp_path / "empty"
    empty.mkdir()
    allow.write_text(json.dumps({"parameters": GATE_PARAMS, "entries": []}))
    rc, out = _gate(empty, target, allow, capsys, tmp_path)
    assert rc == 1 and "the reference is empty" in out
