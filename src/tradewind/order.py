"""Orders: a plain record of what was asked and what was executed."""

from __future__ import annotations

import itertools
from typing import ClassVar

_refs = itertools.count(1)


class OrderExecutionBit:
    __slots__ = ("closed", "comm", "dt", "opened", "pnl", "price", "size", "value")

    def __init__(self, dt=None, size=0, price=0.0, closed=0, opened=0, value=0.0, comm=0.0, pnl=0.0):
        self.dt, self.size, self.price = dt, size, price
        self.closed, self.opened, self.value, self.comm, self.pnl = closed, opened, value, comm, pnl


class OrderData:
    """``order.created`` / ``order.executed``: size, price, value, commission, datetime."""

    __slots__ = ("comm", "dt", "exbits", "margin", "pnl", "price", "pricelimit", "remsize", "size", "value")

    def __init__(self, dt=None, size=0, price=0.0, pricelimit=0.0, remsize=0):
        self.dt, self.size, self.price, self.pricelimit = dt, size, price, pricelimit
        self.value, self.comm, self.pnl, self.margin = 0.0, 0.0, 0.0, 0.0
        self.remsize = remsize
        self.exbits = []

    def _copy(self):
        """A shallow copy (what ``copy.copy`` gives, without its generic machinery)."""
        new = OrderData.__new__(OrderData)
        new.comm, new.dt, new.exbits, new.margin, new.pnl = self.comm, self.dt, self.exbits, self.margin, self.pnl
        new.price, new.pricelimit, new.remsize, new.size, new.value = (self.price, self.pricelimit, self.remsize,
                                                                      self.size, self.value)
        return new

    def add(self, dt, size, price, closed=0, opened=0, value=0.0, comm=0.0, pnl=0.0):
        self.exbits.append(OrderExecutionBit(dt, size, price, closed, opened, value, comm, pnl))
        oldsize = self.size
        self.size = oldsize + size
        if self.size:
            self.price = (oldsize * self.price + size * price) / self.size
        self.value += value
        self.comm += comm
        self.pnl += pnl
        self.remsize -= size
        self.dt = dt


class Order:
    # execution types
    Market, Close, Limit, Stop, StopLimit, StopTrail, StopTrailLimit, Historical = range(8)
    ExecTypes: ClassVar[list] = ["Market", "Close", "Limit", "Stop", "StopLimit", "StopTrail", "StopTrailLimit", "Historical"]

    # statuses
    Created, Submitted, Accepted, Partial, Completed, Canceled, Expired, Margin, Rejected = range(9)
    Cancelled = Canceled
    Status: ClassVar[list] = ["Created", "Submitted", "Accepted", "Partial", "Completed", "Canceled", "Expired", "Margin", "Rejected"]

    Buy, Sell = 0, 1

    def __init__(self, owner, data, size, price=None, plimit=None, exectype=None, valid=None,
                 ordtype=None, tradeid=0, info=None, **kwargs):
        self.ref = next(_refs)
        self.owner = owner
        self.data = data
        self.size = size  # positive for a buy, negative for a sell
        self.price = price
        self.pricelimit = plimit
        self.plimit = plimit
        self.exectype = Order.Market if exectype is None else exectype
        self.valid = valid
        self.ordtype = ordtype if ordtype is not None else (Order.Buy if size > 0 else Order.Sell)
        self.tradeid = tradeid
        self.status = Order.Created
        self.created = OrderData(size=size, price=price or 0.0, pricelimit=plimit or 0.0, remsize=size)
        self.executed = OrderData(remsize=size)
        self.info = dict(info or {})
        self.info.update(kwargs)
        self.comminfo = None
        self.broker = None
        self.parent = None
        self.transmit = True
        self.triggered = False
        self._check = True
        self._status_log = [Order.Created]

    # -- queries ----------------------------------------------------------------
    def isbuy(self) -> bool:
        return self.ordtype == Order.Buy

    def issell(self) -> bool:
        return self.ordtype == Order.Sell

    def alive(self) -> bool:
        return self.status in (Order.Created, Order.Submitted, Order.Accepted, Order.Partial)

    def getstatusname(self, status=None) -> str:
        return Order.Status[self.status if status is None else status]

    def getordername(self, exectype=None) -> str:
        return Order.ExecTypes[self.exectype if exectype is None else exectype]

    def addinfo(self, **kwargs):
        self.info.update(kwargs)

    def __repr__(self):
        side = "Buy" if self.isbuy() else "Sell"
        name = getattr(self.data, "_name", "?")
        return f"Order(ref={self.ref}, {side}, {name}, size={self.size}, {self.getordername()}, {self.getstatusname()})"

    def _snapshot(self):
        """The order as it is now, for a notification: a shallow copy whose ``executed`` is
        frozen too, so a later fill does not show through (the clone semantics)."""
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone.executed = self.executed._copy()
        return clone

    # -- transitions (broker only) ------------------------------------------------
    def _set(self, status):
        self.status = status
        self._status_log.append(status)

    def submit(self, broker=None):
        self.broker = broker
        self._set(Order.Submitted)

    def accept(self, broker=None):
        self._set(Order.Accepted)

    def cancel(self):
        self._set(Order.Canceled)

    def expire(self):
        self._set(Order.Expired)

    def margin(self):
        self._set(Order.Margin)

    def reject(self, broker=None):
        self._set(Order.Rejected)

    def completed(self):
        self._set(Order.Completed)

    def partial(self):
        self._set(Order.Partial)

    def execute(self, dt, size, price, closed, closedvalue, closedcomm, opened, openedvalue, openedcomm, margin, pnl):
        self.executed.add(dt, size, price, closed, opened, openedvalue + closedvalue, openedcomm + closedcomm, pnl)
        self.executed.margin = margin


class BuyOrder(Order):
    def __init__(self, *args, **kwargs):
        kwargs["ordtype"] = Order.Buy
        super().__init__(*args, **kwargs)


class SellOrder(Order):
    def __init__(self, *args, **kwargs):
        kwargs["ordtype"] = Order.Sell
        super().__init__(*args, **kwargs)
