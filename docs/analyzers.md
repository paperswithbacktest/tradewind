# Analyzers

An analyzer watches the run and summarises it. Add it before `run()`, with a name, and read
it from the strategy afterwards:

```python
engine.addanalyzer(tw.analyzers.DrawDown, _name="dd")
strategy = engine.run()[0]
result = strategy.analyzers.dd.get_analysis()
```

## Built-in analyzers

### `DrawDown`

```python
dd = strategy.analyzers.dd.get_analysis()
dd["drawdown"], dd["moneydown"], dd["len"]                 # at the end of the run
dd["max"]["drawdown"], dd["max"]["moneydown"], dd["max"]["len"]   # the worst over the run
```

`drawdown` is a percentage of the running peak of the portfolio value, `moneydown` the
same in cash, `len` the number of bars since the peak. Every bar counts, `prenext` bars
included.

### `SharpeRatio`

```python
engine.addanalyzer(tw.analyzers.SharpeRatio, _name="sharpe",
                    timeframe=tw.TimeFrame.Days, riskfreerate=0.0, annualize=True)
strategy.analyzers.sharpe.get_analysis()["sharperatio"]
```

Returns per period (`timeframe`: days, weeks, months or years), the period's last value
over the previous period's. `riskfreerate` is annual; `annualize=True` scales by
√252, √52 or √12. The standard deviation is the population one unless
`stddev_sample=True`. The result is `None` when returns do not vary. The default
timeframe is years.

### `TradeAnalyzer`

```python
ta = strategy.analyzers.trades.get_analysis()
ta.total.closed, ta.won.total, ta.lost.total
ta.pnl.net.total, ta.won.pnl.average, ta.lost.pnl.max
ta.len.average                                   # bars held
```

A tree of counts and P&L statistics over closed trades, split by won and lost, long and
short. It only holds `total` until the first trade closes. A reversing fill (long to short)
closes one trade and opens another.

### `TimeReturn`

`tw.TimeReturn`: the portfolio's return on each bar, keyed by date.

## Writing an analyzer

```python
class Exposure(tw.Analyzer):
    def start(self):
        self.gross = []

    def next(self):
        value = self.strategy.broker.getvalue()
        held = sum(abs(self.strategy.broker.get_value(datas=[d])) for d in self.strategy.datas)
        self.gross.append(held / value if value else 0.0)

    def get_analysis(self):
        return {"average_gross": sum(self.gross) / max(len(self.gross), 1)}
```

`start()`, `prenext()`, `next()`, `stop()`, `notify_order()` and `notify_trade()` are
called on the same schedule as the strategy's; `self.strategy` is the strategy.
