"""Engine: the single event loop (the ``Engine`` facade of spec/05 § 2).

``Cerebro`` is the same class under backtrader's name, kept for good: code written
for backtrader (``bt.Cerebro()``) runs unchanged.

For every slice of the clock (the sorted union of the feeds' bar dates), sixteen phases in
a fixed order (spec/05 § 3, ``clock.Phase``): the feeds load their bar or keep their last,
the broker applies corporate actions and accruals, accepts the previous bar's orders and
fills them, lands cash transfers and marks the portfolio, indicators step, timers fire,
the event bus delivers the notifications, the strategy steps, analyzers step.
"""

from __future__ import annotations

from .broker import BackBroker
from .clock import Phase, UnionClock
from .dateutils import num2date
from .events import OrderEvent, TradeEvent, UniverseEvent
from .indicator import reset_construction
from .lines import building
from .market import MarketState
from .params import ParamsBase
from .timer import Timer


class Engine(ParamsBase):
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
        self._broker._tw_env = self
        self.runstrats = []
        self._trace = None              # a list to record the phases of each slice (tests)
        self._actions_provider = None
        self._universe_provider = None
        if self.p.cheat_on_open:
            self._broker.set_coo(True)
        # Tradewind's own: the clock (None: UnionClock, the oracle default), the seed of the
        # global generators (None: left as they are), declared data revisions, fingerprint
        self._clock_spec = self._exchange_clock = self.seed = self._fingerprint = None
        self._provenance = {}

    # -- configuration -----------------------------------------------------------------
    @property
    def broker(self):
        return self._broker

    @broker.setter
    def broker(self, broker):
        self._broker = broker
        broker._tw_env = self
        for data in self.datas:
            broker.register(data)

    def setbroker(self, broker):
        self.broker = broker
        return broker

    def getbroker(self):
        return self._broker

    def adddata(self, data, name=None, provenance=None):
        """Add a feed (``SecurityId`` = adddata order). ``provenance`` (Tradewind): where
        the data comes from, such as ``{"dataset": ..., "revision": ...}``, recorded in the
        run's fingerprint."""
        if name is not None:
            data._name = name
        if provenance is not None:
            data.provenance = dict(provenance)
        data._tw_env = self
        self.datas.append(data)
        # SecurityId = adddata order (spec/05 § 4.1)
        self._broker.register(data)
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
    def set_corporate_actions(self, provider) -> None:
        """A ``CorporateActionProvider`` whose events reach phase 3 (none by default: the
        oracle's feeds are adjusted upstream)."""
        self._actions_provider = provider

    def set_clock(self, clock) -> None:
        """Run on ``clock`` instead of the default ``UnionClock``: an object with
        ``bind(dtnums, actions=, universe=)`` returning the clock, such as
        ``clock.ExchangeClock(calendars.NYSE, interval="1min", fill_forward=True)``.
        ``None`` restores the default."""
        if clock is not None and not hasattr(clock, "bind"):
            raise TypeError("set_clock needs a clock with bind(dtnums, actions=, universe=)")
        self._clock_spec = clock

    def set_hours(self, hours, data=None) -> None:
        """The ``ExchangeHours`` of every security (or of ``data``'s alone), read through
        ``data.security.hours``; ``AlwaysOpen`` by default. A name (``"NYSE"``, ``"CME"``,
        ``"CRYPTO"``) picks a shipped calendar. It does not change the clock: see
        ``set_clock``."""
        if isinstance(hours, str):
            from .calendars import get_calendar

            hours = get_calendar(hours)
        broker = self._broker
        if data is None:
            broker.hours = hours
            for security in broker.securities:
                security.hours = hours
        else:
            broker.register(data).hours = hours

    def set_seed(self, seed) -> None:
        """Seed Python's and numpy's global generators at ``run()``, before the strategies
        are built (also ``run(seed=...)``). ``None``, the default, seeds nothing: the
        generators are left as they are. Recorded in the run's fingerprint."""
        self.seed = seed

    def add_provenance(self, name: str, **info) -> None:
        """Declare data the strategy reads by itself (a file, a dataset at a revision) so
        that the run's fingerprint records it: ``add_provenance("fundamentals",
        dataset="...", revision="...")``."""
        self._provenance[str(name)] = dict(info)

    @property
    def fingerprint(self):
        """The ``RunFingerprint`` of the last ``run()`` (None before): engine version, data
        hashes and revisions, strategy code and parameters, broker configuration, seed.
        Built on first read."""
        recorder = self._fingerprint
        return recorder.build() if recorder is not None else None

    def set_universe(self, provider) -> None:
        """A ``UniverseProvider`` whose changes reach phase 1 (none by default)."""
        self._universe_provider = provider

    def run(self, **kwargs):
        if "seed" in kwargs:
            self.seed = kwargs.pop("seed")
        for k, v in kwargs.items():
            if k in self.p:
                setattr(self.p, k, v)
        if not self.datas:
            return []
        for data in self.datas:
            data._start()
        broker = self._broker
        for data in self.datas:
            broker.register(data)
        # one security per feed, in SecurityId (adddata) order
        feeds = [sec.feed for sec in broker.securities]
        for feed in feeds:
            if not feed._started:
                feed._start()
        dtnums = [f._dtnum for f in feeds]
        if self._clock_spec is None:
            clock = UnionClock(dtnums, actions=getattr(self, "_actions_provider", None),
                               universe=getattr(self, "_universe_provider", None))
            self._exchange_clock = None
        else:
            clock = self._clock_spec.bind(dtnums, actions=self._actions_provider,
                                          universe=self._universe_provider)
            self._exchange_clock = clock if getattr(clock, "sessions", None) is not None else None
        market = MarketState(feeds)
        broker.start(feeds, market)

        seed = self.seed
        if seed is not None:
            import random

            import numpy as np

            random.seed(seed)
            np.random.seed(seed)
        from .fingerprint import FingerprintRecorder

        recorder = FingerprintRecorder(self, seed)
        self._fingerprint = recorder
        strategies = []
        for cls, args, skwargs in self.strats:
            strat = self._build_strategy(cls, args, skwargs)
            strategies.append(strat)
        recorder.set_strategies(strategies)
        self.runstrats = [strategies]
        self.clock = clock
        # phase 10 steps only the indicators that need it: one computed before the run
        # follows its clock feed through a shared cursor
        self._tw_syncs = [ind for strat in strategies for ind in strat._tw_indicators
                          if not ind._tw_follow_clock()]
        for strat in strategies:
            strat._tw_start_clock(clock.times)

        for strat in strategies:
            strat.start()
            for an in strat.analyzers:
                an.start()

        bus = broker.bus
        first = strategies[0] if strategies else None

        def deliver_order(event):
            order = event.order
            target = order.owner if order.owner is not None else first
            if target is not None:
                target._tw_notify(order)

        def deliver_trade(event):
            target = event.owner if event.owner is not None else first
            if target is not None:
                target._tw_notify(event.trade)

        quiet = isinstance(broker, BackBroker) and bus.journal is None
        if quiet:
            # Notifications on demand: an order or trade event exists to reach a
            # ``notify_order`` / ``notify_trade``. When no strategy or analyzer of the run
            # overrides it and nothing else subscribed to the bus, the broker does not
            # build the events (a snapshot of the order each) -- the orders, positions
            # and trades themselves move exactly as before.
            broker._tw_quiet_orders = not bus._handlers.get(OrderEvent) and not _listens(
                strategies, "notify_order")
            broker._tw_quiet_trades = not bus._handlers.get(TradeEvent) and not _listens(
                strategies, "notify_trade")
        bus.subscribe(OrderEvent, deliver_order)
        bus.subscribe(TradeEvent, deliver_trade)
        try:
            for s in clock:
                self._slice(s, broker, market, bus, strategies)
        finally:
            bus.unsubscribe(OrderEvent, deliver_order)
            bus.unsubscribe(TradeEvent, deliver_trade)
            if quiet:
                broker._tw_quiet_orders = broker._tw_quiet_trades = False

        # Observed on the oracle: notifications are delivered at the start of the next
        # bar only, so what an order placed on the last bar produced is never notified,
        # and nothing is delivered after the loop.
        for strat in strategies:
            strat.stop()
            for an in strat.analyzers:
                an.stop()
        recorder.finish()
        return strategies

    def _slice(self, s, broker, market, bus, strategies):
        """One slice, in the sixteen phases of spec/05 § 3.

        The feeds move to the new bar first, then the broker fills the previous bar's
        orders against it, then the strategy is notified and steps. Observed on the
        oracle: inside notify_order the date is the fill bar's, and a commission scheme
        that reads the bar's date (the toolbox's yearly schedule) prices a fill on 1
        January at the new year's rate -- two corpus strategies rebalancing at year-end
        diverged when fills were priced before the move.
        """
        trace = self._trace
        # 0 clock: external events onto the bus, the broker's time
        if trace is not None:
            trace.append(Phase.CLOCK)
        if not bus.inbox.empty():
            bus.pump()
        broker.set_time(s)
        # 1 universe
        if trace is not None:
            trace.append(Phase.UNIVERSE)
        if s.universe is not None:
            self._universe_change(s, broker, bus)
        # 2 market data
        if trace is not None:
            trace.append(Phase.MARKET_DATA)
        market.step(s)
        for strat in strategies:
            strat._tw_tick(s.position)
        # 3 to 8: the broker
        if trace is not None:
            trace.append(Phase.CORPORATE_ACTIONS)
        broker.corporate_actions(s)
        if trace is not None:
            trace.append(Phase.ACCRUALS)
        broker.accruals(s)
        if trace is not None:
            trace.append(Phase.ACCEPTANCE)
        broker.acceptance(s)
        if trace is not None:
            trace.append(Phase.FILLS)
        broker.fills(s)
        if trace is not None:
            trace.append(Phase.TRANSFERS)
        broker.transfers(s)
        if trace is not None:
            trace.append(Phase.MARK)
        broker.mark(s)
        # 9 margin call
        if trace is not None:
            trace.append(Phase.MARGIN_CALL)
        broker.margin_call(s)
        # 10 indicators
        if trace is not None:
            trace.append(Phase.INDICATORS)
        for ind in self._tw_syncs:
            ind._tw_sync()
        # 11 timers (session start)
        if trace is not None:
            trace.append(Phase.TIMERS_START)
        date = None
        exchange = self._exchange_clock
        for strat in strategies:
            if strat._tw_timers:
                if exchange is not None:
                    strat._tw_check_timers_at(exchange, s, False)
                    continue
                if date is None:
                    date = num2date(s.dtnum).date()
                strat._tw_check_timers(date, Timer.SESSION_START)
        # 12 delivery: order events, then trade events, ... (kind, seq)
        if trace is not None:
            trace.append(Phase.DELIVERY)
        if len(bus):
            bus.deliver()
        # 13 strategy
        if trace is not None:
            trace.append(Phase.STRATEGY)
        for strat in strategies:
            strat._tw_step()
        # 14 analyzers, observers
        if trace is not None:
            trace.append(Phase.ANALYZERS)
        for strat in strategies:
            if strat.analyzers:
                ready = strat._tw_ready()
                for an in strat.analyzers:
                    if not ready:
                        an.prenext()
                    else:
                        an.next()
        # 15 timers (session end)
        if trace is not None:
            trace.append(Phase.TIMERS_END)
        for strat in strategies:
            if strat._tw_timers:
                if exchange is not None:
                    strat._tw_check_timers_at(exchange, s, True)
                    continue
                if date is None:
                    date = num2date(s.dtnum).date()
                strat._tw_check_timers(date, Timer.SESSION_END)

    @staticmethod
    def _universe_change(s, broker, bus):
        change = s.universe
        for sid in change.removed:
            broker.securities[sid]._delisted = True
        for sid in change.added:
            broker.securities[sid]._delisted = False
        bus.publish(UniverseEvent(s.time, 0, change))

    def _deliver(self, strat):
        """Compat: hand every queued notification to its owner (or ``strat``) now."""
        for event in self._broker.bus.drain():
            item = event.order if isinstance(event, OrderEvent) else getattr(event, "trade", None)
            if item is None:
                continue
            owner = getattr(item, "owner", None) if isinstance(event, OrderEvent) else event.owner
            (owner if owner is not None else strat)._tw_notify(item)

    def _build_strategy(self, cls, args, skwargs):
        env = self
        datas = list(self.datas)
        broker = self._broker

        original = getattr(cls, "_tw_before_init", None)

        def before(obj):
            obj._tw_bind(env, datas, broker)
            if original is not None:
                original(obj)
            env._building = obj

        # MetaParams calls cls._tw_before_init(obj) before __init__; install the binding
        # on a throwaway subclass so the user's class is untouched.
        bound = type(cls.__name__, (cls,), {"_tw_before_init": before, "__module__": cls.__module__,
                                           "__qualname__": cls.__qualname__})
        # The build stage: an operator on lines inside ``__init__`` builds a line
        # (``lineops``), the same operator in ``next()`` reads current values.
        reset_construction()
        try:
            with building():
                strat = bound(*args, **skwargs)
        finally:
            env._building = None
            reset_construction()
        strat.__class__ = cls
        env._building = None
        for ind in strat._tw_indicators:
            ind._tw_compute()
        for ancls, aargs, akwargs in self.analyzers:
            akwargs = dict(akwargs)
            name = akwargs.pop("_name", None) or ancls.__name__.lower()
            an = self._build_analyzer(ancls, strat, aargs, akwargs)
            an._name = name
            strat.analyzers.append(an)
        return strat

    def _build_analyzer(self, ancls, strat, args, kwargs):
        def before(obj):
            obj._tw_bind(strat)
            ancls._tw_before_init(obj)

        bound = type(ancls.__name__, (ancls,), {"_tw_before_init": before, "__module__": ancls.__module__})
        an = bound(*args, **kwargs)
        an.__class__ = ancls
        return an


def _listens(strategies, hook: str) -> bool:
    """Does a strategy of the run, or one of its analyzers, define ``hook`` (on its class
    or on the instance)?"""
    from .analyzer import Analyzer
    from .strategy import Strategy

    for strat in strategies:
        if getattr(type(strat), hook) is not getattr(Strategy, hook) or hook in vars(strat):
            return True
        for an in strat.analyzers:
            if getattr(type(an), hook) is not getattr(Analyzer, hook) or hook in vars(an):
                return True
    return False


#: backtrader's name for the engine, kept so that ``Cerebro()`` code runs unchanged.
Cerebro = Engine
