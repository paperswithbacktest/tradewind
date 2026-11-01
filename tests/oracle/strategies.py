"""Strategies run under both engines by ``tools/run_oracle.py``.

``bt`` is whichever engine the runner injects (backtrader as the oracle, Tradewind as the
candidate). Each strategy records the NAV at every ``next`` and every completed fill, so
the two runs can be compared line by line. They exercise the Tier 1 and Tier 2 idioms of
``spec/01-scope.md``; nothing here reads engine internals.
"""


def build(bt):
    class Recorder(bt.Strategy):
        def __init__(self):
            self.values = []
            self.fills = []
            self.statuses = []

        def next(self):
            self.values.append((self.datas[0].datetime.date(0).isoformat(), round(self.broker.getvalue(), 6)))

        def notify_order(self, order):
            self.statuses.append((order.ref, order.getstatusname()))
            if order.status == order.Completed:
                self.fills.append((self.datas[0].datetime.date(0).isoformat(), order.data._name,
                                   float(order.executed.size), round(float(order.executed.price), 8)))

    class MonthlyEqualWeight(Recorder):
        params = (("target", 0.95),)

        def __init__(self):
            super().__init__()
            self.last = None

        def next(self):
            super().next()
            today = self.datas[0].datetime.date(0)
            if (today.year, today.month) != self.last:
                self.last = (today.year, today.month)
                for d in self.datas:
                    self.order_target_percent(d, target=self.p.target / len(self.datas))

    class TargetSizeLadder(Recorder):
        def next(self):
            super().next()
            n = len(self)
            if n % 7 == 1:
                self.order_target_size(self.datas[0], target=10 * (n // 7 + 1))
            if n % 11 == 0:
                self.order_target_value(self.datas[1], target=20_000)
            if n == 30:
                self.close(self.datas[0])

    class LongShortClose(Recorder):
        def next(self):
            super().next()
            n = len(self)
            if n == 2:
                self.buy(data=self.datas[0], size=50)
                self.sell(data=self.datas[1], size=30)
            if n == 20:
                self.close(self.datas[0])
                self.close(self.datas[1])
            if n == 25:
                self.sell(data=self.datas[2], size=40)
            if n == 40:
                self.order_target_size(self.datas[2], target=0)

    class LimitAndStop(Recorder):
        def next(self):
            super().next()
            n = len(self)
            c = self.datas[0].close[0]
            if n == 3:
                self.buy(data=self.datas[0], size=10, exectype=bt.Order.Limit, price=c * 0.98)
            if n == 10:
                self.buy(data=self.datas[0], size=10, exectype=bt.Order.Stop, price=c * 1.02)
            if n == 20:
                self.sell(data=self.datas[0], size=5, exectype=bt.Order.Limit, price=c * 1.03)
            if n == 30:
                self.sell(data=self.datas[0], size=5, exectype=bt.Order.Stop, price=c * 0.97)
            if n == 45:
                self.buy(data=self.datas[1], size=7, exectype=bt.Order.Close)

    class Lookback(Recorder):
        def next(self):
            super().next()
            if len(self) < 6:
                return
            d = self.datas[0]
            mom = d.close[0] / d.close[-5] - 1.0
            pos = self.getposition(d).size
            if mom > 0 and pos == 0:
                self.order_target_percent(d, target=0.5)
            elif mom < 0 and pos != 0:
                self.order_target_percent(d, target=0.0)

    class AddCashAndMargin(Recorder):
        def next(self):
            super().next()
            n = len(self)
            if n == 2:
                self.buy(data=self.datas[0], size=5_000)  # far more than the cash: Margin
            if n == 5:
                self.broker.add_cash(50_000)
            if n == 6:
                self.buy(data=self.datas[0], size=100)

    class Commissioned(MonthlyEqualWeight):
        pass

    class SmaCross(Recorder):
        params = (("fast", 5), ("slow", 20))

        def __init__(self):
            super().__init__()
            self.fast = bt.indicators.SMA(self.datas[0], period=self.p.fast)
            self.slow = bt.indicators.SMA(self.datas[0], period=self.p.slow)
            self.cross = bt.indicators.CrossOver(self.fast, self.slow)
            self.hi = bt.indicators.Highest(self.datas[1].close, period=10)
            self.roc = bt.indicators.ROC(self.datas[2], period=5)

        def next(self):
            super().next()
            d = self.datas[0]
            if self.cross[0] > 0:
                self.order_target_percent(d, target=0.6)
            elif self.cross[0] < 0:
                self.order_target_percent(d, target=0.0)
            if self.datas[1].close[0] >= self.hi[0] and not self.getposition(self.datas[1]).size:
                self.buy(data=self.datas[1], size=20)
            if self.roc[0] < -0.05 and self.getposition(self.datas[1]).size:
                self.close(self.datas[1])

    class Rsi(Recorder):
        def __init__(self):
            super().__init__()
            self.rsi = bt.indicators.RSI(self.datas[0], period=14)
            self.ema = bt.indicators.EMA(self.datas[0], period=10)
            self.atr = bt.indicators.ATR(self.datas[0], period=14)
            self.macd = bt.indicators.MACD(self.datas[1])

        def next(self):
            super().next()
            self.values[-1] = self.values[-1]
            d = self.datas[0]
            if self.rsi[0] < 45 and not self.getposition(d).size:
                self.buy(data=d, size=int(1000 / self.atr[0]))
            elif self.rsi[0] > 55 and self.getposition(d).size:
                self.close(d)
            if self.macd.macd[0] > self.macd.signal[0] and not self.getposition(self.datas[1]).size:
                self.buy(data=self.datas[1], size=10)
            elif self.macd.macd[0] < self.macd.signal[0] and self.getposition(self.datas[1]).size:
                self.close(self.datas[1])

    return {
        "monthly_equal_weight": MonthlyEqualWeight,
        "target_size_ladder": TargetSizeLadder,
        "long_short_close": LongShortClose,
        "limit_and_stop": LimitAndStop,
        "lookback": Lookback,
        "add_cash_and_margin": AddCashAndMargin,
        "commissioned": Commissioned,
        "sma_cross": SmaCross,
        "rsi": Rsi,
    }
