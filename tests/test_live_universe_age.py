"""The age of the point-in-time universe the sleeves use, in the daily run, without the network.

pwb-toolbox's harness carries the last composition of ``Stocks-Daily-LiquidUniverse`` forward
past the end of the table, so a table that is not rebuilt breaks nothing: the names that
entered or left since are just not seen. The session says how old it is (one line in the
report) and raises ``universe_age`` once it is older than ``universe_max_age_days`` (35).

What is checked: the worker reads the last date off the tables pwb-toolbox already holds (none
loaded: None); the provider keeps it per sleeve, a cached book without it says nothing, a
replay passes it through; the status takes the oldest date and its threshold is strict; the
session raises the alert past the threshold, writes the line either way, and says nothing when
no sleeve uses a point-in-time universe.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import types

import pandas as pd
import pytest

from tradewind.live.daily import (
    MetaPortfolio,
    PwbProvider,
    ReplayProvider,
    SimAccount,
    Sleeve,
    pwb_worker,
)
from tradewind.live.daily.books import SleeveBook
from tradewind.live.daily.session import (
    AlertConfig,
    DailySession,
    universe_line,
    universe_message,
    universe_status,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_live_daily import UNTIL, _provider

DAY = "2026-11-19"
QUIET = AlertConfig(max_gap=1.0, max_gap_day=1.0, max_loss=1.0)
MODULE = "pwb_toolbox.datasets.pit_universe"


# -- the worker ------------------------------------------------------------------------------------------
def _table(*days):
    return pd.DataFrame({"date": pd.to_datetime(list(days)), "symbol": ["AAPL"] * len(days),
                         "rank": [1] * len(days)})


def test_the_worker_reads_the_last_date_of_the_tables_loaded(monkeypatch):
    monkeypatch.delitem(sys.modules, MODULE, raising=False)
    assert pwb_worker.universe_last() is None                       # pwb-toolbox's module never imported
    fake = types.ModuleType(MODULE)
    fake._cache = {}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, MODULE, fake)
    assert pwb_worker.universe_last() is None                       # imported, no universe loaded
    fake._cache = {(500, 0): _table("2026-09-30", "2026-10-07"),   # type: ignore[attr-defined]
                   (1000, 12): _table("2026-10-06"), (100, 0): _table()}
    assert pwb_worker.universe_last() == "2026-10-07"


# -- the provider ----------------------------------------------------------------------------------------
def test_the_provider_keeps_each_sleeve_s_date(tmp_path, monkeypatch):
    meta = MetaPortfolio([Sleeve("pit", 0.5, path=str(tmp_path / "s.py")),
                          Sleeve("plain", 0.5, path=str(tmp_path / "s.py"))])
    (tmp_path / "s.py").write_text("")
    book = SleeveBook(DAY, 100.0, positions={"SPY": 1.0}, target={"SPY": 1.0}, close={"SPY": 10.0})

    def fake_run(cmd, **kw):
        slug = cmd[cmd.index("--slug") + 1]
        with open(cmd[cmd.index("-m") + 3], "w") as fh:
            json.dump({"books": [book.to_dict()],
                       "universe_last": "2026-10-07" if slug == "pit" else None}, fh)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tradewind.live.daily.providers.subprocess.run", fake_run)
    provider = PwbProvider(meta, str(tmp_path / "books"), log=lambda *a: None)
    provider.books(DAY, 2)
    assert provider.universe_last == {"pit": "2026-10-07"}
    cached = PwbProvider(meta, str(tmp_path / "books"), log=lambda *a: None)
    cached.books(DAY, 2)                                            # from the cache: the same
    assert cached.universe_last == {"pit": "2026-10-07"}

    replay = ReplayProvider(PwbProvider(meta, str(tmp_path / "books"), log=lambda *a: None), DAY, 2)
    replay.history                                                  # noqa: B018 - runs the inner provider
    assert replay.universe_last == {"pit": "2026-10-07"}

    old = tmp_path / "books" / DAY / "pit.json"                     # cached before the field existed
    old.write_text(json.dumps({"books": [book.to_dict()]}))
    again = PwbProvider(meta, str(tmp_path / "books"), log=lambda *a: None)
    again.books(DAY, 2)
    assert again.universe_last == {}                                # unknown: nothing said, not rerun


# -- the status and the meta-portfolio ------------------------------------------------------------------
def test_the_status():
    assert universe_status(None, DAY, 35) is None and universe_status({"a": None}, DAY, 35) is None
    u = universe_status({"b": "2026-10-15", "a": "2026-10-07"}, DAY, 35)
    assert u == {"last": "2026-10-07", "age_days": 43, "max_age_days": 35, "old": True, "sleeves": ["a", "b"]}
    assert universe_line(u) == "universe as of 07/10 (43 days)"
    assert "ends on 2026-10-07, 43 days before the session (above 35" in universe_message(u)
    assert not universe_status({"a": "2026-10-15"}, DAY, 35)["old"]           # 35 days: not above
    assert universe_status({"a": "2026-10-14"}, DAY, 35)["old"]               # 36 days


def test_the_meta_portfolio_option():
    assert MetaPortfolio([Sleeve("a", 1.0)]).universe_max_age_days == 35.0
    assert MetaPortfolio([Sleeve("a", 1.0)], universe_max_age_days=10).universe_max_age_days == 10.0
    with pytest.raises(ValueError, match="universe_max_age_days"):
        MetaPortfolio([Sleeve("a", 1.0)], universe_max_age_days=0)


# -- the session ----------------------------------------------------------------------------------------
class WithUniverse:
    """A replay whose sleeves say which point-in-time universe date they ran on."""

    def __init__(self, replay, lasts: dict):
        self.replay, self.universe_last = replay, dict(lasts)
        self.errors: dict = {}

    def books(self, as_of=None, last_n=2):
        return self.replay.books(as_of, last_n)


def _session(tmp_path, lasts: dict, **meta):
    from tradewind.calendars import NYSE

    replay = ReplayProvider(_provider(), UNTIL, 4)
    day = [x for x in replay.dates() if NYSE.is_trading_day(x)][-1]
    directory = str(tmp_path / "paper")
    opts = {"leverage": 1.0, "max_gross": 4.0, "lot": 0.0, "min_order_notional": 0.0}
    opts.update(meta)
    m = MetaPortfolio([Sleeve(slug, 1.0 / len(replay.history)) for slug in replay.history], **opts)
    lasts = {k: (_dt.date.fromisoformat(day) - _dt.timedelta(days=v)).isoformat() for k, v in lasts.items()}
    out = DailySession(m, directory, provider_factory=lambda: WithUniverse(replay, lasts),
                       account_factory=lambda send: SimAccount(os.path.join(directory, "sim.json"), cash=110_000.0),
                       session=day, now=_dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(23, 0),
                                                             tzinfo=_dt.timezone.utc),
                       alerts=QUIET, log=lambda *a: None).run()
    with open(out.report_md) as fh:
        return out, fh.read()


def test_the_session_raises_universe_age(tmp_path):
    out, md = _session(tmp_path, {"momentum": 40})
    alerts = {a["code"]: a["message"] for a in out.report["alerts"]}
    assert "40 days before the session (above 35" in alerts["universe_age"]
    assert out.report["universe"]["age_days"] == 40 and out.report["universe"]["sleeves"] == ["momentum"]
    assert "(40 days), 1 sleeve(s) on a point-in-time universe, **older than 35 days**" in md


def test_a_recent_universe_is_a_line_and_no_alert(tmp_path):
    out, md = _session(tmp_path, {"momentum": 40}, universe_max_age_days=45)
    assert "universe_age" not in {a["code"] for a in out.report["alerts"]}
    assert "(40 days), 1 sleeve(s) on a point-in-time universe\n" in md


def test_no_universe_no_line(tmp_path):
    out, md = _session(tmp_path, {})
    assert out.report["universe"] is None and "universe as of" not in md
    assert "universe_age" not in {a["code"] for a in out.report["alerts"]}
