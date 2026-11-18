"""Multi-asset risk parity, the naive kind: each month, weight six ETFs (US and developed
equities, long and medium Treasuries, gold, commodities) in inverse proportion to their
volatility over the past 63 days. Data: Papers With Backtest (see _pwb_data.py)."""

import math

import tradewind as tw

ASSETS = ["SPY", "EFA", "TLT", "IEF", "GLD", "DBC"]
BENCHMARK = "SPY"
START, END = "2006-02-03", "2026-10-31"      # DBC's first bar
COMMISSION = 0.0005
TITLE = "Inverse-volatility risk parity, 6 ETFs, monthly"
UNIVERSE = "SPY, EFA, TLT, IEF, GLD, DBC; SPY also the benchmark"


class InverseVolatility(tw.Strategy):
    params = (("window", 63), ("invested", 0.97))

    def __init__(self):
        self.month = None

    def next(self):
        month = self.datetime.date(0).month
        if month == self.month:
            return
        self.month = month
        p = self.p
        if len(self) <= p.window:
            return
        inverse = {}
        for d in self.datas:
            closes = d.close.get(size=p.window + 1)
            rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
            mean = sum(rets) / len(rets)
            vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / len(rets))
            inverse[d] = 1.0 / vol
        total = sum(inverse.values())
        target = {d: p.invested * w / total for d, w in inverse.items()}
        value = self.broker.getvalue()
        for d in sorted(self.datas,
                        key=lambda d: target[d] - self.getposition(d).size * d.close[0] / value):
            self.order_target_percent(d, target=target[d])


def build(frames):
    engine = tw.Engine()
    for symbol in ASSETS:
        engine.adddata(tw.feeds.PandasData(dataname=frames[symbol]), name=symbol)
    engine.addstrategy(InverseVolatility)
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
    performance.plot(title=TITLE).savefig("risk_parity.png", dpi=110)
