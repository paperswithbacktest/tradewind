"""A portfolio's currency policy (spec/13): the base currency the investor counts in, and
whether the dollar exposure of the net value is left open (``unhedged``) or sold against the
base currency on a schedule (``hedged``).

The strategies' books are in USD. Write ``x`` for the rate (USD per unit of base), ``E`` the
part of the value held in the base currency (base units) and ``U`` the part exposed to the
dollar (USD); the value in base is ``E + U / x``. ``FxBook`` steps that pair one session at a
time -- carry on ``E``, the order decided at the previous close filled at the open, the
strategies' USD P&L added to ``U`` -- and decides the next order at the close. The same
function serves the daily runner's reference (``runner.py``) and a backtest's NAV series
(``apply_fx_policy``): the live account and its backtest follow one rule.

Short rates (``ShortRates``) are public dated series, % a year, ACT/360: ``fred:<id>`` (the
St. Louis Fed's CSV, no key), ``ecb:<flow>/<key>`` (the ECB data portal), a CSV path
(``date,rate``) or a number. The rate of the interval from ``t-1`` to ``t`` is the last one
published on or before ``t-1``.
"""

from __future__ import annotations

import datetime as _dt
import io
import math
import os
import urllib.request
from dataclasses import dataclass, field

import pandas as pd

from .aggregate import due_for_reset
from .instruments import IDEALPRO, ibkr_fx_commission

POLICIES = ("unhedged", "hedged")
HEDGE_REBALANCE = ("daily", "weekly", "monthly")
#: the short rates used when a meta-portfolio names none (public series, % a year)
DEFAULT_RATES = {"USD": "fred:DFF", "EUR": "fred:ECBESTRVOLWGTTRMDMNRT"}
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
ECB_CSV = "https://data-api.ecb.europa.eu/service/data/{flow}/{key}?format=csvdata"


# --------------------------------------------------------------------------------------------- #
# Short rates                                                                                   #
# --------------------------------------------------------------------------------------------- #
def _parse_csv(text: str) -> pd.Series:
    frame = pd.read_csv(io.StringIO(text))
    cols = {c.lower(): c for c in frame.columns}
    if "time_period" in cols and "obs_value" in cols:          # ECB data portal
        d, v = cols["time_period"], cols["obs_value"]
    else:                                                        # FRED, or date,rate
        d, v = frame.columns[0], frame.columns[-1]
    s = pd.Series(pd.to_numeric(frame[v], errors="coerce").to_numpy(),
                  index=pd.to_datetime(frame[d].astype(str), errors="coerce"))
    s = s[s.index.notna()].dropna().sort_index()
    return s[~s.index.duplicated(keep="last")]


class ShortRates:
    """Daily short rates by currency, % a year. ``sources``: ``{currency: spec}`` (module doc);
    ``cache_dir``: where downloads are kept (refreshed once a day; the cached copy is used
    when the network fails)."""

    def __init__(self, sources: dict | None = None, cache_dir: str | None = None, *, fetch=None, log=print):
        self.sources = {str(k).upper(): v for k, v in (sources or {}).items()}
        self.cache_dir = cache_dir
        self._fetch = fetch or _http_get
        self.log = log
        self._series: dict[str, pd.Series | float | None] = {}
        self.warnings: list[str] = []

    def spec(self, currency: str):
        currency = currency.upper()
        return self.sources.get(currency, DEFAULT_RATES.get(currency))

    def _load(self, currency: str):
        spec = self.spec(currency)
        if spec is None:
            return None
        if isinstance(spec, (int, float)) and not isinstance(spec, bool):
            return float(spec)
        spec = str(spec)
        if spec.startswith(("fred:", "ecb:")):
            kind, ident = spec.split(":", 1)
            url = (FRED_CSV.format(id=ident) if kind == "fred"
                   else ECB_CSV.format(flow=ident.split("/", 1)[0], key=ident.split("/", 1)[1]))
            path = None
            if self.cache_dir:
                os.makedirs(self.cache_dir, exist_ok=True)
                path = os.path.join(self.cache_dir, f"{kind}_{ident.replace('/', '_')}.csv")
            fresh = path is not None and os.path.exists(path) and (
                _dt.date.fromtimestamp(os.path.getmtime(path)) == _dt.date.today())
            text = None
            if not fresh:
                try:
                    text = self._fetch(url)
                    if path is not None:
                        with open(path, "w", encoding="utf-8") as fh:
                            fh.write(text)
                except Exception as exc:  # noqa: BLE001 - the cached copy, else no carry
                    self.log(f"[fx] short rate {currency} ({spec}): {exc}")
            if text is None and path is not None and os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            return _parse_csv(text) if text else None
        with open(os.path.expanduser(spec), encoding="utf-8") as fh:
            return _parse_csv(fh.read())

    def rate(self, currency: str, on: str) -> float | None:
        """The rate of ``currency`` published on or before ``on`` (% a year), or None."""
        currency = currency.upper()
        if currency not in self._series:
            self._series[currency] = self._load(currency)
        s = self._series[currency]
        if s is None:
            return None
        if isinstance(s, float):
            return s
        s = s.loc[:pd.Timestamp(on)]
        return float(s.iloc[-1]) if len(s) else None

    def differential(self, base: str, on: str) -> float:
        """``i_base - i_usd`` (% a year) as of ``on``; 0 with a warning when one is missing."""
        a, b = self.rate(base, on), self.rate("USD", on)
        if a is None or b is None:
            missing = [c for c, r in ((base, a), ("USD", b)) if r is None]
            msg = f"no short rate for {', '.join(missing)} up to {on}: the hedge's carry is taken as 0"
            if msg not in self.warnings:
                self.warnings.append(msg)
            return 0.0
        return a - b


