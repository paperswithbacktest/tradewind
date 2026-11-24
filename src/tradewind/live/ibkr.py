"""Interactive Brokers adapter (spec/07): ``IBAdapter`` and ``IBBars``, over ib_async.

``pip install tradewind[ibkr]`` installs ib_async (BSD 2-Clause), the maintained successor of
ib_insync, which speaks the TWS API socket protocol itself (no IB jar or zip to install). It
is imported only when ``connect`` is called: the module imports without it, and the
mapping functions below are plain Python, tested without it.

Written from IB's public TWS API documentation (order types and times in force, order
statuses, message codes, pacing limits) and from ib_async's public interface; spec/07 § 5
tabulates the mapping.

Threading: none. ib_async runs an asyncio loop, and the engine runs it in ``idle`` -- the
only place a live session waits -- so every callback lands on the engine's thread, between
two slices, and is turned into a queued message.
"""

from __future__ import annotations

import collections
import datetime as _dt
import time

from .adapter import AdapterError, BarRecord
from .guards import KeyedInterval, RateLimiter
from .messages import (
    AccountSnapshot,
    Ack,
    BrokerOrder,
    Canceled,
    Connection,
    Execution,
    Notice,
    OrderRequest,
    Rejected,
)

PAPER_PORTS = frozenset({7497, 4002})       # TWS paper, IB Gateway paper (defaults)
LIVE_PORTS = frozenset({7496, 4001})        # TWS live, IB Gateway live (defaults)
PAPER_PREFIXES = ("DU", "DF")               # paper account ids

# message codes (TWS API "message codes" page)
# warnings (status ValidationError) after which TWS / IB Gateway has not sent the order on:
# 321 "The API interface is currently in Read-Only mode" (API settings: Read-Only API ticked)
REFUSED_WARNINGS = frozenset({321})
LINK_LOST = frozenset({1100, 2110})         # TWS <-> IB servers lost
LINK_RESTORED = frozenset({1101, 1102})     # restored; 1101: data lost, resubscribe
SOCKET_ERRORS = frozenset({502, 504, 1300})  # cannot connect / not connected / socket port reset

# IB order status -> what it means here
_WORKING = frozenset({"PreSubmitted", "Submitted"})
_PENDING = frozenset({"PendingSubmit", "ApiPending", "PendingCancel"})
_CANCELLED = frozenset({"Cancelled", "ApiCancelled"})

_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def _never_sent(trade) -> bool:
    """An order TWS refused before sending it on (a ``REFUSED_WARNINGS`` warning, no permId):
    it is not working at the broker, although ib_async keeps it among the open trades."""
    if trade.orderStatus.status != "ValidationError" or getattr(trade.order, "permId", 0):
        return False
    return any(int(getattr(e, "errorCode", 0) or 0) in REFUSED_WARNINGS for e in getattr(trade, "log", ()) or ())


_CURRENCY = frozenset({"USD", "EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY", "NOK", "SEK", "DKK", "PLN", "CZK", "HUF",
                       "TRY", "MXN", "ZAR", "SGD", "HKD", "CNH", "ILS", "KRW", "RUB"})


def is_cash_pair(symbol: str) -> bool:
    """An IDEALPRO pair written as IB's local symbol, ``EUR.USD`` or ``USD.JPY``."""
    return (len(symbol) == 7 and symbol[3] == "." and symbol[:3] in _CURRENCY and symbol[4:] in _CURRENCY
            and symbol[:3] != symbol[4:])


def contract_name(contract) -> str:
    """The symbol a contract is known by here: its symbol, or ``BASE.QUOTE`` for a forex pair
    (every USD.xxx pair has the symbol ``USD``)."""
    if getattr(contract, "secType", "") == "CASH":
        return f"{contract.symbol}.{contract.currency}"
    return contract.symbol


def _ns_of(when) -> int:
    if isinstance(when, _dt.datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=_dt.timezone.utc)
        return int((when - _EPOCH).total_seconds() * 1_000_000) * 1000
    if isinstance(when, _dt.date):
        return _ns_of(_dt.datetime(when.year, when.month, when.day, tzinfo=_dt.timezone.utc))
    return int(when)


