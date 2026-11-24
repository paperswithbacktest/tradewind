"""The accounts a daily run trades: ``SimAccount`` (simulated), ``IBPaperAccount`` and
``KrakenAccount`` (crypto spot, validation mode unless armed; spec/11).

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

**Instruments at IBKR** (``instruments.py``). A stock or ETF is a ``Stock`` on SMART, sent
market-on-open (``MKT``, ``OPG``). A spot forex symbol of the books (``AUDUSD``, ``JPYUSD``) is
sent on its IDEALPRO pair, in IB's direction and in units of the pair's base currency
(``USD.JPY``: ``q`` JPY bought = ``q x price`` USD sold), as a market order good till
cancelled (IDEALPRO has no opening auction: it fills when sent, or at the reopening on a
weekend); positions and executions come back in the books' units (an inverted pair through
its average cost, so the JPY held is what was exchanged). A future (an index's substitute) is
a ``MKT`` ``DAY`` order on the contract the meta-portfolio names. An index is never sent:
``place`` refuses the whole batch before sending anything.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import time
from dataclasses import dataclass, field

from ..adapter import AdapterError
from ..guards import RiskLimits
from ..ibkr import PAPER_PORTS, PAPER_PREFIXES, IBAdapter
from ..kraken import DEFAULT_TAKER_FEE, NON_TRADABLE_SUFFIXES, KrakenAdapter, catalogue_base
from ..messages import Ack, Canceled, Execution, OrderRequest, Rejected
from .aggregate import ProposedOrder
from .instruments import fx_pair, ibkr_fx_commission, is_index


def ibkr_tiered_commission(quantity: float, price: float) -> float:
    """IBKR Pro, tiered, US stocks, first volume tier: 0.0035 $/share, at least 0.35 $, at most
    1 % of the trade value (exchange and regulatory fees left out)."""
    q = abs(quantity)
    if not q:
        return 0.0
    return min(max(0.0035 * q, 0.35), 0.01 * q * abs(price))


def estimated_commission(order: ProposedOrder) -> float:
    """IBKR's commission for a proposed order: IDEALPRO's for a forex symbol, the tiered stock
    rate otherwise."""
    if fx_pair(order.symbol) is not None:
        return ibkr_fx_commission(order.notional)
    return ibkr_tiered_commission(order.quantity, order.price)


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
    currency: str = "USD"               # the currency of cash and nav (the books': USD)
    base_currency: str = "USD"          # the account's own currency
    fx_rate: float = 1.0                # USD per unit of base_currency
    nav_base: float | None = None       # the NAV in base_currency
    base_cash: float = 0.0              # cash held in base_currency (its value moves with fx_rate)


class SimAccount:
    """A simulated account in ``path`` (JSON). ``half_spread``: a cost per unit of notional,
    one number or ``{symbol: number}`` (symbols absent get ``default_half_spread``);
    ``commission``: ``"none"`` or ``"ibkr_tiered"``; ``slippage``: a signed fraction added to
    the open for buys and taken from it for sells (0: fill at the open)."""

    kind = "sim"
    hedge_in_base_cash = False          # the hedge's pair is a position, apart from base_cash

    def __init__(self, path: str, cash: float = 110_000.0, *, half_spread=0.0, default_half_spread: float = 0.0,
                 commission: str = "none", slippage: float = 0.0, use_book_spreads: bool = False,
                 currency: str | None = None, base_fx: float | None = None):
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
            held = self.state.get("currency", "USD")
            if currency and currency.upper() != held:
                raise ValueError(f"the simulated account in {path} is in {held}, not {currency.upper()}")
        else:
            cur = (currency or "USD").upper()
            usd = cur == "USD"
            # an account in another currency starts with its cash in that currency; the trades
            # settle in USD (a USD balance, negative when borrowed), as at IBKR
            self.state = {"cash": float(cash) if usd else 0.0, "base_cash": 0.0 if usd else float(cash),
                          "currency": cur, "positions": {}, "pending": [], "fills": [], "seq": 0,
                          "last_close": {}}
        self.currency = self.state.get("currency", "USD")
        self.explicit_currency = bool(currency)
        self.base_fx = base_fx
        self.fx_rate: float | None = 1.0 if self.currency == "USD" else None
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
            if fx_pair(symbol) is not None:
                c += ibkr_fx_commission(abs(quantity) * price)
            else:
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

    # -- the account's currency -----------------------------------------------------------------------
    def currency_info(self) -> dict:
        """The account's currency and the rates it knows (USD per unit): a simulated account
        publishes none; ``fallback`` is the rate given (``--base-fx-rate``)."""
        return {"currency": self.currency, "published": None, "fallback": self.base_fx}

    def set_fx(self, rate: float) -> None:
        """The day's rate, USD per unit of the account's currency."""
        self.fx_rate = float(rate)

    def snapshot(self, closes: dict[str, float] | None = None) -> Snapshot:
        st = self.state
        marks = dict(st["last_close"])
        marks.update(closes or {})
        base_cash = float(st.get("base_cash", 0.0))
        if base_cash and self.fx_rate is None:
            raise ValueError(f"the simulated account is in {self.currency}: set the day's rate first (set_fx)")
        rate = self.fx_rate or 1.0
        cash = st["cash"] + base_cash * rate
        value = cash
        for sym, q in st["positions"].items():
            if sym not in marks:
                raise ValueError(f"no price to mark {sym} in the simulated account")
            value += q * marks[sym]
        return Snapshot(cash=cash, nav=value, positions=dict(st["positions"]),
                        fills=[Fill(**f) for f in st.get("fills", [])],
                        open_orders=[(o["symbol"], o["quantity"]) for o in st["pending"]], account="SIM",
                        base_currency=self.currency, fx_rate=rate, nav_base=value / rate, base_cash=base_cash)

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
    #: a trade on the base currency's pair (the hedge of spec/13) settles into the base
    #: currency's cash balance at IBKR (CashBalance); to check with one paper order before a
    #: hedge is ever sent
    hedge_in_base_cash = True

    def __init__(self, host: str = "127.0.0.1", port: int = 7497, client_id: int = 31, *, account: str = "",
                 send: bool = False, ack_wait: float = 15.0, base_fx: float | None = None,
                 adapter: IBAdapter | None = None):
        if int(port) not in PAPER_PORTS:
            raise AdapterError(f"port {port} is not a paper port {sorted(PAPER_PORTS)}: this runner trades "
                               f"paper accounts only")
        if account and not account.startswith(PAPER_PREFIXES):
            raise AdapterError(f"account {account!r} is not a paper account (DU...)")
        self.send = send
        self.ack_wait = float(ack_wait)
        self.adapter = adapter or IBAdapter(host, int(port), int(client_id), account=account,
                                            require_paper=True, readonly=not send, base_fx=base_fx)
        if not self.adapter.require_paper:
            raise AdapterError("IBPaperAccount needs an adapter with require_paper on")
        self.adapter.connect()                  # refuses a live port or a non-paper account
        acct = self.adapter.account
        if not acct or not acct.startswith(PAPER_PREFIXES):
            self.adapter.disconnect()
            raise AdapterError(f"connected account {acct!r} is not a single paper account (DU...); "
                               f"pass account='DU...'")
        self.account = acct

    # -- the account's currency -----------------------------------------------------------------------
    def currency_info(self) -> dict:
        """The account's base currency as IB reports it (the currency of ``NetLiquidation``),
        IB's own rate (USD per unit, once the account holds USD) and the rate given
        (``--base-fx-rate``)."""
        ad = self.adapter
        given = getattr(self, "_given_fx", None)
        if given is None:
            given = self._given_fx = ad.base_fx
        ad.base_fx = given
        ad.account_values()
        if not ad.base_detected:
            raise AdapterError("the account values are not available yet (TWS still synchronising?): its "
                               "currency cannot be read")
        return {"currency": ad.base_currency, "published": ad.published_fx if ad.base_currency != ad.currency else 1.0,
                "fallback": given}

    def set_fx(self, rate: float) -> None:
        """The rate to use when IB publishes none (USD per unit of the base currency)."""
        if getattr(self, "_given_fx", None) is None:
            self._given_fx = self.adapter.base_fx
        self.adapter.base_fx = float(rate)

    # -- the books' symbols at IB --------------------------------------------------------------------
    def set_instruments(self, specs: dict[str, dict]) -> None:
        """Contracts that are not stocks (an index's future substitute): ``{symbol: spec}``."""
        self.adapter.instruments.update(specs)

    def _book_symbol(self, ib_symbol: str, known) -> str:
        """The books' name of an IB symbol (``USD.JPY`` -> ``JPYUSD``, or the name a book uses)."""
        pair = fx_pair(ib_symbol) if "." in ib_symbol else None
        if pair is None:
            return ib_symbol
        for name in known:
            p = fx_pair(name)
            if p is not None and p.ib_pair == pair.ib_pair and p.usd_quoted:
                return name
        return f"{pair.currency}USD"

    def _to_book(self, ib_symbol: str, quantity: float, price: float, known) -> tuple[str, float, float]:
        """An IB position or execution (``quantity`` of ``ib_symbol`` at ``price``) in the books'
        symbol, units and price."""
        name = self._book_symbol(ib_symbol, known)
        pair = fx_pair(name) if name != ib_symbol else None
        if pair is None:
            return name, quantity, price
        return name, pair.book_quantity(quantity, price), pair.book_price(price)

    def settle(self, date: str, opens: dict[str, float], closes: dict[str, float]) -> list[Fill]:
        snap = self.adapter.snapshot(full_sync=True)
        known = set(opens) | set(closes)
        fills = []
        for e in snap.executions:
            sym, q, px = self._to_book(e.symbol, float(e.quantity), float(e.price), known)
            ref = opens.get(sym, px)
            fills.append(Fill(sym, q, px, float(ref), float(e.commission), e.client_id))
        self._fills = fills
        return fills

    def snapshot(self, closes: dict[str, float] | None = None) -> Snapshot:
        snap = self.adapter.snapshot(full_sync=True)
        if getattr(self.adapter, "fx_missing", False):
            base, cur = self.adapter.base_currency, self.adapter.currency
            raise AdapterError(f"the account's base currency is {base}: IB publishes no {cur} exchange rate "
                               f"until the account holds {cur}, so its value cannot be read in {cur}; pass "
                               f"the rate (--base-fx-rate, the price of 1 {base} in {cur})")
        if snap.net_liquidation is None or snap.cash is None:
            raise AdapterError("the account values are not available yet (TWS still synchronising?)")
        known = set(closes or {})
        positions: dict[str, float] = {}
        for s, (q, avg) in snap.position_map().items():
            if not q:
                continue
            pair = fx_pair(s) if "." in s else None
            if pair is not None and pair.inverted and not avg:
                # no average cost: the book's units at the close (approximate)
                name = self._book_symbol(s, known)
                px = (closes or {}).get(name)
                if not px:
                    raise AdapterError(f"{s}: no average cost and no close of {name} to read the position in "
                                       f"{pair.currency}")
                positions[name] = positions.get(name, 0.0) - q / px
                continue
            name, bq, _px = self._to_book(s, q, avg, known)
            positions[name] = positions.get(name, 0.0) + bq
        working = []
        for o in snap.open_orders:
            rest = o.quantity - math.copysign(o.filled, o.quantity)
            name = self._book_symbol(o.symbol, known)
            pair = fx_pair(name) if name != o.symbol else None
            if pair is not None and pair.inverted:
                px = (closes or {}).get(name)
                rest = -rest / px if px else float("nan")
            working.append((name, rest))
        ad = self.adapter
        rate = float(ad.fx_used) if ad.base_currency != ad.currency else 1.0
        return Snapshot(cash=float(snap.cash), nav=float(snap.net_liquidation), positions=positions,
                        fills=list(getattr(self, "_fills", [])), open_orders=working,
                        account=self.account, currency=snap.currency, base_currency=ad.base_currency,
                        fx_rate=rate, nav_base=(ad.nav_base if ad.nav_base is not None
                                                else float(snap.net_liquidation) / rate),
                        base_cash=float(ad.base_cash or 0.0) if ad.base_currency != ad.currency else 0.0)

    def request(self, client_id: str, order: ProposedOrder) -> OrderRequest:
        """The IB order of a proposed order: a stock market-on-open, a forex pair on IDEALPRO in
        IB's direction (market, good till cancelled), a future market for the day."""
        pair = fx_pair(order.symbol)
        if pair is not None:
            if not pair.usd_quoted:
                raise AdapterError(f"{order.symbol}: a forex symbol is traded from its XXXUSD series only")
            if not (order.price > 0 and math.isfinite(order.price)):
                raise AdapterError(f"{order.symbol}: no reference price to size the {pair.ib_pair} order")
            q = round(pair.ib_quantity(float(order.quantity), float(order.price)))
            return OrderRequest(client_id, pair.ib_pair, float(q), "MKT", tif="GTC")
        if order.symbol in self.adapter.instruments:
            return OrderRequest(client_id, order.symbol, float(order.quantity), "MKT", tif="DAY")
        return OrderRequest(client_id, order.symbol, float(order.quantity), "MKT", tif="OPG")

    def shortable(self, symbols: list[str], wait: float = 4.0) -> dict[str, float]:
        """IB's shortable level of each stock (tick 236, delayed data allowed): above 2.5 easy
        to borrow, 1.5-2.5 hard (a locate), 1.5 or less none. Read-only."""
        return self.adapter.shortable(symbols, wait=wait)

    def place(self, orders: list[ProposedOrder], date: str) -> list[dict]:
        if not self.send:
            raise AdapterError("this account was opened read-only (propose mode): nothing is sent")
        indices = sorted({o.symbol for o in orders if is_index(o.symbol)})
        if indices:
            raise AdapterError(f"orders on indices {indices}: an index is not tradable (substitute it in the "
                               f"meta-portfolio); nothing is sent")
        stamp = date.replace("-", "")
        requests = {}
        for i, o in enumerate(orders, 1):
            cid = f"twd-{stamp}-{i:04d}"
            requests[cid] = (o, self.request(cid, o))      # every order is mapped before any is sent
        ids, skipped = {}, []
        for cid, (o, req) in requests.items():
            if not req.quantity:                           # a forex order under one unit of the base
                skipped.append({"id": cid, "symbol": o.symbol, "quantity": o.quantity, "status": "skipped",
                                "reason": f"under one unit of {req.symbol}"})
                continue
            ids[cid] = o
            self.adapter.place(req)
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
                 "reason": reasons.get(cid, "")} for cid, o in ids.items()] + skipped

    def close(self) -> None:
        self.adapter.disconnect()


