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
        self._cash_addition = 0.0
        self._value = None
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
        # Observed on the oracle (probe ``add_cash_timing``): the addition is not visible to
        # ``getcash``/``getvalue`` in the bar that requests it, the next bar's batch check and
        # fills still run on the old cash, and it lands at the end of that ``next`` -- before
        # the notifications of the bar are read.
        self._cash_addition += float(cash)

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
        # Observed on the oracle: with ``datas`` the result is the value of those
        # positions alone, without the cash -- and asking for it applies any pending
        # ``add_cash`` on the spot (``getcash`` reflects it from then on), which is how a
        # same-bar ``order_target_value`` after ``add_cash`` gets its batch checked against
        # the new cash while a plain ``sell`` does not (probe ``add_cash_timing``).
        if datas is None:
            # Observed on the oracle: the plain portfolio value is the one marked once per
            # bar, after that bar's fills and cash additions -- a same-bar ``add_cash`` or a
            # ``get_value(datas=...)`` that applied one does not move it until the next bar.
            return self.cash if self._value is None else self._value
        if self._cash_addition:
            self.cash += self._cash_addition
            self._cash_addition = 0.0
        value = 0.0
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

    def _mark_value(self):
        value = self.cash
        for data, pos in self.positions.items():
            if not pos.size or len(data) == 0:
                continue
            value += self.getcommissioninfo(data).getvalue(pos, data.close[0])
        return value

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
        # object has already moved on; ``executed`` is frozen with it.
        clone = copy.copy(order)
        clone.executed = copy.copy(order.executed)
        self.notifs.append(clone)

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
        order._check = check
        # Cheat-on-close changes nothing here: observed on the oracle, the batch is still
        # accepted and notified when the next bar arrives; only the fill price of a market
        # order becomes the close of the bar it was placed on (see ``next``).
        self.submitted.append(order)
        return order

    def _cash_delta(self, order, price, pos):
        """Cash flow of filling ``order`` at ``price`` against position ``pos``
        (negative when cash goes out), commission included."""
        comminfo = order.comminfo
        size = order.size
        comm = comminfo.getcommission(size, price)
        if comminfo.stocklike:
            return -size * price - comm
        opening = (pos.size == 0) or ((pos.size > 0) == (size > 0))
        if opening:
            return -comminfo.getoperationcost(size, price) - comm
        return -comm

    def _affordable(self, order, price, cash=None, pos=None):
        """Can ``cash`` cover the order at ``price``, commission included? Closing or
        reducing a position never fails; stock-like sells never fail."""
        cash = self.cash if cash is None else cash
        pos = self.positions[order.data] if pos is None else pos
        comminfo = order.comminfo
        size = order.size
        opening = (pos.size == 0) or ((pos.size > 0) == (size > 0))
        if not opening:
            return True
        if comminfo.stocklike and not order.isbuy():
            return True
        return cash + self._cash_delta(order, price, pos) >= 0

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
    def next(self, upcoming):
        """Accept the submitted orders, then execute against the bars about to arrive.

        ``upcoming`` maps each feed that has a bar at this clock tick to that bar's
        ``(open, high, low, close, dtnum)``; an order on a feed with no bar waits.

        Observed on the oracle: the submitted orders of a bar are checked as a batch, in
        submission order, against a *running* cash -- a sell earlier in the batch funds a
        buy later in it -- and each is Accepted or Margin before any fill happens. The
        running cash takes the cash flow of **every** order, refused ones included: an
        order is Accepted when the running cash is still non-negative after it, Margin
        otherwise. So the deficit of a refused buy weighs on the orders after it, and a
        sell -- even one reducing a long -- is refused while the running cash stays
        below zero after its proceeds (``a_new_anomaly…``, 2020-05-28: 48 orders, the
        deficit-carrying rule is the only one that reproduces the 20 refusals).
        """
        cash = self.cash
        pseudo = {}
        while self.submitted:
            order = self.submitted.popleft()
            data = order.data
            # The pre-check prices the order at what was known when it was placed: the
            # close of the submission bar for a market order, the limit or stop price
            # otherwise -- not at the fill price of the bar that just arrived.
            price = self._check_price(order, data)
            pos = pseudo.get(data)
            if pos is None:
                pos = pseudo[data] = self.positions[data].clone()
            if price is not None:
                cash += self._cash_delta(order, price, pos)
                pos.update(order.size, price)
            if order._check and self.p.checksubmit and price is not None and cash < 0:
                order.margin()
                self.notify(order)
                continue
            order.accept(self)
            self.notify(order)
            self.pending.append(order)
        remaining = collections.deque()
        while self.pending:
            order = self.pending.popleft()
            data = order.data
            bar = upcoming.get(data)
            if bar is None:
                remaining.append(order)
                continue
            if order.valid is not None and bar[4] > order.valid:
                order.expire()
                self.notify(order)
                continue
            if self._coc and order.exectype == Order.Market and len(data) >= 2:
                # cheat-on-close (observed): the market order fills at the close of the bar
                # it was placed on, everything else -- batch acceptance, notification at
                # the next bar, the pre-check at that same close -- unchanged
                price = self._slip(data.close[-1], order, True)
            else:
                price = self._fill_price(order, bar)
            if price is None:
                remaining.append(order)
                continue
            self._try_exec(order, price, bar[4])
        self.pending = remaining
        if self._cash_addition:
            self.cash += self._cash_addition
            self._cash_addition = 0.0
        self._value = self._mark_value()

    def _check_price(self, order, data):
        et = order.exectype
        if et in (Order.Limit, Order.StopLimit) and order.pricelimit is not None:
            return order.pricelimit if et == Order.StopLimit else order.price
        if et in (Order.Stop,) and order.price is not None:
            return order.price
        if et == Order.Limit and order.price is not None:
            return order.price
        n = len(data)
        if n >= 2:
            return data.close[-1]
        if n == 1:
            return data.close[0]
        return None

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

    def _fill_price(self, order, bar):
        o, h, lo, c = bar[0], bar[1], bar[2], bar[3]
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

    def _try_exec(self, order, price, dt=None):
        data = order.data
        comminfo = order.comminfo
        size = order.size
        pos = self.positions[data]
        comm = comminfo.getcommission(size, price)
        if not self._affordable(order, price):
            order.margin()
            self.notify(order)
            return
        if dt is None:
            dt = data.datetime[0]
        reversing = pos.size and ((pos.size > 0) != (size > 0)) and abs(size) > abs(pos.size)
        if reversing and comminfo.stocklike:
            # Observed on the oracle (breaking_bad_trends, 1993-04-02: short 1 998, buy 3 947):
            # the closing part of a reversing order always fills; the opening part fills
            # only if the cash left after closing covers it, otherwise the order ends
            # Partial (closing part executed) then Margin, the opening part untouched.
            closed = -pos.size
            opened = size - closed
            comm_closed = comminfo.getcommission(closed, price)
            comm_opened = comminfo.getcommission(opened, price)
            cash_after_close = self.cash - closed * price - comm_closed
            if cash_after_close - opened * price - comm_opened < 0:
                oldprice = pos.price
                newsize, _newprice, _o, c = pos.update(closed, price)
                pnl = comminfo.profitandloss(-c, oldprice, price)
                self.cash -= closed * price
                self.cash -= comm_closed
                comm_final = comminfo.confirmexec(closed, price)
                order.execute(dt, closed, price, c, c * price, comm_final, 0.0, 0.0, 0.0, 0.0, pnl)
                order.partial()
                self.notify(order)
                order.margin()
                self.notify(order)
                self._update_trade(order, data, closed, price, 0.0, c, pnl, comm_final, dt, newsize)
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
