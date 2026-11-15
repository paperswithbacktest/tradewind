# Tradewind vs Lean

**As of 15 November 2026**: Tradewind `main` after pull request #22 (version 0.0.2 on PyPI),
against QuantConnect Lean as described in its public documentation. Lean is Tradewind's
design reference for what a backtester must model (securities, reality models, the order of
events in a bar); both are Apache 2.0. This page says where Tradewind stands against it:
**at level**, **ahead** or **behind**. It is kept by hand and dated; when a row changes,
the date changes with it.

The comparison is of capabilities, not of results: no strategy has been run on both engines
side by side, and no speed comparison with Lean has been measured.

## Summary

| | count | where |
|---|---|---|
| ahead | 5 | installation, backtrader compatibility, run fingerprint, live replay, two APIs on one engine |
| at level | 9 | event loop, reality-model interfaces, corporate actions, normalisation modes, multi-currency cash, consolidators, statistics, typed strategies, licence |
| behind | 13 | asset classes, resolutions, data, universe selection, calendars, options, indicators, live brokerages, optimisation, framework, charts and reports, research environment, maturity |

## Row by row

| area | Tradewind | Lean | standing |
|---|---|---|---|
| Installation and footprint | `pip install tradewind`; numpy and pandas only; plain Python, local | .NET engine with Python through pythonnet; the LEAN CLI and Docker for local runs, or QuantConnect's cloud | **ahead** |
| backtrader compatibility | strategies written for backtrader run unchanged (an import line); identical NAVs on about 1 000 strategies of a 4 200-strategy corpus and on every synthetic oracle probe | none: a backtrader strategy is rewritten | **ahead** |
| Reproducibility | `engine.fingerprint`: hash of engine source, environment, data content and provenance, strategy source and parameters, broker, clock, seed; `diff` between runs ([Reproducibility](reproducibility.md)) | deterministic engine; no built-in fingerprint of data and code that we know of | **ahead** |
| Live session replay | append-only write-ahead journal; a restarted session replays it and must match, else it refuses to start ([Live trading](live-trading.md)) | live results and logs stored; no deterministic replay of a session that we know of | **ahead** |
| Two APIs on one engine | the backtrader-style API and a typed API (`TypedStrategy`, dataclass parameters, typed events, `py.typed`, checked by mypy in CI) share one loop | one API (C# and its Python mirror) | **ahead** (for Python users) |
| Event loop | one synchronous loop, sixteen fixed phases per slice, the same path for backtest and live | a fixed order of events per time slice, the same engine for backtest and live | at level |
| Reality models | fees, fills, slippage, buying power, settlement, shortable provider, margin interest, corporate actions: interfaces per security, defaults reproducing backtrader, a realistic set opt-in (measured spreads per symbol and year, volume-capped partial fills, Reg T margin and margin calls, borrow fees, T+1/T+2) | the same families of models, with many brokerage-specific presets; fills from quote bars when bid/ask data is present | at level (fewer presets, no quote-driven fills) |
| Corporate actions | splits, dividends, delistings, ticker changes from symbol tables, applied before fills | the same events, from Lean's map and factor files shipped with its data | at level (you supply the tables) |
| Price normalisation | `raw`, `split_adjusted`, `adjusted` per feed | `Raw`, `SplitAdjusted`, `Adjusted`, `TotalReturn` and others | at level |
| Several currencies | a cash book per currency, conversion from FX feeds, value in the account currency | `CashBook` with automatic conversion feeds | at level (conversion feeds are added by hand) |
| Consolidators | time and count consolidation of minute bars to N minutes, hours, sessions | time, count, tick and renko consolidators | at level for bars; no tick or renko |
| Run statistics | `Performance`: Sharpe, Sortino, PSR, alpha, beta, drawdown, exposure, turnover, capacity | the same set and more (information ratio, Treynor, tracking error, win and loss rates, and others) | at level for the common set |
| Typed strategies | `TypedStrategy` with typed bars, orders and events | typed C# API; Python stubs | at level |
| Licence | Apache 2.0, clean-room with a similarity gate in CI | Apache 2.0 | at level |
| Asset classes | stocks, ETFs, listed options on daily tables; foreign-quoted securities | equities, options, futures, future options, index options, forex, CFDs, crypto, crypto futures | **behind**: no futures, no FX-pair or CFD trading model yet |
| Resolutions | daily and minute bars, consolidated | tick, second, minute, hour, daily, with quote and trade bars | **behind** |
| Data | bring your own DataFrame; Papers With Backtest datasets through its SDK | a data library, downloaders and alternative data integrated with the engine | **behind** |
| Universe selection | point-in-time membership tables; every feed is added before the run | dynamic selection on fundamentals, ETF constituents or custom data during the run | **behind** |
| Exchange calendars | NYSE, Nasdaq, CME equity index futures, crypto ([Calendars and clock](calendars-and-clock.md)) | a market-hours database for every market Lean supports | **behind** |
| Options | European Black-Scholes-Merton, implied volatility, exercise and assignment at expiry, daily tables | several pricing models (American included), minute option data, early-assignment simulation | **behind** |
| Indicators | about ten built in (those the corpus uses) plus custom indicators and line arithmetic | more than a hundred | **behind** |
| Live brokerages | Interactive Brokers, paper, preview | many brokerages and exchanges in production | **behind** |
| Parameter optimisation | not available (`optstrategy` raises) | grid and other optimisers in the LEAN CLI and the cloud | **behind** |
| Algorithm framework | none: universe, alpha, portfolio, execution and risk are written in the strategy | Alpha / Portfolio construction / Execution / Risk modules | **behind** (not planned for now) |
| Charts and reports | none; analyzers return dictionaries | charts, backtest reports | **behind** |
| Research environment | any notebook, plain Python | QuantBook research notebooks with the engine's data | **behind** |
| Maturity | first releases in November 2026 | a decade of production use | **behind** |

## Reading the table

- Tradewind is ahead where it is small: one `pip install`, the backtrader API kept, and every
  run tied to its inputs by a hash.
- It is at level on what a backtest must model to be honest about costs and events: the
  reality models, corporate actions and statistics follow Lean's concepts, but Tradewind
  leaves the data to the user.
- It is behind on breadth: asset classes, resolutions, markets, brokerages, data and tooling.
  Futures and more of the live path are on the roadmap; the algorithm framework is not.

For a concept-by-concept translation of code, see [Coming from Lean](coming-from-lean.md).
