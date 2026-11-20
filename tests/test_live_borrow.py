"""The day's cost of borrowing the shorts (``tradewind.live.daily.borrow``), without network:
the Hub and IB's tick are replaced by functions and stubs."""

from __future__ import annotations

import json
import os
import sys

import pytest

from tradewind.live.daily import MetaPortfolio, Sleeve
from tradewind.live.daily.__main__ import make_borrow
from tradewind.live.daily.borrow import (
    AutoBorrowSource,
    BorrowQuote,
    BorrowSnapshot,
    HubBorrowSource,
    IBShortableSource,
    borrow_report,
    borrow_source,
    markdown,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_live_session import EXIT_ALERT, EXIT_OK, Recorded, at, day

FILES = ["README.md", "data/usa/2026/2026-11-19.parquet", "data/usa/2026/2026-11-20.parquet",
         "data/canada/2026/2026-11-21.parquet", "raw/usa/2026/2026-11-20.txt.gz"]
ROWS = {
    "data/usa/2026/2026-11-20.parquet": [
        {"symbol": "SPY", "currency": "USD", "fee_rate": 0.2573, "rebate_rate": 3.6227, "available": 6_800_000,
         "available_capped": False},
        {"symbol": "SPY", "currency": "USD", "fee_rate": 0.30, "rebate_rate": 3.58, "available": 1_000,
         "available_capped": False},                              # a second line: the larger is kept
        {"symbol": "BRK B", "currency": "USD", "fee_rate": 0.25, "rebate_rate": 3.63, "available": 10_000_000,
         "available_capped": True},
        {"symbol": "GME", "currency": "USD", "fee_rate": 12.5, "rebate_rate": -8.62, "available": 50_000,
         "available_capped": False},
        {"symbol": "THIN", "currency": "USD", "fee_rate": 0.4, "rebate_rate": 3.48, "available": 300,
         "available_capped": False},
        {"symbol": "ZERO", "currency": "USD", "fee_rate": None, "rebate_rate": None, "available": 0,
         "available_capped": False},
        {"symbol": "ONDS", "currency": "CAD", "fee_rate": 0.25, "rebate_rate": 2.0, "available": 9_000,
         "available_capped": False},                              # another currency's line: ignored
    ],
}


def hub(**kw):
    reads = []

    def read(path):
        reads.append(path)
        return ROWS[path]

    src = HubBorrowSource(list_files=lambda: FILES, read=read, **kw)
    return src, reads


def test_the_hub_source_reads_the_latest_us_snapshot_once():
    src, reads = hub()
    snap = src.quotes(["SPY", "BRK.B", "ONDS", "GME"], "2026-11-20")
    assert snap.as_of == "2026-11-20" and snap.listed and not snap.notes
    assert set(snap.quotes) == {"SPY", "BRK.B", "GME"}                  # ONDS: not lendable in USD
    assert snap.quotes["SPY"].available == 6_800_000 and snap.quotes["SPY"].fee_rate == pytest.approx(0.2573)
    assert snap.quotes["BRK.B"].available_capped                         # IB writes "BRK B"
    src.quotes(["SPY"], "2026-11-20")
    assert reads == ["data/usa/2026/2026-11-20.parquet"]
    old = src.quotes(["SPY"], "2026-11-30")
    assert old.notes and "10 days before 2026-11-30" in old.notes[0]
    with pytest.raises(RuntimeError, match="no japan snapshot"):
        HubBorrowSource(country="japan", list_files=lambda: FILES, read=lambda p: []).quotes(["X"], "2026-11-20")


def test_the_cost_of_carry_and_the_flags():
    src, _ = hub()
    target = {"SPY": -100.0, "GME": -10.0, "ONDS": -50.0, "THIN": -1_000.0, "ZERO": -5.0, "AAPL": 20.0,
              "AUDUSD": -30_000.0}
    held = {"BRK.B": -40.0, "SPY": -80.0}
    closes = {"SPY": 700.0, "GME": 25.0, "ONDS": 8.0, "THIN": 10.0, "ZERO": 3.0, "BRK.B": 480.0, "AAPL": 230.0}
    shorts = ["BRK.B", "GME", "ONDS", "SPY", "THIN", "ZERO"]
    b = borrow_report(target, held, closes, src.quotes(shorts, "2026-11-20"), skip=lambda s: s == "AUDUSD")
    lines = {x["symbol"]: x for x in b["lines"]}
    assert sorted(lines) == shorts                                      # the long and the forex are not shorts here
    spy = lines["SPY"]
    assert spy["short_quantity"] == 100 and spy["short_value"] == pytest.approx(70_000.0)
    assert spy["fee_bps_year"] == pytest.approx(25.73)
    assert spy["daily_cost"] == pytest.approx(70_000.0 * 0.2573 / 100 / 365)
    assert spy["status"] == "ok"
    brk = lines["BRK.B"]                                               # held, closed at the open: one more night
    assert brk["target"] == 0 and brk["short_quantity"] == 40 and brk["status"] == "ok"
    assert lines["GME"]["status"] == "hard_to_borrow" and "12.50 %/year" in lines["GME"]["why"]
    assert lines["ONDS"]["status"] == "no_availability" and lines["ONDS"]["daily_cost"] is None
    assert lines["THIN"]["status"] == "insufficient" and "300 shares" in lines["THIN"]["why"]
    assert lines["ZERO"]["status"] == "no_availability"
    priced = [x["daily_cost"] for x in b["lines"] if x["daily_cost"] is not None]
    assert b["priced"] == 4 and b["daily_cost"] == pytest.approx(sum(priced))
    assert b["annual_cost"] == pytest.approx(365 * b["daily_cost"])
    md = "\n".join(markdown(b))
    assert "## Short borrow (cost of carry)" in md and "| SPY | -100 / -80 | 70,000 | 26 |" in md
    assert "> 10,000,000" in md
    strict = borrow_report(target, held, closes, src.quotes(shorts, "2026-11-20"), fee_alert=0.25)
    assert {x["symbol"] for x in strict["lines"] if x["status"] == "hard_to_borrow"} == {"SPY", "BRK.B", "GME"}


class _IBAccount:
    def __init__(self, levels):
        self.levels = levels
        self.asked = []

    def shortable(self, symbols, wait=4.0):
        self.asked.append(list(symbols))
        return {s: v for s, v in self.levels.items() if s in symbols}


class _Down:
    name = "hub"

    def quotes(self, symbols, day, account=None):
        raise ConnectionError("no network")


def test_ib_tick_is_the_fallback_when_the_hub_cannot_be_read():
    account = _IBAccount({"EASY": 3.0, "HARD": 2.0, "NONE": 1.0})
    snap = AutoBorrowSource([_Down(), IBShortableSource()]).quotes(["EASY", "HARD", "NONE", "MUTE"], "2026-11-20",
                                                                   account)
    assert account.asked == [["EASY", "HARD", "NONE", "MUTE"]]
    assert not snap.listed and "hub: ConnectionError: no network" in snap.notes[0]
    b = borrow_report({s: -10.0 for s in ("EASY", "HARD", "NONE", "MUTE")}, {}, {"EASY": 5.0}, snap)
    st = {x["symbol"]: x["status"] for x in b["lines"]}
    assert st == {"EASY": "ok", "HARD": "hard_to_borrow", "NONE": "no_availability", "MUTE": "unknown"}
    assert b["priced"] == 0 and b["daily_cost"] == 0.0                   # IB's tick has no fee
    assert "IB level 2" in "\n".join(markdown(b))
    with pytest.raises(RuntimeError, match="no borrow source answered"):
        AutoBorrowSource([_Down(), IBShortableSource()]).quotes(["X"], "2026-11-20", object())


def test_the_choice_of_source():
    assert borrow_source("none") is None
    assert isinstance(borrow_source("hub"), HubBorrowSource)
    assert isinstance(borrow_source("auto"), AutoBorrowSource)
    with pytest.raises(ValueError):
        borrow_source("ftp")
    meta = MetaPortfolio([Sleeve("a", 1.0)])
    assert make_borrow(meta, "sim") is None                             # the simulation reads nothing by default
    assert isinstance(make_borrow(meta, "ibkr"), AutoBorrowSource)
    assert isinstance(make_borrow(MetaPortfolio([Sleeve("a", 1.0)], borrow_source="IB"), "sim"), IBShortableSource)
    assert make_borrow(MetaPortfolio([Sleeve("a", 1.0)], borrow_source="none"), "ibkr") is None
    with pytest.raises(ValueError, match="borrow_source"):
        MetaPortfolio([Sleeve("a", 1.0)], borrow_source="ftp")


class _Fixed:
    """Every symbol lendable at one fee."""

    name = "fixed"

    def __init__(self, fee):
        self.fee = fee
        self.calls = 0

    def quotes(self, symbols, day, account=None):
        self.calls += 1
        return BorrowSnapshot("test", day, {s: BorrowQuote(s, fee_rate=self.fee, available=1e7, available_capped=True)
                                            for s in symbols}, listed=True)


def _read(path):
    with open(path) as fh:
        return fh.read()


def test_the_report_of_the_day_carries_the_borrow_section_and_alerts(tmp_path):
    rec = Recorded()
    s = rec.sessions(2)[0]
    cheap = _Fixed(0.25)
    ok = day(tmp_path, rec, at(s), borrow=cheap, name="cheap")
    assert ok.code == EXIT_OK and cheap.calls == 1
    report = json.loads(_read(ok.report_json))
    lines = report["borrow"]["lines"]
    assert lines and all(x["status"] == "ok" and x["daily_cost"] > 0 for x in lines)
    assert report["borrow"]["daily_cost"] == pytest.approx(sum(x["daily_cost"] for x in lines))
    md = _read(ok.report_md)
    assert "## Short borrow (cost of carry)" in md and "hard to borrow at a fee of 1 %/year" in md
    assert "## Short borrow (cost of carry)" in _read(report["proposal"])

    dear = day(tmp_path, rec, at(s), borrow=_Fixed(4.0), name="dear")
    assert dear.code == EXIT_ALERT
    alerts = [a for a in dear.report["alerts"] if a["code"] == "short_borrow"]
    assert len(alerts) == len(lines) and "hard to borrow" in alerts[0]["message"]
    assert any(w.startswith("borrow: ") for w in dear.report["warnings"])

    none = day(tmp_path, rec, at(s), borrow=AutoBorrowSource([_Down()]), name="none")
    assert none.code == EXIT_OK                                         # unknown is said, not alerted
    b = none.report["borrow"]
    assert {x["status"] for x in b["lines"]} == {"unknown"} and "no borrow source answered" in b["notes"][-1]

    off = day(tmp_path, rec, at(s), name="off")                         # no source: no section
    assert off.report["borrow"] == {} and "Short borrow" not in _read(off.report_md)
