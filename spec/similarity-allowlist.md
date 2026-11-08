# The similarity gate and its allow-list

Clean-room rule 4 (`CLEANROOM.md`) asks for a similarity review against backtrader before
every public release. Since 0.0.3 a gate runs it on every pull request and on every release
tag, so that a new range close to backtrader is seen when it is written, not after the tag.

## What runs

`tools/similarity_gate.sh --control`, in the `similarity` job of `.github/workflows/ci.yml`
and of `.github/workflows/release.yml` (where `publish` needs it):

1. reads `spec/similarity-allowlist.json`: the reference (`backtrader==1.9.78.123`, pinned
   by sha256), the standard-library base (13 modules of Python 3.13, base "A" of the 0.0.2
   review) and the parameters (k = 20, w = 5, 41 tokens, 40 AST nodes);
2. installs the reference into a throwaway virtual environment with `--require-hashes`,
   `--no-compile` and `--no-deps`; nothing imports it;
3. runs `python -I tools/similarity.py --ref <reference> --base <stdlib modules> --target
   src/tradewind --allowlist spec/similarity-allowlist.json`. The tool reduces the reference
   to hashes as it reads it and prints scores and **Tradewind** locations only
   (`tests/test_similarity.py` plants canaries in a synthetic reference and checks that none
   reaches stdout, stderr, the JSON report or a warning, in gate mode as well);
4. fails on any token span of 41 tokens or more, or AST statement of 40 nodes or more, that
   matches the reference and is not in the allow-list; also on parameters that differ from
   the allow-list's, on another Python minor version than the base's, and on an empty
   reference;
5. negative control (`--control`): the same gate on a copy of `src/tradewind` with
   `cerebro.py` planted as `_planted_copy.py` must fail on the planted file, or the job fails:
   a gate that passes everything is caught.

## The allow-list

`spec/similarity-allowlist.json` lists the ranges a human has reviewed and accepted. An entry
is keyed by `(file, kind, digest)`:

* `file`: the path relative to `src/tradewind`;
* `kind`: `tokens` (a span) or `ast` (a statement);
* `digest`: a hash of **Tradewind's own text** for the range (its tokens with their names,
  without whitespace, comments, line breaks or line numbers; for `ast`, the statement's
  `ast.dump`). The entry survives the range moving inside its file or being re-indented; any
  edit of the range, any growth of the span (it grows only when the code next to it also
  matches), the same text in another file, or a new range, fails the gate.

The other fields are for the reader: `size`, `lines_at_review` (where the range was when it
was accepted; not used for matching), `class` (API, idiom, semantics), `what` (what the range
is, read on the Tradewind side), `accepted` (date) and `review` (where the classification is
argued). An entry that no longer matches anything is reported as unused and does not fail the
gate; remove it in the next change to the file.

The 19 entries of 0.0.2 are the 18 spans and the one AST statement (`Cerebro.params`, inside
span 1) of `spec/similarity-review-0.0.2.md` § 4. The seven `comminfo.py` entries follow
Édouard's decision of 2026-11-08 to keep that file as is (§ 5).

## When the gate fails

The job prints each new range as `file:first-last  kind size  digest`, followed by a
candidate JSON entry. Then:

1. **Read the range in Tradewind only.** Never open the reference to compare; the tool's
   output is all the comparison there is.
2. **Classify it** with the classes of the 0.0.2 review § 4: API (names, signatures,
   `params`/`lines` declarations fixed by the public API), idiom (commonplace Python),
   semantics (a short rule stated by the public documentation or observed on the oracle and
   recorded in `spec/02`), or **suspect** (long, not dictated by the API, same control
   structure).
3. **Suspect**: do not allow-list it. The range is rewritten from `spec/` by a contributor who
   has not written it, with a deliberately different structure, proven on the oracle probes
   and the parity corpus, and the gate is rerun.
4. **Not a suspect**: add the candidate entry to `spec/similarity-allowlist.json` with `class`,
   `what`, `accepted` and `review` filled in, in the same pull request as the code, and add a
   line to the log of `CLEANROOM.md` (date, files, ranges, class, who reviewed).
5. **Human review is mandatory.** A pull request that touches the allow-list is merged only
   after a person (Édouard or a maintainer he names) has read each new range and its class.
   An assistant may prepare the entry; it never approves it.

Changing the reference version, the base or the parameters is a new calibration: rerun the
noise and positive controls of the 0.0.2 review § 2, write the result in a review under
`spec/`, and regenerate the allow-list from a run that a human reviews range by range.
