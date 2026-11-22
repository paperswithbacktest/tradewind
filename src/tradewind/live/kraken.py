"""Kraken adapter (spec/11): ``KrakenREST``, ``KrakenAdapter`` and ``KrakenBars``, crypto spot.

Written from Kraken's public API documentation (docs.kraken.com: the REST authentication
guide and its worked signature example, the rate-limit guide, ``AssetPairs``, ``OHLC``,
``Ticker``, ``BalanceEx``, ``AddOrder``, ``CancelOrder``, ``OpenOrders``, ``QueryOrders``,
``QueryTrades``, ``TradesHistory``). No client library: REST over the standard library, so
nothing is installed for it.

**Validation mode by default.** Every ``AddOrder`` carries ``validate=true`` -- Kraken checks
the order and does not trade it -- unless the adapter is built with ``live=True`` **and** the
environment holds ``TRADEWIND_KRAKEN_LIVE=1``. A validated order comes back to the engine as
``Rejected("validated by Kraken (validate=true), not sent: ...")``.

**Secrets.** The API key and secret are read only from ``KRAKEN_API_KEY`` and
``KRAKEN_API_SECRET`` (``Credentials.from_env``), never from a file, never written or
printed: ``repr`` is redacted and every error message is scrubbed of both strings.

Threading: none. ``idle`` sleeps; ``drain`` polls Kraken for the orders still working (at
most every ``poll_every`` seconds), so every answer lands on the engine's thread between two
slices, as spec/07 § 2 requires.
"""

from __future__ import annotations

import base64
import binascii
import collections
import datetime as _dt
import hashlib
import hmac
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from ..comminfo import CommInfoBase
from .adapter import AdapterError, BarRecord
from .guards import RiskLimits
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

API_URL = "https://api.kraken.com"
KEY_ENV = "KRAKEN_API_KEY"
SECRET_ENV = "KRAKEN_API_SECRET"
LIVE_ENV = "TRADEWIND_KRAKEN_LIVE"

#: the catalogue's names of the assets Kraken names otherwise
ALIASES = {"BTC": "XBT", "DOGE": "XDG"}
UNALIASES = {v: k for k, v in ALIASES.items()}
#: balance suffixes of staked, rewards and yield balances: read-only, never a position
NON_TRADABLE_SUFFIXES = (".S", ".F", ".M", ".B", ".T", ".P")

#: Kraken's REST counter per verification tier: (maximum, decay per second)
TIERS = {"starter": (15.0, 0.33), "intermediate": (20.0, 0.5), "pro": (20.0, 1.0)}
#: calls that add 2 to the counter (ledger and trade history)
HEAVY_CALLS = frozenset({"TradesHistory", "QueryTrades", "Ledgers", "QueryLedgers"})
#: calls the REST counter does not count (the matching engine's limiter does)
ORDER_CALLS = frozenset({"AddOrder", "AddOrderBatch", "CancelOrder", "CancelOrderBatch", "CancelAll",
                         "EditOrder", "AmendOrder"})
#: errors after which a read call is tried again
RETRYABLE = ("EAPI:Rate limit exceeded", "EService:Throttled", "EService:Unavailable", "EService:Busy",
             "EGeneral:Temporary lockout")

DEFAULT_TAKER_FEE = 0.004            # the entry tier; AssetPairs' fee tables are deprecated
DAY_NS = 86_400 * 1_000_000_000


# --------------------------------------------------------------------------------------------- #
# Sizing                                                                                        #
# --------------------------------------------------------------------------------------------- #
class FractionalCommInfo(CommInfoBase):
    """A commission scheme for crypto spot: fractional sizes and a percentage fee.

    The default stock scheme sizes ``order_target_percent`` / ``order_target_value`` in whole
    units (0 or 1 BTC); this one sizes to ``decimals`` places (8: a satoshi), toward zero, and
    charges ``commission`` of the traded value (0.004: Kraken's entry taker fee, to replace by
    the account's own). The adapter then cuts each order to the pair's ``lot_decimals``.
    Install it on the broker: ``engine.broker.addcommissioninfo(FractionalCommInfo())``."""

    params = (("stocklike", True), ("commtype", CommInfoBase.COMM_PERC), ("commission", DEFAULT_TAKER_FEE),
              ("percabs", True), ("decimals", 8))

    def getsize(self, price, cash):
        scale = 10 ** int(self.p.decimals)
        return math.floor(self.p.leverage * cash / price * scale) / scale


