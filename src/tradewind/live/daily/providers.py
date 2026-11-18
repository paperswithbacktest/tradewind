"""Where the sleeves' books come from: a strategy run in signal mode up to a close.

``EngineProvider`` runs Tradewind strategies the caller builds (a function that returns a
ready engine for a given last date): tests, and any strategy written for Tradewind.

``PwbProvider`` runs catalogue strategies (``strategy.py`` of Papers With Backtest) through
pwb-toolbox on the Tradewind engine, one subprocess per strategy so that memory comes back
after each and a strategy that exits hard does not take the run down; the environment that
has pwb-toolbox is ``python`` (``pwb_worker.py`` is what it executes). Books are cached per
day in ``cache_dir``: a second run of the same day (propose, then send) reuses them.

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


class EngineProvider:
    """``builders``: ``{slug: f(until) -> engine}``, ``until`` an ISO date or None; the
    engine is run inside ``record_books`` and its books kept."""

    def __init__(self, builders: dict[str, Callable]):
        self.builders = builders
        self.seconds: dict[str, float] = {}

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
                 papers_dir: str | None = None, timeout: float = 4 * 3600, log=print):
        self.meta = meta
        self.cache_dir = cache_dir
        self.python = os.path.expanduser(python or meta.python or sys.executable)
        self.papers_dir = os.path.expanduser(papers_dir or meta.papers_dir)
        self.timeout = timeout
        self.log = log
        self.seconds: dict[str, float] = {}
        self.errors: dict[str, str] = {}

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
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, env=self._env(), capture_output=True, text=True, timeout=self.timeout,
                              check=False)
        self.seconds[slug] = time.perf_counter() - t0
        if not os.path.exists(out):
            tail = (proc.stderr or proc.stdout or "")[-1500:]
            raise RuntimeError(f"{slug}: the strategy run wrote no book (exit {proc.returncode}):\n{tail}")
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        if payload.get("error"):
            os.remove(out)                          # not cached: the next run tries again
            raise RuntimeError(f"{slug}: {payload['error']}")
        return payload

    def books(self, as_of: str | None = None, last_n: int = 2, *, until: str | None = None
              ) -> dict[str, dict[str, SleeveBook]]:
        until = until or as_of
        tag = until or f"latest-{_dt.date.today().isoformat()}"
        folder = os.path.join(self.cache_dir, tag)
        os.makedirs(folder, exist_ok=True)
        out: dict[str, dict[str, SleeveBook]] = {}
        for sleeve in self.meta.sleeves:
            path = sleeve.strategy_path(self.papers_dir)
            target = os.path.join(folder, sleeve.slug + ".json")
            payload = None
            if os.path.exists(target):
                with open(target, encoding="utf-8") as fh:
                    payload = json.load(fh)
                if payload.get("error"):
                    payload = None
            cached = payload is not None
            if payload is None:
                if not os.path.exists(path):
                    raise FileNotFoundError(f"{sleeve.slug}: no strategy file at {path}")
                self.log(f"[signal] {sleeve.slug} ...")
                payload = self.run_one(sleeve.slug, path, target, since=since_for(until, last_n),
                                       last_n=last_n + 5, until=until)
            history = {b["date"]: SleeveBook.from_dict(b) for b in payload["books"]}
            out[sleeve.slug] = _cut(history, as_of, last_n)
            last = max(history) if history else "none"
            self.log(f"[signal] {sleeve.slug}: last close {last}"
                     + (" (cached)" if cached else f" in {self.seconds.get(sleeve.slug, 0):.0f} s"))
        return out


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

    def dates(self) -> list[str]:
        """The closes every strategy has a book for, oldest first."""
        sets = [set(h) for h in self.history.values()]
        return sorted(set.intersection(*sets)) if sets else []

    def books(self, as_of: str | None = None, last_n: int = 2) -> dict[str, dict[str, SleeveBook]]:
        return {slug: _cut(h, as_of, last_n) for slug, h in self.history.items()}


__all__ = ["EngineProvider", "PwbProvider", "ReplayProvider", "since_for"]
