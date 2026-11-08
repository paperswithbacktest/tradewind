"""A trade: the life of a position from open to flat, delivered to ``notify_trade``."""

from __future__ import annotations

from .dateutils import num2date


class Trade:
    Created, Open, Closed = range(3)

    def __init__(self, data=None, tradeid=0):
        self.ref = id(self)
        self.data = data
        self.tradeid = tradeid
        self.size = 0  # an int, as the oracle's, until a fractional fill
        self.price = 0.0
        self.value = 0.0
        self.commission = 0.0
        self.pnl = 0.0
        self.pnlcomm = 0.0
        self.justopened = False
        self.long = None  # the side the trade opened on
        self.isopen = False
        self.isclosed = False
        self.baropen = 0
        self.dtopen = 0.0
        self.barclose = 0
        self.dtclose = 0.0
        self.barlen = 0
        self.status = Trade.Created
        self.history = []

    def __len__(self):
        return self.barlen

    def __bool__(self):
        return self.status != Trade.Created

    def open_datetime(self):
        return num2date(self.dtopen)

    def close_datetime(self):
        return num2date(self.dtclose)

    def update(self, order, size, price, opened, closed, pnl, comm, dt, newsize):
        was_open = self.isopen
        self.justopened = not was_open and self.status != Trade.Open and newsize != 0
        if self.justopened:
            self.dtopen = dt
            self.baropen = len(self.data)
            self.status = Trade.Open
            self.isopen = True
            self.long = size > 0
        if opened:
            total = self.size + opened
            self.price = (self.size * self.price + opened * price) / total if total else price
            self.size = total
        if closed:
            self.size += closed
            self.pnl += pnl
        self.commission += comm
        self.pnlcomm = self.pnl - self.commission
        self.value = self.size * self.price
        self.barlen = len(self.data) - self.baropen
        if newsize == 0 and self.isopen:
            self.isclosed = True
            self.isopen = False
            self.status = Trade.Closed
            self.dtclose = dt
            self.barclose = len(self.data)
