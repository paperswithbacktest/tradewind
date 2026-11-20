# 03. The equivalence oracle

backtrader is used in one place only: a separate test environment that produces reference
runs. It is never imported by the engine, never a dependency, never redistributed.

## Reference set

* **Current**: `v1`, the 2026-11 grand recalcul (pwb-toolbox 0.1.51/0.1.52 on the reference
  engine, 4 199 strategies, 33.8 M daily NAV values). Provenance: `oracle/v1.md`.
* **Stored**: on the Hub, private dataset `paperswithbacktest/Tradewind-Oracle`, one folder per
  version (373 MB for v1: too big for git). The repository pins it by revision and sha256 in
  `oracle/<version>.lock.json`; `tools/oracle/run.py` downloads, checks and caches it.
* **Content**: `nav.parquet` (slug, date, value; one row group per strategy) and
  `manifest.parquet`: per strategy, its role (reference or excluded, and why), the engine
  distribution, pwb-toolbox version, papers tree and `strategy.py` blob, hash seed, Python,
  host, UTC start and end, data epoch, the datasets republished during the run, and the
  earlier Tradewind verdict. `hub_commits.json` gives the revision of every dataset at any
  epoch. On the Tradewind side `engine.fingerprint` (spec/05 § 12) says whether two runs read
  the same inputs.
* **Data revisions**: a replay pins every dataset to its revision at the reference's epoch
  (`tools/oracle/pinned_hub.py`), price and cost tables alike, online; no frozen cache.
* **Versioned**: `v1` = the oracle's semantics (kept behaviours). Each "corrected" default of
  `02-semantics.md` that ships, a new pwb-toolbox or a squashed dataset history makes
  `v<n+1>`, with a per-strategy delta against the previous version.
* **Before v1**: the pwb-toolbox 0.1.41 campaign (`nav_0138/`), used by the lots of 50 of
  chantier 2.

## Tolerances

| what | tolerance |
|---|---|
| daily NAV, kept behaviours | `abs(a-b) <= 1e-9 * max(1, abs(b))` |
| order log | identical sequence (date, symbol, size, exec price) |
| strategies calling `random` without a seed | excluded from equality, kept for smoke |

## Subsets

* **CI (every pull request and push to main)**: `tools/oracle/subsets/ci.txt`, 40 strategies
  chosen by `tools/oracle/select_subset.py`: references Tradewind already reproduced on the
  same measurement, at most 12 s each, one papers tree, no dataset republished during the run,
  no unseeded randomness, no options or intraday data, and every idiom found among such
  strategies at least twice (target orders of the three kinds, buy/sell, close, `close[-k]`,
  `notify_order`, indicators and indicator subclasses, limit/stop, `add_cash`, the price
  families, fundamentals, economic data, point-in-time universes). One process, about two
  minutes of strategy time (`.github/workflows/oracle-corpus.yml`, which skips while its two
  secrets are missing). Validated on the Mac on 2026-11-19: 40/40 identical (worst relative
  difference 3.8e-16).
* **Nightly**: the whole set, on the data host, a seventh a night by `crc32(slug) % 7`
  (`tools/oracle/nightly.sh`), each reference in the environment of the pwb-toolbox that made
  it: about 240 hours of reference compute, so a full pass a week at four jobs.
* **Verdicts**: identical; divergent or error, *expected* only when the reference set
  recorded that same verdict for Tradewind on that same measurement; leaked (a backtrader
  module loaded in a Tradewind run). Any unexpected verdict fails the run.
* **Speed benchmark**: 5 symbols × 20 years and 4 000 symbols × 20 years, µs per
  security-bar, Tradewind against backtrader **on the same machine**, the same seeded bars
  and the same strategy classes (`tools/bench_reference.sh small|wide`, backtrader in the
  oracle's environment only); each scenario's final value must be equal on both engines. The
  small case runs in CI on every push (times informational); the wide case (about an hour per
  scenario on backtrader) runs on an idle machine, results committed under `bench/` and
  published in `docs/performance.md`.

## Second oracle

Five to ten Lean regression algorithms (Python), deterministic and data-light, reproduced
with a tolerance set per algorithm: a check of semantics, never of equality.
