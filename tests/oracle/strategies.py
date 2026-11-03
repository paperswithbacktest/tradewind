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
            self.statuses.append((order.ref, order.getstatusname(), round(self.broker.getvalue(), 6),
                                  self.datas[0].datetime.date(0).isoformat()))
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

    class BatchFunding(Recorder):
        """Buys that only the sells of the same bar can fund, in both submission orders,
        and two unaffordable buys in a row."""

        def next(self):
            super().next()
            n = len(self)
            d0, d1, d2 = self.datas[0], self.datas[1], self.datas[2]
            cash = self.broker.getcash()
            if n == 3:
                self.buy(data=d0, size=int(0.6 * cash / d0.close[0]))      # affordable
            if n == 10:
                # two buys of 70 % of cash each: only the first can pass
                self.buy(data=d1, size=int(0.7 * cash / d1.close[0]))
                self.buy(data=d2, size=int(0.7 * cash / d2.close[0]))
            if n == 20:
                # buy first (unaffordable alone), then the sell that would fund it
                self.buy(data=d2, size=int(1.5 * cash / d2.close[0]))
                self.sell(data=d0, size=self.getposition(d0).size)
            if n == 30:
                # sell first, then a buy that only the proceeds can fund
                pos1 = self.getposition(d1).size
                if pos1:
                    self.sell(data=d1, size=pos1)
                    self.buy(data=d0, size=int((cash + pos1 * d1.close[0]) * 0.9 / d0.close[0]))
            if n == 40:
                # submission order S2 then S0: which fills first?
                self.buy(data=d2, size=5)
                self.buy(data=d0, size=5)
            if n == 50:
                # an opening short sale, then a buy: does the short consume cash in the
                # batch check, or fund the buy with its proceeds?
                self.sell(data=d2, size=int(0.8 * cash / d2.close[0]))
                self.buy(data=d1, size=int(0.5 * cash / d1.close[0]))
            if n == 60:
                # a short sale alone, larger than the cash: accepted or Margin?
                self.sell(data=d1, size=int(3.0 * cash / d1.close[0]))
            if n == 70:
                # closing the short of S2, then a buy funded by nothing but the cash
                pos2 = self.getposition(d2).size
                if pos2 < 0:
                    self.buy(data=d2, size=-pos2)
                self.buy(data=d0, size=int(0.5 * cash / d0.close[0]))
            if n == 80:
                # nearly all the cash into S0, then a small opening short and a small
                # reduction of S0: which of the two sells survive a near-zero running cash?
                self.buy(data=d0, size=int(0.99 * cash / d0.close[0]))
                self.sell(data=d2, size=10)
                self.sell(data=d0, size=5)
            if n == 90:
                # an opening short, then a buy only its proceeds could fund
                self.sell(data=d2, size=int(0.8 * cash / d2.close[0]))
                self.buy(data=d1, size=int(1.3 * cash / d1.close[0]))

    class DeficitCarry(Recorder):
        """A batch whose running cash goes negative: does the deficit of a refused order
        weigh on the orders after it, and can a sell that reduces a long be refused?"""

        def next(self):
            super().next()
            n = len(self)
            d0, d1, d2 = self.datas[0], self.datas[1], self.datas[2]
            cash = self.broker.getcash()
            if n == 3:
                self.buy(data=d0, size=int(0.5 * cash / d0.close[0]))
                self.buy(data=d1, size=int(0.3 * cash / d1.close[0]))
            if n == 12:
                # nearly everything into S2, then: a buy that overshoots, a tiny buy that
                # alone would fit, a small reduction of S0, a large reduction of S0, then a
                # buy only the large sale can fund, then another overshoot
                self.buy(data=d2, size=int(0.98 * cash / d2.close[0]))
                self.buy(data=d1, size=int(0.10 * cash / d1.close[0]))
                self.buy(data=d1, size=1)
                self.sell(data=d0, size=2)
                self.sell(data=d0, size=int(0.6 * self.getposition(d0).size))
                self.buy(data=d2, size=int(0.05 * cash / d2.close[0]))
                self.buy(data=d1, size=int(2.0 * cash / d1.close[0]))
            if n == 25:
                # the same shape with an opening short sale inside the deficit
                self.buy(data=d1, size=int(3.0 * cash / d1.close[0]))
                self.sell(data=d2, size=int(0.2 * self.getposition(d2).size))
                self.sell(data=d1, size=int(1.5 * self.getposition(d1).size))
                self.buy(data=d0, size=1)

    class LineEdges(Recorder):
        """Reads at the edges of a line buffer: a bar ahead, before the first bar, the raw
        array, a feed that has not listed yet (from prenext). Each reading steers a distinct
        trade so the NAV tells which semantics the engine has."""

        def prenext(self):
            d0, d3 = self.datas[0], self.datas[3]
            if len(self) == 3:
                try:
                    late = d3.close[0]                      # S3 lists 30 bars late
                    self.buy(data=d0, size=int(late) % 5 + 1)
                except IndexError:
                    self.sell(data=d0, size=2)

        def next(self):
            super().next()
            n = len(self)
            d0 = self.datas[0]
            if n == 35:
                try:
                    ahead = d0.close[1] > d0.close[0]       # a bar that has not arrived
                except IndexError:
                    ahead = None
                if ahead is True:
                    self.buy(data=d0, size=10)
                elif ahead is False:
                    self.sell(data=d0, size=10)
            if n == 37:
                try:
                    before = d0.close[-40]                  # before the first bar
                    self.buy(data=d0, size=int(before) % 7 + 1)
                except IndexError:
                    self.sell(data=d0, size=3)
            if n == 39:
                size = len(d0.close.array) % 50 + 1        # raw buffer length during the run
                self.buy(data=d0, size=size)
            if n == 60:
                try:
                    d0.datetime.date(len(d0.close.array) - len(d0) + 1)   # past the buffer
                    self.buy(data=d0, size=4)
                except IndexError:
                    self.sell(data=d0, size=4)

    class ReverseShortfall(Recorder):
        """A short, then buys sized to 99.9 % of the cash at the close that both close the
        short and open a long: when the next open gaps up, what fills?"""

        def next(self):
            super().next()
            n = len(self)
            d0 = self.datas[0]
            cash = self.broker.getcash()
            if n == 3:
                self.sell(data=d0, size=int(0.5 * cash / d0.close[0]))
            if n in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100):
                pos = self.getposition(d0).size
                if pos < 0:
                    self.buy(data=d0, size=int(0.999 * cash / d0.close[0]))
                elif pos > 0:
                    self.sell(data=d0, size=pos + int(0.5 * cash / d0.close[0]))
                else:
                    self.sell(data=d0, size=int(0.5 * cash / d0.close[0]))

    class CheatOnClose(Recorder):
        """``set_coc(True)``: market orders placed in ``next`` fill at that bar's close. When
        are they notified, what do close() and order_target_percent do, and what of a buy
        the cash cannot cover at that close?"""

        def __init__(self):
            super().__init__()
            self.broker.set_coc(True)

        def next(self):
            super().next()
            n = len(self)
            d0, d1 = self.datas[0], self.datas[1]
            cash = self.broker.getcash()
            if n == 33:
                self.buy(data=d0, size=100)
            if n == 34:
                self.sell(data=d0, size=40)
            if n == 36:
                self.close(d0)
            if n == 38:
                self.order_target_percent(d1, target=0.6)
            if n == 42:
                self.order_target_percent(d1, target=0.0)
                self.buy(data=d0, size=int(1.5 * cash / d0.close[0]))      # unaffordable
            if n == 45:
                self.buy(data=d0, size=int(0.5 * cash / d0.close[0]))
                self.sell(data=d1, size=50)                                 # opening short
            if n == 50:
                self.close(d0)
                self.close(d1)

    class AddCashTiming(Recorder):
        """When does ``add_cash`` take effect? Read back in the same bar, seen by the batch
        pre-check of orders placed in the same bar, pulled below zero, mixed with targets."""

        def next(self):
            super().next()
            n = len(self)
            d0, d1 = self.datas[0], self.datas[1]
            if n == 33:
                before = self.broker.getcash()
                self.broker.add_cash(20_000)
                after = self.broker.getcash()
                self.buy(data=d0, size=10 if after > before else 20)       # read back same bar?
                self.values.append(("value_after_add", round(self.broker.getvalue(), 6)))
            if n == 36:
                self.broker.add_cash(30_000)
                cash = self.broker.getcash()
                self.buy(data=d1, size=int(1.2 * cash / d1.close[0]))       # funded only with the addition
            if n == 40:
                self.order_target_percent(d0, target=0.5)
                self.broker.add_cash(-40_000)                               # pulled while orders are in flight
            if n == 45:
                self.broker.add_cash(-10 * self.broker.getcash())           # far below zero
                self.buy(data=d0, size=1)
            if n == 50:
                self.broker.add_cash(500_000)
                self.order_target_percent(d0, target=0.3)
                self.order_target_percent(d1, target=0.3)
            if n == 55:
                self.broker.add_cash(-2 * self.broker.getcash())            # negative again, lands at bar 56
            if n == 57:
                cash = abs(self.broker.getcash())
                self.sell(data=d1, size=100)                                # improves a negative cash
                self.buy(data=d0, size=1)                                   # tiny buy: above the start cash, still below zero
                self.buy(data=d0, size=int(0.5 * cash / d0.close[0]))       # takes the cash below where it started
            if n == 60:
                self.buy(data=d0, size=1)                                   # a lone tiny buy at negative cash

    class CloseThenTarget(Recorder):
        """close() and order_target_percent on the same data in the same bar."""

        def next(self):
            super().next()
            n = len(self)
            d0, d1 = self.datas[0], self.datas[1]
            if n == 2:
                self.order_target_percent(d0, target=0.5)
                self.order_target_percent(d1, target=0.3)
            if n == 15:
                self.close(d0)
                self.order_target_percent(d0, target=0.45)
                self.close(d1)
                self.order_target_percent(d1, target=0.35)
            if n == 30:
                self.order_target_percent(d0, target=0.2)
                self.close(d0)

    class ForeignRead(bt.Indicator):
        """A next-only indicator that reads a feed it was not given as input."""

        lines = ("x",)
        params = (("other", None),)

        def __init__(self):
            pass

        def next(self):
            self.lines.x[0] = self.p.other.close[0]

    class ForeignFeed(Recorder):
        def __init__(self):
            super().__init__()
            self.ind = ForeignRead(self.datas[0], other=self.datas[1])

        def next(self):
            super().next()
            self.statuses.append((0, "IND", round(float(self.ind[0]), 6), self.datas[0].datetime.date(0).isoformat()))


    class LineOps(Recorder):
        """Lines built in ``__init__`` (corpus ``fixing_the_vix_an_indicator_to_beat_fear``):
        operators on an indicator and a feed line, ``bt.If``, an SMA of the result; trades
        on the cross. Also records what ``bt.LineRoot`` / ``bt.LineIterator`` say of each
        kind of object, and that a built comparison has no truth value."""

        def __init__(self):
            super().__init__()
            d = self.datas[0]
            h = bt.ind.Highest(d.close, period=20)
            self.vf = bt.If(h > 0.0, (h - d.low) / h * 100.0, 0.0)
            self.ma = bt.ind.SMA(self.vf, period=20)
            self.spread = 100.0 - abs(d.close - d.open) * 2.0
            cond = h > 0.0
            try:
                bool(cond)
                truth = "bool"
            except TypeError:
                truth = "TypeError"
            kinds = {"feed": d, "line": d.close, "indicator": h, "built": cond, "strategy": self}
            self.extra = {
                "markers": {k: [isinstance(o, bt.LineRoot), isinstance(o, bt.LineIterator)]
                            for k, o in kinds.items()},
                "built_truth": truth,
                "has_get": hasattr(bt, "get"),
                "rows": [],
            }
            self.prev = None

        def next(self):
            super().next()
            d = self.datas[0]
            v, m = float(self.vf[0]), float(self.ma[0])
            self.extra["rows"].append((len(self), v, m, float(self.spread[0]), type(d.close > d.open).__name__))
            diff = v - m
            if self.prev is not None:
                if self.prev >= 0 > diff:
                    self.order_target_percent(d, target=0.9)
                elif self.prev <= 0 < diff:
                    self.close(d)
            self.prev = diff

    class AddsUp(bt.Indicator):
        """A next-only indicator whose two ``addminperiod`` calls add up."""

        lines = ("x",)

        def __init__(self):
            self.addminperiod(20)
            self.addminperiod(14)

        def next(self):
            self.lines.x[0] = self.data.close[0]

    class MinperiodAdd(Recorder):
        """``addminperiod(20)`` then ``addminperiod(14)``: ``next`` from bar 33."""

        def __init__(self):
            super().__init__()
            self.x = AddsUp(self.datas[0])
            self.extra = {"first_next": None}

        def next(self):
            super().next()
            if self.extra["first_next"] is None:
                self.extra["first_next"] = len(self)
                self.extra["x"] = float(self.x[0])

    class MinperiodChain(Recorder):
        """SMA(20) of SMA(15): ``next`` from bar 34."""

        def __init__(self):
            super().__init__()
            self.x = bt.ind.SMA(bt.ind.SMA(self.datas[0].close, period=15), period=20)
            self.extra = {"first_next": None}

        def next(self):
            super().next()
            if self.extra["first_next"] is None:
                self.extra["first_next"] = len(self)
                self.extra["x"] = float(self.x[0])

    class Analyzers(Recorder):
        """``bt.analyzers`` (corpus ``performance_of_the_average_directional_index...``):
        long and short trades, wins and losses, a reversal, two trades left open."""

        @staticmethod
        def oracle_analyzers(bt):
            return [
                ("trades", bt.analyzers.TradeAnalyzer, {}),
                ("dd", bt.analyzers.DrawDown, {}),
                ("sharpe_days", bt.analyzers.SharpeRatio, {"timeframe": bt.TimeFrame.Days, "compression": 1}),
                ("sharpe_weeks_ann", bt.analyzers.SharpeRatio, {"timeframe": bt.TimeFrame.Weeks, "annualize": True}),
                ("sharpe_days_raw", bt.analyzers.SharpeRatio,
                 {"timeframe": bt.TimeFrame.Days, "convertrate": False, "stddev_sample": True}),
                ("sharpe_years", bt.analyzers.SharpeRatio, {}),
            ]

        def next(self):
            super().next()
            n = len(self)
            d0, d1, d2 = self.datas[0], self.datas[1], self.datas[2]
            plan = {35: (d0, 100), 42: (d0, 0), 44: (d1, -50), 57: (d1, 80), 72: (d1, 0),
                    77: (d2, 60), 82: (d2, 40), 92: (d2, -30), 100: (d2, 0), 105: (d0, 200), 110: (d1, -20)}
            if n in plan:
                data, size = plan[n]
                if size == 0:
                    self.close(data)
                elif size > 0:
                    self.buy(data, size=size)
                else:
                    self.sell(data, size=-size)

    return {
        "line_ops": LineOps,
        "minperiod_add": MinperiodAdd,
        "minperiod_chain": MinperiodChain,
        "analyzers": Analyzers,
        "foreign_feed": ForeignFeed,
        "batch_funding": BatchFunding,
        "deficit_carry": DeficitCarry,
        "line_edges": LineEdges,
        "reverse_shortfall": ReverseShortfall,
        "cheat_on_close": CheatOnClose,
        "add_cash_timing": AddCashTiming,
        "close_then_target": CloseThenTarget,
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
