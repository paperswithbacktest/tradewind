"""Fresh closes for the daily run: the PWB price datasets completed up to the last close.

The PWB daily price datasets are refreshed once a month, and the runner refuses to propose on
data more than a few days old. With ``--fresh-prices alphavantage`` each strategy's price
loads are completed, in its own process, with the bars AlphaVantage has after the dataset's
last day -- for the symbols the strategies ask for, nothing else.

How a series is continued (the rule of the data pipeline's vendor tail):

* the bars come from ``TIME_SERIES_DAILY_ADJUSTED`` (``outputsize=compact``, the last 100
  sessions) for stocks and ETFs, ``FX_DAILY`` for currency pairs, ``DIGITAL_CURRENCY_DAILY``
  for crypto. A share class written with a dot (``PBR.A``) is asked as AlphaVantage writes it
  (``PBR-A``) first. Commodities have no source here; a bond or an index has one only through
  a public daily series of the same quantity (``public_prices.py``: ``US10Y`` from FRED's
  ``DGS10``, ``VIX`` from ``VIXCLS``; off with ``fresh_public_series: false``) or through
  its *proxy* ETF (below);
* the chain factor ``k`` is the ratio of the two adjusted closes on the *anchor*, the last
  common day before the dataset's last bar (that bar may be a partial one: the monthly source
  is sometimes downloaded during the session). The dataset's history up to the anchor is
  divided by ``k``, as a whole: its returns are kept, and a dividend or a split between the
  two cut-offs is chained the way an adjusted series takes a new event -- nothing is
  "un-adjusted" at the junction. The bars after the anchor are AlphaVantage's own, so the
  last close is the price the account trades at (a split since the dataset's cut-off would
  otherwise size every order in pre-split shares);
* the check: ``k`` must agree to the tolerance of the symbol's kind (``DEFAULT_TOLERANCE``:
  0.5 % for stocks, ETFs, crypto and the major currencies, 1.5 % for the other currencies,
  whose two sources fix at different hours; ``fresh_price_tolerance`` in the meta-portfolio
  changes it, by kind or by symbol) on the anchor and the common days before it. When it does
  not, the two sources do not quote the same series (a reused ticker, a split one of them
  missed) and the symbol is refused;
* the dataset's last bar and everything after the anchor are replaced by AlphaVantage's
  bars; bars after the last completed US session are never taken.

**Indices through a proxy ETF.** ``Indices-Daily-Price`` has no daily source, but an index
with a proxy (``proxies``: by default the ETF substitutes of the meta-portfolio, ``SPX`` ->
``SPY``, ``VIX`` -> ``VIXY``) is continued with the ETF's adjusted returns chained on the
index's own level at the anchor (``chain_proxy``): the history keeps its levels, the new bars
are ``level(anchor) x ETF(day) / ETF(anchor)``. An ETF that *tracks* its index
(``TRACKING_PROXIES``: SPY, IVV, VOO for SPX, QQQ for NDX, IWM for RUT, DIA for the Dow) is
checked like a splice (tolerance ``index``, 1 %: the ETF's returns carry its dividends). An
ETF that does not -- VIXY holds VIX futures, its level and its returns are not the VIX's
(roughly half its daily moves, and the roll's decay) -- is chained only for the indices listed
in ``approximate`` (``fresh_index_proxy_approximate``), unchecked; otherwise the index stays
stale with that reason: a strategy that reads the VIX's *level* (a 15 / 30 / 50 gate) would
be fed a level that drifts away from it.

A symbol is *live* when its last bar is within ``LIVE_WINDOW_DAYS`` of the session asked for
(older: a delisted name, left as it is). A live symbol that still ends before the market's
last close after the top-up is *stale*, with a cause (``CAUSES``): delisted or listed anew
according to AlphaVantage's ``LISTING_STATUS`` (read once a session, only when a stock is
stale), the two sources disagree, AlphaVantage does not know it or has no recent bar (a
silent source), no source for the dataset. With ``strict`` (the meta-portfolio's
``stale_symbol_policy: block``) the strategy is stopped (``StalePrices``) and the run proposes
nothing; otherwise its rows are left as they are (the strategy runs on them, the symbol's
price frozen at its last bar) and the run decides what to do with the symbol
(``runner.py``).

Responses are cached on disk per session (``CACHE/DATE/FUNCTION/SYMBOL.json``): a second
strategy, or a rerun after an error, does not call the API again for a symbol already read.
The key is read from ``ALPHAVANTAGE_API_KEY`` and is never written to a file or a message;
the request rate is ``ALPHAVANTAGE_RPM`` per minute (75 by default, the smallest premium
plan). An empty answer (``{}``: seen on 2026-11-20 for TLT, which answered normally a few
minutes later -- a burst the API drops rather than a symbol it does not know) is asked again
after 5, 10 then 20 seconds before the symbol is given up.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import math
import numbers
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import pandas as pd

KEY_ENV = "ALPHAVANTAGE_API_KEY"
RPM_ENV = "ALPHAVANTAGE_RPM"
FUNCTION_ENV = "ALPHAVANTAGE_EQUITY_FUNCTION"   # TIME_SERIES_DAILY_ADJUSTED (default) or TIME_SERIES_DAILY
URL = "https://www.alphavantage.co/query"

TOLERANCE = 0.005           # the chain factor must agree to this on the common days (stocks)
CHECK_DAYS = 5              # common days, up to the anchor, the factor is checked on
LIVE_WINDOW_DAYS = 45       # a series ending earlier than this before the session is not live
CLOSE_TIME = _dt.time(16, 30)   # New York: a session's daily bar is taken after this time

KIND = {
    "Stocks-Daily-Price": "equity",
    "ETFs-Daily-Price": "equity",
    "Forex-Daily-Price": "fx",
    "Cryptocurrencies-Daily-Price": "crypto",
}
INDEX_REPO = "Indices-Daily-Price"
NOT_COVERED = (INDEX_REPO, "Commodities-Daily-Price", "Bonds-Daily-Price")
PRICE_REPOS = tuple(KIND) + NOT_COVERED

#: the largest move of the chain factor accepted on the checked days, by kind (``fx``: the
#: major currencies, ``fx_minor``: the others; ``index``: an index against its tracking ETF)
DEFAULT_TOLERANCE = {"equity": TOLERANCE, "fx": TOLERANCE, "fx_minor": 0.015, "crypto": TOLERANCE, "index": 0.01}
#: the currencies whose pair against USD counts as major for the tolerance
FX_MAJORS = frozenset({"EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "NOK", "SEK", "DKK"})
#: ETFs that track their index: chained and checked; any other proxy is an approximation
TRACKING_PROXIES = {"SPX": ("SPY", "IVV", "VOO", "SPLG"), "NDX": ("QQQ", "QQQM"), "RUT": ("IWM",),
                    "DJI": ("DIA",), "INDU": ("DIA",)}
#: why a symbol stays stale (``FreshPrices.causes``)
CAUSES = {
    "delisted": "delisted (AlphaVantage LISTING_STATUS)",
    "relisted": "the ticker was listed anew (AlphaVantage LISTING_STATUS): another security",
    "diverged": "the two sources disagree",
    "unknown": "unknown to AlphaVantage",
    "silent": "AlphaVantage has no bar after it either (a silent source: halted, renamed?)",
    "no_source": "no fresh source for this dataset",
    "fetch_error": "AlphaVantage could not be read",
    "lagged": "the public daily source publishes a day's value the next business day (public_prices.py)",
}


def tolerance_for(kind: str, symbol: str, table: dict | None = None) -> float:
    """The tolerance of ``symbol``: the table's entry for the symbol, else for its kind
    (``fx`` split into the major currencies and ``fx_minor``), else the default."""
    t = dict(DEFAULT_TOLERANCE)
    t.update(table or {})
    if symbol in t:
        return float(t[symbol])
    if kind == "fx":
        cur = symbol[:3] if symbol[3:] == "USD" else symbol[3:]
        return float(t["fx"] if cur in FX_MAJORS else t["fx_minor"])
    return float(t.get(kind, TOLERANCE))


class StalePrices(RuntimeError):
    """Live symbols still behind the market's last close after the top-up."""


