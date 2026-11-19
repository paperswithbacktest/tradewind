"""The day of the paper run as a scheduler starts it (tradewind.live.daily.session): no network,
no account, the simulated broker and recorded (synthetic) prices.

What is checked: the session comes from the NYSE calendar in New York time (daylight saving,
holidays, early closes); a session runs once, sends once, and a rerun the same day does
nothing; a holiday does nothing; stale prices, a loss, a missed open, an unreachable broker
and refused orders raise their alerts and their exit codes; the report says what happened;
the notification command gets the summary; the schedule examples run the right command at an
hour after the close all year.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import plistlib
import sys
from zoneinfo import ZoneInfo

import pytest

from tradewind.calendars import NYSE
from tradewind.live.daily import ReplayProvider, SimAccount
from tradewind.live.daily.runner import StaleData
from tradewind.live.daily.session import (
    EXIT_ALERT,
    EXIT_BROKER,
    EXIT_BUSY,
    EXIT_OK,
    EXIT_STALE,
    AlertConfig,
    DailySession,
    _Lock,
    check_day,
    closed_session,
    next_open,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_live_daily import UNTIL, _meta, _provider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NY = ZoneInfo("America/New_York")
PARIS = ZoneInfo("Europe/Paris")
QUIET = AlertConfig(max_gap=1.0, max_gap_day=1.0, max_loss=1.0)


def at(day: str, hh: int = 17, mm: int = 30, tz=NY) -> _dt.datetime:
    return _dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(hh, mm), tzinfo=tz)


class Recorded:
    """One replay of the three synthetic strategies, shared by the runs of a test (the
    'recorded prices'); counts how often a run asks for the provider."""

    def __init__(self, n=30):
        self.replay = ReplayProvider(_provider(), UNTIL, n)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.replay

    def sessions(self, n):
        return [d for d in self.replay.dates() if NYSE.is_trading_day(d)][-n:]


def day(tmp_path, provider, now, *, send=False, name="paper", cfg=QUIET, account=None, **kw):
    directory = str(tmp_path / name)
    make_account = account or (lambda send: SimAccount(os.path.join(directory, "sim_account.json"), cash=110_000.0))
    return DailySession(_meta(), directory, provider_factory=provider, account_factory=make_account, send=send,
                        now=now, alerts=cfg, log=lambda *a: None, **kw).run()


def _text(path) -> str:
    with open(path) as fh:
        return fh.read()


def _json(path):
    return json.loads(_text(path))


def sim_state(tmp_path, name="paper"):
    with open(tmp_path / name / "sim_account.json") as fh:
        return json.load(fh)


# -- the calendar ---------------------------------------------------------------------------------------
def test_the_session_is_read_in_new_york_time_with_holidays_and_early_closes():
    utc = _dt.timezone.utc
    assert closed_session(_dt.datetime(2026, 11, 18, 20, 0, tzinfo=utc)) == "2026-11-17"   # 15:00 NY
    assert closed_session(_dt.datetime(2026, 11, 18, 21, 15, tzinfo=utc)) == "2026-11-17"  # 16:15: margin
    assert closed_session(_dt.datetime(2026, 11, 18, 21, 30, tzinfo=utc)) == "2026-11-18"  # 16:30 NY
    assert closed_session(_dt.datetime(2026, 11, 26, 23, 0, tzinfo=utc)) == "2026-11-25"   # Thanksgiving
    assert closed_session(_dt.datetime(2026, 11, 27, 18, 45, tzinfo=utc)) == "2026-11-27"  # 13:00 close
    assert closed_session(_dt.datetime(2026, 11, 29, 12, 0, tzinfo=utc)) == "2026-11-27"   # Sunday
    # between the US and the European clock changes, New York is 5 hours behind Paris, not 6
    assert closed_session(at("2026-03-10", 22, 45, PARIS)) == "2026-03-10"                 # 17:45 NY
    assert closed_session(at("2026-10-28", 22, 45, PARIS)) == "2026-10-28"
    assert next_open("2026-11-25") == at("2026-11-27", 9, 30).astimezone(utc)
    assert next_open("2026-11-20") == at("2026-11-23", 9, 30).astimezone(utc)


def test_the_example_schedules_run_after_the_close_all_year():
    folder = os.path.join(ROOT, "docs", "examples", "schedule")
    with open(os.path.join(folder, "com.paperswithbacktest.tradewind-daily.plist"), "rb") as fh:
        plist = plistlib.load(fh)
    assert plist["ProgramArguments"][-1].endswith("tradewind-daily.sh")
    times = {(c["Hour"], c["Minute"]) for c in plist["StartCalendarInterval"]}
    assert {c["Weekday"] for c in plist["StartCalendarInterval"]} == {1, 2, 3, 4, 5}
    (hh, mm), = times
    day0 = _dt.date(2026, 1, 1)
    for k in range(365):                         # the Mac's clock is Paris time: every weekday of a year
        d = day0 + _dt.timedelta(days=k)
        if NYSE.is_trading_day(d):
            assert closed_session(at(d.isoformat(), hh, mm, PARIS)) == d.isoformat()
    with open(os.path.join(folder, "tradewind-daily.sh")) as fh:
        script = fh.read()
    assert "tradewind.live.daily.session" in script and "--send" not in script.split("ARGS=(")[1].split(")")[0]
    with open(os.path.join(folder, "tradewind-daily.timer")) as fh:
        assert "America/New_York" in fh.read()
    with open(os.path.join(folder, "tradewind-daily.cron")) as fh:
        assert "CRON_TZ=America/New_York" in fh.read()


# -- one run per session -----------------------------------------------------------------------------------
def test_a_normal_session_proposes_then_sends_once_and_reruns_do_nothing(tmp_path):
    rec = Recorded()
    s = rec.sessions(2)[0]
    first = day(tmp_path, rec, at(s))
    assert first.code == EXIT_OK and first.session == s and first.status == "done"
    assert not sim_state(tmp_path)["pending"]                            # proposed, nothing queued
    report = _json(first.report_json)
    assert report["mode"] == "propose" and report["orders"]["proposed"] and not report["orders"]["sent"]
    assert report["portfolio"]["nav"] == pytest.approx(110_000.0)
    assert set(report["tracking"]) >= {"gap_cum", "rounding", "untraded", "timing", "costs", "residual"}
    assert first.summary.startswith(f"{s} DONE | NAV 110,000") and "alerts: none" in first.summary
    assert os.path.exists(tmp_path / "paper" / "reports" / "latest.md")

    again = day(tmp_path, rec, at(s, 18))                                # the scheduler fires twice
    assert again.code == EXIT_OK and again.status == "nothing_to_do" and rec.calls == 1

    sent = day(tmp_path, rec, at(s, 20), send=True)                      # read, then send
    assert sent.code == EXIT_OK and sent.report["mode"] == "send"
    pending = sim_state(tmp_path)["pending"]
    assert len(pending) == len(sent.report["orders"]["sent"]) > 0
    for t in (at(s, 21), at(s, 22)):                                     # sending again: refused
        rerun = day(tmp_path, rec, t, send=True)
        assert rerun.status == "nothing_to_do" and rerun.code == EXIT_OK
    assert sim_state(tmp_path)["pending"] == pending
    marker = _json(tmp_path / "paper" / "sessions" / f"{s}.json")
    assert marker["phase"] == "sent" and len(marker["sent"]) == len(pending)


def test_the_next_session_fills_reconciles_and_tracks(tmp_path):
    rec = Recorded()
    s1, s2, s3 = rec.sessions(3)
    assert day(tmp_path, rec, at(s1), send=True).code == EXIT_OK
    second = day(tmp_path, rec, at(s2), send=True)
    assert second.code == EXIT_OK, second.report["alerts"]
    assert len(second.report["orders"]["filled"]) == len(_json(tmp_path / "paper" / "sessions" / f"{s1}.json")["sent"])
    assert second.report["reconciliation"]["previous"] == s1
    assert not second.report["reconciliation"]["mismatches"]
    assert abs(second.report["tracking"]["gap_cum"]) < 1e-6 * second.report["portfolio"]["nav"]
    # a position the broker changed behind the run's back
    with open(tmp_path / "paper" / "sim_account.json") as fh:
        st = json.load(fh)
    sym = next(iter(st["positions"]))
    st["positions"][sym] += 7
    with open(tmp_path / "paper" / "sim_account.json", "w") as fh:
        json.dump(st, fh)
    third = day(tmp_path, rec, at(s3), send=True)
    assert third.code == EXIT_ALERT and "fills_mismatch" in {a["code"] for a in third.report["alerts"]}


def test_a_second_run_while_one_holds_the_lock_does_nothing(tmp_path):
    rec = Recorded()
    os.makedirs(tmp_path / "paper", exist_ok=True)
    lock = _Lock(str(tmp_path / "paper" / "daily.lock"))
    assert lock.acquire()
    try:
        busy = day(tmp_path, rec, at(rec.sessions(1)[0]), send=True)
        assert busy.code == EXIT_BUSY and rec.calls == 0
    finally:
        lock.release()


def test_a_holiday_does_nothing_once_the_last_session_is_done(tmp_path):
    rec = Recorded()
    holiday = "2025-05-26"                                               # Memorial Day, a Monday
    assert not NYSE.is_trading_day(holiday)
    friday = day(tmp_path, rec, at("2025-05-23"), send=True)
    assert friday.code == EXIT_OK and friday.session == "2025-05-23"
    for t in (at(holiday, 17, 30), at(holiday, 22, 45)):
        h = day(tmp_path, rec, t, send=True)
        assert h.status == "nothing_to_do" and h.session == "2025-05-23"
    assert rec.calls == 1
    # Friday missed: Monday's run catches it up, the orders still go before Tuesday's open
    late = day(tmp_path, rec, at(holiday), send=True, name="late")
    assert late.code == EXIT_OK and late.session == "2025-05-23" and late.report["mode"] == "send"


# -- alerts ---------------------------------------------------------------------------------------------
def test_stale_prices_stop_the_day_list_the_symbols_and_a_rerun_retries(tmp_path):
    rec = Recorded()
    s = rec.sessions(1)[0]
    full = rec.replay.books(s, 3)

    errors = {"momentum": "momentum: 2 symbol(s) still stale after the fresh-price top-up"}

    class Behind:
        def __init__(self):
            self.errors = errors
            self.stale = {"momentum": {"AAA": "unknown to AlphaVantage", "BBB": "behind"}}

        def books(self, as_of=None, last_n=2):
            raise StaleData(self.errors["momentum"])

    out = day(tmp_path, lambda: Behind(), at(s), send=True)
    assert out.code == EXIT_STALE and out.status == "stale"
    assert out.report["stale_symbols"] == ["AAA", "BBB"]
    assert out.report["sleeves_in_error"] == errors
    assert not os.path.exists(tmp_path / "paper" / "sim_account.json") or not sim_state(tmp_path)["pending"]
    md = _text(out.report_md)
    assert "stale_prices" in md and "AAA" in md and "error: momentum" in md

    class OneDayBehind:                                                  # books end the day before
        def books(self, as_of=None, last_n=2):
            return {k: {d: b for d, b in h.items() if d < s} for k, h in full.items()}

    assert day(tmp_path, lambda: OneDayBehind(), at(s, 18), send=True).code == EXIT_STALE
    retry = day(tmp_path, rec, at(s, 19), send=True)                     # the data caught up
    assert retry.code == EXIT_OK and retry.report["mode"] == "send"


def test_a_loss_beyond_the_threshold_raises_an_alert(tmp_path):
    rec = Recorded()
    cfg = AlertConfig(max_gap=1.0, max_gap_day=1.0, max_loss=1e-9)
    seen = []
    for s in rec.sessions(6):
        r = day(tmp_path, rec, at(s), send=True, cfg=cfg)
        loss = r.report["portfolio"]["day_change"] < 0
        assert ("daily_loss" in {a["code"] for a in r.report["alerts"]}) == loss
        assert r.code == (EXIT_ALERT if loss else EXIT_OK)
        seen.append(loss)
    assert any(seen) and not all(seen)
    row = {"nav_real": 96_000.0, "real_change": -4_000.0, "gap_cum": 0.0, "gap_day": 0.0}
    assert [a.code for a in check_day(row, [], AlertConfig(max_loss=0.03))] == ["daily_loss"]
    assert check_day(row, [], AlertConfig(max_loss=0.05)) == []
    row = {"nav_real": 100_000.0, "real_change": 0.0, "gap_cum": -1_500.0, "gap_day": -10.0}
    assert [a.code for a in check_day(row, [], AlertConfig())] == ["tracking_gap"]


def test_a_send_after_the_open_cut_off_is_refused_and_alerted(tmp_path):
    rec = Recorded()
    s1, s2 = rec.sessions(2)
    late = at(s2, 10, 0)                                                 # the session after s1 is open
    out = day(tmp_path, rec, late, send=True)
    assert out.session == s1 and out.code == EXIT_ALERT
    assert [a["code"] for a in out.report["alerts"]] == ["missed_open"]
    assert out.report["mode"] == "propose (send refused)" and not sim_state(tmp_path)["pending"]
    assert day(tmp_path, rec, at(s1, 23), send=True, name="ontime").report["mode"] == "send"


def test_an_unreachable_broker_and_refused_orders(tmp_path):
    rec = Recorded()
    s = rec.sessions(1)[0]

    def down(send):
        raise ConnectionRefusedError("[Errno 61] Connect call failed ('127.0.0.1', 7497)")

    out = day(tmp_path, rec, at(s), send=True, account=down)
    assert out.code == EXIT_BROKER and out.report["alerts"][0]["code"] == "broker_unreachable"
    assert "7497" in _text(out.report_md)

    directory = str(tmp_path / "refused")

    class Refusing(SimAccount):
        def place(self, orders, date):
            return [{"id": f"x{i}", "symbol": o.symbol, "quantity": o.quantity, "status": "rejected",
                     "reason": "no borrow"} for i, o in enumerate(orders)]

    out = day(tmp_path, rec, at(s), send=True, name="refused",
              account=lambda send: Refusing(os.path.join(directory, "sim.json")))
    assert out.code == EXIT_ALERT and "orders_rejected" in {a["code"] for a in out.report["alerts"]}


def test_the_notification_command_gets_the_summary_on_alerts_only(tmp_path):
    rec = Recorded()
    s1, s2 = rec.sessions(2)
    sink = tmp_path / "notified.txt"
    cmd = (f"{sys.executable} -c \"import os, sys; open({str(sink)!r}, 'a').write(sys.stdin.read() + "
           f"os.environ['TW_DAILY_EXIT_CODE'] + '\\n')\"")
    ok = day(tmp_path, rec, at(s1), notify_cmd=cmd)
    assert ok.code == EXIT_OK and not sink.exists()
    bad = day(tmp_path, rec, at(s2, 10), send=True, notify_cmd=cmd, name="late")   # missed open
    assert bad.code == EXIT_ALERT
    assert sink.read_text() == bad.summary + "\n" + str(EXIT_ALERT) + "\n"
    assert _json(bad.report_json)["notify"]["exit"] == 0


def test_books_that_end_before_the_session_are_not_cached(tmp_path, monkeypatch):
    import types

    from tradewind.live.daily import MetaPortfolio, PwbProvider, Sleeve

    (tmp_path / "strategy.py").write_text("")
    meta = MetaPortfolio([Sleeve("s1", 1.0, path=str(tmp_path / "strategy.py"))])
    runs = []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        out = cmd[cmd.index("-m") + 3]
        with open(out, "w") as fh:
            json.dump({"books": [{"date": "2026-10-30", "nav": 1.0, "positions": {}}]}, fh)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tradewind.live.daily.providers.subprocess.run", fake_run)
    provider = PwbProvider(meta, str(tmp_path / "books"), log=lambda *a: None)
    for _ in range(2):                                   # the monthly data end on 2026-10-30
        assert list(provider.books("2026-11-18", 3)["s1"]) == ["2026-10-30"]
    assert len(runs) == 2                                # run again, in case the data caught up
    assert not os.listdir(tmp_path / "books" / "2026-11-18")
