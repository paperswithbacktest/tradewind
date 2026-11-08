"""The strategy contract: what a corpus ``strategy.py`` inherits.

Parameters are consumed before ``__init__`` (``params.MetaParams``); Cerebro binds the
feeds, the broker and the environment before ``__init__`` too, so an ``__init__`` that
builds indicators from ``self.data`` works. Order helpers delegate to the broker.
"""

from __future__ import annotations

import numpy as np

from .lines import DatetimeLine
from .order import Order
from .params import ParamsBase
from .timer import Timer


class _ClockLine(DatetimeLine):
    """``self.datetime`` of a strategy: the clock's slices run so far, not a feed's line.

    Observed on the oracle (probe ``strategy_clock``): it holds the time of every slice of
    the run's clock (the union of the feeds' dates), so it moves while ``data0`` has not
    listed yet; it is not preloaded -- ``date(1)`` raises ``IndexError`` -- and an ``ago``
    reaching before its first slice wraps like a Python list over the slices seen
    (``date(-1)`` on the first slice is that slice). ``len(self.datetime)`` and
    ``len(strategy)`` both count the slices.
    """

    __slots__ = ()

    def __getitem__(self, ago: int) -> float:
        n = self.idx + 1
        i = self.idx + ago
        if i >= n or i < -n:
            raise IndexError("array index out of range")
        return float(self.array[i if i >= 0 else n + i])

    def buflen(self) -> int:
        return self.idx + 1

    def get(self, ago: int = 0, size: int = 1) -> list:
        end = self.idx + ago + 1
        start = end - size
        if start < 0 or end > self.idx + 1:
            return []
        return self.array[start:end].tolist()


