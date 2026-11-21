"""A sleeve in error no longer stops the day (tradewind.live.daily: meta, providers, aggregate,
runner, session), without the network.

What is checked: under ``sleeve_error_policy: skip`` (the default) a sleeve whose run failed,
whose book is missing, whose data end before the session or with too much of its gross on
stale symbols is set aside for the day -- its part of the book stays its book of the day before
at its units of the day before (what the account holds for it does not move, no new line), the
other sleeves are proposed as on a normal day; a sleeve in error on the first day gets nothing;
above ``sleeve_error_max_weight`` of the summed weights nothing is proposed (``StaleData``, exit
code 3); ``block`` stops the day as before; the gross cap scales the other sleeves only; the
provider returns the books that ran; the session raises one ``sleeve_skipped`` alert per sleeve
(exit code 4) and says it in the report.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import types

import pytest

from tradewind.live.daily import (
    DailyRunner,
    MetaPortfolio,
    PwbProvider,
    ReplayProvider,
    SimAccount,
    Sleeve,
)
from tradewind.live.daily.aggregate import combine, gross, plan_day
from tradewind.live.daily.books import SleeveBook
from tradewind.live.daily.runner import SleevesInError, StaleData
from tradewind.live.daily.session import (
    EXIT_ALERT,
    EXIT_FAILED,
    EXIT_STALE,
    AlertConfig,
    DailySession,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_live_daily import UNTIL, _meta, _provider

QUIET = AlertConfig(max_gap=1.0, max_gap_day=1.0, max_loss=1.0)


class Failing:
    """A replay whose sleeves in ``failed`` have no book and an error, as ``PwbProvider`` leaves
    them under ``skip``; ``behind``: sleeves whose books end the day before ``as_of``."""

    def __init__(self, replay, failed=(), behind=()):
        self.replay = replay
        self.failed = set(failed)
        self.behind = set(behind)
        self.errors: dict = {}
        self.stale: dict = {}
        self.stale_info: dict = {}

    def books(self, as_of=None, last_n=2):
        out = self.replay.books(as_of, last_n)
        self.errors = {s: f"{s}: the strategy run wrote no book (exit 1)" for s in self.failed}
        out = {k: v for k, v in out.items() if k not in self.failed}
        for k in self.behind:
            out[k] = {d: b for d, b in out[k].items() if as_of is None or d < as_of}
        return out


def same_month_days(replay):
    """Two consecutive closes in the same month (no monthly reset between them)."""
    dates = replay.dates()
    for a, b in zip(dates[-2::-1], dates[:0:-1]):
        if a[:7] == b[:7]:
            return a, b
    raise AssertionError("no two closes in one month")


def day_one(tmp_path, meta, name):
    replay = ReplayProvider(_provider(), UNTIL, 30)
    d1, d2 = same_month_days(replay)
    directory = str(tmp_path / name)
    account = SimAccount(os.path.join(directory, "sim.json"), cash=110_000.0)
    DailyRunner(meta, replay, account, directory, log=lambda *a: None).run_day(d1, send=True)
    return replay, account, directory, d1, d2


# -- the runner ---------------------------------------------------------------------------------------
def test_a_failed_sleeve_keeps_its_part_and_the_others_are_proposed(tmp_path):
    meta = _meta()
    assert meta.sleeve_error_policy == "skip" and meta.sleeve_error_max_weight == 0.5
    replay, account, directory, d1, d2 = day_one(tmp_path, meta, "skip")
    normal_replay, normal_account, normal_dir, _, _ = day_one(tmp_path, meta, "normal")
    normal = DailyRunner(meta, normal_replay, normal_account, normal_dir, log=lambda *a: None).run_day(d2)

    provider = Failing(replay, failed=["momentum"])
    report = DailyRunner(meta, provider, account, directory, log=lambda *a: None).run_day(d2)
    (row,) = report.skipped
    assert row["slug"] == "momentum" and row["weight"] == 0.4 and "wrote no book" in row["cause"]
    assert row["book_date"] == d1 and not report.plan.rebalanced
    with open(os.path.join(directory, "state.json")) as fh:
        state = json.load(fh)["days"]
    k = state[d1]["units"]["momentum"]
    assert report.plan.units["momentum"] == k and row["units"] == k
    # the plan is the normal day's, momentum's part of today replaced by its part of yesterday
    before = replay.books(d1, 1)["momentum"][d1].target
    today = replay.books(d2, 1)["momentum"][d2].target
    for sym in set(normal.plan.exact) | set(report.plan.exact):
        want = normal.plan.exact.get(sym, 0.0) - k * today.get(sym, 0.0) + k * before.get(sym, 0.0)
        assert report.plan.exact.get(sym, 0.0) == pytest.approx(want, abs=1e-6)
    assert state[d2]["sleeves"]["momentum"]["skipped"].startswith("momentum: the strategy run wrote no book")
    assert state[d2]["sleeves"]["momentum"]["target"] == before
    assert any(w.startswith("sleeve skipped: momentum (weight 0.4000)") for w in report.warnings)
    assert provider.errors["momentum"]


def test_the_maximum_weight_is_set_in_the_meta_portfolio(tmp_path):
    meta = MetaPortfolio([Sleeve("momentum", 0.4), Sleeve("longshort", 0.3), Sleeve("sixtyforty", 0.3)],
                         leverage=2.0, lot=0.0, sleeve_error_max_weight=1.0)
    replay, account, directory, _d1, d2 = day_one(tmp_path, meta, "all-but-one")
    # every sleeve but momentum fails (0.6 of the weights): above the default 50 %, allowed at 100 %
    provider = Failing(replay, failed=["longshort", "sixtyforty"])
    with pytest.raises(SleevesInError):
        DailyRunner(MetaPortfolio(meta.sleeves, leverage=2.0, lot=0.0), provider, account, directory,
                    log=lambda *a: None).run_day(d2)
    report = DailyRunner(meta, provider, account, directory, log=lambda *a: None).run_day(d2)
    assert {x["slug"] for x in report.skipped} == {"longshort", "sixtyforty"}
    every = Failing(replay, failed=["momentum", "longshort", "sixtyforty"])
    with pytest.raises(StaleData, match="no sleeve has a book"):
        DailyRunner(meta, every, account, directory, log=lambda *a: None).run_day(d2)


def test_a_sleeve_behind_the_session_is_skipped(tmp_path):
    replay, account, directory, d1, d2 = day_one(tmp_path, _meta(), "behind")
    report = DailyRunner(_meta(), Failing(replay, behind=["sixtyforty"]), account, directory,
                         log=lambda *a: None).run_day(d2)
    (row,) = report.skipped
    assert row["slug"] == "sixtyforty" and f"last close {d1}, not {d2}" in row["cause"]


def test_above_the_maximum_weight_nothing_is_proposed(tmp_path):
    replay, account, directory, _d1, d2 = day_one(tmp_path, _meta(), "over")
    provider = Failing(replay, failed=["momentum", "longshort"])                  # 0.7 of the weights
    with pytest.raises(SleevesInError, match="70.0% of the summed weights, above sleeve_error_max_weight 50%"):
        DailyRunner(_meta(), provider, account, directory, log=lambda *a: None).run_day(d2)
    assert set(provider.errors) == {"momentum", "longshort"}
    report = DailyRunner(_meta(sleeve_error_max_weight=0.75), provider, account, directory,
                         log=lambda *a: None).run_day(d2)
    assert len(report.skipped) == 2


def test_block_stops_the_day_as_before(tmp_path):
    replay, account, directory, _d1, d2 = day_one(tmp_path, _meta(), "block")
    with pytest.raises(RuntimeError, match="no book for"):
        DailyRunner(_meta(sleeve_error_policy="block"), Failing(replay, failed=["momentum"]), account, directory,
                    log=lambda *a: None).run_day(d2)
    with pytest.raises(StaleData, match="sixtyforty: last close"):
        DailyRunner(_meta(sleeve_error_policy="block"), Failing(replay, behind=["sixtyforty"]), account,
                    directory, log=lambda *a: None).run_day(d2)


def test_a_sleeve_in_error_on_the_first_day_gets_nothing(tmp_path):
    replay = ReplayProvider(_provider(), UNTIL, 4)
    d = replay.dates()[-1]
    directory = str(tmp_path / "first")
    account = SimAccount(os.path.join(directory, "sim.json"), cash=110_000.0)
    report = DailyRunner(_meta(), Failing(replay, failed=["momentum"]), account, directory,
                         log=lambda *a: None).run_day(d)
    (row,) = report.skipped
    assert row["units"] == 0.0 and not row["kept"] and report.plan.units["momentum"] == 0.0
    assert "nothing held for it" in report.warnings[[w.startswith("sleeve skipped") for w in report.warnings].index(True)]


def test_too_much_stale_gross_skips_the_sleeve_under_skip(tmp_path):
    from test_live_stale import two_days

    runner, provider, d2, _ = two_days(tmp_path, _meta(), lambda book: [min(book.positions)])
    report = runner.run_day(d2, send=False)
    (row,) = report.skipped
    assert row["slug"] == "momentum" and "above stale_symbol_max_weight 20%" in row["cause"]
    assert not report.stale                         # its stale symbol was only momentum's
    assert "momentum" in provider.errors


# -- the plan -------------------------------------------------------------------------------------------
def test_the_gross_cap_scales_the_other_sleeves_only():
    meta = MetaPortfolio([Sleeve("a", 0.5), Sleeve("b", 0.5)], max_gross=1.0, lot=0.0)
    books = {"a": SleeveBook("2026-11-18", 100.0, target={"X": 10.0}, close={"X": 10.0, "Y": 10.0}),
             "b": SleeveBook("2026-11-18", 100.0, target={"Y": 10.0}, close={"X": 10.0, "Y": 10.0})}
    plan = plan_day(meta, books, {}, 100.0, "2026-11-18", units={"a": 0.6, "b": 0.6}, last_reset="2026-11-17",
                    weights_seen=meta.weights, kept={"a": 0.6})
    assert plan.units["a"] == 0.6 and plan.exact["X"] == pytest.approx(6.0)
    assert plan.units["b"] == pytest.approx(0.4, abs=1e-9) and gross(plan.exact, {"X": 10.0, "Y": 10.0}) <= 100.0
    alone = plan_day(meta, books, {}, 100.0, "2026-11-18", units={"a": 1.2, "b": 0.6}, last_reset="2026-11-17",
                     weights_seen=meta.weights, kept={"a": 1.2})       # the kept part alone is above the cap
    assert alone.units["b"] == 0.0 and alone.exact == {"X": pytest.approx(12.0)}
    reset = plan_day(meta, books, {}, 100.0, "2026-12-01", units={"a": 0.3, "b": 0.3}, last_reset="2026-11-17",
                     weights_seen=meta.weights, kept={"a": 0.3})       # a reset leaves the kept units
    assert reset.rebalanced and reset.units["a"] == 0.3 and reset.units["b"] == pytest.approx(0.5)
    assert combine(reset.units, books) == {"X": pytest.approx(3.0), "Y": pytest.approx(5.0)}


def test_the_meta_portfolio_options():
    with pytest.raises(ValueError, match="sleeve_error_policy"):
        _meta(sleeve_error_policy="ignore")
    with pytest.raises(ValueError, match="sleeve_error_max_weight"):
        _meta(sleeve_error_max_weight=1.5)
    assert _meta(sleeve_error_policy="BLOCK").sleeve_error_policy == "block"


# -- the provider ----------------------------------------------------------------------------------------
def test_the_provider_returns_the_books_that_ran(tmp_path, monkeypatch):
    for name in ("good", "bad"):
        (tmp_path / f"{name}.py").write_text("")
    book = SleeveBook("2026-11-18", 100.0, positions={"AAA": 1.0}, target={"AAA": 1.0}, close={"AAA": 10.0})

    def fake_run(cmd, **kw):
        if cmd[cmd.index("-m") + 2].endswith("good.py"):
            with open(cmd[cmd.index("-m") + 3], "w") as fh:
                json.dump({"books": [book.to_dict()]}, fh)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return types.SimpleNamespace(returncode=1, stdout="", stderr="Traceback ...\nKeyError: 'close'")

    monkeypatch.setattr("tradewind.live.daily.providers.subprocess.run", fake_run)
    sleeves = [Sleeve("good", 0.6, path=str(tmp_path / "good.py")), Sleeve("bad", 0.4, path=str(tmp_path / "bad.py"))]
    provider = PwbProvider(MetaPortfolio(sleeves), str(tmp_path / "books"), log=lambda *a: None)
    out = provider.books("2026-11-18", 2)
    assert list(out) == ["good"] and "wrote no book" in provider.errors["bad"]
    strict = PwbProvider(MetaPortfolio(sleeves, sleeve_error_policy="block"), str(tmp_path / "books2"),
                         log=lambda *a: None)
    with pytest.raises(RuntimeError, match="1 sleeve.s. in error"):
        strict.books("2026-11-18", 2)


# -- the session -----------------------------------------------------------------------------------------
def session(tmp_path, provider, meta, when, name):
    directory = str(tmp_path / name)
    account = (lambda send: SimAccount(os.path.join(directory, "sim_account.json"), cash=110_000.0))
    return DailySession(meta, directory, provider_factory=lambda: provider, account_factory=account,
                        session=when, now=_dt.datetime.combine(_dt.date.fromisoformat(when), _dt.time(23, 0),
                                                               tzinfo=_dt.timezone.utc),
                        alerts=QUIET, log=lambda *a: None).run()


def test_the_session_proposes_the_others_with_a_sleeve_skipped_alert(tmp_path):
    from tradewind.calendars import NYSE

    replay = ReplayProvider(_provider(), UNTIL, 4)
    d = [x for x in replay.dates() if NYSE.is_trading_day(x)][-1]
    out = session(tmp_path, Failing(replay, failed=["momentum"]), _meta(), d, "skip")
    assert out.code == EXIT_ALERT and out.status == "alert"
    alerts = [a for a in out.report["alerts"] if a["code"] == "sleeve_skipped"]
    assert len(alerts) == 1 and alerts[0]["message"].startswith("momentum (weight 0.4000) set aside for the day")
    assert "wrote no book" in alerts[0]["message"]
    assert out.report["orders"]["proposed"] and out.report["sleeves_skipped"][0]["slug"] == "momentum"
    assert "1 sleeve(s) skipped (40% of the weights)" in out.summary
    with open(out.report_md) as fh:
        md = fh.read()
    assert "skipped (holdings kept): momentum: the strategy run wrote no book" in md

    over = session(tmp_path, Failing(replay, failed=["momentum", "longshort"]), _meta(), d, "over")
    assert over.code == EXIT_STALE and not over.report["orders"]["proposed"]
    assert set(over.report["sleeves_in_error"]) == {"momentum", "longshort"}
    assert "sleeve_error_max_weight" in over.report["alerts"][0]["message"]

    block = session(tmp_path, Failing(replay, failed=["momentum"]), _meta(sleeve_error_policy="block"), d, "block")
    assert block.code == EXIT_FAILED and not block.report["orders"]["proposed"]
