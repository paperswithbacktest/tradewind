"""Cerebro: the single event loop.

For every date of the clock (the sorted union of the feeds' bar dates): feeds load their
bar or keep their last, the broker executes the orders accepted on the previous bar,
timers fire, notifications are delivered, the strategy steps, analyzers step.
"""

from __future__ import annotations

import numpy as np

from .broker import BackBroker
from .dateutils import num2date
from .lines import building
from .params import ParamsBase
from .timer import Timer


class Cerebro(ParamsBase):
    params = (
        ("preload", True),
        ("runonce", True),
        ("maxcpus", None),
        ("stdstats", True),
        ("oldbuysell", False),
        ("oldtrades", False),
        ("lookahead", 0),
        ("exactbars", False),
        ("optdatas", True),
        ("optreturn", True),
        ("objcache", False),
        ("live", False),
        ("writer", False),
        ("tradehistory", False),
        ("oldsync", False),
        ("tz", None),
        ("cheat_on_open", False),
        ("broker_coo", True),
        ("quicknotify", False),
    )

    def __init__(self):
        self.datas = []
        self.strats = []
        self.analyzers = []
        self.observers = []
        self.sizers = {}
        self._broker = BackBroker()
        self._broker._env = self
        self.runstrats = []
        if self.p.cheat_on_open:
            self._broker.set_coo(True)

    # -- configuration -----------------------------------------------------------------
    @property
    def broker(self):
        return self._broker

    @broker.setter
    def broker(self, broker):
        self._broker = broker
        broker._env = self

    def setbroker(self, broker):
        self.broker = broker
        return broker

    def getbroker(self):
        return self._broker

    def adddata(self, data, name=None):
        if name is not None:
            data._name = name
        data._env = self
        self.datas.append(data)
        return data

    def addstrategy(self, strategy, *args, **kwargs):
        self.strats.append((strategy, args, kwargs))
        return len(self.strats) - 1

    def optstrategy(self, *args, **kwargs):
        raise NotImplementedError("optstrategy is not part of Tradewind v0; see spec/01-scope.md")

    def addanalyzer(self, ancls, *args, **kwargs):
        self.analyzers.append((ancls, args, kwargs))

    def addobserver(self, obscls, *args, **kwargs):
        self.observers.append((obscls, args, kwargs))

    def addobservermulti(self, obscls, *args, **kwargs):
        self.observers.append((obscls, args, kwargs))

    def addsizer(self, sizercls, *args, **kwargs):
        self.sizers[None] = (sizercls, args, kwargs)

    def addsizer_by_idx(self, idx, sizercls, *args, **kwargs):
        self.sizers[idx] = (sizercls, args, kwargs)

    def addwriter(self, *args, **kwargs):
        pass

    def addcalendar(self, cal):
        pass

    def addtz(self, tz):
        pass

    def add_signal(self, *args, **kwargs):
        raise NotImplementedError("signals are not part of Tradewind v0")

    def resampledata(self, *args, **kwargs):
        raise NotImplementedError("resampling is not part of Tradewind v0")

    def replaydata(self, *args, **kwargs):
        raise NotImplementedError("replaying is not part of Tradewind v0")

    def plot(self, *args, **kwargs):
        raise NotImplementedError("plotting is not part of Tradewind v0")

    # -- run ------------------------------------------------------------------------------
    def run(self, **kwargs):
        for k, v in kwargs.items():
            if k in self.p:
                setattr(self.p, k, v)
        if not self.datas:
            return []
        for data in self.datas:
            data._start()
        clock = np.unique(np.concatenate([d._dtnum for d in self.datas]))

        strategies = []
        for cls, args, skwargs in self.strats:
            strat = self._build_strategy(cls, args, skwargs)
            strategies.append(strat)
        self.runstrats = [strategies]

        for strat in strategies:
            strat.start()
            for an in strat.analyzers:
                an.start()

        broker = self._broker
        for dtnum in clock:
            # The feeds move to the new bar first, then the broker fills the previous
            # bar's orders against it, then the strategy is notified and steps. Observed
            # on the oracle: inside notify_order the date is the fill bar's, and a
            # commission scheme that reads the bar's date (the toolbox's yearly schedule)
            # prices a fill on 1 January at the new year's rate -- two corpus strategies
            # rebalancing at year-end diverged when fills were priced before the move.
            upcoming = {}
            for d in self.datas:
                if d._advance_to(dtnum):
                    upcoming[d] = (d.open[0], d.high[0], d.low[0], d.close[0], dtnum)
            broker.next(upcoming)
            date = num2date(dtnum).date()
            for strat in strategies:
                for ind in strat._indicators:
                    ind._sync()
                strat._check_timers(date, Timer.SESSION_START)
                self._deliver(strat)
                strat._step()
                for an in strat.analyzers:
                    if not strat._ready():
                        an.prenext()
                    else:
                        an.next()
                strat._check_timers(date, Timer.SESSION_END)

        # Observed on the oracle: notifications are delivered at the start of the next
        # bar only, so what an order placed on the last bar produced is never notified,
        # and nothing is delivered after the loop.
        for strat in strategies:
            strat.stop()
            for an in strat.analyzers:
                an.stop()
        return strategies

    def _deliver(self, strat):
        broker = self._broker
        while True:
            item = broker.get_notification()
            if item is None:
                break
            owner = getattr(item, "owner", None)
            target = owner if owner is not None else strat
            target._notify(item)

    def _build_strategy(self, cls, args, skwargs):
        env = self
        datas = list(self.datas)
        broker = self._broker

        original = getattr(cls, "_before_init", None)

        def before(obj):
            obj._bind(env, datas, broker)
            if original is not None:
                original(obj)
            env._building = obj

        # MetaParams calls cls._before_init(obj) before __init__; install the binding
        # on a throwaway subclass so the user's class is untouched.
        bound = type(cls.__name__, (cls,), {"_before_init": before, "__module__": cls.__module__,
                                           "__qualname__": cls.__qualname__})
        # The build stage: an operator on lines inside ``__init__`` builds a line
        # (``lineops``), the same operator in ``next()`` reads current values.
        try:
            with building():
                strat = bound(*args, **skwargs)
        finally:
            env._building = None
        strat.__class__ = cls
        env._building = None
        for ind in strat._indicators:
            ind._compute()
        for ancls, aargs, akwargs in self.analyzers:
            akwargs = dict(akwargs)
            name = akwargs.pop("_name", None) or ancls.__name__.lower()
            an = self._build_analyzer(ancls, strat, aargs, akwargs)
            an._name = name
            strat.analyzers.append(an)
        return strat

    def _build_analyzer(self, ancls, strat, args, kwargs):
        def before(obj):
            obj._bind(strat)
            ancls._before_init(obj)

        bound = type(ancls.__name__, (ancls,), {"_before_init": before, "__module__": ancls.__module__})
        an = bound(*args, **kwargs)
        an.__class__ = ancls
        return an
