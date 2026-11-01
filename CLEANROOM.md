# Clean-room journal

Tradewind is written **from the public API contract and from black-box behaviour**, never
from backtrader's source code. This file is the record that keeps it so.

## Rules

1. Nobody writing Tradewind code opens, copies, translates or paraphrases backtrader's source.
   This includes AI assistants: they code from the specification in `spec/` and from tests,
   never from a recalled or pasted excerpt.
2. Allowed inputs: backtrader's public documentation and docstrings as rendered on its
   website; the behaviour observed through black-box tests (NAV paths, order logs) run in a
   **separate** test environment where backtrader is installed as an oracle; the strategies of
   the Papers With Backtest corpus (what they call, read statically with `tools/inventory.py`);
   QuantConnect Lean's code and documentation (Apache 2.0, attributed in `NOTICE`).
3. backtrader never appears in `src/`, in `pyproject.toml` dependencies or in any published
   artefact. CI greps for it.
4. Before every public release a similarity review is run against backtrader's source by
   someone who has not written the module under review, and its result is logged here.

## Log

| date (UTC) | module | sources consulted | author | note |
|---|---|---|---|---|
| 2026-11-01 | `tools/inventory.py` | the corpus' own `strategy.py` files (ast) | Claude (Fable 5.1) for PWB | static inventory, no backtrader source read |
| 2026-11-01 | `spec/` | inventory output, backtrader public docs (API names only) | Claude for PWB | specification started |
