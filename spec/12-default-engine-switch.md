# 12. Tradewind as the default engine of pwb-toolbox

Status: **proposal, for Édouard** (2026-11-22). Decision of 2026-11-22: make Tradewind the
default engine for research and backtests first, then for the catalogue's monthly run. The
package rename (`paperswithbacktest`, `import pwb`) and the import codemod are **not**
prerequisites: they come later, on their own schedule.

## What "default" means

pwb-toolbox picks its engine once per process (`pwb_toolbox/engine.py`): `engine.use()` /
`engine.select()` before the import, else `PWB_BACKTEST_ENGINE`, else the default. Today the
default is backtrader. The switch changes **only that default** to `tradewind`;
`PWB_BACKTEST_ENGINE=backtrader` (or `engine.use("backtrader")`) keeps the old engine for one
month. Strategies are not edited: the name `backtrader` resolves to Tradewind in the process
(`engine.alias_backtrader()`), as in every parity run since November.

## Criteria (all required)

1. **NAV parity, per strategy, on the whole catalogue**, measured on the pwb-toolbox release
   that will carry the switch (0.1.54 for the campaign, 0.1.55 for the switch), both engines
   in **one** environment, one frozen papers tree, every dataset pinned to one recorded Hub
   revision, `PYTHONHASHSEED=0`. Each strategy must be either
   * **identical to the bit** (same dates, every daily value equal as a float), or
   * **within tolerance** (`abs(a-b) <= 1e-9 * max(1, abs(b))`, spec/03), with the cause
     written down (summation order, one unit in the last place), or
   * **divergent with a bounded, explained cause** that is not the engine's: the strategy
     (unseeded randomness, set iteration, a decision on a float tie), the data, or a
     behaviour of the reference engine that spec/02 lists as *corrected*. Each such case is
     listed by slug with its first differing date, its final NAV gap and its Sharpe gap.
   A divergence whose cause is the engine is fixed in Tradewind (pull request, a probe in
   `tests/oracle/`, clean-room rules) before the switch. A strategy that errors on one engine
   only blocks the switch until fixed or explained.
2. **No run-time dependency on backtrader.** With Tradewind selected no module is loaded
   from a backtrader package (the `leaked` verdict of spec/03 is zero on the campaign);
   the CI grep of `src/` holds. Today backtrader is a hard requirement of pwb-toolbox
   (`install_requires`) and Tradewind an extra: 0.1.55 swaps them (Tradewind required,
   backtrader in an extra `pwb-toolbox[backtrader]` for the fallback month).
3. **Similarity review** of the Tradewind release that ships the switch (CLEANROOM.md rule
   4, `tools/similarity_gate.sh` green, any new allow-list entry logged).
4. **End-to-end test of the wheels** (pwb-toolbox and Tradewind built once, installed in a
   clean environment, real backtests through the papers harness on both engines; the figures
   read against the reference, not only `status ok`) before any upload.
5. **Speed**: no strategy slower than on backtrader by more than its timeout margin (the
   campaign records both wall times).

## Fallback

* For one month after 0.1.55, `PWB_BACKTEST_ENGINE=backtrader` restores the old engine
  (with `pwb-toolbox[backtrader]` installed; the catalogue's server images keep it); backtrader
  is never imported when Tradewind is selected.
* A divergence found after the switch: the strategy is pinned to backtrader in its run
  (environment variable on that task), a Tradewind issue is opened with the slug and the
  first differing date, and the fix goes through the usual parity probe.
* Rolling back the whole switch is one pwb-toolbox release that sets the default back to
  `backtrader`; no strategy or dataset changes.
* The catalogue's monthly run switches only after one full month of research use without
  an unexplained divergence; until then it pins `PWB_BACKTEST_ENGINE=backtrader`.

## Calendar

| when | what |
|---|---|
| early December 2026 | full parity on pwb-toolbox 0.1.54 (campaign of the whole catalogue, report `parity_0154`) |
| first half of December | fixes in Tradewind for the engine-caused divergences, re-run of those strategies |
| about 15-20 December | pwb-toolbox 0.1.55: Tradewind is the default for research and backtests; backtrader as a fallback for one month |
| 7 January 2027 | the catalogue's monthly run on Tradewind (if criterion 1 still holds on 0.1.55) |
| later | rename to `paperswithbacktest`, codemod of the imports, removal of the backtrader fallback |

Every merge and every PyPI release goes through Édouard.
