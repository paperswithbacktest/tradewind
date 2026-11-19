"""Fresh closes for the daily run: the PWB price datasets completed up to the last close.

The PWB daily price datasets are refreshed once a month, and the runner refuses to propose on
data more than a few days old. With ``--fresh-prices alphavantage`` each strategy's price
loads are completed, in its own process, with the bars AlphaVantage has after the dataset's
last day -- for the symbols the strategies ask for, nothing else.

How a series is continued (the rule of the data pipeline's vendor tail):

* the bars come from ``TIME_SERIES_DAILY_ADJUSTED`` (``outputsize=compact``, the last 100
  sessions) for stocks and ETFs, ``FX_DAILY`` for currency pairs, ``DIGITAL_CURRENCY_DAILY``
  for crypto. Indices, commodities and bonds have no source here: a live one that is behind
  makes the run fail, listed as not covered;
* the chain factor ``k`` is the ratio of the two adjusted closes on the *anchor*, the last
  common day before the dataset's last bar (that bar may be a partial one: the monthly source
  is sometimes downloaded during the session). The dataset's history up to the anchor is
  divided by ``k``, as a whole: its returns are kept, and a dividend or a split between the
  two cut-offs is chained the way an adjusted series takes a new event -- nothing is
  "un-adjusted" at the junction. The bars after the anchor are AlphaVantage's own, so the
  last close is the price the account trades at (a split since the dataset's cut-off would
  otherwise size every order in pre-split shares);
* the check: ``k`` must agree to ``TOLERANCE`` (0.5 %) on the anchor and the common days
  before it. When it does not, the two sources do not quote the same series (a reused ticker,
  a split one of them missed) and the symbol is refused;
* the dataset's last bar and everything after the anchor are replaced by AlphaVantage's
  bars; bars after the last completed US session are never taken.

A symbol is *live* when its last bar is within ``LIVE_WINDOW_DAYS`` of the session asked for
(older: a delisted name, left as it is). A live symbol that still ends before the market's
last close after the top-up is *stale*: the strategy is stopped (``StalePrices``) and the run
proposes nothing, with the list.

Responses are cached on disk per session (``CACHE/DATE/FUNCTION/SYMBOL.json``): a second
strategy, or a rerun after an error, does not call the API again for a symbol already read.
The key is read from ``ALPHAVANTAGE_API_KEY`` and is never written to a file or a message;
the request rate is ``ALPHAVANTAGE_RPM`` per minute (75 by default, the smallest premium
plan).
"""

from __future__ import annotations

import datetime as _dt
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

TOLERANCE = 0.005           # the chain factor must agree to this on the common days
CHECK_DAYS = 5              # common days, up to the anchor, the factor is checked on
LIVE_WINDOW_DAYS = 45       # a series ending earlier than this before the session is not live
CLOSE_TIME = _dt.time(16, 30)   # New York: a session's daily bar is taken after this time

