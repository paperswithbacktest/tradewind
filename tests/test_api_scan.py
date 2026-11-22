"""The surfaces found by the API scan of the catalogue (spec/02, "API scan 2026-11-22") that no
parity run had exercised, pinned without the oracle; the probes ``cheat_on_open*``,
``oco_groups``, ``timer_clock``, ``line_delay`` and ``target_value_price`` of
``tests/oracle/strategies.py`` hold them against it."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

import tradewind as tw


def frame(n=12):
    close = 100.0 + np.arange(n, dtype=float)
    return pd.DataFrame({"open": close - 0.5, "high": close + 1.0, "low": close - 1.0, "close": close,
                         "volume": 1e5}, index=pd.bdate_range("2021-01-04", periods=n))


def run(cls, n=12, **cerebro_kwargs):
    engine = tw.Engine(stdstats=False, **cerebro_kwargs)
    engine.adddata(tw.feeds.PandasData(dataname=frame(n)), name="A")
    engine.addstrategy(cls)
    return engine.run()[0], engine


# -- cheat on open ---------------------------------------------------------------------------

def _cheat(coo, coc, late):
    rows, fills = [], []

    class S(tw.Strategy):
        def __init__(self):
            if late:
                self.cerebro.p.cheat_on_open = True
            if coo:
                self.broker.set_coo(True)
            if coc:
                self.broker.set_coc(True)

        def next_open(self):
            rows.append((len(self), self.data.datetime.date(0).day, self.data.open[0]))
            if len(self) == 3:
                self.buy(size=1)

        def notify_order(self, order):
            if order.status == order.Completed:
                fills.append((len(self), order.executed.price))

    run(S, **({} if late else {"cheat_on_open": True}))
    return rows, fills


def test_next_open_sees_the_new_bar_with_the_previous_clock():
    rows, _ = _cheat(coo=False, coc=False, late=True)
    # first call on the second bar (after the first ``next``), len still the previous count
    assert rows[0] == (1, 5, 100.5)
    assert rows[2] == (3, 7, 102.5)


def test_next_open_orders_fill_at_this_open_when_the_broker_cheats_on_open():
    assert _cheat(coo=False, coc=False, late=False)[1] == [(4, 102.5)]   # engine kwarg: coo set
    assert _cheat(coo=True, coc=False, late=True)[1] == [(4, 102.5)]
    assert _cheat(coo=False, coc=False, late=True)[1] == [(5, 103.5)]    # late flag: next open
    assert _cheat(coo=True, coc=True, late=True)[1] == [(4, 103.0)]      # coc: this bar's close


def test_order_coc_false_turns_the_broker_cheat_on_close_off():
    fills = []

    class S(tw.Strategy):
        def __init__(self):
            self.broker.set_coc(True)

        def next(self):
            if len(self) == 2:
                self.buy(size=1)
            if len(self) == 4:
                self.buy(size=1, coc=False)
            if len(self) == 6:
                self.buy(size=1, coc=True)

        def notify_order(self, order):
            if order.status == order.Completed:
                fills.append(order.executed.price)

    run(S)
    assert fills == [101.0, 103.5, 105.0]


# -- one-cancels-others and validity ---------------------------------------------------------

def test_oco_members_end_together_latest_first():
    log = []

    class S(tw.Strategy):
        def next(self):
            n, c = len(self), self.data.close[0]
            if n == 2:
                a = self.buy(size=1, exectype=tw.Order.Limit, price=c * 1.5)        # fills
                self.sell(size=1, exectype=tw.Order.Limit, price=c * 1.5, oco=a)
                self.buy(size=1, exectype=tw.Order.Limit, price=c * 0.5, oco=a)
            if n == 5:
                self.a = self.buy(size=1, exectype=tw.Order.Limit, price=c * 0.5)
                self.b = self.sell(size=1, exectype=tw.Order.Limit, price=c * 1.5, oco=self.a)
            if n == 7:
                self.cancel(self.b)
            if n == 8:
                a = self.buy(size=1, exectype=tw.Order.Limit, price=c * 0.5,
                             valid=self.data.datetime.date(0) + dt.timedelta(days=1))
                self.sell(size=1, exectype=tw.Order.Limit, price=c * 1.5, oco=a)

        def notify_order(self, order):
            if order.status not in (order.Submitted, order.Accepted):
                log.append((len(self), order.ref - self.ref0, order.getstatusname()))

        def start(self):
            self.ref0 = None

    class T(S):
        def notify_order(self, order):
            if self.ref0 is None:
                self.ref0 = order.ref
            super().notify_order(order)

    run(T)
    assert log == [(3, 0, "Completed"), (3, 2, "Canceled"), (3, 1, "Canceled"),
                   (8, 4, "Canceled"), (8, 3, "Canceled"),
                   (10, 5, "Expired"), (10, 6, "Canceled")]


@pytest.mark.parametrize("valid, expired_at", [
    (lambda d: d, 3),                                                  # a date: its midnight
    (lambda d: dt.datetime.combine(d, dt.time()) + dt.timedelta(days=1), 4),
    (lambda d: dt.timedelta(days=1), 4),                                # from the bar it is made on
])
def test_valid_as_date_datetime_or_timedelta(valid, expired_at):
    seen = []

    class S(tw.Strategy):
        def next(self):
            if len(self) == 2:
                self.buy(size=1, exectype=tw.Order.Limit, price=1.0, valid=valid(self.data.datetime.date(0)))

        def notify_order(self, order):
            if order.status == order.Expired:
                seen.append(len(self))

    run(S)
    assert seen == [expired_at]


# -- timers ----------------------------------------------------------------------------------

def test_notify_timer_runs_on_the_previous_clock_with_a_datetime():
    rows, submitted = [], []

    class S(tw.Strategy):
        def __init__(self):
            self.add_timer(when=tw.Timer.SESSION_START, monthdays=[1], monthcarry=True)

        def notify_timer(self, timer, when, *args, **kwargs):
            rows.append((len(self), len(self.data), when))
            self.buy(size=1)

        def notify_order(self, order):
            if order.status == order.Submitted:
                submitted.append(len(self))

    engine = tw.Engine(stdstats=False)
    idx = pd.bdate_range("2021-01-25", periods=10)
    engine.adddata(tw.feeds.PandasData(dataname=frame(10).set_axis(idx)), name="A")
    engine.addstrategy(S)
    engine.run()
    # the first bar is past the 1st: carried there; then 1 February
    assert rows == [(0, 1, dt.datetime(2021, 1, 25)), (5, 6, dt.datetime(2021, 2, 1))]
    assert submitted == [2, 7]       # delivered with the next bar's notifications


# -- lines, targets, schemes -----------------------------------------------------------------

def test_a_line_called_with_an_ago_is_a_delayed_line_in_init_only():
    rows = []

    class S(tw.Strategy):
        def __init__(self):
            self.lag = self.data.close(-1)
            self.hh = tw.ind.Highest(self.data.close(-1), period=3)

        def next(self):
            with pytest.raises(TypeError):
                float(self.data.close(-1))
            rows.append((len(self), self.lag[0], self.hh[0]))

    run(S, n=6)
    assert rows == [(4, 102.0, 102.0), (5, 103.0, 103.0), (6, 104.0, 104.0)]


def test_order_target_value_price_is_the_order_price():
    orders = []

    class S(tw.Strategy):
        def next(self):
            if len(self) == 2:
                self.order_target_value(self.data, target=1000.0, exectype=tw.Order.Limit, price=50.0)

        def notify_order(self, order):
            if order.status == order.Submitted:
                orders.append((order.getordername(), order.size, order.price))

    run(S)
    assert orders == [("Limit", 20, 50.0)]


def test_percentage_commission_parameter_reads_the_converted_rate():
    class Scheme(tw.CommInfoBase):
        params = (("commission", 0.2), ("stocklike", True), ("commtype", tw.CommInfoBase.COMM_PERC))

        def _getcommission(self, size, price, pseudoexec):
            return abs(size) * price * self.p.commission + 0.5

    scheme = Scheme()
    assert scheme.p.commission == pytest.approx(0.002)
    assert scheme.getcommission(10, 100.0) == pytest.approx(2.5)
