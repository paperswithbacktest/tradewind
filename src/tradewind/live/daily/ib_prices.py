"""Fresh daily bars from Interactive Brokers, collected once in the run's own process.

With ``--fresh-prices ibkr,alphavantage`` the run asks IB first and AlphaVantage for what IB
does not give, symbol by symbol. The strategies run in their own processes (``pwb_worker``);
they never connect to IB. Before they run, the parent process (``PwbProvider``) opens **one**
read-only connection on a client id kept for data (957 by default, never the account's), takes
the union of the symbols the strategies are expected to load, asks one daily request per symbol
and writes the answers to the session's price cache, next to AlphaVantage's::

    CACHE/SESSION/IB_ADJUSTED_LAST/SYMBOL.json      stocks, ETFs, ADRs (SMART, USD)
    CACHE/SESSION/IB_MIDPOINT/SYMBOL.json           spot forex (IDEALPRO), in the book's direction
    CACHE/SESSION/IB_TRADES/SYMBOL.json             the indices SPX and VIX (CBOE): their real levels

The workers read those files (``IBPriceCache``) and fall back on AlphaVantage for a symbol
whose file is missing, says an error, or has no bar for the session.

**What is asked.** Stocks: ``ADJUSTED_LAST`` (one request gives the day's close -- equal to
the raw close -- and a history adjusted for splits and dividends, like AlphaVantage's
``TIME_SERIES_DAILY_ADJUSTED``), ``endDateTime=""`` (the only end this type accepts), regular
hours, over ``duration`` (3 months by default: the monthly datasets may end a month back, and
the splice needs common days before their last bar). A share class is written with a space
(``BRK.B`` -> ``BRK B``). Forex: ``MIDPOINT`` on IDEALPRO, IB's direction (``instruments.fx_pair``)
turned into the book's (``JPYUSD`` is ``1 / USD.JPY``; high and low swap); BRL, CLP, IDR, INR and
ISK have no IDEALPRO pair and are never asked. Indices: ``TRADES`` on ``Index(SPX|VIX, CBOE)``.
Crypto, rates and commodities: never IB.

**Errors.** A symbol falls back on AlphaVantage on error 200 (no such contract -- with a
cached conId, the contract is qualified again once), 162 (no permission, no data; but a
*pacing violation* is retried), 354, 10089, 10090, an empty answer, or a request still
unanswered after its retries. Pacing: IB's 60-requests-in-10-minutes rule is for bars of 30
seconds or less, not daily bars (``IBAdapter.history_limiter`` is not used): up to
``concurrency`` requests at a time (40), a pacing violation waits 10, 20, 40, 80 s, each
request is given ``timeout`` seconds (90). Measured on 2026-11-21 (paper account, 1 203
symbols): 28-38 s of requests at 40-50 at a time, no pacing violation.

**conIds.** Qualifying a contract costs more than its history (48-76 s against 28-38 s for
1 200 symbols): the conIds are kept in ``CACHE/ib_conids.json`` and reused for
``conid_max_age_days`` (7); a cached conId IB no longer knows (200) is qualified again. A
symbol IB has no contract for (a delisted name among the feeds a strategy reads) is kept too,
and not asked again before that age.

**The day's bar.** IB returns the bar still forming during the session. A bar dated D is kept
only when the file was written after D's close plus a grace (``bar_complete_at``: stocks and
indices 16:00 New York, early closes included, plus 15 minutes; forex 17:00 New York plus 15
minutes); otherwise the bar is dropped and the symbol counts as without the session's bar. A
rerun after that time asks again.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import math
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pandas as pd

from .instruments import FX_UNAVAILABLE, IDEALPRO, fx_pair

#: the client id the data connection uses unless told otherwise; the account's is another
DATA_CLIENT_ID = 957
#: the cache directory of each kind, by IB's whatToShow
FOLDERS = {"equity": "IB_ADJUSTED_LAST", "fx": "IB_MIDPOINT", "index": "IB_TRADES"}
WHAT = {"equity": "ADJUSTED_LAST", "fx": "MIDPOINT", "index": "TRADES"}
#: the indices IB serves, with their exchange
IB_INDICES = {"SPX": "CBOE", "VIX": "CBOE"}
#: currencies IB never serves here (no IDEALPRO pair)
NO_IB_CURRENCIES = frozenset({"BRL", "CLP", "IDR", "INR", "ISK"}) | frozenset(FX_UNAVAILABLE)
#: error codes after which the symbol is left to AlphaVantage (162 only when it is not pacing)
FALLBACK_CODES = frozenset({162, 200, 354, 10089, 10090, 366, 321})
#: informational codes (market data farm connections...)
INFO_CODES = frozenset({2103, 2104, 2105, 2106, 2107, 2108, 2119, 2158})
#: the grace after the close before a daily bar is taken, minutes
DEFAULT_GRACE = {"equity": 15.0, "index": 15.0, "fx": 15.0}
_EQUITY = re.compile(r"^[A-Z]{1,6}(\.[A-Z]{1,2})?$")


# --------------------------------------------------------------------------------------------- #
# The day's bar                                                                                 #
# --------------------------------------------------------------------------------------------- #
def bar_complete_at(kind: str, day: str, grace: dict | None = None) -> float | None:
    """Epoch seconds when the daily bar of ``day`` (ISO) is final: stocks, ETFs and indices
    at the NYSE close of that day (early closes included; 16:00 New York on a day the calendar
    does not trade) plus the grace, forex at 17:00 New York plus the grace. None for a kind
    without a rule (crypto)."""
    from zoneinfo import ZoneInfo

    g = dict(DEFAULT_GRACE)
    g.update({k: float(v) for k, v in (grace or {}).items()})
    d = _dt.date.fromisoformat(day[:10])
    ny = ZoneInfo("America/New_York")
    if kind in ("equity", "index"):
        from ...calendars import NYSE

        hours = NYSE.session(d)
        close = (float(hours[1]) / 1e9 if hours is not None
                 else _dt.datetime.combine(d, _dt.time(16, 0), ny).timestamp())
        return close + 60.0 * g.get(kind, g["equity"])
    if kind == "fx":
        return _dt.datetime.combine(d, _dt.time(17, 0), ny).timestamp() + 60.0 * g["fx"]
    return None


def needed_session(kind: str, session: str) -> str:
    """The last day a bar is expected for at ``session``: the last NYSE session on or before it
    for stocks and indices, the last weekday for forex."""
    d = _dt.date.fromisoformat(session[:10])
    if kind in ("equity", "index"):
        from ...calendars import NYSE

        for _ in range(15):
            if NYSE.is_trading_day(d):
                return d.isoformat()
            d -= _dt.timedelta(days=1)
        return session[:10]
    while d.weekday() >= 5:
        d -= _dt.timedelta(days=1)
    return d.isoformat()


def drop_unfinished(frame: pd.DataFrame | None, kind: str, session: str, fetched: float,
                    grace: dict | None = None) -> pd.DataFrame | None:
    """``frame`` (``date`` as ISO strings) without the bars after ``session`` and without the
    session's bar when it was fetched before ``bar_complete_at``."""
    if frame is None or frame.empty:
        return frame
    frame = frame[frame["date"] <= session[:10]]
    done = bar_complete_at(kind, session, grace)
    if done is not None and fetched < done:
        frame = frame[frame["date"] < session[:10]]
    return frame.reset_index(drop=True)


