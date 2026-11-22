# Orders, broker and models

The simulated broker, commission schemes and the pluggable reality models with their
defaults (the defaults reproduce backtrader). Guide: [Orders and the broker](../orders-and-broker.md).

::: tradewind.broker

::: tradewind.comminfo

::: tradewind.models

::: tradewind.securities

## Fractional sizes (crypto)

The stock-like schemes above size `order_target_percent` and `order_target_value` in whole
units. `FractionalCommInfo` sizes to 1e-8 of a coin and charges a percentage fee (0.40 % by
default, Kraken's entry taker fee): install it with
`engine.broker.addcommissioninfo(FractionalCommInfo())`, in the backtest and live alike.
Guide: [live trading on Kraken](../live-kraken.md#crypto-is-not-a-stock-market).

::: tradewind.live.kraken.FractionalCommInfo
