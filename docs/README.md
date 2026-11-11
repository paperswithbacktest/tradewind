# Tradewind documentation

Tradewind is a backtesting engine for Python, under the Apache 2.0 licence, that runs
strategies written against the backtrader API. It is event-driven (one `next()` per bar,
the same code path a live run will take), vectorised across securities inside each bar,
and has two runtime dependencies: numpy and pandas.

```bash
pip install tradewind
```

```python
import tradewind as tw          # or, in an existing project: import tradewind as bt
```

## Status

Version 0.0.2, November 2026. The compatible surface is complete for the strategies it was
built against (a corpus of about 4 200 published strategies) and is checked against
backtrader's own results, bar by bar:

| what | where it stands |
|---|---|
| Strategies, data feeds, orders (market, close, limit, stop), positions, notifications, timers | done, identical NAVs to backtrader on ~1 000 corpus strategies and on 33 synthetic probes |
| Indicators used by the corpus (SMA, EMA, Highest, Lowest, ROC, ATR, RSI, MACD, CrossOver, StdDev) and custom indicators | done, bit for bit |
| Analyzers: `TradeAnalyzer`, `DrawDown`, `SharpeRatio`, `TimeReturn` | done |
| Pluggable reality models (fees, fills, slippage, buying power, settlement, shorting, margin interest, corporate actions), per security | interfaces done; the default set reproduces backtrader |
| Calendars, universes, corporate actions, multi-currency, futures | interfaces in place, implementations planned |
| Resampling, replay, plotting, `optstrategy`, live trading | not available yet |

Speed: the engine itself costs 0.2 to 1 µs per security and bar on a 2 000-security
universe (`tools/bench_engine.py`); on the corpus the median strategy runs about 2.3 times
faster than on backtrader, data loading included. See [Performance](performance.md).

## Contents

1. [Getting started](getting-started.md): install, a first backtest, reading the result.
2. [How a backtest runs](concepts.md): the clock, the sixteen phases of a bar, lines and
   `ago` indexing, when an order fills.
3. [Strategies](strategies.md): parameters, data access, orders, positions, notifications,
   timers.
4. [Data feeds](data-feeds.md): `PandasData`, column mapping, several instruments, late
   listings.
5. [Orders and the broker](orders-and-broker.md): order types and states, cash checks,
   commissions, slippage, leverage, per-security models.
6. [Indicators](indicators.md): the built-in ones, writing your own, line arithmetic.
7. [Analyzers](analyzers.md): drawdown, Sharpe ratio, trade statistics, period returns.
8. [Migrating from backtrader](migrating-from-backtrader.md): what changes (an import),
   what is not supported, and the behaviours worth knowing.
9. [Coming from QuantConnect Lean](coming-from-lean.md): a concept-by-concept map.
10. [Performance](performance.md): what costs what, and how to write fast strategies.

The [examples](examples/) run as they are, on synthetic data: `cd docs/examples && python
01_sma_crossover.py`.

The design documents behind the engine are in [`spec/`](../spec/): scope, the behaviours
kept from backtrader and those to be corrected, the oracle, and the interfaces.
