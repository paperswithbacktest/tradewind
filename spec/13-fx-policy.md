# 13. A portfolio's currency policy: base currency, hedged or not, the same in backtest and live

Status: **proposal and first implementation** (2026-11-24), for the daily runner
(`tradewind.live.daily`). Nothing changes for a meta-portfolio that declares nothing: its base
currency is USD, it has no hedge, and its run is bit-identical to before.

## Why

The strategies' books are in USD. An investor who counts in another currency (the Phase 1
account is in EUR) holds two things at once: the strategies' returns, and an exposure to the
dollar that depends on where the capital sits. Today the runner's reference (the *expected
NAV*, `tracking.py`) is a USD portfolio, while the account keeps its capital in EUR and borrows
USD for its positions: the gap between the two is dominated by EUR/USD (78 % of the gap on the
10-day replay of 2026-11-24, `fx` term). That gap says nothing about execution. The fix is not
to explain the gap better but to **declare the currency policy once and apply it to both
sides**: the reference is computed in the base currency under that policy, and the live account
is steered to the same exposure.

## The declaration (meta-portfolio)

```yaml
base_currency: EUR          # the currency the investor counts in; default USD (nothing changes)
fx_policy: hedged           # unhedged (default) or hedged
fx_hedge_rebalance: monthly # hedged: when the net exposure is sold again: daily, weekly, monthly,
                            #   or never (no currency trade at all, below)
fx_hedge_half_spread: 0.000025   # the pair's half-spread, a fraction of the notional (0.25 bp)
fx_rates:                   # daily short rates for the carry, % a year, ACT/360
  USD: fred:DFF             #   (default for USD) effective federal funds rate
  EUR: fred:ECBESTRVOLWGTTRMDMNRT   # (default for EUR) euro short-term rate
```

* `base_currency` is USD or the account's currency (the run stops otherwise: the account is
  the only place the base currency's cash can be).
* `fx_policy` is about the **net value** of the portfolio, not its gross exposure. A book at
  leverage 4 is long and short about four times its value in USD; the shorts' proceeds and the
  margin loan finance the longs in USD, so the gross creates no dollar exposure beyond the net
  value. What a hedge sells is the net value's dollar exposure.
* `fx_rates` values: `fred:<series>` (public CSV, no key), `ecb:<flow>/<key>` (ECB data
  portal), a path to a CSV (`date,rate`), or a number (a constant rate, for tests). A missing
  rate gives no carry and the `fx_policy` alert naming the series and the cause (never a stop,
  never a silent zero). Downloads identify themselves (`User-Agent: tradewind/<version>
  (+https://github.com/paperswithbacktest/tradewind)`: FRED does not answer a bare name) and
  time out after 10 s; a copy cached on an earlier day is used when a download fails, and the
  alert says so.

## The two policies

Write `x` for the rate (USD per unit of base), `E` the part of the value held in the base
currency (in base units) and `U` the part exposed to the dollar (in USD). The value in base is
`E + U / x`.

* **unhedged**: the value is the strategies' USD value converted at the day's rate; the dollar
  exposure is suffered. On the first day the allocated capital is converted to USD (`U` brought
  to the capital in USD); nothing is traded afterwards.
* **hedged**: on the first day and then at each `fx_hedge_rebalance` date, `U` is sold against
  the base currency (`U -> 0`), on the real tradable pair (`EURUSD` in the books' naming,
  `EUR.USD` on IDEALPRO). Between two dates the month's P&L stays exposed.
* **hedged with `fx_hedge_rebalance: never`**: no currency order, on either side, ever (not even
  on the first day). Both sides start from the account's own split and keep it: the capital
  stays in the base currency (and earns the carry), the foreign positions are financed in USD
  (a USD balance borrowed at the broker), so `U` is only the strategies' P&L since the start.
  **This is how to declare the structure "capital in EUR + USD overdraft"** of an account that
  never converts its capital nor sells its dollar P&L; `fx_base` stays near zero without any
  order. (`unhedged` with `never` is refused: unhedged means converting the capital.)

Both sides apply the same rule to their own state: the reference to `(E, U)`, the account to
its own split (below). An order whose notional is under `min_order_notional` is not made on
either side.

## The reference in the base currency (`fxpolicy.FxBook`)

One stepping function serves the runner's reference and a backtest's NAV series
(`apply_fx_policy`), so that both follow the same arithmetic. Day `t`, from the close of `t-1`:

1. **carry**: `E += (E - O) x (i_base - i_usd) x days / 360` with the rates as of `t-1` (no
   look ahead). `O` (`FxBook.outside`) is the base currency the reference holds beyond the
   capital: an account whose cash serves other uses (1 000 000 EUR for 110 000 EUR of capital)
   starts with `O = E - capital` (in base), and that cash is in the value but earns no carry;
   the carry is on the capital's part held in the base currency. Holding base currency instead of dollars earns the difference of the short rates:
   it is the cost (or the gain) of a hedge, the same as a forward's points under covered
   interest parity; the cross-currency basis is not modelled;
2. **the order decided at `t-1`** fills at `t`'s open `o` of the pair (the close of `t-1` when
   no open is known): `E += q`, `U -= q x o + fee`, `fee` = IDEALPRO's commission (0.2 bp,
   at least 2 USD) plus the half-spread;