# --------------------------------------------------------------------------------------------- #
# Which contract                                                                                #
# --------------------------------------------------------------------------------------------- #
def ib_kind(symbol: str, kind: str | None = None) -> str | None:
    """The kind IB serves ``symbol`` as (``equity``, ``fx``, ``index``), or None. ``kind`` is
    the dataset's when known (an AlphaVantage cache folder); otherwise it is guessed from the
    symbol (a pair against USD, SPX / VIX, a ticker of capital letters)."""
    if kind in ("crypto", "rates", "commodity"):
        return None
    if kind == "index" or (kind is None and symbol in IB_INDICES):
        return "index" if symbol in IB_INDICES else None
    pair = fx_pair(symbol)
    if kind == "fx" or (kind is None and pair is not None):
        if pair is None or pair.currency in NO_IB_CURRENCIES or pair.currency not in IDEALPRO:
            return None
        return "fx"
    if kind == "equity" or (kind is None and _EQUITY.match(symbol)):
        return "equity" if _EQUITY.match(symbol) else None
    return None


def ib_local(symbol: str, kind: str) -> dict:
    """What to ask IB for ``symbol``: ``{sec, symbol, exchange, what, invert}``."""
    if kind == "fx":
        pair = fx_pair(symbol)
        assert pair is not None
        return {"sec": "CASH", "symbol": pair.ib_base + pair.ib_quote, "exchange": "IDEALPRO", "what": WHAT[kind],
                "invert": pair.inverted}
    if kind == "index":
        return {"sec": "IND", "symbol": symbol, "exchange": IB_INDICES[symbol], "what": WHAT[kind], "invert": False}
    return {"sec": "STK", "symbol": symbol.replace(".", " "), "exchange": "SMART", "what": WHAT[kind],
            "invert": False}


