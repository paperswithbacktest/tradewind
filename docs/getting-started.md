# Getting started

## Install

```bash
pip install tradewind
```

Python 3.10 or newer. The only dependencies are numpy and pandas; nothing needs to be
compiled.

## A first backtest

A backtest has four parts: a **strategy** (what to do on each bar), one or more **data
feeds** (the bars), a **broker** (cash, positions, fills, costs) and **Cerebro**, which runs
the loop.

```python
import pandas as pd
import tradewind as tw


class SmaTrend(tw.Strategy):
    params = (("period", 50),)

    def __init__(self):
        # built once, before the run: a moving average of the close
        self.sma = tw.ind.SMA(self.data.close, period=self.p.period)

    def next(self):
        # called once per bar; [0] is the current bar, [-1] the previous one
        if self.data.close[0] > self.sma[0]:
            self.order_target_percent(target=0.95)
        else:
            self.order_target_percent(target=0.0)


prices = pd.read_csv("prices.csv", index_col="date", parse_dates=True)  # open high low close volume

cerebro = tw.Cerebro()
cerebro.adddata(tw.feeds.PandasData(dataname=prices), name="SPY")
cerebro.addstrategy(SmaTrend, period=50)
cerebro.broker.setcash(100_000)
cerebro.broker.setcommission(commission=0.001)   # 0.1 % of each fill's value

strategy = cerebro.run()[0]
print(cerebro.broker.getvalue())
```

The same strategy on synthetic data, with nothing to download, is
[`examples/01_sma_crossover.py`](examples/01_sma_crossover.py).

## What happened

- `__init__` ran once. The SMA was computed over the whole series right away; the strategy
  reads it bar by bar through `self.sma[0]`.
- For the first 49 bars the SMA has no value yet, so Cerebro called `prenext()` (empty by
  default) instead of `next()`. From bar 50 on, `next()` runs on every bar.
- `order_target_percent(target=0.95)` compared 95 % of the portfolio value with the current
  position and placed a market order for the difference, in whole shares.
- That order filled at the **next bar's open**, after a cash check. A strategy never trades
  at a price it has already seen, unless it asks for it (`set_coc`, see
  [Orders and the broker](orders-and-broker.md)).
- `getvalue()` is the cash plus the positions valued at the last bar's close.

## Reading results

```python
cerebro.addanalyzer(tw.analyzers.DrawDown, _name="dd")
cerebro.addanalyzer(tw.analyzers.SharpeRatio, _name="sharpe", timeframe=tw.TimeFrame.Days,
                    annualize=True)
strategy = cerebro.run()[0]
print(strategy.analyzers.dd.get_analysis()["max"]["drawdown"])   # percent
print(strategy.analyzers.sharpe.get_analysis()["sharperatio"])
```

Analyzers are added before `run()` and read from the strategy afterwards. For the value
on every bar, record it yourself in `next()` (`self.broker.getvalue()`) or add
`tw.TimeReturn`. See [Analyzers](analyzers.md).

## Next

- [How a backtest runs](concepts.md) explains the order of events inside a bar. It is
  short, and it answers most "why did my order fill there" questions.
- Coming from backtrader? Replace the import and read
  [Migrating from backtrader](migrating-from-backtrader.md).
