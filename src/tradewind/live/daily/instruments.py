"""What a symbol of a sleeve's book is at the broker: a stock or ETF, a spot forex pair, an
index (not tradable) or the substitute of one.

**Forex.** A catalogue strategy names a currency the way PWB's forex dataset does, ``AUDUSD``:
the price is the USD value of one unit of AUD, and the book holds AUD units (a long AUD
position against USD). At IBKR spot forex trades on IDEALPRO as ``CASH`` contracts quoted in
IB's own direction: the base is the first of EUR, GBP, AUD, NZD, USD that the pair contains
(``AUD.USD``, but ``USD.JPY``, ``USD.NOK``, ``USD.KRW`` ...), and an order's quantity is in
units of that base currency. So:

* a **direct** pair (``AUDUSD`` -> ``AUD.USD``): the book's quantity is IB's quantity;
* an **inverted** pair (``JPYUSD`` -> ``USD.JPY``): ``q`` JPY bought is ``q x price`` USD sold
  (``price`` the USD value of 1 JPY); a USD.JPY position of ``x`` USD at an average cost of
  ``S`` JPY per USD holds ``-x x S`` JPY in the book's units.

The run itself (targets, gross, tracking) stays in the book's units, whose notional is USD;
only the account converts at the broker. A symbol quoted the other way (``USDJPY``, a price in
JPY per USD) is not traded: the strategy's backtest values it in JPY as if it were USD.

Currencies without a spot pair on IDEALPRO are never traded (``FX_UNAVAILABLE``: the reason is
given in the run's warnings): BRL, CLP, ISK, INR, IDR (non-deliverable, no IDEALPRO pair in
either direction at IB, checked on 2026-11-20) and RUB (the pair exists, its trading is
restricted). A currency not in ``IDEALPRO`` is refused too. KRW has a pair (USD.KRW) but is
not freely convertible: it is traded, with a warning until a paper order has been tried.

IDEALPRO fills orders of at least about 25,000 USD at the interbank quote; a smaller order is
an *odd lot*, routed elsewhere at a wider price (``FX_ODD_LOT_USD``): the run warns.

**Indices** (``SPX``, ``VIX``, a symbol ending in ``.IDX`` ...) cannot be bought. A
meta-portfolio substitutes a tradable instrument for them (``Substitute``): an ETF at the same
notional by default (``SPX`` -> ``SPY``), or an index future at ``1 / multiplier`` contract
per index unit, an option to turn on by hand (its expiry is given and checked, nothing rolls
by itself). An index left without a substitute is refused: no order is ever proposed on it.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass

from .books import SleeveBook

#: IDEALPRO spot currencies (against USD) the runner trades
IDEALPRO = frozenset({"USD", "EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY", "NOK", "SEK", "DKK", "PLN", "CZK",
                      "HUF", "TRY", "MXN", "ZAR", "SGD", "HKD", "CNH", "ILS", "KRW"})
#: currencies the runner never trades on IDEALPRO, and why
FX_UNAVAILABLE = {
    "BRL": "no spot pair on IDEALPRO (non-deliverable currency)",
    "CLP": "no spot pair on IDEALPRO (non-deliverable currency)",
    "ISK": "no spot pair on IDEALPRO",
    "INR": "no spot pair on IDEALPRO (non-deliverable currency)",
    "IDR": "no spot pair on IDEALPRO (non-deliverable currency)",
    "RUB": "USD.RUB exists on IDEALPRO but its trading is restricted (sanctions)",
}
#: currencies traded with a warning
FX_CAUTION = {"KRW": "KRW is not freely convertible: USD.KRW is listed on IDEALPRO, try a paper order before "
                     "relying on it"}
#: IB's order of precedence for the base of a pair (the first one present is the base)
_BASE_ORDER = ("EUR", "GBP", "AUD", "NZD", "USD")
_ISO = IDEALPRO | frozenset(FX_UNAVAILABLE) | frozenset({"TWD", "PHP", "THB", "MYR", "COP", "PEN", "ARS", "KZT",
                                                         "RON", "BGN", "HRK", "SAR", "AED", "CNY", "VND", "EGP",
                                                         "NGN", "PKR", "UAH", "QAR", "KWD"})

#: an IDEALPRO order below this USD notional is an odd lot (a wider price)
FX_ODD_LOT_USD = 25_000.0

#: index symbols that cannot be traded (besides any symbol ending in .IDX or :IDX)
INDICES = frozenset({"SPX", "VIX", "NDX", "RUT", "DJI", "INDU", "VXN", "VVIX", "OEX", "XSP", "SPXEW", "COMP",
                     "NYA", "MID", "SML", "UKX", "DAX", "CAC", "NKY", "HSI", "SX5E"})

#: the substitutes a meta-portfolio uses unless it says otherwise
DEFAULT_SUBSTITUTES = {"SPX": "SPY"}


# --------------------------------------------------------------------------------------------- #
# Forex                                                                                         #
# --------------------------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FxPair:
    """A book symbol ``AAABBB`` (a price in BBB per AAA, the book long AAA) and IB's pair."""

    symbol: str                 # as the book names it
    currency: str               # the foreign currency (not USD)
    book_base: str              # AAA
    book_quote: str             # BBB
    ib_base: str
    ib_quote: str

    @property
    def ib_pair(self) -> str:
        """IB's local symbol, ``USD.JPY``."""
        return f"{self.ib_base}.{self.ib_quote}"

    @property
    def inverted(self) -> bool:
        return self.ib_base != self.book_base

    @property
    def usd_quoted(self) -> bool:
        """The book's price is in USD per unit (``XXXUSD``): its notional is in USD."""
        return self.book_quote == "USD"

    def ib_quantity(self, quantity: float, price: float) -> float:
        """IB's signed quantity (units of the IB base) for ``quantity`` units of the book's base
        bought at ``price`` (the book's price, BBB per AAA)."""
        return quantity if not self.inverted else -quantity * price

    def book_quantity(self, ib_quantity: float, ib_price: float) -> float:
        """The book's quantity for an IB position (or execution) of ``ib_quantity`` units of
        the IB base at ``ib_price`` (IB's price: quote per base, an average cost for a
        position)."""
        return ib_quantity if not self.inverted else -ib_quantity * ib_price

    def book_price(self, ib_price: float) -> float:
        return ib_price if not self.inverted else (1.0 / ib_price if ib_price else float("nan"))


