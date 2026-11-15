# Runnable examples

Every script below runs as it is, on synthetic data (no download, no account): clone the
repository, `pip install -e .`, then `cd docs/examples && python 01_sma_crossover.py`. The
test suite runs each numbered example on every pull request (`tests/test_docs.py`): the
code shown here is the code the tests run, included from the files at build time.

For strategies on real data, see [Examples from the catalogue](catalogue-examples.md).

## A first strategy

`01_sma_crossover.py`: A first strategy: long one instrument while its close is above a 50-day average. Guide: [Getting started](getting-started.md).

??? example "01_sma_crossover.py"

    ```python
    --8<-- "01_sma_crossover.py"
    ```

## Monthly equal-weight rebalance

`02_monthly_rebalance.py`: Several feeds, a monthly equal-weight rebalance, and the order notifications. Guide: [Strategies](strategies.md).

??? example "02_monthly_rebalance.py"

    ```python
    --8<-- "02_monthly_rebalance.py"
    ```

## Timer and limit order

`03_timer_and_limit.py`: A monthly timer, a limit order placed under the close, and a trade notification. Guide: [Strategies: timers](strategies.md#timers).

??? example "03_timer_and_limit.py"

    ```python
    --8<-- "03_timer_and_limit.py"
    ```

## Custom indicators

`04_custom_indicator.py`: A custom indicator with ``next``, another built from line arithmetic, and their warm-up. Guide: [Indicators](indicators.md).

??? example "04_custom_indicator.py"

    ```python
    --8<-- "04_custom_indicator.py"
    ```

## A model for one security

`05_per_security_models.py`: Swap a model for one security only: a fixed per-share slippage on the illiquid name. Guide: [Orders and the broker](orders-and-broker.md).

??? example "05_per_security_models.py"

    ```python
    --8<-- "05_per_security_models.py"
    ```

## Cash-secured put

`06_cash_secured_put.py`: A monthly cash-secured put: sell one put about 5 % out of the money, 20 to 45 days out, hold it to expiry, and if it is assigned, sell the shares the next day. Guide: [Options](options.md).

??? example "06_cash_secured_put.py"

    ```python
    --8<-- "06_cash_secured_put.py"
    ```

## Opening-range breakout on minute bars

`07_opening_range.py`: Opening-range breakout on minute bars: buy when a minute closes above the high of the first 30-minute bar of the session, flat at 15:55. The 30-minute bars come from ``engine.consolidate``; the minute bars are synthetic (no download). Guide: [Intraday data](intraday.md).

??? example "07_opening_range.py"

    ```python
    --8<-- "07_opening_range.py"
    ```

## Typed API

`08_typed_api.py`: The typed API: a dataclass of parameters, typed bars and orders, and the run's statistics. Guide: [Typed API](typed-api.md).

??? example "08_typed_api.py"

    ```python
    --8<-- "08_typed_api.py"
    ```

## The realistic profile

`09_realistic_profile.py`: The realistic profile: measured spreads per symbol and year, volume-capped fills, a Reg T margin account, borrow fees on shorts, T+1/T+2 settlement -- against the oracle defaults. Guide: [Realistic models and currencies](realistic-models.md).

??? example "09_realistic_profile.py"

    ```python
    --8<-- "09_realistic_profile.py"
    ```

## A security in a foreign currency

`10_foreign_currency.py`: A security quoted in euros in a dollar account: cash per currency, the value in dollars. Guide: [Realistic models and currencies](realistic-models.md).

??? example "10_foreign_currency.py"

    ```python
    --8<-- "10_foreign_currency.py"
    ```

## Point-in-time universe and corporate actions

`11_universe_and_corporate_actions.py`: An equal-weight book over a point-in-time universe, on raw prices with corporate actions. Guide: [Universes and corporate actions](universe-and-corporate-actions.md).

??? example "11_universe_and_corporate_actions.py"

    ```python
    --8<-- "11_universe_and_corporate_actions.py"
    ```

## Paper trading on Interactive Brokers

`live_ibkr_paper.py`: Paper trading on Interactive Brokers: a catalogue-style monthly rebalance, run live. Guide: [Live trading (IB)](live-trading.md).

`python live_ibkr_paper.py --fake` runs it against a simulated venue; without
`--fake` it connects to a paper account, read-only unless `--trade`.

??? example "live_ibkr_paper.py"

    ```python
    --8<-- "live_ibkr_paper.py"
    ```

## The synthetic data

`_data.py` builds the bars every example reads: a seeded random walk on business days,
the same numbers on every run.

??? example "_data.py"

    ```python
    --8<-- "_data.py"
    ```
