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
from .instruments import IDEALPRO, fx_pair, ibkr_fx_commission

POLICIES = ("unhedged", "hedged")
#: ``never``: no currency trade at all, on either side (the capital stays in the base currency,
#: the foreign positions are financed in their currency: only their P&L is exposed)
HEDGE_REBALANCE = ("daily", "weekly", "monthly", "never")
#: the short rates used when a meta-portfolio names none (public series, % a year)
DEFAULT_RATES = {"USD": "fred:DFF", "EUR": "fred:ECBESTRVOLWGTTRMDMNRT"}
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={id}"
ECB_CSV = "https://data-api.ecb.europa.eu/service/data/{flow}/{key}?format=csvdata"
#: the part of the NAV (and at least ``min_order_notional``) the account's items may leave
#: unexplained before the live hedge order is withheld (``AccountSplit.unexplained``)
UNEXPLAINED_TOLERANCE = 0.001
#: seconds before a download of a short rate gives up (the cached copy is then used, if any)
HTTP_TIMEOUT = 10.0
PROJECT_URL = "https://github.com/paperswithbacktest/tradewind"


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

    def __init__(self, sources: dict | None = None, cache_dir: str | None = None, *, fetch=None, log=print,
                 timeout: float = HTTP_TIMEOUT):
        self.sources = {str(k).upper(): v for k, v in (sources or {}).items()}
        self.cache_dir = cache_dir
        self.timeout = float(timeout)
        self._fetch = fetch or (lambda url: _http_get(url, timeout=self.timeout))
        self.log = log
        self._series: dict[str, pd.Series | float | None] = {}
        self._errors: dict[str, str] = {}
        #: what went wrong with the rates, one line each, prefixed ``fx:`` (the session raises
        #: them as the ``fx_rates`` alert): never a silent zero
        self.warnings: list[str] = []

    def _warn(self, msg: str) -> None:
        msg = "fx: " + msg
        if msg not in self.warnings:
            self.warnings.append(msg)
            self.log(f"[fx] {msg[4:]}")

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
                    if not _parse_csv(text).size:
                        raise ValueError("no dated observation in the answer")
                    if path is not None:
                        with open(path, "w", encoding="utf-8") as fh:
                            fh.write(text)
                except Exception as exc:  # noqa: BLE001 - the cached copy, else no carry (and an alert)
                    text = None
                    self._errors[currency] = f"{type(exc).__name__}: {exc}"
                    use = "no cached copy"
                    if path is not None and os.path.exists(path):
                        use = f"the cached copy of {_dt.date.fromtimestamp(os.path.getmtime(path)).isoformat()} is used"
                    self._warn(f"short rate {currency} ({spec}) not downloaded from {url} "
                               f"({self._errors[currency]}); {use}")
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
        """``i_base - i_usd`` (% a year) as of ``on``; 0 when one is missing, with a warning
        (``warnings``, raised as an alert by the session) that names the series and the cause."""
        a, b = self.rate(base, on), self.rate("USD", on)
        if a is None or b is None:
            missing = [f"{c} ({self.spec(c)}" + (f": {self._errors[c]}" if c in self._errors else "") + ")"
                       for c, r in ((base.upper(), a), ("USD", b)) if r is None]
            self._warn(f"no short rate for {', '.join(missing)} up to {on}: the reference's carry is taken as 0 "
                       f"until the series is read (or give fx_rates in the meta-portfolio)")
            return 0.0
        return a - b


def user_agent() -> str:
    """The HTTP ``User-Agent`` of the downloads: the product, its version and where it lives
    (some public servers do not answer a bare name)."""
    from ... import __version__

    return f"tradewind/{__version__} (+{PROJECT_URL})"


