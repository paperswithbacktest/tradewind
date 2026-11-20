"""A strategy's book at a close, read off a backtest: ``SleeveBook`` and ``BookRecorder``.

A strategy run in **signal mode** is an ordinary backtest up to the last close: at that
close it holds its positions and has just placed the orders that a backtest fills at the next
open. Its *target book* is the two together, read by an analyzer that steps after the
strategy (phase 14 of a slice). Nothing in the strategy changes, and the same class trades
in the backtest whose NAV the live book is compared with.

``record_books`` attaches a ``BookRecorder`` to every run of the engine started inside it, so
code that builds its own engine (a catalogue strategy through pwb-toolbox) is recorded
without being edited; the run that ends last is the one kept.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import math
from dataclasses import dataclass, field

from ...analyzer import Analyzer
from ...order import Order


@dataclass
class SleeveBook:
    """One strategy at one close.

    ``positions``: shares held at the close (after the fills of that bar); ``target``: the
    shares it will hold after the next open, its positions plus the remaining size of its
    open market orders (at-close and conditional orders are listed in ``conditional`` and
    not counted); ``open``/``close``: the bar of every feed the strategy reads -- for a name
    the harness marks at another price than its feed's (``MARK_HOOK``: a name outside a
    point-in-time universe, whose feed is a flat line), that mark for both; ``costs``:
    the commission charged on that bar's executions; ``half_spread``: the cost per unit of
    notional the strategy's commission scheme would charge each symbol (when it says);
    ``stale_bars``: the symbols held or targeted whose last bar is older than the book's date
    (a delisted stock whose data stopped), with that date."""

    date: str
    nav: float
    positions: dict[str, float] = field(default_factory=dict)
    target: dict[str, float] = field(default_factory=dict)
    open: dict[str, float] = field(default_factory=dict)
    close: dict[str, float] = field(default_factory=dict)
    costs: float = 0.0
    cash: float = 0.0
    half_spread: dict[str, float] = field(default_factory=dict)
    conditional: list = field(default_factory=list)
    stale_bars: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> SleeveBook:
        return cls(date=str(d["date"]), nav=float(d["nav"]),
                   positions={k: float(v) for k, v in d.get("positions", {}).items()},
                   target={k: float(v) for k, v in d.get("target", {}).items()},
                   open={k: float(v) for k, v in d.get("open", {}).items()},
                   close={k: float(v) for k, v in d.get("close", {}).items()},
                   costs=float(d.get("costs", 0.0)), cash=float(d.get("cash", 0.0)),
                   half_spread={k: float(v) for k, v in d.get("half_spread", {}).items()},
                   conditional=list(d.get("conditional", [])),
                   stale_bars={k: str(v) for k, v in d.get("stale_bars", {}).items()})


# The price a harness marks a holding at when the feed does not carry it. pwb-toolbox's harness
# (``run_strategy`` with a point-in-time universe, ``pit_exit="hold"``, its default) blanks the
# bars of a name outside the universe into a flat line at its last member close, and marks what
# is still held there to the real close through the cash; ``_pwb_mark_price(name, data)`` on the
# strategy returns that real close (the feed's close for any other name).
MARK_HOOK = "_pwb_mark_price"


def _real_close(mark, name, data) -> float | None:
    """The strategy's own mark for ``data`` when it has one (``MARK_HOOK``), else None."""
    try:
        v = _finite(mark(name, data))
    except Exception:  # noqa: BLE001 - a feed before its first bar, a harness that cannot say
        return None
    return v if v is not None and v > 0 else None


