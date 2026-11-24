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
fx_hedge_rebalance: monthly # hedged: when the net exposure is sold again: daily, weekly, monthly
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
  rate gives no carry and a warning, never a stop.

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

Both sides apply the same rule to their own state: the reference to `(E, U)`, the account to
its own split (below). An order whose notional is under `min_order_notional` is not made on
either side.

## The reference in the base currency (`fxpolicy.FxBook`)

One stepping function serves the runner's reference and a backtest's NAV series
(`apply_fx_policy`), so that both follow the same arithmetic. Day `t`, from the close of `t-1`:

1. **carry**: `E += E x (i_base - i_usd) x days / 360` with the rates as of `t-1` (no look
   ahead). Holding base currency instead of dollars earns the difference of the short rates:
   it is the cost (or the gain) of a hedge, the same as a forward's points under covered
   interest parity; the cross-currency basis is not modelled;
2. **the order decided at `t-1`** fills at `t`'s open `o` of the pair (the close of `t-1` when
   no open is known): `E += q`, `U -= q x o + fee`, `fee` = IDEALPRO's commission (0.2 bp,
   at least 2 USD) plus the half-spread;
3. **the strategies' P&L**: `U += expected change` (the USD reference of `tracking.py`,
   unchanged);
4. **value**: `E + U / x(t)`; then at the close the policy decides the next order.

On the first day the reference starts from the account's own split (`E` = the account's base
cash, `U` = the rest), so that both start equal.

## The live account

The account's base units `B` are its cash in the base currency plus the hedge it holds through
the pair (`SimAccount` keeps the pair as a position; at IBKR the pair settles into the base
cash: `hedge_in_base_cash`, **to check on the paper account before any order is sent**). Its
dollar exposure is `A = NAV(USD) - B x x`. The runner proposes the hedge order (same rule as the
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

`fx_base` tends to zero when the policy is applied on both sides: it is the difference of the
two dollar exposures times the move, and the two exposures differ only by the gap in USD.

## Tests (`tests/test_live_fx_policy.py`)

* base EUR unhedged: the reference's value is the USD reference converted at the day's rate
  (after the first-day conversion);
* hedged: after each rebalance the reference's and the account's dollar exposures are about
  zero, and the hedge order is on `EURUSD`;
* carry and fees are counted (a constant rate differential gives `E x diff x days / 360`);
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
