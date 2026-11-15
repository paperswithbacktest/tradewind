"""Listed options: contracts, chains, Black-Scholes-Merton, exercise and assignment (spec/06 § 5).

Read as a specification from QuantConnect Lean (Apache 2.0, see ``NOTICE``): ``Option``
securities with a contract multiplier, ``OptionChain`` and its filter universe
(``Strikes``, ``Expiration``, ``CallsOnly``, ``PutsOnly``, ``FrontMonth``), the
Black-Scholes price model and the exercise of contracts at expiry. The code, the formulas
(from their textbook definitions) and the names are Tradewind's own.

* ``OptionContract``: underlying, expiry, strike, right, style, multiplier (100), settlement
  (physical delivery of the underlying or cash);
* ``bsm_price``, ``bsm_greeks``, ``implied_volatility``: European Black-Scholes-Merton with a
  continuous dividend yield, vectorised over numpy arrays;
* ``OptionChain``: the contracts of one underlying on one date, filtered by expiry,
  strike rank around the money, moneyness, delta, right;
* ``add_options``: one feed per contract of a daily option table (one row per contract and
  day it traded), the multiplier on the contract's security, and the expiry of every contract
  processed by the engine (exercise of a long in the money, assignment of a short, the rest
  expiring worthless).
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .dateutils import date2num, num2date
from .feeds import PandasData
from .securities import SecurityId, SymbolProperties, Timestamp

CALL, PUT = "call", "put"
AMERICAN, EUROPEAN = "american", "european"
PHYSICAL, CASH = "physical", "cash"

#: OCC's exercise-by-exception threshold: a contract in the money by at least this much at
#: expiry is exercised (long) or assigned (short) unless instructed otherwise
EXERCISE_THRESHOLD = 0.01


# -- contracts ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionContract:
    underlying: str
    expiry: _dt.date
    strike: float
    right: str                          # "call" | "put"
    style: str = AMERICAN               # "american" | "european"
    multiplier: float = 100.0
    settlement: str = PHYSICAL          # "physical" | "cash"
    symbol: str = ""

    def __post_init__(self):
        if self.right not in (CALL, PUT):
            raise ValueError(f"right is 'call' or 'put', got {self.right!r}")
        if self.style not in (AMERICAN, EUROPEAN):
            raise ValueError(f"style is 'american' or 'european', got {self.style!r}")
        if self.settlement not in (PHYSICAL, CASH):
            raise ValueError(f"settlement is 'physical' or 'cash', got {self.settlement!r}")
        if not self.symbol:
            object.__setattr__(self, "symbol", occ_symbol(self.underlying, self.expiry, self.right,
                                                          self.strike))

    @property
    def is_call(self) -> bool:
        return self.right == CALL

    def intrinsic(self, spot: float) -> float:
        """Value of exercising now, per share."""
        return max(spot - self.strike, 0.0) if self.right == CALL else max(self.strike - spot, 0.0)

    def moneyness(self, spot: float) -> float:
        """strike / spot."""
        return self.strike / spot

    def days_to_expiry(self, day) -> int:
        return (self.expiry - _as_date(day)).days

    def symbol_properties(self) -> SymbolProperties:
        return SymbolProperties(description=f"{self.underlying} {self.expiry} {self.strike:g} {self.right}",
                                contract_multiplier=float(self.multiplier), minimum_price_variation=0.01,
                                market_ticker=self.symbol)


_OCC = re.compile(r"^(?P<root>[A-Z0-9.]{1,6})\s*(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


def occ_symbol(underlying: str, expiry, right: str, strike: float) -> str:
    """The OCC symbol without padding: ``SPY190118P00250000``."""
    d = _as_date(expiry)
    return f"{underlying}{d:%y%m%d}{'C' if right == CALL else 'P'}{round(strike * 1000):08d}"


def parse_occ(symbol: str, style: str = AMERICAN, multiplier: float = 100.0,
              settlement: str = PHYSICAL) -> OptionContract:
    """An ``OptionContract`` from an OCC symbol (``SPY190118P00250000``, the padded 21-character
    form ``SPY   190118P00250000``, or either after a ``PREFIX:``)."""
    raw = symbol.rsplit(":", 1)[-1].strip().upper()
    m = _OCC.match(raw)
    if m is None:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    ymd = m.group("ymd")
    expiry = _dt.date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    return OptionContract(m.group("root"), expiry, int(m.group("strike")) / 1000.0,
                          CALL if m.group("cp") == "C" else PUT, style, multiplier, settlement, symbol)


def _as_date(day) -> _dt.date:
    if isinstance(day, _dt.datetime):
        return day.date()
    if isinstance(day, _dt.date):
        return day
    return pd.Timestamp(day).date()


def is_standard_expiry(day) -> bool:
    """The third Friday of its month (the monthly expiry; Thursday when Friday is a holiday is
    not detected here)."""
    d = _as_date(day)
    return d.weekday() == 4 and 15 <= d.day <= 21


# -- Black-Scholes-Merton --------------------------------------------------------------------------

_SQRT2 = math.sqrt(2.0)
_erfc = np.frompyfunc(math.erfc, 1, 1)


def norm_cdf(x) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return np.asarray(0.5 * _erfc(-x / _SQRT2), dtype=np.float64)


def norm_pdf(x) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    return np.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _is_call(right) -> np.ndarray:
    r = np.asarray(right)
    if r.dtype == bool:
        return r
    return np.char.lower(r.astype(str)) == CALL


def _d1d2(spot, strike, t, rate, div, vol):
    sqrt_t = np.sqrt(t)
    d1 = (np.log(spot / strike) + (rate - div + 0.5 * vol * vol) * t) / (vol * sqrt_t)
    return d1, d1 - vol * sqrt_t


def bsm_price(spot, strike, t, rate, div, vol, right) -> np.ndarray:
    """European price per share; ``t`` in years, ``rate`` and ``div`` continuous annual rates,
    ``right`` "call" / "put" (or a bool array, True for calls). At ``t <= 0`` or ``vol <= 0``:
    the discounted intrinsic value."""
    spot, strike, t, rate, div, vol = np.broadcast_arrays(*(np.asarray(a, dtype=np.float64)
                                                            for a in (spot, strike, t, rate, div, vol)))
    call = np.broadcast_to(_is_call(right), spot.shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1, d2 = _d1d2(spot, strike, t, rate, div, vol)
        df_q, df_r = np.exp(-div * t), np.exp(-rate * t)
        c = spot * df_q * norm_cdf(d1) - strike * df_r * norm_cdf(d2)
        p = strike * df_r * norm_cdf(-d2) - spot * df_q * norm_cdf(-d1)
    out = np.where(call, c, p)
    dead = (t <= 0) | (vol <= 0)
    if dead.any():
        fwd = np.where(call, spot * df_q - strike * df_r, strike * df_r - spot * df_q)
        out = np.where(dead, np.maximum(fwd, 0.0), out)
    return out


def bsm_greeks(spot, strike, t, rate, div, vol, right) -> dict[str, np.ndarray]:
    """``delta``, ``gamma``, ``vega`` (per 1.00 of volatility: divide by 100 for a vol point),
    ``theta`` (per year: divide by 365 for a calendar day), ``rho`` (per 1.00 of rate)."""
    spot, strike, t, rate, div, vol = np.broadcast_arrays(*(np.asarray(a, dtype=np.float64)
                                                            for a in (spot, strike, t, rate, div, vol)))
    call = np.broadcast_to(_is_call(right), spot.shape)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1, d2 = _d1d2(spot, strike, t, rate, div, vol)
        df_q, df_r = np.exp(-div * t), np.exp(-rate * t)
        pdf1 = norm_pdf(d1)
        sqrt_t = np.sqrt(t)
        gamma = df_q * pdf1 / (spot * vol * sqrt_t)
        vega = spot * df_q * pdf1 * sqrt_t
        common = -spot * df_q * pdf1 * vol / (2.0 * sqrt_t)
        delta = np.where(call, df_q * norm_cdf(d1), df_q * (norm_cdf(d1) - 1.0))
        theta = np.where(call,
                         common - rate * strike * df_r * norm_cdf(d2) + div * spot * df_q * norm_cdf(d1),
                         common + rate * strike * df_r * norm_cdf(-d2) - div * spot * df_q * norm_cdf(-d1))
        rho = np.where(call, strike * t * df_r * norm_cdf(d2), -strike * t * df_r * norm_cdf(-d2))
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta, "rho": rho}


def implied_volatility(price, spot, strike, t, rate, div, right, lo: float = 1e-4, hi: float = 5.0,
                       iterations: int = 80) -> np.ndarray:
    """The volatility at which ``bsm_price`` equals ``price`` (bisection on ``[lo, hi]``, ~1e-24
    wide after 80 steps: deterministic, no starting guess). NaN where the price is outside
    the no-arbitrage bounds of a European option or the root is outside ``[lo, hi]``.
    American prices (early-exercise premium) give a European volatility: slightly high for
    deep in-the-money puts."""
    price, spot, strike, t, rate, div = np.broadcast_arrays(*(np.asarray(a, dtype=np.float64)
                                                              for a in (price, spot, strike, t, rate, div)))
    call = np.broadcast_to(_is_call(right), price.shape)
    a = np.full(price.shape, lo)
    b = np.full(price.shape, hi)
    f_lo = bsm_price(spot, strike, t, rate, div, a, call) - price
    f_hi = bsm_price(spot, strike, t, rate, div, b, call) - price
    ok = (t > 0) & (price > 0) & np.isfinite(price) & (f_lo <= 0) & (f_hi >= 0)
    for _ in range(iterations):
        mid = 0.5 * (a + b)
        above = bsm_price(spot, strike, t, rate, div, mid, call) - price > 0
        b = np.where(above, mid, b)
        a = np.where(above, a, mid)
    return np.where(ok, 0.5 * (a + b), np.nan)


# -- option table and chain -----------------------------------------------------------------------

#: the columns of a daily option table (one row per contract and day it traded); PWB's table
#: has these names, plus ``vwap``, ``transactions`` and, in its IV file, ``implied_volatility``,
#: ``delta``, ``gamma``, ``vega``, ``theta``, ``underlying_close``, ``rate``
TABLE_COLUMNS = ("symbol", "date", "underlying", "expiration", "strike", "option_type")


def option_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalise a daily option table: ``date`` and ``expiration`` as datetimes, ``option_type``
    lower case, ``exercise_style`` and ``shares_per_contract`` filled with the US equity
    defaults when missing; sorted by (date, expiration, strike, option_type)."""
    missing = [c for c in TABLE_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"option table: missing columns {missing}")
    f = frame.copy()
    f["date"] = pd.to_datetime(f["date"])
    f["expiration"] = pd.to_datetime(f["expiration"])
    f["option_type"] = f["option_type"].astype(str).str.lower()
    if "exercise_style" not in f:
        f["exercise_style"] = AMERICAN
    f["exercise_style"] = f["exercise_style"].fillna(AMERICAN).astype(str).str.lower()
    if "shares_per_contract" not in f:
        f["shares_per_contract"] = 100.0
    f["shares_per_contract"] = f["shares_per_contract"].fillna(100.0)
    return f.sort_values(["date", "expiration", "strike", "option_type"], kind="stable").reset_index(drop=True)


