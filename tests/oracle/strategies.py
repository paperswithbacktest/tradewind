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

    class NotifySequence(Recorder):
        """Two fills on one bar, and an order placed from inside ``notify_order``: are the
        trade notifications grouped after the order ones, and when is an order placed
        during a notification first notified?"""

        def notify_order(self, order):
            super().notify_order(order)
            n = len(self)
            if order.status == order.Completed and n == 38 and order.data._name == "S0":
                self.buy(data=self.datas[2], size=3)

        def notify_trade(self, trade):
            self.statuses.append((0, f"trade {trade.data._name} {'closed' if trade.isclosed else 'open'}",
                                  round(self.broker.getvalue(), 6), self.datas[0].datetime.date(0).isoformat()))

        def next(self):
            super().next()
            n = len(self)
            d0, d1 = self.datas[0], self.datas[1]
            if n == 34:
                self.buy(data=d0, size=10)
                self.buy(data=d1, size=10)
            if n == 37:
                self.close(d0)
                self.close(d1)
            if n == 51:
                self.sell(data=d0, size=5)
                self.buy(data=d1, size=5)

    class CancelTiming(Recorder):
        """``cancel()`` at four moments: in the ``next`` that placed the order (a far limit,
        then a market order), on the next bar once it is Accepted, and on an order placed
        inside ``notify_order``. Which ones are cancelled, and which still fill?"""

        def __init__(self):
            super().__init__()
            self.kept = {}

        def notify_order(self, order):
            super().notify_order(order)
            if order.status == order.Completed and order.data._name == "S1" and len(self) == 44:
                self.kept["d"] = self.buy(data=self.datas[2], size=1, exectype=bt.Order.Limit,
                                          price=self.datas[2].close[0] * 0.5)

        def next(self):
            super().next()
            n = len(self)
            d0 = self.datas[0]
            if n == 33:
                self.cancel(self.buy(data=d0, size=1, exectype=bt.Order.Limit, price=d0.close[0] * 0.5))
            if n == 36:
                self.kept["b"] = self.buy(data=d0, size=1, exectype=bt.Order.Limit, price=d0.close[0] * 0.5)
            if n == 37:
                self.cancel(self.kept["b"])
            if n == 40:
                self.cancel(self.buy(data=d0, size=2))
            if n == 43:
                self.buy(data=self.datas[1], size=1)
            if n == 46:
                self.cancel(self.kept["d"])

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


    # -- probes of the 0.1.45 parity pass (spec/02 § "parity 0.1.45") ---------------------------

    class StrategyClock(Recorder):
        """``self.datetime`` and ``len(self)`` of a strategy whose data0 lists 30 bars late
        (feeds S3 first): the clock's own line, not data0's; an SMA on an early feed adds no
        warm-up, one on the late feed does."""

        def __init__(self):
            super().__init__()
            self.sma_early = bt.indicators.SMA(self.datas[1].close, period=5)
            self.sma_late = bt.indicators.SMA(self.datas[3].close, period=5)
            self.extra = {"rows": []}

        def _row(self, stage):
            dt = self.datetime
            try:
                ahead = dt.date(1).isoformat()
            except IndexError as exc:
                ahead = type(exc).__name__
            self.extra["rows"].append((stage, len(self), len(dt), dt.date(0).isoformat(),
                                       dt.date(-1).isoformat(), ahead, len(self.datas[3])))

        def prenext(self):
            if len(self) in (1, 2, 30, 31, 33):
                self._row("prenext")

        def nextstart(self):
            self._row("nextstart")
            self.next()

        def next(self):
            super().next()
            if len(self) % 20 == 0:
                self._row("next")

    class LineBinding(Recorder):
        """Indicators that only bind their declared lines in ``__init__`` (corpus ROC5Signal,
        DualMomentumSignal): values, min periods, an SMA of one."""

        def __init__(self):
            super().__init__()

            class Bound(bt.Indicator):
                lines = ("value",)
                params = (("period", 5),)

                def __init__(self):
                    super().__init__()
                    self.lines.value = bt.indicators.RateOfChange(self.data.close, period=self.p.period)

            class TwoLines(bt.Indicator):
                lines = ("a", "b")

                def __init__(self):
                    self.l.b = bt.indicators.SMA(self.data.close, period=3)
                    self.lines.a = self.data.close - self.data.open

            self.bound = Bound(self.datas[0])
            self.two = TwoLines(self.datas[1])
            self.nested = bt.indicators.SMA(Bound(self.datas[2], period=4), period=3)
            self.extra = {"rows": []}

        def next(self):
            super().next()
            if len(self) % 9 == 0:
                self.extra["rows"].append((len(self), self.bound[0], self.bound.value[-1], self.two.a[0],
                                           self.two.l.b[0], self.nested[0]))
            if len(self) == 40:
                self.buy(data=self.datas[0], size=10 if self.bound[0] > 0 else 20)

    class IndicatorBits(Recorder):
        """RateOfChange and StandardDeviation, compared to the last bit (``repr``)."""

        def __init__(self):
            super().__init__()
            ind = bt.indicators
            self.inds = [ind.RateOfChange(self.datas[0].close, period=5), ind.RateOfChange(self.datas[1].close, period=1),
                         ind.StandardDeviation(self.datas[0].close, period=10)]
            self.extra = {"bits": []}

        def next(self):
            super().next()
            self.extra["bits"].append([repr(float(i[0])) for i in self.inds])

    class PrivateNames(Recorder):
        """A strategy and an indicator owning names an engine might use itself: ``_ready``,
        ``_resolve(name, default)``, ``_step``, ``_listed``, ``_indicators`` (a dict),
        ``_computed``, ``_timers`` (corpus ``regimes_of_index…``, ``credit_informed…``)."""

        def __init__(self):
            super().__init__()

            class Ind(bt.Indicator):
                lines = ("x",)

                def __init__(self):
                    self._computed = False
                    self._step = 0
                    self._ready = False
                    self.addminperiod(3)

                def next(self):
                    self._step += 1
                    self.lines.x[0] = self.data.close[0] - self.data.close[-2]

            self._ready = False
            self._step = 0
            self._listed = set()
            self._indicators = {d._name: Ind(d) for d in self.datas[:3]}
            self._computed = False
            self._timers = []

        def _resolve(self, name, default=None):
            return self.getdatabyname(name) if name in self.getdatanames() else default

        def next(self):
            super().next()
            self._step += 1
            self._ready = self._ready or self._step >= 5
            if self._ready and self._step % 10 == 0:
                self.order_target_percent(self._resolve("S1"), target=0.5 if (self._step // 10) % 2 else 0.1)
                self.order_target_value(self._resolve("S2"), target=20000)
                self.order_target_size(data=self._resolve("S0"), target=self._step)

    class AddCashFlat(Recorder):
        """``add_cash`` then ``order_target_value`` on a flat feed: the target's position read
        lands the withdrawal, and the fill is checked against it (corpus
        ``investing_in_the_s_p_index…``); ``order_target_size`` does not read it."""

        def next(self):
            super().next()
            n = len(self)
            d0, d1, d2 = self.datas[0], self.datas[1], self.datas[2]
            if n == 40:
                cash = self.broker.getcash()
                self.broker.add_cash(-0.5 * cash)
                self.order_target_value(d0, target=int(0.9 * cash / d0.close[0]) * d0.close[0])
            if n == 50:
                cash = self.broker.getcash()
                self.broker.add_cash(-0.5 * cash)
                self.order_target_size(d1, target=int(0.9 * cash / d1.close[0]))
            if n == 60:
                cash = self.broker.getcash()
                self.broker.add_cash(-0.5 * cash)
                self.order_target_percent(d2, target=0.9)

    class ChecksubmitOff(Recorder):
        """``set_checksubmit(False)``: accepted at once (no Submitted), cancellable in the bar
        that placed it, refused only at the fill (corpus ``robust_asset_allocation…``)."""

        def __init__(self):
            super().__init__()
            self.broker.set_checksubmit(False)

        def next(self):
            super().next()
            n = len(self)
            d0, d1 = self.datas[0], self.datas[1]
            if n == 33:
                self.buy(data=d0, size=500)
                self.buy(data=d1, size=int(2 * self.broker.getcash() / d1.close[0]))
            if n == 36:
                self.order_target_percent(d0, 0.0)
                self.buy(data=d1, size=10, exectype=bt.Order.Limit, price=d1.close[0] * 0.5)
            if n == 38:
                self.cancel(self.buy(data=d1, size=5))

    class StockLeverage(Recorder):
        """A stock-like scheme with ``leverage = 2`` set on the scheme in place (corpus
        ``structural_convexity…``): longs pay and count their margin, shorts the full price;
        the pre-check, the fills, the value and the target sizes."""

        def __init__(self):
            super().__init__()
            self.broker.setcommission(commission=0.001)
            for ci in self.broker.comminfo.values():
                ci.p.leverage = 2.0
            self.extra = {"cash": []}

        def next(self):
            super().next()
            n = len(self)
            d0, d1 = self.datas[0], self.datas[1]
            self.extra["cash"].append(repr(self.broker.getcash()))
            if n == 32:
                self.buy(data=d0, size=int(1.5 * self.broker.getcash() / d0.close[0]))
            if n == 40:
                self.buy(data=d1, size=200)
            if n == 48:
                self.sell(data=d0, size=300)
            if n == 56:
                self.close(d0)
                self.sell(data=d1, size=500)
            if n == 64:
                self.close(d1)
            if n == 70:
                self.order_target_percent(d0, 0.5)
            if n == 76:
                self.order_target_value(d1, -30000)
            if n == 82:
                self.order_target_percent(d0, 1.6)
            if n == 88:
                self.close(d0)
                self.close(d1)

    class SetcashHeld(Recorder):
        """``setcash`` while holding a position: the value becomes the cash alone until the
        next bar (corpus ``parameter_uncertainty…`` credits a monthly rate this way)."""

        def __init__(self):
            super().__init__()
            self.extra = {"rows": []}

        def next(self):
            super().next()
            n = len(self)
            d0 = self.datas[0]
            if n == 32:
                self.buy(data=d0, size=500)
            if n in (40, 50):
                self.broker.setcash(self.broker.getcash() * 1.01)
                self.extra["rows"].append((n, self.broker.getvalue(), self.broker.get_value(datas=[d0])))
                if n == 50:
                    self.order_target_percent(d0, 0.5)

    class ReversalFees(Recorder):
        """Random targets that often flip the side, under a percentage fee: a reversal is two
        executions each with its own fee, a close returns entry value + P&L -- the cash to the
        last bit (``repr``)."""

        def __init__(self):
            super().__init__()
            import random

            self.broker.setcommission(commission=0.0013)
            self.rng = random.Random(5)
            self.extra = {"cash": [], "comm": []}

        def next(self):
            super().next()
            for d in self.datas[:3]:
                pos = self.getposition(d).size
                tgt = self.rng.choice([-1, 1]) * self.rng.randint(1, 60)
                if pos and (pos > 0) == (tgt > 0):
                    tgt = -tgt
                self.order_target_size(d, tgt)
            # the cash to the last bit; the value within the diff's tolerance (one mark in about
            # a hundred still differs in its last bit, spec/02 "value of a book")
            self.extra["cash"].append((repr(self.broker.getcash()), self.broker.getvalue()))

        def notify_order(self, order):
            super().notify_order(order)
            if order.status == order.Completed:
                ex = order.executed
                self.extra["comm"].append((repr(ex.comm), repr(ex.value), repr(ex.price)))

    class ExecutedValues(Recorder):
        """``order.executed`` (closed part valued at the entry price) and ``notify_trade``
        (heard at open and close only, a resize is silent)."""

        def __init__(self):
            super().__init__()
            self.broker.setcommission(commission=0.001)
            self.extra = {"executed": [], "trades": []}

        def next(self):
            super().next()
            n = len(self)
            d0, d1 = self.datas[0], self.datas[1]
            if n == 33:
                self.buy(data=d0, size=91)
                self.sell(data=d1, size=37)
            if n == 38:
                self.sell(data=d0, size=30)
                self.buy(data=d1, size=50)
            if n == 42:
                self.buy(data=d0, size=13)
            if n == 45:
                self.close(d0)
                self.close(d1)

        def notify_order(self, order):
            super().notify_order(order)
            if order.status == order.Completed:
                ex = order.executed
                self.extra["executed"].append((order.data._name, ex.size, ex.price, ex.value, ex.comm, ex.pnl))

        def notify_trade(self, trade):
            self.extra["trades"].append((len(self), trade.data._name, trade.isclosed, trade.pnl, trade.pnlcomm,
                                         trade.size))

    return {
        "strategy_clock": StrategyClock,
        "line_binding": LineBinding,
        "indicator_bits": IndicatorBits,
        "private_names": PrivateNames,
        "add_cash_flat": AddCashFlat,
        "checksubmit_off": ChecksubmitOff,
        "stock_leverage": StockLeverage,
        "setcash_held": SetcashHeld,
        "reversal_fees": ReversalFees,
        "executed_values": ExecutedValues,
        "line_ops": LineOps,
        "minperiod_add": MinperiodAdd,
        "minperiod_chain": MinperiodChain,
        "analyzers": Analyzers,
        "notify_sequence": NotifySequence,
        "cancel_timing": CancelTiming,
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