def ib_order_fields(req: OrderRequest, account: str = "") -> dict:
    """The fields of an IB ``Order`` for a generic request (spec/07 § 5.1)."""
    fields = {
        "action": "BUY" if req.quantity > 0 else "SELL",
        "totalQuantity": abs(float(req.quantity)),
        "orderType": req.order_type,
        "tif": req.tif,
        "orderRef": req.client_id,
        "outsideRth": False,
        "transmit": True,
    }
    if req.order_type in ("LMT", "STP LMT"):
        fields["lmtPrice"] = float(req.limit_price)
    if req.order_type in ("STP", "STP LMT"):
        fields["auxPrice"] = float(req.stop_price)
    if req.tif == "GTD":
        when = _dt.datetime.fromtimestamp(req.good_till / 1e9, tz=_dt.timezone.utc)
        fields["goodTillDate"] = when.strftime("%Y%m%d %H:%M:%S") + " UTC"
    if account:
        fields["account"] = account
    return fields


def status_message(client_id: str, status: str, *, acked: bool, cancel_asked: bool, tif: str,
                   reason: str = "", code: int = 0, t: int = 0):
    """The message an IB order status means, or None (spec/07 § 5.2).

    ``Cancelled`` before any acknowledgement is a rejection (ib_async turns an order error into
    a ``Cancelled`` status, the error code in the trade's log); after one, a cancellation --
    an expiry when nobody asked for it and the order had a DAY, OPG or GTD time in force.
    ``Inactive`` is a rejection (IB's risk checks, a rejected order kept inactive)."""
    if status in _WORKING:
        return None if acked else Ack(client_id, "", t)
    if status in _CANCELLED:
        if not acked and not cancel_asked:
            return Rejected(client_id, reason or "rejected by the broker", code, t)
        expired = not cancel_asked and tif in ("DAY", "OPG", "GTD")
        return Canceled(client_id, reason or ("time in force elapsed" if expired else "canceled"), expired, t)
    if status == "Inactive":
        return Rejected(client_id, reason or "order inactive at the broker", code, t)
    if status == "ValidationError":
        if code in REFUSED_WARNINGS:    # a warning that means the order never left TWS / IB Gateway
            return Rejected(client_id, reason or "order refused by TWS / IB Gateway", code, t)
        return Notice(reason or "order validation warning", code, client_id, t)
    return None                         # Filled (executions carry it), pending states


