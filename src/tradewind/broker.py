"""The simulated broker: cash, positions, order execution, commissions, notifications.

Compat fill model (``spec/02-semantics.md``): an order accepted during bar *t* executes
against bar *t+1* of its feed; a market order at that bar's open, ``Order.Close`` at its
close, limit and stop orders against its range with the open as the cap when the price
gapped through. With ``set_coc(True)`` a market order fills at the close of the bar it was
placed on. An order whose cost the cash cannot cover is ``Margin`` and never fills.
"""

from __future__ import annotations

import collections
import copy

from .comminfo import CommInfoBase, CommissionInfo
from .order import BuyOrder, Order, SellOrder
from .params import ParamsBase
from .position import Position
from .trade import Trade


class BrokerBase(ParamsBase):
    params = (("commission", None),)

    def __init__(self):
        self.comminfo = {None: self.p.commission or CommInfoBase()}

    def getcommissioninfo(self, data):
        name = getattr(data, "_name", None)
        if name in self.comminfo:
            return self.comminfo[name]
        return self.comminfo[None]

    def setcommission(self, commission=0.0, margin=None, mult=1.0, commtype=None, percabs=True,
                      stocklike=False, interest=0.0, interest_long=False, leverage=1.0,
                      automargin=False, name=None):
        comm = CommissionInfo(commission=commission, margin=margin, mult=mult, commtype=commtype,
                              stocklike=stocklike, percabs=percabs, interest=interest,
                              interest_long=interest_long, leverage=leverage, automargin=automargin)
        self.comminfo[name] = comm

    def addcommissioninfo(self, comminfo, name=None):
        self.comminfo[name] = comminfo