class OptionChain:
    """The contracts of one underlying on one date, with the spot they are measured against.

    Every filter returns a new chain (Lean's filter universe, chained):

        chain.puts_only().expiration(20, 45).moneyness(0.90, 1.00)
        chain.strikes(-2, 2).front_month()

    ``frame`` holds one row per contract (the table's columns plus ``dte`` and
    ``moneyness``); ``contracts()`` gives ``OptionContract`` objects.
    """

    def __init__(self, frame: pd.DataFrame, spot: float, date, settlement: str = PHYSICAL,
                 feeds: dict | None = None):
        self.frame = frame
        self.spot = float(spot)
        self.date = pd.Timestamp(date).normalize()
        self.settlement = settlement
        self._feeds = feeds or {}
        if len(frame) and "dte" not in frame:
            self.frame = frame.assign(dte=(frame["expiration"] - self.date).dt.days.to_numpy(),
                                      moneyness=frame["strike"].to_numpy() / self.spot)

    def _new(self, frame) -> OptionChain:
        return OptionChain(frame, self.spot, self.date, self.settlement, self._feeds)

    def __len__(self):
        return len(self.frame)

    def __iter__(self):
        return iter(self.contracts())

    def __repr__(self):
        return f"OptionChain({len(self)} contracts on {self.date.date()}, spot {self.spot:g})"

    # -- filters ---------------------------------------------------------------------------
    def calls_only(self) -> OptionChain:
        return self._new(self.frame[self.frame["option_type"] == CALL])

    def puts_only(self) -> OptionChain:
        return self._new(self.frame[self.frame["option_type"] == PUT])

    def expiration(self, min_days: int = 0, max_days: int = 36500) -> OptionChain:
        """Contracts expiring in ``[min_days, max_days]`` calendar days."""
        d = self.frame["dte"]
        return self._new(self.frame[(d >= min_days) & (d <= max_days)])

    def front_month(self) -> OptionChain:
        """The nearest expiry left in the chain."""
        if not len(self):
            return self
        return self._new(self.frame[self.frame["expiration"] == self.frame["expiration"].min()])

    def standards_only(self) -> OptionChain:
        """Monthly (third-Friday) expiries only."""
        return self._new(self.frame[[is_standard_expiry(d) for d in self.frame["expiration"]]])

    def strikes(self, min_rank: int, max_rank: int) -> OptionChain:
        """The strikes from ``min_rank`` to ``max_rank`` around the money, rank 0 being the
        strike nearest the spot (Lean's ``Strikes(-n, +n)``), on the chain's distinct strikes."""
        if not len(self):
            return self
        strikes = np.unique(self.frame["strike"].to_numpy())
        atm = int(np.argmin(np.abs(strikes - self.spot)))
        keep = strikes[max(0, atm + min_rank): max(0, atm + max_rank + 1)]
        return self._new(self.frame[self.frame["strike"].isin(keep)])

    def moneyness(self, lo: float, hi: float) -> OptionChain:
        """``lo <= strike / spot <= hi``."""
        m = self.frame["moneyness"]
        return self._new(self.frame[(m >= lo) & (m <= hi)])

    def delta(self, lo: float, hi: float, rate: float = 0.0, div: float = 0.0) -> OptionChain:
        """``lo <= delta <= hi`` (puts have negative deltas): the table's ``delta`` column, or
        Black-Scholes-Merton at its ``implied_volatility`` when it has none."""
        f = self.with_greeks(rate, div).frame
        return self._new(f[(f["delta"] >= lo) & (f["delta"] <= hi)])

    def liquid(self, min_volume: float = 1) -> OptionChain:
        return self._new(self.frame[self.frame["volume"] >= min_volume]) if "volume" in self.frame else self

    def where(self, mask) -> OptionChain:
        return self._new(self.frame[np.asarray(mask, dtype=bool)])

    def nearest(self, strike: float | None = None, days: int | None = None) -> OptionChain:
        """The contracts at the expiry nearest ``days`` and/or the strike nearest ``strike``."""
        f = self.frame
        if days is not None and len(f):
            best = (f["dte"] - days).abs().min()
            f = f[(f["dte"] - days).abs() == best]
            f = f[f["dte"] == f["dte"].min()]
        if strike is not None and len(f):
            best = (f["strike"] - strike).abs().min()
            f = f[(f["strike"] - strike).abs() == best]
        return self._new(f)

    # -- values ----------------------------------------------------------------------------
    def with_greeks(self, rate: float = 0.0, div: float = 0.0, price: str = "close") -> OptionChain:
        """The chain with ``implied_volatility`` (from ``price`` when absent) and the
        Black-Scholes-Merton ``delta``, ``gamma``, ``vega``, ``theta`` (when absent)."""
        f = self.frame.copy()
        if not len(f):
            return self._new(f)
        t = f["dte"].to_numpy(dtype=np.float64) / 365.0
        right = f["option_type"].to_numpy()
        if "implied_volatility" not in f:
            f["implied_volatility"] = implied_volatility(f[price].to_numpy(dtype=np.float64), self.spot,
                                                         f["strike"].to_numpy(dtype=np.float64), t, rate,
                                                         div, right)
        if "delta" not in f:
            g = bsm_greeks(self.spot, f["strike"].to_numpy(dtype=np.float64), t, rate, div,
                           f["implied_volatility"].to_numpy(dtype=np.float64), right)
            for k in ("delta", "gamma", "vega", "theta"):
                f[k] = g[k]
        return self._new(f)

    def contracts(self) -> list[OptionContract]:
        f = self.frame
        return [OptionContract(u, e.date(), float(k), r, s, float(m), self.settlement, sym)
                for sym, u, e, k, r, s, m in zip(f["symbol"], f["underlying"], f["expiration"], f["strike"],
                                                 f["option_type"], f["exercise_style"],
                                                 f["shares_per_contract"])]

    def feeds(self) -> list:
        """The engine feeds of the chain's contracts (``add_options``), in the chain's order."""
        return [self._feeds[s] for s in self.frame["symbol"] if s in self._feeds]

    def feed(self, symbol: str):
        return self._feeds[symbol]


