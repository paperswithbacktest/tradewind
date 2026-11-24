"""Stale symbols in the daily run, without the network (tradewind.live.daily: fresh, runner,
session): a symbol still behind after the fresh-price top-up no longer stops the whole day.

What is checked: an empty AlphaVantage answer is asked again with a backoff; a share class
written with a dot is asked with a dash; the tolerance of the chain factor depends on the kind
(the minor currencies get more room) and can be set by symbol; out of strict mode the stale
symbols are listed with a cause (delisted or listed anew according to LISTING_STATUS, silent
source, sources that disagree, no source) and the strategy runs on; an index is continued with
its proxy ETF's returns on its own level (SPX on SPY, checked), VIX on VIXY only when allowed
(unchecked); the provider keeps the books and the list; the runner holds or closes the stale
symbols, or blocks, and keeps a sleeve with too much of its gross on them in error; the
session raises one ``stale_symbol`` alert, exit code 4, and lists them in the report.
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
    DailyRunner,
    MetaPortfolio,
    PwbProvider,
    ReplayProvider,
    SimAccount,
    Sleeve,
    StaleData,
)
from tradewind.live.daily.aggregate import plan_day
from tradewind.live.daily.books import SleeveBook
from tradewind.live.daily.fresh import (
    AlphaVantage,
    FetchError,
    FreshPrices,
    StalePrices,
    chain_proxy,
    parse_daily,
    tolerance_for,
)
from tradewind.live.daily.pwb_worker import fresh_options
from tradewind.live.daily.runner import stale_share
from tradewind.live.daily.session import EXIT_ALERT, EXIT_STALE, AlertConfig, DailySession

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_live_daily import UNTIL, _meta, _provider
from test_live_fresh import DAYS, KEY, SESSION, SPY, av_equity, price_path, pwb_rows

QUIET = AlertConfig(max_gap=1.0, max_gap_day=1.0, max_loss=1.0)

DELISTED_CSV = ("symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
                "GONE,Gone Inc,NYSE,Stock,1990-01-02,2026-11-04,Delisted\n"
                "NEWT,Old Newt Corp,NYSE,Stock,1995-01-03,2026-10-01,Delisted\n")
ACTIVE_CSV = ("symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
              "SPY,SPDR S&P 500,NYSE ARCA,ETF,1993-01-29,null,Active\n"
              "NEWT,New Newt Corp,NYSE,Stock,2026-11-11,null,Active\n"
              "MUTE,Mute Corp,NASDAQ,Stock,2001-05-01,null,Active\n")


class API:
    """``url -> text``: answers by symbol (a list is served in turn), LISTING_STATUS as CSV."""

    def __init__(self, answers: dict, listing: bool = True):
        self.answers = answers
        self.listing = listing
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        if q["function"] == "LISTING_STATUS":
            if not self.listing:
                return "{}"
            return DELISTED_CSV if q.get("state") == "delisted" else ACTIVE_CSV
        sym = q.get("symbol") or q["from_symbol"] + q["to_symbol"]
        ans = self.answers.get(sym, {"Error Message": "Invalid API call."})
        if isinstance(ans, list):
            ans = ans.pop(0) if len(ans) > 1 else ans[0]
        return json.dumps(ans)

    def asked(self, function: str) -> list[str]:
        return [u for u in self.urls if f"function={function}" in u]


def client(tmp_path, api, **kw) -> AlphaVantage:
    kw.setdefault("sleep", lambda s: None)
    return AlphaVantage(str(tmp_path / "prices"), SESSION, key=KEY, get=api, log=lambda *a: None, **kw)


def fresh(tmp_path, api, **kw) -> FreshPrices:
    return FreshPrices(client(tmp_path, api), SESSION, log=lambda *a: None, **kw)


def fx_answer(rates: dict[str, float]) -> dict:
    return {"Time Series FX (Daily)": {d: {"1. open": str(r), "2. high": str(r), "3. low": str(r), "4. close": str(r)}
                                       for d, r in rates.items()}}


def lasts(frame: pd.DataFrame) -> dict[str, str]:
    return frame.groupby("symbol")["date"].max().astype(str).to_dict()


# -- the client ----------------------------------------------------------------------------------------
def test_an_empty_answer_is_asked_again_with_a_backoff(tmp_path):
    api = API({"TLT": [{}, {}, av_equity(SPY)]})
    slept: list[float] = []
    c = client(tmp_path, api, sleep=slept.append)
    assert c.daily("equity", "TLT")["date"].iloc[-1] == SESSION
    assert [s for s in slept if s >= 5] == [5.0, 10.0] and len(api.urls) == 3

    never = API({"TLT": [{}]})
    c2 = AlphaVantage(str(tmp_path / "other"), SESSION, key=KEY, get=never, sleep=lambda s: None)
    with pytest.raises(FetchError, match="empty answer for TLT"):
        c2.daily("equity", "TLT")
    assert len(never.urls) == 4                                 # 1 + 3 retries
    assert not os.path.exists(tmp_path / "other" / SESSION / "TIME_SERIES_DAILY_ADJUSTED" / "TLT.json")


def test_a_dotted_share_class_is_asked_with_a_dash(tmp_path):
    api = API({"PBR-A": av_equity(SPY)})
    c = client(tmp_path, api)
    assert c.daily("equity", "PBR.A")["date"].iloc[-1] == SESSION
    assert "symbol=PBR-A" in api.urls[0] and len(api.urls) == 1
    assert os.path.exists(tmp_path / "prices" / SESSION / "TIME_SERIES_DAILY_ADJUSTED" / "PBR.A.json")
    api2 = API({"BF.B": av_equity(SPY)})                        # the dot, when the dash is unknown
    assert client(tmp_path / "x", api2).daily("equity", "BF.B") is not None and len(api2.urls) == 2


# -- the tolerance -------------------------------------------------------------------------------------
def test_the_tolerance_depends_on_the_kind_and_can_be_set_by_symbol():
    assert tolerance_for("equity", "AAPL") == 0.005 and tolerance_for("index", "SPX") == 0.01
    assert tolerance_for("fx", "EURUSD") == 0.005 and tolerance_for("fx", "JPYUSD") == 0.005
    assert tolerance_for("fx", "KRWUSD") == 0.015 and tolerance_for("fx", "USDKRW") == 0.015
    assert tolerance_for("equity", "CHGG", {"CHGG": 0.01}) == 0.01
    assert tolerance_for("fx", "KRWUSD", {"fx_minor": 0.02}) == 0.02


def drifting(rates: dict[str, float], until: str, by: float) -> dict[str, float]:
    """``rates`` with the days up to ``until`` moved by up to ``by`` (a slow drift: the two
    sources fix at different hours)."""
    days = [d for d in rates if d <= until][-5:]
    return {d: r * (1 + by * (days.index(d) / 4 if d in days else 0.0)) for d, r in rates.items()}


def test_a_minor_currency_gets_more_room_than_a_major_one(tmp_path):
    krw, eur = price_path(DAYS, 0.00072, 21), price_path(DAYS, 1.1, 22)
    api = API({"SPY": av_equity(SPY), "KRWUSD": fx_answer(drifting(krw, "2026-10-29", 0.01)),
               "EURUSD": fx_answer(drifting(eur, "2026-10-29", 0.01))})
    f = fresh(tmp_path, api, strict=False)
    frame = pd.concat([pwb_rows("KRWUSD", krw, "2026-10-30"), pwb_rows("EURUSD", eur, "2026-10-30")])
    out = f.extend("Forex-Daily-Price", frame)
    assert lasts(out) == {"KRWUSD": SESSION, "EURUSD": "2026-10-30"}
    assert f.stale_info["EURUSD"]["cause"] == "diverged" and "tolerance 0.50%" in f.stale["EURUSD"]
    f2 = fresh(tmp_path, api, strict=False, tolerance={"EURUSD": 0.02})
    assert lasts(f2.extend("Forex-Daily-Price", frame))["EURUSD"] == SESSION and not f2.stale


# -- stale symbols out of strict mode -----------------------------------------------------------------
def test_stale_symbols_are_listed_with_their_cause_and_the_strategy_runs_on(tmp_path):
    a = price_path(DAYS, 30.0, 6)
    gone = {d: c for d, c in a.items() if d <= "2026-11-04"}           # AlphaVantage stops too
    mute = {d: c for d, c in a.items() if d <= "2026-11-06"}
    newt = {d: c for d, c in a.items() if d >= "2026-11-11"}           # the ticker reused
    api = API({"SPY": av_equity(SPY), "AAA": av_equity(a), "GONE": av_equity(gone), "MUTE": av_equity(mute),
               "NEWT": av_equity(newt)})                                 # ZZZ: unknown
    f = fresh(tmp_path, api, strict=False)
    frame = pd.concat([pwb_rows(s, a, "2026-10-30") for s in ("AAA", "GONE", "MUTE", "NEWT", "ZZZ")])
    out = f.extend("Stocks-Daily-Price", frame)
    assert lasts(out) == {"AAA": SESSION, "GONE": "2026-11-04", "MUTE": "2026-11-06", "NEWT": "2026-10-30",
                          "ZZZ": "2026-10-30"}
    causes = {s: i["cause"] for s, i in f.stale_info.items()}
    assert causes == {"GONE": "delisted", "MUTE": "silent", "NEWT": "relisted", "ZZZ": "unknown"}
    assert "delisted on 2026-11-04" in f.stale["GONE"] and "listed anew on 2026-11-11" in f.stale["NEWT"]
    assert f.stale_info["GONE"]["last"] == "2026-11-04"
    assert len(api.asked("LISTING_STATUS")) == 2                         # read once for the session
    f.extend("Stocks-Daily-Price", frame)
    assert len(api.asked("LISTING_STATUS")) == 2
    s = f.summary()
    assert s["stale_info"]["NEWT"]["cause"] == "relisted" and s["filled"] >= 1


def test_strict_mode_still_stops_the_strategy(tmp_path):
    a = price_path(DAYS, 30.0, 6)
    f = fresh(tmp_path, API({"SPY": av_equity(SPY), "AAA": av_equity(a)}), strict=True)
    with pytest.raises(StalePrices, match="ZZZ"):
        f.extend("Stocks-Daily-Price", pd.concat([pwb_rows("AAA", a, "2026-10-30"), pwb_rows("ZZZ", a, "2026-10-30")]))


def test_listing_status_that_cannot_be_read_leaves_the_cause_as_it_was(tmp_path):
    a = price_path(DAYS, 30.0, 6)
    gone = {d: c for d, c in a.items() if d <= "2026-11-04"}
    f = fresh(tmp_path, API({"SPY": av_equity(SPY), "GONE": av_equity(gone)}, listing=False), strict=False)
    f.extend("Stocks-Daily-Price", pwb_rows("GONE", a, "2026-10-30"))
    assert f.stale_info["GONE"]["cause"] == "silent"


# -- indices through a proxy ETF ----------------------------------------------------------------------
def test_spx_is_continued_with_spys_returns_on_its_own_level(tmp_path):
    spx = {d: c * 11.0 for d, c in SPY.items()}                         # the index moves like SPY
    api = API({"SPY": av_equity(SPY)})
    f = fresh(tmp_path, api, strict=False, proxies={"SPX": "SPY"})
    rows = pwb_rows("SPX", spx, "2026-10-21", kind="")
    out = f.extend("Indices-Daily-Price", rows)
    s = out.set_index(out["date"].astype(str))["close"]
    assert s.index.max() == SESSION and not f.stale
    for d in ("2026-10-01", "2026-10-20"):                              # the history keeps its level
        assert s[d] == pytest.approx(spx[d], rel=1e-12)
    assert s[SESSION] == pytest.approx(spx[SESSION], rel=1e-6)          # SPY's returns, chained
    done = f.splices["SPX"]
    assert done.source == "SPY" and done.anchor == "2026-10-20" and not done.approximate
    assert f.summary()["proxied"] == {"SPX": {"proxy": "SPY", "anchor": "2026-10-20", "approximate": False}}
    assert out["volume"].dtype == rows["volume"].dtype


def test_an_index_that_moves_away_from_its_tracking_etf_is_refused(tmp_path):
    spx = {d: c * 11.0 * (1.03 if d < "2026-10-15" else 1.0) for d, c in SPY.items()}
    f = fresh(tmp_path, API({"SPY": av_equity(SPY)}), strict=False, proxies={"SPX": "SPY"})
    f.extend("Indices-Daily-Price", pwb_rows("SPX", spx, "2026-10-21", kind=""))
    assert f.stale_info["SPX"]["cause"] == "diverged" and "SPX and SPY disagree" in f.stale["SPX"]


def test_vix_on_vixy_is_chained_only_when_the_approximation_is_allowed(tmp_path):
    vix = price_path(DAYS, 18.0, 30)
    vixy = price_path(DAYS, 40.0, 31)                                   # its own path: not the VIX
    api = API({"SPY": av_equity(SPY), "VIXY": av_equity(vixy)})
    rows = pwb_rows("VIX", vix, "2026-10-21", kind="")
    f = fresh(tmp_path, api, strict=False, proxies={"VIX": "VIXY"})
    out = f.extend("Indices-Daily-Price", rows)
    assert lasts(out)["VIX"] == "2026-10-21" and f.stale_info["VIX"]["cause"] == "no_source"
    assert "fresh_index_proxy_approximate" in f.stale["VIX"] and "VIXY" not in "".join(api.urls)
    g = fresh(tmp_path, api, strict=False, proxies={"VIX": "VIXY"}, approximate={"VIX"})
    s = g.extend("Indices-Daily-Price", rows).set_index("date")["close"]
    assert not g.stale and g.splices["VIX"].approximate
    anchor = _dt.date(2026, 10, 20)
    assert s[anchor] == pytest.approx(vix["2026-10-20"])                # the VIX's level at the anchor
    assert s[_dt.date(2026, 11, 18)] / s[anchor] == pytest.approx(vixy[SESSION] / vixy["2026-10-20"], rel=1e-6)
    h = fresh(tmp_path, api, strict=False)                              # no proxy: no source
    h.extend("Indices-Daily-Price", rows)
    assert h.stale_info["VIX"]["reason"] == "no fresh source for this dataset"


def test_chain_proxy_with_an_adjusted_close_column():
    spx = {d: c * 11.0 for d, c in SPY.items()}
    rows = pwb_rows("SPX", spx, "2026-10-21", kind="").assign(adj_close=lambda f: f["close"])
    out, done = chain_proxy(rows, parse_daily("equity", av_equity(SPY)), SESSION, proxy_symbol="SPY")
    assert done.status == "filled" and out["adj_close"].iloc[-1] == pytest.approx(out["close"].iloc[-1])


# -- the meta-portfolio and the worker ----------------------------------------------------------------
def test_the_meta_portfolio_options():
    m = MetaPortfolio([Sleeve("a", 1.0)], substitutes={"VIX": "VIXY"})
    assert m.stale_symbol_policy == "hold" and m.stale_symbol_max_weight == 0.2
    assert m.fresh_config() == {"strict": False, "tolerance": {}, "proxies": {"SPX": "SPY", "VIX": "VIXY"},
                                "approximate": [], "sources": ["alphavantage"], "grace": {}, "public": True}
    m2 = MetaPortfolio([Sleeve("a", 1.0)], stale_symbol_policy="BLOCK", fresh_price_tolerance=0.01,
                       fresh_index_proxies={}, fresh_index_proxy_approximate=["VIX"], fresh_public_series=False)
    conf = m2.fresh_config()
    assert conf["strict"] and conf["proxies"] == {} and conf["approximate"] == ["VIX"]
    assert conf["tolerance"]["fx_minor"] == 0.01 and conf["tolerance"]["equity"] == 0.01
    for bad in ({"stale_symbol_policy": "sell"}, {"stale_symbol_max_weight": 1.5},
                {"fresh_price_tolerance": {"equity": 0.9}}):
        with pytest.raises(ValueError):
            MetaPortfolio([Sleeve("a", 1.0)], **bad)
    assert fresh_options(json.dumps(conf)) == {"strict": True, "tolerance": conf["tolerance"], "proxies": {},
                                               "approximate": ["VIX"], "sources": ("alphavantage",), "grace": {},
                                               "public": False}
    assert fresh_options(None)["strict"] is True and fresh_options(None)["public"] is True


def test_the_provider_keeps_books_that_come_with_stale_symbols(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", KEY)
    meta = MetaPortfolio([Sleeve("s1", 1.0, path=str(tmp_path / "strategy.py"))], fresh_prices="alphavantage")
    (tmp_path / "strategy.py").write_text("")
    book = SleeveBook("2026-11-18", 100.0, positions={"AAA": 1.0}, target={"AAA": 1.0}, close={"AAA": 10.0})
    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        with open(cmd[cmd.index("-m") + 3], "w") as fh:
            json.dump({"books": [book.to_dict()], "stale": {"KVUE": "Stocks-Daily-Price: last bar 2026-10-30, ..."},
                       "stale_info": {"KVUE": {"dataset": "Stocks-Daily-Price", "last": "2026-10-30",
                                               "cause": "delisted", "reason": "delisted on 2026-10-30"}},
                       "fresh": {"filled": 1, "last": "2026-11-18", "requests": 2, "max_spread": 0.0}}, fh)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tradewind.live.daily.providers.subprocess.run", fake_run)
    provider = PwbProvider(meta, str(tmp_path / "run" / "books"), log=lambda *a: None)
    out = provider.books("2026-11-18", 2)
    assert list(out["s1"]) == ["2026-11-18"] and not provider.errors
    assert provider.stale == {"s1": {"KVUE": "Stocks-Daily-Price: last bar 2026-10-30, ..."}}
    assert provider.stale_info["s1"]["KVUE"]["cause"] == "delisted"
    conf = json.loads(seen["cmd"][seen["cmd"].index("--fresh-config") + 1])
    assert conf["strict"] is False and conf["proxies"] == {"SPX": "SPY"}
    again = PwbProvider(meta, str(tmp_path / "run" / "books"), log=lambda *a: None)   # cached, still listed
    again.books("2026-11-18", 2)
    assert again.stale_info["s1"]["KVUE"]["last"] == "2026-10-30"


# -- the runner ---------------------------------------------------------------------------------------
class WithStale:
    """A replay whose provider says ``stale`` ({slug: {symbol: why}}) for every close."""

    def __init__(self, replay, stale: dict, info: dict | None = None):
        self.replay = replay
        self.stale = stale
        self.stale_info = info or {}
        self.errors: dict = {}

    def books(self, as_of=None, last_n=2):
        return self.replay.books(as_of, last_n)


def two_days(tmp_path, meta, stale_of, name="run"):
    """Day 1 on a clean replay (orders sent), day 2 with the stale symbols ``stale_of(book)``
    (a function of momentum's book on day 2) for the momentum sleeve."""
    replay = ReplayProvider(_provider(), UNTIL, 4)
    d1, d2 = replay.dates()[-2:]
    directory = str(tmp_path / name)
    account = SimAccount(os.path.join(directory, "sim.json"), cash=110_000.0)
    DailyRunner(meta, replay, account, directory, log=lambda *a: None).run_day(d1, send=True)
    book = replay.books(d2, 1)["momentum"][d2]
    syms = stale_of(book)
    info = {"momentum": {s: {"dataset": "Stocks-Daily-Price", "last": d1, "cause": "delisted",
                             "reason": f"delisted on {d1}"} for s in syms}}
    provider = WithStale(replay, {"momentum": {s: "stale" for s in syms}}, info)
    runner = DailyRunner(meta, provider, account, directory, log=lambda *a: None)
    return runner, provider, d2, syms


def held_by_momentum(book):
    return [min(book.positions)]


def test_hold_keeps_the_position_and_sends_no_order_on_it(tmp_path):
    meta = _meta(stale_symbol_max_weight=0.9)
    runner, _, d2, (sym,) = two_days(tmp_path, meta, held_by_momentum)
    report = runner.run_day(d2, send=False)
    held = runner.account.snapshot({}).positions
    assert held.get(sym) and report.plan.target[sym] == held[sym]
    assert sym not in {o.symbol for o in report.plan.orders} and report.plan.frozen == {sym: "hold"}
    (row,) = report.stale
    assert row["symbol"] == sym and row["cause"] == "delisted" and row["policy"] == "hold"
    assert "no order" in row["action"] and 0 < row["shares"]["momentum"] <= 0.9
    assert any(w.startswith(f"stale symbol: {sym}: last bar") for w in report.warnings)
    assert not any(w.startswith(f"watch: {sym}") for w in report.warnings)


def test_close_sells_the_position_at_the_open(tmp_path):
    meta = _meta(stale_symbol_max_weight=0.9, stale_symbol_policy="close")
    runner, _, d2, (sym,) = two_days(tmp_path, meta, held_by_momentum)
    report = runner.run_day(d2, send=False)
    held = runner.account.snapshot({}).positions
    (order,) = [o for o in report.plan.orders if o.symbol == sym]
    assert order.quantity == -held[sym] and order.target == 0 and sym not in report.plan.target
    assert report.stale[0]["action"].startswith("closed")


def test_block_proposes_nothing(tmp_path):
    runner, _, d2, _ = two_days(tmp_path, _meta(stale_symbol_policy="block"), held_by_momentum)
    with pytest.raises(StaleData, match="stale_symbol_policy is block"):
        runner.run_day(d2, send=False)


def test_a_sleeve_with_too_much_of_its_gross_stale_stays_in_error(tmp_path):
    runner, provider, d2, (sym,) = two_days(tmp_path, _meta(sleeve_error_policy="block"),
                                            held_by_momentum)                           # max weight 0.2
    with pytest.raises(StaleData, match="above stale_symbol_max_weight 20%"):
        runner.run_day(d2, send=False)
    assert "momentum" in provider.errors and sym in provider.errors["momentum"]


def test_stale_share_counts_the_larger_of_position_and_target():
    b = SleeveBook("2026-11-18", 100.0, positions={"A": 10.0, "B": 5.0}, target={"A": 10.0, "C": -20.0},
                   close={"A": 10.0, "B": 20.0, "C": 5.0})
    assert stale_share(b, {"B"}) == pytest.approx(100.0 / 300.0)
    assert stale_share(b, {"Z"}) == 0.0 and stale_share(SleeveBook("d", 1.0), {"A"}) == 0.0


def test_a_stale_index_is_frozen_under_its_substitute():
    meta = MetaPortfolio([Sleeve("v", 1.0)], substitutes={"VIX": "VIXY"}, stale_symbol_max_weight=0.5)
    book = SleeveBook("2026-11-18", 100.0, positions={"SPY": 9.0, "VIX": 0.1}, target={"SPY": 9.0, "VIX": 0.1},
                      close={"SPY": 10.0, "VIX": 20.0})
    provider = types.SimpleNamespace(stale={"v": {"VIX": "no fresh source"}}, stale_info={})
    runner = DailyRunner.__new__(DailyRunner)
    runner.meta, runner.provider = meta, provider
    out = runner._stale_symbols({"v": {"2026-11-18": book}}, "2026-11-18")
    assert out["VIX"]["plan_symbol"] == "VIXY" and out["VIX"]["shares"]["v"] == pytest.approx(2 / 92)
    plan = plan_day(meta, {"v": SleeveBook("2026-11-18", 100.0, target={"SPY": 9.0, "VIXY": 3.0},
                                           close={"SPY": 10.0, "VIXY": 30.0})},
                    {"VIXY": 1.0}, 100.0, "2026-11-18", frozen={"VIXY": "hold"})
    assert plan.target["VIXY"] == 1.0 and [o.symbol for o in plan.orders] == ["SPY"]
    with pytest.raises(ValueError):
        plan_day(meta, {}, {}, 100.0, "2026-11-18", frozen={"VIXY": "sell"})


def test_a_symbol_never_traded_is_not_counted_in_the_stale_share():
    """BRLUSD, CLPUSD, ISKUSD have no IDEALPRO pair: stale or not, no order is ever proposed on
    them, so they do not put their sleeve in error; a stale symbol the account trades still does."""
    meta = MetaPortfolio([Sleeve("fx", 0.5), Sleeve("other", 0.5)], stale_symbol_max_weight=0.2)
    book = SleeveBook("2026-11-18", 100.0,
                      positions={"BRLUSD": 120.0, "CLPUSD": 9000.0, "ISKUSD": 1000.0, "EURUSD": 30.0, "AUDUSD": 40.0},
                      target={"BRLUSD": 120.0, "CLPUSD": 9000.0, "ISKUSD": 1000.0, "EURUSD": 30.0, "AUDUSD": 40.0},
                      close={"BRLUSD": 0.18, "CLPUSD": 0.0011, "ISKUSD": 0.0075, "EURUSD": 1.09, "AUDUSD": 0.65})
    stale = {"BRLUSD": "silent", "CLPUSD": "silent", "ISKUSD": "silent"}
    provider = types.SimpleNamespace(stale={"fx": dict(stale)}, stale_info={}, errors={})
    runner = DailyRunner.__new__(DailyRunner)
    runner.meta, runner.provider = meta, provider
    assert stale_share(book, set(stale)) > 0.2                          # the share the run used to see
    skipped: dict[str, str] = {}
    out = runner._stale_symbols({"fx": {"2026-11-18": book}}, "2026-11-18", skipped)
    assert not skipped and "fx" not in provider.errors                 # the sleeve is proposed
    assert set(out) == set(stale) and all(not d["traded"] for d in out.values())
    assert out["BRLUSD"]["refusal"].startswith("no spot pair on IDEALPRO") and out["BRLUSD"]["shares"]["fx"] == 0.0
    plan = plan_day(meta, {"fx": book}, {}, 100.0, "2026-11-18",
                    frozen={d["plan_symbol"]: "hold" for d in out.values()})
    assert not {o.symbol for o in plan.orders} & set(stale) and set(stale) <= set(plan.refused)
    rows, warnings = runner._stale_report(out, plan, {})
    assert all("never traded" in r["action"] for r in rows) and len(warnings) == 3

    provider2 = types.SimpleNamespace(stale={"fx": dict(stale, EURUSD="silent")}, stale_info={}, errors={})
    runner.provider = provider2
    skipped = {}
    out = runner._stale_symbols({"fx": {"2026-11-18": book}}, "2026-11-18", skipped)
    eur = 30.0 * 1.09 / (120 * 0.18 + 9000 * 0.0011 + 1000 * 0.0075 + 30 * 1.09 + 40 * 0.65)
    assert out == {} and "fx" in skipped                                # EURUSD alone is over 20 %
    assert f"{eur:.1%} of its gross on stale symbols (EURUSD)" in provider2.errors["fx"]


# -- the session --------------------------------------------------------------------------------------
def session(tmp_path, provider, meta, when, name="paper"):
    directory = str(tmp_path / name)
    account = (lambda send: SimAccount(os.path.join(directory, "sim_account.json"), cash=110_000.0))
    return DailySession(meta, directory, provider_factory=lambda: provider, account_factory=account,
                        session=when, now=_dt.datetime.combine(_dt.date.fromisoformat(when), _dt.time(23, 0),
                                                               tzinfo=_dt.timezone.utc),
                        alerts=QUIET, log=lambda *a: None).run()


def test_the_session_proposes_with_a_stale_symbol_alert(tmp_path):
    from tradewind.calendars import NYSE

    replay = ReplayProvider(_provider(), UNTIL, 4)
    d = [x for x in replay.dates() if NYSE.is_trading_day(x)][-1]
    book = replay.books(d, 1)["momentum"][d]
    sym = min(book.target)
    info = {"momentum": {sym: {"dataset": "Stocks-Daily-Price", "last": "2026-10-30", "cause": "delisted",
                               "reason": "delisted on 2026-10-30 (AlphaVantage LISTING_STATUS)"}}}
    out = session(tmp_path, WithStale(replay, {"momentum": {sym: "stale"}}, info), _meta(stale_symbol_max_weight=0.9), d)
    assert out.code == EXIT_ALERT and out.status == "alert"
    alerts = {a["code"]: a["message"] for a in out.report["alerts"]}
    assert f"{sym}: last bar 2026-10-30, delisted" in alerts["stale_symbol"]
    assert out.report["orders"]["proposed"] and out.report["stale_symbols"] == [sym]
    assert out.report["stale_detail"][0]["policy"] == "hold"
    with open(out.report_md) as fh:
        md = fh.read()
    assert "## Stale symbols" in md and "| symbol | last bar | cause |" in md and "`momentum`" in md

    over = session(tmp_path, WithStale(replay, {"momentum": {sym: "stale"}}, info),
                   _meta(sleeve_error_policy="block"), d, name="over")
    assert over.code == EXIT_STALE and "momentum" in over.report["sleeves_in_error"]
    assert not over.report["orders"]["proposed"] and over.report["stale_symbols"] == [sym]