class BackBroker(BrokerBase):
    params = (
        ("cash", 10000.0),
        ("checksubmit", True),
        ("coc", False),
        ("coo", False),
        ("slip_perc", 0.0),
        ("slip_fixed", 0.0),
        ("slip_open", False),
        ("slip_match", True),
        ("slip_limit", True),
        ("slip_out", False),
        ("shortcash", True),
        ("fundstartval", 100.0),
        ("fundmode", False),
    )

    def __init__(self):
        super().__init__()
        self.cash = self.startingcash = float(self.p.cash)
        self.positions: dict = collections.defaultdict(Position)
        self.orders: list[Order] = []          # every order ever submitted
        self.submitted: collections.deque = collections.deque()  # submitted, accepted on the next bar
        self.pending: collections.deque = collections.deque()  # accepted, not yet executed
        self.notifs: collections.deque = collections.deque()
        self.trades: dict = {}
        self._coc = self.p.coc
        self._coo = self.p.coo
        self._slip_perc = self.p.slip_perc
        self._slip_fixed = self.p.slip_fixed
        self._env = None
        self._value = self.cash

    # -- configuration -----------------------------------------------------------------
    def set_cash(self, cash):
        self.cash = self.startingcash = float(cash)

    setcash = set_cash

    def get_cash(self):
        return self.cash

    getcash = get_cash

    def add_cash(self, cash):
        self.cash += float(cash)

    def set_coc(self, coc):
        self._coc = bool(coc)

    def set_coo(self, coo):
        self._coo = bool(coo)

    def set_slippage_perc(self, perc, slip_open=True, slip_limit=True, slip_match=True, slip_out=False):
        self._slip_perc, self._slip_fixed = float(perc), 0.0

    def set_slippage_fixed(self, fixed, slip_open=True, slip_limit=True, slip_match=True, slip_out=False):
        self._slip_fixed, self._slip_perc = float(fixed), 0.0

    def set_checksubmit(self, checksubmit):
        self.p.checksubmit = checksubmit

    def set_shortcash(self, shortcash):
        self.p.shortcash = shortcash

    def set_fundmode(self, fundmode, fundstartval=None):
        self.p.fundmode = fundmode

    def get_fundmode(self):
        return self.p.fundmode

    def set_filler(self, filler):
        self._filler = filler

    # -- state ---------------------------------------------------------------------------
    def getposition(self, data):
        return self.positions[data]

    def get_value(self, datas=None, mkt=False, lever=False):
        value = self.cash
        for data, pos in self.positions.items():
            if datas is not None and data not in datas:
                continue
            if not pos.size:
                continue
            if len(data) == 0:
                continue
            comminfo = self.getcommissioninfo(data)
            value += comminfo.getvalue(pos, data.close[0])
        return value

    getvalue = get_value

    def get_orders_open(self, safe=False):
        return [o for o in self.pending]

    def get_notification(self):
        try:
            return self.notifs.popleft()
        except IndexError:
            return None

    def notify(self, order):
        # A notification carries the status at the moment it was raised: the strategy
        # sees Submitted then Accepted, not Accepted twice, even though the live order
        # object has already moved on.
        self.notifs.append(copy.copy(order))

    # -- order entry -------------------------------------------------------------------------
    def buy(self, owner, data, size, price=None, plimit=None, exectype=None, valid=None,
            tradeid=0, oco=None, trailamount=None, trailpercent=None, parent=None, transmit=True,
            histnotify=False, _checksubmit=True, **kwargs):
        order = BuyOrder(owner=owner, data=data, size=abs(size), price=price, plimit=plimit,
                         exectype=exectype, valid=valid, tradeid=tradeid, **kwargs)
        return self.submit(order, check=_checksubmit)

    def sell(self, owner, data, size, price=None, plimit=None, exectype=None, valid=None,
             tradeid=0, oco=None, trailamount=None, trailpercent=None, parent=None, transmit=True,
             histnotify=False, _checksubmit=True, **kwargs):
        order = SellOrder(owner=owner, data=data, size=-abs(size), price=price, plimit=plimit,
                          exectype=exectype, valid=valid, tradeid=tradeid, **kwargs)
        return self.submit(order, check=_checksubmit)

    def submit(self, order, check=True):
        """Submitted now; accepted, as a batch, when the next bar arrives.

        Observed on the oracle: the notifications of a bar arrive as all the Submitted,
        then all the Accepted, then the fills -- acceptance is a broker step at the next
        bar, not part of submission. With ``checksubmit`` the order is pre-checked
        against the cash at the current close and goes straight to ``Margin`` when it
        cannot be afforded, without ever being Accepted.
        """
        order.comminfo = self.getcommissioninfo(order.data)
        order.submit(self)
        self.orders.append(order)
        self.notify(order)
        data = order.data
        if check and self.p.checksubmit and len(data) and not self._affordable(order, data.close[0]):
            order.margin()
            self.notify(order)
            return order
        if self._coc and order.exectype == Order.Market and len(data):
            # cheat-on-close: the market order fills at the close of the bar it was placed on
            order.accept(self)
            self.notify(order)
            self._try_exec(order, data.close[0])
            return order
        self.submitted.append(order)
        return order

    def _affordable(self, order, price):
        """Can the cash cover the order at ``price``, commission included? Closing or
        reducing a position never fails; stock-like sells never fail."""
        comminfo = order.comminfo
        size = order.size
        pos = self.positions[order.data]
        opening = (pos.size == 0) or ((pos.size > 0) == (size > 0))
        if not opening:
            return True
        if comminfo.stocklike and not order.isbuy():
            return True
        comm = comminfo.getcommission(size, price)
        cost = comminfo.getoperationcost(size, price)
        return self.cash - cost - comm >= 0

    def cancel(self, order, bracket=False):
        if order in self.submitted:
            self.submitted.remove(order)
            order.cancel()
            self.notify(order)
            return True
        if order in self.pending:
            self.pending.remove(order)
            order.cancel()
            self.notify(order)
            return True
        return False

    # -- execution ---------------------------------------------------------------------------
    def next(self, advanced):
        """Execute the pending orders against the bars that just arrived.

        ``advanced`` is the set of feeds that have a new bar at this clock tick; an order
        on a feed with no bar today waits.
        """
        while self.submitted:
            order = self.submitted.popleft()
            order.accept(self)
            self.notify(order)
            self.pending.append(order)
        remaining = collections.deque()
        while self.pending:
            order = self.pending.popleft()
            data = order.data
            if data not in advanced:
                remaining.append(order)
                continue
            if order.valid is not None and data.datetime[0] > order.valid:
                order.expire()
                self.notify(order)
                continue
            price = self._fill_price(order, data)
            if price is None:
                remaining.append(order)
                continue
            self._try_exec(order, price)
        self.pending = remaining

    def _slip(self, price, order, is_open):
        if self._slip_perc:
            adj = price * self._slip_perc
        elif self._slip_fixed:
            adj = self._slip_fixed
        else:
            return price
        if is_open and not self.p.slip_open:
            return price
        return price + adj if order.isbuy() else price - adj

    def _fill_price(self, order, data):
        o, h, lo, c = data.open[0], data.high[0], data.low[0], data.close[0]
        et = order.exectype
        if et == Order.Market:
            return self._slip(o, order, True)
        if et == Order.Close:
            return c
        if et == Order.Limit:
            lim = order.price
            if order.isbuy():
                if o <= lim:
                    return o
                return lim if lo <= lim else None
            if o >= lim:
                return o
            return lim if h >= lim else None
        if et == Order.Stop:
            stp = order.price
            if order.isbuy():
                if o >= stp:
                    return self._slip(o, order, True)
                return self._slip(stp, order, False) if h >= stp else None
            if o <= stp:
                return self._slip(o, order, True)
            return self._slip(stp, order, False) if lo <= stp else None
        if et == Order.StopLimit:
            stp, lim = order.price, order.pricelimit
            if not order.triggered:
                hit = (h >= stp) if order.isbuy() else (lo <= stp)
                if not hit:
                    return None
                order.triggered = True
            if order.isbuy():
                return lim if lo <= lim else None
            return lim if h >= lim else None
        raise NotImplementedError(f"execution type {order.getordername()} is not supported")

    def _try_exec(self, order, price):
        data = order.data
        comminfo = order.comminfo
        size = order.size
        pos = self.positions[data]
        comm = comminfo.getcommission(size, price)
        if not self._affordable(order, price):
            order.margin()
            self.notify(order)
            return
        oldprice = pos.price
        newsize, _newprice, opened, closed = pos.update(size, price)
        pnl = comminfo.profitandloss(-closed, oldprice, price) if closed else 0.0
        # cash flow: stock-like pays/receives the traded value; futures-like only P&L
        if comminfo.stocklike:
            self.cash -= size * price
        else:
            self.cash += pnl
        self.cash -= comm
        comm_final = comminfo.confirmexec(size, price)
        dt = data.datetime[0]
        order.execute(dt, size, price, closed, closed * price, comm_final if closed else 0.0,
                      opened, opened * price, comm_final if not closed else 0.0, 0.0, pnl)
        order.completed()
        self.notify(order)
        self._update_trade(order, data, size, price, opened, closed, pnl, comm_final, dt, newsize)

    def _update_trade(self, order, data, size, price, opened, closed, pnl, comm, dt, newsize):
        trade = self.trades.get(data)
        if trade is None or trade.isclosed:
            trade = Trade(data=data, tradeid=order.tradeid)
            self.trades[data] = trade
        trade.update(order, size, price, opened, closed, pnl, comm, dt, newsize)
        self.notifs.append(trade)
