"""The compat semantics of spec/02 on synthetic bars, checked by hand."""

import numpy as np
import pandas as pd

import tradewind as tw


def frames(n_sym=3, n_bars=40, seed=7):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2020-01-01", periods=n_bars)
    out = {}
    for i in range(n_sym):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n_bars)))
        out[f"S{i}"] = pd.DataFrame(
            {"open": close * 0.999, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e5},
            index=index,
        )
    return out


class BuyOnce(tw.Strategy):
    params = (("size", 10),)

    def __init__(self):
        self.fills = []
        self.values = []

    def next(self):
        self.values.append(self.broker.getvalue())
        if len(self) == 3:
            self.buy(size=self.p.size)

    def notify_order(self, order):
        if order.status == order.Completed:
            self.fills.append((self.data.datetime.date(0), order.executed.price, order.executed.size))


def run(strategy, fr, **kw):
    cerebro = tw.Cerebro(stdstats=False)
    for name, f in fr.items():
        cerebro.adddata(tw.feeds.PandasData(dataname=f), name=name)
    cerebro.addstrategy(strategy, **kw)
    cerebro.broker.set_cash(100_000)
    return cerebro, cerebro.run()[0]


def test_market_order_fills_at_next_open():
    fr = frames(1)
    cerebro, strat = run(BuyOnce, fr)
    f = fr["S0"]
    date, price, size = strat.fills[0]
    assert date == f.index[3].date()          # placed on bar 3 (len==3), filled on bar 4
    assert price == f["open"].iloc[3]
    assert size == 10
    # value marked at the close of each bar
    assert strat.values[-1] == 100_000 - 10 * f["open"].iloc[3] + 10 * f["close"].iloc[-1]
    assert cerebro.broker.getposition(strat.data).size == 10


def test_params_are_consumed_before_init():
    fr = frames(1)
    _, strat = run(BuyOnce, fr, size=5)
    assert strat.p.size == 5 and strat.fills[0][2] == 5


class Monthly(tw.Strategy):
    params = (("target", 0.3),)

    def __init__(self):
        self.last = None
        self.values = []

    def next(self):
        self.values.append(self.broker.getvalue())
        today = self.datas[0].datetime.date(0)
        if (today.year, today.month) != self.last:
            self.last = (today.year, today.month)
            for d in self.datas:
                self.order_target_percent(d, target=self.p.target)


def test_order_target_percent_sizes_on_value_at_close_and_truncates():
    fr = frames(3, n_bars=15)  # all in January: one rebalance, on the first bar
    cerebro, strat = run(Monthly, fr)
    f = fr["S0"]
    expected = int(100_000 * 0.3 // f["close"].iloc[0])
    assert cerebro.broker.getposition(strat.datas[0]).size == expected
    assert len(strat.values) == 15


def test_lines_ago_indexing_and_get():
    fr = frames(1, n_bars=10)

    class Peek(tw.Strategy):
        def __init__(self):
            self.seen = []

        def next(self):
            if len(self) >= 3:
                self.seen.append((self.data.close[0], self.data.close[-2], self.data.close.get(size=3)))

    _, strat = run(Peek, fr)
    c = fr["S0"]["close"].to_numpy()
    assert strat.seen[0][0] == c[2] and strat.seen[0][1] == c[0]
    assert strat.seen[0][2] == c[:3].tolist()


def test_close_flattens_and_notifies_trade():
    fr = frames(1, n_bars=12)

    class InOut(tw.Strategy):
        def __init__(self):
            self.trades = []

        def next(self):
            if len(self) == 2:
                self.buy(size=4)
            if len(self) == 6:
                self.close()

        def notify_trade(self, trade):
            if trade.isclosed:
                self.trades.append(trade.pnl)

    cerebro, strat = run(InOut, fr)
    f = fr["S0"]
    assert cerebro.broker.getposition(strat.data).size == 0
    assert strat.trades and abs(strat.trades[0] - 4 * (f["open"].iloc[6] - f["open"].iloc[2])) < 1e-9


def test_margin_when_cash_is_short():
    fr = frames(1, n_bars=8)

    class TooBig(tw.Strategy):
        def __init__(self):
            self.statuses = []

        def next(self):
            if len(self) == 1:
                self.buy(size=10_000)

        def notify_order(self, order):
            self.statuses.append(order.getstatusname())

    _, strat = run(TooBig, fr)
    assert strat.statuses == ["Submitted", "Accepted", "Margin"]


def test_num2date_roundtrip():
    import datetime as dt

    d = dt.datetime(2024, 5, 15, 13, 30, 15)
    assert tw.num2date(tw.date2num(d)) == d
    assert tw.num2date(tw.date2num(d.date())).date() == d.date()
