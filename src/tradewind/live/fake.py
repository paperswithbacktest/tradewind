"""A simulated venue for tests and dry runs: ``FakeMarket``, ``FakeIB``, ``FakeBars``.

No account, no network. ``FakeMarket`` holds daily bars and a simulated wall clock: a bar
stamped on day D opens at D + ``open_after``, closes at D + ``close_after`` and is published
(complete, pollable) at D + ``publish_after``. ``FakeIB`` is a ``BrokerAdapter`` on that
clock with the behaviours a real venue has and a test needs to provoke:

* acknowledgements (``Ack``) on placement, executions at the next bar's open (limit, stop
  and market-on-close priced against the bar), a commission per share with a minimum;
* partial fills (``fill_fraction`` of the order per bar), expiry of DAY / OPG remainders;
* rejections (``reject_next``), cancellations, a dropped and restored connection (the
  executions that happen while it is down arrive with the resync snapshot);
* an account (cash, positions) the strategy's book can be reconciled against, and manual
  trades behind the strategy's back (``external_trade``).

Time only moves in ``idle``: a whole session runs in milliseconds and the same way every time.
"""

from __future__ import annotations

import bisect
import collections
import math

import numpy as np

from .adapter import BarRecord
from .messages import (
    AccountSnapshot,
    Ack,
    BrokerOrder,
    Canceled,
    Connection,
    Execution,
    OrderRequest,
    Rejected,
)

_HOUR = 3_600_000_000_000
_DAY = 24 * _HOUR


def _ns(stamp) -> int:
    import pandas as pd

    ts = pd.Timestamp(stamp)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return int(ts.as_unit("ns").value)


class FakeMarket:
    """Daily bars of several symbols, released on a simulated wall clock."""

    def __init__(self, frames: dict, start, *, open_after: float = 13.5, close_after: float = 20.0,
                 publish_after: float = 20.25):
        self.symbols = list(frames)
        self.bars: dict[str, list[BarRecord]] = {}
        self.stamps: dict[str, list[int]] = {}
        for symbol, frame in frames.items():
            index = frame.index
            stamps = [_ns(x) for x in index]
            cols = [np.asarray(frame[c], dtype=np.float64) for c in ("open", "high", "low", "close", "volume")]
            self.bars[symbol] = [BarRecord(t, *(float(c[i]) for c in cols)) for i, t in enumerate(stamps)]
            self.stamps[symbol] = stamps
        self.open_after = int(open_after * _HOUR)
        self.close_after = int(close_after * _HOUR)
        self.publish_after = int(publish_after * _HOUR)
        self.time = _ns(start)

    def history(self, symbol: str, now: int | None = None):
        """The bars published at ``now`` (the start by default), as a feed's DataFrame."""
        import pandas as pd

        now = self.time if now is None else now
        bars = [b for b in self.bars[symbol] if b.time + self.publish_after <= now]
        index = pd.to_datetime([b.time for b in bars], unit="ns")
        return pd.DataFrame({"open": [b.open for b in bars], "high": [b.high for b in bars],
                             "low": [b.low for b in bars], "close": [b.close for b in bars],
                             "volume": [b.volume for b in bars]}, index=index)

    def published(self, symbol: str, after: int, now: int) -> list[BarRecord]:
        stamps = self.stamps[symbol]
        i = bisect.bisect_right(stamps, after)
        j = bisect.bisect_right(stamps, now - self.publish_after)
        return self.bars[symbol][i:j] if j > i else []

    def last_close(self, symbol: str, now: int | None = None) -> float | None:
        now = self.time if now is None else now
        stamps = self.stamps[symbol]
        j = bisect.bisect_right(stamps, now - self.close_after)
        return self.bars[symbol][j - 1].close if j else None

    def finished(self, now: int) -> bool:
        return all(not s or s[-1] + self.publish_after <= now for s in self.stamps.values())


