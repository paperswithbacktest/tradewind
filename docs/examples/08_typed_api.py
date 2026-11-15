"""The typed API: a dataclass of parameters, typed bars and orders, and the run's statistics."""

from dataclasses import dataclass

from _data import bars

import tradewind as tw


@dataclass(frozen=True)
class TrendParams:
    period: int = 100
    weight: float = 0.45          # per instrument, two instruments


class TrendFollowing(tw.TypedStrategy[TrendParams]):
    def initialize(self) -> None:
        self.trend = {s: self.indicator(tw.ind.SMA, s, period=self.params.period) for s in self.symbols}
        self.schedule("rebalance", month_days=(1,))
        self.fills = 0

    def on_timer(self, event: tw.TimerEvent) -> None:
        if self.is_warming_up:
            return
        for symbol in self.symbols:
            up = self.bars[symbol].close[0] > self.trend[symbol][0]
            self.order_target_weight(symbol, self.params.weight if up else 0.0)

    def on_fill(self, fill: tw.Fill) -> None:
        self.fills += 1


engine = tw.Engine()
for i, symbol in enumerate(["SPY", "TLT"]):
    engine.adddata(tw.feeds.PandasData(dataname=bars(symbol, seed=20 + i)), name=symbol)
engine.addstrategy(TrendFollowing, period=120)
engine.addanalyzer(tw.analyzers.Performance, _name="performance", benchmark="SPY")
engine.broker.setcash(1_000_000)
strategy = engine.run()[0]

report = strategy.analyzers.performance.report
print(f"fills {strategy.typed.fills}, final value {engine.broker.getvalue():,.2f}")
print(f"Sharpe {report.sharpe:.2f}, PSR {report.probabilistic_sharpe:.2f}, "
      f"max drawdown {report.max_drawdown:.1%} over {report.max_drawdown_duration} bars, "
      f"beta {report.beta:.2f}, gross exposure {report.gross_exposure:.0%}, "
      f"turnover {report.turnover:.1f}x a year, capacity {report.capacity:,.0f}")
