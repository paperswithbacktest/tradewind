"""Names a sleeve holds outside its point-in-time universe, in the daily run, without the network.

A backtest with a point-in-time universe (pwb-toolbox, ``pit_exit="hold"``) keeps a name that
left the universe until the next rebalance. The live account must not *open* such a line: under
``out_of_universe_policy: no_new`` (the default) the outside sleeves' part of a symbol keeps,
reduces or closes what the account holds, never adds to it nor turns it around; ``follow``
trades the books as they are.

What is checked: the book records the names the harness says are outside (``OUT_HOOK``); a
first buy is refused; a reduction and a sale are followed; a long is never turned short; a
book wanting more than is held gets what is held; a sleeve holding the name *inside* its
universe still trades it; ``follow`` is the behaviour before; the planned exact book is the
capped one; the runner warns, the session raises ``out_of_universe`` and lists the names; a
cached book written before ``outside`` existed is recomputed.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import types

import pandas as pd
import pytest

import tradewind as tw
from tradewind.live.daily import (
    DailyRunner,
    MetaPortfolio,
    PwbProvider,
    ReplayProvider,
    SimAccount,
    Sleeve,
)
from tradewind.live.daily.aggregate import cap_outside, plan_day
from tradewind.live.daily.books import SleeveBook, record_books
from tradewind.live.daily.session import EXIT_ALERT, AlertConfig, DailySession

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_live_daily import FRAMES, UNTIL, _provider

DAY = "2026-11-19"
QUIET = AlertConfig(max_gap=1.0, max_gap_day=1.0, max_loss=1.0)


def _meta1(**kw):
    opts = {"leverage": 1.0, "max_gross": 4.0, "lot": 1.0, "min_order_notional": 0.0}
    opts.update(kw)
    return MetaPortfolio([Sleeve("pe", 1.0)], **opts)


def _book(target, positions=None, outside=("ET",), nav=1000.0):
    close = {"ET": 10.0, "SPY": 100.0}
    return SleeveBook(DAY, nav, positions=dict(target if positions is None else positions), target=dict(target),
                      open=dict(close), close=dict(close), outside=list(outside))


# -- the plan ------------------------------------------------------------------------------------------
def test_a_first_buy_of_a_name_outside_the_universe_is_refused():
    # units 1 x 1000 / 1000 = 1: the book's shares are the account's
    plan = plan_day(_meta1(), {"pe": _book({"ET": 30.0, "SPY": 5.0})}, {}, 1000.0, DAY)
    assert "ET" not in plan.target and [o.symbol for o in plan.orders] == ["SPY"]
    assert "ET" not in plan.exact                    # the planned book is the capped one
    (row,) = plan.out_of_universe
    assert row["symbol"] == "ET" and row["sleeves"] == {"pe": 30.0} and row["held"] == 0.0
    assert row["followed"] == 30.0 and row["target"] == 0.0 and row["capped"] and row["policy"] == "no_new"


def test_follow_trades_the_book_as_it_is():
    plan = plan_day(_meta1(out_of_universe_policy="follow"), {"pe": _book({"ET": 30.0, "SPY": 5.0})}, {},
                    1000.0, DAY)
    assert plan.target["ET"] == 30.0 and {o.symbol: o.quantity for o in plan.orders}["ET"] == 30.0
    assert plan.exact["ET"] == 30.0 and not plan.out_of_universe[0]["capped"]


@pytest.mark.parametrize(("wanted", "held", "target"), [
    (6.0, 10.0, 6.0),        # a reduction is followed
    (0.0, 10.0, 0.0),        # a sale is followed
    (10.0, 10.0, 10.0),      # kept: no order
    (25.0, 10.0, 10.0),      # more than held: what is held
    (-5.0, 10.0, 0.0),       # a long is closed, never turned short
    (5.0, -8.0, 0.0),        # a short is covered, never turned long
    (-12.0, -8.0, -8.0),     # a larger short: what is held
    (-3.0, -8.0, -3.0),      # a smaller short is followed
])
def test_the_account_line_is_kept_reduced_or_closed_never_added_to(wanted, held, target):
    book = _book({"ET": wanted} if wanted else {}, positions={"ET": 10.0})
    plan = plan_day(_meta1(), {"pe": book}, {"ET": held}, 1000.0, DAY)
    assert plan.target.get("ET", 0.0) == target
    assert abs(plan.target.get("ET", 0.0)) <= abs(held)
    assert plan.target.get("ET", 0.0) * held >= 0
    orders = {o.symbol: o.quantity for o in plan.orders}
    assert orders.get("ET", 0.0) == pytest.approx(target - held)


def test_rounding_to_a_lot_never_goes_above_the_held_line():
    plan = plan_day(_meta1(lot=10.0), {"pe": _book({"ET": 15.0})}, {"ET": 15.0}, 1000.0, DAY)
    assert plan.target["ET"] == 15.0 and not plan.orders


def test_a_sleeve_holding_the_name_inside_its_universe_still_trades_it():
    meta = MetaPortfolio([Sleeve("pe", 0.5), Sleeve("value", 0.5)], lot=0.0)
    books = {"pe": _book({"ET": 40.0}, nav=1000.0),                          # outside: 0.5 x 40 = 20
             "value": _book({"ET": 16.0}, outside=(), nav=1000.0)}           # inside: 0.5 x 16 = 8
    plan = plan_day(meta, books, {}, 1000.0, DAY)
    assert plan.target["ET"] == pytest.approx(8.0)                           # the members' part only
    (row,) = plan.out_of_universe
    assert row["sleeves"] == {"pe": 20.0} and row["inside"] == pytest.approx(8.0)
    held = plan_day(meta, books, {"ET": 15.0}, 1000.0, DAY)                  # 7 held beyond the members' part
    assert held.target["ET"] == pytest.approx(15.0)
    more = plan_day(meta, books, {"ET": 40.0}, 1000.0, DAY)                  # beyond both: down to the book
    assert more.target["ET"] == pytest.approx(28.0)


def test_cap_outside():
    assert cap_outside(0.0, 30.0, 0.0) == 0.0
    assert cap_outside(0.0, 30.0, 12.0) == 12.0 and cap_outside(0.0, 6.0, 12.0) == 6.0
    assert cap_outside(0.0, -4.0, 12.0) == 0.0 and cap_outside(5.0, 0.0, 99.0) == 5.0
    assert cap_outside(8.0, 20.0, 15.0) == 15.0 and cap_outside(8.0, 20.0, 3.0) == 8.0


def test_the_meta_portfolio_option():
    assert _meta1().out_of_universe_policy == "no_new"
    assert _meta1(out_of_universe_policy="FOLLOW").out_of_universe_policy == "follow"
    with pytest.raises(ValueError, match="out_of_universe_policy"):
        _meta1(out_of_universe_policy="hold")


def test_a_stale_symbol_policy_applies_after():
    plan = plan_day(_meta1(), {"pe": _book({"ET": 30.0})}, {"ET": 4.0}, 1000.0, DAY, frozen={"ET": "close"})
    assert "ET" not in plan.target and plan.orders[0].quantity == -4.0


# -- the book -------------------------------------------------------------------------------------------
class _Holder(tw.Strategy):
    """Buys AAA and BBB on the first bar and holds them; the harness says AAA is outside its
    universe after ``CUT`` (``_pwb_is_out``, as pwb-toolbox's ``pit_exit="hold"``)."""

    CUT = pd.Timestamp(0)
    fail = False

    def next(self):
        if len(self) == 1:
            self.order_target_size(self.datas[0], 10)
            self.order_target_size(self.datas[1], 5)

    def _pwb_is_out(self, data):
        if self.fail:
            raise KeyError(data._name)
        return data._name == "AAA" and pd.Timestamp(data.datetime.date(0)) > self.CUT


@pytest.mark.parametrize("fail", [False, True])
def test_the_book_records_the_names_the_harness_says_are_outside(fail):
    cut = FRAMES["AAA"].index[-10]
    strategy = type("Holder", (_Holder,), {"CUT": cut, "fail": fail})
    cerebro = tw.Cerebro()
    for name in ("AAA", "BBB", "CCC"):
        cerebro.adddata(tw.feeds.PandasData(dataname=FRAMES[name]), name=name)
    cerebro.addstrategy(strategy)
    cerebro.broker.setcash(100_000.0)
    with record_books(since=str(FRAMES["AAA"].index[-20].date()), last_n=30) as rec:
        cerebro.run()
    books = rec.books
    for day, book in books.items():
        assert book.positions == {"AAA": 10.0, "BBB": 5.0}
        expected = ["AAA"] if not fail and pd.Timestamp(day) > cut else []
        assert book.outside == expected, day
        assert SleeveBook.from_dict(json.loads(json.dumps(book.to_dict()))).outside == expected


def test_a_cached_book_without_outside_is_computed_again(tmp_path, monkeypatch):
    meta = MetaPortfolio([Sleeve("s1", 1.0, path=str(tmp_path / "strategy.py"))])
    (tmp_path / "strategy.py").write_text("")
    book = SleeveBook(DAY, 100.0, positions={"ET": 1.0}, target={"ET": 1.0}, close={"ET": 10.0}, outside=["ET"])
    runs: list = []

    def fake_run(cmd, **kw):
        runs.append(cmd)
        with open(cmd[cmd.index("-m") + 3], "w") as fh:
            json.dump({"books": [book.to_dict()]}, fh)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tradewind.live.daily.providers.subprocess.run", fake_run)
    folder = tmp_path / "books" / DAY
    folder.mkdir(parents=True)
    old = book.to_dict()
    del old["outside"]
    (folder / "s1.json").write_text(json.dumps({"books": [old]}))
    provider = PwbProvider(meta, str(tmp_path / "books"), log=lambda *a: None)
    assert provider.books(DAY, 2)["s1"][DAY].outside == ["ET"] and len(runs) == 1
    PwbProvider(meta, str(tmp_path / "books"), log=lambda *a: None).books(DAY, 2)
    assert len(runs) == 1                            # the new cache is kept


# -- the runner and the session --------------------------------------------------------------------------
class WithOutside:
    """A replay whose momentum books say ``symbol`` is outside the universe."""

    def __init__(self, replay, slug: str, symbol: str):
        self.replay, self.slug, self.symbol = replay, slug, symbol
        self.errors: dict = {}

    def books(self, as_of=None, last_n=2):
        out = self.replay.books(as_of, last_n)
        for d, b in list(out[self.slug].items()):
            nb = SleeveBook.from_dict(b.to_dict())
            nb.outside = [self.symbol]
            out[self.slug][d] = nb
        return out


def _mom(**kw):
    """The momentum sleeve alone: the name it holds outside is held by no other sleeve."""
    opts = {"leverage": 1.0, "max_gross": 4.0, "lot": 0.0, "min_order_notional": 0.0}
    opts.update(kw)
    return MetaPortfolio([Sleeve("momentum", 1.0)], **opts)


def _outside_symbol(replay, day):
    book = replay.books(day, 1)["momentum"][day]
    return min(s for s, q in book.target.items() if q > 0)


def test_the_runner_does_not_buy_and_warns(tmp_path):
    replay = ReplayProvider(_provider(), UNTIL, 3)
    day = replay.dates()[-1]
    sym = _outside_symbol(replay, day)
    directory = str(tmp_path / "run")
    account = SimAccount(os.path.join(directory, "sim.json"), cash=110_000.0)
    report = DailyRunner(_mom(), WithOutside(replay, "momentum", sym), account, directory,
                         log=lambda *a: None).run_day(day, send=False)
    assert sym not in report.plan.target and sym not in {o.symbol for o in report.plan.orders}
    (row,) = report.out_of_universe
    assert row["symbol"] == sym and row["capped"] and row["target"] == 0.0 and row["held"] == 0.0
    assert any(w.startswith(f"out of universe: {sym}: outside the universe of momentum") for w in report.warnings)

    follow = DailyRunner(_mom(out_of_universe_policy="follow"), WithOutside(replay, "momentum", sym),
                         SimAccount(os.path.join(str(tmp_path / "f"), "sim.json"), cash=110_000.0),
                         str(tmp_path / "f"), log=lambda *a: None).run_day(day, send=False)
    plain = DailyRunner(_mom(), replay, SimAccount(os.path.join(str(tmp_path / "p"), "sim.json"), cash=110_000.0),
                        str(tmp_path / "p"), log=lambda *a: None).run_day(day, send=False)
    assert follow.plan.target == plain.plan.target and sym in follow.plan.target
    assert [(o.symbol, o.quantity) for o in follow.plan.orders] == [(o.symbol, o.quantity) for o in plain.plan.orders]


def test_the_session_raises_out_of_universe(tmp_path):
    from tradewind.calendars import NYSE

    replay = ReplayProvider(_provider(), UNTIL, 4)
    day = [x for x in replay.dates() if NYSE.is_trading_day(x)][-1]
    sym = _outside_symbol(replay, day)
    directory = str(tmp_path / "paper")
    out = DailySession(_mom(), directory, provider_factory=lambda: WithOutside(replay, "momentum", sym),
                       account_factory=lambda send: SimAccount(os.path.join(directory, "sim.json"), cash=110_000.0),
                       session=day, now=_dt.datetime.combine(_dt.date.fromisoformat(day), _dt.time(23, 0),
                                                             tzinfo=_dt.timezone.utc),
                       alerts=QUIET, log=lambda *a: None).run()
    assert out.code == EXIT_ALERT
    alerts = {a["code"]: a["message"] for a in out.report["alerts"]}
    assert f"{sym}: legs momentum" in alerts["out_of_universe"] and "target +0" in alerts["out_of_universe"]
    assert sym not in {o["symbol"] for o in out.report["orders"]["proposed"]}
    assert out.report["out_of_universe"][0]["symbol"] == sym
    with open(out.report_md) as fh:
        assert "## Held outside a point-in-time universe" in fh.read()
