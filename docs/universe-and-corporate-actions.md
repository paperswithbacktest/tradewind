# Universes and corporate actions

Two opt-in features for equity backtests that should not see the future:

- a **point-in-time universe**: the set of names a strategy may hold changes during the
  run, as an index or a liquidity screen did at the time;
- **corporate actions**: splits, dividends, delistings and ticker changes applied to the
  holdings, so that a backtest can run on raw prices and still be right.

Neither changes anything when it is not set: an engine without a universe and without a
corporate-action provider runs exactly as before, bit for bit.

The example [`examples/11_universe_and_corporate_actions.py`](examples/11_universe_and_corporate_actions.py)
uses both on synthetic data.

## A point-in-time universe

```python
import tradewind as tw
from tradewind.universe import MembershipUniverse

engine = tw.Engine()
for name, frame in prices.items():          # one feed per symbol the universe ever holds
    engine.adddata(tw.feeds.PandasData(dataname=frame), name=name)

members = ...   # a DataFrame: one row per member and date, columns "date" and "symbol"
engine.set_universe(MembershipUniverse(members), on_removal="liquidate")
```

**The table.** One row per member and date, the layout of PWB's
`Stocks-Daily-LiquidUniverse` (a `rank` column is optional: `top=500` keeps the rows ranked
500 or better). The members listed at a date are the universe from that date until the
next date of the table; `tolerance="7D"` empties the universe when the last snapshot is
older than that (pwb-toolbox's rule), `lag="1D"` makes each date take effect a day later
(when the table is computed from that day's close and you want no doubt about look-ahead).
For windows rather than snapshots -- listing and delisting dates, index inclusion and
exclusion -- use `IntervalUniverse(table, symbol=, start=, end=)`:

```python
from tradewind.universe import IntervalUniverse

engine.set_universe(IntervalUniverse(listing, start="ipoDate", end="delistingDate"))
```

**Feeds.** The engine does not add feeds during a run: every symbol the table ever names
needs a feed, added before `run()`. The table's symbols are matched to the feeds by name
when the run starts; symbols without a feed are ignored and listed in
`engine.universe.unresolved`. A feed the table never names (an index, a hedge) stays in the
universe all along. A feed the table names starts outside it.

**What the strategy sees.**

```python
from tradewind.universe import feeds

class Book(tw.Strategy):
    def notify_universe(self, change):            # optional
        for data in feeds(self, change.removed):
            self.close(data)
        # change.added, change.removed: SecurityIds; change.time: ns UTC

    def next(self):
        members = self.engine.universe.members    # the feeds in the universe now
        ...
```

`notify_universe(change)` is called at phase 12 of the slice where the change takes
effect, before that slice's `next()` (Lean's `OnSecuritiesChanged`). `change` is the engine's
`clock.UniverseChange`: `time` (ns UTC), `added` and `removed` as SecurityIds;
`tradewind.universe.feeds(self, ids)` turns them into the strategy's feeds (import it with
`from tradewind.universe import feeds`). With the typed API the same event reaches
`on_universe_change(SecurityChanges)`. `data.security.in_universe` says whether a feed is a
member now; `engine.universe.is_member(data)` too.

**Leaving the universe.** `on_removal="keep"` (the default) leaves the position to the
strategy. `on_removal="liquidate"` closes it with a market order placed right after the
strategy's `next()` of the slice that removed it -- so it fills at the next bar's open,
like any order of that bar, with the commission scheme and the cash check of any order --
unless the strategy placed an order on that feed in that slice (its own exit).

**The `prenext` gate.** Normally `next()` waits until every feed has delivered a bar. A
point-in-time universe holds names listed years apart, so the feeds it manages do not hold
the strategy back (`set_universe(..., gate=False)`, the default). A member may then have no
bar yet: check `len(data)` or `data.security.is_tradable` before reading it.

## Corporate actions

```python
from tradewind.corporate import SymbolCorporateActions

engine.adddata(tw.feeds.PandasData(dataname=raw_aapl), name="AAPL", normalization="raw")
engine.set_corporate_actions(SymbolCorporateActions(
    splits=splits,            # symbol, date (ex-date), split_from, split_to
    dividends=dividends,      # symbol, date (ex-date), cash_amount, pay_date, split_adjusted_cash_amount
    delistings=delistings,    # symbol, delistingDate (or date), optionally last_price
    symbol_changes=renames,   # date, old, new
))
```

The tables are keyed by symbol; their layouts are those of PWB's `Stocks-Daily-Splits`,
`Stocks-Daily-Dividends` and ListingStatus, and any table with the same columns works.
Each action reaches **phase 3** of its slice, before that slice's fills (orders placed the
day before an ex-date fill in post-split shares), and a strategy that defines
`notify_corporate_action(action)` receives it at phase 12.