# --------------------------------------------------------------------------------------------- #
# Authentication                                                                                #
# --------------------------------------------------------------------------------------------- #
def sign(urlpath: str, postdata: str, nonce, secret: str) -> str:
    """``API-Sign``: HMAC-SHA512 of ``urlpath + SHA256(nonce + postdata)`` keyed with the
    base64-decoded secret, base64-encoded (Kraken's REST authentication guide)."""
    digest = hashlib.sha256((str(nonce) + postdata).encode()).digest()
    mac = hmac.new(base64.b64decode(secret), urlpath.encode() + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class Credentials:
    """The API key pair. Read from the environment only; ``repr`` never shows it."""

    __slots__ = ("_key", "_secret")

    def __init__(self, key: str, secret: str):
        if not key or not secret:
            raise AdapterError(f"Kraken credentials missing: set {KEY_ENV} and {SECRET_ENV} in the environment")
        try:
            base64.b64decode(secret, validate=True)
        except (binascii.Error, ValueError):
            raise AdapterError(f"{SECRET_ENV} is not a base64 string (copy the private key as Kraken shows it)"
                               ) from None
        self._key = key
        self._secret = secret

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Credentials:
        env = os.environ if environ is None else environ
        return cls(env.get(KEY_ENV, ""), env.get(SECRET_ENV, ""))

    @property
    def key(self) -> str:
        return self._key

    def sign(self, urlpath: str, postdata: str, nonce) -> str:
        return sign(urlpath, postdata, nonce, self._secret)

    def scrub(self, text: str) -> str:
        """``text`` without the key or the secret (an error message, a URL)."""
        for s in (self._key, self._secret):
            if s:
                text = text.replace(s, "***")
        return text

    def __repr__(self) -> str:
        return "Credentials(key=***, secret=***)"

    __str__ = __repr__

    def __reduce__(self):                      # never pickled with the secret
        raise TypeError("Kraken credentials are not serialisable")


class NonceSource:
    """Millisecond Unix timestamps, strictly increasing within the process."""

    def __init__(self, clock: Callable[[], int] = time.time_ns):
        self._clock = clock
        self._last = 0

    def next(self) -> int:
        n = int(self._clock()) // 1_000_000
        if n <= self._last:
            n = self._last + 1
        self._last = n
        return n


class CallCounter:
    """Kraken's REST counter, kept on our side: each call adds its cost, the level decays by
    ``decay`` a second, and a call that would take it over ``maximum`` waits."""

    def __init__(self, maximum: float, decay: float, now=time.monotonic, sleep=time.sleep):
        self.maximum, self.decay = float(maximum), float(decay)
        self._now, self._sleep = now, sleep
        self.level = 0.0
        self._t = now()

    def _decay(self) -> None:
        t = self._now()
        self.level = max(0.0, self.level - self.decay * (t - self._t))
        self._t = t

    def acquire(self, cost: float = 1.0) -> float:
        self._decay()
        waited = 0.0
        over = self.level + cost - self.maximum
        if over > 0:
            waited = over / self.decay
            self._sleep(waited)
            self._decay()
        self.level += cost
        return waited


# --------------------------------------------------------------------------------------------- #
# REST                                                                                          #
# --------------------------------------------------------------------------------------------- #
class KrakenError(AdapterError):
    """Kraken answered with errors (``errors``: Kraken's strings, e.g. ``EOrder:Insufficient funds``)."""

    def __init__(self, errors, method: str = ""):
        self.errors = [str(e) for e in (errors if isinstance(errors, (list, tuple)) else [errors])]
        self.method = method
        super().__init__(f"{method + ': ' if method else ''}{', '.join(self.errors)}")

    def retryable(self) -> bool:
        return any(e.startswith(RETRYABLE) for e in self.errors)


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, str]:
    """The default transport: one HTTPS request, ``(status, text)``."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _form(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class KrakenREST:
    """Kraken's spot REST API: ``public(method, params)`` (no key) and ``private(method, data)``
    (signed). ``transport(method, url, headers, body, timeout) -> (status, text)`` is
    replaceable (tests answer from recorded documentation examples).

    ``tier``: the account's verification tier, for the REST counter (``TIERS``).
    ``public_interval``: seconds between two public calls (1 by default).
    ``retries``: read calls answered with a ``RETRYABLE`` error or a network error are tried
    again after ``backoff`` seconds; an order call is never retried (a duplicate would be a
    second order)."""

    def __init__(self, credentials: Credentials | None = None, *, base_url: str = API_URL,
                 transport: Callable | None = None, timeout: float = 30.0, tier: str = "starter",
                 public_interval: float = 1.0, retries: int = 3, backoff: tuple = (2.0, 5.0, 15.0),
                 now=time.monotonic, sleep=time.sleep, nonce: NonceSource | None = None):
        if tier not in TIERS:
            raise ValueError(f"tier is one of {sorted(TIERS)}")
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.transport = transport or urllib_transport
        self.timeout = float(timeout)
        self.counter = CallCounter(*TIERS[tier], now=now, sleep=sleep)
        self.public_interval = float(public_interval)
        self.retries = int(retries)
        self.backoff = tuple(backoff)
        self._now, self._sleep = now, sleep
        self._last_public = -math.inf
        self.nonce = nonce or NonceSource()
        self.calls: collections.Counter = collections.Counter()

    def _scrub(self, text: str) -> str:
        return self.credentials.scrub(text) if self.credentials is not None else text

    def _decode(self, method: str, status: int, text: str):
        try:
            payload = json.loads(text)
        except ValueError:
            raise KrakenError([f"HTTP {status}: not JSON: {self._scrub(text[:200])}"], method) from None
        errors = payload.get("error") or []
        if errors:
            raise KrakenError([self._scrub(str(e)) for e in errors], method)
        if status >= 400:
            raise KrakenError([f"HTTP {status}"], method)
        return payload.get("result")

    def _call(self, method: str, once: Callable, retry: bool):
        attempt = 0
        while True:
            try:
                return once()
            except KrakenError as exc:
                if not retry or not exc.retryable() or attempt >= self.retries:
                    raise
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                if not retry or attempt >= self.retries:
                    raise KrakenError([self._scrub(f"network: {type(exc).__name__}: {exc}")], method) from None
            self._sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
            attempt += 1

    def public(self, method: str, params: Mapping | None = None):
        """``GET /0/public/<method>``; the ``result`` of the answer."""
        query = urllib.parse.urlencode({k: _form(v) for k, v in (params or {}).items()})
        url = f"{self.base_url}/0/public/{method}" + (f"?{query}" if query else "")

        def once():
            wait = self._last_public + self.public_interval - self._now()
            if wait > 0:
                self._sleep(wait)
            self._last_public = self._now()
            self.calls[method] += 1
            status, text = self.transport("GET", url, {"User-Agent": "tradewind"}, None, self.timeout)
            return self._decode(method, status, text)

        return self._call(method, once, retry=True)

    def private(self, method: str, data: Mapping | None = None):
        """``POST /0/private/<method>``, signed; the ``result`` of the answer."""
        if self.credentials is None:
            raise AdapterError(f"Kraken credentials missing: set {KEY_ENV} and {SECRET_ENV} in the environment")
        path = f"/0/private/{method}"
        order_call = method in ORDER_CALLS

        def once():
            if not order_call:
                self.counter.acquire(2.0 if method in HEAVY_CALLS else 1.0)
            fields = {"nonce": self.nonce.next()}
            fields.update({k: v for k, v in (data or {}).items() if v is not None})
            postdata = urllib.parse.urlencode({k: _form(v) for k, v in fields.items()})
            headers = {"API-Key": self.credentials.key,
                       "API-Sign": self.credentials.sign(path, postdata, fields["nonce"]),
                       "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                       "User-Agent": "tradewind"}
            self.calls[method] += 1
            status, text = self.transport("POST", self.base_url + path, headers, postdata.encode(), self.timeout)
            return self._decode(method, status, text)

        return self._call(method, once, retry=not order_call)


# --------------------------------------------------------------------------------------------- #
# Pairs                                                                                         #
# --------------------------------------------------------------------------------------------- #
def _dec(x, default: str = "0") -> Decimal:
    return Decimal(str(x if x not in (None, "") else default))


@dataclass(frozen=True)
class PairInfo:
    """One ``AssetPairs`` entry: what an order on it must respect."""

    key: str                    # XXBTZUSD
    altname: str                # XBTUSD
    wsname: str                 # XBT/USD
    base: str                   # asset id, XXBT
    quote: str                  # asset id, ZUSD
    lot_decimals: int = 8
    pair_decimals: int = 1
    cost_decimals: int = 5
    ordermin: float = 0.0
    costmin: float = 0.0
    tick_size: float = 0.0
    status: str = "online"

    @property
    def base_name(self) -> str:
        return self.wsname.split("/")[0] if "/" in self.wsname else self.altname

    @property
    def quote_name(self) -> str:
        return self.wsname.split("/")[1] if "/" in self.wsname else ""

    @classmethod
    def parse(cls, key: str, d: Mapping) -> PairInfo:
        return cls(key, str(d.get("altname", key)), str(d.get("wsname", "")), str(d.get("base", "")),
                   str(d.get("quote", "")), int(d.get("lot_decimals", 8)), int(d.get("pair_decimals", 1)),
                   int(d.get("cost_decimals", 5)), float(_dec(d.get("ordermin"))), float(_dec(d.get("costmin"))),
                   float(_dec(d.get("tick_size"))), str(d.get("status", "online")))

    def round_volume(self, quantity: float) -> float:
        """``quantity`` cut toward zero to ``lot_decimals`` (sign kept)."""
        step = Decimal(1).scaleb(-self.lot_decimals)
        v = _dec(abs(quantity)).quantize(step, rounding=ROUND_DOWN)
        return math.copysign(float(v), quantity) if v else 0.0

    def fmt_volume(self, quantity: float) -> str:
        return f"{abs(quantity):.{self.lot_decimals}f}"

    def fmt_price(self, price: float) -> str:
        p = _dec(price)
        if self.tick_size > 0:
            tick = _dec(self.tick_size)
            p = (p / tick).quantize(Decimal(1)) * tick
        return f"{float(p):.{self.pair_decimals}f}"

    def minimum_refusal(self, volume: float, price: float | None) -> str:
        v = abs(volume)
        if not v:
            return f"under one lot of {self.altname} ({self.lot_decimals} decimals)"
        if self.ordermin and v < self.ordermin - 1e-15:
            return f"volume {v:g} under {self.altname}'s ordermin {self.ordermin:g}"
        if self.costmin and price is not None and math.isfinite(price) and v * abs(price) < self.costmin:
            return f"cost {v * abs(price):.2f} under {self.altname}'s costmin {self.costmin:g}"
        return ""


def catalogue_base(symbol: str) -> str:
    """The catalogue's name of a Kraken base asset (``XBT`` -> ``BTC``)."""
    return UNALIASES.get(symbol, symbol)


