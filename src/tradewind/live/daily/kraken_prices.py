"""Fresh crypto closes from Kraken's public data, collected once in the run's own process.

With ``--fresh-prices kraken`` (alone or before the other sources) the parent process
(``PwbProvider``) asks Kraken's public ``OHLC`` (daily, interval 1440, no key) for the crypto
symbols the strategies are expected to load, and writes the answers to the session's price
cache, next to the other sources' (spec/11 § 8)::

    CACHE/SESSION/KRAKEN_OHLC/SYMBOL.json

The workers read those files (``KrakenPriceCache``) when they complete
``Cryptocurrencies-Daily-Price``; a symbol whose file is missing, says an error, or has no
complete bar for the session falls back on the next source listed.

**The day's bar.** Kraken stamps a daily bar at its 00:00 UTC open, as the catalogue's crypto
dataset does; the bar of day D is complete at D+1 00:00 UTC (``crypto_bar_complete_at``,
plus a grace of 5 minutes). The last row of an answer is the one still forming and is never
written. Crypto trades every day: the bar expected at session D is D's own.

**Which symbols.** The catalogue's bare base tickers (``BTC``, ``ETH``); Kraken's names
(``XBT``, ``XDG``) are resolved through ``AssetPairs``, against USD. A symbol with no Kraken
USD pair is skipped; a forex pair (``EURUSD``) is never asked here. Public calls are paced
at one a second.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
from collections.abc import Callable

import pandas as pd

from ..adapter import AdapterError
from ..kraken import KrakenError, KrakenREST, PairTable, bar_day, parse_ohlc
from .instruments import fx_pair

FOLDER = "KRAKEN_OHLC"
DEFAULT_GRACE_MINUTES = 5.0


def _safe(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", symbol)


def crypto_bar_complete_at(day: str, grace_minutes: float = DEFAULT_GRACE_MINUTES) -> float:
    """Epoch seconds when the crypto daily bar of ``day`` (ISO) is final: the next 00:00 UTC
    plus the grace."""
    d = _dt.date.fromisoformat(day[:10]) + _dt.timedelta(days=1)
    return _dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc).timestamp() + 60.0 * grace_minutes


def kraken_kind(symbol: str, kind: str | None = None) -> bool:
    """Whether ``symbol`` is asked from Kraken: a crypto symbol (``kind`` ``crypto``), or a
    symbol of unknown kind that is not a forex pair."""
    if kind is not None:
        return kind == "crypto"
    return fx_pair(symbol) is None and bool(re.fullmatch(r"[A-Z0-9]{1,12}", symbol))


class KrakenPriceCache:
    """The bars the parent collected for ``session``, read by the strategies' workers."""

    def __init__(self, cache_dir: str, session: str, grace_minutes: float = DEFAULT_GRACE_MINUTES):
        self.dir = os.path.join(os.path.expanduser(cache_dir), session)
        self.session = session
        self.grace = float(grace_minutes)

    def path(self, symbol: str) -> str:
        return os.path.join(self.dir, FOLDER, _safe(symbol) + ".json")

    def bars(self, kind: str, symbol: str) -> tuple[pd.DataFrame | None, str]:
        """``(bars, why)``: ``date, open, high, low, close, adj_close, volume`` up to the
        session (its bar only when final), or None and the reason to fall back."""
        if kind != "crypto":
            return None, "not served by Kraken"
        path = self.path(symbol)
        if not os.path.exists(path):
            return None, "not collected from Kraken"
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            return None, f"Kraken cache unreadable: {exc}"
        if payload.get("error"):
            return None, f"Kraken: {payload['error']}"
        rows = payload.get("bars") or []
        if not rows:
            return None, "Kraken returned no bar"
        frame = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        frame["adj_close"] = frame["close"]
        frame = frame[["date", "open", "high", "low", "close", "adj_close", "volume"]].sort_values("date")
        frame = frame[frame["date"] <= self.session]
        if float(payload.get("fetched_ts", 0.0)) < crypto_bar_complete_at(self.session, self.grace):
            frame = frame[frame["date"] < self.session]
        frame = frame.reset_index(drop=True)
        if frame.empty:
            return None, "Kraken returned no final bar"
        if str(frame["date"].iloc[-1]) < self.session:
            return None, f"Kraken has no final bar for {self.session} (last {frame['date'].iloc[-1]})"
        return frame, ""


class KrakenDailyPrices:
    """Collect the daily bars of the crypto symbols of a session into the cache (public data,
    no key). ``rest``: a ``KrakenREST`` (tests give one with a recorded transport)."""

    def __init__(self, cache_dir: str, session: str, *, rest: KrakenREST | None = None, quote: str = "USD",
                 log: Callable = print, clock: Callable[[], float] = time.time):
        self.cache = KrakenPriceCache(cache_dir, session)
        self.session = session
        self.rest = rest or KrakenREST()
        self.quote = quote
        self.log = log
        self.clock = clock
        self.stats: dict = {}

    def _write(self, symbol: str, payload: dict) -> None:
        path = self.cache.path(symbol)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)

    def collect(self, symbols: dict[str, str | None]) -> dict:
        """Collect ``{symbol: kind or None}`` (symbols that are not crypto are skipped).
        Returns the statistics; a symbol already cached with the session's final bar is
        kept."""
        t0 = self.clock()
        wanted = [s for s, k in symbols.items() if kraken_kind(s, k)]
        todo = [s for s in wanted if self.cache.bars("crypto", s)[0] is None]
        self.stats = {"session": self.session, "kept": len(wanted) - len(todo), "asked": 0, "served": 0,
                      "fallback": 0, "unknown": 0, "errors": {}, "seconds": 0.0}
        if not todo:
            return self.stats
        try:
            pairs = PairTable.from_result(self.rest.public("AssetPairs"))
        except KrakenError as exc:
            self.stats["error"] = str(exc)
            return self.stats
        for sym in todo:
            try:
                pair = pairs.resolve(sym, self.quote)
            except AdapterError:
                self.stats["unknown"] += 1
                continue
            self.stats["asked"] += 1
            fetched = self.clock()
            try:
                rows = parse_ohlc(self.rest.public("OHLC", {"pair": pair.altname, "interval": 1440}))
            except KrakenError as exc:
                self._write(sym, {"kind": "crypto", "symbol": sym, "pair": pair.altname, "fetched_ts": fetched,
                                  "error": str(exc)})
                self.stats["errors"][sym] = str(exc)
                self.stats["fallback"] += 1
                continue
            bars = [[bar_day(ts), o, h, lo, c, v] for ts, o, h, lo, c, v in rows]
            self._write(sym, {"kind": "crypto", "symbol": sym, "pair": pair.altname, "fetched_ts": fetched,
                              "bars": bars})
            ok = self.cache.bars("crypto", sym)[0] is not None
            self.stats["served" if ok else "fallback"] += 1
        self.stats["seconds"] = round(self.clock() - t0, 1)
        s = self.stats
        self.log(f"[prices] Kraken: {s['served']}/{s['asked']} crypto symbol(s) in {s['seconds']:.0f} s"
                 + (f", {s['unknown']} without a Kraken {self.quote} pair" if s["unknown"] else "")
                 + (f"; {s['kept']} already in the cache" if s["kept"] else ""))
        return self.stats


__all__ = ["FOLDER", "KrakenDailyPrices", "KrakenPriceCache", "crypto_bar_complete_at", "kraken_kind"]