class FetchError(RuntimeError):
    """AlphaVantage could not be read for a symbol (network, throttle, refused endpoint)."""


def last_close_session(now: _dt.datetime | None = None) -> str:
    """The last weekday session whose close has passed in New York (holidays: the data's
    business, see ``FreshPrices.market_close``)."""
    try:
        from zoneinfo import ZoneInfo

        ny = (now or _dt.datetime.now(_dt.timezone.utc)).astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 - no tz database: UTC-5 is close enough for a cut-off
        ny = (now or _dt.datetime.now(_dt.timezone.utc)).astimezone(_dt.timezone(_dt.timedelta(hours=-5)))
    day = ny.date()
    if ny.time() < CLOSE_TIME:
        day -= _dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= _dt.timedelta(days=1)
    return day.isoformat()


# --------------------------------------------------------------------------------------------- #
# The API                                                                                       #
# --------------------------------------------------------------------------------------------- #
def _http_get(url: str, timeout: float = 30.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _number(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return math.nan


def parse_daily(kind: str, payload: dict) -> pd.DataFrame | None:
    """``date, open, high, low, close, adj_close, volume`` (dates as ISO strings, oldest
    first) from an AlphaVantage daily payload; None when it holds no series."""
    series_key = next((k for k in payload if k.startswith("Time Series")), None)
    if series_key is None:
        return None
    rows = []
    for day, bar in payload[series_key].items():
        def pick(name: str, bar=bar) -> float:
            keys = [k for k in bar if name in k.lower()]
            usd = [k for k in keys if "(usd)" in k.lower()]
            return _number(bar[(usd or keys)[0]]) if keys else math.nan
        raw = [k for k in bar if "close" in k.lower() and "adjusted" not in k.lower()]
        usd = [k for k in raw if "(usd)" in k.lower()]
        close = _number(bar[(usd or raw)[0]]) if raw else math.nan
        # TIME_SERIES_DAILY, FX_DAILY and crypto have no adjusted close: the close is it
        adj = _number(bar["5. adjusted close"]) if "5. adjusted close" in bar else close
        rows.append({"date": day, "open": pick("open"), "high": pick("high"), "low": pick("low"),
                     "close": close, "adj_close": adj, "volume": pick("volume")})
    if not rows:
        return None
    frame = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    return frame


class AlphaVantage:
    """A daily-bars client with a per-session disk cache, a request rate and retries.

    ``get``: a function ``url -> text`` (tests pass a fake one); ``sleep`` and ``clock`` too.
    """

    def __init__(self, cache_dir: str, session: str, *, key: str | None = None, rpm: float | None = None,
                 equity_function: str | None = None, get: Callable[[str], str] | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 retries: int = 4, throttle_rounds: int = 5, empty_retries: int = 3, empty_backoff: float = 5.0,
                 log=print, grace: dict | None = None, wall: Callable[[], float] = time.time):
        self.key = key if key is not None else os.environ.get(KEY_ENV, "")
        self.rpm = float(rpm or os.environ.get(RPM_ENV) or 75)
        self.equity_function = equity_function or os.environ.get(FUNCTION_ENV) or "TIME_SERIES_DAILY_ADJUSTED"
        self.cache_dir = os.path.join(os.path.expanduser(cache_dir), session)
        self.get = get or _http_get
        self.sleep, self.clock = sleep, clock
        self.retries, self.throttle_rounds = retries, throttle_rounds
        self.empty_retries, self.empty_backoff = empty_retries, empty_backoff
        self.log = log
        self.calls = 0
        self._last_call: float | None = None
        self.session = session
        self.grace = dict(grace or {})          # minutes after the close before the day's bar is final
        self.wall = wall                        # the time a file is written at (epoch seconds)

    # -- plumbing ----------------------------------------------------------------------------
    def _redact(self, text: str) -> str:
        return text.replace(self.key, "***") if self.key else text

    def _wait_turn(self) -> None:
        gap = 60.0 / self.rpm
        if self._last_call is not None:
            wait = self._last_call + gap - self.clock()
            if wait > 0:
                self.sleep(wait)
        self._last_call = self.clock()

    def _request(self, params: dict) -> dict:
        if not self.key:
            raise FetchError(f"no AlphaVantage key: set {KEY_ENV}")
        url = URL + "?" + urllib.parse.urlencode({**params, "apikey": self.key})
        attempt = throttled = empty = 0
        while True:
            self._wait_turn()
            self.calls += 1
            try:
                text = self.get(url)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                attempt += 1
                if attempt > self.retries:
                    raise FetchError(self._redact(f"{type(exc).__name__}: {exc}")) from None
                self.sleep(2.0 ** attempt)
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                attempt += 1
                if attempt > self.retries:
                    raise FetchError("the answer is not JSON") from None
                self.sleep(2.0 ** attempt)
                continue
            if not payload or not isinstance(payload, dict):
                # ``{}`` (or ``[]``): a burst dropped by the API, not an unknown symbol --
                # the same request answers a few seconds later
                empty += 1
                if empty > self.empty_retries:
                    return {}
                wait = self.empty_backoff * 2.0 ** (empty - 1)
                self.log(f"[prices] AlphaVantage gave an empty answer for {params.get('symbol') or params.get('from_symbol', '')}"
                         f"{params.get('to_symbol', '')}: asked again in {wait:g} s")
                self.sleep(wait)
                continue
            note = " ".join(str(payload.get(k, "")) for k in ("Note", "Information")).lower()
            if "premium" in note:
                raise FetchError(f"{params['function']} is not in this AlphaVantage plan")
            if "frequency" in note or "rate limit" in note or "requests per" in note:
                throttled += 1
                if throttled > self.throttle_rounds:
                    raise FetchError("AlphaVantage keeps throttling the requests")
                self.sleep(60.0)
                continue
            return payload

    def _params(self, kind: str, symbol: str) -> dict | None:
        if kind == "equity":
            return {"function": self.equity_function, "symbol": symbol, "outputsize": "compact"}
        if kind == "fx":
            if not re.fullmatch(r"[A-Z]{6}", symbol):
                return None
            return {"function": "FX_DAILY", "from_symbol": symbol[:3], "to_symbol": symbol[3:],
                    "outputsize": "compact"}
        if kind == "crypto":
            base = symbol[:-3] if symbol.endswith("USD") and len(symbol) > 3 else symbol
            return {"function": "DIGITAL_CURRENCY_DAILY", "symbol": base, "market": "USD"}
        return None

    # -- the bars ----------------------------------------------------------------------------
    def _path(self, kind: str, symbol: str) -> str | None:
        params = self._params(kind, symbol)
        if params is None:
            return None
        return os.path.join(self.cache_dir, params["function"], re.sub(r"[^A-Za-z0-9._-]", "_", symbol) + ".json")

    def _final(self, kind: str, frame: pd.DataFrame | None, fetched: float) -> pd.DataFrame | None:
        """``frame`` without the session's bar when it was read before that bar is final
        (``ib_prices.bar_complete_at``: the close plus the grace)."""
        from .ib_prices import drop_unfinished

        return drop_unfinished(frame, "equity" if kind == "equity" else kind, self.session, fetched, self.grace)

    def cached(self, kind: str, symbol: str) -> pd.DataFrame | None:
        """The bars already in the cache for ``symbol``, or None; never asks the API."""
        path = self._path(kind, symbol)
        if path is None or not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            return None
        return None if "Error Message" in payload else self._final(kind, parse_daily(kind, payload),
                                                                    os.path.getmtime(path))

    def daily(self, kind: str, symbol: str) -> pd.DataFrame | None:
        """The symbol's recent daily bars, or None when AlphaVantage does not know it.
        Raises ``FetchError`` when it could not be read (nothing is cached then). A share
        class written with a dot (``PBR.A``) is asked as ``PBR-A`` first (AlphaVantage's
        way), then as it is. The session's bar is kept only when the answer was read after
        it is final (the close plus the grace); a cached answer read before is asked again
        once that time has passed."""
        from .ib_prices import bar_complete_at

        params = self._params(kind, symbol)
        if params is None:
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", symbol)
        path = os.path.join(self.cache_dir, params["function"], safe + ".json")
        done = bar_complete_at(kind, self.session, self.grace) if kind in ("equity", "fx") else None
        if done is not None and os.path.exists(path) and os.path.getmtime(path) < done <= self.wall():
            os.remove(path)                         # read before the bar was final: ask again
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        else:
            payload = {}
            names = [symbol.replace(".", "-"), symbol] if kind == "equity" and "." in symbol else [symbol]
            for name in names:
                payload = self._request(dict(params, symbol=name) if kind == "equity" else params)
                if "Error Message" not in payload:
                    break
            if not payload:
                raise FetchError(f"empty answer for {symbol} after {self.empty_retries + 1} tries")
            if "Error Message" not in payload and parse_daily(kind, payload) is None:
                raise FetchError(f"unexpected answer for {symbol}: {sorted(payload)[:3]}")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
            stamp = self.wall()
            os.utime(path, (stamp, stamp))          # the time it was read: the day's bar rule
        if "Error Message" in payload:
            return None
        return self._final(kind, parse_daily(kind, payload), os.path.getmtime(path))

    def listing(self) -> dict[str, dict]:
        """AlphaVantage's ``LISTING_STATUS``, active and delisted, ``{symbol: row}`` (a delisted
        row wins over an active one only when no active row has the symbol). Read once a
        session (cached); ``{}`` when it cannot be read -- it only explains a stale symbol."""
        out: dict[str, dict] = {}
        for state in ("delisted", "active"):
            path = os.path.join(self.cache_dir, "LISTING_STATUS", state + ".csv")
            text = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            elif self.key:
                url = URL + "?" + urllib.parse.urlencode({"function": "LISTING_STATUS", "state": state,
                                                          "apikey": self.key})
                self._wait_turn()
                self.calls += 1
                try:
                    text = self.get(url)
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    self.log(self._redact(f"[prices] LISTING_STATUS {state} not read: {exc}"))
                    continue
                if not text.startswith("symbol,"):
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(tmp, path)
            for row in csv.DictReader(io.StringIO(text)):
                sym = row.get("symbol") or ""
                if sym and (state == "active" or sym not in out):
                    out[sym] = dict(row, state=state)
        return out


# --------------------------------------------------------------------------------------------- #
# The splice                                                                                    #
# --------------------------------------------------------------------------------------------- #
@dataclass
class Splice:
    symbol: str
    status: str                     # "filled", "refused", "unknown", "current"
    last_before: str = ""
    last_after: str = ""
    bars: int = 0
    anchor: str = ""
    factor: float = math.nan        # PWB adjusted close / AlphaVantage adjusted close on the anchor
    spread: float = math.nan        # largest relative gap of the factor on the checked common days
    last_bar_gap: float = math.nan  # the dataset's last bar against AlphaVantage's, once chained
    reason: str = ""
    source: str = ""                # an index: the proxy ETF whose returns continue it
    approximate: bool = False       # an index chained on a proxy that does not track it (unchecked)


def _like(value, template):
    """``value`` (an ISO date) in the type of the frame's date column."""
    if isinstance(template, pd.Timestamp):
        return pd.Timestamp(value)
    if isinstance(template, _dt.datetime):
        return _dt.datetime.fromisoformat(value)
    if isinstance(template, _dt.date):
        return _dt.date.fromisoformat(value)
    return value


def splice(rows: pd.DataFrame, fresh: pd.DataFrame, session: str, *, tolerance: float = TOLERANCE,
           check_days: int = CHECK_DAYS) -> tuple[pd.DataFrame, Splice]:
    """One symbol's dataset rows continued with ``fresh`` (AlphaVantage, ``parse_daily``).

    ``rows`` is adjusted (``close`` is the adjusted close) unless it has an ``adj_close``
    column (a load with ``adjust=False``). Returns the new rows and what was done.
    """
    symbol = str(rows["symbol"].iloc[0])
    rows = rows.copy()
    iso = rows["date"].map(lambda d: str(d)[:10])
    rows = rows.assign(_iso=iso).sort_values("_iso")
    level_col = "adj_close" if "adj_close" in rows.columns else "close"
    last_before = str(rows["_iso"].iloc[-1])
    out = Splice(symbol, "current", last_before=last_before, last_after=last_before)
    fresh = fresh[fresh["date"] <= session]
    fresh = fresh[(fresh["adj_close"] > 0) & (fresh["close"] > 0)]
    if fresh.empty or fresh["date"].iloc[-1] <= last_before:
        return rows.drop(columns="_iso"), out
    pwb_level = rows.set_index("_iso")[level_col]
    pwb_level = pwb_level[~pwb_level.index.duplicated(keep="last")]
    av = fresh.set_index("date")
    common = [d for d in av.index if d in pwb_level.index and pwb_level[d] > 0]
    if not common:
        out.status, out.reason = "refused", "no common day with the dataset"
        return rows.drop(columns="_iso"), out
    before_last = [d for d in common if d < last_before]
    anchor = before_last[-1] if before_last else common[-1]
    checked = [d for d in common if d <= anchor][-check_days:]
    factors = [float(pwb_level[d] / av.loc[d, "adj_close"]) for d in checked]
    k = factors[-1]
    out.anchor, out.factor = anchor, k
    out.spread = max(abs(f / k - 1.0) for f in factors)
    if last_before in av.index and last_before != anchor:
        out.last_bar_gap = float(pwb_level[last_before] / (k * av.loc[last_before, "adj_close"]) - 1.0)
    if out.spread > tolerance:
        out.status = "refused"
        out.reason = (f"the two sources disagree: chain factor {k:.6f} moves by {out.spread:.2%} "
                      f"over {len(checked)} common days (tolerance {tolerance:.2%})")
        return rows.drop(columns="_iso"), out
    new = av[av.index > anchor]
    template = rows.iloc[-1]
    adjusted = level_col == "close"
    records = []
    for day, bar in new.iterrows():
        rec = {}
        for col in rows.columns:
            if col == "_iso":
                continue
            v = template[col]
            rec[col] = math.nan if isinstance(v, numbers.Number) and not isinstance(v, bool) else v
        ratio = bar["adj_close"] / bar["close"]
        scale = ratio if adjusted else 1.0
        for col in ("open", "high", "low", "close"):
            if col in rows.columns:
                rec[col] = float(bar[col]) * scale
        if not adjusted:
            rec["adj_close"] = float(bar["adj_close"])
        if "volume" in rows.columns:
            rec["volume"] = float(bar["volume"])
        rec["symbol"] = template["symbol"]
        rec["date"] = _like(str(day), template["date"])
        records.append(rec)
    kept = rows[rows["_iso"] <= anchor].drop(columns="_iso")
    for col in (("open", "high", "low", "close") if adjusted else ("adj_close",)):
        if col in kept.columns:
            kept[col] = kept[col] / k            # the history onto AlphaVantage's adjustment
    added = pd.DataFrame(records, columns=kept.columns)
    for col in ("open", "high", "low", "close", "adj_close", "volume"):
        if col not in added:
            continue
        added[col] = pd.to_numeric(added[col], errors="coerce")
        if pd.api.types.is_integer_dtype(kept[col]):       # an integer volume stays one (FX: 0)
            added[col] = added[col].fillna(0).round().astype(kept[col].dtype)
    joined = pd.concat([kept, added], ignore_index=True)
    out.status, out.bars, out.last_after = "filled", len(added), str(new.index[-1])
    return joined, out


def chain_proxy(rows: pd.DataFrame, proxy: pd.DataFrame, session: str, *, proxy_symbol: str,
                tolerance: float = DEFAULT_TOLERANCE["index"], check_days: int = CHECK_DAYS,
                checked: bool = True) -> tuple[pd.DataFrame, Splice]:
    """An index's dataset rows continued with ``proxy``'s returns (an ETF's AlphaVantage bars,
    ``parse_daily``), chained on the index's level at the anchor.

    The anchor is chosen as in ``splice`` (the last common day before the index's last bar);
    the history up to it keeps its levels, every later bar is the ETF's bar times
    ``level(anchor) / ETF adjusted close(anchor)`` (open, high and low scaled like the close).
    ``checked``: the ratio must not move by more than ``tolerance`` over the checked common
    days (an ETF that tracks the index); unchecked, the bars are an approximation
    (``Splice.approximate``)."""
    symbol = str(rows["symbol"].iloc[0])
    rows = rows.copy()
    rows = rows.assign(_iso=rows["date"].map(lambda d: str(d)[:10])).sort_values("_iso")
    level_col = "adj_close" if "adj_close" in rows.columns else "close"
    last_before = str(rows["_iso"].iloc[-1])
    out = Splice(symbol, "current", last_before=last_before, last_after=last_before, source=proxy_symbol,
                 approximate=not checked)
    proxy = proxy[proxy["date"] <= session]
    proxy = proxy[(proxy["adj_close"] > 0) & (proxy["close"] > 0)]
    if proxy.empty or proxy["date"].iloc[-1] <= last_before:
        return rows.drop(columns="_iso"), out
    level = rows.set_index("_iso")[level_col]
    level = level[~level.index.duplicated(keep="last")]
    av = proxy.set_index("date")
    common = [d for d in av.index if d in level.index and level[d] > 0]
    if not common:
        out.status, out.reason = "refused", f"no common day with {proxy_symbol}"
        return rows.drop(columns="_iso"), out
    before_last = [d for d in common if d < last_before]
    anchor = before_last[-1] if before_last else common[-1]
    days = [d for d in common if d <= anchor][-check_days:]
    ratios = [float(level[d] / av.loc[d, "adj_close"]) for d in days]
    k = ratios[-1]
    out.anchor, out.factor = anchor, k
    out.spread = max(abs(r / k - 1.0) for r in ratios)
    if checked and out.spread > tolerance:
        out.status = "refused"
        out.reason = (f"{symbol} and {proxy_symbol} disagree: their ratio moves by {out.spread:.2%} over "
                      f"{len(days)} common days (tolerance {tolerance:.2%})")
        return rows.drop(columns="_iso"), out
    new = av[av.index > anchor]
    template = rows.iloc[-1]
    records = []
    for day, bar in new.iterrows():
        rec = {}
        for col in rows.columns:
            if col == "_iso":
                continue
            v = template[col]
            rec[col] = math.nan if isinstance(v, numbers.Number) and not isinstance(v, bool) else v
        scale = k * bar["adj_close"] / bar["close"]
        for col in ("open", "high", "low", "close"):
            if col in rows.columns:
                rec[col] = float(bar[col]) * scale
        if "adj_close" in rows.columns:
            rec["adj_close"] = float(bar["close"]) * scale
        if "volume" in rows.columns:
            rec["volume"] = 0.0                     # an index has no volume of its own
        rec["symbol"] = template["symbol"]
        rec["date"] = _like(str(day), template["date"])
        records.append(rec)
    kept = rows[rows["_iso"] <= anchor].drop(columns="_iso")
    added = pd.DataFrame(records, columns=kept.columns)
    for col in ("open", "high", "low", "close", "adj_close", "volume"):
        if col not in added:
            continue
        added[col] = pd.to_numeric(added[col], errors="coerce")
        if pd.api.types.is_integer_dtype(kept[col]):
            added[col] = added[col].fillna(0).round().astype(kept[col].dtype)
    joined = pd.concat([kept, added], ignore_index=True)
    out.status, out.bars, out.last_after = "filled", len(added), str(new.index[-1])
    return joined, out


# --------------------------------------------------------------------------------------------- #
# The top-up of whole loads                                                                     #
# --------------------------------------------------------------------------------------------- #
@dataclass
class FreshPrices:
    """Completes the price frames ``pwb_toolbox.datasets.load_dataset`` returns.

    ``strict``: a live symbol still stale stops the load (``StalePrices``); otherwise its rows
    are left as they are and it is listed (``stale``, ``stale_info``). ``tolerance``: by kind
    or symbol (``tolerance_for``). ``proxies``: ``{index: ETF}`` continued with the ETF's
    returns; ``approximate``: the indices whose proxy is chained although it does not track
    them (``TRACKING_PROXIES``). ``ib``: the bars the run collected from IB
    (``ib_prices.IBPriceCache``), asked first for stocks, ETFs, forex and the indices it has;
    ``av``: AlphaVantage is asked for what IB does not give (``False``: nothing else)."""

    client: AlphaVantage
    session: str
    log: Callable = print
    strict: bool = True
    tolerance: dict = field(default_factory=dict)
    proxies: dict = field(default_factory=dict)
    approximate: set = field(default_factory=set)
    splices: dict = field(default_factory=dict)        # symbol -> Splice
    stale: dict = field(default_factory=dict)          # symbol -> why
    stale_info: dict = field(default_factory=dict)     # symbol -> {dataset, last, cause, reason}
    not_covered: dict = field(default_factory=dict)    # symbol -> dataset
    inactive: set = field(default_factory=set)
    ib: object | None = None                           # ib_prices.IBPriceCache
    av: bool = True
    kraken: object | None = None                       # kraken_prices.KrakenPriceCache (crypto)
    served: dict = field(default_factory=dict)         # symbol -> "ibkr" | "alphavantage"
    ib_fallbacks: dict = field(default_factory=dict)   # symbol -> why IB did not serve it
    crosscheck: dict = field(default_factory=dict)     # symbol -> {date, ib, av, gap}: both sources at hand
    fred: object | None = None                         # public_prices.FredClient: public daily signal series
    public: dict = field(default_factory=dict)         # symbol -> public_prices.PublicSplice
    _market: str | None = None
    _memo: dict = field(default_factory=dict)
    _listing: dict | None = None

    @classmethod
    def from_env(cls, cache_dir: str, session: str | None = None, log=print, *, strict: bool = True,
                 tolerance: dict | None = None, proxies: dict | None = None, approximate=(),
                 sources=("alphavantage",), grace: dict | None = None, public: bool = True,
                 **client_kwargs) -> FreshPrices:
        from .ib_prices import IBPriceCache
        from .public_prices import FredClient

        session = session or last_close_session()
        sources = tuple(sources or ("alphavantage",))
        ib = IBPriceCache(cache_dir, session, grace) if "ibkr" in sources else None
        kraken = None
        if "kraken" in sources:
            from .kraken_prices import KrakenPriceCache

            kraken = KrakenPriceCache(cache_dir, session, float((grace or {}).get("crypto", 5.0)))
        av_grace = {k: v for k, v in (grace or {}).items() if not k.startswith("av_")}
        av_grace.update({k[3:]: v for k, v in (grace or {}).items() if k.startswith("av_")})
        return cls(AlphaVantage(cache_dir, session, log=log, grace=av_grace, **client_kwargs), session, log=log,
                   strict=strict, tolerance=dict(tolerance or {}), proxies=dict(proxies or {}),
                   approximate=set(approximate), ib=ib, av="alphavantage" in sources, kraken=kraken,
                   fred=FredClient(cache_dir, session, log=log) if public else None)

    def _ib(self, kind: str, symbol: str) -> pd.DataFrame | None:
        """IB's bars for ``symbol`` when the run collected them and they reach the session."""
        if self.ib is None:
            return None
        frame, why = self.ib.bars(kind, symbol)  # type: ignore[attr-defined]
        if frame is None:
            self.ib_fallbacks[symbol] = why
        return frame

    def _cross(self, kind: str, symbol: str, ib: pd.DataFrame) -> None:
        """IB's last close against AlphaVantage's when AlphaVantage's answer is already in the
        cache (never asked for it): logged, kept in the summary."""
        av = self.client.cached(kind, symbol) if kind in ("equity", "fx") else None
        if av is None or av.empty:
            return
        common = sorted(set(ib["date"]) & set(av["date"]))
        if not common:
            return
        day = common[-1]
        a = float(av.loc[av["date"] == day, "close"].iloc[-1])
        b = float(ib.loc[ib["date"] == day, "close"].iloc[-1])
        if not (a > 0 and b > 0):
            return
        gap = b / a - 1.0
        self.crosscheck[symbol] = {"date": day, "ib": b, "av": a, "gap": gap}
        if abs(gap) > TOLERANCE:
            self.log(f"[prices] {symbol}: IB {b:g} against AlphaVantage {a:g} on {day} ({gap:+.2%})")

    def _kraken(self, kind: str, symbol: str) -> pd.DataFrame | None:
        """Kraken's daily bars for a crypto ``symbol`` when the run collected them."""
        if self.kraken is None or kind != "crypto":
            return None
        frame, why = self.kraken.bars(kind, symbol)  # type: ignore[attr-defined]
        if frame is None:
            self.ib_fallbacks[symbol] = why
        return frame

    def _bars(self, kind: str, symbol: str) -> pd.DataFrame | None:
        if (kind, symbol) not in self._memo:
            bars = self._ib(kind, symbol) if kind in ("equity", "fx") else self._kraken(kind, symbol)
            if bars is not None:
                if kind == "crypto":
                    self.served[symbol] = "kraken"
                else:
                    self.served[symbol] = "ibkr"
                    self._cross(kind, symbol, bars)
            elif not self.av:
                raise FetchError(f"no listed source served it ({self.ib_fallbacks.get(symbol, 'not asked')}) and "
                                 "alphavantage is not in --fresh-prices")
            else:
                bars = self.client.daily(kind, symbol)
                if bars is not None:
                    self.served[symbol] = "alphavantage"
            self._memo[(kind, symbol)] = bars
        return self._memo[(kind, symbol)]

    def market_close(self) -> str:
        """The market's last close: SPY's last AlphaVantage bar (a holiday has none), capped
        at the session."""
        if self._market is None:
            try:
                spy = self._bars("equity", "SPY")
            except FetchError as exc:
                raise StalePrices(f"the market's last close could not be read (SPY): {exc}") from None
            last = str(spy["date"].iloc[-1]) if spy is not None and len(spy) else self.session
            self._market = min(last, self.session)
        return self._market

    def _mark_stale(self, symbol: str, repo: str, last: str, cause: str, reason: str) -> None:
        self.stale[symbol] = f"{repo}: last bar {last}, {reason}"
        self.stale_info[symbol] = {"dataset": repo, "last": last, "cause": cause, "reason": reason}

    def _explain(self, symbols: list[str]) -> None:
        """Put the delisted and relisted stocks among ``symbols`` under their cause
        (AlphaVantage's LISTING_STATUS, read once and only when needed)."""
        if not symbols:
            return
        if self._listing is None:
            try:
                self._listing = self.client.listing()
            except Exception as exc:  # noqa: BLE001 - an explanation, never a reason to stop
                self.log(f"[prices] LISTING_STATUS not read: {type(exc).__name__}: {exc}")
                self._listing = {}
        for s in symbols:
            info = self.stale_info[s]
            row = self._listing.get(s) or self._listing.get(s.replace(".", "-"))
            if row is None:
                continue
            delisted, ipo = str(row.get("delistingDate") or ""), str(row.get("ipoDate") or "")
            if row.get("state") == "delisted" and delisted[:4].isdigit():
                self._mark_stale(s, info["dataset"], info["last"], "delisted",
                                 f"delisted on {delisted} (AlphaVantage LISTING_STATUS)")
            elif ipo[:4].isdigit() and ipo > info["last"]:
                self._mark_stale(s, info["dataset"], info["last"], "relisted",
                                 f"the ticker was listed anew on {ipo} (AlphaVantage LISTING_STATUS: "
                                 f"{row.get('name', '')}), another series: {info['reason']}")

    def _public(self, repo: str, frame: pd.DataFrame, s: str, lasts, parts: list, replaced: set) -> bool:
        """``s`` continued with its public daily series (``public_prices.py``) when it has one;
        False when it has none, or the public source could not be read or has nothing newer
        (the next source is tried)."""
        from .public_prices import public_series, splice_public

        spec = public_series(repo, s) if self.fred is not None else None
        if spec is None:
            return False
        last = str(lasts[s])[:10]
        start = (_dt.date.fromisoformat(last) - _dt.timedelta(days=120)).isoformat()
        try:
            series = self.fred.daily(spec.series, start)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - the next source is tried, said in the log
            self.log(f"[prices] {s}: FRED {spec.series} not read: {exc}")
            return False
        new_rows, done = splice_public(frame[frame["symbol"] == s], series, spec, self.session)
        self.public[s] = done
        if done.status == "current":
            return False
        if done.alert:
            self.log(f"[prices] {done.alert}")
        if done.status == "refused":
            self._mark_stale(s, repo, last, "diverged", done.reason)
            return True
        parts.append(new_rows)
        replaced.add(s)
        self.served[s] = "public"
        self.log(f"[prices] {s}: continued with FRED {spec.series} since {done.anchor} (factor {done.factor:.6f}, "
                 f"median gap {done.median_gap:.3%} over {done.checked_days} days)")
        if done.last_after < self.market_close():
            self._mark_stale(s, repo, done.last_after, "lagged",
                             f"FRED {spec.series} ends on {done.last_after} (a day's value is published the next "
                             "business day)")
        return True

    def _index(self, repo: str, frame: pd.DataFrame, behind: list[str], lasts) -> tuple[list, set]:
        """The indices and bonds behind: IB's own index levels, then a public daily series
        (``public_prices.py``), then a proxy ETF."""
        from .public_prices import NO_PUBLIC_DAILY

        parts, replaced = [], set()
        for s in behind:
            levels = self._ib("index", s) if repo == INDEX_REPO else None
            if levels is not None:                  # IB's own levels (SPX, VIX): no proxy needed
                new_rows, done = splice(frame[frame["symbol"] == s], levels, self.session,
                                        tolerance=tolerance_for("index", s, self.tolerance))
                done.source = "IB index"
                self.splices[s] = done
                if done.status == "filled":
                    parts.append(new_rows)
                    replaced.add(s)
                    self.served[s] = "ibkr"
                if done.last_after >= self.market_close():
                    continue
                if done.status == "refused":        # the levels disagree with the dataset's
                    self._mark_stale(s, repo, done.last_after, "diverged", f"IB index levels: {done.reason}")
                    continue
            if self._public(repo, frame, s, lasts, parts, replaced):
                continue
            proxy = self.proxies.get(s) if repo == INDEX_REPO else None
            if not proxy:
                self.not_covered[s] = repo
                why = NO_PUBLIC_DAILY.get((repo, s)) if self.fred is not None else None
                self._mark_stale(s, repo, str(lasts[s]), "no_source",
                                 "no fresh source for this dataset" + (f" ({why})" if why else ""))
                continue
            tracks = proxy in TRACKING_PROXIES.get(s, ())
            if not tracks and s not in self.approximate:
                self.not_covered[s] = repo
                self._mark_stale(s, repo, str(lasts[s]), "no_source",
                                 f"its proxy {proxy} does not track {s} in level (it is not chained unless "
                                 f"{s} is listed in fresh_index_proxy_approximate)")
                continue
            try:
                bars = self._bars("equity", proxy)
            except FetchError as exc:
                self._mark_stale(s, repo, str(lasts[s]), "fetch_error", f"its proxy {proxy} not read: {exc}")
                continue
            if bars is None:
                self._mark_stale(s, repo, str(lasts[s]), "unknown", f"its proxy {proxy} is unknown to AlphaVantage")
                continue
            new_rows, done = chain_proxy(frame[frame["symbol"] == s], bars, self.session, proxy_symbol=proxy,
                                         tolerance=tolerance_for("index", s, self.tolerance), checked=tracks)
            self.splices[s] = done
            if done.status == "filled":
                parts.append(new_rows)
                replaced.add(s)
                if done.approximate:
                    self.log(f"[prices] {s}: continued with {proxy}'s returns since {done.anchor} "
                             "(an approximation: the proxy does not track the index)")
            if done.last_after < self.market_close():
                self._mark_stale(s, repo, done.last_after, "diverged" if done.status == "refused" else "silent",
                                 f"through {proxy} after the top-up" + (f" ({done.reason})" if done.reason else ""))
        return parts, replaced

    def extend(self, path, frame):
        repo = str(path).rsplit("/", 1)[-1]
        if (repo not in PRICE_REPOS or not isinstance(frame, pd.DataFrame) or frame.empty
                or not {"symbol", "date"} <= set(frame.columns)):
            return frame
        horizon = (_dt.date.fromisoformat(self.session) - _dt.timedelta(days=LIVE_WINDOW_DAYS)).isoformat()
        iso = frame["date"].map(lambda d: str(d)[:10])
        lasts = iso.groupby(frame["symbol"]).max()
        live = lasts[lasts >= horizon]
        self.inactive.update(str(s) for s in lasts.index if s not in live.index)
        kind = "index" if repo in NOT_COVERED else KIND.get(repo, "crypto")
        target = self.session if kind == "crypto" else self.market_close()
        behind = [str(s) for s, last in live.items() if last < target]
        if not behind:
            return frame
        if repo in NOT_COVERED:
            parts, replaced = self._index(repo, frame, behind, lasts)
        else:
            parts, replaced = [], set()
            for s in behind:
                rows = frame[frame["symbol"] == s]
                try:
                    bars = self._bars(kind, s)
                except FetchError as exc:
                    self._mark_stale(s, repo, str(lasts[s]), "fetch_error", f"AlphaVantage not read: {exc}")
                    continue
                if bars is None:
                    self.splices[s] = Splice(s, "unknown", last_before=str(lasts[s]), last_after=str(lasts[s]),
                                             reason="AlphaVantage does not know the symbol")
                    self._mark_stale(s, repo, str(lasts[s]), "unknown", "unknown to AlphaVantage")
                    continue
                new_rows, done = splice(rows, bars, self.session, tolerance=tolerance_for(kind, s, self.tolerance))
                self.splices[s] = done
                if done.status == "filled":
                    parts.append(new_rows)
                    replaced.add(s)
                if done.last_after < target:
                    cause = "diverged" if "disagree" in done.reason else "silent"
                    if done.status == "refused" and "no common day" in done.reason:
                        cause = "silent"
                    self._mark_stale(s, repo, done.last_after, cause,
                                     "after the top-up" + (f" ({done.reason})" if done.reason else ""))
            if kind == "equity":
                self._explain([s for s in behind if s in self.stale_info
                               and self.stale_info[s]["cause"] in ("silent", "unknown")])
        if self.stale and self.strict:
            raise StalePrices(self.message())
        if not parts:
            return frame
        out = pd.concat([frame[~frame["symbol"].isin(replaced)], *parts], ignore_index=True)
        return out.reset_index(drop=True)

    def message(self) -> str:
        names = sorted(self.stale)
        lines = [f"{len(names)} symbol(s) still stale at the close of {self.session}:"]
        lines += [f"  {s}: {self.stale[s]}" for s in names[:50]]
        if len(names) > 50:
            lines.append(f"  ... and {len(names) - 50} more")
        return "\n".join(lines)

    def summary(self) -> dict:
        filled = [s for s in self.splices.values() if s.status == "filled"]
        return {
            "session": self.session,
            "market_close": self._market,
            "requests": self.client.calls,
            "filled": len(filled),
            "last": max((s.last_after for s in filled), default=None),
            "max_spread": max((s.spread for s in filled if not s.approximate), default=None),
            "partial_last_bars": sorted(s.symbol for s in filled
                                        if not math.isnan(s.last_bar_gap) and abs(s.last_bar_gap) > TOLERANCE),
            "refused": {s.symbol: s.reason for s in self.splices.values() if s.status in ("refused", "unknown")},
            "proxied": {s.symbol: {"proxy": s.source, "anchor": s.anchor, "approximate": s.approximate}
                        for s in filled if s.source},
            "stale": dict(sorted(self.stale.items())),
            "stale_info": dict(sorted(self.stale_info.items())),
            "not_covered": dict(sorted(self.not_covered.items())),
            "inactive": len(self.inactive),
            "served": {src: sorted(s for s, v in self.served.items() if v == src)
                       for src in ("ibkr", "kraken", "alphavantage", "public")
                       if (src != "kraken" or self.kraken is not None)
                       and (src != "public" or "public" in self.served.values())},
            "ib_fallbacks": dict(sorted(self.ib_fallbacks.items())),
            "crosscheck": dict(sorted(self.crosscheck.items())),
            "splices": {name: asdict(done) for name, done in self.splices.items()},
            "public": {name: asdict(done) for name, done in sorted(self.public.items())},
            "public_alerts": {name: done.alert for name, done in sorted(self.public.items()) if done.alert},
        }


def install(fresh: FreshPrices, module=None):
    """Wrap ``pwb_toolbox.datasets.load_dataset`` (and so ``get_pricing``, which calls it)
    so that every price load goes through ``fresh.extend``. Returns the original.

    Only the outermost load is completed: the loads ``load_dataset`` makes for itself (the
    forex table of ``to_usd``, the index proxies of ``extend``) are conversions and history,
    not the strategy's symbols, and pass through."""
    if module is None:
        import pwb_toolbox.datasets as module
    original = module.load_dataset
    depth = [0]

    def load_dataset(path, *args, **kwargs):
        depth[0] += 1
        try:
            frame = original(path, *args, **kwargs)
        finally:
            depth[0] -= 1
        return fresh.extend(path, frame) if depth[0] == 0 else frame

    load_dataset.__wrapped__ = original  # type: ignore[attr-defined]
    module.load_dataset = load_dataset
    return original


__all__ = ["CAUSES", "DEFAULT_TOLERANCE", "FX_MAJORS", "TRACKING_PROXIES", "AlphaVantage", "FetchError",
           "FreshPrices", "Splice", "StalePrices", "chain_proxy", "install", "last_close_session", "parse_daily",
           "splice", "tolerance_for"]