# -- the engine side -----------------------------------------------------------------------------


class OptionData(PandasData):
    """The daily bars of one option contract (prices per share; the security carries the
    multiplier). It does not hold back a strategy's ``next`` until it lists: a contract lists
    when it first trades, often months into a run."""

    params = (("contract", None),)

    def __init__(self):
        super().__init__()
        contract = self.p.contract
        if contract is None:
            raise ValueError("OptionData needs a contract")
        self.contract = contract
        self.symbol_properties = contract.symbol_properties()
        self._tw_gate = False


@dataclass(slots=True)
class OptionExpiry:
    """Expiry of one option contract, applied at phase 3 of the first slice after its expiry
    date (before any fill of that slice). Filled in by ``apply_to``: the settlement price,
    whether the contract was exercised or assigned, the quantity it moved."""

    security: SecurityId
    time: Timestamp
    underlying: SecurityId | None
    settle_price: float = float("nan")
    exercised: bool = False
    quantity: float = 0.0               # contracts held at expiry (signed)
    delivered: float = 0.0              # underlying units received (+) or delivered (-)
    cash: float = 0.0                   # cash moved by the exercise or cash settlement
    rank = 4                            # after symbol change, split, dividend, delisting

    def apply_to(self, broker) -> None:
        security = broker.securities[self.security]
        feed = security.feed
        contract: OptionContract = feed.contract
        # orders left on the contract: an accepted one is cancelled, one still waiting for its
        # batch acceptance expires (the contract has no bar left to fill it)
        for order in list(broker.pending):
            if order.data is feed:
                broker.cancel(order)
        for order in list(broker.submitted):
            if order.data is feed:
                broker.submitted.remove(order)
                order.expire()
                broker.notify(order)
        security._delisted = True
        pos = broker.getposition(feed)
        q = pos.size
        self.quantity = q
        spot = self._settle(broker, contract)
        self.settle_price = spot
        if not q:
            return
        m = security.properties.contract_multiplier
        intrinsic = 0.0 if math.isnan(spot) else contract.intrinsic(spot)
        self.exercised = intrinsic >= EXERCISE_THRESHOLD
        closing = intrinsic if self.exercised else 0.0
        broker.portfolio.realized_pnl[security.id] += (closing - pos.price) * q * m
        pos.set(0, 0.0)
        if not self.exercised:
            return
        ccy = security.properties.quote_currency
        if contract.settlement == CASH:
            self.cash = q * intrinsic * m
            broker.cashbook.add(ccy, self.cash)
            return
        under = broker.securities[self.underlying]
        shares = q * m if contract.is_call else -q * m
        upos = broker.getposition(under.feed)
        oldprice = upos.price
        _, _, _, closed = upos.update(shares, contract.strike)
        if closed:
            broker.portfolio.realized_pnl[under.id] += -closed * (contract.strike - oldprice)
        self.delivered = shares
        self.cash = -shares * contract.strike
        broker.cashbook.add(ccy, self.cash)

    def _settle(self, broker, contract) -> float:
        """The underlying's last close on or before the expiry date (the close of expiry day:
        PM settlement; AM-settled index options settle on the next morning's opening print,
        which a daily feed does not carry)."""
        if self.underlying is None:
            return float("nan")
        feed = broker.securities[self.underlying].feed
        n = len(feed)
        if not n:
            return float("nan")
        cutoff = date2num(contract.expiry) + 1.0
        dt = feed._dtnum[:n]
        k = int(np.searchsorted(dt, cutoff, side="left")) - 1
        return float(feed._arrays["close"][k]) if k >= 0 else float("nan")