class PairTable:
    """The ``AssetPairs`` of the account's region, searchable by catalogue symbol."""

    def __init__(self, pairs: Mapping[str, PairInfo]):
        self.pairs = dict(pairs)
        self._ws = {p.wsname: p for p in self.pairs.values() if p.wsname}
        self._alt = {p.altname: p for p in self.pairs.values()}

    @classmethod
    def from_result(cls, result: Mapping) -> PairTable:
        return cls({k: PairInfo.parse(k, v) for k, v in (result or {}).items() if isinstance(v, Mapping)})

    def _lookup(self, base: str, quote: str) -> PairInfo | None:
        for b in dict.fromkeys((ALIASES.get(base, base), base)):
            p = self._ws.get(f"{b}/{quote}") or self._alt.get(f"{b}{quote}")
            if p is not None:
                return p
        return None

    def resolve(self, symbol: str, quote: str = "USD") -> PairInfo:
        """The pair that trades ``symbol`` (``BTC``, ``BTCUSD``, ``BTC/USD``) against ``quote``."""
        s = symbol.upper().replace("/", "").replace("-", "")
        found = self._lookup(s, quote)
        if found is None:
            for suffix in dict.fromkeys((quote, "USD")):
                if s.endswith(suffix) and len(s) > len(suffix):
                    found = self._lookup(s[: -len(suffix)], quote)
                    if found is not None:
                        break
        if found is None:
            raise AdapterError(f"Kraken has no {quote} pair for {symbol!r}")
        return found

    def by_base_asset(self, asset: str, quote: str) -> PairInfo | None:
        """The pair of base asset id ``asset`` (``XXBT``) against ``quote`` (``USD``)."""
        for p in self.pairs.values():
            if p.base == asset and p.quote_name == quote:
                return p
        return None

    def quote_asset(self, quote: str) -> str:
        """The asset id of ``quote`` (``USD`` -> ``ZUSD``)."""
        for p in self.pairs.values():
            if p.quote_name == quote:
                return p.quote
        raise AdapterError(f"no Kraken pair is quoted in {quote}")


