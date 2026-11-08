# Similarity review of Tradewind 0.0.2 against backtrader

*Run on 2026-11-08 on tag `v0.0.2` (commit `7c1e817`), after the release reached PyPI.
Clean-room rule 4 asks for this review **before** a public release; for 0.0.2 it came late,
and the log in `CLEANROOM.md` says so.*

**Verdict: no suspect range.** No span of Tradewind code matches backtrader beyond what the
public API dictates (declarative `params`, documented signatures, empty hook methods) or what
the documented semantics force (the one-line formulas of `CommInfoBase`, the
`order_target_size` rule). The longest match outside declarations is 9 lines. One file,
`comminfo.py`, stands well above the others and is discussed in § 5; it does not meet the
definition of a suspect, and 0.0.2 can stay published.

## 1. Method

The reviewer never saw backtrader's source. `tools/similarity.py` reads the reference corpus
by program and keeps **hashes only**: no line, token, identifier, file name, line number or
exception message of the reference is stored, printed or written. Its outputs are scores and
line ranges **in Tradewind**, whose code the reviewer then read alone to classify each range.
`tests/test_similarity.py` plants canaries in a synthetic reference (identifiers, strings,
comments, the directory name, a line that makes the compiler warn, files that fail to tokenise
or to parse) and checks that none reaches stdout, stderr, the JSON report or a warning.

Two independent measures:

* **Tokens.** `tokenize`, comments and docstrings dropped, identifiers → `V`, numbers → `N`,
  strings → `S`; keywords, operators and NEWLINE / INDENT / DEDENT kept. Every run of
  k = 20 normalised tokens is hashed. File score = share of the file's winnowed fingerprints
  (window w = 5, Schleimer, Wilkerson & Aiken 2003) found in the reference. Spans = maximal
  runs of consecutive Tradewind k-grams present in the reference, as line ranges with their
  length in tokens.
* **AST.** Every statement serialised with identifiers, attribute names and constants erased
  (constants keep their type); compound statements carry their body. File score = share of
  the file's AST nodes covered by maximal matching statements of at least 40 nodes. Whole
  functions whose normalised body matches are listed apart, whatever their size.

**Generic Python discounted** (`--base`): k-grams and statements that also occur in an
unrelated corpus are removed from the reference before matching, as MOSS does with base
files. The base is 13 modules of the CPython 3.13 standard library ("A": `asyncio`, `email`,
`logging`, `json`, `unittest`, `concurrent`, `http`, `collections`, `argparse`,
`dataclasses`, `enum`, `functools`, `_pydecimal`). It removed 5 394 of backtrader's
110 338 distinct 20-grams and 652 of its 5 562 distinct statements.

Corpora (all read by the program only):

| role | corpus | files | tokens |
|---|---|---:|---:|
| reference | backtrader 1.9.78.123, PyPI wheel, sha256 `9a07a516…9b5dfd` | 171 | 154 047 |
| reference (check) | the `backtrader` package of the PWB fork installed in `venv145` | 171 | 154 047 |
| noise control | CPython 3.13 stdlib "B", disjoint from A (`multiprocessing`, `importlib`, `xml`, `sqlite3`, `zoneinfo`, `pathlib`, `tomllib`, `wsgiref`, `urllib`, `statistics`, `fractions`, `_pydatetime`, `calendar`, `csv`, `heapq`, `inspect`, `typing`) | 106 | 240 309 |
| same-domain control | PyAlgoTrade 0.20 (Apache 2.0, sdist sha256 `7927c87a…541cbd`) + QSTrader 0.3.0 (MIT, wheel sha256 `7373d3e2…59baa5`): two backtesting engines with strategies, brokers, feeds, commissions and analyzers, unrelated to backtrader | 171 | 81 409 |
| target | `src/tradewind` at `v0.0.2` | 24 | 36 688 |

The fork gives exactly the same scores and spans as the upstream wheel.

## 2. Thresholds and controls

| run (k = 20, base A discounted) | token score | AST score | longest span | spans ≥ 41 tokens |
|---|---:|---:|---:|---:|
| noise: Tradewind vs stdlib B | 3.73 % | 0.00 % | 40 | 0 |
| same domain: Tradewind vs PyAlgoTrade + QSTrader | 1.46 % | 0.00 % | 32 | 0 |
| **Tradewind vs backtrader** | **8.06 %** | **0.28 %** | **139** | **18** |

