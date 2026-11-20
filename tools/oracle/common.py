"""Shared pieces of the corpus oracle harness (spec/03-oracle.md).

The oracle is the set of daily NAV paths the PWB catalogue's strategies produce on the
reference engine (backtrader, run as a black box in its own environment). Tradewind must
reproduce every one of them. Nothing here imports Tradewind or backtrader: these helpers
read and compare files, so the tests can run them anywhere.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LOCK_DIR = os.path.join(ROOT, "oracle")
SUBSET_DIR = os.path.join(HERE, "subsets")
#: the Hub repository holding the versioned reference sets (private, not gated)
ORACLE_REPO = "paperswithbacktest/Tradewind-Oracle"
#: the organisation whose datasets the corpus reads
HUB_ORG = "paperswithbacktest"
#: per-bar tolerance of spec/03 for kept behaviours, relative to max(1, |reference|)
DEFAULT_TOL = 1e-9
#: wall-clock time zone of the machines that wrote ``measured_at`` (naive local times)
HOST_TZ = {"mac": "Europe/Paris", "ns3208333": "UTC"}


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                return h.hexdigest()
            h.update(b)


def to_utc(stamp: str, host: str) -> datetime:
    """A naive ``measured_at`` written by ``time.strftime`` on ``host``, as an aware UTC time."""
    from zoneinfo import ZoneInfo

    t = datetime.fromisoformat(stamp)
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo(HOST_TZ.get(host, "UTC")))
    return t.astimezone(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compare(ref: list, run: list, tol: float = DEFAULT_TOL) -> dict:
    """Bar-by-bar comparison of two NAV paths ``[{"date", "value"}, ...]``.

    Same dates in the same order, and ``|run - ref| <= tol * max(1, |ref|)`` on every bar
    (NaN only matches NaN). Returns ``same`` plus where and how far it first breaks.
    """
    if ref is None or run is None:
        return {"same": False, "missing": "reference" if ref is None else "run"}
    if len(ref) != len(run) or any(str(a["date"]) != str(b["date"]) for a, b in zip(ref, run)):
        n = min(len(ref), len(run))
        k = next((i for i in range(n) if str(ref[i]["date"]) != str(run[i]["date"])), n)
        return {"same": False, "dates_differ": True, "n_ref": len(ref), "n_run": len(run), "first_index": k,
                "ref_date": str(ref[k]["date"]) if k < len(ref) else None,
                "run_date": str(run[k]["date"]) if k < len(run) else None}
    worst, first = 0.0, None
    for i, (a, b) in enumerate(zip(ref, run)):
        va, vb = float(a["value"]), float(b["value"])
        if math.isnan(va) or math.isnan(vb):
            err = 0.0 if (math.isnan(va) and math.isnan(vb)) else math.inf
        else:
            err = abs(va - vb) / max(1.0, abs(va))
        worst = max(worst, err)
        if err > tol and first is None:
            first = (i, str(a["date"]), va, vb)
    out = {"same": first is None, "n": len(ref), "max_rel": worst}
    if first:
        out.update(first_index=first[0], first_date=first[1], ref_value=first[2], run_value=first[3],
                   final_ref=float(ref[-1]["value"]), final_run=float(run[-1]["value"]))
    return out


class HubHistory:
    """Commit history of the organisation's datasets: which revision a run read at a time.

    ``commits`` maps ``repo_id`` to ``[(created_at UTC, sha), ...]``. pwb-toolbox resolves a
    dataset's revision once per process (``dataset_info`` -> ``sha``), so a run started at
    ``t`` read the last commit made at or before ``t``.
    """

    def __init__(self, commits: dict):
        self.commits = {r: sorted((_aware(t), s) for t, s in v) for r, v in commits.items()}
        self._times = {r: [t for t, _ in v] for r, v in self.commits.items()}

    @classmethod
    def load(cls, path: str) -> HubHistory:
        with open(path) as fh:
            data = json.load(fh)
        return cls({r: [(t, s) for t, s in v] for r, v in data["commits"].items()})

    def revision_at(self, repo: str, t: datetime):
        times = self._times.get(repo)
        if not times:
            return None
        k = bisect.bisect_right(times, t)
        return self.commits[repo][k - 1][1] if k else None

    def revisions_at(self, t: datetime) -> dict:
        out = {}
        for repo in self.commits:
            sha = self.revision_at(repo, t)
            if sha:
                out[repo] = sha
        return out

    def moved_between(self, t0: datetime, t1: datetime) -> list:
        """Datasets with a commit in ``(t0, t1]``: their revision depends on when in the run
        the strategy first read them."""
        out = []
        for repo, times in self._times.items():
            if bisect.bisect_right(times, t1) > bisect.bisect_right(times, t0):
                out.append(repo)
        return sorted(out)


def _aware(t):
    if isinstance(t, str):
        t = datetime.fromisoformat(t.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def started_at(measured_at_utc: datetime, seconds) -> datetime:
    """Start of a measurement from its end stamp and duration (``measured_at`` is written last)."""
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        s = 0.0
    return measured_at_utc - timedelta(seconds=0.0 if math.isnan(s) else s)


def read_lines(path: str) -> list:
    with open(path) as fh:
        return [ln.split("#", 1)[0].strip() for ln in fh if ln.split("#", 1)[0].strip()]