def _finite(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class BookRecorder(Analyzer):
    """Records a ``SleeveBook`` at every bar dated on or after ``since`` (an ISO date, or
    None for every bar -- slow on wide universes) and keeps the last ``last_n``."""

    params = (("since", None), ("last_n", 30))

    def start(self):
        self.books: list[SleeveBook] = []
        self._costs = 0.0
        self._since = None if self.p.since is None else str(self.p.since)[:10]

    def notify_order(self, order):
        # ``executed.comm`` is cumulative on the order: a partial then a completion would
        # count twice, so only the completion is added (daily bars fill whole)
        if order.status == Order.Completed:
            self._costs += float(order.executed.comm or 0.0)

    def prenext(self):
        self.next()

    def nextstart(self):
        self.next()

    def _today(self) -> str:
        strat = self.strategy
        try:
            day = strat.datetime.date(0)
        except Exception:  # noqa: BLE001 - before the first bar of some feeds
            day = strat.datas[0].datetime.date(0)
        return day.isoformat() if isinstance(day, _dt.date) else str(day)[:10]

    def next(self):
        costs, self._costs = self._costs, 0.0
        today = self._today()
        if self._since is not None and today < self._since:
            return
        strat = self.strategy
        broker = strat.broker
        mark = getattr(strat, MARK_HOOK, None)
        positions: dict[str, float] = {}
        opens: dict[str, float] = {}
        closes: dict[str, float] = {}
        spreads: dict[str, float] = {}
        bar_dates: dict[str, str] = {}
        for d in strat.datas:
            name = getattr(d, "_name", None) or str(d)
            size = float(broker.getposition(d).size)
            if size:
                positions[name] = size
            with contextlib.suppress(Exception):    # a feed before its first bar
                bar_day = d.datetime.date(0)
                if isinstance(bar_day, _dt.date) and bar_day.isoformat() < today:
                    bar_dates[name] = bar_day.isoformat()
            o, c = _finite(d.open[0]), _finite(d.close[0])
            real = _real_close(mark, name, d) if mark is not None else None
            if real is not None and real != c:
                o = c = real                        # a flat bar outside the universe: its open is stale too
            if c is not None and c > 0:
                closes[name] = c
                opens[name] = o if o is not None and o > 0 else c
            rates = getattr(broker.getcommissioninfo(d), "_rates_now", None)
            if rates is not None:
                try:
                    hs = _finite(rates()[0])
                except Exception:  # noqa: BLE001 - a scheme that cannot say outside a fill
                    hs = None
                if hs is not None:
                    spreads[name] = hs
        target = dict(positions)
        conditional = []
        # submitted (accepted as a batch at the next slice) and accepted, not yet executed
        open_orders = broker.open_orders() if hasattr(broker, "open_orders") else broker.get_orders_open()
        for order in open_orders:
            if not order.alive():
                continue
            name = getattr(order.data, "_name", None) or str(order.data)
            remaining = float(order.size) - float(order.executed.size)
            if order.exectype == Order.Market:
                target[name] = target.get(name, 0.0) + remaining
            else:
                conditional.append({"symbol": name, "size": remaining, "type": order.getordername()})
        target = {k: v for k, v in target.items() if v}
        stale = {k: v for k, v in bar_dates.items() if k in target or k in positions}
        book = SleeveBook(date=today, nav=float(broker.getvalue()), positions=positions, target=target,
                          open=opens, close=closes, costs=costs, cash=float(broker.getcash()),
                          half_spread=spreads, conditional=conditional, stale_bars=stale)
        if self.books and self.books[-1].date == today:
            self.books[-1] = book
        else:
            self.books.append(book)
        if len(self.books) > self.p.last_n:
            del self.books[0]

    def get_analysis(self):
        return {b.date: b for b in self.books}


class _Collector:
    def __init__(self):
        self.runs: list[list[SleeveBook]] = []

    @property
    def books(self) -> dict[str, SleeveBook]:
        """The books of the last run that recorded any (``{date: SleeveBook}``, in order)."""
        for run in reversed(self.runs):
            if run:
                return {b.date: b for b in run}
        return {}


@contextlib.contextmanager
def record_books(since: str | None = None, last_n: int = 30):
    """Record the books of every engine run started inside the block::

        with record_books(since="2026-10-01") as rec:
            run_something_that_backtests()
        rec.books        # {date: SleeveBook} of the last run
    """
    from ...engine import Engine

    original = Engine.run
    collector = _Collector()

    def run(self, *args, **kwargs):
        if not getattr(self, "_tw_book_recorder", False):
            self.addanalyzer(BookRecorder, since=since, last_n=last_n)
            self._tw_book_recorder = True
        result = original(self, *args, **kwargs)
        for strat in result or ():
            for an in getattr(strat, "analyzers", ()):
                if isinstance(an, BookRecorder):
                    collector.runs.append(list(an.books))
        return result

    Engine.run = run  # type: ignore[method-assign]
    try:
        yield collector
    finally:
        Engine.run = original  # type: ignore[method-assign]


__all__ = ["BookRecorder", "SleeveBook", "record_books"]