class OptionExpiries:
    """The ``CorporateActionProvider`` of a set of option feeds: each contract's
    ``OptionExpiry`` is handed to the first slice at or after the day following its expiry
    (dates, as daily feeds stamp them: midnight UTC of that day)."""

    def __init__(self):
        self._pending: list[tuple[float, int, int | None]] = []
        self._sorted = True

    def add(self, option_feed, underlying_feed=None) -> None:
        sid = option_feed.security.id
        uid = None if underlying_feed is None else underlying_feed.security.id
        cutoff = date2num(option_feed.contract.expiry) + 1.0
        self._pending.append((cutoff, sid, uid))
        self._sorted = False

    def events(self, time) -> Sequence[OptionExpiry]:
        if not self._pending:
            return ()
        if not self._sorted:
            self._pending.sort()
            self._sorted = True
        from .clock import ns2num

        now = float(ns2num(np.int64(time)))
        out = []
        while self._pending and self._pending[0][0] <= now + 1e-9:
            _, sid, uid = self._pending.pop(0)
            out.append(OptionExpiry(sid, np.int64(time), uid))
        return out


@dataclass
class OptionUniverse:
    """What ``add_options`` returns: the feeds of the contracts and the chain on any date."""

    underlying: Any
    table: pd.DataFrame
    feeds: dict = field(default_factory=dict)
    settlement: str = PHYSICAL
    expiries: OptionExpiries | None = None

    def chain(self, date=None, spot: float | None = None, traded_only: bool = False) -> OptionChain:
        """The contracts of the table quoted on ``date`` (the underlying's current date by
        default; the last table date on or before it), against ``spot`` (the underlying's
        current close by default). Only contracts that have a feed are listed;
        ``traded_only`` keeps those whose feed delivered a bar at that date."""
        if date is None:
            date = num2date(self.underlying.datetime[0]).date()
        if spot is None:
            spot = float(self.underlying.close[0])
        day = pd.Timestamp(date).normalize()
        dates = self._dates
        k = int(np.searchsorted(dates, day.to_datetime64(), side="right")) - 1
        if k < 0:
            return OptionChain(self.table.iloc[:0], spot, day, self.settlement, self.feeds)
        lo, hi = self._bounds[k], self._bounds[k + 1]
        f = self.table.iloc[lo:hi]
        f = f[f["symbol"].isin(self.feeds.keys()) & (f["expiration"] >= day)]
        if traded_only:
            f = f[f["date"] == day]
        return OptionChain(f, spot, day, self.settlement, self.feeds)

    def __post_init__(self):
        dates = self.table["date"].to_numpy()
        change = np.flatnonzero(dates[1:] != dates[:-1]) + 1
        self._dates = dates[np.concatenate(([0], change))] if len(dates) else dates
        self._bounds = np.concatenate(([0], change, [len(dates)])).astype(np.int64)


