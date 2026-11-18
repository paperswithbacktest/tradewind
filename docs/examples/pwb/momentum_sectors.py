"""Cross-sectional momentum on the nine original US sector ETFs: each month, hold the three
with the best return over the past twelve months skipping the last one (12-1), in equal
weights. Data: Papers With Backtest (see _pwb_data.py)."""

import tradewind as tw

SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]
BENCHMARK = "SPY"
START, END = "1999-01-01", "2026-10-31"
COMMISSION = 0.0005                 # 5 bp of the traded value, on every fill
TITLE = "Sector momentum (12-1), top 3 of 9 SPDR sector ETFs, monthly"
UNIVERSE = "the nine SPDR sector ETFs (XLB, XLE, XLF, XLI, XLK, XLP, XLU, XLV, XLY); SPY as benchmark, not traded"


class SectorMomentum(tw.Strategy):
    params = (("lookback", 252), ("skip", 21), ("hold", 3), ("invested", 0.96))

    def __init__(self):
        self.sectors = [d for d in self.datas if d._name in SECTORS]
        self.month = None

    def next(self):
        month = self.datetime.date(0).month
        if month == self.month:
            return
        self.month = month
        p = self.p
        if len(self) <= p.lookback:
            return
        score = {d: d.close[-p.skip] / d.close[-p.lookback] - 1 for d in self.sectors}
        winners = sorted(self.sectors, key=score.get, reverse=True)[: p.hold]
        target = {d: (p.invested / p.hold if d in winners else 0.0) for d in self.sectors}
        # sells first, so their cash is there for the buys of the same batch
        for d in sorted(self.sectors, key=lambda d: target[d] - self._weight(d)):
            self.order_target_percent(d, target=target[d])

    def _weight(self, data):
        return self.getposition(data).size * data.close[0] / self.broker.getvalue()


def build(frames):
    engine = tw.Engine()
    for symbol in SECTORS + [BENCHMARK]:
        engine.adddata(tw.feeds.PandasData(dataname=frames[symbol]), name=symbol)
    engine.addstrategy(SectorMomentum)
    engine.addanalyzer(tw.analyzers.Performance, _name="performance", benchmark=BENCHMARK)
    engine.broker.setcash(1_000_000)
    engine.broker.setcommission(commission=COMMISSION)
    return engine


if __name__ == "__main__":
    from _pwb_data import daily_bars

    strategy = build(daily_bars(SECTORS + [BENCHMARK], START, END)).run()[0]
    performance = strategy.analyzers.performance
    report = performance.report
    print(f"CAGR {report.cagr:.2%}, Sharpe {report.sharpe:.2f}, "
          f"max drawdown {report.max_drawdown:.1%}, final value {performance.values[-1]:,.0f}")
    performance.plot(title=TITLE).savefig("momentum_sectors.png", dpi=110)