def _split(symbol: str) -> tuple[str, str] | None:
    s = symbol.upper()
    if "." in s and len(s) == 7 and s[3] == ".":                        # IB's local symbol, USD.JPY
        a, b = s[:3], s[4:]
    elif len(s) == 6 and s.isalpha():
        a, b = s[:3], s[3:]
    else:
        return None
    if a == b or a not in _ISO or b not in _ISO or "USD" not in (a, b):
        return None
    return a, b


def fx_pair(symbol: str) -> FxPair | None:
    """The pair a book symbol (``AUDUSD``, ``JPYUSD``) or an IB local symbol (``USD.JPY``) is,
    or None: only pairs against USD are recognised."""
    parts = _split(symbol)
    if parts is None:
        return None
    a, b = parts
    base = next(c for c in _BASE_ORDER if c in (a, b))
    quote = b if base == a else a
    return FxPair(symbol=symbol, currency=a if b == "USD" else b, book_base=a, book_quote=b,
                  ib_base=base, ib_quote=quote)


def is_fx(symbol: str) -> bool:
    return fx_pair(symbol) is not None


def fx_refusal(symbol: str) -> str | None:
    """Why a forex symbol is not traded (None: it is, or it is not forex)."""
    pair = fx_pair(symbol)
    if pair is None:
        return None
    if not pair.usd_quoted:
        return (f"quoted in {pair.book_quote} per USD: the backtest values it in {pair.book_quote}; "
                f"use the {pair.currency}USD series")
    if pair.currency in FX_UNAVAILABLE:
        return FX_UNAVAILABLE[pair.currency]
    if pair.currency not in IDEALPRO:
        return f"{pair.currency} is not an IDEALPRO currency known to Tradewind"
    return None