def _safe(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", symbol)


# --------------------------------------------------------------------------------------------- #
# The workers' side: reading the cache                                                          #
# --------------------------------------------------------------------------------------------- #
class IBPriceCache:
    """The bars the parent collected for ``session``, read by the strategies' workers."""

    def __init__(self, cache_dir: str, session: str, grace: dict | None = None):
        self.dir = os.path.join(os.path.expanduser(cache_dir), session)
        self.session = session
        self.grace = dict(grace or {})

    def path(self, kind: str, symbol: str) -> str:
        return os.path.join(self.dir, FOLDERS[kind], _safe(symbol) + ".json")

    def bars(self, kind: str, symbol: str) -> tuple[pd.DataFrame | None, str]:
        """``(bars, why)``: the symbol's bars (``date, open, high, low, close, adj_close,
        volume``, the session's bar only when final), or None and the reason to fall back."""
        if kind not in FOLDERS or ib_kind(symbol, kind) is None:
            return None, "not served by IB"
        path = self.path(kind, symbol)
        if not os.path.exists(path):
            return None, "not collected from IB"
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            return None, f"IB cache unreadable: {exc}"
        if payload.get("kind", kind) != kind:
            return None, f"IB cache holds a {payload.get('kind')}"
        if payload.get("error"):
            return None, f"IB error {payload.get('code', '')}: {payload['error']}".strip()
        rows = payload.get("bars") or []
        if not rows:
            return None, "IB returned no bar"
        frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        frame["date"] = frame["date"].map(lambda d: str(d)[:10])
        frame["adj_close"] = frame["close"]
        frame = frame[["date", "open", "high", "low", "close", "adj_close", "volume"]].sort_values("date")
        frame = drop_unfinished(frame, kind, self.session, float(payload.get("fetched_ts", 0.0)), self.grace)
        if frame is None or frame.empty:
            return None, "IB returned no final bar"
        want = needed_session(kind, self.session)
        if str(frame["date"].iloc[-1]) < want:
            return None, f"IB has no final bar for {want} (last {frame['date'].iloc[-1]})"
        return frame, ""


# --------------------------------------------------------------------------------------------- #
# The parent's side: one connection, every symbol                                               #
# --------------------------------------------------------------------------------------------- #
@dataclass
class IBDataConfig:
    """How the parent asks IB (``fresh_ib`` in the meta-portfolio)."""

    client_id: int = DATA_CLIENT_ID
    concurrency: int = 40
    timeout: float = 90.0
    duration: str = "3 M"
    retries: int = 4
    backoff: float = 10.0
    conid_max_age_days: float = 7.0
    connect_timeout: float = 20.0

    @classmethod
    def from_dict(cls, d: dict | None) -> IBDataConfig:
        d = dict(d or {})
        known = set(cls.__dataclass_fields__)
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown fresh_ib options: {sorted(unknown)}")
        out = cls(**d)
        if out.concurrency < 1 or out.timeout <= 0 or out.retries < 1:
            raise ValueError("fresh_ib: concurrency and retries at least 1, timeout above 0")
        return out


@dataclass
class _Result:
    bars: list = field(default_factory=list)
    code: int = 0
    error: str = ""
    retries: int = 0


class IBDailyPrices:
    """Collects the daily bars of a list of symbols on one read-only connection and writes
    them to the session's cache (module doc). ``ib`` / ``ib_module``: an ``ib_async.IB`` and the
    module (tests pass stubs); ``sleep``: an async sleep (tests pass one that does not wait)."""

    def __init__(self, cache_dir: str, session: str, *, host: str = "127.0.0.1", port: int = 4002,
                 config: IBDataConfig | None = None, account_client_id: int | None = None, ib=None,
                 ib_module=None, log: Callable = print, sleep=None, clock: Callable[[], float] = time.time):
        self.cache_dir = os.path.expanduser(cache_dir)
        self.session = session
        self.host, self.port = host, int(port)
        self.config = config or IBDataConfig()
        if account_client_id is not None and int(account_client_id) == int(self.config.client_id):
            raise ValueError(f"the data connection's client id {self.config.client_id} is the account's: give "
                             "it its own (fresh_ib client_id, --ib-data-client-id)")
        self.ib, self._ibm = ib, ib_module
        self.log = log
        self.sleep = sleep or asyncio.sleep
        self.clock = clock
        self.conid_path = os.path.join(self.cache_dir, "ib_conids.json")
        self._errors: dict[int, list[tuple[int, str]]] = {}
        self.stats: dict = {}

    # -- the connection ------------------------------------------------------------------------
    def _lib(self):
        if self._ibm is None:
            import ib_async

            self._ibm = ib_async
        return self._ibm

    def connect(self):
        """One read-only connection through ``IBAdapter``'s guards: a paper port, and every
        managed account a paper account (``DU...``), or nothing."""
        from ..adapter import AdapterError
        from ..ibkr import PAPER_PORTS, IBAdapter

        if self.port not in PAPER_PORTS:
            raise AdapterError(f"port {self.port} is not a paper port {sorted(PAPER_PORTS)}: the price "
                               "connection is for paper accounts only")
        adapter = IBAdapter(self.host, self.port, self.config.client_id, require_paper=True, readonly=True,
                            ib=self.ib, ib_module=self._lib())
        adapter.connect()
        self.ib = adapter.ib
        self.ib.errorEvent.connect(self._on_error)
        return adapter

    def _on_error(self, req_id, code, message, contract=None, *rest) -> None:
        code = int(code or 0)
        if code in INFO_CODES or contract is None:
            return
        self._errors.setdefault(id(contract), []).append((code, str(message)[:200]))

    # -- the conId cache -----------------------------------------------------------------------
    def _load_conids(self) -> dict:
        try:
            with open(self.conid_path, encoding="utf-8") as fh:
                table = json.load(fh)
        except (OSError, ValueError):
            return {}
        horizon = self.clock() - 86400.0 * self.config.conid_max_age_days
        return {k: v for k, v in table.items()
                if float(v.get("ts", 0.0)) >= horizon and (v.get("conId") or v.get("missing"))}

    def _save_conids(self, table: dict) -> None:
        os.makedirs(self.cache_dir, exist_ok=True)
        tmp = self.conid_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(table, fh, indent=0, sort_keys=True)
        os.replace(tmp, self.conid_path)

    # -- contracts -----------------------------------------------------------------------------
    def _contract(self, spec: dict, con_id: int = 0):
        lib = self._lib()
        if con_id:
            return lib.Contract(conId=int(con_id), exchange=spec["exchange"])
        if spec["sec"] == "CASH":
            return lib.Forex(pair=spec["symbol"], exchange="IDEALPRO")
        if spec["sec"] == "IND":
            return lib.Index(spec["symbol"], spec["exchange"], "USD")
        return lib.Stock(spec["symbol"], "SMART", "USD")

    async def _qualify(self, contracts: list) -> None:
        sem = asyncio.Semaphore(self.config.concurrency)

        async def chunk(items):
            async with sem:
                try:
                    await asyncio.wait_for(self.ib.qualifyContractsAsync(*items), self.config.timeout)
                except asyncio.TimeoutError:
                    pass

        await asyncio.gather(*(chunk(contracts[i:i + 50]) for i in range(0, len(contracts), 50)))

    # -- requests ------------------------------------------------------------------------------
    async def _history(self, contract, what: str, sem) -> _Result:
        out = _Result()
        for attempt in range(self.config.retries):
            async with sem:
                seen = len(self._errors.get(id(contract), []))
                try:
                    bars = await asyncio.wait_for(
                        self.ib.reqHistoricalDataAsync(contract, "", self.config.duration, "1 day", what, True,
                                                       formatDate=1, timeout=0), self.config.timeout)
                except asyncio.TimeoutError:
                    bars, new = None, [(-1, f"no answer in {self.config.timeout:g} s")]
                else:
                    new = self._errors.get(id(contract), [])[seen:]
            if bars:
                out.bars = [[str(b.date)[:10], float(b.open), float(b.high), float(b.low), float(b.close),
                             float(b.volume) if b.volume is not None and float(b.volume) >= 0 else 0.0] for b in bars]
                return out
            pacing = any("pacing" in m.lower() for _, m in new)
            if pacing or any(c == -1 for c, _ in new):
                out.retries += 1
                out.code, out.error = new[-1]
                await self.sleep(self.config.backoff * 2 ** attempt)
                continue
            code, msg = (new[-1] if new else (0, "no bar returned"))
            out.code, out.error = code, msg
            return out
        out.error = (out.error or "no answer") + f" (after {self.config.retries} tries)"
        return out

    def _write(self, kind: str, symbol: str, payload: dict) -> None:
        folder = os.path.join(self.cache_dir, self.session, FOLDERS[kind])
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, _safe(symbol) + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)

    def _cached_ok(self, kind: str, symbol: str) -> bool:
        """A file of this session already final (a rerun): not asked again."""
        cache = IBPriceCache(self.cache_dir, self.session)
        frame, _why = cache.bars(kind, symbol)
        return frame is not None

    async def _collect(self, wanted: dict[str, str]) -> dict:
        t0 = self.clock()
        conids = self._load_conids()
        specs = {s: ib_local(s, k) for s, k in wanted.items()}
        key = {s: f"{sp['sec']}:{sp['symbol']}" for s, sp in specs.items()}
        missing = {s for s in wanted if conids.get(key[s], {}).get("missing")}     # no contract, known
        contracts = {s: self._contract(specs[s], int(conids.get(key[s], {}).get("conId", 0) or 0))
                     for s in wanted if s not in missing}
        fresh = [c for s, c in contracts.items() if not getattr(c, "conId", 0)]
        await self._qualify(fresh)
        t_qualify = self.clock() - t0
        for s, c in contracts.items():
            if getattr(c, "conId", 0) and key[s] not in conids:
                conids[key[s]] = {"conId": int(c.conId), "ts": self.clock()}
            elif not getattr(c, "conId", 0) and any(code == 200 for code, _ in self._errors.get(id(c), [])):
                conids[key[s]] = {"conId": 0, "missing": True, "ts": self.clock()}   # asked again in a week
        sem = asyncio.Semaphore(self.config.concurrency)
        results: dict[str, _Result] = {}

        async def one(sym: str) -> None:
            if sym in missing:
                results[sym] = _Result(code=200, error="no contract at IB (conId cache: no security definition)")
                return
            c = contracts[sym]
            if not getattr(c, "conId", 0):
                errs = self._errors.get(id(c), [])
                code, msg = errs[-1] if errs else (200, "no security definition found")
                results[sym] = _Result(code=code, error=msg)
                return
            res = await self._history(c, specs[sym]["what"], sem)
            if not res.bars and res.code == 200 and key[sym] in conids:     # a stale conId: once more
                conids.pop(key[sym], None)
                again = self._contract(specs[sym])
                await self._qualify([again])
                if getattr(again, "conId", 0):
                    conids[key[sym]] = {"conId": int(again.conId), "ts": self.clock()}
                    res = await self._history(again, specs[sym]["what"], sem)
            results[sym] = res

        await asyncio.gather(*(one(s) for s in wanted))
        self._save_conids(conids)
        fetched = self.clock()
        stamp = _dt.datetime.fromtimestamp(fetched, _dt.timezone.utc).isoformat(timespec="seconds")
        codes: dict[str, int] = {}
        served = 0
        for sym, res in results.items():
            kind, spec = wanted[sym], specs[sym]
            base = {"symbol": sym, "kind": kind, "contract": spec["symbol"], "what": spec["what"],
                    "fetched": stamp, "fetched_ts": fetched}
            if res.bars:
                bars = res.bars
                if spec["invert"]:
                    bars = [[d, 1.0 / o, 1.0 / lo, 1.0 / hi, 1.0 / c, v] for d, o, hi, lo, c, v in bars
                            if o > 0 and hi > 0 and lo > 0 and c > 0]
                self._write(kind, sym, dict(base, bars=bars, retries=res.retries))
                served += 1
            else:
                self._write(kind, sym, dict(base, error=res.error or "no bar", code=res.code, retries=res.retries))
                label = "pacing" if "pacing" in (res.error or "").lower() else str(res.code or "none")
                codes[label] = codes.get(label, 0) + 1
        return {"asked": len(wanted), "served": served, "fallback": len(wanted) - served, "errors": codes,
                "qualified": len(fresh), "conid_cache": len(wanted) - len(fresh) - len(missing),
                "known_missing": len(missing),
                "retries": sum(r.retries for r in results.values()),
                "seconds_qualify": round(t_qualify, 1), "seconds": round(self.clock() - t0, 1)}

    def collect(self, symbols: dict[str, str | None]) -> dict:
        """Collect ``{symbol: kind or None}`` (kinds as ``ib_kind`` reads them; symbols IB does
        not serve are skipped). Connects, asks, writes, disconnects. Returns the statistics."""
        wanted: dict[str, str] = {}
        for sym, kind in symbols.items():
            k = ib_kind(sym, kind)
            if k is not None:
                wanted[sym] = k
        todo = {s: k for s, k in wanted.items() if not self._cached_ok(k, s)}
        self.stats = {"session": self.session, "kept": len(wanted) - len(todo), "asked": 0, "served": 0,
                      "fallback": 0, "errors": {}, "seconds": 0.0}
        if not todo:
            return self.stats
        adapter = self.connect()
        try:
            self.stats.update(self.ib.run(self._collect(todo)))
        finally:
            adapter.disconnect()
        s = self.stats
        self.log(f"[prices] IB: {s['served']}/{s['asked']} symbol(s) in {s['seconds']:.0f} s "
                 f"({s['qualified']} contract(s) qualified in {s['seconds_qualify']:.0f} s, {s['conid_cache']} from the "
                 f"conId cache, {s['retries']} retr{'y' if s['retries'] == 1 else 'ies'}); left to AlphaVantage: "
                 f"{s['fallback']}" + (f" {s['errors']}" if s["errors"] else "")
                 + (f"; {s['kept']} already in the cache" if s["kept"] else ""))
        return self.stats