def add_options(engine, underlying, table: pd.DataFrame, select: Callable[[OptionChain], OptionChain]
                | None = None, settlement: str = PHYSICAL, fill: str = "none",
                min_bars: int = 1, symbol: str | None = None) -> OptionUniverse:
    """Add one ``OptionData`` per contract of ``table`` (``option_table`` layout) to ``engine``,
    with the expiry of each contract handled by the engine. ``underlying`` is the feed the
    contracts are measured against and delivered in (already added).

    ``select`` keeps a subset: it receives each date's chain (against the underlying's close
    of that date) and returns the part whose contracts get a feed -- the union over the dates
    is added, so a run never misses a contract its strategy could pick, and an option
    universe of a few hundred feeds stays cheap. ``fill="model"`` fills the days a contract
    did not trade with a Black-Scholes-Merton price at its last implied volatility (needs an
    ``implied_volatility`` column; the bar's volume is 0, so it marks the position but its
    price is a model's, not a trade's); ``"none"`` keeps the engine's rule (the last bar
    repeats). ``symbol`` picks the table's rows of that underlying (default: the table's only
    underlying, or the feed's name).
    """
    t = option_table(table)
    names = t["underlying"].unique()
    if symbol is None:
        symbol = names[0] if len(names) == 1 else underlying._name
    t = t[t["underlying"] == symbol]
    if select is not None:
        spots = _underlying_closes(underlying)
        keep: set[str] = set()
        for day, rows in t.groupby("date", sort=True):
            s = spots.get(pd.Timestamp(day).normalize())
            if s is None:
                continue
            keep.update(select(OptionChain(rows, s, day, settlement)).frame["symbol"].tolist())
        t = t[t["symbol"].isin(keep)]
    expiries = OptionExpiries()
    universe = OptionUniverse(underlying, t.reset_index(drop=True), {}, settlement, expiries)
    for sym, rows in t.groupby("symbol", sort=False):
        if len(rows) < min_bars:
            continue
        r0 = rows.iloc[0]
        contract = OptionContract(str(r0["underlying"]), r0["expiration"].date(), float(r0["strike"]),
                                  str(r0["option_type"]), str(r0["exercise_style"]),
                                  float(r0["shares_per_contract"]), settlement, str(sym))
        frame = rows.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
        if fill == "model":
            frame = _model_fill(frame, rows, contract, underlying)
        feed = OptionData(dataname=frame, contract=contract)
        engine.adddata(feed, name=str(sym))
        expiries.add(feed, underlying)
        universe.feeds[str(sym)] = feed
    engine.add_corporate_actions(expiries)
    return universe