class FakeBars:
    """The ``BarSource`` of a ``FakeMarket``."""

    def __init__(self, market: FakeMarket):
        self.market = market
        self.polls = 0

    def poll(self, symbol: str, after: int, now: int) -> list[BarRecord]:
        self.polls += 1
        return self.market.published(symbol, after, now)

    def finished(self, now: int) -> bool:
        return self.market.finished(now)


class FakeIB:
    """A ``BrokerAdapter`` that behaves like a broker on a ``FakeMarket``."""

    def __init__(self, market: FakeMarket, cash: float = 100_000.0, *, commission: float = 0.0,
                 min_commission: float = 0.0, fill_fraction: float = 1.0, account: str = "DU0000000",
                 positions: dict | None = None):
        self.market = market
        self.account = account
        self.cash = float(cash)
        self.positions: dict[str, list[float]] = {s: [float(q), float(p)] for s, (q, p) in (positions or {}).items()}
        self.commission = float(commission)
        self.min_commission = float(min_commission)
        self.fill_fraction = float(fill_fraction)
        self.connected = False
        self.commands: list[tuple] = []
        self.executions: list[Execution] = []
        self._queue: collections.deque = collections.deque()
        self._orders: dict[str, dict] = {}
        self._rejects: list[tuple[str, int]] = []
        self._n_exec = 0
        self._n_orders = 0

    # -- BrokerAdapter ----------------------------------------------------------------------------
    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    def now(self) -> int:
        return self.market.time

    def bar_source(self) -> FakeBars:
        return FakeBars(self.market)

    def drain(self) -> list:
        out = list(self._queue)
        self._queue.clear()
        return out

    def place(self, request: OrderRequest) -> None:
        t = self.now()
        self.commands.append(("place", request))
        if not self.connected:
            self._queue.append(Rejected(request.client_id, "not connected", 504, t))
            return
        if self._rejects:
            reason, code = self._rejects.pop(0)
            self._queue.append(Rejected(request.client_id, reason, code, t))
            return
        self._n_orders += 1
        self._orders[request.client_id] = {"req": request, "placed": t, "filled": 0.0, "working": True}
        self._queue.append(Ack(request.client_id, str(self._n_orders), t))

    def cancel(self, client_id: str) -> None:
        self.commands.append(("cancel", client_id))
        order = self._orders.get(client_id)
        if order is None or not order["working"] or not self.connected:
            return
        order["working"] = False
        self._queue.append(Canceled(client_id, "canceled by the client", False, self.now()))

    def snapshot(self, full_sync: bool = False) -> AccountSnapshot:
        nlv = self.cash
        for symbol, (q, _p) in self.positions.items():
            price = self.market.last_close(symbol) if symbol in self.market.bars else None
            if q and price is not None:
                nlv += q * price
        return AccountSnapshot(
            cash=self.cash, net_liquidation=nlv, currency="USD",
            positions=tuple((s, q, p) for s, (q, p) in sorted(self.positions.items()) if q),
            open_orders=tuple(BrokerOrder(cid, o["req"].symbol, o["req"].quantity, o["filled"])
                              for cid, o in self._orders.items() if o["working"]),
            executions=tuple(self.executions) if full_sync else (),
            full_sync=full_sync, time=self.now())

    def idle(self, seconds: float) -> None:
        end = self.market.time + int(seconds * 1e9)
        market = self.market
        events = []
        for symbol in market.symbols:
            stamps = market.stamps[symbol]
            lo = bisect.bisect_right(stamps, market.time - market.open_after)
            hi = bisect.bisect_right(stamps, end - market.open_after)
            events.extend((stamps[i] + market.open_after, symbol, i) for i in range(lo, hi))
        for opens, symbol, i in sorted(events):
            market.time = opens
            self._trade_bar(symbol, market.bars[symbol][i], opens)
        market.time = end

    # -- simulation controls ------------------------------------------------------------------------
    def reject_next(self, reason: str = "Order rejected", code: int = 201, count: int = 1) -> None:
        self._rejects.extend([(reason, code)] * count)

    def drop_connection(self, code: int = 1100) -> None:
        self.connected = False
        self._queue.append(Connection(False, "Connectivity between IB and TWS has been lost", code, self.now()))

    def restore_connection(self, code: int = 1102) -> None:
        self.connected = True
        self._queue.append(Connection(True, "Connectivity between IB and TWS has been restored", code, self.now()))
        self._queue.append(self.snapshot(full_sync=True))

    def external_trade(self, symbol: str, quantity: float, price: float) -> None:
        """A trade made outside the strategy (by hand in TWS): the account moves, the
        strategy hears nothing until reconciliation."""
        self._book(symbol, float(quantity), float(price), 0.0)

    # -- matching ---------------------------------------------------------------------------------------
    def _trade_bar(self, symbol: str, bar: BarRecord, opens: int) -> None:
        for client_id, order in list(self._orders.items()):
            req = order["req"]
            if not order["working"] or req.symbol != symbol or order["placed"] >= opens:
                continue
            if req.good_till is not None and req.tif == "GTD" and opens > req.good_till:
                order["working"] = False
                self._queue.append(Canceled(client_id, "good-till date reached", True, opens))
                continue
            price = self._price(req, bar)
            if price is not None:
                remaining = abs(req.quantity) - order["filled"]
                q = remaining if self.fill_fraction >= 1 else min(
                    remaining, max(1.0, math.floor(abs(req.quantity) * self.fill_fraction)))
                self._execute(client_id, order, math.copysign(q, req.quantity), price, opens)
            if order["working"] and req.tif in ("DAY", "OPG"):
                order["working"] = False
                self._queue.append(Canceled(client_id, "time in force elapsed", True,
                                            bar.time + self.market.close_after))

    @staticmethod
    def _price(req: OrderRequest, bar: BarRecord) -> float | None:
        buy = req.quantity > 0
        t = req.order_type
        if t == "MKT":
            return bar.open
        if t == "MOC":
            return bar.close
        if t in ("STP", "STP LMT"):
            stop = req.stop_price
            if buy:
                trigger = bar.open if bar.open >= stop else (stop if bar.high >= stop else None)
            else:
                trigger = bar.open if bar.open <= stop else (stop if bar.low <= stop else None)
            if trigger is None:
                return None
            if t == "STP":
                return trigger
            limit = req.limit_price
            return trigger if (trigger <= limit if buy else trigger >= limit) else None
        if t == "LMT":
            limit = req.limit_price
            if buy:
                return bar.open if bar.open <= limit else (limit if bar.low <= limit else None)
            return bar.open if bar.open >= limit else (limit if bar.high >= limit else None)
        return None

    def _execute(self, client_id: str, order: dict, quantity: float, price: float, t: int) -> None:
        req = order["req"]
        comm = max(self.min_commission, self.commission * abs(quantity)) if (self.commission or self.min_commission) else 0.0
        self._n_exec += 1
        ex = Execution(client_id, f"fake.{self._n_exec:06d}", req.symbol, quantity, price, comm, t)
        order["filled"] += abs(quantity)
        if order["filled"] >= abs(req.quantity) - 1e-9:
            order["working"] = False
        self._book(req.symbol, quantity, price, comm)
        self.executions.append(ex)
        if self.connected:
            self._queue.append(ex)

    def _book(self, symbol: str, quantity: float, price: float, comm: float) -> None:
        self.cash -= quantity * price + comm
        q, avg = self.positions.get(symbol, [0.0, 0.0])
        new = q + quantity
        if not new:
            avg = 0.0
        elif q == 0 or (q > 0) != (new > 0):
            avg = price
        elif (q > 0) == (quantity > 0):
            avg = (q * avg + quantity * price) / new
        self.positions[symbol] = [new, avg]


__all__ = ["FakeBars", "FakeIB", "FakeMarket"]
