# 03. The equivalence oracle

backtrader is used in one place only: a separate test environment that produces reference
runs. It is never imported by the engine, never a dependency, never redistributed.

## Reference set

* **Source**: the pwb-toolbox 0.1.41 campaign (`campaign_results_0138.jsonl`, NAV paths in
  `nav_0138/<slug>.json`), 4 202 strategies run on the Hub data pinned by commit hash.
* **Versioned**: `oracle/v1/` = backtrader 1.9.78 semantics (kept behaviours). Each
  "corrected" default of `02-semantics.md` that ships regenerates `oracle/v<n+1>/` with a
  per-strategy delta file.
* **Stored**: slug, data snapshot hashes, strategy file hash, parameters, daily NAV, final
  value, order log (date, symbol, size, price, status) for the subset below. On the
  Tradewind side the first four are `engine.fingerprint` (spec/05 § 12): its digest says
  whether two runs read the same inputs.

## Tolerances

| what | tolerance |
|---|---|
| daily NAV, kept behaviours | `abs(a-b) <= 1e-9 * max(1, abs(b))` |
| order log | identical sequence (date, symbol, size, exec price) |
| strategies calling `random` without a seed | excluded from equality, kept for smoke |

## Subsets

* **CI (every push)**: 40 strategies, chosen to cover every Tier 1 and Tier 2 idiom at least
  twice, each running under 10 s on the public sample data shipped with the repo.
* **Nightly**: the 4 202, on the data host, from the Hub snapshot.
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
