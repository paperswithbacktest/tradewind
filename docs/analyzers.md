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

### `Performance`

The statistics of a backtest report in one analyzer: return, risk, benchmark, drawdown,
exposure, turnover and capacity.

```python
engine.addanalyzer(tw.analyzers.Performance, _name="performance", benchmark="SPY")
strategy = engine.run()[0]
report = strategy.analyzers.performance.report      # a PerformanceReport (typed fields)
report.sharpe, report.max_drawdown, report.beta, report.capacity
strategy.analyzers.performance.get_analysis()["sharpe"]   # the same, as a dict
strategy.analyzers.performance.values                     # the NAV, one value per slice
```

It reads the portfolio value marked at every slice (warm-up slices included, as a NAV
does) and never changes the run: the values are identical with or without it.

| field | definition |
|---|---|
| `total_return` | last value / first value − 1 |
| `cagr` | (last / first) ^ (1 / years) − 1, years = (bars − 1) / `periods_per_year` |
| `volatility` | population standard deviation of the bar returns × √`periods_per_year` |
| `sharpe` | mean / population deviation of the bar returns (less `risk_free_rate` / `periods_per_year`) × √`periods_per_year`; 0 when the returns do not vary |
| `sortino` | mean excess return / √(Σ min(0, r − rf)² / n) × √`periods_per_year` |
| `probabilistic_sharpe` | probability that the true Sharpe ratio exceeds `psr_benchmark` given the record's length, skewness and kurtosis (Bailey and López de Prado, 2012) |
| `max_drawdown`, `max_drawdown_duration` | deepest fall from a running peak, as a positive fraction; longest run of bars spent below a previous peak |
| `alpha`, `beta` | CAPM regression of the bar returns on the benchmark's (the closes of the feed named `benchmark`): beta = population covariance / variance, alpha = (mean return − beta × the benchmark's) × `periods_per_year`; None without a benchmark |
| `gross_exposure`, `net_exposure`, `long_exposure`, `short_exposure` | Σ\|position value\|, Σ position value, the longs, the shorts, as a fraction of the portfolio value, averaged over the slices |
| `turnover` | traded value / (2 × portfolio value), averaged over the slices × `periods_per_year`: a full switch of the book once a year is 1.0 |
| `capacity` | the portfolio value at which 5 % of the traded dollars (`capacity_quantile`) would exceed 1 % (`participation`) of the security's average dollar volume over 20 bars (`adv_window`); None without volume |
| `exposure_by_currency`, `exposure_by_asset_class` | the net exposure split by the security's quote currency and by asset class (a feed's `asset_class` attribute, `option` for option contracts, else `unclassified`) |
| `fills`, `bars`, `start`, `end` | the run's extent |

Parameters: `benchmark` (a feed name or feed, default None), `periods_per_year` (252),
`risk_free_rate` (0.0, annual), `psr_benchmark` (0.0, an annual Sharpe ratio),
`participation` (0.01), `adv_window` (20), `capacity_quantile` (0.05). The functions behind
it are public: `tw.metrics.sharpe_ratio(values)`, `max_drawdown`, `alpha_beta`,
`probabilistic_sharpe_ratio`, `estimated_capacity`...

**The catalogue's definitions.** The Sharpe ratio, the volatility and the drawdown depth are
those of the Papers With Backtest catalogue (daily NAV, population deviation, √252, no
risk-free rate), and every formula shared with pwb-toolbox (`pwb_toolbox.performance`)
gives the same float to the last bit (`tests/test_metrics.py`). The differences:

| | Tradewind | pwb-toolbox | catalogue |
|---|---|---|---|
| drawdown depth | positive fraction | negative fraction | positive fraction |
| drawdown duration | longest run of bars strictly below a previous peak | also counts bars equal to the peak and the first bar (1 on a series that never falls) | not reported |
| annual return | `cagr`, over bars − 1 periods | same | `(last / first) ^ (252 / bars) − 1`, over bars periods |
| alpha | annualised (× `periods_per_year`), the per-period one in `tw.metrics.alpha_beta(...).alpha_per_period` | per period | not reported |
| benchmark alignment | the slices where the benchmark has a positive close | the first `min(len)` values of each series | not reported |
| ruined account (last value ≤ 0) in `cagr` | −1.0 | a complex number | NaN |
| PSR, capacity, exposure, turnover | yes | no | no |

Compared with the `SharpeRatio` analyzer (backtrader's, kept as it is): that one defaults
to yearly returns and a 1 % risk-free rate; `Performance.sharpe` is the catalogue's figure.

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
