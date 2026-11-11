# Indicators

## Built-in indicators

Available as `tw.indicators.X` or the short alias `tw.ind.X`:

| indicator | aliases | parameters | lines |
|---|---|---|---|
| `SimpleMovingAverage` | `SMA`, `MovingAverageSimple` | `period=30` | `sma` |
| `ExponentialMovingAverage` | `EMA`, `MovingAverageExponential` | `period=30`, `alpha=None` (2 / (period + 1)) | `ema` |
| `Highest` | `MaxN` | `period=1` | `highest` |
| `Lowest` | `MinN` | `period=1` | `lowest` |
| `RateOfChange` | `ROC` | `period=12` | `roc` = (x − x[−p]) / x[−p] |
| `TrueRange` | | | `tr` |
| `AverageTrueRange` | `ATR` | `period=14` (Wilder smoothing) | `atr` |
| `RelativeStrengthIndex` | `RSI` | `period=14`, `safediv=False` | `rsi` |
| `MACD` | | `period_me1=12`, `period_me2=26`, `period_signal=9` | `macd`, `signal` |
| `CrossOver`, `CrossUp`, `CrossDown` | | two inputs | `crossover`: +1 / −1 / 0 |
| `StandardDeviation` | `StdDev` | `period=20` | `stddev` (population) |

```python
def __init__(self):
    self.fast = tw.ind.EMA(self.data.close, period=12)
    self.slow = tw.ind.EMA(self.data.close, period=26)
    self.cross = tw.ind.CrossOver(self.fast, self.slow)
    self.atr = tw.ind.ATR(self.data, period=14)          # reads high, low and close

def next(self):
    if self.cross[0] > 0:
        size = int(0.01 * self.broker.getvalue() / self.atr[0])
        self.buy(size=size)
```

An indicator takes a line (`self.data.close`) or a feed (`self.data`, read as its close,
or as high/low/close for the range-based ones). Its first value comes after its warm-up,
and the strategy's `next()` waits for it ([How a backtest runs](concepts.md)).

The arithmetic is chosen to give the same floating-point results as backtrader, to the last
bit: a window sum is a compensated sum, a smoothing step is `prev * (1 - a) + x * a`. It
matters more than it seems: a strategy that compares a price with its average with `>`
flips on a 1e-14 difference whenever the series is flat.

## Writing an indicator

Declare its lines and parameters; compute either bar by bar in `next()` or the whole series
at once in `once()`.

```python
class Momentum(tw.Indicator):
    lines = ("mom",)
    params = (("period", 20),)

    def __init__(self):
        self.addminperiod(self.p.period + 1)      # first value on bar period + 1

    def next(self):
        self.lines.mom[0] = self.data[0] / self.data[-self.p.period] - 1.0
```

```python
class Momentum(tw.Indicator):              # same, vectorised
    lines = ("mom",)
    params = (("period", 20),)

    def __init__(self):
        self.addminperiod(self.p.period + 1)

    def once(self, start, end):
        x = self.data.array                # the whole input series, as a numpy array
        p = self.p.period
        out = self.lines.mom.array
        out[:] = np.nan                    # no value during the warm-up
        out[p:] = x[p:] / x[:-p] - 1.0
```

- `self.data` is the indicator's first input, `self.datas` all of them.
- `addminperiod(n)` adds `n - 1` bars to the warm-up the inputs already impose;
  `updateminperiod(n)` raises it to at least `n`.
- With `once()`, the whole series is computed in one call before the run starts
  (`start` is 0, `end` the series length): fill the whole line, NaN where there is no
  value yet. `next()` is then only used when the strategy runs with `runonce=False`.
- An indicator with only `next()` is, by default (`runonce=True`), stepped over the whole
  series before the run. During that pass every feed that is **not one of its inputs** sits
  at its last bar: an indicator that reads another feed through a parameter sees one
  constant value (backtrader does the same). Pass that feed as an input
  (`MyIndicator(self.data0, self.data1)`), or run with `tw.Engine(runonce=False)`.
- An indicator can also be defined by binding lines built from other indicators, with no
  `next()` nor `once()`:

```python
class Spread(tw.Indicator):
    lines = ("spread",)

    def __init__(self):
        self.lines.spread = tw.ind.SMA(self.data0, period=5) - tw.ind.SMA(self.data1, period=5)
```

## Line arithmetic

In a strategy's or an indicator's `__init__`, operators on lines build new lines; `tw.If`
is the conditional:

```python
def __init__(self):
    hl = self.data.high - self.data.low
    self.range_pct = hl / self.data.close
    self.signal = tw.If(self.data.close > tw.ind.SMA(self.data.close, period=200), 1.0, -1.0)
```

A built line takes the largest warm-up of its operands and is NaN before it. Comparisons
give 1.0 or 0.0. See [Two stages](concepts.md#two-stages-building-and-stepping).

See [`examples/04_custom_indicator.py`](examples/04_custom_indicator.py).