def _http_get(url: str, timeout: float = HTTP_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept": "text/csv, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


# --------------------------------------------------------------------------------------------- #
# The account's split, read off its items                                                        #
# --------------------------------------------------------------------------------------------- #
@dataclass
class AccountSplit:
    """An account's base / dollar split, read **currency by currency** off its items (spec/13):
    ``base_units`` (``B``): the cash and accrued interest in the base currency, plus the
    hedge's units when the broker keeps the pair as a position; ``usd`` (``A``, the dollar
    exposure): the cash and accrued interest in every other currency, in USD, plus the
    positions' values (forex pairs that settle into the cash balances are not counted twice);
    ``unexplained``: the NAV minus ``B x + A`` (USD), what the items do not account for (marks
    that differ, an instrument whose value the items miss); ``notes``: why, when known."""

    base_units: float
    usd: float
    unexplained: float
    by_currency: dict[str, float] = field(default_factory=dict)     # USD value of each currency's items
    notes: list[str] = field(default_factory=list)


def account_split(snap, base: str, rate: float, *, marks: dict[str, float] | None = None, pair: str | None = None,
                  hedge_units: float = 0.0, hedge_in_base_cash: bool = False) -> AccountSplit:
    """The split of ``snap`` (a ``brokers.Snapshot``) for the base currency ``base`` at
    ``rate`` (USD per unit of base). ``pair`` / ``hedge_units``: the hedge's pair and the units
    of it the hedge holds (kept out of the dollar exposure: they are base currency).
    ``marks``: prices for the positions the account does not value itself. Without items by
    currency (a broker that gives only its NAV and its base cash) the dollar exposure is the
    rest of the NAV, as before."""
    base = base.upper()
    nav = float(snap.nav)
    balances = dict(getattr(snap, "balances", None) or {})
    accrued = dict(getattr(snap, "accrued", None) or {})
    notes: list[str] = []
    if not balances:
        b = float(getattr(snap, "base_cash", 0.0) or 0.0) + (0.0 if hedge_in_base_cash else hedge_units)
        note = "no cash balances by currency: the dollar exposure is the NAV less the base currency's cash"
        return AccountSplit(b, nav - b * rate, 0.0, {}, [note])
    usd_per = dict(getattr(snap, "fx_rates", None) or {})
    usd_per["USD"] = 1.0
    usd_per[base] = rate
    by_currency: dict[str, float] = {}
    for cur in sorted(set(balances) | set(accrued)):
        units = float(balances.get(cur, 0.0)) + float(accrued.get(cur, 0.0))
        if not units:
            continue
        r = usd_per.get(cur)
        if r is None or not (r > 0):
            notes.append(f"{cur} {units:+,.2f}: no rate to USD")
            continue
        by_currency[cur] = units * r
    base_units = float(balances.get(base, 0.0)) + float(accrued.get(base, 0.0))
    usd = sum(v for cur, v in by_currency.items() if cur != base)
    values = getattr(snap, "values", None)
    in_balances = bool(getattr(snap, "fx_in_balances", False))
    for sym, q in sorted((getattr(snap, "positions", None) or {}).items()):
        if in_balances and fx_pair(sym) is not None:
            continue                                    # the pair is the cash balances already
        if sym == pair and hedge_units:
            q -= hedge_units                            # the hedge: base units, below
            if abs(q) < 1e-9:
                continue
            v = None
            if values is not None and sym in values and snap.positions[sym]:
                v = values[sym] * q / snap.positions[sym]
        else:
            v = values.get(sym) if values is not None else None
        if v is None and marks and marks.get(sym):
            v = q * float(marks[sym])
        if v is None:
            notes.append(f"{sym} {q:+g}: no value")
            continue
        usd += v
        by_currency["USD"] = by_currency.get("USD", 0.0) + v
    if not hedge_in_base_cash:
        base_units += hedge_units
    return AccountSplit(base_units, usd, nav - base_units * rate - usd, by_currency, notes)


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
        if self.rebalance == "never" and self.policy != "hedged":
            raise ValueError("fx_hedge_rebalance never goes with fx_policy hedged (the capital stays in the base "
                             "currency, nothing is ever converted); unhedged converts the capital to USD")
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
    def trades_never(self) -> bool:
        """``fx_hedge_rebalance: never``: no currency order, on either side, ever."""
        return self.rebalance == "never"

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
        if not self.active or not (rate > 0) or self.trades_never:
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
    and the date of the last decision (``last``). ``outside``: base units counted in ``base``
    that are not the portfolio's capital (an account's cash kept for other uses): they are part
    of the value but earn no carry. Plain data: it goes into ``state.json``."""

    base: float = 0.0
    usd: float = 0.0
    pending: float = 0.0
    last: str | None = None
    outside: float = 0.0

    def value(self, rate: float) -> float:
        return self.base + self.usd / rate

    @property
    def carried(self) -> float:
        """The base units the carry applies to: the capital's part held in the base currency."""
        return self.base - self.outside

    def to_dict(self) -> dict:
        return {"base": self.base, "usd": self.usd, "pending": self.pending, "last": self.last,
                "outside": self.outside}

    @classmethod
    def from_dict(cls, d: dict) -> FxBook:
        return cls(float(d.get("base", 0.0)), float(d.get("usd", 0.0)), float(d.get("pending", 0.0)),
                   d.get("last"), float(d.get("outside", 0.0)))

    def step(self, policy: FxPolicy, *, pnl_usd: float, open_rate: float, days: float,
             differential: float) -> dict:
        """One session: carry on the capital's base units (``base - outside``) over ``days`` at
        ``differential`` (``i_base - i_usd``, % a year, ACT/360), the pending order filled at
        ``open_rate``, the strategies' P&L added to ``usd``. Returns ``{carry (base), fee (USD),
        filled (base units), fill_price}``."""
        carry = self.carried * differential / 100.0 * days / 360.0
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
        if self.last is None or q or (policy.policy == "hedged" and not policy.trades_never
                                      and due_for_reset(policy.rebalance, self.last, day)):
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


__all__ = ["DEFAULT_RATES", "HEDGE_REBALANCE", "POLICIES", "AccountSplit", "FxBook", "FxPolicy", "ShortRates",
           "account_split", "apply_fx_policy", "user_agent"]
