"""Strategies run under both engines by ``tools/run_oracle.py``.

``bt`` is whichever engine the runner injects (backtrader as the oracle, Tradewind as the
candidate). Each strategy records the NAV at every ``next`` and every completed fill, so
the two runs can be compared line by line. They exercise the Tier 1 and Tier 2 idioms of
``spec/01-scope.md``; nothing here reads engine internals.
"""


import math


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

    class LineGet(Recorder):
        """``line.get(ago, size)`` at the edges of the buffer: a window ending a bar ahead, one
        cut by the end of the buffer, one reaching before the first bar. Every reading is kept
        and the sum of the first one steers a trade, so both the values and the NAV compare."""

        CASES = ((0, 1), (0, 3), (-1, 2), (1, 1), (1, 3), (2, 2), (1, 6), (-4, 3), (0, 200),
                 (3, 8), (-2, 7), (0, 0), (-130, 1))

        def __init__(self):
            super().__init__()
            self.extra = {"gets": []}

        def next(self):
            super().next()
            n = len(self)
            c = self.datas[0].close
            if n in (1, 2, 3, 4, 60, 118, 119, 120):
                row = [n]
                for ago, size in self.CASES:
                    row.append([round(float(v), 8) for v in c.get(ago=ago, size=size)])
                self.extra["gets"].append(row)
            if n == 60:
                window = c.get(ago=1, size=3)
                if len(window) == 3 and window[-1] > c[0]:
                    self.buy(data=self.datas[0], size=7)
                else:
                    self.sell(data=self.datas[0], size=7)

    class FeedEquality(Recorder):
        """A feed compared with another feed, a line, a number or ``None`` while the strategy
        runs: ``in``, ``index``, ``==``, ``!=``, ``<`` and ``+`` are kept per bar, and a feed
        found ``in`` a list of the others (equal closes) is bought, so the NAV compares too."""

        def __init__(self):
            super().__init__()
            self.extra = {"cmp": []}

        def next(self):
            super().next()
            a, b, c, d = self.datas
            others = [b, c, d]
            row = [len(self), a in others, a == b, a != b, b.close == a, a == None, a == "x",
                   a == float(a.close[0]), a < b, round(a + 1.0, 8), round(b - a, 8)]
            try:
                row.append(others.index(a))
            except ValueError:
                row.append(None)
            self.extra["cmp"].append(row)
            if row[1] and not self.getposition(a).size:
                self.buy(data=a, size=3)

        @staticmethod
        def oracle_frames(fr):
            out = dict(fr)
            twin = fr["S1"].copy()
            twin.loc[twin.index[::7], "close"] = fr["S0"]["close"].iloc[::7].values   # equal closes
            out["S1"] = twin
            return out

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

    class ExtraSession(Recorder):
        """A weekday only one feed has a bar on (S1 keeps 2021-03-09, S0/S2/S3 lose it), the
        shape of a stray holiday row in one name of a panel: is it a slice, what do the
        other feeds' lengths do, and where do market orders placed the bar before fill?"""

        @staticmethod
        def oracle_frames(fr):
            import pandas as pd

            day = pd.Timestamp("2021-03-09")
            return {k: (f if k == "S1" else f.drop(index=day, errors="ignore")) for k, f in fr.items()}

        def __init__(self):
            super().__init__()
            self.extra = {"rows": []}

        def next(self):
            super().next()
            d = self.datetime.date(0).isoformat()
            if "2021-03-05" <= d <= "2021-03-12":
                self.extra["rows"].append((d, len(self), [len(x) for x in self.datas],
                                           round(self.datas[1].close[0], 8)))
            if d == "2021-03-08":
                self.buy(self.datas[0], size=10)
                self.buy(self.datas[1], size=10)

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

    # -- probes of the 0.1.47 parity pass (spec/02 § "parity 0.1.47") ---------------------------

    class StrategyMinperiod(Recorder):
        """``addminperiod`` / ``updateminperiod`` called on a strategy (corpus
        ``MomentumRSIStrategy``): accepted, and the first ``next`` stays where the feeds
        (S3 lists 30 bars late) and the indicators (an SMA(5)) put it."""

        def __init__(self):
            super().__init__()
            self.addminperiod(40)
            self.sma = bt.indicators.SMA(self.datas[1].close, period=5)
            self.updateminperiod(200)
            self.extra = {"prenext": 0, "first": None}

        def prenext(self):
            self.extra["prenext"] += 1

        def next(self):
            super().next()
            if self.extra["first"] is None:
                self.extra["first"] = (len(self), self.datas[0].datetime.date(0).isoformat())
                self.buy(data=self.datas[1], size=25)

    class ChildIndicators(Recorder):
        """Indicators built inside another indicator's ``__init__`` (corpus
        ``RSIBullRangeMomentum`` builds an ``RSI_Safe``): the parent's ``next`` reads the
        child on the bar being computed, the parent's min period is the maximum of its
        own (``addminperiod`` included) and its children's, the strategy reads a child
        frozen on its last value; ``RSI_Safe`` on flat (0/0) and rising (x/0) stretches."""

        def __init__(self):
            super().__init__()

            class Signal(bt.Indicator):
                lines = ("long",)
                params = (("lookback", 10), ("rsi_period", 5))

                def __init__(self):
                    super().__init__()
                    self.rsi = bt.indicators.RSI_Safe(self.data.close, period=self.p.rsi_period)
                    self.addminperiod(self.p.lookback + self.p.rsi_period)

                def next(self):
                    vals = [self.rsi[-i] for i in range(self.p.lookback)]
                    entered = min(vals) >= 40.0 and max(vals) > 70.0
                    both_reversed = min(vals) < 40.0 and max(vals) <= 70.0
                    if not bool(self.lines.long[-1]):
                        self.lines.long[0] = 1 if entered else 0
                    else:
                        self.lines.long[0] = 0 if both_reversed else 1

            class NextParent(bt.Indicator):
                lines = ("v",)

                def __init__(self):
                    self.child = bt.indicators.SMA(self.data.close, period=20)
                    self.addminperiod(3)

                def next(self):
                    self.lines.v[0] = self.child[0] - self.child[-1]

            class OnceParent(bt.Indicator):
                lines = ("v",)

                def __init__(self):
                    self.child = bt.indicators.SMA(self.data.close, period=4)

                def once(self, start, end):
                    dst, src = self.lines.v.array, self.child.lines.sma.array
                    for i in range(start, end):
                        dst[i] = src[i] * 2.0

            class BindParent(bt.Indicator):
                lines = ("v",)

                def __init__(self):
                    self.child = bt.indicators.SMA(self.data.close, period=4)
                    self.lines.v = bt.indicators.SMA(self.child, period=3)

            class Ramp(bt.Indicator):
                """Flat, then rising, then flat, then falling, then data0's close."""

                lines = ("close",)

                def once(self, start, end):
                    dst, src = self.lines.close.array, self.data.close.array
                    for i in range(start, end):
                        if i < 30:
                            dst[i] = 50.0
                        elif i < 50:
                            dst[i] = 50.0 + (i - 29)
                        elif i < 60:
                            dst[i] = 70.0
                        elif i < 75:
                            dst[i] = 70.0 - (i - 59)
                        else:
                            dst[i] = src[i]

            self.addminperiod(15)
            self.sigs = [Signal(d) for d in self.datas[:3]]
            self.next_parent = NextParent(self.datas[0])
            self.once_parent = OnceParent(self.datas[1])
            self.bind_parent = BindParent(self.datas[2])
            self.chained = [bt.indicators.SMA(self.next_parent, period=3), bt.indicators.SMA(self.once_parent, period=3)]
            ramp = Ramp(self.datas[0])
            self.safe = [bt.indicators.RSI_Safe(ramp, period=5), bt.indicators.RSI(ramp, period=5, safediv=True)]
            self.extra = {"rows": [], "prenext": 0}

        def prenext(self):
            self.extra["prenext"] += 1

        def next(self):
            super().next()
            self.extra["rows"].append((
                len(self), [repr(s.long[0]) for s in self.sigs], [repr(s.rsi[0]) for s in self.sigs],
                [len(s.rsi) for s in self.sigs], repr(self.next_parent[0]), repr(self.next_parent.child[0]),
                len(self.next_parent.child), repr(self.once_parent[0]), repr(self.once_parent.child[0]),
                len(self.once_parent.child), repr(self.bind_parent[0]), repr(self.bind_parent.child[0]),
                [repr(c[0]) for c in self.chained], [repr(r[0]) for r in self.safe]))
            longs = [d for d, s in zip(self.datas, self.sigs) if s.long[0] == 1]
            for d in self.datas[:3]:
                self.order_target_percent(d, target=0.9 / len(longs) if d in longs else 0.0)

    class InertIndicator(Recorder):
        """An indicator with lines that defines neither ``once`` nor ``next`` and binds
        nothing (corpus ``DecileMomentumSignal``): it holds the feeds and a ``compute()``
        the strategy calls monthly; its line is NaN, its length the clock's."""

        def __init__(self):
            super().__init__()

            class Holder(bt.Indicator):
                lines = ("signal",)
                params = (("top", 1), ("lookback", 20))

                def __init__(self, *datas):
                    super().__init__()
                    self._feeds = list(datas) if datas else list(self.datas)

                def compute(self):
                    lb = self.p.lookback
                    mom = [(d, d.close[0] / d.close[-lb] - 1.0) for d in self._feeds if len(d) > lb]
                    if len(mom) < 2:
                        return None
                    mom.sort(key=lambda x: x[1], reverse=True)
                    return [d for d, _ in mom[: self.p.top]]

            self.sig = Holder(*self.datas, top=1)
            self.month = None
            self.extra = {"rows": []}

        def next(self):
            super().next()
            self.extra["rows"].append((len(self), repr(self.sig.signal[0]), len(self.sig), len(self.sig._feeds)))
            month = self.datas[0].datetime.date(0).month
            if month == self.month:
                return
            self.month = month
            pick = self.sig.compute()
            if pick is None:
                return
            for d in self.datas:
                self.order_target_percent(d, target=0.9 if d in pick else 0.0)

    class TrueRangeInverted(Recorder):
        """``TrueRange`` and ``ATR`` on bars whose high is below their low (the inverted
        series of the catalogue, corpus ``following_the_trend``), every bar or one in
        two, and fractional sizes from the ATR as that strategy sizes them."""

        def __init__(self):
            super().__init__()

            class Swapped(bt.Indicator):
                lines = ("high", "low", "close")
                params = (("mode", "all"),)

                def __init__(self):
                    d = self.data
                    if self.p.mode == "all":
                        self.lines.high = d.low
                        self.lines.low = d.high
                    else:
                        self.lines.high = bt.If(d.close > d.open, d.low, d.high)
                        self.lines.low = bt.If(d.close > d.open, d.high, d.low)
                    self.lines.close = d.close

            swapped, mixed = Swapped(self.datas[0], mode="all"), Swapped(self.datas[1], mode="mixed")
            self.tr = [bt.indicators.TrueRange(swapped), bt.indicators.TrueRange(mixed)]
            self.atr = [bt.indicators.ATR(swapped, period=5), bt.indicators.ATR(mixed, period=5),
                        bt.indicators.ATR(self.datas[2], period=5)]
            self.extra = {"rows": []}

        def next(self):
            super().next()
            self.extra["rows"].append((len(self), [repr(t[0]) for t in self.tr], [repr(a[0]) for a in self.atr]))
            n = len(self)
            if n % 10 == 0:
                for d, a in zip(self.datas[:3], self.atr):
                    if self.getposition(d).size:
                        self.close(data=d)
                    else:
                        self.sell(data=d, size=0.002 * self.broker.getvalue() / a[0])

    class ShortFillDeficit(Recorder):
        """A short sale re-checked at its fill when closing buys earlier in the same pass
        drove the cash below zero (corpus ``investing_just_another_hype…``, LCID on
        2024-01-02): a small short is ``Margin``, a large one fills, a later small one
        too. Checked at the fill only (``checksubmit`` off) and with the batch check."""

        params = (("checksubmit", False),)

        def __init__(self):
            super().__init__()
            if not self.p.checksubmit:
                self.broker.set_checksubmit(False)
            self.extra = {"rows": []}
            self.step = 0

        def notify_order(self, order):
            super().notify_order(order)
            if order.status in (order.Completed, order.Margin, order.Partial):
                self.extra["rows"].append((len(self), order.data._name, order.getstatusname(), order.size,
                                           repr(self.broker.getcash())))

        def next(self):
            super().next()
            self.step += 1  # ``next`` starts once S3 has listed
            n = self.step
            d0, d1, d2 = self.datas[:3]
            if n == 2:
                self.buy(data=d0, size=int(90_000 / d0.close[0]))
            if n == 5:
                self.sell(data=d1, size=int(20_000 / d1.close[0]))
            if n == 8:
                self.buy(data=d2, size=int(0.9 * self.broker.getcash() / d2.close[0]))
                self.close(data=d1)
                self.sell(data=d1, size=10)
                self.sell(data=d1, size=int(40_000 / d1.close[0]))
                self.sell(data=d1, size=5)
            if n == 12:
                self.sell(data=d0, size=10)

    class ShortFillDeficitChecked(ShortFillDeficit):
        params = (("checksubmit", True),)

    class PrivateGetValue(Recorder):
        """``broker._get_value()`` right after ``add_cash`` (corpus: four published
        strategies settle a synthetic sleeve inside the bar this way): what it returns, and
        what ``getcash`` / ``getvalue`` read before and after it, in that bar and the next;
        an order placed after it; a call with no addition queued."""

        def __init__(self):
            super().__init__()
            self.extra = {"rows": []}

        def _row(self, tag, r=None):
            b = self.broker
            self.extra["rows"].append((getattr(self, "tw_step", 0), tag, repr(b.getcash()), repr(b.getvalue()),
                                       None if r is None else repr(r)))

        def next(self):
            super().next()
            self.tw_step = getattr(self, "tw_step", 0) + 1  # ``next`` starts once S3 lists
            n = self.tw_step
            d0, d1 = self.datas[:2]
            self._row("top")
            if n == 2:
                self.buy(data=d0, size=300)
                self.sell(data=d1, size=100)
            if n in (6, 9, 15):
                self.broker.add_cash(1234.5 * n)
                self._row("added")
                self._row("settled", self.broker._get_value())
                self._row("again", self.broker._get_value())
            if n == 9:
                # sized on the settled cash: accepted only if the batch sees it
                self.buy(data=d0, size=int(self.broker.getcash() / d0.close[0]))
            if n == 12:
                self._row("plain", self.broker._get_value())
            if n == 15:
                self.broker.add_cash(-500.25)
                self.broker.add_cash(77.125)
                self._row("two", self.broker._get_value())
                self.order_target_value(d1, target=0.0)

    class SumNDivByZero(Recorder):
        """``bt.ind.SumN`` and ``bt.DivByZero`` as the corpus builds a volume-weighted price
        (``a_comparative_study_of_the_macd…``) and counts flat closes (``do_options_markets…``):
        values bit for bit, zero volumes on a stretch of S0, a NaN-led next-only line."""

        @staticmethod
        def oracle_frames(fr):
            import numpy as np

            out = {}
            for i, (k, f) in enumerate(fr.items()):
                f = f.copy()
                rng = np.random.default_rng(100 + i)
                f["volume"] = np.round(rng.uniform(1e3, 5e5, len(f)) * (1 + i))
                if k == "S0":
                    f.iloc[40:52, f.columns.get_loc("volume")] = 0.0
                    f.iloc[70:74, f.columns.get_loc("close")] = f["close"].iloc[69]
                out[k] = f
            return out

        def __init__(self):
            super().__init__()

            class Flat(bt.Indicator):
                lines = ("flat",)

                def __init__(self):
                    self.addminperiod(2)

                def next(self):
                    self.l.flat[0] = 1.0 if self.data.close[0] == self.data.close[-1] else 0.0

            class Vwap(bt.Indicator):
                lines = ("vw", "raw")
                params = (("period", 5),)

                def __init__(self):
                    d = self.data
                    pv = (d.high + d.low + d.close) / 3.0 * d.volume
                    self.l.vw = bt.DivByZero(bt.ind.SumN(pv, period=self.p.period),
                                             bt.ind.SumN(d.volume, period=self.p.period), 0.0)
                    self.l.raw = bt.ind.SumN(pv, period=self.p.period)

            self.vw = [Vwap(d, period=p) for d, p in zip(self.datas[:3], (5, 12, 3))]
            self.nanzero = bt.DivByZero(self.datas[0].close, self.datas[0].volume, float("nan"))
            self.flat = bt.ind.SumN(Flat(self.datas[0]).flat, period=6)
            self.sum_close = bt.ind.SumN(self.datas[1].close, period=20)
            self.extra = {"rows": []}

        def next(self):
            super().next()
            self.extra["rows"].append((
                len(self), [repr(v.vw[0]) for v in self.vw], [repr(v.raw[0]) for v in self.vw],
                [len(v) for v in self.vw], repr(self.nanzero[0]), repr(self.flat[0]),
                repr(self.sum_close[0])))
            d0 = self.datas[0]
            if self.flat[0] > 0 and not self.getposition(d0).size:
                self.buy(data=d0, size=50)
            elif d0.close[0] > self.vw[0].vw[0] > 0 and self.getposition(d0).size:
                self.close(data=d0)

    class MeanPctChange(Recorder):
        """``bt.ind.Mean(*rocs)`` and ``bt.ind.PctChange(close, period=1)`` as the corpus
        writes them (``exploration_of_long_short_etf_momentum…``), feeding a standard
        deviation with ``movav=SMA``; ``Mean`` with an explicit period too."""

        def __init__(self):
            super().__init__()

            class Signal(bt.Indicator):
                lines = ("avg_mom", "vol")
                params = (("mom_periods", [3, 5, 7, 9]), ("vol_period", 6))

                def __init__(self):
                    moms = [bt.ind.RateOfChange(self.data.close, period=p) for p in self.p.mom_periods]
                    self._mom_mean = bt.ind.Mean(*moms)
                    daily_ret = bt.ind.PctChange(self.data.close, period=1)
                    self._vol = bt.ind.StandardDeviation(daily_ret, period=self.p.vol_period, movav=bt.ind.SMA)

                def next(self):
                    self.lines.avg_mom[0] = self._mom_mean[0]
                    self.lines.vol[0] = self._vol[0]

            self.sig = [Signal(d) for d in self.datas[:3]]
            self.mean4 = bt.ind.Mean(self.datas[1].close, period=4)
            self.pct = [bt.ind.PctChange(self.datas[2].close), bt.ind.PercentChange(self.datas[2], period=3)]
            self.avg = bt.ind.Average(self.datas[0].close, period=7)
            self.extra = {"rows": []}

        def next(self):
            super().next()
            self.extra["rows"].append((
                len(self), [repr(s.avg_mom[0]) for s in self.sig], [repr(s.vol[0]) for s in self.sig],
                repr(self.mean4[0]), [repr(p[0]) for p in self.pct], repr(self.avg[0]),
                [len(s) for s in self.sig]))
            if len(self) % 5:
                return
            moms = [(d, s.avg_mom[0]) for d, s in zip(self.datas[:3], self.sig)]
            best = max(moms, key=lambda x: x[1])[0]
            for d, _ in moms:
                self.order_target_percent(d, target=0.6 if d is best else 0.0)

    class BracketOrders(Recorder):
        """``buy_bracket`` / ``sell_bracket`` (corpus ``double_bottom_country_trading…``):
        the three orders returned, their notifications, which child fills, the other one
        cancelled, a parent cancelled before it fills, a child cancelled by hand."""

        def __init__(self):
            super().__init__()
            self.extra = {"brackets": [], "rows": []}
            self.br = {}

        def _keep(self, tag, orders):
            self.br[tag] = orders
            self.extra["brackets"].append((len(self), tag, [
                (o.getordername(), o.isbuy(), o.size, repr(o.price), o.getstatusname(),
                 None if o.parent is None else orders.index(o.parent),
                 bool(o.transmit))
                for o in orders]))

        def notify_order(self, order):
            super().notify_order(order)
            for tag, orders in self.br.items():
                if any(order.ref == o.ref for o in orders):
                    which = [o.ref for o in orders].index(order.ref)
                    self.extra["rows"].append((len(self), tag, which, order.getstatusname(),
                                               float(order.executed.size), repr(order.executed.price),
                                               [o.getstatusname() for o in orders], order.alive()))

        def next(self):
            super().next()
            self.tw_step = getattr(self, "tw_step", 0) + 1  # ``next`` starts once S3 lists
            n = self.tw_step
            d0, d1, d2 = self.datas[:3]
            c0, c1, c2 = d0.close[0], d1.close[0], d2.close[0]
            if n == 3:
                self._keep("limit_entry", self.buy_bracket(data=d0, size=40, price=c0 * 0.995,
                                                           exectype=bt.Order.Limit,
                                                           limitprice=c0 * 1.03, stopprice=c0 * 0.97))
                self._keep("market_entry", self.buy_bracket(data=d1, size=30, exectype=bt.Order.Market,
                                                            limitprice=c1 * 1.02, stopprice=c1 * 0.98))
                self._keep("never_fills", self.buy_bracket(data=d2, size=20, price=c2 * 0.7,
                                                           limitprice=c2 * 0.8, stopprice=c2 * 0.6))
            if n == 10:
                self.cancel(self.br["never_fills"][0])
            if n == 40:
                self._keep("short", self.sell_bracket(data=d0, size=25, exectype=bt.Order.Market,
                                                      stopprice=c0 * 1.04, limitprice=c0 * 0.96))
                self._keep("cancel_child", self.buy_bracket(data=d1, size=15, exectype=bt.Order.Market,
                                                            limitprice=c1 * 1.5, stopprice=c1 * 0.5))
                self._keep("child_first", self.buy_bracket(data=d2, size=10, price=c2 * 0.5,
                                                           limitprice=c2 * 0.9, stopprice=c2 * 0.4))
            if n == 45:
                self.cancel(self.br["cancel_child"][1])
                self.cancel(self.br["child_first"][2])
            if n == 50:
                self.cancel(self.br["child_first"][0])
            if n == 60:
                self._keep("default_exec", self.buy_bracket(data=d0, size=12, price=c0,
                                                            limitprice=c0 * 1.01, stopprice=c0 * 0.99))
            if n == 80:
                self._keep("stop_entry", self.buy_bracket(data=d1, size=11, price=c1 * 1.01,
                                                          exectype=bt.Order.Stop,
                                                          limitprice=c1 * 1.08, stopprice=c1 * 0.95))

    class PositionByName(Recorder):
        """``self.positionbyname`` (corpus ``feasible_momentum_strategies…`` calls it with a
        name): what kind of object it is, which feed's position it reads, and what calling
        it does."""

        def __init__(self):
            super().__init__()
            self.extra = {"rows": []}

        def next(self):
            super().next()
            self.tw_step = getattr(self, "tw_step", 0) + 1  # ``next`` starts once S3 lists
            n = self.tw_step
            if n == 2:
                self.buy(data=self.datas[0], size=7)
                self.sell(data=self.datas[1], size=3)
            if n in (1, 5):
                p = self.positionbyname
                try:
                    called = repr(p("S1").size)
                except TypeError as e:
                    called = type(e).__name__
                self.extra["rows"].append((n, type(p).__name__, p.size, repr(p.price),
                                           p is self.position, p is self.getposition(self.datas[0]),
                                           p.size == self.getpositionbyname("S1").size,
                                           self.positionsbyname["S1"].size, called))

    class BracketBatch(Recorder):
        """Brackets inside the batch pre-check: whether a bracket's children weigh on the
        running cash of the orders placed after them, a parent refused (Margin) with its
        children, a bracket placed with ``checksubmit`` off."""

        params = (("checksubmit", True),)

        def __init__(self):
            super().__init__()
            if not self.p.checksubmit:
                self.broker.set_checksubmit(False)
            self.extra = {"rows": []}
            self.br = []

        def notify_order(self, order):
            super().notify_order(order)
            for k, orders in enumerate(self.br):
                refs = [o.ref for o in orders]
                if order.ref in refs:
                    self.extra["rows"].append((len(self), k, refs.index(order.ref), order.getstatusname(),
                                               repr(order.executed.price)))

        def next(self):
            super().next()
            self.tw_step = getattr(self, "tw_step", 0) + 1  # ``next`` starts once S3 lists
            n = self.tw_step
            d0, d1, d2 = self.datas[:3]
            cash = self.broker.getcash()
            if n == 2:
                # the children's sells would fund the next buy if they counted
                size = int(0.6 * cash / d0.close[0])
                self.br.append(self.buy_bracket(data=d0, size=size, exectype=bt.Order.Market,
                                                limitprice=d0.close[0] * 1.2, stopprice=d0.close[0] * 0.8))
                self.buy(data=d1, size=int(0.6 * cash / d1.close[0]))
            if n == 20:
                for d in self.datas:
                    self.close(data=d)
                for o in self.br[0][1:]:
                    self.cancel(o)
            if n == 25:
                # a short bracket: its buy children would drain the running cash
                size = int(0.9 * cash / d1.close[0])
                self.br.append(self.sell_bracket(data=d1, size=size, exectype=bt.Order.Market,
                                                 stopprice=d1.close[0] * 1.2, limitprice=d1.close[0] * 0.8))
                self.buy(data=d2, size=int(0.5 * cash / d2.close[0]))
            if n == 40:
                for d in self.datas:
                    self.close(data=d)
                for o in self.br[1][1:]:
                    self.cancel(o)
            if n == 45:
                # a parent too large: refused, and its children?
                self.br.append(self.buy_bracket(data=d2, size=int(3 * cash / d2.close[0]), exectype=bt.Order.Market,
                                                limitprice=d2.close[0] * 1.05, stopprice=d2.close[0] * 0.95))
                self.buy(data=d0, size=10)
            if n == 60:
                # children refused by the pre-check while their parent is accepted
                self.buy(data=d0, size=int(0.95 * cash / d0.close[0]))
                size = int(0.5 * cash / d1.close[0])
                self.br.append(self.sell_bracket(data=d1, size=size, exectype=bt.Order.Market,
                                                 stopprice=d1.close[0] * 1.2, limitprice=d1.close[0] * 0.8))
                self.buy(data=d2, size=3)

    class BracketBatchUnchecked(BracketBatch):
        params = (("checksubmit", False),)

    class SlippagePerc(Recorder):
        """``broker.set_slippage_perc`` / ``set_slippage_fixed`` called with only the amount
        (corpus ``the_profitability_of_technical_analysis…``,
        ``price_overreactions_in_the_cryptocurrency_market``), then with each flag turned:
        market orders at the open (buys and sells, a large slip that leaves the bar's range),
        limit, stop and close orders. The fill prices say which flag moves what."""

        params = (("slip", ("perc", 0.01, {})),)

        def __init__(self):
            super().__init__()
            kind, amount, flags = self.p.slip
            if kind == "perc":
                self.broker.set_slippage_perc(amount, **flags)
            else:
                self.broker.set_slippage_fixed(amount, **flags)
            self.tw_step = 0

        def next(self):
            super().next()
            self.tw_step += 1  # ``next`` starts once S3 has listed
            n = self.tw_step
            d0, d1, d2 = self.datas[:3]
            c0, c1 = d0.close[0], d1.close[0]
            if n in (2, 9, 16, 30):
                self.buy(data=d0, size=40)
                self.sell(data=d1, size=30)
            if n in (5, 12, 23):
                self.sell(data=d0, size=40)
                self.buy(data=d1, size=30)
            if n == 34:
                self.buy(data=d2, size=20, exectype=bt.Order.Limit, price=d2.close[0] * 0.995)
                self.sell(data=d2, size=10, exectype=bt.Order.Limit, price=d2.close[0] * 1.005)
                self.buy(data=d0, size=15, exectype=bt.Order.Limit, price=c0 * 1.03)   # marketable
            if n == 40:
                self.buy(data=d1, size=12, exectype=bt.Order.Stop, price=c1 * 1.004)
                self.sell(data=d0, size=12, exectype=bt.Order.Stop, price=c0 * 0.996)
                self.sell(data=d2, size=8, exectype=bt.Order.Stop, price=d2.close[0] * 1.05)  # gapped
            if n == 50:
                self.buy(data=d2, size=9, exectype=bt.Order.Close)
                self.sell(data=d1, size=9, exectype=bt.Order.Close)
            if n == 60:
                for d in self.datas:
                    self.close(data=d)
            if n in (64, 72):
                # limits and stops a few bars away: filled through the bar, not at the open
                for k, d in enumerate(self.datas[:3]):
                    c = d.close[0]
                    self.buy(data=d, size=5 + k, exectype=bt.Order.Limit, price=c * (0.992 - 0.002 * k))
                    self.sell(data=d, size=5 + k, exectype=bt.Order.Limit, price=c * (1.008 + 0.002 * k))
                    self.buy(data=d, size=3 + k, exectype=bt.Order.Stop, price=c * (1.004 + 0.002 * k))
                    self.sell(data=d, size=3 + k, exectype=bt.Order.Stop, price=c * (0.996 - 0.002 * k))
            if n == 82:
                for d in self.datas:
                    self.close(data=d)

    class SlippageNoOpen(SlippagePerc):
        params = (("slip", ("perc", 0.01, {"slip_open": False})),)

    class SlippageOut(SlippagePerc):
        params = (("slip", ("perc", 0.01, {"slip_out": True})),)

    class SlippageNoLimit(SlippagePerc):
        params = (("slip", ("perc", 0.01, {"slip_limit": False})),)

    class SlippageNoMatch(SlippagePerc):
        params = (("slip", ("perc", 0.01, {"slip_match": False})),)

    class SlippageStrict(SlippagePerc):
        params = (("slip", ("perc", 0.01, {"slip_match": False, "slip_limit": False})),)

    class SlippageFixed(SlippagePerc):
        params = (("slip", ("fixed", 0.9, {})),)

    class SlippageFixedOut(SlippagePerc):
        params = (("slip", ("fixed", 0.9, {"slip_out": True, "slip_open": True})),)

    class OrderIdentity(Recorder):
        """The order a notification carries against the one ``buy`` returned (corpus
        ``option_implied_currency_risk_premia`` keeps its placed orders in a list and
        removes them in ``notify_order``): identity, equality, membership."""

        def __init__(self):
            super().__init__()
            self.placed = []
            self.extra = {"rows": []}

        def next(self):
            super().next()
            if not getattr(self, "tw_done", False):
                self.tw_done = True
                self.placed.append(self.buy(data=self.datas[0], size=5))
                self.placed.append(self.sell(data=self.datas[1], size=3))

        def notify_order(self, order):
            super().notify_order(order)
            for p in self.placed:
                self.extra["rows"].append((order.ref - p.ref, order.getstatusname(), order is p, order == p,
                                           order != p, order in self.placed, self.placed.index(order) if order in self.placed else None))
            if order.status == order.Completed and order in self.placed:
                self.placed.remove(order)
                self.extra["rows"].append(("removed", len(self.placed)))

    class OwnerLine(bt.Indicator):
        lines = ("x",)

        def __init__(self):
            self.lines.x = self.data
            o = self._owner
            OWNER_SEEN.append(("child", type(o).__name__ == "OwnerHolder"))

    class OwnerHolder(bt.Indicator):
        lines = ("y",)

        def __init__(self):
            o = self._owner
            OWNER_SEEN.append(("indicator", o is BUILDING[-1], o.getdatabyname("S1") is o.datas[1]))
            self.kid = OwnerLine(self.data)
            self.lines.y = self.kid.x

    OWNER_SEEN = []
    BUILDING = []

    class IndicatorOwner(Recorder):
        """``self._owner`` inside an indicator's ``__init__`` (corpus
        ``when_to_own_stocks_and_when_to_own_gold`` reaches the strategy's feeds by name
        through it): the strategy for an indicator it builds, the parent for a child."""

        def __init__(self):
            super().__init__()
            BUILDING.append(self)
            del OWNER_SEEN[:]
            self.h = OwnerHolder(self.datas[0])
            self.sma = bt.indicators.SMA(self.datas[1], period=3)
            self.extra = {"rows": list(OWNER_SEEN) + [("sma", self.sma._owner is self)]}

        def next(self):
            super().next()
            self.extra["rows"].append((len(self), round(self.h.y[0], 8)))

    class SelfBound(bt.Indicator):
        lines = ("upper", "lower", "middle", "spread")
        params = (("period", 6),)

        def __init__(self):
            self.addminperiod(self.p.period)
            self.lines.upper = bt.indicators.Highest(self.data.high, period=self.p.period)
            self.lines.lower = bt.indicators.Lowest(self.data.low, period=self.p.period)
            self.lines.middle = (self.lines.upper + self.lines.lower) / 2.0
            self.lines.spread = self.l.upper - self.lines.middle

    class SelfBoundLines(Recorder):
        """An indicator binding a line to an expression of its own bound lines (corpus
        ``analysis_of_trend_following_systems``' Donchian channel, ``RecursionError`` on
        Tradewind 0.0.2), read from the strategy and through a line built on it."""

        def __init__(self):
            super().__init__()
            self.dc = SelfBound(self.datas[0])
            self.after = (self.dc.middle + self.dc.upper) / 2.0
            self.extra = {"rows": []}

        def next(self):
            super().next()
            r = lambda x: None if math.isnan(x) else round(x, 9)
            self.extra["rows"].append((len(self), r(self.dc.upper[0]), r(self.dc.lower[0]), r(self.dc.middle[0]),
                                       r(self.dc.spread[0]), r(self.after[0]), r(self.dc.middle[-1])))
            if len(self) % 9 == 0:
                d = self.datas[0]
                if d.high[0] >= self.dc.upper[-1]:
                    self.buy(data=d, size=10)
                elif self.getposition(d).size:
                    self.close(data=d)

    class LeveredNegativeClose(Recorder):
        """A long under a leveraged stock-like scheme marked at a close of zero and below
        (corpus ``the_best_defensive_strategies…``: crude oil at -37.63 on 2020-04-20, a
        3x scheme set in place): the marked value against ``size * close``."""

        @staticmethod
        def oracle_frames(fr):
            out = dict(fr)
            f = fr["S0"].copy()
            k = f.index
            for i, c in ((40, 5.0), (41, 0.0), (42, -1.0), (43, -37.63), (44, 1e-9), (45, 20.0)):
                f.loc[k[i], "close"] = c
                f.loc[k[i], "low"] = min(c, f.loc[k[i], "low"])
            out["S0"] = f
            return out

        def __init__(self):
            super().__init__()
            self.broker.setcommission(commission=0.0, leverage=3.0)
            self.extra = {"rows": []}

        def next(self):
            super().next()
            n = len(self)
            if n == 32:
                self.buy(data=self.datas[0], size=100)
                self.sell(data=self.datas[1], size=50)
                self.buy(data=self.datas[2], size=40)
            self.extra["rows"].append((n, self.datas[0].close[0], repr(self.broker.getvalue()),
                                       repr(self.broker.get_value(datas=[self.datas[0]]))))

    class CreatedPrice(Recorder):
        """``order.created.price`` / ``.pricelimit`` of every kind of order (parity 0.1.54):
        pwb-toolbox reads ``created.price`` as the price a market order was sized on."""

        def __init__(self):
            super().__init__()
            self.extra = {"rows": []}

        def next(self):
            super().next()
            self._n = getattr(self, "_n", 0) + 1
            n, d = self._n, self.datas[0]
            if n == 2:
                self.buy(data=d, size=1)
            if n == 3:
                self.buy(data=d, size=1, exectype=bt.Order.Limit, price=round(d.close[0] * 0.99, 2))
            if n == 4:
                self.sell(data=d, size=1, exectype=bt.Order.Stop, price=round(d.close[0] * 0.97, 2))
            if n == 5:
                self.order_target_size(data=self.datas[1], target=5)
            if n == 6:
                self.buy(data=d, size=1, price=99.0)
            if n == 7:
                self.close(data=self.datas[1])
            if n == 8:
                self.buy(data=d, size=1, exectype=bt.Order.StopLimit, price=round(d.close[0] * 1.2, 2),
                         plimit=round(d.close[0] * 1.21, 2))

        def notify_order(self, order):
            super().notify_order(order)
            if order.status == order.Submitted:
                self.extra["rows"].append((order.ref - self._first_ref(order), order.data._name,
                                           repr(order.created.price), repr(order.created.pricelimit)))

        def _first_ref(self, order):
            if not hasattr(self, "_ref0"):
                self._ref0 = order.ref
            return self._ref0

    class NoInputConstant(bt.Indicator):
        lines = ("r",)
        params = (("k", 0.25),)

        def next(self):
            self.lines.r[0] = self.p.k + len(self)

    class NoInputClose(bt.Indicator):
        lines = ("q",)

        def next(self):
            self.lines.q[0] = self.data.close[0]

    class IndicatorNoInput(Recorder):
        """An indicator built with no input (parity 0.1.54, corpus
        ``trend_following_algorithms_on_india_s_nifty_index``): it follows the strategy's
        first feed, whether its ``next`` reads it or not."""

        def __init__(self):
            super().__init__()
            self.konst = NoInputConstant(k=0.5)
            self.close0 = NoInputClose()
            self.extra = {"rows": []}

        def next(self):
            super().next()
            self.extra["rows"].append((len(self), len(self.konst), self.konst.r[0], len(self.close0),
                                       self.close0.q[0]))

    class ForeignReadForced(ForeignRead):
        """``ForeignRead`` with ``_nextforce`` set on the class."""

        _nextforce = True

    class NextForce(Recorder):
        """``_nextforce`` on an indicator class (parity 0.1.54, corpus
        ``a_study_in_portfolio_diversification_using_vix_options``, three in the catalogue):
        the runonce pre-pass is off for the whole run, so the forced indicator and an
        un-forced one reading another foreign feed both read the bar's value; the strategy
        trades on the forced one."""

        def __init__(self):
            super().__init__()
            self.forced = ForeignReadForced(self.datas[0], other=self.datas[1])
            self.plain = ForeignRead(self.datas[0], other=self.datas[2])
            self.extra = {"rows": []}

        def next(self):
            super().next()
            f, p = float(self.forced[0]), float(self.plain[0])
            self.extra["rows"].append((self.datas[0].datetime.date(0).isoformat(), round(f, 6), round(p, 6)))
            if len(self) % 10 == 0:
                target = 0.9 if f > float(self.datas[1].close[-5]) else 0.3
                self.order_target_percent(self.datas[0], target=target)

    return {
        "levered_negative_close": LeveredNegativeClose,
        "slippage_perc": SlippagePerc,
        "slippage_no_open": SlippageNoOpen,
        "slippage_out": SlippageOut,
        "slippage_no_limit": SlippageNoLimit,
        "slippage_no_match": SlippageNoMatch,
        "slippage_strict": SlippageStrict,
        "slippage_fixed": SlippageFixed,
        "slippage_fixed_out": SlippageFixedOut,
        "order_identity": OrderIdentity,
        "indicator_owner": IndicatorOwner,
        "self_bound_lines": SelfBoundLines,
        "short_fill_deficit": ShortFillDeficit,
        "short_fill_deficit_checked": ShortFillDeficitChecked,
        "strategy_minperiod": StrategyMinperiod,
        "child_indicators": ChildIndicators,
        "inert_indicator": InertIndicator,
        "true_range_inverted": TrueRangeInverted,
        "extra_session": ExtraSession,
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
        "line_get": LineGet,
        "feed_equality": FeedEquality,
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
        "private_get_value": PrivateGetValue,
        "sumn_divbyzero": SumNDivByZero,
        "mean_pctchange": MeanPctChange,
        "bracket_orders": BracketOrders,
        "bracket_batch": BracketBatch,
        "bracket_batch_unchecked": BracketBatchUnchecked,
        "position_by_name": PositionByName,
        "created_price": CreatedPrice,
        "indicator_no_input": IndicatorNoInput,
        "nextforce": NextForce,
    }