def _http_get(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "tradewind"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


# --------------------------------------------------------------------------------------------- #
# The policy and its book                                                                       #
# --------------------------------------------------------------------------------------------- #
@dataclass
class FxPolicy:
    base_currency: str = "USD"
    policy: str = "unhedged"
    rebalance: str = "monthly"
    half_spread: float = 0.000025          # of the notional: about a quarter of a basis point on EUR.USD
    rates: dict = field(default_factory=dict)
    min_notional: float = 0.0              # an order under it (USD) is not made, on either side

    def __post_init__(self):
        self.base_currency = (self.base_currency or "USD").upper()
        self.policy = (self.policy or "unhedged").lower()
        self.rebalance = (self.rebalance or "monthly").lower()
        if self.policy not in POLICIES:
            raise ValueError(f"fx_policy is one of {POLICIES}, not {self.policy!r}")
        if self.rebalance not in HEDGE_REBALANCE:
            raise ValueError(f"fx_hedge_rebalance is one of {HEDGE_REBALANCE}, not {self.rebalance!r}")
        if not (len(self.base_currency) == 3 and self.base_currency.isalpha()):
            raise ValueError(f"base_currency is a currency code (EUR, USD ...), not {self.base_currency!r}")
        if self.active and self.base_currency not in IDEALPRO:
            raise ValueError(f"base_currency {self.base_currency}: no spot pair against USD on IDEALPRO")
        if not 0.0 <= float(self.half_spread) < 0.01:
            raise ValueError("fx_hedge_half_spread is a fraction of the notional (0.000025 is 0.25 bp)")
        self.half_spread = float(self.half_spread)

    @property
    def active(self) -> bool:
        """Whether there is anything to do: a base currency other than the books' USD."""
        return self.base_currency != "USD"

    @property
    def pair(self) -> str:
        """The pair in the books' naming (``EURUSD``: the USD price of one unit of base)."""
        return f"{self.base_currency}USD"

    def fee(self, notional_usd: float) -> float:
        """The cost of trading ``notional_usd`` on the pair: IDEALPRO's commission plus the
        half-spread."""
        n = abs(float(notional_usd))
        return ibkr_fx_commission(n) + n * self.half_spread if n else 0.0

    def order(self, usd: float, rate: float, *, day: str, last: str | None, capital_usd: float) -> float:
        """Base units to buy (+) or sell (-) at the next open, decided at ``day``'s close from
        the dollar exposure ``usd``; 0 when nothing is due or the order is under the minimum.
        ``last``: the date of the previous decision (None: the first day)."""
        if not self.active or not (rate > 0):
            return 0.0
        if self.policy == "hedged":
            if last is not None and not due_for_reset(self.rebalance, last, day):
                return 0.0
            target = 0.0
        else:
            if last is not None:                 # unhedged: the first conversion only
                return 0.0
            target = float(capital_usd)
        q = (usd - target) / rate
        if not math.isfinite(q) or abs(q) * rate < max(self.min_notional, 1e-9):
            return 0.0
        return q


@dataclass
class FxBook:
    """The base / dollar split of a portfolio under an ``FxPolicy``: ``base`` (``E``, base
    units), ``usd`` (``U``, USD), the order to fill at the next open (``pending``, base units)
    and the date of the last decision (``last``). Plain data: it goes into ``state.json``."""

    base: float = 0.0
    usd: float = 0.0
    pending: float = 0.0
    last: str | None = None

    def value(self, rate: float) -> float:
        return self.base + self.usd / rate

    def to_dict(self) -> dict:
        return {"base": self.base, "usd": self.usd, "pending": self.pending, "last": self.last}

    @classmethod
    def from_dict(cls, d: dict) -> FxBook:
        return cls(float(d.get("base", 0.0)), float(d.get("usd", 0.0)), float(d.get("pending", 0.0)),
                   d.get("last"))

    def step(self, policy: FxPolicy, *, pnl_usd: float, open_rate: float, days: float,
             differential: float) -> dict:
        """One session: carry on ``base`` over ``days`` at ``differential`` (``i_base - i_usd``,
        % a year, ACT/360), the pending order filled at ``open_rate``, the strategies' P&L
        added to ``usd``. Returns ``{carry (base), fee (USD), filled (base units), fill_price}``."""
        carry = self.base * differential / 100.0 * days / 360.0
        self.base += carry
        q, fee = self.pending, 0.0
        if q:
            fee = policy.fee(q * open_rate)
            self.base += q
            self.usd -= q * open_rate + fee
        self.pending = 0.0
        self.usd += pnl_usd
        return {"carry": carry, "fee": fee, "filled": q, "fill_price": open_rate if q else float("nan")}

    def decide(self, policy: FxPolicy, rate: float, day: str, capital_usd: float) -> float:
        q = policy.order(self.usd, rate, day=day, last=self.last, capital_usd=capital_usd)
        if self.last is None or q or (policy.policy == "hedged" and due_for_reset(policy.rebalance, self.last, day)):
            self.last = day
        self.pending = q
        return q


def apply_fx_policy(nav_usd: pd.Series, rate: pd.Series, policy: FxPolicy, *, rates: ShortRates | None = None,
                    opens: pd.Series | None = None, capital_usd: float | None = None) -> pd.DataFrame:
    """A backtest's USD NAV seen in ``policy.base_currency``: the investor starts with the
    first NAV in base currency (``E = nav / x``, ``U = 0``) and follows the policy (the
    unhedged conversion and the hedges fill at the next open: ``opens`` of the pair, else the
    previous close). Returns, per date, ``value`` (base), ``base``, ``usd``, ``carry``,
    ``fee`` (USD), ``order`` (base units decided at the close)."""
    nav = nav_usd.astype(float).dropna()
    x = rate.astype(float).reindex(nav.index).ffill()
    if x.isna().any():
        raise ValueError("apply_fx_policy: no rate for the first dates of the NAV")
    capital = float(nav.iloc[0]) if capital_usd is None else float(capital_usd)
    book = FxBook(base=float(nav.iloc[0]) / float(x.iloc[0]))
    rows = []
    prev_day, prev_nav, prev_rate = None, None, None
    for d, v in nav.items():
        day = str(pd.Timestamp(d).date())
        xr = float(x.loc[d])
        info = {"carry": 0.0, "fee": 0.0}
        if prev_day is not None:
            o = float(opens.loc[d]) if opens is not None and d in opens.index and opens.loc[d] > 0 else prev_rate
            days = (pd.Timestamp(d) - pd.Timestamp(prev_day)).days
            diff = rates.differential(policy.base_currency, prev_day) if rates is not None else 0.0
            info = book.step(policy, pnl_usd=float(v) - prev_nav, open_rate=o, days=days, differential=diff)
        q = book.decide(policy, xr, day, capital)
        rows.append({"date": pd.Timestamp(d), "value": book.value(xr), "base": book.base, "usd": book.usd,
                     "carry": info["carry"], "fee": info["fee"], "order": q})
        prev_day, prev_nav, prev_rate = day, float(v), xr
    return pd.DataFrame(rows).set_index("date")


__all__ = ["DEFAULT_RATES", "HEDGE_REBALANCE", "POLICIES", "FxBook", "FxPolicy", "ShortRates", "apply_fx_policy"]
