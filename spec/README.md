# Tradewind specification

The engine is specified before it is coded. Each document here states a contract the
engine must honour, the backtrader behaviour it reproduces (observed, not read), the Lean
concept it borrows where backtrader has none, and the tests that prove it.

| file | what it fixes |
|---|---|
| `inventory/backtrader-surface.md` | what the 4 300 corpus strategies and pwb-toolbox actually call (generated) |
| `01-scope.md` | the compatible surface Tradewind commits to, in three tiers |
| `02-semantics.md` | event order, fills, sizing, notifications: kept vs corrected (to be validated) |
| `03-oracle.md` | the equivalence oracle: reference NAVs, tolerances, CI subset |
| `04-design.md` | the modules of the first engine and their public names |
| `05-interfaces.md` | chantier 2: Security, Broker, pluggable reality models, clock, event bus, feeds; event order in a bar (validated 2026-11-03, implemented 2026-11-06, § 11) |
| `similarity-review-0.0.2.md` | the clean-room similarity review of release 0.0.2 against backtrader (rule 4 of `CLEANROOM.md`), with `tools/similarity.py` |
| `similarity-allowlist.md`, `similarity-allowlist.json` | the similarity gate of CI and of the release workflow (`tools/similarity_gate.sh`), the ranges accepted after human review, and the procedure to add one |
