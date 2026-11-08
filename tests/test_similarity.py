"""``tools/similarity.py`` must find a renamed copy and never print the reference corpus.

The reference of a real review is code nobody on the project may read. These tests plant
canaries (identifiers, strings, comments, a path, a line that makes the parser warn, files
that fail to tokenise or to parse) in a synthetic reference and check that none of them
reaches stdout, stderr or the JSON report, while the copy hidden in the target is found."""

import ast
import json
import os
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
