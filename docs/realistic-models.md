# Realistic models and currencies

The default models reproduce backtrader bar for bar (`ModelSet.oracle()`). `tradewind.realistic`
adds models that price what backtrader leaves out: the spread of each security in each year,
fills limited by the bar's volume, a margin account, the cost of borrowing a short, settlement
delays and interest. They are **opt-in**: nothing changes unless you install them.

## The realistic profile

```python
import pandas as pd
from tradewind.realistic import CostTable

costs = CostTable.from_frame(pd.read_parquet("transaction_cost_params.parquet"))
engine.broker.set_profile("realistic", costs=costs)
```

`set_profile("realistic", **options)` installs `ModelSet.realistic(**options)` as the default
model set of every security that has no models of its own, and adds the margin-call model.
Call it before `run()`. The options and their defaults:

| option | default | model |
|---|---|---|
| `costs` | `CostTable()`: 1 bp half-spread for every name | spread per symbol and year |
| `spread_as` | `"fee"` | `"fee"`: the half-spread is charged as a commission (`SpreadFeeModel`); `"slippage"`: it moves the fill price (`SpreadSlippage`). Never both |
| `spread_fraction` | `1.0` | share of the half-spread paid on each side |
| `per_share`, `minimum` | `0.0`, `0.0` | a broker commission per share, with a minimum per fill |
| `impact_factor` | `0.0` (off) | linear-book impact from the table's `lambda_bps`: `factor × lambda × notional / 1e6` basis points |
| `participation` | `0.1` | fills capped at 10 % of the bar's volume (`VolumeShareFillModel`); `None`: whole fills |
| `account` | `"margin"` | `"margin"`: Reg T (`MarginAccount`); `"cash"`: `CashAccount` |
| `initial_margin`, `maintenance_margin` | `0.5`, `0.25` | Reg T |
| `borrow` | `0.0025` | annual borrow fee on shorts, or a `BorrowRates`; `None`: free |
| `settlement` | `"us-equity"` | T+3, T+2 from 2017-09-05, T+1 from 2024-05-28; an `int` for T+n; `None`: immediate |
| `debit_rate`, `credit_rate` | `0.0`, `0.0` | interest on negative and positive cash (`FlatMarginInterest`) |

Any single model can still be replaced for one security with
`engine.broker.set_models(data, fee=..., fill=...)`, and `ModelSet.realistic(...)` builds the set
without installing it. `set_profile("oracle")` puts the defaults back.

Speed: on 2 000 names × 500 bars a monthly rebalance runs at about 1.3 µs per security and bar
under the realistic profile (0.65 µs under the oracle defaults); see
`tools/bench_engine.py --profile realistic`.

## Costs: a spread per symbol and per year

`CostTable` holds a full bid-ask spread in basis points for each `(symbol, year)`. It is shaped
like PWB's `Stocks-Yearly-TransactionCostParams` dataset (columns `symbol`, `year`,
`spread_bps`, `lambda_bps`; 245 897 rows, 36 089 symbols, 2003-2026; ETFs included). The fee
of a fill uses the spread of the fill's year, so a 1995 trade and a 2025 trade on the same
name do not pay the same.

```python
table = CostTable.from_frame(frame, max_gap=None, max_spread_bps=5_000)
table.lookup("SPY", 2025)       # (half_spread, "measured" | "nearest" | "default")
```