# --------------------------------------------------------------------------------------------- #
# Orders                                                                                        #
# --------------------------------------------------------------------------------------------- #
def cl_ord_id(client_id: str) -> str:
    """Kraken's client order id for a Tradewind ``client_id``: free text of 18 ASCII
    characters, deterministic (a replay sends the same), unique in practice."""
    return "tw" + hashlib.sha256(client_id.encode()).hexdigest()[:16]


def next_midnight_utc(now_ns: int) -> int:
    """Seconds since the epoch of the first 00:00 UTC after ``now_ns``."""
    return (int(now_ns) // DAY_NS + 1) * 86_400


def order_fields(req: OrderRequest, pair: PairInfo, *, cl_ord: str, validate: bool, now_ns: int) -> dict:
    """``AddOrder``'s fields for a generic request (spec/11 § 4). Raises ``ValueError`` for
    an order type Kraken spot has no counterpart for."""
    if req.order_type not in ("MKT", "LMT", "STP", "STP LMT"):
        raise ValueError(f"{req.order_type} orders have no Kraken counterpart (no closing auction)")
    volume = pair.round_volume(abs(float(req.quantity)))
    fields = {"pair": pair.altname, "type": "buy" if req.quantity > 0 else "sell",
              "ordertype": {"MKT": "market", "LMT": "limit", "STP": "stop-loss",
                            "STP LMT": "stop-loss-limit"}[req.order_type],
              "volume": pair.fmt_volume(volume), "oflags": "fciq", "cl_ord_id": cl_ord}
    if req.order_type == "LMT":
        fields["price"] = pair.fmt_price(float(req.limit_price))
    elif req.order_type == "STP":
        fields["price"] = pair.fmt_price(float(req.stop_price))
    elif req.order_type == "STP LMT":
        fields["price"] = pair.fmt_price(float(req.stop_price))
        fields["price2"] = pair.fmt_price(float(req.limit_price))
    if req.order_type != "MKT":
        if req.tif == "GTD" and req.good_till:
            fields.update(timeinforce="GTD", expiretm=str(int(req.good_till) // 1_000_000_000))
        elif req.tif == "DAY":
            fields.update(timeinforce="GTD", expiretm=str(next_midnight_utc(now_ns)))
        else:
            fields["timeinforce"] = "GTC"
    if validate:
        fields["validate"] = True
    return fields


def balance_refusal(side: str, volume: float, price: float | None, pair: PairInfo, base_available: float,
                    quote_available: float, taker_fee: float) -> str:
    """Why the account cannot cover the order, or ``""``: a buy's cost plus the taker fee
    against the available quote balance, a sell against the available base balance."""
    v = abs(volume)
    if side == "sell":
        if v > base_available + 1e-12:
            return f"sell of {v:g} {pair.base_name} over the {base_available:g} available (spot: no short)"
        return ""
    if price is None or not math.isfinite(price):
        return f"no reference price to check the {pair.quote_name} balance"
    cost = v * price * (1.0 + taker_fee)
    if cost > quote_available + 1e-9:
        return f"buy of {cost:,.2f} {pair.quote_name} (fee included) over the {quote_available:,.2f} available"
    return ""


@dataclass
class _Working:
    client_id: str
    txid: str
    symbol: str
    pair: str
    sign: float
    requested: float            # |quantity| the engine asked for
    volume: float               # |quantity| sent (rounded to the lot)
    cancel_asked: bool = False


def _armed(live: bool, environ: Mapping[str, str] | None) -> bool:
    env = os.environ if environ is None else environ
    return bool(live) and env.get(LIVE_ENV, "") == "1"


class KrakenAdapter:
    """A ``BrokerAdapter`` on a Kraken spot account (spec/11 § 2).

    ``quote``: the session's quote currency (``"USD"`` or ``"EUR"``); cash is its balance.
    ``live``: with ``TRADEWIND_KRAKEN_LIVE=1`` in the environment, orders trade; otherwise
    every ``AddOrder`` is sent with ``validate=true`` (the default). ``limits``: the
    ``RiskLimits`` checked here too (shorting is always refused: spot). ``taker_fee``: for
    the balance check. ``symbols``: the names the strategy trades, so positions come back
    under them (default: the catalogue's bare base, ``BTC``). ``state_path``: a JSON file
    keeping our ``cl_ord_id`` -> ``client_id`` table for a restart (no secret in it).
    ``since``: ns from which a full sync lists the executions (default: a week before
    ``connect``).
    """

    def __init__(self, rest: KrakenREST | None = None, *, quote: str = "USD", live: bool = False,
                 limits: RiskLimits | None = None, taker_fee: float = DEFAULT_TAKER_FEE, symbols=(),
                 poll_every: float = 10.0, state_path: str | None = None, since: int | None = None,
                 environ: Mapping[str, str] | None = None, clock_ns: Callable[[], int] = time.time_ns,
                 monotonic=time.monotonic, sleep=time.sleep):
        self.rest = rest
        self.quote = quote.upper()
        self.live_requested = bool(live)
        self.environ = environ
        self.limits = limits or RiskLimits()
        self.taker_fee = float(taker_fee)
        self.poll_every = float(poll_every)
        self.state_path = state_path
        self.since = since
        self._clock_ns, self._monotonic, self._sleep = clock_ns, monotonic, sleep
        self.pairs: PairTable | None = None
        self._names: dict[str, str] = {}               # pair key -> the strategy's symbol
        self._wanted_symbols = list(symbols)
        self._queue: collections.deque = collections.deque()
        self._connected = False
        self._working: dict[str, _Working] = {}        # client_id -> order working at Kraken
        self._client: dict[str, str] = {}              # cl_ord_id -> client_id
        self._reported: set[str] = set()
        self._seen_trades: set[str] = set()
        self._last_poll = -math.inf
        self.validated: list[dict] = []                # what validation mode checked
        self.ignored_balances: dict[str, float] = {}   # staked / rewards balances
        self._load_state()

    # -- mode --------------------------------------------------------------------------------
    @property
    def armed(self) -> bool:
        """True only with ``live=True`` and ``TRADEWIND_KRAKEN_LIVE=1``: orders then trade."""
        return _armed(self.live_requested, self.environ)

    @property
    def mode(self) -> str:
        return "LIVE" if self.armed else "validation (validate=true)"

    # -- state file (no secret) --------------------------------------------------------------
    def _load_state(self) -> None:
        if self.state_path and os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as fh:
                self._client.update(json.load(fh).get("cl_ord_id", {}))

    def _save_state(self) -> None:
        if not self.state_path:
            return
        directory = os.path.dirname(os.path.abspath(self.state_path))
        os.makedirs(directory, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cl_ord_id": self._client}, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    # -- connection --------------------------------------------------------------------------
    def connect(self) -> None:
        if self.rest is None:
            self.rest = KrakenREST(Credentials.from_env(self.environ))
        if self.rest.credentials is None:
            self.rest.credentials = Credentials.from_env(self.environ)
        self.pairs = PairTable.from_result(self.rest.public("AssetPairs"))
        self.pairs.quote_asset(self.quote)               # the quote currency exists
        self.balances()                                  # the key works (Query Funds)
        for s in self._wanted_symbols:
            self.register(s)
        if self.since is None:
            self.since = self.now() - 7 * DAY_NS
        self._connected = True
        if self.live_requested and not self.armed:
            self._queue.append(Notice(f"live=True but {LIVE_ENV} is not 1: validation mode, nothing trades",
                                      0, "", self.now()))
        self._queue.append(Notice(f"Kraken {self.quote} spot, mode {self.mode}", 0, "", self.now()))

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def now(self) -> int:
        return int(self._clock_ns())

    def bar_source(self) -> KrakenBars:
        """Kraken's public daily bars, on a client of their own that carries no key."""
        r = self.rest
        public = None if r is None else KrakenREST(transport=r.transport, base_url=r.base_url, timeout=r.timeout,
                                                   public_interval=r.public_interval, now=r._now, sleep=r._sleep)
        return KrakenBars(public, quote=self.quote, pairs=self.pairs)

    def register(self, symbol: str) -> PairInfo:
        """The pair of ``symbol``; positions on it are reported under ``symbol``."""
        pair = self.pairs.resolve(symbol, self.quote)
        self._names.setdefault(pair.key, symbol)
        return pair

    def name_of(self, pair: PairInfo) -> str:
        return self._names.get(pair.key) or catalogue_base(pair.base_name)

    # -- account -----------------------------------------------------------------------------
    def balances(self) -> dict[str, tuple[float, float]]:
        """``{asset id: (balance, available)}`` (``BalanceEx``; available = balance + credit -
        credit_used - hold_trade)."""
        out = {}
        for asset, v in (self.rest.private("BalanceEx") or {}).items():
            if isinstance(v, Mapping):
                bal = float(_dec(v.get("balance")))
                avail = bal + float(_dec(v.get("credit"))) - float(_dec(v.get("credit_used"))) \
                    - float(_dec(v.get("hold_trade")))
            else:                                      # Balance's plain form
                bal = avail = float(_dec(v))
            out[asset] = (bal, avail)
        return out

    def last_prices(self, pairs: list[PairInfo]) -> dict[str, float]:
        """The last trade price of each pair (public ``Ticker``), by pair key."""
        if not pairs:
            return {}
        res = self.rest.public("Ticker", {"pair": ",".join(p.altname for p in pairs)}) or {}
        out = {}
        for p in pairs:
            t = res.get(p.key) or res.get(p.altname) or res.get(p.wsname)
            if t and t.get("c"):
                out[p.key] = float(t["c"][0])
        return out

    def _available(self, pair: PairInfo) -> tuple[float, float]:
        bal = self.balances()
        return bal.get(pair.base, (0.0, 0.0))[1], bal.get(pair.quote, (0.0, 0.0))[1]

    # -- orders ------------------------------------------------------------------------------
    def submit(self, client_id: str, symbol: str, quantity: float, *, order_type: str = "MKT",
               limit_price: float | None = None, stop_price: float | None = None, tif: str = "GTC",
               good_till: int | None = None, reference_price: float | None = None,
               check_balance: bool = True, available: tuple[float, float] | None = None) -> dict:
        """Check and send one order. Returns ``{status, reason, txid, volume, pair, descr}``,
        ``status`` one of ``sent`` (armed, Kraken took it), ``validated`` (validation mode,
        Kraken checked it), ``rejected`` (Kraken refused it), ``refused`` (a local guard:
        nothing was sent). Never raises for a refusal."""
        out = {"status": "refused", "reason": "", "txid": "", "volume": 0.0, "pair": "", "descr": ""}
        try:
            pair = self.register(symbol)
        except AdapterError as exc:
            out["reason"] = str(exc)
            return out
        out["pair"] = pair.altname
        if pair.status != "online":
            out["reason"] = f"{pair.altname} is {pair.status} at Kraken"
            return out
        req = OrderRequest(client_id, symbol, float(quantity), order_type, limit_price, stop_price, tif, good_till)
        price = limit_price if order_type in ("LMT", "STP LMT") else stop_price if order_type == "STP" else None
        price = price if price is not None else reference_price
        if price is None:
            price = self.last_prices([pair]).get(pair.key)
        volume = pair.round_volume(abs(float(quantity)))
        out["volume"] = volume
        reason = pair.minimum_refusal(volume, price)
        side = "buy" if quantity > 0 else "sell"
        if not reason:
            base_av, quote_av = available if available is not None else self._available(pair)
            signed = volume if side == "buy" else -volume
            reason = self.limits.check(symbol, signed, price, base_av, 0)
            if not reason and (check_balance or side == "sell"):     # spot: never sell what is not held
                reason = balance_refusal(side, volume, price, pair, base_av, quote_av, self.taker_fee)
        if reason:
            out["reason"] = reason
            return out
        cl = cl_ord_id(client_id)
        self._client[cl] = client_id
        self._save_state()
        validate = not self.armed
        try:
            fields = order_fields(req, pair, cl_ord=cl, validate=validate, now_ns=self.now())
        except ValueError as exc:
            out["reason"] = str(exc)
            return out
        try:
            res = self.rest.private("AddOrder", fields) or {}
        except KrakenError as exc:
            out.update(status="rejected", reason=str(exc))
            return out
        descr = str((res.get("descr") or {}).get("order", ""))
        txids = res.get("txid") or []
        out["descr"] = descr
        if validate or not txids:
            out.update(status="validated", reason=f"validated by Kraken (validate=true), not sent: {descr}".rstrip(": "))
            self.validated.append({"client_id": client_id, "symbol": symbol, "pair": pair.altname,
                                   "side": side, "volume": volume, "descr": descr})
            return out
        out.update(status="sent", txid=str(txids[0]))
        self._working[client_id] = _Working(client_id, str(txids[0]), symbol, pair.key, 1.0 if side == "buy" else -1.0,
                                            abs(float(quantity)), volume)
        return out

    def place(self, request: OrderRequest) -> None:
        t = self.now()
        if not self.is_connected():
            self._final(Rejected(request.client_id, "not connected to Kraken", 0, t))
            return
        r = self.submit(request.client_id, request.symbol, request.quantity, order_type=request.order_type,
                        limit_price=request.limit_price, stop_price=request.stop_price, tif=request.tif,
                        good_till=request.good_till)
        if r["status"] == "sent":
            self._queue.append(Ack(request.client_id, r["txid"], t))
        else:
            self._final(Rejected(request.client_id, r["reason"], 0, t))

    def cancel(self, client_id: str) -> None:
        w = self._working.get(client_id)
        if w is None:
            return
        w.cancel_asked = True
        try:
            self.rest.private("CancelOrder", {"txid": w.txid})
        except KrakenError as exc:
            self._queue.append(Notice(f"cancel {client_id}: {exc}", 0, client_id, self.now()))

    # -- the venue's answers -----------------------------------------------------------------
    def _final(self, message) -> None:
        if message.client_id in self._reported:
            return
        self._reported.add(message.client_id)
        self._queue.append(message)

    def _execution(self, trade_id: str, t: Mapping, client_id: str, symbol: str) -> Execution:
        sign = 1.0 if t.get("type") == "buy" else -1.0
        return Execution(client_id, trade_id, symbol, sign * float(_dec(t.get("vol"))), float(_dec(t.get("price"))),
                         float(_dec(t.get("fee"))), int(_dec(t.get("time"), "0") * 1_000_000_000))

    def poll(self, force: bool = False) -> None:
        """Ask Kraken about the orders still working (``QueryOrders`` + ``QueryTrades``) and
        queue what changed: executions, then cancellations, expiries, completions."""
        if not self._working or not self._connected:
            return
        if not force and self._monotonic() - self._last_poll < self.poll_every:
            return
        self._last_poll = self._monotonic()
        by_txid = {w.txid: w for w in self._working.values()}
        txids = list(by_txid)
        orders: dict = {}
        for i in range(0, len(txids), 50):
            orders.update(self.rest.private("QueryOrders", {"txid": ",".join(txids[i:i + 50]), "trades": True}) or {})
        new_trades = sorted({tid for o in orders.values() for tid in (o.get("trades") or [])} - self._seen_trades)
        trades: dict = {}
        for i in range(0, len(new_trades), 20):
            trades.update(self.rest.private("QueryTrades", {"txid": ",".join(new_trades[i:i + 20])}) or {})
        for tid, tr in sorted(trades.items(), key=lambda kv: float(kv[1].get("time", 0))):
            w = by_txid.get(str(tr.get("ordertxid", "")))
            if w is None or tid in self._seen_trades:
                continue
            self._seen_trades.add(tid)
            self._queue.append(self._execution(tid, tr, w.client_id, w.symbol))
        t = self.now()
        for txid, o in orders.items():
            w = by_txid.get(txid)
            if w is None:
                continue
            status = o.get("status", "")
            executed = float(_dec(o.get("vol_exec")))
            reason = str(o.get("reason") or "")
            if status == "closed":
                del self._working[w.client_id]
                if w.requested - executed > 1e-12:
                    self._final(Canceled(w.client_id, f"remainder {w.requested - executed:g} under the lot size "
                                                      f"of {w.pair}", False, t))
                else:
                    self._reported.add(w.client_id)
            elif status == "canceled":
                del self._working[w.client_id]
                self._final(Canceled(w.client_id, reason or "canceled", False, t))
            elif status == "expired":
                del self._working[w.client_id]
                self._final(Canceled(w.client_id, reason or "time in force elapsed", True, t))

    def drain(self) -> list:
        try:
            self.poll()
        except KrakenError as exc:
            self._queue.append(Notice(f"poll failed: {exc}", 0, "", self.now()))
        out = list(self._queue)
        self._queue.clear()
        return out

    def idle(self, seconds: float) -> None:
        self._sleep(max(0.0, float(seconds)))

    # -- snapshot ----------------------------------------------------------------------------
    def snapshot(self, full_sync: bool = False) -> AccountSnapshot:
        t = self.now()
        if not self._connected:
            return AccountSnapshot(None, None, self.quote, time=t)
        try:
            bal = self.balances()
        except KrakenError as exc:
            self._queue.append(Connection(False, str(exc), 0, t))
            return AccountSnapshot(None, None, self.quote, time=t)
        quote_asset = self.pairs.quote_asset(self.quote)
        cash = bal.get(quote_asset, (0.0, 0.0))[0]
        held: list[tuple[str, PairInfo | None, float]] = []
        self.ignored_balances = {}
        for asset, (q, _avail) in sorted(bal.items()):
            if asset == quote_asset or not q:
                continue
            if asset.endswith(NON_TRADABLE_SUFFIXES):
                self.ignored_balances[asset] = q
                continue
            held.append((asset, self.pairs.by_base_asset(asset, self.quote), q))
        prices = self.last_prices([p for _a, p, _q in held if p is not None])
        positions, nlv = [], cash
        for asset, pair, q in held:
            if pair is None:
                positions.append((asset, q, 0.0))       # no pair against the quote: foreign
                continue
            px = prices.get(pair.key, 0.0)
            positions.append((self.name_of(pair), q, px))
            nlv += q * px
        open_orders = []
        for txid, o in ((self.rest.private("OpenOrders") or {}).get("open") or {}).items():
            client = self._client.get(str(o.get("cl_ord_id") or ""))
            if client is None:
                continue
            d = o.get("descr") or {}
            sign = 1.0 if d.get("type") == "buy" else -1.0
            pair = self._pair_of(str(d.get("pair", "")))
            symbol = self.name_of(pair) if pair else str(d.get("pair", ""))
            vol = float(_dec(o.get("vol")))
            if client not in self._working and pair is not None:   # after a restart: follow it again
                self._working[client] = _Working(client, txid, symbol, pair.key, sign, vol, vol)
            open_orders.append(BrokerOrder(client, symbol, sign * vol, float(_dec(o.get("vol_exec")))))
        executions = tuple(self._missed_executions()) if full_sync else ()
        return AccountSnapshot(cash, nlv, self.quote, tuple(sorted(positions)), tuple(open_orders), executions,
                               full_sync, t)

    def _pair_of(self, name: str) -> PairInfo | None:
        for p in self.pairs.pairs.values():
            if name in (p.key, p.altname, p.wsname):
                return p
        return None

    def _missed_executions(self) -> list[Execution]:
        """Our executions since ``since`` (``TradesHistory``), tied to our orders through
        their ``cl_ord_id`` (``QueryOrders``)."""
        res = self.rest.private("TradesHistory", {"start": int(self.since // 1_000_000_000)}) or {}
        trades = res.get("trades") or {}
        known = {w.txid: w.client_id for w in self._working.values()}
        unknown = sorted({str(t.get("ordertxid", "")) for t in trades.values()} - set(known) - {""})
        for i in range(0, len(unknown), 50):
            for txid, o in (self.rest.private("QueryOrders", {"txid": ",".join(unknown[i:i + 50])}) or {}).items():
                client = self._client.get(str(o.get("cl_ord_id") or ""))
                if client is not None:
                    known[txid] = client
        out = []
        for tid, tr in sorted(trades.items(), key=lambda kv: float(kv[1].get("time", 0))):
            client = known.get(str(tr.get("ordertxid", "")))
            if client is None:
                continue
            pair = self._pair_of(str(tr.get("pair", "")))
            symbol = self.name_of(pair) if pair else str(tr.get("pair", ""))
            self._seen_trades.add(tid)
            out.append(self._execution(tid, tr, client, symbol))
        return out


# --------------------------------------------------------------------------------------------- #
# Bars                                                                                          #
# --------------------------------------------------------------------------------------------- #
def parse_ohlc(result: Mapping) -> list[tuple[int, float, float, float, float, float]]:
    """``OHLC``'s rows, ``(time s, open, high, low, close, volume)``, oldest first, **without**
    the last one: Kraken documents it as the current, uncommitted bar."""
    rows = next((v for k, v in (result or {}).items() if k != "last" and isinstance(v, list)), [])
    out = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[6])) for r in rows]
    return sorted(out)[:-1]


class KrakenBars:
    """Daily bars from Kraken's public ``OHLC`` (interval 1440), a ``BarSource``; no key.

    A bar is stamped at its 00:00 UTC open, as PWB's crypto dataset is; the bar of day D is
    complete at D+1 00:00 UTC and passed on ``grace`` minutes later. The last row of an
    answer (still forming) is never used. While no new bar can be complete nothing is asked;
    while one is due, a symbol is asked at most every ``retry`` minutes.
    """

    def __init__(self, rest: KrakenREST | None = None, *, quote: str = "USD", grace: float = 5.0,
                 retry: float = 10.0, pairs: PairTable | None = None):
        self.rest = rest or KrakenREST()
        self.quote = quote.upper()
        self.grace_ns = int(grace * 60e9)
        self.retry_ns = int(retry * 60e9)
        self._pairs = pairs
        self._asked: dict[str, int] = {}

    @property
    def pairs(self) -> PairTable:
        if self._pairs is None:
            self._pairs = PairTable.from_result(self.rest.public("AssetPairs"))
        return self._pairs

    def due(self, now: int) -> int:
        """The stamp (ns) of the last daily bar complete at ``now``."""
        return ((int(now) - self.grace_ns) // DAY_NS - 1) * DAY_NS

    def _fetch(self, symbol: str, since_s: int | None = None) -> list:
        pair = self.pairs.resolve(symbol, self.quote)
        params = {"pair": pair.altname, "interval": 1440}
        if since_s is not None:
            params["since"] = since_s
        return parse_ohlc(self.rest.public("OHLC", params))

    def _records(self, rows, after: int, now: int) -> list[BarRecord]:
        due = self.due(now)
        out = []
        for ts, o, h, lo, c, v in rows:
            stamp = ts * 1_000_000_000
            if after < stamp <= due:
                out.append(BarRecord(stamp, o, h, lo, c, v))
        return out

    def poll(self, symbol: str, after: int, now: int) -> list[BarRecord]:
        if self.due(now) <= after:
            return []
        asked = self._asked.get(symbol)
        if asked is not None and now - asked < self.retry_ns:
            return []
        self._asked[symbol] = now
        return self._records(self._fetch(symbol, after // 1_000_000_000 if after > 0 else None), after, now)

    def history(self, symbol: str, now: int | None = None):
        """The complete daily bars Kraken keeps (720 at most) as the DataFrame a
        ``LiveFeed`` warms up on."""
        import pandas as pd

        records = self._records(self._fetch(symbol), -(1 << 62), time.time_ns() if now is None else now)
        index = pd.to_datetime([r.time for r in records], unit="ns")
        return pd.DataFrame({"open": [r.open for r in records], "high": [r.high for r in records],
                             "low": [r.low for r in records], "close": [r.close for r in records],
                             "volume": [r.volume for r in records]}, index=index)


def bar_day(stamp_s: int) -> str:
    """The ISO date of a bar stamped at ``stamp_s`` (its 00:00 UTC open)."""
    return _dt.datetime.fromtimestamp(int(stamp_s), tz=_dt.timezone.utc).date().isoformat()


__all__ = ["API_URL", "KEY_ENV", "LIVE_ENV", "SECRET_ENV", "CallCounter", "Credentials", "FractionalCommInfo",
           "KrakenAdapter",
           "KrakenBars", "KrakenError", "KrakenREST", "NonceSource", "PairInfo", "PairTable", "balance_refusal",
           "bar_day", "catalogue_base", "cl_ord_id", "order_fields", "parse_ohlc", "sign"]