3. **the strategies' P&L**: `U += expected change` (the USD reference of `tracking.py`,
   unchanged);
4. **value**: `E + U / x(t)`; then at the close the policy decides the next order.

On the first day the reference starts from the account's own split (`U` = the account's
dollar exposure `A` below, `E` = the rest of its value), so that both start equal and make the
same first decision. A reference saved before `O` existed gets `O = max(0, E - capital)` on its
next day.

## The live account

The account's split is read **currency by currency off its items** (`fxpolicy.account_split`),
not as a difference with the NAV:

* `B` (base units): the cash balance **and the accrued interest** in the base currency, plus the
  hedge it holds through the pair (`SimAccount` keeps the pair as a position; at IBKR the pair
  settles into the base cash: `hedge_in_base_cash`, **to check on the paper account before any
  order is sent**);
* `A` (the dollar exposure, USD): the cash balances and accrued interest of every other
  currency, converted to USD (IB's `CashBalance`, `AccruedCash` and `ExchangeRate` by
  currency), plus the positions at the closes; forex pairs that settle into the cash balances
  (IBKR) are not counted twice. IB reports the interest accrued on the EUR cash in the NAV
  (`AccruedCash`) but not in `CashBalance`: read as `NAV - CashBalance x x`, it looked like
  dollars and the hedge proposed to sell dollars the account did not have (+272 EUR on
  2026-11-24, a month of interest by the next monthly date). When IB gives the accrued interest
  only in total (`BASE`), it is counted in the base currency;
* `fx_unexplained = NAV - (B x x + A)`: what the items do not account for (marks that differ,
  a position without a close, an instrument whose value the items miss, such as a future's
  notional). Above `max(min_order_notional, 0.1 % of the NAV)` the live hedge order is
  withheld and the `fx_policy` alert says why. A broker that gives no cash by currency
  (Kraken) keeps the old reading `A = NAV - B x x`. The runner proposes the hedge order (same rule as the
reference, on `A`) next to the strategies' orders; the hedge's units are tracked in the state
(by the ids of the orders sent) and taken out of the positions the plan and the tracking see.

## The gap in the base currency

The USD columns of `tracking.csv` are unchanged, except that the `fx` term also counts the
hedge's revaluation (`B(t-1) x (x(t) - x(t-1))` plus each hedge fill `q x (x(t) - open)`).
With a policy declared, base-currency columns are added, and they add up exactly to the gap in
base:

| column | definition |
|---|---|
| `nav_expected_base` | the reference's value in base |
| `gap_day_base`, `gap_cum_base` | the account's NAV in base minus the reference's |
| `rounding_base`, `untraded_base`, `timing_base`, `residual_base` | the USD terms divided by `x(t)` |
| `costs_base` | `(USD costs + the reference's hedge fee) / x(t)` |
| `carry_base` | minus the reference's carry (the account books its interest; a simulated account books none) |
| `fx_base` | the translation of the account minus the reference's: `(A - U) x (1/x(t) - 1/x(t-1))` plus the difference of their hedge fills |
| `fx_exposure`, `fx_exposure_expected` | `A` and `U` after the close, in USD |
| `fx_unexplained` | the part of the account's NAV its items by currency do not explain, USD |

`fx_base` tends to zero when the policy is applied on both sides: it is the difference of the
two dollar exposures times the move, and the two exposures differ only by the gap in USD.

## Tests (`tests/test_live_fx_policy.py`)

* base EUR unhedged: the reference's value is the USD reference converted at the day's rate
  (after the first-day conversion);
* hedged: after each rebalance the reference's and the account's dollar exposures are about
  zero, and the hedge order is on `EURUSD`;
* carry and fees are counted (a constant rate differential gives `E x diff x days / 360`); on an
  account of 1 000 000 EUR for 110 000 EUR of capital the carry is the capital's, the same as
  on an account of 110 000 EUR;
* accrued interest: an IB EUR account with interest accrued in EUR (values of the paper account
  on 2026-11-24) has no dollar exposure and no hedge order; on a replay, an account accruing
  EUR interest has the same exposure and hedge orders as one that does not;
* `never`: no currency order is proposed on a replay, `fx_base` stays near zero, the dollar
  exposure is the strategies' P&L on both sides;
* the short rates: a local server that answers only a descriptive agent serves them; a series
  that hangs times out, and the session raises the `fx_policy` alert;
* coherence: on a simulated replay the account in EUR under either policy tracks its reference
  with `fx_base` about zero, and `apply_fx_policy` on the reference's USD series gives the
  runner's reference;
* default: no declaration, the run is unchanged (existing tests).

No backtrader behaviour is concerned (the runner has no counterpart in it): no black-box probe.

## Open points (for Édouard)

1. The policy for Phase 1 (decision before 2027-01-15; study:
   `phase1/fx_hedge_study_2026-11-24.md`).
2. How IBKR reports a hedge on `EUR.USD` in an EUR account (base cash, a forex position, or
   both): check with one paper order before any `--send` with a hedge.
3. The sleeves that trade `EURUSD` themselves share the pair with the hedge; the hedge's units
   are kept apart by their order ids.