def ibkr_fx_commission(notional_usd: float) -> float:
    """IBKR Pro, IDEALPRO spot, first volume tier: 0.20 basis point of the trade value, at least
    2 USD."""
    if not notional_usd:
        return 0.0
    return max(2.0, 0.00002 * abs(notional_usd))


# --------------------------------------------------------------------------------------------- #
# Indices and their substitutes                                                                 #
# --------------------------------------------------------------------------------------------- #
def is_index(symbol: str, extra=()) -> bool:
    s = symbol.upper()
    return s in INDICES or s in {x.upper() for x in extra} or s.endswith((".IDX", ":IDX")) or s.startswith("^")


@dataclass(frozen=True)
class Substitute:
    """What is traded in place of an index.

    ``kind="etf"``: ``symbol`` at the same notional, ``q_sub = q x price / price_sub`` (its
    prices come from a sleeve that holds it, or from the provider's price loader).
    ``kind="future"``: ``q / multiplier`` contracts of ``symbol`` expiring ``expiry``
    (``YYYYMMDD`` or ``YYYYMM``) on ``exchange``; a contract is priced at ``multiplier x`` the
    index (the basis is ignored: right for an equity index future, poor for VIX futures,
    which trade far from the spot). The run refuses a contract within ``roll_days`` of its
    expiry: the next one has to be written in the file."""

    symbol: str
    kind: str = "etf"
    multiplier: float = 1.0
    expiry: str = ""
    exchange: str = ""
    currency: str = "USD"
    roll_days: int = 5

    def __post_init__(self):
        if self.kind not in ("etf", "future"):
            raise ValueError(f"substitute kind is 'etf' or 'future', not {self.kind!r}")
        if self.kind == "future":
            if not (self.multiplier > 0) or not self.expiry or not self.exchange:
                raise ValueError(f"the future {self.symbol} needs multiplier, expiry and exchange")
            if not self.expiry.isdigit() or len(self.expiry) not in (6, 8):
                raise ValueError(f"expiry of {self.symbol}: YYYYMM or YYYYMMDD, not {self.expiry!r}")

    @classmethod
    def parse(cls, value) -> Substitute | None:
        if value is None or value is False or value == "":
            return None
        if isinstance(value, Substitute):
            return value
        if isinstance(value, str):
            return cls(symbol=value)
        d = dict(value)
        return cls(symbol=str(d.pop("symbol")), kind=str(d.pop("kind", "etf")),
                   multiplier=float(d.pop("multiplier", 1.0)), expiry=str(d.pop("expiry", "") or ""),
                   exchange=str(d.pop("exchange", "") or ""), currency=str(d.pop("currency", "USD")),
                   roll_days=int(d.pop("roll_days", 5)), **d)

    def last_day(self) -> _dt.date:
        e = self.expiry
        if len(e) == 8:
            return _dt.date(int(e[:4]), int(e[4:6]), int(e[6:]))
        y, m = int(e[:4]), int(e[4:6])                       # a month: its third Friday
        first = _dt.date(y, m, 1)
        return first + _dt.timedelta(days=(4 - first.weekday()) % 7 + 14)

    def check_expiry(self, day: str) -> None:
        if self.kind != "future":
            return
        left = (self.last_day() - _dt.date.fromisoformat(day)).days
        if left < self.roll_days:
            raise ValueError(f"the future {self.symbol} {self.expiry} expires in {left} day(s) after {day}: "
                             f"write the next expiry in the meta-portfolio (nothing rolls by itself)")

    def spec(self) -> dict:
        """What the IBKR account needs to build the contract."""
        if self.kind == "future":
            return {"sec_type": "FUT", "symbol": self.symbol, "expiry": self.expiry, "exchange": self.exchange,
                    "currency": self.currency, "multiplier": self.multiplier}
        return {}