Without the base the noise run already reaches a 425-token span (`lines.py:68-125`, the
operator methods, which match any repeated `def __x__(self, other): return …` block); the
base brings the noise maximum down to 40 tokens. **Threshold: a span of 41 tokens or more**
(one more than the longest noise span; about four to five lines) is reviewed by hand. For
the AST measure, no statement of 40 nodes or more matched either control.

**Positive controls.**

* Each of the 23 modules of `src/tradewind/`, every identifier renamed, every string and
  comment changed, indexed alone: token score 100 % on all of them, AST score 67-99 % (the
  rest is statements below 40 nodes; 0 % on `__init__.py`, which holds only imports).
* Fifteen Tradewind functions (the median, 80th and 97th percentile by length in `broker`,
  `indicator`, `cerebro`, `analyzers`, `models`), renamed and buried in stdlib B, base
  discounted: each of the 12 functions of 41 normalised tokens or more is found with a span
  of 41 tokens or more (e.g. `broker.fills` 362 tokens → longest span 286, `cerebro.run`
  496 → 243, `analyzers.stop` 266 → 267); the three functions of 20-35 tokens show their
  whole length, below the threshold. With one line in five also replaced by `pass` (an edited
  port), 13 copies still parse; 9 of the 10 that are 41 tokens or longer are found above the
  threshold, the miss being `indicator._tw_input_lines` (90 tokens, longest surviving run 39).
* Sensitivity: at k = 15 the noise maximum rises to 62 tokens and the six backtrader spans
  above it are all `params` declarations listed below; at k = 30 the base no longer removes
  the generic operator block (noise maximum 425) and nothing new appears. k = 20 is kept.

The tool cannot see a copy shorter than about 41 normalised tokens (four lines), nor a port
rewritten line by line into another control structure; neither would show as a "copy" to any
token or AST detector.

## 3. Scores by file

Token score in %, against backtrader without and with the base, the longest span after the
base, the AST score, and the two controls (token score, base discounted).

| file | bt raw | **bt − base** | longest | AST | noise | domain |
|---|---:|---:|---:|---:|---:|---:|
| `comminfo.py` | 66.1 | **53.2** | 75 | 0.0 | 1.3 | 3.9 |
| `__init__.py` | 60.0 | 24.3 | 32 | 0.0 | 4.3 | 0.0 |
| `lines.py` | 31.6 | 21.2 | 35 | 0.0 | 21.5 | 3.6 |
| `analyzer.py` | 43.0 | 20.7 | 29 | 0.0 | 5.9 | 11.9 |
| `cerebro.py` | 19.7 | 13.9 | 139 | 4.5 | 6.6 | 2.4 |
| `strategy.py` | 22.6 | 13.7 | 53 | 0.0 | 2.6 | 2.2 |
| `trade.py` | 33.3 | 12.4 | 27 | 0.0 | 2.6 | 3.9 |
| `feeds.py` | 10.0 | 9.4 | 64 | 0.0 | 3.2 | 0.0 |
| `order.py` | 16.6 | 8.4 | 39 | 0.0 | 4.3 | 2.4 |
| `indicators/__init__.py` | 10.6 | 8.4 | 40 | 0.0 | 0.5 | 0.2 |
| `broker.py` | 12.9 | 7.8 | 80 | 0.0 | 2.7 | 0.9 |
| `analyzers.py` | 9.0 | 6.1 | 78 | 0.0 | 2.1 | 1.2 |
| `params.py` | 17.2 | 4.5 | 27 | 0.0 | 4.5 | 4.5 |
| `position.py` | 5.5 | 3.4 | 29 | 0.0 | 7.5 | 0.0 |
| `market.py` | 5.4 | 3.1 | 27 | 0.0 | 2.2 | 2.7 |
| `securities.py` | 6.1 | 2.9 | 41 | 0.0 | 2.0 | 0.0 |
| `timer.py` | 4.9 | 2.8 | 26 | 0.0 | 0.0 | 0.7 |
| `events.py` | 3.7 | 2.5 | 29 | 0.0 | 0.8 | 1.7 |
| `cashbook.py` | 4.3 | 1.9 | 24 | 0.0 | 5.8 | 2.9 |
| `indicator.py` | 3.1 | 1.8 | 31 | 0.0 | 1.8 | 0.5 |
| `clock.py` | 4.6 | 0.7 | 27 | 0.0 | 1.0 | 1.3 |
| `lineops.py` | 1.1 | 0.4 | 20 | 0.0 | 6.9 | 0.4 |
| `models.py` | 2.7 | 0.4 | 26 | 0.0 | 2.6 | 0.4 |
| `dateutils.py` | 0.0 | 0.0 | 0 | 0.0 | 0.8 | 0.0 |
| **all** | 13.0 | **8.06** | 139 | 0.28 | 3.73 | 1.46 |