| action | when | what it does |
|---|---|---|
| split, ratio r = `split_to / split_from` | the feed's first bar on or after the ex-date | quantity × r, average price ÷ r, open orders' sizes × r and prices ÷ r, the open trade's size and price likewise; value unchanged |
| dividend, amount per share | the feed's first bar on or after the ex-date | quantity × amount credited (a short pays it); `total_dividends` of the holding |
| delisting | first slice on or after the delisting date, never before the feed's last bar | open orders on it end; the position is sold at the last close (or `last_price`), less `haircut`, without fee; the trade closes (`notify_trade`, `TradeAnalyzer`); `data.security.is_delisted` |
| ticker change | first slice on or after its date | `data.security.symbol` becomes the new ticker; the feed keeps its name, so `getdatabyname` is unchanged, and in the typed API `self.bars[name]` too (the new ticker is `self.bars[name].symbol`, see [Typed API](typed-api.md#names-and-ticker-changes)) |

**Dividends on the pay date.** By default the cash lands on the ex-date (the decision of
spec/05 § 10, Q2). To credit it on the pay date instead, give the feed's securities the
policy: `engine.broker.set_models(data, corporate_actions=tw.models.OracleCorporateActions(dividends="pay_date"))`.
The entitlement is fixed on the ex-date and carried as a receivable, counted in
`getvalue()` but not in `getcash()`, until the pay date (`engine.broker.receivables()`).

**Delistings.** `SymbolCorporateActions(liquidate=True, haircut=0.0)`: the defaults sell at
the last close. `haircut=0.3` books a −30 % delisting return, a common assumption for
performance delistings when the last trade is unknown. `liquidate=False` keeps the
oracle's behaviour (the position stays, marked at its last close forever). Two guards:

- a feed that still trades more than `grace` (seven days) after the delisting date is
  another instrument under a reused ticker: that delisting is set aside, in `conflicts`;
- `delist_ended=True` also delists a feed that stops before the end of the run without a
  delisting row, at the slice after its last bar. Use it only when the feeds stop because
  the security stopped, not because the data provider stopped updating it.

After the run starts, `unresolved` lists the symbols without a feed, `dropped` the actions
outside their feed's bars, `actions` those applied.

## Raw or adjusted prices

What a split or a dividend does depends on what the prices already contain, declared per
feed (Lean's `DataNormalizationMode`):

| `normalization=` | the prices | split | dividend | delisting, ticker change |
|---|---|---|---|---|
| `"raw"` | as traded | scales the position | pays cash | apply |
| `"split_adjusted"` | back-adjusted for splits | already in the prices: ignored | pays cash, per share in today's shares | apply |
| `"adjusted"` | back-adjusted for splits and dividends (total return) | ignored | ignored | apply |
| undeclared (`None`) | | applies | applies | apply |

On split-adjusted prices the dividend per share must be in the same shares as the prices:
`SymbolCorporateActions` takes the table's `split_adjusted_cash_amount`, or divides the cash
amount by the ratios of the later splits in the split table. Declare the mode with
`engine.adddata(data, normalization=...)` or `engine.set_normalization(data, ...)`; it is
recorded in the run's fingerprint, with a digest of the universe and corporate-action
tables.

A wrong declaration is the classic error: an adjusted series with splits applied doubles
them (the position jumps ×r while the price does not move). The undeclared default applies
everything, as a feed carrying events always did.

## PWB's tables

`tradewind.corporate` reads them from the Hugging Face Hub with a bounded read (the
`huggingface_hub` and `pyarrow` packages, which Tradewind does not install; a token with
access to the gated datasets in `HF_ACCESS_TOKEN`): only the files of the years or months
the window touches, only the needed columns, the symbol and date filters pushed down to
Parquet.

```python
from tradewind.corporate import pwb_corporate_actions, pwb_liquid_universe

symbols = ["AAPL", "NVDA", "YHOO"]
actions = pwb_corporate_actions(symbols, "2017-01-01", "2024-12-31")
universe = pwb_liquid_universe("2017-01-01", "2017-12-31", top=100, tolerance="7D")
```

What the data does and does not carry, measured on 2026-11-15:

- **`Stocks-Daily-Splits`**: 9 583 rows on the Hub, ex-dates (`date`), `split_from`,
  `split_to`; AAPL 4-for-1 on 2020-08-31, NVDA 10-for-1 on 2024-06-10 (NVDA's 4-for-1 of
  2021-07-20 is typed `stock_dividend`, with the same ratio). Announced future splits are
  in the table.
- **`Stocks-Daily-Dividends`**: 517 935 rows, ex-date (`date`), `pay_date`, `cash_amount`,
  `split_adjusted_cash_amount`, `currency` (non-US listings included: the reader keeps USD
  by default). Rows with future ex-dates are declared distributions.
- **ListingStatus** (`AlphaVantage-Stocks-Daily-ListingStatus`): `delistingDate` for the
  delisted names (YHOO 2017-06-26, AET 2018-11-28, TWTR 2022-10-28); the field lags recent
  takeovers, and only a minority of the delisted names have prices at all.
- **No ticker-change table**: PWB's prices are filed under the **current** symbol (a
  renamed company's whole history is under its new ticker), so a rename is invisible in the
  prices; pass your own `symbol_changes` table when you need one.
- **`Stocks-Daily-Price` is back-adjusted for most names** (those from its historical
  source) and raw for some recent ones: declare `normalization="adjusted"` (splits and
  dividends ignored, delistings applied) unless you have checked the series. Raw daily
  prices are not in the catalogue; `Stocks-1Min-Price` is raw for splits (see
  [Intraday data](intraday.md)).
- **`Stocks-Daily-LiquidUniverse`** starts on 2003-10-07, ranks by 60-session median dollar
  volume (a liquidity screen, not index membership); a name near the cut enters and leaves
  often (YHOO left the top 100 five times in 2017), so `on_removal="liquidate"`
  trades on each crossing.

A check on the data host (2026-11-15): the 2017 top-100 universe (142 feeds, 35 157 price
rows) with its corporate actions read in 7 s and ran in 0.13 s; YHOO left the universe on
2017-06-19 and was delisted on 2017-06-26 at its last close of 2017-06-16; AABA entered on
2017-07-17.
