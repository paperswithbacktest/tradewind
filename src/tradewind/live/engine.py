"""``LiveEngine``: the engine's loop driven by a live clock (spec/05 § 8, spec/07 § 4-7).

A live session has three stages, all through the sixteen phases of ``Engine._slice``:

1. **warm-up**: the history of every ``LiveFeed`` is stepped as in a backtest, so indicators
   and strategy state are ready; orders placed here are refused silently (nothing is sent);
2. **replay** (on resume): the slices of the journal are stepped again, the venue's messages
   applied where they were applied, and every command the strategy asks for is checked
   against the journalled one instead of being sent (``LiveDivergence`` if they differ);
3. **live**: the adapter connects, a full account snapshot reconciles the book, then each
   settled bar time of the ``LiveClock`` is one slice: the venue's queued messages are
   journalled and applied (phase 5), the bars written into the feeds (phase 2), the
   strategy steps (phase 13) and its orders go out at once.

The journal is the session: replaying it reproduces the run, and resuming from it after a
crash or a stop rebuilds the strategy's state exactly before trading again.
"""

from __future__ import annotations

import uuid

import numpy as np

from ..clock import TimeSlice
from ..engine import Engine
from .adapter import BarRecord
from .broker import LiveBroker, LiveDivergence
from .clock import LiveClock
from .feed import LiveFeed, history_frame, ns2num
from .guards import KillSwitch, RiskLimits
from .journal import Journal
from .messages import decode, encode


