"""Where the sleeves' books come from: a strategy run in signal mode up to a close.

``EngineProvider`` runs Tradewind strategies the caller builds (a function that returns a
ready engine for a given last date): tests, and any strategy written for Tradewind.

``PwbProvider`` runs catalogue strategies (``strategy.py`` of Papers With Backtest) through
pwb-toolbox on the Tradewind engine, one subprocess per strategy so that memory comes back
after each and a strategy that exits hard does not take the run down; the environment that
has pwb-toolbox is ``python`` (``pwb_worker.py`` is what it executes). Books are cached per
day in ``cache_dir``: a second run of the same day (propose, then send) reuses them. With
``fresh_prices="alphavantage"`` each strategy's price loads are completed up to the last
close first (``fresh.py``); with ``"ibkr,alphavantage"`` the provider first collects the daily
bars of every symbol the strategies are expected to load from IB, on one read-only connection
of its own (``ib_prices.py``), and the strategies' workers read them from the session's cache,
AlphaVantage giving the rest. A symbol still stale is listed per sleeve (``stale``,
``stale_info``) and the books are kept; the runner decides what to do with it
(``stale_symbol_policy``). With ``stale_symbol_policy: block`` it stops the run
(``StaleData``), as before. The last date of the point-in-time universe each sleeve loaded is
kept too (``universe_last``; the session's ``universe_age`` alert).

``ReplayProvider`` runs each strategy once up to ``until`` and serves every earlier day from
that run: a strategy is causal, so its book at a past close is the same whether the run
stopped there or went on (look-ahead is the exception, and the reason a strategy reading
``data.close[1]`` cannot trade live).

A provider's ``books(as_of, last_n)`` returns ``{slug: {date: SleeveBook}}``: the last
``last_n`` closes up to ``as_of`` (the last close of the data when None).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable

from .books import SleeveBook, record_books
from .meta import MetaPortfolio
from .runner import StaleData

FRESH_SOURCES = ("alphavantage", "ibkr")


def parse_sources(text: str | None) -> tuple[str, ...]:
    """``"ibkr,alphavantage"`` -> ``("ibkr", "alphavantage")``: the fresh-price sources in the
    order they are asked; ``""`` / ``"none"``: none. Raises ``ValueError`` on an unknown one."""
    items = tuple(x.strip().lower() for x in str(text or "").split(",") if x.strip() and x.strip().lower() != "none")
    bad = [x for x in items if x not in FRESH_SOURCES]
    if bad or len(set(items)) != len(items):
        raise ValueError(f"fresh_prices is a list of {FRESH_SOURCES} (in the order they are asked), not {text!r}")
    return items


def since_for(as_of: str | None, last_n: int) -> str:
    """A start for the recorder that covers ``last_n`` sessions before ``as_of`` (calendar
    days, with room for holidays)."""
    end = _dt.date.fromisoformat(as_of) if as_of else _dt.date.today()
    # without a date the data may end before today (a dataset not yet updated): a month more
    slack = 10 if as_of else 40
    return (end - _dt.timedelta(days=int(last_n * 7 / 5) + slack)).isoformat()


def _cut(history: dict[str, SleeveBook], as_of: str | None, last_n: int) -> dict[str, SleeveBook]:
    days = sorted(d for d in history if as_of is None or d <= as_of)[-last_n:]
    return {d: history[d] for d in days}


def _behind(payload: dict, until: str | None) -> bool:
    """A run up to ``until`` whose books end before it (the data end earlier)."""
    if not until:
        return False
    dates = [b["date"] for b in payload.get("books", [])]
    return not dates or max(dates) < until


class EngineProvider:
    """``builders``: ``{slug: f(until) -> engine}``, ``until`` an ISO date or None; the
    engine is run inside ``record_books`` and its books kept. ``prices``: an optional
    ``f(symbols, start, end) -> {symbol: {date: (open, close)}}`` for the substitutes of
    indices."""

    def __init__(self, builders: dict[str, Callable], prices: Callable | None = None):
        self.builders = builders
        self.seconds: dict[str, float] = {}
        self._prices = prices

    def prices(self, symbols, start, end):
        """``prices(symbols, start, end)`` given at construction (the substitutes' prices)."""
        if self._prices is None:
            raise RuntimeError("no price loader given to this EngineProvider")
        return self._prices(symbols, start, end)

    def books(self, as_of: str | None = None, last_n: int = 2) -> dict[str, dict[str, SleeveBook]]:
        out = {}
        for slug, build in self.builders.items():
            t0 = time.perf_counter()
            engine = build(as_of)
            since = since_for(as_of, last_n) if as_of else None     # no date: every bar (small books)
            with record_books(since=since, last_n=last_n + 5) as rec:
                engine.run()
            out[slug] = _cut(rec.books, as_of, last_n)
            self.seconds[slug] = time.perf_counter() - t0
        return out


class PwbProvider:
    """Catalogue strategies through pwb-toolbox, one subprocess each (see the module doc)."""

    def __init__(self, meta: MetaPortfolio, cache_dir: str, *, python: str | None = None,
                 papers_dir: str | None = None, timeout: float = 4 * 3600, log=print,
                 fresh_prices: str | None = None, fresh_cache: str | None = None, ib: dict | None = None,
                 ib_factory=None):
        self.meta = meta
        self.fresh_sources = parse_sources(fresh_prices if fresh_prices is not None else meta.fresh_prices)
        self.fresh_prices = ",".join(self.fresh_sources) or None
        if "alphavantage" in self.fresh_sources:
            from .fresh import KEY_ENV

            if not os.environ.get(KEY_ENV):
                raise RuntimeError(f"fresh prices from AlphaVantage need the key in {KEY_ENV}")
        # the IB price connection: {host, port, client_id, account_client_id}; ib_factory(session,
        # cache, **ib) -> an object with collect({symbol: kind}) (tests); default IBDailyPrices
        self.ib = dict(ib or {})
        self.ib_factory = ib_factory
        self.ib_stats: dict = {}
        self._ib_session: str | None = None
        if "ibkr" in self.fresh_sources:
            from .ib_prices import IBDataConfig

            conf = IBDataConfig.from_dict(meta.fresh_ib)
            if self.ib.get("client_id") is not None:
                conf.client_id = int(self.ib["client_id"])
            acct = self.ib.get("account_client_id")
            if acct is not None and int(acct) == conf.client_id:
                raise ValueError(f"the IB price connection's client id {conf.client_id} is the account's: give it "
                                 "its own (--ib-data-client-id, fresh_ib client_id)")
            self.ib_config = conf
        self.fresh_cache = fresh_cache or os.path.join(os.path.dirname(os.path.abspath(cache_dir)), "prices")
        self.fresh: dict[str, dict] = {}
        self.warnings: list[str] = []
        self.cache_dir = cache_dir
        self.python = os.path.expanduser(python or meta.python or sys.executable)
        self.papers_dir = os.path.expanduser(papers_dir or meta.papers_dir)
        self.timeout = timeout
        self.log = log
        self.seconds: dict[str, float] = {}
        self.errors: dict[str, str] = {}            # the sleeves of the last books() that failed
        self.stale: dict[str, dict[str, str]] = {}  # their live symbols still behind, when stale
        self.stale_info: dict[str, dict[str, dict]] = {}    # {slug: {symbol: {dataset, last, cause, reason}}}
        self.universe_last: dict[str, str] = {}     # {slug: last date of its point-in-time universe}

    def _env(self) -> dict:
        env = dict(os.environ)
        src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env.update({"PWB_BACKTEST_ENGINE": "tradewind", "TQDM_DISABLE": "1", "MPLBACKEND": "Agg",
                    "PAPERS_DIR": self.papers_dir})
        return env

    def run_one(self, slug: str, path: str, out: str, *, since: str, last_n: int, until: str | None) -> dict:
        cmd = [self.python, "-m", "tradewind.live.daily.pwb_worker", path, out, "--slug", slug,
               "--since", since, "--last-n", str(last_n), "--papers", self.papers_dir]
        if until:
            cmd += ["--until", until]
        if self.fresh_prices:
            cmd += ["--fresh-prices", self.fresh_cache, "--fresh-config", json.dumps(self._fresh_config())]
            if not until and self._ib_session:      # the session the IB bars were collected for
                cmd += ["--session", self._ib_session]
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, env=self._env(), capture_output=True, text=True, timeout=self.timeout,
                              check=False)
        self.seconds[slug] = time.perf_counter() - t0
        if not os.path.exists(out):
            tail = (proc.stderr or proc.stdout or "")[-1500:]
            raise RuntimeError(f"{slug}: the strategy run wrote no book (exit {proc.returncode}):\n{tail}")
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("fresh"):
            self.fresh[slug] = payload["fresh"]
        if payload.get("error"):
            os.remove(out)                          # not cached: the next run tries again
            if payload.get("stale"):
                stale = payload["stale"]
                self.stale[slug] = dict(stale)
                raise StaleData(f"{slug}: {len(stale)} symbol(s) still stale after the fresh-price top-up "
                                "(nothing is proposed):\n" + "\n".join(f"  {s}: {why}" for s, why in stale.items()))
            raise RuntimeError(f"{slug}: {payload['error']}")
        return payload

    def _fresh_config(self) -> dict:
        return dict(self.meta.fresh_config(), sources=list(self.fresh_sources))

    def collect_ib(self, session: str | None) -> dict:
        """The IB pre-collection of ``session`` (once per session and provider): the union of
        the symbols the strategies are expected to load (``ib_prices.universe``) asked on one
        read-only connection, written to the session's cache. An IB that cannot be reached is
        a warning: AlphaVantage then serves every symbol."""
        if "ibkr" not in self.fresh_sources:
            return {}
        from .fresh import last_close_session
        from .ib_prices import IBDailyPrices, universe

        session = session or last_close_session()
        if self._ib_session == session:
            return self.ib_stats
        self._ib_session = session
        extra = sorted({"SPY"} | {sub.symbol for sub in self.meta.substitutes.values() if sub.kind == "etf"}
                       | set(self.meta.index_proxies.values()))
        wanted = universe(self.fresh_cache, session, self.cache_dir, [s.slug for s in self.meta.sleeves], extra)
        t0 = time.perf_counter()
        try:
            if self.ib_factory is not None:
                collector = self.ib_factory(session, self.fresh_cache, config=self.ib_config, **self.ib)
            else:
                collector = IBDailyPrices(self.fresh_cache, session, host=self.ib.get("host", "127.0.0.1"),
                                          port=int(self.ib.get("port", 4002)), config=self.ib_config,
                                          account_client_id=self.ib.get("account_client_id"), log=self.log)
            self.ib_stats = dict(collector.collect(wanted))
        except Exception as exc:  # noqa: BLE001 - AlphaVantage takes over, said in the warnings
            msg = f"IB prices not collected ({type(exc).__name__}: {exc}): AlphaVantage serves every symbol"
            self.log(f"[prices] {msg}")
            self.warnings.append(msg)
            self.ib_stats = {"error": f"{type(exc).__name__}: {exc}"}
        self.ib_stats.setdefault("seconds", round(time.perf_counter() - t0, 1))
        self.ib_stats["candidates"] = len(wanted)
        return self.ib_stats

    def price_report(self) -> dict:
        """Who served the fresh prices of the last ``books()`` (unique symbols over the
        sleeves): IB, AlphaVantage, stale; and the IB collection's statistics."""
        from .ib_prices import summarize

        served: dict[str, set] = {"ibkr": set(), "alphavantage": set()}
        stale: set = set()
        fallbacks: dict[str, str] = {}
        cross: dict[str, dict] = {}
        for fresh in self.fresh.values():
            for src, syms in (fresh.get("served") or {}).items():
                served.setdefault(src, set()).update(syms)
            stale.update(fresh.get("stale") or {})
            fallbacks.update(fresh.get("ib_fallbacks") or {})
            cross.update(fresh.get("crosscheck") or {})
        out = summarize(served, stale)
        gaps = sorted((abs(v["gap"]), s) for s, v in cross.items() if isinstance(v.get("gap"), (int, float)))
        out.update(sources=list(self.fresh_sources), ib=dict(self.ib_stats),
                   ib_fallbacks=len([s for s in fallbacks if s in served.get("alphavantage", set()) | stale]),
                   crosscheck={"n": len(gaps), "max": (gaps[-1][1], gaps[-1][0]) if gaps else None,
                               "over_0.5%": [s for g, s in gaps if g > 0.005]})
        return out

    def _note_stale(self, slug: str, payload: dict) -> None:
        """A sleeve's books that came with stale symbols (not strict): listed, kept."""
        if payload.get("stale"):
            self.stale[slug] = dict(payload["stale"])
            self.stale_info[slug] = {k: dict(v) for k, v in (payload.get("stale_info") or {}).items()}
            self.log(f"[prices] {slug}: {len(self.stale[slug])} symbol(s) still stale, left to the "
                     f"stale-symbol policy ({self.meta.stale_symbol_policy}): " + ", ".join(sorted(self.stale[slug])))

    def _note_fresh(self, slug: str, fresh: dict) -> None:
        served = fresh.get("served") or {}
        by = (f", {len(served.get('ibkr', []))} from IB, {len(served.get('alphavantage', []))} from AlphaVantage"
              if served.get("ibkr") else "")
        msg = (f"[prices] {slug}: {fresh['filled']} symbol(s) completed to {fresh['last']} "
               f"({fresh['requests']} AlphaVantage request(s){by}), largest gap of the chain factor "
               f"{(fresh['max_spread'] or 0.0):.3%}")
        self.log(msg)
        if fresh.get("partial_last_bars"):
            self.warnings.append(f"{slug}: the dataset's last bar differed from AlphaVantage's by more than "
                                 "0.5% (a partial bar, replaced): " + ", ".join(fresh["partial_last_bars"][:20]))

    def prices(self, symbols: list[str], start: str, end: str) -> dict[str, dict[str, tuple[float, float]]]:
        """Daily ``(open, close)`` of ``symbols`` from the PWB datasets, ``{symbol: {date: ...}}``
        (the substitutes of indices that no sleeve holds); completed from AlphaVantage like the
        strategies' loads when fresh prices are on."""
        folder = os.path.join(self.cache_dir, "prices")
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(folder, f"{'_'.join(symbols)}_{start}_{end}{'-fresh' if self.fresh_prices else ''}.json")
        if not os.path.exists(out):
            cmd = [self.python, "-m", "tradewind.live.daily.pwb_prices", out, ",".join(symbols),
                   "--start", start, "--end", end]
            if self.fresh_prices:
                cmd += ["--fresh-prices", self.fresh_cache, "--session", end,
                        "--fresh-config", json.dumps(self._fresh_config())]
            proc = subprocess.run(cmd, env=self._env(), capture_output=True, text=True, timeout=1800, check=False)
            if not os.path.exists(out):
                raise RuntimeError(f"prices of {symbols}: nothing written (exit {proc.returncode}): "
                                   f"{(proc.stderr or proc.stdout or '')[-800:]}")
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("error"):
            os.remove(out)
            raise RuntimeError(f"prices of {symbols}: {payload['error']}")
        return {sym: {d: (float(v[0]), float(v[1])) for d, v in rows.items()}
                for sym, rows in payload.get("prices", {}).items()}

    def books(self, as_of: str | None = None, last_n: int = 2, *, until: str | None = None
              ) -> dict[str, dict[str, SleeveBook]]:
        until = until or as_of
        tag = until or f"latest-{_dt.date.today().isoformat()}"
        if self.fresh_prices:
            tag += "-fresh"                         # books on the monthly data are not these
        folder = os.path.join(self.cache_dir, tag)
        os.makedirs(folder, exist_ok=True)
        out: dict[str, dict[str, SleeveBook]] = {}
        self.errors, self.stale, self.stale_info, self.universe_last = {}, {}, {}, {}
        self.fresh = {}
        if "ibkr" in self.fresh_sources:
            self.collect_ib(until)
        failures: list[BaseException] = []
        for sleeve in self.meta.sleeves:
            try:
                out[sleeve.slug] = self._one(sleeve, folder, as_of, last_n, until)
            except (StaleData, RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
                # the next sleeves still run: one run lists every sleeve in error, and their
                # books are cached for the rerun
                self.errors[sleeve.slug] = str(exc).splitlines()[0][:500]
                failures.append(exc)
                self.log(f"[signal] {sleeve.slug}: ERROR {self.errors[sleeve.slug]}")
        if failures:
            text = "\n".join(str(e) for e in failures)
            if all(isinstance(e, StaleData) for e in failures):
                raise StaleData(text)
            raise RuntimeError(f"{len(failures)} sleeve(s) in error (nothing is proposed):\n{text}")
        return out

    def _one(self, sleeve, folder: str, as_of: str | None, last_n: int, until: str | None
             ) -> dict[str, SleeveBook]:
        path = sleeve.strategy_path(self.papers_dir)
        target = os.path.join(folder, sleeve.slug + ".json")
        payload = None
        if os.path.exists(target):
            with open(target, encoding="utf-8") as fh:
                payload = json.load(fh)
            if payload.get("error") or _behind(payload, until):
                payload = None                      # the data may have caught up since: run again
            elif any("outside" not in b for b in payload.get("books", [])):
                payload = None                      # written before the books said what is outside
        cached = payload is not None
        if cached and self.fresh_prices and payload.get("fresh"):
            self.fresh[sleeve.slug] = payload["fresh"]
        if payload is None:
            if not os.path.exists(path):
                raise FileNotFoundError(f"{sleeve.slug}: no strategy file at {path}")
            self.log(f"[signal] {sleeve.slug} ...")
            payload = self.run_one(sleeve.slug, path, target, since=since_for(until, last_n),
                                   last_n=last_n + 5, until=until)
            if _behind(payload, until):
                os.remove(target)                   # not cached: the next run tries again
        if self.fresh_prices and payload.get("fresh"):
            self._note_fresh(sleeve.slug, payload["fresh"])
        self._note_stale(sleeve.slug, payload)
        if payload.get("universe_last"):            # absent: no universe, or a book cached before
            self.universe_last[sleeve.slug] = str(payload["universe_last"])[:10]
        history = {b["date"]: SleeveBook.from_dict(b) for b in payload["books"]}
        last = max(history) if history else "none"
        self.log(f"[signal] {sleeve.slug}: last close {last}"
                 + (" (cached)" if cached else f" in {self.seconds.get(sleeve.slug, 0):.0f} s"))
        return _cut(history, as_of, last_n)


class ReplayProvider:
    """Serve past closes from one run per strategy up to ``until`` (``last_n`` closes)."""

    def __init__(self, inner, until: str | None, last_n: int):
        self.inner = inner
        self.until = until
        self.last_n = last_n
        self._history: dict[str, dict[str, SleeveBook]] | None = None

    @property
    def history(self) -> dict[str, dict[str, SleeveBook]]:
        if self._history is None:
            if isinstance(self.inner, PwbProvider):
                self._history = self.inner.books(self.until, self.last_n, until=self.until)
            else:
                self._history = self.inner.books(self.until, self.last_n)
        return self._history

    @property
    def stale(self) -> dict:
        """The inner provider's stale symbols by sleeve (none for one that has no fresh prices)."""
        return getattr(self.inner, "stale", None) or {}

    @property
    def stale_info(self) -> dict:
        return getattr(self.inner, "stale_info", None) or {}

    @property
    def universe_last(self) -> dict:
        """The inner provider's last date of each sleeve's point-in-time universe."""
        return getattr(self.inner, "universe_last", None) or {}

    @property
    def errors(self) -> dict:
        errors = getattr(self.inner, "errors", None)
        return errors if isinstance(errors, dict) else {}

    def prices(self, symbols, start, end):
        loader = getattr(self.inner, "prices", None)
        if loader is None:
            raise RuntimeError("this provider loads no prices")
        return loader(symbols, start, end)

    def dates(self) -> list[str]:
        """The closes every strategy has a book for, oldest first."""
        sets = [set(h) for h in self.history.values()]
        return sorted(set.intersection(*sets)) if sets else []

    def books(self, as_of: str | None = None, last_n: int = 2) -> dict[str, dict[str, SleeveBook]]:
        return {slug: _cut(h, as_of, last_n) for slug, h in self.history.items()}


__all__ = ["FRESH_SOURCES", "EngineProvider", "PwbProvider", "ReplayProvider", "parse_sources", "since_for"]