The engine proper (the 16-phase loop, `broker.py`'s fills and batch check, `models.py`,
`market.py`, `securities.py`, `clock.py`, `events.py`, `indicator.py`, the indicator
arithmetic) sits at or below the noise level. The excess over the controls is concentrated
where the API itself is the code: class-level declarations, signatures, empty hooks,
`__all__` and import lists, and `comminfo.py`.

## 4. Ranges above the threshold, classified

Classes: **API** = names, signatures, `params` or `lines` declarations fixed by the public
API; **idiom** = commonplace Python; **semantics** = a short formula or rule stated by the
public documentation (or observed on the oracle and recorded in `spec/02`); **suspect** = a
long range, not dictated by the API, with an identical control structure.

| # | Tradewind range | tokens | what it is (read on the Tradewind side only) | class |
|---|---|---:|---|---|
| 1 | `cerebro.py:22-46` (AST `23-43`, 60 nodes) | 139 | `Cerebro.params`: the 19 documented parameters with their defaults | API |
| 2 | `broker.py:65-71` | 80 | `setcommission(...)` signature and the one-to-one forwarding of its keywords to `CommissionInfo` | API |
| 3 | `analyzers.py:218-245` | 78 | `SharpeRatio.params`: the 10 documented parameters | API |
| 4 | `comminfo.py:22-38` | 75 | `COMM_PERC, COMM_FIXED = 0, 1` and `CommInfoBase.params` (10 documented parameters) | API |
| 5 | `comminfo.py:74-82` | 74 | `getsize` (units bought by `cash` at `price`, margin for futures-like, times `leverage`) and the head of `getoperationcost` | semantics (§ 5) |
| 6 | `feeds.py:151-161` | 64 | `PandasData.params`: column names, `-1` = find the column by name | API |
| 7 | `feeds.py:33-43` | 63 | `DataBase.params` | API |
| 8 | `comminfo.py:102-111` | 60 | `cashadjust` (futures-like only) and the head of `_getcommission` | semantics (§ 5) |
| 9 | `comminfo.py:80-88` | 58 | `getoperationcost` / `getvaluesize`: `abs(size) × margin` or `× price` | semantics (§ 5) |
| 10 | `comminfo.py:86-93` | 55 | `getvaluesize` / head of `getvalue` | semantics (§ 5) |
| 11 | `strategy.py:234-239` | 53 | `order_target_size`: buy the gap if the target is above the position, sell it if below | semantics |
| 12 | `comminfo.py:66-74` | 46 | `get_margin`: `automargin` off → `margin`; negative → `price × mult`; positive → `price × automargin` | semantics (§ 5) |
| 13 | `comminfo.py:99-105` | 46 | end of `getvalue`, `profitandloss` = `size × (new − old) × mult` | semantics (§ 5) |
| 14 | `broker.py:421-423` | 45 | `BackBroker.buy(...)` signature (documented order keywords) building a `BuyOrder` | API |
| 15 | `broker.py:428-430` | 45 | `BackBroker.sell(...)`, the same | API |
| 16 | `cerebro.py:104-107` | 44 | `addsizer` / `addsizer_by_idx`: store `(cls, args, kwargs)` | API + idiom |
| 17 | `broker.py:133-140` | 41 | tail of `BackBroker.params` (`slip_*`, `shortcash`, `fundstartval`, `fundmode`) | API |
| 18 | `securities.py:157` | 41 | `Bar(feed.datetime[0], feed.open[0], …, feed.volume[0])`: reading the current bar through the documented `[0]` indexing | API + idiom |

