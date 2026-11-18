"""Time-series momentum (trend following) across eight asset-class ETFs: each month, long an
ETF whose past twelve-month return is positive, short it otherwise, each position sized to
10 % annual volatility (63-day estimate) and capped at an eighth of the portfolio. Data:
Papers With Backtest (see _pwb_data.py)."""

import math

import tradewind as tw

ASSETS = ["SPY", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]
BENCHMARK = "SPY"
START, END = "2006-02-03", "2026-10-31"
COMMISSION = 0.0005
TITLE = "Trend following (12-month sign), 8 asset-class ETFs, long/short, monthly"
UNIVERSE = "SPY, EFA, EEM, TLT, IEF, GLD, DBC, VNQ; SPY also the benchmark"


class TimeSeriesMomentum(tw.Strategy):
    params = (("lookback", 252), ("window", 63), ("target_vol", 0.10), ("cap", 0.12))

    def __init__(self):
        self.month = None

    def next(self):
        month = self.datetime.date(0).month
        if month == self.month:
            return
        self.month = month
        p = self.p
        if len(self) <= p.lookback:
            return
        target = {}
        for d in self.datas:
            trend = d.close[0] / d.close[-p.lookback] - 1
            closes = d.close.get(size=p.window + 1)
            rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
            mean = sum(rets) / len(rets)
            vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets)) * math.sqrt(252)
            size = min(p.target_vol / vol / len(self.datas), p.cap) if vol > 0 else 0.0
            target[d] = size if trend > 0 else -size
        value = self.broker.getvalue()
        for d in sorted(self.datas,
                        key=lambda d: target[d] - self.getposition(d).size * d.close[0] / value):
            self.order_target_percent(d, target=target[d])


def build(frames):
    engine = tw.Engine()
    for symbol in ASSETS:
        engine.adddata(tw.feeds.PandasData(dataname=frames[symbol]), name=symbol)
    engine.addstrategy(TimeSeriesMomentum)
    engine.addanalyzer(tw.analyzers.Performance, _name="performance", benchmark=BENCHMARK)
    engine.broker.setcash(1_000_000)
    engine.broker.setcommission(commission=COMMISSION)
    return engine


if __name__ == "__main__":
    from _pwb_data import daily_bars

    strategy = build(daily_bars(ASSETS, START, END)).run()[0]
    performance = strategy.analyzers.performance
    report = performance.report
    print(f"CAGR {report.cagr:.2%}, Sharpe {report.sharpe:.2f}, "
          f"max drawdown {report.max_drawdown:.1%}, final value {performance.values[-1]:,.0f}")
    performance.plot(title=TITLE).savefig("trend_following.png", dpi=110)
