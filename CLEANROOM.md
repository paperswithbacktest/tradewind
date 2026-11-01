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
| 2026-11-01 | `src/tradewind/*` (lines, feeds, order, position, comminfo, broker, strategy, cerebro, timer, analyzer, trade, dateutils) | `spec/01-04`; public parameter names of the compatible API; behaviour observed through `tools/run_oracle.py` on a separate environment where the oracle is installed | Claude (Fable 5.1) for PWB | three semantics fixed from black-box observation only: batch acceptance at the next bar, Submitted→Margin pre-check, value-gap truncation in `order_target_value` |
| 2026-11-01 | `src/tradewind/indicator.py`, `indicators/` | mathematical definitions (SMA, EMA with SMA seed, Wilder RSI/ATR, MACD, ROC, Highest/Lowest, CrossOver); oracle runs for equality | Claude for PWB | 9/9 oracle strategies identical at 1e-9 |
| 2026-11-01 | `broker.py` batch acceptance, `cerebro.py` peek-and-fill | `tools/run_corpus_strategy.py` on three corpus strategies against the campaign's reference NAVs; order notification logs of both engines compared | Claude for PWB | learned: the submitted orders of a bar are checked against a running cash (a sell funds a later buy) before any fill; 3/3 real strategies identical at 1e-15 |
| 2026-11-01 | `indicator.py` runonce pre-pass, `broker.py` check price | synthetic probes `foreign_feed`, `batch_funding`, `close_then_target` run on both engines; corpus VIX strategy order logs | Claude for PWB | learned: in the oracle's runonce mode a next-only indicator is precomputed with every non-input feed at its last bar; pre-check priced at the submission bar's close. 12/12 synthetic, 17/20 validation strategies identical |
| 2026-11-01 | `broker.py` batch pre-check (deficit carry), probe `deficit_carry` | order logs of both engines on `a_new_anomaly…` (48-order batch of 2020-05-28, 20 refusals) and `cryptocurrency_momentum…`; rule candidates searched offline against the oracle's Accepted/Margin verdicts; synthetic probe confirmed on both engines | Claude for PWB | learned: the running cash of the pre-check takes the cash flow of every order, refused ones included, and an order is Accepted iff that cash is non-negative after it -- a refused buy's deficit weighs on the orders after it and a long-reducing sell can be refused. 13/13 synthetic, 20/20 validation strategies identical at 1e-15 against the oracle run with the same hash seed |
| 2026-11-01 | `lines.py` indexing, `strategy.py` / `cerebro.py` readiness, probe `line_edges`, oracle feed `S3` listing 30 bars late | lot of 50 corpus strategies against the campaign NAVs (two divergent: `regime_detection…` reads `close.array` and dates back to the first bar, `long_term_investing_in_triple_leveraged_etfs` reads `datetime.date(1)`); public documentation of the oracle on positive indexing being look-ahead; synthetic probe on both engines | Claude for PWB | learned: a line indexes its preloaded buffer as a plain sequence (a bar ahead is readable, before the first bar wraps to the end, only outside the buffer raises; the raw array holds the whole series during the run), and `next` waits until every feed has delivered a bar (`prenext` meanwhile). 14/14 synthetic, both corpus strategies identical |
| 2026-11-01 | `broker.py` reversing fill, probe `reverse_shortfall` | order logs of both engines on `breaking_bad_trends` (1993-04-02: short 1 998, buy 3 947 at a gapped-up open); synthetic probe on both engines | Claude for PWB | learned: the closing part of a reversing order always fills; its opening part fills only if the cash left after closing covers it, otherwise the order ends Partial then Margin with the opening part untouched. 15/15 synthetic, corpus strategy identical |
| 2026-11-01 | `broker.py` cheat-on-close, `cerebro.py`, probe `cheat_on_close` | order logs of both engines on `profitable_mean_reversion_after_large_price_drops…` (`set_coc(True)`); synthetic probe on both engines (fills, statuses, dates) | Claude for PWB | learned: with cheat-on-close only the fill price of a market order changes (the close of the bar it was placed on); batch acceptance, the pre-check at that same close and the notification at the next bar are unchanged. 16/16 synthetic, corpus strategy identical |
| 2026-11-01 | `broker.py` add_cash / get_value, `strategy.py` order_target_value, probe `add_cash_timing` | order logs of both engines on `consumer_protection…pension_product` (synthetic cash sleeves moved with `add_cash`); 30 isolated scenarios run on the oracle alone (scratchpad `dbg_addcash*.py`), each varying one step | Claude for PWB | learned: `add_cash` lands at the end of the next bar's broker pass (invisible to `getcash`, to the batch check and to the fills of that pass); `get_value(datas=…)` returns the positions' value alone and applies any pending addition on the spot; the plain `get_value()` is marked once per bar after fills and additions. 17/17 synthetic, corpus strategy identical |
