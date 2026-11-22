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

from . import indicator as _indicator
from .broker import BackBroker
from .clock import Phase, UnionClock
from .dateutils import num2date
from .events import CorporateActionEvent, OrderEvent, TradeEvent, UniverseEvent
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
        self._universe_removal = "keep"
        self._universe_gate = False
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

    def adddata(self, data, name=None, provenance=None, normalization=None):
        """Add a feed (``SecurityId`` = adddata order). ``provenance`` (Tradewind): where
        the data comes from, such as ``{"dataset": ..., "revision": ...}``, recorded in the
        run's fingerprint. ``normalization`` (Tradewind): how its prices treat splits and
        dividends, ``"raw"``, ``"split_adjusted"`` or ``"adjusted"`` (``models.NORMALIZATIONS``;
        undeclared, every corporate action applies)."""
        if name is not None:
            data._name = name
        if provenance is not None:
            data.provenance = dict(provenance)
        if normalization is not None:
            _check_normalization(normalization)
            data.normalization = normalization
        data._tw_env = self
        self.datas.append(data)
        # SecurityId = adddata order (spec/05 § 4.1)
        security = self._broker.register(data)
        if normalization is not None:
            security.normalization = normalization
        return data

    def set_normalization(self, data, normalization) -> None:
        """Declare how ``data``'s prices treat splits and dividends (see ``adddata``)."""
        if normalization is not None:
            _check_normalization(normalization)
        data.normalization = normalization
        self._broker.register(data).normalization = normalization

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
        raise NotImplementedError("backtrader's resampling is not part of Tradewind; "
                                  "Engine.consolidate builds a consolidated feed (spec/06)")

    def consolidate(self, data, period="5min", name=None, tz="UTC", session=None, hours=None,
                    count=None):
        """Add a feed of ``data``'s bars consolidated by ``period`` ("5min", "1h", "D") or by
        ``count`` bars, in the wall-clock time of ``tz`` (spec/06 § 2). ``session`` (a pair
        of ``datetime.time``) or ``hours`` (an ``ExchangeHours``) anchors the bins at the
        open, clips them at the close and drops the bars outside. Returns the new feed."""
        from .consolidators import ConsolidatedData

        feed = ConsolidatedData(source=data, period=period, count=count, tz=tz, session=session,
                                hours=hours)
        label = name or f"{getattr(data, '_name', '') or 'data'}_{count or period}"
        return self.adddata(feed, name=label)

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
    def add_corporate_actions(self, provider) -> None:
        """Add a ``CorporateActionProvider`` next to the ones already set (splits of a minute
        feed, the expiries of option contracts, ...)."""
        from .models import CombinedActions

        current = self._actions_provider
        if current is None:
            self._actions_provider = provider
        elif isinstance(current, CombinedActions):
            current.providers.append(provider)
        else:
            self._actions_provider = CombinedActions(current, provider)

    def set_universe(self, provider, on_removal: str = "keep", gate: bool = False) -> None:
        """A ``UniverseProvider`` whose changes reach phase 1 (none by default), such as
        ``universe.MembershipUniverse(table)``: securities enter and leave the universe during
        the run, and a strategy defining ``notify_universe(change)`` hears of it at phase 12,
        before its ``next``: ``change`` is the ``clock.UniverseChange`` (``time`` in ns,
        ``added`` and ``removed`` as SecurityIds; ``self.broker.securities[i].feed`` is the
        feed, ``tradewind.universe.feeds(self, ids)`` the list).

        ``on_removal``: ``"keep"`` (default) leaves a removed security's position to the
        strategy; ``"liquidate"`` closes it with a market order placed after the strategy's
        ``next`` of the slice that removed it (filled at the next bar's open, like any order
        of that bar), unless the strategy placed an order on it in that slice.
        ``gate``: with False (default) the feeds the provider manages do not hold the
        strategy in ``prenext`` until they have all delivered a bar -- a point-in-time
        universe lists names years apart. ``None`` removes the provider."""
        if on_removal not in ("keep", "liquidate"):
            raise ValueError("on_removal is 'keep' or 'liquidate'")
        self._universe_provider = provider
        self._universe_removal = on_removal
        self._universe_gate = bool(gate)

    @property
    def universe(self):
        """The ``UniverseProvider`` set by ``set_universe`` (None without one)."""
        return self._universe_provider

    def run(self, **kwargs):
        ctx = self._tw_open_run(kwargs)
        if ctx is None:
            return []
        clock, market, strategies = ctx
        broker, bus = self._broker, self._broker.bus
        try:
            for s in clock:
                self._slice(s, broker, market, bus, strategies)
        finally:
            self._tw_release_bus()
        # Observed on the oracle: notifications are delivered at the start of the next
        # bar only, so what an order placed on the last bar produced is never notified,
        # and nothing is delivered after the loop.
        self._tw_stop_run(strategies)
        return strategies

    def _tw_open_run(self, kwargs, make_clock=None, quiet_allowed=True):
        """Everything a run does before its first slice: parameters, feeds, the clock, the
        market state, the strategies (built, started) and the bus subscriptions. Returns
        ``(clock, market, strategies)``, or None without feeds. ``make_clock(feeds, dtnums)``
        replaces the backtest's clock (the live engine's warm-up clock); with
        ``quiet_allowed`` false the broker always publishes its notifications."""
        if "seed" in kwargs:
            self.seed = kwargs.pop("seed")
        for k, v in kwargs.items():
            if k in self.p:
                setattr(self.p, k, v)
        if not self.datas:
            return None
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
        if make_clock is not None:
            clock = make_clock(feeds, dtnums)
            self._exchange_clock = None
        elif self._clock_spec is None:
            clock = UnionClock(dtnums, actions=getattr(self, "_actions_provider", None),
                               universe=getattr(self, "_universe_provider", None))
            self._exchange_clock = None
        else:
            clock = self._clock_spec.bind(dtnums, actions=self._actions_provider,
                                          universe=self._universe_provider)
            self._exchange_clock = clock if getattr(clock, "sessions", None) is not None else None
        market = MarketState(feeds)
        broker.start(feeds, market)
        self._tw_bind_providers(broker, make_clock is None)

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

        quiet = quiet_allowed and isinstance(broker, BackBroker) and bus.journal is None
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

        def deliver_action(event):
            # opt-in (spec/06 § 5.4): a strategy that defines ``notify_corporate_action`` hears
            # of splits, expiries, ... at phase 12; the compatible contract has no such method
            for strat in strategies:
                hook = getattr(strat, "notify_corporate_action", None)
                if hook is not None:
                    hook(event.action)

        def deliver_universe(event):
            # opt-in (spec/05 § 10, Q6): ``notify_universe(change)`` on the strategies that
            # define it; the compatible contract has no such method
            for strat in strategies:
                hook = getattr(strat, "notify_universe", None)
                if hook is not None:
                    hook(event.change)

        bus.subscribe(OrderEvent, deliver_order)
        bus.subscribe(TradeEvent, deliver_trade)
        bus.subscribe(CorporateActionEvent, deliver_action)
        bus.subscribe(UniverseEvent, deliver_universe)
        self._tw_bus_handlers = (deliver_order, deliver_trade, quiet, deliver_action, deliver_universe)
        return clock, market, strategies

    def _tw_bind_providers(self, broker, backtest: bool) -> None:
        """Give the providers that resolve symbols the run's securities (``bind``), and
        ungate the feeds a universe manages (``set_universe(gate=False)``)."""
        self._tw_ungated = []
        self._tw_removed = []
        if not backtest:
            return
        for provider in (self._actions_provider, self._universe_provider):
            bind = getattr(provider, "bind", None)
            if bind is not None:
                bind(broker.securities)
        universe = self._universe_provider
        if universe is None:
            return
        for security in broker.securities:
            security._member = True
        managed = getattr(universe, "managed", None)
        if managed is None:
            return
        for sid in managed:
            # outside the universe until its first change says otherwise
            security = broker.securities[sid]
            security._member = False
            feed = security.feed
            if not self._universe_gate and getattr(feed, "_tw_gate", True):
                feed._tw_gate = False
                self._tw_ungated.append(feed)

    def _tw_release_bus(self):
        deliver_order, deliver_trade, quiet, deliver_action, deliver_universe = self._tw_bus_handlers
        broker = self._broker
        broker.bus.unsubscribe(OrderEvent, deliver_order)
        broker.bus.unsubscribe(TradeEvent, deliver_trade)
        broker.bus.unsubscribe(CorporateActionEvent, deliver_action)
        broker.bus.unsubscribe(UniverseEvent, deliver_universe)
        if quiet:
            broker._tw_quiet_orders = broker._tw_quiet_trades = False
        for feed in getattr(self, "_tw_ungated", ()):
            feed._tw_gate = True
        self._tw_ungated = []

    def _tw_stop_run(self, strategies):
        for strat in strategies:
            strat.stop()
            for an in strat.analyzers:
                an.stop()
        self._fingerprint.finish()

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
        if self._tw_removed:
            self._liquidate_removed(broker, strategies)
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

    def _universe_change(self, s, broker, bus):
        """Phase 1: the securities that enter and leave the universe now. A removed
        security keeps its position (``on_removal="liquidate"`` closes it after this slice's
        ``next``); it is no longer ``in_universe``. It is not delisted: it may come back."""
        change = s.universe
        securities = broker.securities
        for sid in change.removed:
            securities[sid]._member = False
        for sid in change.added:
            securities[sid]._member = True
        if self._universe_removal == "liquidate" and change.removed:
            self._tw_removed = list(change.removed)
        bus.publish(UniverseEvent(s.time, 0, change))

    def _liquidate_removed(self, broker, strategies):
        """``on_removal="liquidate"``: close the position of each security removed at this
        slice and still out of the universe, unless an order on it was placed in this slice
        (the strategy's own exit). Placed after the strategy's ``next``, the orders join its
        batch: accepted and filled at the next bar."""
        removed, self._tw_removed = self._tw_removed, []
        owner = strategies[0] if strategies else None
        placed = {id(o.data) for o in broker.submitted}
        for sid in removed:
            security = broker.securities[sid]
            if security._member or security._delisted:
                continue
            feed = security.feed
            if id(feed) in placed:
                continue
            size = broker.getposition(feed).size
            if size > 0:
                broker.sell(owner, feed, size)
            elif size < 0:
                broker.buy(owner, feed, -size)

    def _deliver(self, strat):
        """Compat: hand every queued notification to its owner (or ``strat``) now."""
        for event in self._broker.bus.drain():
            item = event.order if isinstance(event, OrderEvent) else getattr(event, "trade", None)
            if item is None:
                continue
            owner = getattr(item, "owner", None) if isinstance(event, OrderEvent) else event.owner
            (owner if owner is not None else strat)._tw_notify(item)

    def _build_strategy(self, cls, args, skwargs):
        from .typed import is_typed_strategy

        if is_typed_strategy(cls):
            # a typed strategy runs inside a compatible host strategy: same engine, same path
            cls = cls._tw_host_class()
        env = self
        datas = list(self.datas)
        broker = self._broker

        original = getattr(cls, "_tw_before_init", None)

        def before(obj):
            obj._tw_bind(env, datas, broker)
            if original is not None:
                original(obj)
            env._building = obj
            _indicator._BUILDING.append(obj)

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
            _indicator._BUILDING.clear()
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


def _check_normalization(mode) -> None:
    from .models import NORMALIZATIONS

    if mode not in NORMALIZATIONS:
        raise ValueError(f"normalization is one of {NORMALIZATIONS}, not {mode!r}")


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
