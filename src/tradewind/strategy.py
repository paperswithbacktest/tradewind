"""The strategy contract: what a corpus ``strategy.py`` inherits.

Parameters are consumed before ``__init__`` (``params.MetaParams``); Cerebro binds the
feeds, the broker and the environment before ``__init__`` too, so an ``__init__`` that
builds indicators from ``self.data`` works. Order helpers delegate to the broker.
"""

from __future__ import annotations

from .order import Order
from .params import ParamsBase
from .timer import Timer


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
    def _before_init(self):
        # Filled by Cerebro right before this is called; defaults keep a bare
        # ``Strategy()`` constructible in tests.
        if not hasattr(self, "datas"):
            self._bind(env=None, datas=[], broker=None)
        self._orders = []
        self._timers = []
        self._minperiod = 1
        self._indicators = []
        self._nextstarted = False
        self.analyzers = _Analyzers()
        self.stats = []

    def _bind(self, env, datas, broker):
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

    # -- introspection -------------------------------------------------------------------
    def __len__(self):
        return len(self.data) if self.data is not None else 0

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
        self._sizer = sizer

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
        data = self.data if data is None else self._resolve(data)
        size = self.getsizing(data, isbuy=True) if size is None else size
        if not size:
            return None
        order = self.broker.buy(self, data, size=abs(size), price=price, plimit=plimit, exectype=exectype,
                                valid=valid, tradeid=tradeid, **kwargs)
        self._orders.append(order)
        return order

    def sell(self, data=None, size=None, price=None, plimit=None, exectype=None, valid=None,
             tradeid=0, oco=None, trailamount=None, trailpercent=None, parent=None, transmit=True,
             **kwargs):
        data = self.data if data is None else self._resolve(data)
        size = self.getsizing(data, isbuy=False) if size is None else size
        if not size:
            return None
        order = self.broker.sell(self, data, size=abs(size), price=price, plimit=plimit, exectype=exectype,
                                 valid=valid, tradeid=tradeid, **kwargs)
        self._orders.append(order)
        return order

    def close(self, data=None, size=None, **kwargs):
        data = self.data if data is None else self._resolve(data)
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
        data = self.data if data is None else self._resolve(data)
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
        data = self.data if data is None else self._resolve(data)
        price = data.close[0] if price is None else price
        possize = self.getposition(data).size
        comminfo = self.broker.getcommissioninfo(data)
        if not target and possize:
            return self.close(data=data, size=possize, **kwargs)
        value = possize * price
        if target > value:
            size = comminfo.getsize(price, target - value)
            return self.buy(data=data, size=size, **kwargs) if size else None
        if target < value:
            size = comminfo.getsize(price, value - target)
            return self.sell(data=data, size=size, **kwargs) if size else None
        return None

    def order_target_percent(self, data=None, target=0.0, **kwargs):
        data = self.data if data is None else self._resolve(data)
        possize = self.getposition(data).size
        target *= self.broker.getvalue()
        if not target and possize:
            return self.close(data=data, size=possize, **kwargs)
        return self.order_target_value(data=data, target=target, **kwargs)

    def _resolve(self, data):
        if isinstance(data, str):
            return self.dnames[data]
        return data

    # -- timers ---------------------------------------------------------------------------------
    def add_timer(self, when=Timer.SESSION_START, offset=None, repeat=None, weekdays=(), weekcarry=False,
                  monthdays=(), monthcarry=True, allow=None, tzdata=None, cheat=False, *args, **kwargs):
        import datetime as _dt

        timer = Timer(len(self._timers), self, False, when, offset or _dt.timedelta(),
                      repeat or _dt.timedelta(), weekdays, weekcarry, monthdays, monthcarry,
                      allow, tzdata, cheat, *args, **kwargs)
        self._timers.append(timer)
        return timer

    # -- engine-side stepping ---------------------------------------------------------------------
    def _check_timers(self, date, when):
        for timer in self._timers:
            if timer.when == when and timer.check(date):
                self.notify_timer(timer, date, *timer.args, **timer.kwargs)

    def _step(self):
        """prenext / nextstart / next by the clock feed's length against the min period."""
        if len(self) < self._minperiod:
            self.prenext()
        elif not self._nextstarted:
            self._nextstarted = True
            self.nextstart()
        else:
            self.next()

    def _notify(self, order_or_trade):
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
