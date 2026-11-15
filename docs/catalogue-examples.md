# Examples from the catalogue

[Papers With Backtest](https://paperswithbacktest.com/strategies) publishes a catalogue of
about 4 200 trading strategies, each taken from a research paper, implemented and
backtested on real data. Those implementations are written against the backtrader API, and
Tradewind was built against them: it was specified from what they call, and checked against
their results, bar by bar (identical NAVs on about 1 000 of them so far). They are the
largest body of real Tradewind strategies there is.

Each card on the website gives the paper, the rules as the catalogue implemented them, the
instruments and dates, and the backtest's statistics. The code of a card is part of the
catalogue and is not reproduced here; the sketches below are short illustrations written for
this page from the papers' published rules, on whatever feeds you add.

!!! note "Links checked"
    Every link on this page answered HTTP 200 when the page was written, and the
    documentation's CI checks them again on every build (`tools/check_docs.py --urls`).

## A selection

| strategy | idea | Tradewind features it exercises |
|---|---|---|
| [Time Series Momentum](https://paperswithbacktest.com/strategies/time-series-momentum) | each instrument long if its own past-year return is positive, short if negative | several feeds, monthly timer, `order_target_percent` with signed targets |
| [Equity Returns at the Turn of the Month](https://paperswithbacktest.com/strategies/equity-returns-at-the-turn-of-the-month) | hold equities from the last trading day of a month to the third trading day of the next | calendar logic on dates, [exchange calendars](calendars-and-clock.md) to find the last session |
| [Momentum Effects in Country Equity Indices](https://paperswithbacktest.com/strategies/momentum-effects-in-country-equity-indexes) | each month, hold the four country indices with the best past-11-month return | ranking across feeds, sells before buys in one batch |
| [Breadth Momentum and Vigilant Asset Allocation (VAA)](https://paperswithbacktest.com/strategies/breadth-momentum-and-vigilant-asset-allocation-vaa-winning-more-by-losing-less) | dual momentum whose crash protection is driven by how many assets of the universe have negative momentum | a risky and a defensive universe, a custom momentum score, monthly rebalance |
| [Pairs Trading: Performance of a Relative Value Arbitrage Rule](https://paperswithbacktest.com/strategies/pairs-trading-performance-of-a-relative-value-arbitrage-rule) | match stocks by the distance between their normalised prices, open when a pair diverges by two standard deviations, close when it converges | long and short legs, [realistic models](realistic-models.md) for borrow fees and spreads |
| [The Low Volatility Anomaly in Equity Sectors: 10 Years Later](https://paperswithbacktest.com/strategies/the-low-volatility-anomaly-in-equity-sectors-years-later) | rank stocks by volatility within each sector rather than across the market | `StdDev` indicators, a [point-in-time universe](universe-and-corporate-actions.md) to avoid survivors only |
| [Reversing the Trend of Short-Term Reversal](https://paperswithbacktest.com/strategies/reversing-the-trend-of-short-term-reversal) | buy last month's losers and sell its winners, after removing the industry and factor momentum the plain reversal fights against | cross-sectional signals, many feeds, [Performance](analyzers.md#performance) turnover and capacity |
| [The Seasonality of Bitcoin](https://paperswithbacktest.com/strategies/the-seasonality-of-bitcoin) | hold bitcoin only during the short window of the day where its returns concentrate | the `CRYPTO` calendar, minute bars and [consolidation](intraday.md), timers in exchange time |

## Two sketches

Both run on any set of daily feeds, for instance the synthetic ones of the
[runnable examples](examples.md). They are illustrations of the rules, not the catalogue's
implementations: no costs beyond the defaults, no universe, no data cleaning.

Time series momentum, one signal per instrument:

```python
import tradewind as tw

class TimeSeriesMomentum(tw.Strategy):
    params = (("lookback", 252), ("weight", 0.2))

    def __init__(self):
        self.past = {d: tw.ind.ROC(d.close, period=self.p.lookback) for d in self.datas}
        self.add_timer(when=tw.Timer.SESSION_START, monthdays=[1])

    def notify_timer(self, timer, when, *args, **kwargs):
        for d in self.datas:          # long the risers, short the fallers, monthly
            sign = 1 if self.past[d][0] > 0 else -1
            self.order_target_percent(d, target=sign * self.p.weight)
```

Country momentum, a ranking across feeds:

```python
class TopCountries(tw.Strategy):
    params = (("lookback", 231), ("hold", 4))        # about 11 months of sessions

    def __init__(self):
        self.past = {d: tw.ind.ROC(d.close, period=self.p.lookback) for d in self.datas}
        self.add_timer(when=tw.Timer.SESSION_START, monthdays=[1])

    def notify_timer(self, timer, when, *args, **kwargs):
        ranked = sorted(self.datas, key=lambda d: self.past[d][0], reverse=True)
        for d in ranked[self.p.hold:]:               # sells first: they fund the buys
            self.order_target_percent(d, target=0.0)
        for d in ranked[: self.p.hold]:
            self.order_target_percent(d, target=0.98 / self.p.hold)
```

## Running a catalogue strategy

A catalogue strategy written for backtrader runs on Tradewind after one change, its import
line (`import tradewind as bt`); [Migrating from backtrader](migrating-from-backtrader.md)
lists what else to know. The data the cards use comes from the Papers With Backtest
datasets; see the [datasets documentation](https://paperswithbacktest.com/docs/datasets/).