- A missing year takes the nearest year of the same symbol (`max_gap` limits the distance;
  `None`, the default, does as PWB's harness does). Before 2003 that is the 2003 spread,
  which understates the pre-decimalisation spreads.
- An unknown symbol takes `default_half_spread` (1 bp).
- A zero, negative or missing spread is dropped: absent, not free.
- `aliases={"SPY.ETF": "SPY"}` maps feed names to the table's symbols. A ticker is the
  table's only key, and tickers are reused: a symbol with holes of several years can carry
  another instrument's spread.
- `max_spread_bps` caps the few rows whose spread is wider than the price.

The PWB harness (`pwb_toolbox.backtesting.commission`) charges this half-spread on each side;
where no measurement exists it falls back to Roll's estimator on daily closes. `CostTable`
does not estimate: pass a table with your own estimates if you want them.

`CostTable.flat(10.0)` charges 10 bp (5 bp a side) to every name.

## Fills capped by volume

`VolumeShareFillModel(participation=0.1)` fills at the oracle's prices (market at the open,
limit and stop against the bar's range) but never more than `participation × volume` of the
bar, rounded down to whole units. The rest stays open and fills on the next bars of the same
feed: the order is notified `Partial` after each part and `Completed` after the last. A bar
with zero volume fills nothing; a feed without volume (NaN, as forex or index feeds) is not
capped. To let the rest of an order lapse, give it `valid=` or cancel it after the first
`Partial` (cancel the order `buy()` returned: a notification is a snapshot).

## Margin, cash account, margin calls

`MarginAccount(initial=0.5, maintenance=0.25)`:

- equity = cash in every currency + unsettled proceeds + positions, in the account currency,
  valued at this bar's open (the last close for a security without a bar);
- an order may add initial margin only while `equity − initial margin of the book` stays
  non-negative; the batch of a bar is checked in submission order; an order that reduces a
  position always passes; **a refused order consumes nothing** (backtrader's check carries
  its cost into the rest of the batch);
- at the fill, the order is re-checked at the fill price; the margin a sale frees funds the
  buys filled after it in the same bar;
- a short beyond the `ShortableProvider`'s quantity is refused (`Margin`).

`CashAccount()` pays purchases in full from **settled** cash and refuses short sales.

`MarginCallModel` (installed by `set_profile` with a margin account) runs at phase 9: when the
equity falls below the maintenance margin of the book, every position is cut pro rata so that
the requirement comes back to the equity. The orders carry `order.info["margin_call"] = True`
and fill at the next bar's open.

`order_target_value` / `_percent` size the gap in the account currency, toward zero, in
multiples of the security's `lot_size`.

## Borrowing shares

```python
from tradewind.realistic import BorrowRates

borrow = BorrowRates(rate=0.0025, rates={"GME": 0.35}, easy_to_borrow={"AAPL", "GME"},
                     shortable={"GME": 10_000})
```

The fee accrues at each slice on the short value at the last close, for the time since the
previous slice, in the security's quote currency; `data.security.holding.total_borrow_fees`
sums it. `rebate` is subtracted from every rate.

## Settlement

`DelayedSettlement(days=1, calendar=None)`: the proceeds of a sale (or a dividend) wait
`days` business days; purchases and fees leave at once. Waiting money is not in `getcash()`
but is in `getvalue()`. `UsEquitySettlement()` follows the US rule by trade date and skips
NYSE holidays. With `CashAccount` the unsettled proceeds are not buying power.

## Several currencies

The cash book holds one balance per currency; the portfolio value and `getcash()` are in the
account currency (USD unless `set_account_currency`).

```python
from tradewind.securities import SymbolProperties

sap = engine.adddata(tw.feeds.PandasData(dataname=sap_eur), name="SAP")
eurusd = engine.adddata(tw.feeds.PandasData(dataname=eurusd_frame), name="EURUSD")
engine.broker.set_properties(sap, SymbolProperties(quote_currency="EUR"))
engine.broker.set_conversion("EUR", eurusd)           # close = dollars per euro
engine.broker.set_models(sap, buying_power=MarginAccount())
```

- Every cash flow of a security (price, fee, borrow fee, dividend) is in its quote currency:
  buying SAP debits euros. Without euros the balance goes negative (borrowed euros, as in a
  margin account at a broker); it is not swept into dollars.
- `set_conversion(currency, data, invert=False)`: the feed's last close is the value of one
  unit of `currency` in the account currency, refreshed at each bar; `invert=True` for a pair
  quoted the other way (`USDJPY` to price JPY in a USD account).
- Positions and foreign balances are marked at that rate; `engine.broker.cashbook["EUR"]`
  holds the amount and the rate.
- Use `MarginAccount` or `CashAccount` for a foreign security: the compatible buying-power
  model compares a quote-currency cost with the account-currency cash.
- A feed of conversion is an ordinary feed: the strategy sees it in `self.datas`, and `next()`
  waits for its first bar.

**PWB data.** Most foreign indices in PWB's `Indices-Daily-Price` are **already in dollars**
(the local index times the `XXXUSD` rate: `NKY`, `DAX`, `CAC`, `HSI`, `SENSEX`, `IBOV`,
`SPTSX`, `OMX`, `JALSH`...): add them as USD securities, and their return mixes the market and
the currency. A few are in local currency (`UKX` in GBP, `TWSE`, `WIG`, `TOP40`): give them
`quote_currency` and a conversion. For the local-currency series of a dollar index, divide it
by the PWB rate; ask `Forex-Daily-Price` for the pair as dealers quote it (`USDJPY`, `USDCAD`;
only `EURUSD`, `GBPUSD`, `AUDUSD`, `NZDUSD` the other way round). The column it returns is
named the other way (`JPYUSD`, `CADUSD`) and holds dollars per unit of the local currency:
that is the rate `set_conversion` takes, without `invert`.

See [`examples/09_realistic_profile.py`](examples/09_realistic_profile.py) and
[`examples/10_foreign_currency.py`](examples/10_foreign_currency.py).
