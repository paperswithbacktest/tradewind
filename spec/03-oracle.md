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
  value, order log (date, symbol, size, price, status) for the subset below.

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
* **Speed benchmark** (CI, informational; nightly, gating): 5 symbols × 20 years and
  4 000 symbols × 20 years, µs per symbol-bar, against the backtrader reference measured
  in pwb-toolbox 0.1.41 (35 µs).

## Second oracle

Five to ten Lean regression algorithms (Python), deterministic and data-light, reproduced
with a tolerance set per algorithm: a check of semantics, never of equality.