class LiveEngine(Engine):
    """Run strategies against ``adapter`` (a ``BrokerAdapter``), with bars from ``bars`` (a
    ``BarSource``; the adapter's own ``bar_source()`` by default).

    ``journal``: a path (or a ``Journal``); an existing journal is resumed. ``max_slices``
    of ``run`` stops after that many live slices (tests, a one-shot daily run).
    """

    def __init__(self, adapter, bars=None, *, journal=None, limits: RiskLimits | None = None,
                 kill_switch: KillSwitch | None = None, read_only: bool = False, reconcile: str = "adopt",
                 capital: float | None = None, market_tif: str = "OPG", poll_interval: float = 60.0,
                 assemble_timeout: float = 600.0, reconcile_every: int = 1, fsync: bool = True):
        super().__init__()
        # live indicators step bar by bar (spec/05 § 10, Q4); vectorised ones are recomputed
        # on the grown buffer after each bar (``_refresh_indicators``)
        self.p.runonce = False
        self.adapter = adapter
        self.bar_source = bars if bars is not None else adapter.bar_source()
        self.kill_switch = kill_switch or KillSwitch()
        self.journal = journal if isinstance(journal, Journal) else Journal(journal, fsync=fsync)
        self.poll_interval = poll_interval
        self.assemble_timeout = assemble_timeout
        self.reconcile_every = int(reconcile_every)
        self.broker = LiveBroker(adapter, limits=limits, kill_switch=self.kill_switch, read_only=read_only,
                                 reconcile=reconcile, capital=capital, market_tif=market_tif)
        self.session = ""
        self.live_slices = 0
        self.replayed_slices = 0
        self.stopped_by = ""
        self.clock_late: list = []

    # -- run --------------------------------------------------------------------------------------
    def run(self, max_slices: int | None = None, connect: bool = True, **kwargs):
        for k, v in kwargs.items():
            if k in self.p:
                setattr(self.p, k, v)
        if not self.datas:
            return []
        for data in self.datas:
            if not isinstance(data, LiveFeed):
                raise TypeError(f"{data!r}: a live run needs LiveFeed feeds")
        journal = self.journal
        records = journal.existing()
        header = records[0] if records else None
        if header is not None and header.get("k") != "session":
            raise ValueError("the journal does not start with a session header")
        if header is not None:
            self.session = header["session"]
            for data in self.datas:
                columns = header["history"].get(data.symbol)
                if columns is None:
                    raise ValueError(f"the journal has no history for {data.symbol}")
                data.p.dataname = history_frame(columns)
        else:
            self.session = uuid.uuid4().hex[:8]
        broker: LiveBroker = self._broker
        broker.session = f"tw-{self.session}"

        feeds_holder = []

        def warmup_clock(feeds, dtnums):
            if header is not None:
                for feed in feeds:
                    if header["digests"].get(feed.symbol) != feed.history_digest():
                        raise ValueError(f"{feed.symbol}: the history rebuilt from the journal does not match it")
            feeds_holder.extend(feeds)
            return _WarmupClock(feeds)

        # the engine's own preparation (feeds, market state, strategies, indicators' phase-10
        # list, bus); notifications are always built live: the session's operator may listen
        ctx = self._tw_open_run({}, make_clock=warmup_clock, quiet_allowed=False)
        clock0, market, strategies = ctx
        feeds = feeds_holder
        bus = broker.bus
        self._ctx = (broker, market, bus, strategies, feeds)
        journal.open()
        if header is None:
            journal.write("session", session=self.session, symbols=[f.symbol for f in feeds],
                          digests={f.symbol: f.history_digest() for f in feeds},
                          history={f.symbol: f.history_columns() for f in feeds})
        broker.journal = journal
        connected_here = False
        position = 0
        try:
            # 1 warm-up
            broker.suppress = "warm-up"
            for s in clock0:
                self._slice(s, broker, market, bus, strategies)
                position += 1
            broker.suppress = ""
            # 2 replay
            carry = []
            if header is not None:
                position, carry = self._replay(records[1:], position)
            # 3 live
            if connect and not self.adapter.is_connected():
                self.adapter.connect()
                connected_here = True
            journalled = len(carry)
            carry = carry + [self.adapter.snapshot(full_sync=True)]
            clock = LiveClock([f.symbol for f in feeds], self.bar_source, [f.last_time_ns() for f in feeds],
                              now=self.adapter.now, idle=self.adapter.idle, poll_interval=self.poll_interval,
                              assemble_timeout=self.assemble_timeout, stop=self._should_stop,
                              finished=getattr(self.bar_source, "finished", None))
            for tick in clock:
                messages = carry + self.adapter.drain()
                if self.reconcile_every and self.live_slices and self.live_slices % self.reconcile_every == 0:
                    messages.append(self.adapter.snapshot())
                self._live_slice(tick.time, tick.bars, tick.stale, messages, position, record=True,
                                 skip=journalled)
                carry, journalled = [], 0
                position += 1
                self.live_slices += 1
                if max_slices is not None and self.live_slices >= max_slices:
                    self.stopped_by = "max_slices"
                    break
            self.clock_late = clock.late
            if not self.stopped_by:
                self.stopped_by = (f"kill switch: {self.kill_switch.reason or broker.killed}"
                                   if self._should_stop() else "bar source finished")
        except KeyboardInterrupt:
            self.stopped_by = "interrupted"
        finally:
            if self.kill_switch.tripped and not broker.replaying:
                sent = broker.emergency_cancel()
                journal.write("kill", reason=self.kill_switch.reason, cancelled=sent)
            self._tw_stop_run(strategies)
            journal.write("end", stopped_by=self.stopped_by or "error", live_slices=self.live_slices)
            journal.close()
            self._tw_release_bus()
            if connected_here:
                self.adapter.disconnect()
        return strategies

    def _should_stop(self) -> bool:
        return self.kill_switch.tripped or bool(self._broker.killed)

    # -- one live (or replayed) slice ----------------------------------------------------------------
    def _live_slice(self, time_ns: int, bars: dict, stale: bool, messages: list, position: int,
                    record: bool, skip: int = 0) -> None:
        """One slice from settled bars; ``messages[:skip]`` are already in the journal."""
        broker, market, bus, strategies, feeds = self._ctx
        journal = self.journal
        if record:
            for message in messages[skip:]:
                journal.write("evt", event=encode(message))
            journal.write("slice", t=int(time_ns), pos=int(position), stale=bool(stale),
                          bars={feeds[j].symbol: [b.open, b.high, b.low, b.close, b.volume]
                                for j, b in sorted(bars.items())})
        for j, bar in bars.items():
            feeds[j].append(bar.time, bar.open, bar.high, bar.low, bar.close, bar.volume)
        self._refresh_indicators(strategies)
        has_bar = np.zeros(len(feeds), dtype=bool)
        has_bar[list(bars)] = True
        dtnum = ns2num(time_ns)
        for strat in strategies:
            strat.datetime.array[position] = dtnum
        broker.receive(messages)
        broker.suppress = "catch-up" if stale else ""
        s = TimeSlice(np.int64(time_ns), position, has_bar, (), None, dtnum, bool(has_bar.all()))
        try:
            self._slice(s, broker, market, bus, strategies)
        finally:
            broker.suppress = ""

    def _replay(self, records: list, position: int):
        broker, _market, _bus, _strategies, feeds = self._ctx
        index = {f.symbol: j for j, f in enumerate(feeds)}
        broker.replaying = True
        broker._expected.extend(r for r in records if r.get("k") == "cmd")
        pending = []
        try:
            for r in records:
                kind = r.get("k")
                if kind == "evt":
                    pending.append(decode(r["event"]))
                elif kind == "slice":
                    if r["pos"] != position:
                        raise LiveDivergence(f"journal slice {r['pos']} replayed at position {position}")
                    bars = {index[sym]: BarRecord(r["t"], *values) for sym, values in r["bars"].items()}
                    self._live_slice(r["t"], bars, r["stale"], pending, position, record=False)
                    pending = []
                    position += 1
                    self.replayed_slices += 1
            if broker._expected:
                raise LiveDivergence(f"the journal holds {len(broker._expected)} command(s) the replay "
                                     f"did not ask for, first {broker._expected[0]}")
        finally:
            broker.replaying = False
            broker._expected.clear()
        # messages journalled after the last slice (a stop between the two writes) are
        # applied at the first live slice, and not journalled twice
        return position, pending

    @staticmethod
    def _refresh_indicators(strategies) -> None:
        """Recompute the vectorised indicators over the grown buffers: ``once`` is a pure,
        causal function of its inputs, so the rows up to the new bar get the values a
        backtest over the same bars gives (spec/05 § 10, Q4); next-only ones step in phase 10."""
        seen = set()
        stack = [ind for strat in strategies for ind in strat._tw_indicators]
        order = []
        while stack:
            ind = stack.pop()
            if id(ind) in seen:
                continue
            seen.add(id(ind))
            order.append(ind)
            stack.extend(ind._tw_children)
        for ind in order:
            if ind._tw_vectorised or ind._tw_bindings:
                ind._tw_computed = False
        for ind in order:
            ind._tw_compute()


class _WarmupClock:
    """The warm-up slices: the union of the feeds' histories, as the backtest's clock gives
    them; ``times`` reserves the live slices' rows after them (the strategies' clock line)."""

    def __init__(self, feeds):
        hist = [f.lines.datetime.array[:f.history_rows] for f in feeds]
        times = np.unique(np.concatenate(hist)) if hist else np.empty(0)
        self.has_bar = np.zeros((len(times), len(feeds)), dtype=bool)
        for j, dt in enumerate(hist):
            self.has_bar[np.searchsorted(times, dt), j] = True
        capacity = max(f.buflen() - f.history_rows for f in feeds)
        self.times = np.full(len(times) + capacity, np.nan)
        self.times[:len(times)] = times
        self._hist = times
        self._ns = np.round((times - 719163.0) * 86_400e9).astype(np.int64)

    def __len__(self):
        return len(self._hist)

    def __iter__(self):
        for t in range(len(self._hist)):
            row = self.has_bar[t]
            yield TimeSlice(self._ns[t], t, row, (), None, float(self._hist[t]), bool(row.all()))


__all__ = ["LiveEngine"]