def _underlying_closes(feed) -> dict:
    frame = feed.p.dataname
    if isinstance(frame, pd.DataFrame):
        close = frame["close"] if "close" in frame else frame.iloc[:, 3]
        idx = pd.DatetimeIndex(frame.index)
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        return dict(zip(idx.normalize(), close.to_numpy(dtype=np.float64)))
    feed._start()
    return {pd.Timestamp(num2date(d)).normalize(): c for d, c in zip(feed._dtnum, feed._arrays["close"])}


def _model_fill(frame: pd.DataFrame, rows: pd.DataFrame, contract: OptionContract, underlying,
                rate: float = 0.0) -> pd.DataFrame:
    if "implied_volatility" not in rows:
        raise ValueError("fill='model' needs an implied_volatility column")
    spots = pd.Series(_underlying_closes(underlying)).sort_index()
    first, last = frame.index.min(), min(pd.Timestamp(contract.expiry), spots.index.max())
    days = spots.loc[first:last].index
    iv = rows.set_index("date")["implied_volatility"].astype(float).reindex(days).ffill()
    out = frame.reindex(days)
    missing = out["close"].isna() & iv.notna()
    if missing.any():
        d = days[missing.to_numpy()]
        t = np.maximum((pd.Timestamp(contract.expiry) - d).days.to_numpy(dtype=np.float64), 0.0) / 365.0
        px = bsm_price(spots.loc[d].to_numpy(), contract.strike, t, rate, 0.0, iv.loc[d].to_numpy(),
                       contract.right)
        for col in ("open", "high", "low", "close"):
            out.loc[d, col] = px
        out.loc[d, "volume"] = 0.0
    return out.dropna(subset=["close"])


