# API reference

Generated from the docstrings of the public modules (`src/tradewind/`), at the version of
`main` the site was built from. Names starting with an underscore are internal and left out.
The guides explain how the pieces fit; these pages list what each module exposes.

| page | modules |
|---|---|
| [Engine and strategies](engine.md) | `tradewind.engine`, `strategy`, `order`, `position`, `trade`, `timer`, `params`, `events` |
| [Typed API](typed.md) | `tradewind.typed` |
| [Data feeds and lines](feeds.md) | `tradewind.feeds`, `lines`, `lineops`, `market`, `dateutils` |
| [Orders, broker and models](broker.md) | `tradewind.broker`, `comminfo`, `models`, `securities` |
| [Realistic models and cash](realistic.md) | `tradewind.realistic`, `cashbook` |
| [Indicators](indicators.md) | `tradewind.indicator`, `indicators` |
| [Analyzers and metrics](analyzers.md) | `tradewind.analyzer`, `analyzers`, `performance`, `metrics` |
| [Calendars and clock](calendars.md) | `tradewind.calendars`, `clock` |
| [Universes and corporate actions](universe.md) | `tradewind.universe`, `corporate` |
| [Intraday and options](intraday-options.md) | `tradewind.intraday`, `consolidators`, `options` |
| [Fingerprint](fingerprint.md) | `tradewind.fingerprint` |
| [Live trading](live.md) | `tradewind.live` and its submodules |

`import tradewind as tw` exposes the most used names at the top level (`tw.Engine`,
`tw.Strategy`, `tw.TypedStrategy`, `tw.feeds.PandasData`, `tw.ind`, `tw.analyzers`,
`tw.ExchangeClock`, ...); `tw.Cerebro` is a permanent alias of `tw.Engine`.
