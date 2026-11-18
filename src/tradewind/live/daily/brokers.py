"""The two accounts a daily run trades: ``SimAccount`` (simulated) and ``IBPaperAccount``.

Both answer the same four calls: ``settle`` (the fills since the last run), ``snapshot`` (cash,
positions, NAV), ``place`` (send the day's orders) and ``close``.

``SimAccount`` keeps its state in a JSON file and fills every order at the **next open**, the
price a backtest fills a market order placed on a close at, with the harness's costs: a
half-spread per symbol (``|quantity| x price x half_spread``, the spread charged by the
catalogue's backtests) and, optionally, IBKR's tiered commission. It needs no account, no TWS
and no network.

``IBPaperAccount`` is the ``IBAdapter`` of ``tradewind.live`` -- the adapter ``LiveEngine``
drives -- on a **paper account only**: it refuses any port other than the paper ports (TWS
7497, IB Gateway 4002) and any account whose id is not a paper id (``DU...``), and it connects
read-only unless the run sends orders. It holds no credentials: it attaches to a TWS or IB
Gateway session the user has already logged into.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field

from ..adapter import AdapterError
from ..ibkr import PAPER_PORTS, PAPER_PREFIXES, IBAdapter
from ..messages import Ack, Canceled, Execution, OrderRequest, Rejected
from .aggregate import ProposedOrder


def ibkr_tiered_commission(quantity: float, price: float) -> float:
    """IBKR Pro, tiered, US stocks, first volume tier: 0.0035 $/share, at least 0.35 $, at most
    1 % of the trade value (exchange and regulatory fees left out)."""
    q = abs(quantity)
    if not q:
        return 0.0
    return min(max(0.0035 * q, 0.35), 0.01 * q * abs(price))


@dataclass
class Fill:
    symbol: str
    quantity: float
    price: float
    reference: float                    # the open the backtest fills at
    costs: float
    order_id: str = ""


@dataclass
class Snapshot:
    cash: float
    nav: float
    positions: dict[str, float]
    fills: list[Fill] = field(default_factory=list)
    open_orders: list[tuple[str, float]] = field(default_factory=list)
    account: str = ""
    currency: str = "USD"


class SimAccount:
    """A simulated account in ``path`` (JSON). ``half_spread``: a cost per unit of notional,
    one number or ``{symbol: number}`` (symbols absent get ``default_half_spread``);
    ``commission``: ``"none"`` or ``"ibkr_tiered"``; ``slippage``: a signed fraction added to
    the open for buys and taken from it for sells (0: fill at the open)."""

    kind = "sim"

    def __init__(self, path: str, cash: float = 110_000.0, *, half_spread=0.0, default_half_spread: float = 0.0,
                 commission: str = "none", slippage: float = 0.0, use_book_spreads: bool = False):
        self.path = path
        self.half_spread = half_spread
        self.default_half_spread = float(default_half_spread)
        self.use_book_spreads = use_book_spreads
        if commission not in ("none", "ibkr_tiered"):
            raise ValueError("commission is 'none' or 'ibkr_tiered'")
        self.commission = commission
        self.slippage = float(slippage)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.state = json.load(fh)
        else:
            self.state = {"cash": float(cash), "positions": {}, "pending": [], "fills": [], "seq": 0,
                          "last_close": {}}
        self._spreads_from_books: dict[str, float] = {}

    # -- costs ------------------------------------------------------------------------------------
    def set_book_spreads(self, spreads: dict[str, float]) -> None:
        """The sleeves' own half-spreads (``SleeveBook.half_spread``), used when
        ``use_book_spreads`` is on: the simulated account then pays what the backtest pays."""
        self._spreads_from_books = dict(spreads)

    def _half_spread(self, symbol: str) -> float:
        if self.use_book_spreads and symbol in self._spreads_from_books:
            return self._spreads_from_books[symbol]
        if isinstance(self.half_spread, dict):
            return float(self.half_spread.get(symbol, self.default_half_spread))
        return float(self.half_spread)

    def _costs(self, quantity: float, price: float, symbol: str) -> float:
        c = abs(quantity) * price * self._half_spread(symbol)
        if self.commission == "ibkr_tiered":
            c += ibkr_tiered_commission(quantity, price)
        return c

    # -- the four calls ---------------------------------------------------------------------------
    def settle(self, date: str, opens: dict[str, float], closes: dict[str, float]) -> list[Fill]:
        """Fill the pending orders at ``date``'s open; an order on a symbol without an open
        that day stays pending."""
        st = self.state
        if st.get("date") == date:                  # this day is settled already (a second run)
            st["last_close"].update(closes)
            self._save()
            return [Fill(**f) for f in st.get("fills", [])]
        fills, keep = [], []
        for order in st["pending"]:
            sym, q = order["symbol"], float(order["quantity"])
            if order.get("placed", "") >= date:     # placed at this close: fills at the next open
                keep.append(order)
                continue
            ref = opens.get(sym)
            if ref is None or not math.isfinite(ref) or ref <= 0:
                keep.append(order)
                continue
            price = ref * (1 + math.copysign(self.slippage, q))
            costs = self._costs(q, price, sym)
            st["cash"] -= q * price + costs
            pos = st["positions"].get(sym, 0.0) + q
            if abs(pos) < 1e-12:
                st["positions"].pop(sym, None)
            else:
                st["positions"][sym] = pos
            fills.append(Fill(sym, q, price, ref, costs, order["id"]))
        st["pending"] = keep
        st["fills"] = [f.__dict__ for f in fills]
        st["last_close"].update(closes)
        st["date"] = date
        self._save()
        return fills

    def snapshot(self, closes: dict[str, float] | None = None) -> Snapshot:
        st = self.state
        marks = dict(st["last_close"])
        marks.update(closes or {})
        value = st["cash"]
        for sym, q in st["positions"].items():
            if sym not in marks:
                raise ValueError(f"no price to mark {sym} in the simulated account")
            value += q * marks[sym]
        return Snapshot(cash=st["cash"], nav=value, positions=dict(st["positions"]),
                        fills=[Fill(**f) for f in st.get("fills", [])],
                        open_orders=[(o["symbol"], o["quantity"]) for o in st["pending"]], account="SIM")

    def place(self, orders: list[ProposedOrder], date: str) -> list[dict]:
        out = []
        for o in orders:
            self.state["seq"] += 1
            oid = f"sim-{date}-{self.state['seq']:05d}"
            self.state["pending"].append({"id": oid, "symbol": o.symbol, "quantity": float(o.quantity),
                                          "placed": date})
            out.append({"id": oid, "symbol": o.symbol, "quantity": o.quantity, "status": "queued"})
        self._save()
        return out

    def close(self) -> None:
        self._save()

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


class IBPaperAccount:
    """A paper account at Interactive Brokers through TWS / IB Gateway (``IBAdapter``).

    ``send=False`` (propose): connects read-only, reads the account, never places anything.
    ``send=True``: places the orders as market-on-open (``MKT``, ``OPG``) and waits
    ``ack_wait`` seconds for IB's acknowledgements or rejections.
    """

    kind = "ibkr"

    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 31, *, account: str = "",
                 send: bool = False, ack_wait: float = 15.0, adapter: IBAdapter | None = None):
        if int(port) not in PAPER_PORTS:
            raise AdapterError(f"port {port} is not a paper port {sorted(PAPER_PORTS)}: this runner trades "
                               f"paper accounts only")
        if account and not account.startswith(PAPER_PREFIXES):
            raise AdapterError(f"account {account!r} is not a paper account (DU...)")
        self.send = send
        self.ack_wait = float(ack_wait)
        self.adapter = adapter or IBAdapter(host, int(port), int(client_id), account=account,
                                            require_paper=True, readonly=not send)
        if not self.adapter.require_paper:
            raise AdapterError("IBPaperAccount needs an adapter with require_paper on")
        self.adapter.connect()                  # refuses a live port or a non-paper account
        acct = self.adapter.account
        if not acct or not acct.startswith(PAPER_PREFIXES):
            self.adapter.disconnect()
            raise AdapterError(f"connected account {acct!r} is not a single paper account (DU...); "
                               f"pass account='DU...'")
        self.account = acct

    def settle(self, date: str, opens: dict[str, float], closes: dict[str, float]) -> list[Fill]:
        snap = self.adapter.snapshot(full_sync=True)
        fills = []
        for e in snap.executions:
            ref = opens.get(e.symbol, e.price)
            fills.append(Fill(e.symbol, float(e.quantity), float(e.price), float(ref), float(e.commission),
                              e.client_id))
        self._fills = fills
        return fills

    def snapshot(self, closes: dict[str, float] | None = None) -> Snapshot:
        snap = self.adapter.snapshot(full_sync=True)
        if snap.net_liquidation is None or snap.cash is None:
            raise AdapterError("the account values are not available yet (TWS still synchronising?)")
        positions = {s: q for s, (q, _avg) in snap.position_map().items() if q}
        return Snapshot(cash=float(snap.cash), nav=float(snap.net_liquidation), positions=positions,
                        fills=list(getattr(self, "_fills", [])),
                        open_orders=[(o.symbol, o.quantity - math.copysign(o.filled, o.quantity))
                                     for o in snap.open_orders],
                        account=self.account, currency=snap.currency)

    def place(self, orders: list[ProposedOrder], date: str) -> list[dict]:
        if not self.send:
            raise AdapterError("this account was opened read-only (propose mode): nothing is sent")
        stamp = date.replace("-", "")
        ids = {}
        for i, o in enumerate(orders, 1):
            cid = f"twd-{stamp}-{i:04d}"
            ids[cid] = o
            self.adapter.place(OrderRequest(cid, o.symbol, float(o.quantity), "MKT", tif="OPG"))
        status = {cid: "sent" for cid in ids}
        reasons: dict[str, str] = {}
        deadline = time.monotonic() + self.ack_wait
        while time.monotonic() < deadline and any(v == "sent" for v in status.values()):
            self.adapter.idle(0.5)
            for m in self.adapter.drain():
                cid = getattr(m, "client_id", "")
                if cid not in status:
                    continue
                if isinstance(m, Ack):
                    status[cid] = "acknowledged"
                elif isinstance(m, Rejected):
                    status[cid], reasons[cid] = "rejected", m.reason
                elif isinstance(m, Canceled):
                    status[cid], reasons[cid] = "cancelled", m.reason
                elif isinstance(m, Execution):
                    status[cid] = "filled"
        return [{"id": cid, "symbol": o.symbol, "quantity": o.quantity, "status": status[cid],
                 "reason": reasons.get(cid, "")} for cid, o in ids.items()]

    def close(self) -> None:
        self.adapter.disconnect()


__all__ = ["Fill", "IBPaperAccount", "SimAccount", "Snapshot", "ibkr_tiered_commission"]
