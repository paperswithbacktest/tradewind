"""``LiveBroker``: the ``Broker`` of spec/05 § 6.5 in front of a real account (spec/07 § 5).

It keeps the compatible facade of the simulated broker (``getcash``, ``getvalue``,
``getposition``, ``buy``/``sell``/``cancel``, the notifications), so a strategy runs
unchanged; what differs is where the truth comes from:

* ``submit`` runs the guards and the local cash pre-check, journals the request, then sends
  it through the adapter at once (the order is placed on the bar's close, the venue fills it
  later);
* the venue's answers -- acknowledgements, executions, cancellations, rejections, account
  snapshots -- are messages the adapter queued; the engine moves them here at the next slice
  boundary (phase 5) and only then do orders, positions and cash change. The fills of the
  simulated broker's phase 6 are the venue's executions; its fill model is not used;
* reconciliation compares the positions and cash the broker reports with the local book.

Order states (Tradewind <- venue): ``Submitted`` when sent, ``Accepted`` on ``Ack``,
``Partial`` / ``Completed`` on ``Execution`` (by quantity executed), ``Canceled`` or
``Expired`` on ``Canceled``, ``Rejected`` on ``Rejected`` or when a guard refuses the order
locally, ``Margin`` when the local cash pre-check refuses it.
"""

from __future__ import annotations

import collections
import datetime as _dt

from ..broker import BackBroker
from ..dateutils import num2date
from ..models import BatchState
from ..order import Order
from ..trade import Trade
from .guards import KillSwitch, RiskLimits
from .messages import (
    AccountSnapshot,
    Ack,
    Canceled,
    Connection,
    Execution,
    Kill,
    Notice,
    OrderRequest,
    Rejected,
    decode,
    encode,
)

_EPS = 1e-9


class LiveDivergence(RuntimeError):
    """A replay asked for a command the journal does not hold at that point: the strategy
    is not deterministic, or the journal belongs to another strategy or history."""


def _valid_to_ns(valid) -> int:
    if isinstance(valid, _dt.datetime):
        if valid.tzinfo is None:
            valid = valid.replace(tzinfo=_dt.timezone.utc)
        return int(valid.timestamp() * 1e9)
    if isinstance(valid, _dt.date):
        return _valid_to_ns(_dt.datetime(valid.year, valid.month, valid.day, 23, 59, 59))
    return _valid_to_ns(num2date(float(valid)))


def order_request(order: Order, client_id: str, symbol: str, market_tif: str = "OPG") -> OrderRequest | None:
    """The generic request for a Tradewind order (spec/07 § 5.1); None when the order type has
    no live mapping yet (trailing stops, ``Historical``)."""
    et = order.exectype
    valid = order.valid
    if valid is None:
        tif, good_till = "GTC", None
    elif isinstance(valid, _dt.timedelta) and not valid or valid == 0 and not isinstance(valid, (_dt.date, _dt.timedelta)):
        tif, good_till = "DAY", None
    elif isinstance(valid, _dt.timedelta):
        return None                     # a relative validity needs the venue's clock: not mapped
    else:
        tif, good_till = "GTD", _valid_to_ns(valid)
    qty = float(order.size)
    if et == Order.Market:
        return OrderRequest(client_id, symbol, qty, "MKT", tif=market_tif if valid is None else tif,
                            good_till=good_till)
    if et == Order.Close:
        return OrderRequest(client_id, symbol, qty, "MOC", tif="DAY")
    if et == Order.Limit:
        return OrderRequest(client_id, symbol, qty, "LMT", limit_price=float(order.price), tif=tif,
                            good_till=good_till)
    if et == Order.Stop:
        return OrderRequest(client_id, symbol, qty, "STP", stop_price=float(order.price), tif=tif,
                            good_till=good_till)
    if et == Order.StopLimit:
        return OrderRequest(client_id, symbol, qty, "STP LMT", stop_price=float(order.price),
                            limit_price=float(order.pricelimit), tif=tif, good_till=good_till)
    return None