class _DNames(dict):
    """``self.dnames.SPY`` and ``self.dnames['SPY']`` both resolve a feed by name."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _Positions(dict):
    """``self.positions[data]``: a Position for every feed, empty when flat."""

    def __init__(self, broker, datas):
        super().__init__()
        self._broker = broker
        self._datas = datas

    def __getitem__(self, data):
        return self._broker.getposition(data)

    def items(self):
        return [(d, self._broker.getposition(d)) for d in self._datas]

    def values(self):
        return [self._broker.getposition(d) for d in self._datas]

    def keys(self):
        return list(self._datas)


class Strategy(ParamsBase):
    def _tw_before_init(self):
        # Filled by Cerebro right before this is called; defaults keep a bare
        # ``Strategy()`` constructible in tests.
        if not hasattr(self, "datas"):
            self._tw_bind(env=None, datas=[], broker=None)
        self._tw_orders = []
        self._tw_timers = []
        self._tw_minperiod = 1
        self._tw_indicators = []
        self._tw_nextstarted = False
        self._tw_listed = False
        self._tw_warm = False
        self.analyzers = _Analyzers()
        self.stats = []

    def _tw_bind(self, env, datas, broker):
        self.env = self.cerebro = env
        self.datas = list(datas)
        self.data = self.datas[0] if self.datas else None
        for i, d in enumerate(self.datas):
            setattr(self, f"data{i}", d)
            setattr(self, f"data_{i}", d)
        self.dnames = _DNames((d._name, d) for d in self.datas if d._name)
        self.broker = broker
        self.positions = _Positions(broker, self.datas) if broker is not None else {}
        self.positionsbyname = _DNames()
        self.datetime = _ClockLine(np.empty(0), "datetime", owner=self)

    def _tw_start_clock(self, times):
        """The run's slice times, before the first slice (``Cerebro.run``)."""
        self.datetime = _ClockLine(np.asarray(times, dtype=np.float64), "datetime", owner=self)

    def _tw_tick(self, position: int):
        """Slice ``position`` of the clock has started (phase 2, with the feeds)."""
        self.datetime.idx = position

    # -- introspection -------------------------------------------------------------------
    def __len__(self):
        # observed on the oracle: the slices of the clock run so far, whatever data0 holds
        return self.datetime.idx + 1

    def getdatanames(self):
        return [d._name for d in self.datas]

    def getdatabyname(self, name):
        return self.dnames[name]

    @property
    def position(self):
        return self.getposition()

    def getposition(self, data=None, broker=None):
        data = self.data if data is None else data
        broker = self.broker if broker is None else broker
        return broker.getposition(data)

    def getpositionbyname(self, name=None, broker=None):
        data = self.data if name is None else self.dnames[name]
        return self.getposition(data, broker)

    def getpositions(self, broker=None):
        return (self.broker if broker is None else broker).positions

    def getsizing(self, data=None, isbuy=True):
        return 1

    def getsizer(self):
        return None

    def setsizer(self, sizer):
        self._tw_sizer = sizer

    # -- lifecycle hooks -------------------------------------------------------------------
    def start(self):
        pass

    def prenext(self):
        pass

    def nextstart(self):
        self.next()

    def next(self):
        pass

    def stop(self):
        pass

    def notify_order(self, order):
        pass

    def notify_trade(self, trade):
        pass

    def notify_cashvalue(self, cash, value):
        pass

    def notify_fund(self, cash, value, fundvalue, shares):
        pass

    def notify_timer(self, timer, when, *args, **kwargs):
        pass

    def notify_data(self, data, status, *args, **kwargs):
        pass

    def notify_store(self, msg, *args, **kwargs):
        pass

    # -- orders -------------------------------------------------------------------------------
    def buy(self, data=None, size=None, price=None, plimit=None, exectype=None, valid=None,
            tradeid=0, oco=None, trailamount=None, trailpercent=None, parent=None, transmit=True,
            **kwargs):
        data = self.data if data is None else self._tw_resolve(data)
        size = self.getsizing(data, isbuy=True) if size is None else size
        if not size:
            return None
        order = self.broker.buy(self, data, size=abs(size), price=price, plimit=plimit, exectype=exectype,
                                valid=valid, tradeid=tradeid, **kwargs)
        self._tw_orders.append(order)
        return order

    def sell(self, data=None, size=None, price=None, plimit=None, exectype=None, valid=None,
             tradeid=0, oco=None, trailamount=None, trailpercent=None, parent=None, transmit=True,
             **kwargs):
        data = self.data if data is None else self._tw_resolve(data)
        size = self.getsizing(data, isbuy=False) if size is None else size
        if not size:
            return None
        order = self.broker.sell(self, data, size=abs(size), price=price, plimit=plimit, exectype=exectype,
                                 valid=valid, tradeid=tradeid, **kwargs)
        self._tw_orders.append(order)
        return order

    def close(self, data=None, size=None, **kwargs):
        data = self.data if data is None else self._tw_resolve(data)
        possize = self.getposition(data).size
        size = abs(size if size is not None else possize)
        if not possize or not size:
            return None
        size = min(size, abs(possize))
        if possize > 0:
            return self.sell(data=data, size=size, **kwargs)
        return self.buy(data=data, size=size, **kwargs)

    def cancel(self, order):
        return self.broker.cancel(order)

    def order_target_size(self, data=None, target=0, **kwargs):
        data = self.data if data is None else self._tw_resolve(data)
        possize = self.getposition(data).size
        if target > possize:
            return self.buy(data=data, size=target - possize, **kwargs)
        if target < possize:
            return self.sell(data=data, size=possize - target, **kwargs)
        return None

    def order_target_value(self, data=None, target=0.0, price=None, **kwargs):
        """Size the *difference* between the target value and the position's value.

        Observed on the oracle: the integer truncation applies to the value gap, not to
        the target size -- truncating the target and subtracting the position overshoots
        by one share whenever the fractional parts straddle an integer.
        """
        data = self.data if data is None else self._tw_resolve(data)
        price = data.close[0] if price is None else price
        possize = self.getposition(data).size
        if not target and possize:
            return self.close(data=data, size=possize, **kwargs)
        # the position's value through the broker, as the oracle does, **flat or not**:
        # that read lands any pending ``add_cash`` on the spot (``BackBroker.get_value``),
        # so a buy placed after a same-bar withdrawal is checked at its fill against the
        # reduced cash (probe ``add_cash_flat``; corpus ``investing_in_the_s_p_index…``,
        # 2023-02-02: a monthly fee withdrawn before a re-entry turns it into Margin)
        value = self.broker.get_value(datas=[data])
        # the security's buying-power model turns the value gap into a size (the oracle's:
        # the scheme's ``getsize`` on the gap, toward zero)
        security = self.broker.register(data)
        size = security.models.buying_power.quantity_for_target_value(security, target, value, price)
        # the side is the gap's: a negative price (crude oil, April 2020) gives a negative
        # size whose magnitude is still the order's, as in the oracle
        if target > value:
            return self.buy(data=data, size=abs(size), **kwargs) if size else None
        if target < value:
            return self.sell(data=data, size=abs(size), **kwargs) if size else None
        return None

    def order_target_percent(self, data=None, target=0.0, **kwargs):
        data = self.data if data is None else self._tw_resolve(data)
        possize = self.getposition(data).size
        target *= self.broker.getvalue()
        if not target and possize:
            return self.close(data=data, size=possize, **kwargs)
        return self.order_target_value(data=data, target=target, **kwargs)

    def _tw_resolve(self, data):
        if isinstance(data, str):
            return self.dnames[data]
        return data

    # -- timers ---------------------------------------------------------------------------------
    def add_timer(self, when=Timer.SESSION_START, offset=None, repeat=None, weekdays=(), weekcarry=False,
                  monthdays=(), monthcarry=True, allow=None, tzdata=None, cheat=False, *args, **kwargs):
        import datetime as _dt

        timer = Timer(len(self._tw_timers), self, False, when, offset or _dt.timedelta(),
                      repeat or _dt.timedelta(), weekdays, weekcarry, monthdays, monthcarry,
                      allow, tzdata, cheat, *args, **kwargs)
        self._tw_timers.append(timer)
        return timer

    # -- engine-side stepping ---------------------------------------------------------------------
    def _tw_check_timers(self, date, when):
        for timer in self._tw_timers:
            if timer.when == when and timer.check(date):
                self.notify_timer(timer, date, *timer.args, **timer.kwargs)

    def _tw_ready(self) -> bool:
        """``next`` runs once **every feed has delivered a bar** and **every indicator has
        reached its own min period on its own input**. Observed on the oracle (probes
        ``line_edges``, ``strategy_clock``): a feed listing 30 bars after the others holds
        the strategy in ``prenext`` for those 30 bars, and an SMA(5) on that late feed for
        4 more, while an SMA(5) on a feed that listed early adds nothing -- the warm-up is
        counted on each indicator's clock, not on ``data0``'s nor on the strategy's."""
        if self._tw_warm:
            return True
        # a feed's length never shrinks during a run: once all have listed, they stay so
        if not self._tw_listed:
            self._tw_listed = all(len(d) > 0 for d in self.datas)
            if not self._tw_listed:
                return False
        for ind in self._tw_indicators:
            clock = ind._tw_clock
            if clock is not None and len(clock) < ind._tw_minperiod:
                return False
        self._tw_warm = True
        return True

    def _tw_step(self):
        """prenext / nextstart / next by the clock feed's length against the min period."""
        if not self._tw_ready():
            self.prenext()
        elif not self._tw_nextstarted:
            self._tw_nextstarted = True
            self.nextstart()
        else:
            self.next()

    def _tw_notify(self, order_or_trade):
        if isinstance(order_or_trade, Order):
            self.notify_order(order_or_trade)
            for analyzer in self.analyzers:
                analyzer.notify_order(order_or_trade)
        else:
            self.notify_trade(order_or_trade)
            for analyzer in self.analyzers:
                analyzer.notify_trade(order_or_trade)


class _Analyzers(list):
    """``self.analyzers.<name>`` and iteration over the list."""

    def __getattr__(self, name):
        for item in self:
            if getattr(item, "_name", None) == name:
                return item
        raise AttributeError(name)

    def getbyname(self, name):
        return getattr(self, name)