def chain_from_table(table: pd.DataFrame, date, spot: float, settlement: str = PHYSICAL) -> OptionChain:
    """A chain straight from a table (no engine): the rows quoted on ``date``."""
    t = option_table(table)
    day = pd.Timestamp(date).normalize()
    return OptionChain(t[t["date"] == day], spot, day, settlement)


def synthetic_option_table(underlying: pd.DataFrame, symbol: str, vol: float = 0.2, rate: float = 0.0,
                           expiries: Iterable | None = None, strikes_pct: Sequence[float] = tuple(
                               np.round(np.arange(0.80, 1.201, 0.025), 3)), step: float = 1.0,
                           seed: int = 0, right: Sequence[str] = (CALL, PUT)) -> pd.DataFrame:
    """A daily option table priced by Black-Scholes-Merton on a price series (examples and
    tests): monthly third-Friday expiries, strikes on a grid around the spot of the listing
    day (``strikes_pct`` x spot, rounded to ``step``), a contract quoted every day from its
    listing (two months before expiry) to its expiry, with a little noise on the volatility."""
    rng = np.random.default_rng(seed)
    close = underlying["close"]
    idx = pd.DatetimeIndex(close.index).tz_localize(None) if close.index.tz is not None else \
        pd.DatetimeIndex(close.index)
    close = pd.Series(close.to_numpy(dtype=np.float64), index=idx.normalize())
    if expiries is None:
        months = pd.date_range(close.index[0], close.index[-1] + pd.Timedelta(days=62), freq="MS")
        expiries = [d + pd.offsets.WeekOfMonth(week=2, weekday=4) for d in months]
    rows = []
    for exp in expiries:
        exp = pd.Timestamp(exp).normalize()
        live = close.loc[exp - pd.Timedelta(days=62): exp]
        if len(live) < 2:
            continue
        s0 = float(live.iloc[0])
        strikes = sorted({round(s0 * p / step) * step for p in strikes_pct})
        days = live.index
        t = np.maximum((exp - days).days.to_numpy(dtype=np.float64), 0.0) / 365.0
        for k in strikes:
            for r in right:
                v = vol * (1 + rng.normal(0, 0.02, len(days)))
                px = np.round(np.maximum(bsm_price(live.to_numpy(), k, t, rate, 0.0, v, r), 0.01), 2)
                sym = occ_symbol(symbol, exp, r, k)
                for d, p in zip(days, px):
                    rows.append((sym, d, symbol, exp, float(k), r, AMERICAN, 100.0, p, p, p, p, 10.0))
    return pd.DataFrame(rows, columns=["symbol", "date", "underlying", "expiration", "strike", "option_type",
                                       "exercise_style", "shares_per_contract", "open", "high", "low", "close",
                                       "volume"])


__all__ = [
    "AMERICAN", "CALL", "CASH", "EUROPEAN", "EXERCISE_THRESHOLD", "PHYSICAL", "PUT", "OptionChain",
    "OptionContract", "OptionData", "OptionExpiries", "OptionExpiry", "OptionUniverse", "add_options",
    "bsm_greeks", "bsm_price", "chain_from_table", "implied_volatility", "is_standard_expiry", "norm_cdf",
    "norm_pdf", "occ_symbol", "option_table", "parse_occ", "synthetic_option_table",
]