def _convert(q: float, price: float, sub: Substitute, sub_price: float | None) -> float | None:
    if sub.kind == "future":
        return q / sub.multiplier
    if sub_price is None or not (sub_price > 0) or not (price > 0):
        return None
    return q * price / sub_price


def substitute_books(history: dict[str, dict[str, SleeveBook]], substitutes: dict[str, Substitute],
                     prices: dict[str, dict[str, tuple[float, float]]]) -> tuple[dict, list[str]]:
    """Every book with its indices replaced by their substitutes (shares converted at each
    book's own close, prices of the substitute set), and the warnings.

    ``prices``: ``{symbol: {date: (open, close)}}`` of the ETF substitutes (the sleeves' own
    closes are used first). A book whose index cannot be converted (no price of the substitute
    on that date) keeps the index, which the plan then refuses."""
    warnings: list[str] = []
    seen: dict[str, dict[str, tuple[float, float]]] = {s: dict(v) for s, v in prices.items()}
    for h in history.values():                                  # the sleeves' own prices first
        for d, b in h.items():
            for sym, c in b.close.items():
                seen.setdefault(sym, {}).setdefault(d, (b.open.get(sym, c), c))
    out: dict[str, dict[str, SleeveBook]] = {}
    noted: set[tuple[str, str]] = set()
    for slug, h in history.items():
        out[slug] = {}
        for d, b in h.items():
            used = {s for s in substitutes if s in b.close or s in b.target or s in b.positions}
            if not used:
                out[slug][d] = b
                continue
            nb = SleeveBook.from_dict(b.to_dict())
            for idx in sorted(used):
                sub = substitutes[idx]
                c_idx = b.close.get(idx)
                o_idx = b.open.get(idx, c_idx)
                if sub.kind == "future":
                    po, pc = (o_idx or float("nan")) * sub.multiplier, (c_idx or float("nan")) * sub.multiplier
                else:
                    po, pc = seen.get(sub.symbol, {}).get(d, (None, None))
                if c_idx is None or not (c_idx > 0) or pc is None or not (pc > 0):
                    if (slug, idx) not in noted:
                        warnings.append(f"{slug}: no price of {sub.symbol} at {d} to substitute it for {idx}; "
                                        f"{idx} is left in the book (and refused)")
                        noted.add((slug, idx))
                    continue
                for attr in ("positions", "target"):
                    book = getattr(nb, attr)
                    if idx in book:
                        q = _convert(book.pop(idx), c_idx, sub, pc)
                        if q:
                            book[sub.symbol] = book.get(sub.symbol, 0.0) + q
                for attr, p in (("close", pc), ("open", po)):
                    getattr(nb, attr).pop(idx, None)
                    if p is not None and math.isfinite(p) and p > 0:
                        getattr(nb, attr).setdefault(sub.symbol, p)
                if idx in nb.half_spread:
                    nb.half_spread.setdefault(sub.symbol, nb.half_spread.pop(idx))
            out[slug][d] = nb
    for idx, sub in sorted(substitutes.items()):
        if any(idx in b.close or idx in b.target for h in history.values() for b in h.values()):
            how = (f"{sub.multiplier:g} index units per contract, expiry {sub.expiry}" if sub.kind == "future"
                   else "same notional")
            warnings.append(f"substitute: {idx} -> {sub.symbol} ({sub.kind}, {how}); the sleeve's backtest stays on "
                            f"{idx}, the difference goes to the residual of the tracking")
    return out, warnings


__all__ = ["DEFAULT_SUBSTITUTES", "FX_CAUTION", "FX_ODD_LOT_USD", "FX_UNAVAILABLE", "IDEALPRO", "INDICES", "FxPair",
           "Substitute", "fx_pair", "fx_refusal", "ibkr_fx_commission", "is_fx", "is_index", "substitute_books"]