Whole functions whose normalised body matches (AST, any size): five in `comminfo.py`
(`getsize` 37 nodes, `getoperationcost` 27, `cashadjust` 26, `getvaluesize` 25,
`profitandloss` 19), five one-liners of `cerebro.py` (`addanalyzer`, `addobserver`,
`addobservermulti`, `addsizer`, `addsizer_by_idx`, 15-16 nodes), and 30 bodies of at most 11
nodes (`pass` hooks, `return len(...)`, the 16 operator methods of `lines.py`, which also
match the stdlib). Below the threshold the spans of 30-40 tokens are the same kinds:
operator methods (35 each, also in the stdlib), indicator class headers
(`lines = (...)`, `params = (...)`, 31-40), `__all__` and import lists.

Spans of 60 tokens or more found **only without the base**, all present in the stdlib too:
`lines.py:68-125` (operator methods), `__init__.py:27-64` and `models.py:637-644`
(`__all__`), `analyzer.py:28-46` and `strategy.py:151-169` (empty hooks),
`strategy.py:188-206` (`buy`/`sell` signatures), `cerebro.py:93-110` (`add*` one-liners),
`clock.py:28-43` (an `IntEnum`), `market.py:143-151` (`np.zeros` columns),
`broker.py:150-153` (a keyword-argument call). All API or idiom.

## 5. `comminfo.py`, the closest file

After the base, 53 % of its fingerprints occur in backtrader (next file: 24 %, an import
list), and it holds the only whole function bodies of more than 16 nodes that match. Read on
the Tradewind side, every method is one stock-like/futures-like branch and a one-line formula
that the public documentation of commission schemes states: size bought by cash, operation
cost, value, P&L times `mult`, the futures-only cash adjustment, the `automargin` rule,
percentage or fixed commission. With the parameter names fixed and the formula fixed, the
normalised shape `if not stocklike: return f(margin); return g(price)` leaves almost no room,
and each method is at most 9 lines. Spans crossing method boundaries are compatible with
the same method order, but these methods all share nearly the same normalised shape, so the
measure cannot tell order from shape. This is the definition of "dictated by the API", not of
a suspect.

Options for Édouard (no change in this PR):

1. **Accept as is** (recommended): the ranges are short, documented and forced; 0.0.2 stays.
2. **Precautionary rewrite in 0.0.3**: a contributor who has not written `comminfo.py` rewrites
   its bodies from `spec/` alone with a deliberately different structure (for example the
   stock-like / futures-like split held by two value models behind the spec/05 fee and
   buying-power interfaces, `CommInfoBase` keeping only the public names), proves it on the
   oracle probes and the parity corpus, and this tool is rerun. 0.0.2 would not be yanked.

## 6. Limits

* Independence: the review was run by a Claude (Opus 5.5) session that wrote none of the
  modules under review, but the code itself was written by Claude sessions; the
  classification in § 4 can be checked by anyone reading the Tradewind lines listed.
* Blind by design: a short copy (under about four lines) or a port rewritten into another
  control structure is invisible to this tool, as to any token or AST detector. The clean-room
  discipline of `CLEANROOM.md`, not this review, is the protection against those.
* The comparison is with backtrader 1.9.78.123 and the PWB fork; other backtrader versions were
  not indexed.

## 7. Reproduce

```bash
# reference: the wheel unpacked in a fresh, empty directory; tool run isolated (-I)
python -m pip download --no-deps backtrader==1.9.78.123 -d /tmp/bt && \
  python -c "import zipfile,glob; zipfile.ZipFile(glob.glob('/tmp/bt/*.whl')[0]).extractall('/tmp/bt/src')"
STD=$(python -c "import sysconfig; print(sysconfig.get_paths()['stdlib'])")
BASE=$(for m in asyncio email logging json unittest concurrent http collections argparse.py \
       dataclasses.py enum.py functools.py _pydecimal.py; do printf -- "--base $STD/$m "; done)
python -I tools/similarity.py --ref /tmp/bt/src/backtrader $BASE --target src/tradewind \
  --json /tmp/similarity.json
# noise control: replace --ref by the stdlib B modules of § 1; positive control:
python tools/similarity.py --self-test-control src/tradewind/broker.py
```

Before the next release this should run in the release workflow, next to the oracle job (the
reference installed in a throwaway environment, as the oracle is), failing on any new span of
41 tokens or more that is not in an allow-list of the API ranges above.