KIND = {
    "Stocks-Daily-Price": "equity",
    "ETFs-Daily-Price": "equity",
    "Forex-Daily-Price": "fx",
    "Cryptocurrencies-Daily-Price": "crypto",
}
NOT_COVERED = ("Indices-Daily-Price", "Commodities-Daily-Price", "Bonds-Daily-Price")
PRICE_REPOS = tuple(KIND) + NOT_COVERED


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
                 retries: int = 4, throttle_rounds: int = 5, log=print):
        self.key = key if key is not None else os.environ.get(KEY_ENV, "")
        self.rpm = float(rpm or os.environ.get(RPM_ENV) or 75)
        self.equity_function = equity_function or os.environ.get(FUNCTION_ENV) or "TIME_SERIES_DAILY_ADJUSTED"
        self.cache_dir = os.path.join(os.path.expanduser(cache_dir), session)
        self.get = get or _http_get
        self.sleep, self.clock = sleep, clock
        self.retries, self.throttle_rounds = retries, throttle_rounds
        self.log = log
        self.calls = 0
        self._last_call: float | None = None

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
        attempt = throttled = 0
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
    def daily(self, kind: str, symbol: str) -> pd.DataFrame | None:
        """The symbol's recent daily bars, or None when AlphaVantage does not know it.
        Raises ``FetchError`` when it could not be read (nothing is cached then)."""
        params = self._params(kind, symbol)
        if params is None:
            return None
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", symbol)
        path = os.path.join(self.cache_dir, params["function"], safe + ".json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        else:
            payload = self._request(params)
            if "Error Message" not in payload and parse_daily(kind, payload) is None:
                raise FetchError(f"unexpected answer for {symbol}: {sorted(payload)[:3]}")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        if "Error Message" in payload:
            return None
        return parse_daily(kind, payload)


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


# --------------------------------------------------------------------------------------------- #
# The top-up of whole loads                                                                     #
# --------------------------------------------------------------------------------------------- #
@dataclass
class FreshPrices:
    """Completes the price frames ``pwb_toolbox.datasets.load_dataset`` returns."""

    client: AlphaVantage
    session: str
    log: Callable = print
    splices: dict = field(default_factory=dict)        # symbol -> Splice
    stale: dict = field(default_factory=dict)          # symbol -> why
    not_covered: dict = field(default_factory=dict)    # symbol -> dataset
    inactive: set = field(default_factory=set)
    _market: str | None = None
    _memo: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, cache_dir: str, session: str | None = None, log=print, **client_kwargs) -> FreshPrices:
        session = session or last_close_session()
        return cls(AlphaVantage(cache_dir, session, log=log, **client_kwargs), session, log=log)

    def _bars(self, kind: str, symbol: str) -> pd.DataFrame | None:
        if (kind, symbol) not in self._memo:
            self._memo[(kind, symbol)] = self.client.daily(kind, symbol)
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
        kind = KIND.get(repo, "crypto")
        target = self.session if kind == "crypto" else self.market_close()
        behind = [str(s) for s, last in live.items() if last < target]
        if not behind:
            return frame
        if repo in NOT_COVERED:
            for s in behind:
                self.not_covered[s] = repo
                self.stale[s] = f"{repo}: last bar {lasts[s]}, no fresh source for this dataset"
            raise StalePrices(self.message())
        parts, replaced = [], set()
        for s in behind:
            rows = frame[frame["symbol"] == s]
            try:
                bars = self._bars(kind, s)
            except FetchError as exc:
                self.stale[s] = f"{repo}: last bar {lasts[s]}, AlphaVantage not read: {exc}"
                continue
            if bars is None:
                self.splices[s] = Splice(s, "unknown", last_before=str(lasts[s]), last_after=str(lasts[s]),
                                         reason="AlphaVantage does not know the symbol")
                self.stale[s] = f"{repo}: last bar {lasts[s]}, unknown to AlphaVantage"
                continue
            new_rows, done = splice(rows, bars, self.session)
            self.splices[s] = done
            if done.status == "filled":
                parts.append(new_rows)
                replaced.add(s)
            if done.last_after < target:
                self.stale[s] = (f"{repo}: last bar {done.last_after} after the top-up"
                                 + (f" ({done.reason})" if done.reason else ""))
        if self.stale:
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
            "max_spread": max((s.spread for s in filled), default=None),
            "partial_last_bars": sorted(s.symbol for s in filled
                                        if not math.isnan(s.last_bar_gap) and abs(s.last_bar_gap) > TOLERANCE),
            "refused": {s.symbol: s.reason for s in self.splices.values() if s.status in ("refused", "unknown")},
            "stale": dict(sorted(self.stale.items())),
            "not_covered": dict(sorted(self.not_covered.items())),
            "inactive": len(self.inactive),
            "splices": {name: asdict(done) for name, done in self.splices.items()},
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


__all__ = ["AlphaVantage", "FetchError", "FreshPrices", "Splice", "StalePrices", "install",
           "last_close_session", "parse_daily", "splice"]