class LiveBroker(BackBroker):
    """A broker whose fills come from a real (or simulated) venue through ``adapter``.

    ``reconcile``: what a position mismatch found after the start does -- ``"adopt"`` takes the
    broker's quantity (the default; logged), ``"halt"`` trips the kill switch. The first
    snapshot of a session is always adopted: it is the account the strategy starts from.
    ``capital``: run as a sleeve of the account -- the cash the strategy sees starts at
    ``capital`` minus what its positions cost and then moves with its own executions only;
    without it the strategy sees (and follows) the account's cash.
    ``market_tif``: the time in force of a market order, ``"OPG"`` (the next open, what the
    backtest assumes for daily bars) by default.
    """

    def __init__(self, adapter, *, limits: RiskLimits | None = None, kill_switch: KillSwitch | None = None,
                 read_only: bool = False, reconcile: str = "adopt", capital: float | None = None,
                 market_tif: str = "OPG", session: str = "tw"):
        super().__init__()
        if reconcile not in ("adopt", "halt"):
            raise ValueError("reconcile must be 'adopt' or 'halt'")
        self.adapter = adapter
        self.limits = limits or RiskLimits()
        self.kill_switch = kill_switch or KillSwitch()
        self.read_only = bool(read_only)
        self.reconcile_policy = reconcile
        self.capital = None if capital is None else float(capital)
        self.market_tif = market_tif
        self.session = session
        self.journal = None
        self.connected = True
        self.killed = ""
        # orders
        self._by_client: dict[str, Order] = {}
        self._alive: dict[str, Order] = {}
        self._client_of: dict[int, str] = {}
        self._n_orders = 0
        self._exec_seen: set[str] = set()
        self._incoming: list = []
        self._batch: BatchState | None = None
        self._slice_orders = 0
        self._position = -1
        # modes: warm-up / catch-up suppress orders; replay checks commands instead of sending
        self.suppress = ""
        self.replaying = False
        self._expected: collections.deque = collections.deque()
        self._synced = False
        # what happened, for the operator (also journalled)
        self.reconciliations: list[dict] = []
        self.unmatched_executions: list[Execution] = []
        self.foreign_positions: dict[str, float] = {}
        self.notices: list = []

    # -- facade ------------------------------------------------------------------------------
    def add_cash(self, cash):
        raise NotImplementedError("add_cash has no live meaning: the cash is the account's "
                                  "(use LiveBroker(capital=...) to run as a sleeve)")

    def set_cash(self, cash):
        """Before a live run: the sleeve's capital (``capital``); the account's cash is read
        from the broker."""
        self.capital = float(cash)
        self.cash = self.startingcash = float(cash)
        self._value = self.cash

    setcash = set_cash

    def open_orders(self) -> list[Order]:
        return list(self._alive.values())

    def get_orders_open(self, safe=False):
        return list(self._alive.values())

    def client_id(self, order: Order) -> str | None:
        return self._client_of.get(order.ref)

    # -- the slice ------------------------------------------------------------------------------
    def set_time(self, s) -> None:
        super().set_time(s)
        self._batch = None
        self._slice_orders = 0
        self._position = s.position

    def receive(self, messages) -> None:
        """Messages for the next phase 5 (the engine calls this at the slice boundary)."""
        self._incoming.extend(messages)

    def acceptance(self, s) -> None:
        """Phase 5, live: apply the venue's messages in the order they arrived."""
        messages, self._incoming = self._incoming, []
        touched: set[str] = set()
        for message in messages:
            self._apply(message, touched)

    def fills(self, s) -> None:
        """Phase 6 has nothing left to do live: executions were applied in phase 5."""

    def transfers(self, s) -> None:
        """No ``add_cash`` live."""

    # -- order entry -------------------------------------------------------------------------------
    def submit(self, order, check=True):
        security = self._security(order.data)
        order.comminfo = self.getcommissioninfo(order.data)
        order.submit(self)
        self.orders.append(order)
        order._check = check
        if self.suppress:
            # warm-up and catch-up slices: the strategy steps, nothing is sent and nothing
            # is notified (Lean refuses orders during warm-up the same way)
            order.reject(self)
            order.info["tw_reason"] = self.suppress
            return order
        self.notify(order)
        symbol = getattr(order.data, "symbol", None) or security.symbol
        price = self._reference_price(order)
        reason = self._refusal(symbol, order, price)
        if reason:
            return self._refuse(order, reason)
        if self._batch is None:
            self._batch = BatchState(self.cash, {}, self._now)
        check = security.models.buying_power.check_order(security, order, price, self._batch)
        self._batch.running_cash -= check.cash_consumed
        if not check.accepted:
            order.margin()
            self.notify(order)
            return order
        self._n_orders += 1
        client_id = f"{self.session}-{self._n_orders:06d}"
        request = order_request(order, client_id, symbol, self.market_tif)
        if request is None:
            return self._refuse(order, f"{order.getordername()} orders are not supported live")
        self._slice_orders += 1
        self._by_client[client_id] = order
        self._client_of[order.ref] = client_id
        self._alive[client_id] = order
        order.info["tw_client_id"] = client_id
        self._command("place", encode(request))
        return order

    def cancel(self, order, bracket=False):
        client_id = self._client_of.get(order.ref)
        if client_id is None or client_id not in self._alive:
            return False
        self._command("cancel", {"client_id": client_id})
        return True

    @staticmethod
    def _reference_price(order):
        if order.exectype in (Order.Limit, Order.Stop, Order.StopLimit) and order.price is not None:
            return float(order.price)
        data = order.data
        return float(data.close[0]) if len(data) else None

    def _refusal(self, symbol, order, price) -> str:
        if self.killed:
            return f"kill switch: {self.killed}"
        if self.read_only:
            return "read-only session"
        if not self.connected:
            return "not connected to the broker"
        position = self.positions[order.data].size
        reason = self.limits.check(symbol, order.size, price, position, self._slice_orders)
        if reason and self.limits.trip_on_breach:
            self.kill(f"risk limit: {reason}")
        return reason

    def _refuse(self, order, reason):
        order.reject(self)
        order.info["tw_reason"] = reason
        self.notify(order)
        self._log("guard", ref=order.ref, symbol=getattr(order.data, "_name", ""), size=order.size, reason=reason)
        return order

    # -- commands: journalled first, then sent (or, in a replay, checked) ----------------------------
    def _command(self, op: str, payload: dict) -> None:
        record = {"op": op, "pos": int(self._position), "payload": payload}
        if self.replaying:
            expected = self._expected.popleft() if self._expected else None
            got = {"op": op, "pos": int(self._position), "payload": payload}
            if expected is None or {k: expected.get(k) for k in got} != got:
                raise LiveDivergence(f"replay asked for {got}, the journal holds {expected}")
            return
        if self.journal is not None:
            self.journal.write("cmd", **record)
        if op == "place":
            self.adapter.place(decode(payload))
        else:
            self.adapter.cancel(payload["client_id"])

    def _log(self, kind: str, **payload) -> None:
        if self.journal is not None and not self.replaying:
            self.journal.write(kind, **payload)

    # -- the kill switch ---------------------------------------------------------------------------
    def kill(self, reason: str) -> None:
        """Stop sending: every working order is cancelled, every later order refused."""
        if self.killed:
            return
        self.killed = reason or "kill switch"
        self._log("kill", reason=self.killed)
        for client_id in list(self._alive):
            self._command("cancel", {"client_id": client_id})

    def emergency_cancel(self) -> list[str]:
        """Cancel every working order now, from outside a slice (the kill switch tripping
        between bars). Not journalled as commands: the cancellations come back as messages."""
        sent = []
        for client_id in list(self._alive):
            try:
                self.adapter.cancel(client_id)
                sent.append(client_id)
            except Exception as exc:  # noqa: BLE001 - the switch must go on cancelling the others
                self.notices.append(f"cancel {client_id} failed: {exc!r}")
        return sent

    # -- the venue's messages ------------------------------------------------------------------------
    def _apply(self, message, touched: set) -> None:
        if isinstance(message, Execution):
            self._on_execution(message, touched)
        elif isinstance(message, Ack):
            order = self._alive.get(message.client_id)
            if order is not None and order.status == Order.Submitted:
                order.accept(self)
                self.notify(order)
        elif isinstance(message, Canceled):
            order = self._alive.pop(message.client_id, None)
            if order is not None:
                if message.expired:
                    order.expire()
                else:
                    order.cancel()
                order.info["tw_reason"] = message.reason
                self.notify(order)
        elif isinstance(message, Rejected):
            order = self._alive.pop(message.client_id, None)
            if order is not None:
                order.reject(self)
                order.info["tw_reason"] = message.reason
                self.notify(order)
        elif isinstance(message, Connection):
            self.connected = bool(message.connected)
        elif isinstance(message, AccountSnapshot):
            self._reconcile(message, touched)
        elif isinstance(message, Kill):
            self.kill(message.reason)
        elif isinstance(message, Notice):
            self.notices.append(message)

    def _on_execution(self, ex: Execution, touched: set) -> None:
        if ex.exec_id in self._exec_seen:
            return
        self._exec_seen.add(ex.exec_id)
        touched.add(ex.symbol)
        order = self._by_client.get(ex.client_id)
        if order is None or not ex.quantity:
            self.unmatched_executions.append(ex)
            return
        late = ex.client_id not in self._alive    # after the order's final message
        if order.status == Order.Submitted:
            order.accept(self)
            self.notify(order)
        data = order.data
        security = data.security
        comminfo = order.comminfo
        size, price, comm = float(ex.quantity), float(ex.price), float(ex.commission)
        book = self.positions[data]
        entry = book.price
        newsize, _avg, opened, closed = book.update(size, price)
        pnl = 0.0
        if closed:                              # realised on the part that reduced the position
            pnl = comminfo.profitandloss(-closed, entry, price)
        # the cash moves by the execution's own flow; the account's figure replaces it at the
        # next reconciliation unless the session runs as a sleeve (``capital``)
        self.cashbook.add(self.cashbook.account_currency, -size * price - comm)
        share = abs(closed) / abs(size) if size else 0.0
        closedcomm = comm * share
        openedcomm = comm - closedcomm
        order.execute(self._dtnum, size, price, closed, -closed * entry, closedcomm, opened,
                      opened * price, openedcomm, 0.0, pnl)
        self._account(security, comm, pnl)
        done = abs(order.executed.size) >= abs(order.size) - _EPS
        if late:
            # the book follows the execution; the order keeps the final status it was given
            self.notices.append(f"execution {ex.exec_id} after the final status of {ex.client_id}")
        elif done:
            order.completed()
            self._alive.pop(ex.client_id, None)
            self.notify(order)
        else:
            order.partial()
            self.notify(order)
        parts = (closedcomm, openedcomm) if closed and opened else None
        self._update_trade(order, data, size, price, opened, closed, pnl, comm, self._dtnum, newsize, parts=parts)

    # -- reconciliation ---------------------------------------------------------------------------------
    def _reconcile(self, snap: AccountSnapshot, touched: set) -> None:
        for ex in snap.executions:              # what we missed while away (resume, reconnect)
            self._on_execution(ex, touched)
        record = {"time": int(snap.time), "first": not self._synced, "positions": [], "orphans": []}
        if snap.full_sync:
            listed = {o.client_id for o in snap.open_orders}
            for client_id, order in list(self._alive.items()):
                if client_id not in listed:
                    # sent, then lost (a crash between the journal and the socket) or done while
                    # we were away without an execution we could see
                    self._alive.pop(client_id)
                    order.cancel()
                    order.info["tw_reason"] = "not working at the broker after a resync"
                    self.notify(order)
                    record["orphans"].append(client_id)
        held = snap.position_map()
        names = set()
        working: dict[str, float] = {}         # symbol -> signed quantity still to execute
        for o in self._alive.values():
            sym = getattr(o.data, "symbol", None) or o.data._name
            working[sym] = working.get(sym, 0.0) + (o.size - o.executed.size)
        for security in self.securities:
            data = security.feed
            symbol = getattr(data, "symbol", None) or security.symbol
            names.add(symbol)
            qty, avg = held.get(symbol, (0.0, 0.0))
            pos = self.positions[data]
            diff = qty - pos.size
            if abs(diff) <= _EPS:
                continue
            if self._synced:
                if symbol in touched:
                    continue                    # executions of this batch: the figure may lag
                rest = working.get(symbol, 0.0)
                if min(0.0, rest) - _EPS <= diff <= max(0.0, rest) + _EPS:
                    continue                    # explained by a working order executing now
            record["positions"].append({"symbol": symbol, "local": pos.size, "broker": qty})
            if self._synced and self.reconcile_policy == "halt":
                self.kill(f"position mismatch on {symbol}: local {pos.size:g}, broker {qty:g}")
                continue
            self._adopt(data, qty, avg)
        self.foreign_positions = {s: q for s, (q, _a) in held.items() if s not in names and q}
        if snap.cash is not None:
            if self.capital is None:
                if not self._synced or not self._alive:
                    record["cash_drift"] = snap.cash - self.cash
                    self.cash = float(snap.cash)
            elif not self._synced:
                cost = sum(abs(self.positions[s.feed].size) * self.positions[s.feed].price
                           for s in self.securities)
                self.cash = self.capital - cost
        self._synced = True
        self._mark_cache = None
        if record["positions"] or record["orphans"] or record["first"] or abs(record.get("cash_drift", 0.0)) > 0.005:
            self.reconciliations.append(record)
            self._log("recon", **record)

    def _adopt(self, data, qty: float, avg: float) -> None:
        pos = self.positions[data]
        if qty and isinstance(qty, float) and qty.is_integer():
            qty = int(qty)
        pos.set(qty, float(avg) if qty else 0.0)
        trade = Trade(data=data)
        if qty:
            trade.update(None, qty, float(avg), qty, 0.0, 0.0, 0.0, self._dtnum, qty)
            self.trades[data] = trade
        else:
            self.trades.pop(data, None)


__all__ = ["LiveBroker", "LiveDivergence", "order_request"]