class IBAdapter:
    """A ``BrokerAdapter`` on TWS or IB Gateway.

    ``require_paper`` (default on) refuses to connect unless the port is a paper port and every
    managed account is a paper account (``DU...``): going live is a deliberate change of two
    arguments. ``readonly`` connects with the API's read-only flag (no order can be sent).
    ``market_tif`` is decided by ``LiveBroker``; this adapter maps what it is given.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 17, *, account: str = "",
                 require_paper: bool = True, readonly: bool = False, exchange: str = "SMART",
                 currency: str = "USD", contracts: dict | None = None, reconnect: bool = True,
                 reconnect_delays: tuple = (2, 5, 10, 30, 60), commission_wait: float = 10.0,
                 base_fx: float | None = None, ib=None, ib_module=None):
        self.host, self.port, self.client_id = host, int(port), int(client_id)
        self.base_fx = base_fx                 # one unit of the base currency in `currency` (EURUSD)
        self.base_currency = currency
        self.fx_missing = False
        self.fx_used = 1.0
        self.fx_source = ""                    # "" (no conversion), "ib" or "given"
        self.base_detected = False             # the base currency was read off the account
        self.published_fx: float | None = None  # IB's rate: units of `currency` per unit of the base
        self.base_cash: float | None = None    # the cash held in the base currency (CashBalance)
        self.nav_base: float | None = None     # NetLiquidation in the base currency
        self.balances: dict[str, float] = {}   # CashBalance by currency, in its own units
        self.accrued: dict[str, float] = {}    # AccruedCash by currency (the base's when IB gives only BASE)
        self.currency_rates: dict[str, float] = {}  # ExchangeRate by currency: base units per unit
        self.account = account
        self.require_paper = require_paper
        self.readonly = readonly
        self.exchange = exchange
        self.currency = currency
        self.contracts = dict(contracts or {})
        self.instruments: dict[str, dict] = {}     # {symbol: spec} of contracts that are not stocks
        self.reconnect = reconnect
        self.reconnect_delays = tuple(reconnect_delays)
        self.commission_wait = float(commission_wait)
        self.ib = ib
        self._ibm = ib_module
        self._queue: collections.deque = collections.deque()
        self._acked: set[str] = set()
        self._cancel_asked: set[str] = set()
        self._tif: dict[str, str] = {}
        self._reported: set[str] = set()       # client ids with a final message already queued
        self._pending_exec: dict[str, tuple] = {}
        self._emitted_exec: set[str] = set()
        self._connected = False
        self._hooked = False
        self._retry_at = 0.0
        self._attempt = 0
        self.messages = RateLimiter(45, 1.0)           # IB: 50 messages a second
        self.history_limiter = RateLimiter(60, 600.0)  # IB: 60 historical requests in 10 minutes
        self.history_interval = KeyedInterval(15.0)    # IB: no identical request within 15 s

    # -- the library --------------------------------------------------------------------------------
    def _lib(self):
        if self._ibm is None:
            try:
                import ib_async
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise AdapterError("IBAdapter needs ib_async: pip install 'tradewind[ibkr]'") from exc
            self._ibm = ib_async
        return self._ibm

    def _build(self, symbol: str):
        """The unqualified contract of ``symbol``: a forex pair (``USD.JPY``) on IDEALPRO, a
        future described in ``instruments``, or a stock on ``exchange``."""
        lib = self._lib()
        if is_cash_pair(symbol):
            return lib.Forex(pair=symbol.replace(".", ""), exchange="IDEALPRO")
        spec = self.instruments.get(symbol)
        if spec and spec.get("sec_type") == "FUT":
            mult = spec.get("multiplier")
            return lib.Future(spec.get("symbol", symbol), str(spec["expiry"]), spec["exchange"],
                              currency=spec.get("currency", self.currency),
                              multiplier=("" if mult is None else f"{float(mult):g}"))
        return lib.Stock(symbol, self.exchange, self.currency)

    def contract(self, symbol: str):
        contract = self.contracts.get(symbol)
        if contract is None:
            contract = self._build(symbol)
            self.messages.acquire()
            qualified = self.ib.qualifyContracts(contract)
            if not qualified:
                raise AdapterError(f"IB does not know the contract for {symbol!r}")
            contract = self.contracts[symbol] = qualified[0]
        return contract

    # -- connection ---------------------------------------------------------------------------------
    def connect(self) -> None:
        lib = self._lib()
        if self.ib is None:
            self.ib = lib.IB()
        if self.require_paper and self.port in LIVE_PORTS:
            raise AdapterError(f"port {self.port} is a live-trading port and require_paper is on")
        self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=self.readonly,
                        account=self.account)
        accounts = list(self.ib.managedAccounts())
        if self.require_paper:
            chosen = [self.account] if self.account else accounts
            if not chosen or not all(a.startswith(PAPER_PREFIXES) for a in chosen):
                self.ib.disconnect()
                raise AdapterError(f"accounts {chosen} are not paper accounts and require_paper is on")
        if not self.account and len(accounts) == 1:
            self.account = accounts[0]
        self._hook()
        self._connected = True
        self._attempt = 0

    def _hook(self) -> None:
        if self._hooked:
            return
        handlers = {"orderStatusEvent": self._on_status, "execDetailsEvent": self._on_exec,
                    "commissionReportEvent": self._on_commission, "errorEvent": self._on_error,
                    "disconnectedEvent": self._on_disconnected}
        for name, handler in handlers.items():
            getattr(self.ib, name).connect(handler)
        self._hooked = True

    def disconnect(self) -> None:
        self._connected = False
        if self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()

    def is_connected(self) -> bool:
        return self._connected and self.ib is not None and self.ib.isConnected()

    def now(self) -> int:
        return time.time_ns()

    def bar_source(self) -> IBBars:
        return IBBars(self)

    # -- orders -------------------------------------------------------------------------------------
    def place(self, request: OrderRequest) -> None:
        t = self.now()
        if not self.is_connected():
            self._final(Rejected(request.client_id, "not connected to TWS / IB Gateway", 504, t))
            return
        if self.readonly:
            self._final(Rejected(request.client_id, "adapter connected read-only", 0, t))
            return
        try:
            contract = self.contract(request.symbol)
        except AdapterError as exc:
            self._final(Rejected(request.client_id, str(exc), 200, t))
            return
        self._tif[request.client_id] = request.tif
        order = self._lib().Order(**ib_order_fields(request, self.account))
        self.messages.acquire()
        self.ib.placeOrder(contract, order)

    def cancel(self, client_id: str) -> None:
        self._cancel_asked.add(client_id)
        for trade in self.ib.openTrades() if self.ib is not None else ():
            if trade.order.orderRef == client_id:
                self.messages.acquire()
                self.ib.cancelOrder(trade.order)
                return

    # -- callbacks (run inside idle) ------------------------------------------------------------------
    def _final(self, message) -> None:
        if message.client_id in self._reported:
            return
        self._reported.add(message.client_id)
        self._queue.append(message)

    def _on_status(self, trade) -> None:
        client_id = trade.order.orderRef
        if not client_id:
            return                      # not ours (another client, a manual order)
        status = trade.orderStatus.status
        reason, code = "", 0
        if trade.log:
            last = trade.log[-1]
            reason, code = last.message or "", int(last.errorCode or 0)
        message = status_message(client_id, status, acked=client_id in self._acked,
                                 cancel_asked=client_id in self._cancel_asked,
                                 tif=self._tif.get(client_id, trade.order.tif), reason=reason, code=code,
                                 t=self.now())
        if message is None:
            return
        if isinstance(message, Ack):
            self._acked.add(client_id)
            self._queue.append(Ack(client_id, str(trade.order.permId or trade.order.orderId), message.time))
        elif isinstance(message, Notice):
            self._queue.append(message)
        else:
            self._final(message)

    def _execution(self, fill, commission: float) -> Execution:
        ex = fill.execution
        sign = 1.0 if ex.side in ("BOT", "BUY") else -1.0
        return Execution(ex.orderRef, ex.execId, contract_name(fill.contract), sign * float(ex.shares), float(ex.price),
                         float(commission), _ns_of(fill.time))

    def _on_exec(self, trade, fill) -> None:
        if fill.execution.orderRef and fill.execution.execId not in self._emitted_exec:
            self._pending_exec[fill.execution.execId] = (fill, time.monotonic())

    def _on_commission(self, trade, fill, report) -> None:
        exec_id = fill.execution.execId
        self._pending_exec.pop(exec_id, None)
        if not fill.execution.orderRef or exec_id in self._emitted_exec:
            return
        self._emitted_exec.add(exec_id)
        self._queue.append(self._execution(fill, report.commission))

    def _flush_executions(self) -> None:
        """Executions whose commission report did not come within ``commission_wait``: sent
        with no commission (and a notice), rather than held back."""
        now = time.monotonic()
        for exec_id, (fill, seen) in list(self._pending_exec.items()):
            if now - seen >= self.commission_wait:
                del self._pending_exec[exec_id]
                self._emitted_exec.add(exec_id)
                self._queue.append(self._execution(fill, 0.0))
                self._queue.append(Notice(f"execution {exec_id} sent without its commission report", 0,
                                          fill.execution.orderRef, self.now()))

    def _on_error(self, req_id, code, message, contract) -> None:
        t = self.now()
        if code in LINK_LOST:
            self._queue.append(Connection(False, message, code, t))
        elif code in LINK_RESTORED:
            self._queue.append(Connection(True, message, code, t))
            self._queue.append(self.snapshot(full_sync=True))
        elif code in SOCKET_ERRORS:
            self._queue.append(Notice(message, code, "", t))
        elif 2100 <= code < 2200:
            return                      # farm status and other informational codes
        else:
            self._queue.append(Notice(message, code, "", t))

    def _on_disconnected(self) -> None:
        if self._connected:
            self._connected = False
            self._queue.append(Connection(False, "API socket closed", -1, self.now()))
            self._retry_at = time.monotonic()

    # -- account --------------------------------------------------------------------------------------
    def account_values(self) -> tuple[float | None, float | None]:
        """The account's cash and net liquidation value in ``self.currency``.

        IB reports ``NetLiquidation`` and ``TotalCashValue`` in the account's **base** currency
        only. When the base is not ``self.currency`` (an EUR account trading in USD), they are
        converted with IB's own ``ExchangeRate`` for ``self.currency`` (base units per unit,
        published once the account holds that currency), or else with ``base_fx`` (the price
        of one unit of the base currency in ``self.currency``: EURUSD for an EUR account).
        Neither: ``(None, None)``, and ``base_currency`` / ``fx_missing`` say why."""
        values: dict[tuple[str, str], float] = {}
        for value in self.ib.accountValues(self.account):
            tag = value.tag.removeprefix("$LEDGER-")
            try:
                values.setdefault((tag, value.currency), float(value.value))
            except (TypeError, ValueError):
                continue
        bases = sorted({cur for (tag, cur) in values if tag == "NetLiquidation" and cur != "BASE"})
        self.base_currency = self.currency if self.currency in bases or not bases else bases[0]
        self.base_detected = bool(bases)
        self.fx_missing = False
        self.nav_base = values.get(("NetLiquidation", self.base_currency))
        self.base_cash = values.get(("CashBalance", self.base_currency))
        self.balances = {cur: v for (tag, cur), v in values.items() if tag == "CashBalance" and cur != "BASE"}
        self.accrued = {cur: v for (tag, cur), v in values.items() if tag == "AccruedCash" and cur != "BASE"}
        if not self.accrued and ("AccruedCash", "BASE") in values:
            self.accrued = {self.base_currency: values[("AccruedCash", "BASE")]}
        self.currency_rates = {cur: v for (tag, cur), v in values.items()
                               if tag == "ExchangeRate" and cur != "BASE" and v > 0}
        if self.base_currency == self.currency:
            self.published_fx, self.fx_used, self.fx_source = 1.0, 1.0, ""
            return values.get(("TotalCashValue", self.currency)), values.get(("NetLiquidation", self.currency))
        rate = values.get(("ExchangeRate", self.currency))         # base units per unit of self.currency
        self.published_fx = 1.0 / rate if rate and rate > 0 else None
        if self.published_fx is not None:
            per_base, self.fx_source = self.published_fx, "ib"
        elif self.base_fx:
            per_base, self.fx_source = float(self.base_fx), "given"
        else:
            self.fx_missing = True
            return None, None
        self.fx_used = per_base
        cash = values.get(("TotalCashValue", self.base_currency))
        nlv = values.get(("NetLiquidation", self.base_currency))
        return (None if cash is None else cash * per_base), (None if nlv is None else nlv * per_base)

    def snapshot(self, full_sync: bool = False) -> AccountSnapshot:
        ib = self.ib
        t = self.now()
        if ib is None or not ib.isConnected():
            return AccountSnapshot(None, None, self.currency, time=t)
        cash, nlv = self.account_values()
        positions = tuple(sorted((contract_name(p.contract), float(p.position), float(p.avgCost))
                                 for p in ib.positions(self.account) if p.position))
        orders = tuple(BrokerOrder(tr.order.orderRef, contract_name(tr.contract),
                                   (1.0 if tr.order.action == "BUY" else -1.0) * float(tr.order.totalQuantity),
                                   float(tr.orderStatus.filled))
                       for tr in ib.openTrades() if tr.order.orderRef and not _never_sent(tr))
        executions = ()
        if full_sync:
            executions = tuple(self._execution(f, f.commissionReport.commission if f.commissionReport else 0.0)
                               for f in ib.fills() if f.execution.orderRef)
            self._emitted_exec.update(e.exec_id for e in executions)
        return AccountSnapshot(cash, nlv, self.currency, positions, orders, executions, full_sync, t)

    def shortable(self, symbols, wait: float = 4.0) -> dict[str, float]:
        """IB's shortable level of each stock (generic tick 236; delayed data accepted, since
        a paper account often has no real-time subscription): above 2.5 easy to borrow, above
        1.5 hard to borrow (a locate), otherwise not available. Read-only; a symbol IB does
        not answer for is left out."""
        if not self.is_connected():
            raise AdapterError("not connected to TWS / IB Gateway")
        ib = self.ib
        ib.reqMarketDataType(3)                 # delayed when there is no subscription
        tickers = {}
        for sym in symbols:
            try:
                contract = self.contract(sym)
            except AdapterError:
                continue
            self.messages.acquire()
            tickers[sym] = (contract, ib.reqMktData(contract, "236", False, False))
        end = time.monotonic() + wait
        while time.monotonic() < end and any(_nan(getattr(t, "shortable", None)) for _c, t in tickers.values()):
            ib.sleep(0.25)
        out = {}
        for sym, (contract, t) in tickers.items():
            v = getattr(t, "shortable", None)
            if not _nan(v):
                out[sym] = float(v)
            ib.cancelMktData(contract)
        return out

    # -- waiting ----------------------------------------------------------------------------------------
    def drain(self) -> list:
        self._flush_executions()
        out = list(self._queue)
        self._queue.clear()
        return out

    def idle(self, seconds: float) -> None:
        if self.is_connected():
            self.ib.sleep(seconds)
            return
        if not self.reconnect or self.ib is None:
            time.sleep(seconds)
            return
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.is_connected():
            if time.monotonic() >= self._retry_at:
                self._try_reconnect()
            if not self.is_connected():
                time.sleep(max(0.0, min(end, self._retry_at) - time.monotonic()))
        if self.is_connected() and time.monotonic() < end:
            self.ib.sleep(end - time.monotonic())

    def _try_reconnect(self) -> None:
        delay = self.reconnect_delays[min(self._attempt, len(self.reconnect_delays) - 1)]
        self._attempt += 1
        self._retry_at = time.monotonic() + delay
        try:
            self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=self.readonly,
                            account=self.account)
        except Exception as exc:  # noqa: BLE001 - TWS restarting, port closed: try again later
            self._queue.append(Notice(f"reconnect failed: {exc!r}", 502, "", self.now()))
            return
        self._connected = True
        self._attempt = 0
        self._queue.append(Connection(True, "reconnected", 0, self.now()))
        self._queue.append(self.snapshot(full_sync=True))


def _nan(v) -> bool:
    try:
        return v is None or not float(v) >= 0       # None, NaN or IB's -1
    except (TypeError, ValueError):
        return True


class IBBars:
    """Daily bars from IB's historical data, a ``BarSource``.

    A bar dated D is complete once the session of D has closed (``close`` in ``tz``) plus
    ``grace`` minutes; until then IB returns the bar still forming and it is never passed on.
    Requests are paced: none while no new bar can be complete, at most one per symbol every
    ``retry`` minutes while one is due, and IB's limits on top (``IBAdapter.history_limiter``).
    Exchange holidays are not known here: on one, the bar simply does not come.
    """

    def __init__(self, adapter: IBAdapter, *, what: str = "TRADES", use_rth: bool = True,
                 close: str = "16:00", tz: str = "America/New_York", grace: float = 15.0,
                 retry: float = 15.0, lookback_days: int = 10):
        from zoneinfo import ZoneInfo

        self.adapter = adapter
        self.what = what
        self.use_rth = use_rth
        hh, mm = (int(x) for x in close.split(":"))
        self.close_time = _dt.time(hh, mm)
        self.tz = ZoneInfo(tz)
        self.grace = _dt.timedelta(minutes=grace)
        self.retry_ns = int(retry * 60e9)
        self.lookback_days = int(lookback_days)
        self._asked: dict[str, int] = {}

    def complete_at(self, day: _dt.date) -> int:
        """ns when the daily bar of ``day`` is complete."""
        return _ns_of(_dt.datetime.combine(day, self.close_time, self.tz) + self.grace)

    def due(self, now: int) -> _dt.date:
        """The last weekday whose bar is complete at ``now``."""
        day = _dt.datetime.fromtimestamp(now / 1e9, tz=self.tz).date()
        while day.weekday() >= 5 or self.complete_at(day) > now:
            day -= _dt.timedelta(days=1)
        return day

    def _request(self, symbol: str, duration: str):
        adapter = self.adapter
        contract = adapter.contract(symbol)
        adapter.history_limiter.acquire()
        adapter.history_interval.acquire((symbol, duration, self.what))
        return adapter.ib.reqHistoricalData(contract, "", duration, "1 day", self.what, self.use_rth, formatDate=1)

    def _records(self, bars, after: int, now: int) -> list[BarRecord]:
        out = []
        for bar in bars:
            day = bar.date if not isinstance(bar.date, _dt.datetime) else bar.date.date()
            stamp = _ns_of(day)
            if stamp > after and self.complete_at(day) <= now:
                out.append(BarRecord(stamp, float(bar.open), float(bar.high), float(bar.low), float(bar.close),
                                     float(bar.volume)))
        return out

    def poll(self, symbol: str, after: int, now: int) -> list[BarRecord]:
        if not self.adapter.is_connected():
            return []
        if _ns_of(self.due(now)) <= after:
            return []                   # nothing new can be complete yet
        asked = self._asked.get(symbol)
        if asked is not None and now - asked < self.retry_ns:
            return []
        self._asked[symbol] = now
        return self._records(self._request(symbol, f"{self.lookback_days} D"), after, now)

    def history(self, symbol: str, duration: str = "2 Y"):
        """Complete daily bars as the DataFrame a ``LiveFeed`` warms up on."""
        import pandas as pd

        records = self._records(self._request(symbol, duration), -(1 << 62), self.adapter.now())
        index = pd.to_datetime([r.time for r in records], unit="ns")
        return pd.DataFrame({"open": [r.open for r in records], "high": [r.high for r in records],
                             "low": [r.low for r in records], "close": [r.close for r in records],
                             "volume": [r.volume for r in records]}, index=index)


__all__ = ["IBAdapter", "IBBars", "contract_name", "ib_order_fields", "is_cash_pair", "status_message"]