def universe(cache_dir: str, session: str, books_dir: str | None, slugs: list[str], extra: Sequence[str] = (),
             sessions_back: int = 3) -> dict[str, str | None]:
    """The symbols the strategies are expected to load at ``session``, ``{symbol: kind or
    None}``: what the last sessions' price caches hold (their folder says the kind), the feeds
    of each sleeve's last cached book (kind guessed), and ``extra`` (the substitutes)."""
    out: dict[str, str | None] = {}
    root = os.path.expanduser(cache_dir)
    folders = {"TIME_SERIES_DAILY_ADJUSTED": "equity", "TIME_SERIES_DAILY": "equity", "FX_DAILY": "fx",
               "DIGITAL_CURRENCY_DAILY": "crypto", **{v: k for k, v in FOLDERS.items()}}
    if os.path.isdir(root):
        days = sorted(d for d in os.listdir(root) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d <= session)
        for day in days[-sessions_back:]:
            for folder, kind in folders.items():
                path = os.path.join(root, day, folder)
                if not os.path.isdir(path):
                    continue
                for name in os.listdir(path):
                    if name.endswith(".json"):
                        out.setdefault(name[:-5], kind)
    if books_dir and os.path.isdir(books_dir):
        for slug in slugs:
            found = []
            for tag in os.listdir(books_dir):
                p = os.path.join(books_dir, tag, slug + ".json")
                if os.path.exists(p):
                    found.append((os.path.getmtime(p), p))
            if not found:
                continue
            try:
                with open(max(found)[1], encoding="utf-8") as fh:
                    books = json.load(fh).get("books") or []
            except (OSError, ValueError):
                continue
            for b in books[-1:]:
                for sym in b.get("close", {}):
                    out.setdefault(str(sym), None)
    for sym in extra:
        out.setdefault(sym, "equity")
    return out


def summarize(served: dict[str, set], stale: set) -> dict:
    """``{n, ibkr, alphavantage, stale, shares}`` over the unique symbols the workers filled."""
    ib, av = set(served.get("ibkr", set())), set(served.get("alphavantage", set())) - set(served.get("ibkr", set()))
    every = ib | av | set(stale)
    n = len(every)

    def share(x):
        return len(x) / n if n else math.nan

    return {"n": n, "ibkr": len(ib), "alphavantage": len(av), "stale": len(set(stale)),
            "shares": {"ibkr": share(ib), "alphavantage": share(av), "stale": share(set(stale))}}


__all__ = ["DATA_CLIENT_ID", "DEFAULT_GRACE", "FALLBACK_CODES", "FOLDERS", "IB_INDICES", "NO_IB_CURRENCIES",
           "IBDailyPrices", "IBDataConfig", "IBPriceCache", "bar_complete_at", "drop_unfinished", "ib_kind",
           "ib_local", "needed_session", "summarize", "universe"]