def _previous_weekday(day: _dt.date) -> _dt.date:
    day -= _dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= _dt.timedelta(days=1)
    return day


class KrakenAccount:
    """A Kraken spot account (spec/11 § 8), through ``KrakenAdapter``.

    The books are in USD and in base units (``BTC`` 0.25); the account holds its cash in
    ``quote`` (USD or EUR) and trades the pairs ``<base>/<quote>``. Positions come back under
    the books' symbols (``BTC``, or ``BTCUSD`` when that is the name the books use).

    ``send=False`` (propose): reads the account, never calls ``AddOrder``. ``send=True``:
    market orders, sells first, each checked locally (pair whitelist ``allowed``,
    ``max_order_notional`` in USD, minimums, balances) and then sent to ``AddOrder`` **with
    validate=true** unless ``live=True`` and ``TRADEWIND_KRAKEN_LIVE=1``: the status is then
    ``validated`` (Kraken checked it, nothing traded) instead of ``sent``. An order under the
    pair's lot or minimum is ``skipped``; one a guard refuses (whitelist, notional, balance,
    no short, no pair) is ``refused``; one Kraken refuses is ``rejected``. The key is read from
    ``KRAKEN_API_KEY`` / ``KRAKEN_API_SECRET`` only.
    """

    kind = "kraken"

    def __init__(self, *, quote: str = "USD", send: bool = False, live: bool = False,
                 allowed: list[str] | None = None, max_order_notional: float | None = None,
                 taker_fee: float = DEFAULT_TAKER_FEE, base_fx: float | None = None,
                 adapter: KrakenAdapter | None = None, environ=None):
        self.send = bool(send)
        self.quote = quote.upper()
        self.base_fx = base_fx
        self.fx_rate: float | None = 1.0 if self.quote == "USD" else None
        self.taker_fee = float(taker_fee)
        self.max_order_notional = max_order_notional
        self.allowed = None if allowed is None else {a.upper() for a in allowed}
        limits = RiskLimits(allow_short=False)
        self.adapter = adapter or KrakenAdapter(quote=self.quote, live=live and send, limits=limits,
                                                taker_fee=taker_fee, environ=environ)
        self.adapter.connect()
        self.account = "KRAKEN"
        self._fills: list[Fill] = []

    @property
    def mode(self) -> str:
        return self.adapter.mode if self.send else "propose (read-only)"

    # -- the account's currency -----------------------------------------------------------------------
    def currency_info(self) -> dict:
        """The quote currency is the account's; Kraken publishes no rate to the books' USD
        here (the runner takes PWB's, or ``--base-fx-rate``)."""
        return {"currency": self.quote, "published": 1.0 if self.quote == "USD" else None, "fallback": self.base_fx}

    def set_fx(self, rate: float) -> None:
        """USD per unit of the quote currency."""
        self.fx_rate = float(rate)

    def _rate(self) -> float:
        if self.fx_rate is None:
            raise AdapterError(f"the Kraken account is in {self.quote}: set the day's rate first (set_fx)")
        return self.fx_rate

    def _book_name(self, base: str, known) -> str:
        name = catalogue_base(base)
        for candidate in (name, name + "USD", base, base + "USD"):
            if candidate in known:
                return candidate
        return name

    # -- the four calls -------------------------------------------------------------------------------
    def settle(self, date: str, opens: dict[str, float], closes: dict[str, float]) -> list[Fill]:
        """The trades since the day after the previous weekday session (00:00 UTC): the
        orders placed after its close execute in the first minutes of the next UTC day,
        weekends included. Prices and fees in USD (``fx_rate``)."""
        rate = self._rate()
        day = _dt.date.fromisoformat(date[:10])
        start = _previous_weekday(day) + _dt.timedelta(days=1)
        since = int(_dt.datetime(start.year, start.month, start.day, tzinfo=_dt.timezone.utc).timestamp())
        res = self.adapter.rest.private("TradesHistory", {"start": since}) or {}
        known = set(opens) | set(closes)
        fills = []
        for tid, tr in sorted((res.get("trades") or {}).items(), key=lambda kv: float(kv[1].get("time", 0))):
            pair = self.adapter._pair_of(str(tr.get("pair", "")))
            if pair is None or pair.quote_name != self.quote:
                continue
            sym = self._book_name(pair.base_name, known)
            q = float(tr.get("vol", 0.0)) * (1.0 if tr.get("type") == "buy" else -1.0)
            px = float(tr.get("price", 0.0)) * rate
            fills.append(Fill(sym, q, px, float(opens.get(sym, px)), float(tr.get("fee", 0.0)) * rate,
                              str(tr.get("ordertxid", tid))))
        self._fills = fills
        return fills

    def snapshot(self, closes: dict[str, float] | None = None) -> Snapshot:
        """Cash (the quote balance, in USD), positions in the books' symbols and base units,
        the NAV at the books' closes (at Kraken's last trade for an asset no book prices)."""
        rate = self._rate()
        closes = closes or {}
        ad = self.adapter
        bal = ad.balances()
        quote_asset = ad.pairs.quote_asset(self.quote)
        cash_quote = bal.get(quote_asset, (0.0, 0.0))[0]
        positions: dict[str, float] = {}
        unpriced = []
        for asset, (q, _avail) in sorted(bal.items()):
            if asset == quote_asset or not q or asset.endswith(NON_TRADABLE_SUFFIXES):
                continue
            pair = ad.pairs.by_base_asset(asset, self.quote) or ad.pairs.by_base_asset(asset, "USD")
            if pair is None:
                continue                                    # another fiat or an asset without a pair
            name = self._book_name(pair.base_name, closes)
            positions[name] = positions.get(name, 0.0) + q
            if name not in closes:
                unpriced.append((name, pair))
        last = ad.last_prices([p for _n, p in unpriced])
        marks = dict(closes)
        for name, pair in unpriced:
            if pair.key in last:                            # a USD pair's price is already in USD
                marks[name] = last[pair.key] * (1.0 if pair.quote_name == "USD" else rate)
        cash = cash_quote * rate
        nav = cash + sum(q * marks.get(s, 0.0) for s, q in positions.items())
        working = []
        for o in ((ad.rest.private("OpenOrders") or {}).get("open") or {}).values():
            d = o.get("descr") or {}
            pair = ad._pair_of(str(d.get("pair", "")))
            name = self._book_name(pair.base_name, closes) if pair else str(d.get("pair", ""))
            sign = 1.0 if d.get("type") == "buy" else -1.0
            working.append((name, sign * (float(o.get("vol", 0.0)) - float(o.get("vol_exec", 0.0)))))
        return Snapshot(cash=cash, nav=nav, positions=positions, fills=list(self._fills), open_orders=working,
                        account=self.account, currency="USD", base_currency=self.quote, fx_rate=rate,
                        nav_base=nav / rate, base_cash=cash_quote if self.quote != "USD" else 0.0)

    def place(self, orders: list[ProposedOrder], date: str) -> list[dict]:
        if not self.send:
            raise AdapterError("this account was opened read-only (propose mode): nothing is sent")
        rate = self._rate()
        ad = self.adapter
        bal = ad.balances()
        quote_asset = ad.pairs.quote_asset(self.quote)
        quote_left = bal.get(quote_asset, (0.0, 0.0))[1]
        stamp = date.replace("-", "")
        out = []
        # sells first: their proceeds (less the fee) fund the buys that follow
        ordered = sorted(enumerate(orders, 1), key=lambda io: (io[1].quantity > 0, io[0]))
        for i, o in ordered:
            cid = f"twk-{stamp}-{i:04d}"
            row = {"id": cid, "symbol": o.symbol, "quantity": o.quantity, "status": "", "reason": ""}
            out.append(row)
            reason = ""
            if self.allowed is not None and o.symbol.upper() not in self.allowed:
                reason = f"{o.symbol} is not in the allowed pairs"
            elif self.max_order_notional is not None and o.notional > self.max_order_notional:
                reason = f"notional {o.notional:,.2f} USD over max_order_notional {self.max_order_notional:,.2f}"
            elif not (o.price > 0 and math.isfinite(o.price)):
                reason = "no reference price"
            if reason:
                row.update(status="refused", reason=reason)
                continue
            try:
                pair = ad.register(o.symbol)
            except AdapterError as exc:
                row.update(status="refused", reason=str(exc))
                continue
            price_quote = o.price / rate
            base_left = bal.get(pair.base, (0.0, 0.0))[1]
            r = ad.submit(cid, o.symbol, o.quantity, reference_price=price_quote, check_balance=True,
                          available=(base_left, quote_left))
            if r["status"] == "refused":
                # under the pair's lot or minimum: skipped (dust, no alert); any other guard:
                # refused (the session raises orders_rejected)
                dust = r["reason"].startswith("under one lot") or "'s ordermin" in r["reason"] \
                    or "'s costmin" in r["reason"]
                row.update(status="skipped" if dust else "refused", reason=r["reason"])
                continue
            vol = r["volume"]
            if o.quantity > 0:
                cost = vol * price_quote * (1.0 + self.taker_fee)
                if r["status"] in ("sent", "validated"):
                    quote_left -= cost
            else:
                quote_left += vol * price_quote * (1.0 - self.taker_fee)
                b, a = bal.get(pair.base, (0.0, 0.0))
                bal[pair.base] = (b, a - vol)
            row.update(status=r["status"], reason=r["reason"], volume=vol, pair=r["pair"], txid=r["txid"])
        by_id = {r["id"]: r for r in out}
        return [by_id[f"twk-{stamp}-{i:04d}"] for i in range(1, len(orders) + 1)]

    def estimated_commission(self, order: ProposedOrder) -> float:
        """The taker fee of a proposed order, in USD."""
        return order.notional * self.taker_fee

    def close(self) -> None:
        self.adapter.disconnect()


__all__ = ["Fill", "IBPaperAccount", "KrakenAccount", "SimAccount", "Snapshot", "estimated_commission",
           "ibkr_tiered_commission"]
