# Options

Listed options run on the engine as securities of their own: one feed per contract, a
contract multiplier of 100 on its security, and the engine handling what happens at expiry.
`tradewind.options` also prices them (Black-Scholes-Merton, Greeks, implied volatility) and
filters chains. The design is in [`spec/06`](../spec/06-intraday-and-options.md).

## The data: a daily option table

One row per contract and day it traded:

| column | example |
|---|---|
| `symbol` | `SPY190215P00250000` (OCC) |
| `date` | 2019-01-02 |
| `underlying` | `SPY` |
| `expiration` | 2019-02-15 |
| `strike` | 250.0 |
| `option_type` | `put` / `call` |
| `open`, `high`, `low`, `close`, `volume` | prices per share |
| optional: `exercise_style`, `shares_per_contract`, `implied_volatility`, `delta`, ... | |

PWB's daily option table has these columns. `tw.options.synthetic_option_table(bars,
"XYZ")` builds one from a price series (BSM prices, monthly expiries) for tests and examples.

## Adding contracts to a run

```python
import tradewind as tw
from tradewind.options import add_options

engine = tw.Engine()
spy = engine.adddata(tw.feeds.PandasData(dataname=spy_daily), name="SPY")   # unadjusted prices
chain = add_options(engine, spy, table,
                    select=lambda c: c.puts_only().expiration(0, 62).moneyness(0.85, 1.02))
engine.addstrategy(MyStrategy, chain=chain)
```

`add_options` adds a feed for every contract that `select` keeps on at least one date (the
engine cannot add feeds during a run, so the selection is made over the whole table first).
Keep it narrow: SPY puts over a year, 0 to 62 days and 85 % to 102 % of the spot, is about
12 000 contracts and runs in a few seconds. Option feeds do not hold back `next()` until
they list.

The underlying must be quoted **as traded** (not adjusted for dividends or splits), since
strikes are: an adjusted series moves the moneyness of every contract.

## Choosing contracts in the strategy

```python
def next(self):
    stock = self.datas[0]
    puts = (self.p.chain.chain()                 # today's contracts, against today's close
            .puts_only()
            .expiration(20, 45)                  # 20 to 45 calendar days
            .nearest(strike=stock.close[0] * 0.95))
    if len(puts):
        feed = puts.feeds()[0]                   # the contract's feed: trade it like any other
        self.sell(feed, size=1)                  # one contract = 100 shares
```

The filters, each returning a new chain: `calls_only()`, `puts_only()`,
`expiration(min_days, max_days)`, `front_month()`, `standards_only()` (third-Friday
expiries), `strikes(-n, +n)` (the n strikes below and above the one nearest the spot),
`moneyness(lo, hi)` (strike / spot), `delta(lo, hi)`, `liquid(min_volume)`,
`nearest(strike=..., days=...)`. `chain.frame` is the underlying DataFrame;
`chain.contracts()` gives `OptionContract` objects (`underlying`, `expiry`, `strike`,
`right`, `style`, `multiplier`, `settlement`, `symbol`).

## Money: the multiplier

Prices are per share and the security of an option carries `contract_multiplier = 100`.
Selling one put at 2.50 credits 250; the position is marked at `close × 100` per contract;
`order_target_value(feed, target=1_000)` at 2.00 buys 5 contracts. Commissions come from the
commission scheme: set them per contract (`commtype=COMM_FIXED`), since a percentage scheme
reads the per-share premium.

## Expiry

On the first bar after a contract's expiry date, before that bar's fills, the engine
settles every position in it against the underlying's close on the expiry date:

* in the money by at least $0.01: a long position is exercised, a short one assigned in full;
  otherwise the contract expires worthless;
* `settlement="physical"` (equity and ETF options, the default): 100 shares per contract
  change hands at the strike (a short put assigned on 3 contracts buys 300 shares);
  `settlement="cash"` (index options): the intrinsic value × 100 is paid in cash;
* open orders on the contract are cancelled; the contract's security is marked delisted.

A strategy that defines `notify_corporate_action(action)` receives each expiry with
`action.settle_price`, `action.exercised`, `action.quantity` (contracts held, signed),
`action.delivered` (shares received or delivered) and `action.cash`. Exercises are not
reported to `notify_order` or `notify_trade`. Early exercise and early assignment are not
modelled; AM-settled index options settle on the expiry day's close here, not on the next
morning's opening print.

## Pricing and Greeks

```python
from tradewind.options import bsm_price, bsm_greeks, implied_volatility

bsm_price(spot=100, strike=95, t=30 / 365, rate=0.04, div=0.01, vol=0.2, right="put")
iv = implied_volatility(price, spot, strike, t, rate, div, right)        # arrays welcome
g = bsm_greeks(spot, strike, t, rate, div, iv, right)                     # delta, gamma, vega, theta, rho
chain.with_greeks(rate=0.04, div=0.01)          # adds implied_volatility and the Greeks to a chain
```

European Black-Scholes-Merton with a continuous dividend yield. Units: `t` in years, rates
continuous and annual, `vega` per 1.00 of volatility (divide by 100 for a volatility point),
`theta` per year (divide by 365 for a day). American options get a European implied
volatility, a little high on deep in-the-money puts.

## Example

[`examples/06_cash_secured_put.py`](examples/06_cash_secured_put.py): sell a put about 5 %
out of the money every month, take the shares if assigned and sell them the next day. It
runs on a synthetic table; `python 06_cash_secured_put.py table.parquet underlying.parquet`
runs it on a real one.
