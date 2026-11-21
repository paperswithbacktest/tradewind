"""Fresh prices from Interactive Brokers first, AlphaVantage after (tradewind.live.daily:
ib_prices, fresh, providers), without the network: a stub of ib_async's interface answers as
the paper account did on 2026-11-21.

What is checked: one read-only connection on the data client id, refused on a live port, on a
non-paper account and on the account's client id; stocks asked as ``ADJUSTED_LAST`` (a share
class with a space), indices as ``Index(.., CBOE)``, forex as IDEALPRO ``MIDPOINT`` turned into
the book's direction, BRL and the other non-deliverable currencies never asked; error 162
(no permission), 200, 354 written as a fallback, a pacing violation and an unanswered request
retried with a backoff; the conIds kept and reused, a stale one qualified again; the day's bar
kept only after the close plus the grace, for IB's bars and AlphaVantage's alike; the workers'
loads served by IB first, by AlphaVantage for the rest (an error file, a missing file, a bar
not final), an index by its IB levels with no proxy, the two sources compared when both are at
hand; the provider collects once before the strategies and reports who served what.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import sys
import types

import pandas as pd
import pytest

from tradewind.live.adapter import AdapterError
from tradewind.live.daily import MetaPortfolio, PwbProvider, Sleeve
from tradewind.live.daily.books import SleeveBook
from tradewind.live.daily.fresh import AlphaVantage, FetchError, FreshPrices
from tradewind.live.daily.ib_prices import (
    IBDailyPrices,
    IBDataConfig,
    IBPriceCache,
    bar_complete_at,
    drop_unfinished,
    ib_kind,
    ib_local,
    needed_session,
    universe,
)
from tradewind.live.daily.providers import parse_sources

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_live_fresh import DAYS, KEY, SPY, FakeAPI, av_equity, price_path, pwb_rows, sessions

SESSION = "2026-11-18"                          # a Wednesday
AFTER = _dt.datetime(2026, 11, 18, 22, 40, tzinfo=_dt.timezone.utc).timestamp()   # 17:40 New York
DURING = _dt.datetime(2026, 11, 18, 21, 5, tzinfo=_dt.timezone.utc).timestamp()   # 16:05: not final


# -- a stub of ib_async ----------------------------------------------------------------------------------
class Event:
    def __init__(self):
        self.handlers = []

    def connect(self, handler):
        self.handlers.append(handler)

    def emit(self, *args):
        for h in list(self.handlers):
            h(*args)


class C:
    """A contract: whatever fields it was built with, conId 0 until qualified."""

    def __init__(self, **kw):
        self.conId = 0
        self.symbol = ""
        self.__dict__.update(kw)


LIB = types.SimpleNamespace(
    Stock=lambda s, e, c: C(secType="STK", symbol=s, exchange=e, currency=c),
    Forex=lambda pair, exchange: C(secType="CASH", symbol=pair[:3], currency=pair[3:], pair=pair, exchange=exchange),
    Index=lambda s, e, c: C(secType="IND", symbol=s, exchange=e, currency=c),
    Contract=lambda conId, exchange: C(conId=conId, exchange=exchange),
)


def bars(closes: dict[str, float]):
    return [types.SimpleNamespace(date=_dt.date.fromisoformat(d), open=c * 0.99, high=c * 1.01, low=c * 0.98,
                                  close=c, volume=1000.0) for d, c in sorted(closes.items())]


class StubIB:
    """``known``: {(secType, symbol or pair): conId}; ``answers``: {conId: a list of answers
    served in turn, each a bar list, ("error", code, message) or "hang"}."""

    def __init__(self, known, answers, accounts=("DU1234503",)):
        for name in ("orderStatusEvent", "execDetailsEvent", "commissionReportEvent", "errorEvent",
                     "disconnectedEvent"):
            setattr(self, name, Event())
        self.known, self.answers, self.accounts = dict(known), {k: list(v) for k, v in answers.items()}, list(accounts)
        self.connected = None
        self.qualified: list[str] = []
        self.asked: list[tuple] = []

    def connect(self, host, port, clientId, readonly=False, account=""):
        self.connected = {"host": host, "port": port, "clientId": clientId, "readonly": readonly}

    def disconnect(self):
        self.connected = None

    def isConnected(self):
        return self.connected is not None

    def managedAccounts(self):
        return self.accounts

    def run(self, coro):
        return asyncio.run(coro)

    async def qualifyContractsAsync(self, *contracts):
        for c in contracts:
            key = (c.secType, getattr(c, "pair", c.symbol))
            self.qualified.append(key[1])
            if key in self.known:
                c.conId = self.known[key]
            else:
                self.errorEvent.emit(1, 200, "No security definition has been found for the request", c)
        return [c for c in contracts if c.conId]

    async def reqHistoricalDataAsync(self, contract, end, duration, size, what, rth, formatDate=1, timeout=60):
        self.asked.append((contract.conId, end, duration, size, what, rth))
        queue = self.answers.get(contract.conId) or [("error", 200, "No security definition has been found")]
        ans = queue.pop(0) if len(queue) > 1 else queue[0]
        if ans == "hang":
            await asyncio.sleep(5)
            return []
        if isinstance(ans, tuple):
            self.errorEvent.emit(7, ans[1], ans[2], contract)
            return []
        return ans


async def no_wait(seconds, slept=None):
    if slept is not None:
        slept.append(seconds)


def collector(tmp_path, ib, session=SESSION, clock=AFTER, **conf):
    slept: list[float] = []
    c = IBDailyPrices(str(tmp_path / "prices"), session, port=4002, ib=ib, ib_module=LIB, log=lambda *a: None,
                      config=IBDataConfig(**conf), clock=lambda: clock,
                      sleep=lambda s: no_wait(s, slept))
    c.slept = slept  # type: ignore[attr-defined]
    return c


def read(tmp_path, folder, sym, session=SESSION):
    with open(tmp_path / "prices" / session / folder / f"{sym}.json") as fh:
        return json.load(fh)


D = sessions("2026-08-01", SESSION)
AAPL = price_path(D, 270.0, 3)
BRKB = price_path(D, 480.0, 4)
SPX = price_path(D, 6900.0, 5)
VIX = price_path(D, 20.0, 6)
USDJPY = price_path(D, 155.0, 7)


# -- which contract --------------------------------------------------------------------------------------
def test_what_is_asked_for_which_symbol():
    assert ib_kind("AAPL") == "equity" and ib_kind("BRK.B") == "equity" and ib_kind("SPX") == "index"
    assert ib_kind("JPYUSD") == "fx" and ib_kind("EURUSD", "fx") == "fx"
    for never in ("BRLUSD", "USDCLP", "IDRUSD", "INRUSD", "ISKUSD", "GC1", "US10Y", "AU1Y"):
        assert ib_kind(never) is None
    assert ib_kind("BTC", "crypto") is None and ib_kind("NDX", "index") is None
    assert ib_local("BRK.B", "equity") == {"sec": "STK", "symbol": "BRK B", "exchange": "SMART",
                                           "what": "ADJUSTED_LAST", "invert": False}
    assert ib_local("JPYUSD", "fx")["symbol"] == "USDJPY" and ib_local("JPYUSD", "fx")["invert"]
    assert ib_local("EURUSD", "fx")["what"] == "MIDPOINT" and not ib_local("EURUSD", "fx")["invert"]
    assert ib_local("VIX", "index") == {"sec": "IND", "symbol": "VIX", "exchange": "CBOE", "what": "TRADES",
                                        "invert": False}
    assert parse_sources("ibkr,alphavantage") == ("ibkr", "alphavantage") and parse_sources("none") == ()
    with pytest.raises(ValueError):
        parse_sources("yahoo")


def test_the_days_bar_is_final_after_the_close_and_the_grace():
    close = _dt.datetime(2026, 11, 18, 21, 0, tzinfo=_dt.timezone.utc).timestamp()      # 16:00 New York
    assert bar_complete_at("equity", SESSION) == close + 15 * 60
    assert bar_complete_at("fx", SESSION) == close + 3600 + 15 * 60                         # 17:00 New York
    assert bar_complete_at("equity", SESSION, {"equity": 60}) == close + 3600
    early = _dt.datetime(2026, 11, 27, 18, 0, tzinfo=_dt.timezone.utc).timestamp()      # 13:00, the day after Thanksgiving
    assert bar_complete_at("equity", "2026-11-27") == early + 15 * 60
    assert bar_complete_at("crypto", SESSION) is None
    frame = pd.DataFrame({"date": ["2026-11-17", SESSION, "2026-11-19"], "close": [1.0, 2.0, 3.0]})
    assert drop_unfinished(frame, "equity", SESSION, AFTER)["date"].tolist() == ["2026-11-17", SESSION]
    assert drop_unfinished(frame, "equity", SESSION, DURING)["date"].tolist() == ["2026-11-17"]
    assert needed_session("equity", "2026-11-26") == "2026-11-25"       # Thanksgiving: no bar expected
    assert needed_session("fx", "2026-11-26") == "2026-11-26" and needed_session("fx", "2026-11-22") == "2026-11-20"


# -- the collection --------------------------------------------------------------------------------------
def test_one_read_only_connection_asks_each_kind_its_own_way(tmp_path):
    known = {("STK", "AAPL"): 1, ("STK", "BRK B"): 2, ("IND", "SPX"): 3, ("IND", "VIX"): 4, ("CASH", "USDJPY"): 5,
             ("CASH", "EURUSD"): 6, ("STK", "TSLA"): 7}
    answers = {1: [bars(AAPL)], 2: [bars(BRKB)], 3: [bars(SPX)], 4: [bars(VIX)], 5: [bars(USDJPY)],
               6: [("error", 162, ("Historical Market Data Service error message:No market data permissions for "
                    "IDEALPRO CASH"))],
               7: [("error", 354, "Requested market data is not subscribed.")]}
    ib = StubIB(known, answers)
    c = collector(tmp_path, ib)
    stats = c.collect({"AAPL": "equity", "BRK.B": "equity", "SPX": None, "VIX": "index", "JPYUSD": "fx",
                       "EURUSD": "fx", "BRLUSD": "fx", "TSLA": "equity", "KVUE": "equity", "GC1": None})
    assert ib.connected is None                                          # disconnected at the end
    assert stats["asked"] == 8 and stats["served"] == 5 and stats["fallback"] == 3
    assert stats["errors"] == {"162": 1, "354": 1, "200": 1}
    assert "BRLUSD" not in ib.qualified and "GC1" not in ib.qualified
    whats = {cid: (end, dur, size, what, rth) for cid, end, dur, size, what, rth in ib.asked}
    assert whats[1] == ("", "3 M", "1 day", "ADJUSTED_LAST", True) and whats[3][3] == "TRADES"
    assert whats[5][3] == "MIDPOINT"
    a = read(tmp_path, "IB_ADJUSTED_LAST", "AAPL")
    assert a["contract"] == "AAPL" and a["bars"][-1][0] == SESSION and a["bars"][-1][4] == AAPL[SESSION]
    assert read(tmp_path, "IB_ADJUSTED_LAST", "BRK.B")["contract"] == "BRK B"
    assert read(tmp_path, "IB_TRADES", "VIX")["bars"][-1][4] == VIX[SESSION]
    jpy = read(tmp_path, "IB_MIDPOINT", "JPYUSD")["bars"][-1]                 # 1 / USD.JPY, high and low swapped
    assert jpy[4] == pytest.approx(1 / USDJPY[SESSION]) and jpy[2] == pytest.approx(1 / (USDJPY[SESSION] * 0.98))
    eur = read(tmp_path, "IB_MIDPOINT", "EURUSD")
    assert eur["code"] == 162 and "permissions" in eur["error"]
    assert read(tmp_path, "IB_ADJUSTED_LAST", "KVUE")["code"] == 200
    assert read(tmp_path, "IB_ADJUSTED_LAST", "TSLA")["code"] == 354


def test_the_connection_is_paper_read_only_and_on_its_own_client_id(tmp_path):
    ib = StubIB({("STK", "AAPL"): 1}, {1: [bars(AAPL)]})
    collector(tmp_path, ib).collect({"AAPL": "equity"})
    assert ib.asked and ib.connected is None
    seen = {}
    ib2 = StubIB({("STK", "AAPL"): 1}, {1: [bars(AAPL)]})
    ib2.connect = lambda host, port, clientId, readonly=False, account="": seen.update(  # type: ignore[method-assign]
        clientId=clientId, readonly=readonly) or setattr(ib2, "connected", {})
    collector(tmp_path / "b", ib2).collect({"AAPL": "equity"})
    assert seen == {"clientId": 957, "readonly": True}
    real = StubIB({("STK", "AAPL"): 1}, {1: [bars(AAPL)]}, accounts=("DU1234503", "U7654321"))
    with pytest.raises(AdapterError, match="not paper accounts"):
        collector(tmp_path / "c", real).collect({"AAPL": "equity"})
    assert not real.asked
    with pytest.raises(AdapterError, match="not a paper port"):
        IBDailyPrices(str(tmp_path), SESSION, port=4001, ib=StubIB({}, {}), ib_module=LIB).collect({"AAPL": "equity"})
    with pytest.raises(ValueError, match="is the account's"):
        IBDailyPrices(str(tmp_path), SESSION, config=IBDataConfig(client_id=951), account_client_id=951)


def test_a_pacing_violation_and_an_unanswered_request_are_retried_with_a_backoff(tmp_path):
    pacing = ("error", 162, ("Historical Market Data Service error message:API historical data query cancelled: "
                            "pacing violation"))
    ib = StubIB({("STK", "AAPL"): 1, ("STK", "HD"): 2}, {1: [pacing, pacing, bars(AAPL)], 2: ["hang"]})
    c = collector(tmp_path, ib, timeout=0.01)
    stats = c.collect({"AAPL": "equity", "HD": "equity"})
    assert read(tmp_path, "IB_ADJUSTED_LAST", "AAPL")["retries"] == 2
    hd = read(tmp_path, "IB_ADJUSTED_LAST", "HD")
    assert "no answer in 0.01 s" in hd["error"] and "after 4 tries" in hd["error"]
    assert sorted(c.slept) == [10.0, 10.0, 20.0, 20.0, 40.0, 80.0]
    assert stats["served"] == 1 and stats["retries"] == 6


def test_the_conids_are_kept_and_a_stale_one_is_qualified_again(tmp_path):
    ib = StubIB({("STK", "AAPL"): 1, ("STK", "HD"): 2}, {1: [bars(AAPL)], 2: [bars(AAPL)]})
    collector(tmp_path, ib).collect({"AAPL": "equity", "HD": "equity"})
    assert sorted(ib.qualified) == ["AAPL", "HD"]
    with open(tmp_path / "prices" / "ib_conids.json") as fh:
        assert json.load(fh)["STK:AAPL"]["conId"] == 1
    # the next session: no qualification, the cached conIds are asked directly
    nxt = "2026-11-19"
    ib2 = StubIB({("STK", "AAPL"): 1, ("STK", "HD"): 22}, {1: [bars(AAPL)], 22: [bars(AAPL)]})
    stats = collector(tmp_path, ib2, session=nxt, clock=AFTER + 86400).collect({"AAPL": "equity", "HD": "equity"})
    assert ib2.qualified == ["HD"] and stats["conid_cache"] == 2       # HD's old conId 2 answered 200: asked again
    assert [a[0] for a in ib2.asked].count(2) == 1 and 22 in [a[0] for a in ib2.asked]
    assert stats["served"] == 2
    with open(tmp_path / "prices" / "ib_conids.json") as fh:
        assert json.load(fh)["STK:HD"]["conId"] == 22
    # a symbol without a contract is not qualified again the next session
    ib4 = StubIB({("STK", "AAPL"): 1}, {1: [bars(AAPL)]})
    collector(tmp_path / "m", ib4).collect({"AAPL": "equity", "KVUE": "equity"})
    ib5 = StubIB({("STK", "AAPL"): 1}, {1: [bars(AAPL)]})
    st = collector(tmp_path / "m", ib5, session=nxt, clock=AFTER + 86400).collect({"AAPL": "equity", "KVUE": "equity"})
    assert ib5.qualified == [] and st["known_missing"] == 1
    assert read(tmp_path / "m", "IB_ADJUSTED_LAST", "KVUE", nxt)["code"] == 200
    # a week later the table is too old: everything is qualified again
    ib3 = StubIB({("STK", "AAPL"): 1}, {1: [bars(AAPL)]})
    collector(tmp_path, ib3, session="2026-11-27", clock=AFTER + 9 * 86400).collect({"AAPL": "equity"})
    assert ib3.qualified == ["AAPL"]


def test_a_rerun_asks_only_what_is_not_final_yet(tmp_path):
    ib = StubIB({("STK", "AAPL"): 1}, {1: [bars(AAPL)]})
    collector(tmp_path, ib, clock=DURING).collect({"AAPL": "equity"})       # 16:05: the bar is still forming
    cache = IBPriceCache(str(tmp_path / "prices"), SESSION)
    frame, why = cache.bars("equity", "AAPL")
    assert frame is None and "no final bar for 2026-11-18" in why
    again = StubIB({("STK", "AAPL"): 1}, {1: [bars(AAPL)]})
    collector(tmp_path, again).collect({"AAPL": "equity"})
    assert again.asked and cache.bars("equity", "AAPL")[0]["date"].iloc[-1] == SESSION
    third = StubIB({("STK", "AAPL"): 1}, {1: [bars(AAPL)]})
    assert collector(tmp_path, third).collect({"AAPL": "equity"})["kept"] == 1 and not third.asked


# -- the workers' loads ----------------------------------------------------------------------------------
def write_ib(tmp_path, folder, sym, closes, *, fetched=AFTER, kind="equity", error=None):
    path = tmp_path / "prices" / SESSION / folder
    path.mkdir(parents=True, exist_ok=True)
    payload = {"symbol": sym, "kind": kind, "fetched_ts": fetched}
    if error:
        payload.update(error=error, code=162)
    else:
        payload["bars"] = [[d, c * 0.99, c * 1.01, c * 0.98, c, 1000.0] for d, c in sorted(closes.items())]
    (path / f"{sym}.json").write_text(json.dumps(payload))


def fresh(tmp_path, api, sources=("ibkr", "alphavantage"), **kw):
    logs: list[str] = []
    f = FreshPrices.from_env(str(tmp_path / "prices"), SESSION, log=logs.append, strict=False, sources=sources,
                             key=KEY, get=api, sleep=lambda s: None, **kw)
    f.logs = logs  # type: ignore[attr-defined]
    return f


def test_ib_serves_first_and_alphavantage_the_rest(tmp_path):
    closes = {s: price_path(DAYS, 50.0 + i, 10 + i) for i, s in enumerate(("AAA", "BBB", "CCC", "DDD"))}
    write_ib(tmp_path, "IB_ADJUSTED_LAST", "SPY", SPY)
    write_ib(tmp_path, "IB_ADJUSTED_LAST", "AAA", closes["AAA"])
    write_ib(tmp_path, "IB_ADJUSTED_LAST", "BBB", closes["BBB"], error="No market data permissions")
    write_ib(tmp_path, "IB_ADJUSTED_LAST", "CCC", closes["CCC"], fetched=DURING)    # its last bar not final
    api = FakeAPI({s: av_equity(c) for s, c in closes.items()})
    f = fresh(tmp_path, api)
    frame = pd.concat([pwb_rows(s, c, "2026-10-30") for s, c in closes.items()], ignore_index=True)
    out = f.extend("paperswithbacktest/Stocks-Daily-Price", frame)
    assert set(out.groupby("symbol")["date"].max().astype(str)) == {SESSION}
    asked = sorted(u.split("symbol=")[1].split("&")[0] for u in api.urls)
    assert asked == ["BBB", "CCC", "DDD"]                                # neither SPY nor AAA went to AlphaVantage
    summary = f.summary()
    assert summary["served"] == {"ibkr": ["AAA", "SPY"], "alphavantage": ["BBB", "CCC", "DDD"]}
    assert "IB error 162" in summary["ib_fallbacks"]["BBB"] and "no final bar" in summary["ib_fallbacks"]["CCC"]
    assert summary["ib_fallbacks"]["DDD"] == "not collected from IB"
    aaa = out[out["symbol"] == "AAA"].set_index(out[out["symbol"] == "AAA"]["date"].astype(str))["close"]
    assert aaa[SESSION] == pytest.approx(closes["AAA"][SESSION])


def test_ib_alone_leaves_the_rest_stale(tmp_path):
    write_ib(tmp_path, "IB_ADJUSTED_LAST", "SPY", SPY)
    closes = price_path(DAYS, 50.0, 10)
    api = FakeAPI({})
    f = fresh(tmp_path, api, sources=("ibkr",))
    f.extend("paperswithbacktest/Stocks-Daily-Price", pwb_rows("ZZZ", closes, "2026-10-30"))
    assert not api.urls and f.stale_info["ZZZ"]["cause"] == "fetch_error"
    assert "alphavantage is not in --fresh-prices" in f.stale["ZZZ"]


def test_an_index_takes_ibs_levels_without_a_proxy(tmp_path):
    write_ib(tmp_path, "IB_ADJUSTED_LAST", "SPY", SPY)
    vix = price_path(DAYS, 20.0, 6)
    write_ib(tmp_path, "IB_TRADES", "VIX", vix, kind="index")
    api = FakeAPI({})
    f = fresh(tmp_path, api, proxies={"VIX": "VIXY"})                  # VIXY is not chained: not needed
    rows = pwb_rows("VIX", vix, "2026-10-30")
    out = f.extend("paperswithbacktest/Indices-Daily-Price", rows)
    last = out.sort_values("date").iloc[-1]
    assert str(last["date"]) == SESSION and last["close"] == pytest.approx(vix[SESSION])
    assert f.splices["VIX"].source == "IB index" and not f.stale and not api.urls
    assert f.served["VIX"] == "ibkr"


def test_forex_falls_back_on_alphavantage_until_the_subscription_is_active(tmp_path):
    write_ib(tmp_path, "IB_ADJUSTED_LAST", "SPY", SPY)
    eur = price_path(DAYS, 1.15, 8)
    write_ib(tmp_path, "IB_MIDPOINT", "EURUSD", eur, kind="fx",
             error="No market data permissions for IDEALPRO CASH")
    fx = {"Time Series FX (Daily)": {d: {"1. open": str(r), "2. high": str(r), "3. low": str(r), "4. close": str(r)}
                                     for d, r in eur.items()}}
    api = FakeAPI({"EURUSD": fx})
    f = fresh(tmp_path, api)
    out = f.extend("paperswithbacktest/Forex-Daily-Price", pwb_rows("EURUSD", eur, "2026-10-30"))
    assert str(out["date"].max()) == SESSION and f.served["EURUSD"] == "alphavantage"
    # once IDEAL FX is shared with the paper user, the same run serves it from IB
    write_ib(tmp_path, "IB_MIDPOINT", "EURUSD", eur, kind="fx")
    api2 = FakeAPI({})
    f2 = fresh(tmp_path, api2)
    f2.extend("paperswithbacktest/Forex-Daily-Price", pwb_rows("EURUSD", eur, "2026-10-30"))
    assert f2.served["EURUSD"] == "ibkr" and not api2.urls


def test_the_two_sources_are_compared_when_both_are_at_hand(tmp_path):
    write_ib(tmp_path, "IB_ADJUSTED_LAST", "SPY", SPY)
    closes = price_path(DAYS, 100.0, 12)
    ib = dict(closes, **{SESSION: closes[SESSION] * 1.01})            # IB's final close, AV's provisional one
    write_ib(tmp_path, "IB_ADJUSTED_LAST", "ABBV", ib)
    client = AlphaVantage(str(tmp_path / "prices"), SESSION, key=KEY, get=FakeAPI({"ABBV": av_equity(closes)}),
                          sleep=lambda s: None, wall=lambda: AFTER)
    client.daily("equity", "ABBV")                                     # an earlier run left it in the cache
    f = fresh(tmp_path, FakeAPI({}))
    f.extend("paperswithbacktest/Stocks-Daily-Price", pwb_rows("ABBV", closes, "2026-10-30"))
    cross = f.summary()["crosscheck"]["ABBV"]
    assert cross["date"] == SESSION and cross["gap"] == pytest.approx(0.01, abs=1e-4)
    assert any("ABBV: IB" in x and "+1.00%" in x for x in f.logs)


def test_alphavantages_bar_follows_the_same_rule(tmp_path):
    closes = price_path(DAYS, 100.0, 13)
    api = FakeAPI({"ESTC": av_equity(closes)})
    now = [DURING]
    c = AlphaVantage(str(tmp_path / "prices"), SESSION, key=KEY, get=api, sleep=lambda s: None, wall=lambda: now[0])
    path = tmp_path / "prices" / SESSION / "TIME_SERIES_DAILY_ADJUSTED" / "ESTC.json"
    first = c.daily("equity", "ESTC")
    os.utime(path, (DURING, DURING))                                   # written at 16:05 New York
    first = c.daily("equity", "ESTC")
    assert first["date"].iloc[-1] < SESSION and len(api.urls) == 1    # the provisional bar is not taken
    now[0] = AFTER
    again = c.daily("equity", "ESTC")
    assert again["date"].iloc[-1] == SESSION and len(api.urls) == 2   # asked again once final
    # av_equity: 120 (fresh_close_grace): read at 16:40 New York, AlphaVantage's bar is not taken
    f = FreshPrices.from_env(str(tmp_path / "p2"), SESSION, strict=False, key=KEY, get=api, sleep=lambda s: None,
                             grace={"av_equity": 120}, wall=lambda: AFTER - 3600)
    assert f.client.grace == {"equity": 120} and f.ib is None
    assert f.client.daily("equity", "ESTC")["date"].iloc[-1] < SESSION
    with pytest.raises(FetchError):
        AlphaVantage(str(tmp_path / "p3"), SESSION, key="", get=api).daily("equity", "ESTC")


# -- the provider -----------------------------------------------------------------------------------------
def test_the_provider_collects_once_before_the_strategies(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", KEY)
    (tmp_path / "s.py").write_text("")
    meta = MetaPortfolio([Sleeve("s1", 1.0, path=str(tmp_path / "s.py"))], fresh_prices="ibkr,alphavantage",
                         substitutes={"VIX": "VIXY"})
    books = tmp_path / "run" / "books"
    old = books / "2026-11-17-fresh"
    old.mkdir(parents=True)
    prev = SleeveBook("2026-11-17", 100.0, close={"AAPL": 1.0, "EURUSD": 1.1, "VIX": 20.0, "GC1": 2000.0})
    (old / "s1.json").write_text(json.dumps({"books": [prev.to_dict()]}))
    (tmp_path / "run" / "prices" / "2026-11-17" / "TIME_SERIES_DAILY_ADJUSTED").mkdir(parents=True)
    (tmp_path / "run" / "prices" / "2026-11-17" / "TIME_SERIES_DAILY_ADJUSTED" / "HD.json").write_text("{}")
    calls: list = []
    order: list[str] = []

    class Collector:
        def __init__(self, session, cache, **kw):
            calls.append((session, cache, kw))

        def collect(self, wanted):
            order.append("ib")
            calls.append(dict(wanted))
            return {"asked": 3, "served": 3, "fallback": 0, "errors": {}, "seconds": 1.0}

    book = SleeveBook(SESSION, 100.0, positions={"AAPL": 1.0}, target={"AAPL": 1.0}, close={"AAPL": 10.0})
    seen: dict = {}

    def fake_run(cmd, **kw):
        order.append("worker")
        seen["cmd"] = cmd
        with open(cmd[cmd.index("-m") + 3], "w") as fh:
            json.dump({"books": [book.to_dict()], "fresh": {
                "filled": 2, "last": SESSION, "requests": 1, "max_spread": 0.0,
                "served": {"ibkr": ["AAPL", "SPY"], "alphavantage": ["EURUSD"]}, "stale": {"KVUE": "x"},
                "ib_fallbacks": {"EURUSD": "IB error 162"}, "crosscheck": {}}}, fh)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tradewind.live.daily.providers.subprocess.run", fake_run)
    provider = PwbProvider(meta, str(books), log=lambda *a: None, ib_factory=Collector,
                           ib={"host": "127.0.0.1", "port": 4002, "client_id": None, "account_client_id": 951})
    provider.books(SESSION, 2)
    assert order == ["ib", "worker"]
    session, cache, kw = calls[0]
    assert session == SESSION and cache == str(tmp_path / "run" / "prices") and kw["config"].client_id == 957
    wanted = calls[1]
    assert wanted["HD"] == "equity" and wanted["AAPL"] is None and wanted["SPY"] == "equity"
    assert wanted["VIXY"] == "equity" and "VIX" in wanted and "GC1" in wanted   # GC1: skipped by ib_kind
    conf = json.loads(seen["cmd"][seen["cmd"].index("--fresh-config") + 1])
    assert conf["sources"] == ["ibkr", "alphavantage"]
    report = provider.price_report()
    assert report["n"] == 4 and report["ibkr"] == 2 and report["alphavantage"] == 1 and report["stale"] == 1
    assert report["ib"]["served"] == 3
    provider.books(SESSION, 2)                                          # cached books, collected once
    assert order.count("ib") == 1
    with pytest.raises(ValueError, match="is the account's"):
        PwbProvider(meta, str(books), ib={"client_id": 951, "account_client_id": 951})


def test_an_unreachable_ib_leaves_every_symbol_to_alphavantage(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", KEY)
    meta = MetaPortfolio([Sleeve("s1", 1.0)], fresh_prices="ibkr,alphavantage")

    class Down:
        def __init__(self, *a, **kw):
            pass

        def collect(self, wanted):
            raise ConnectionRefusedError("[Errno 61] Connection refused")

    provider = PwbProvider(meta, str(tmp_path / "books"), log=lambda *a: None, ib_factory=Down)
    stats = provider.collect_ib(SESSION)
    assert "ConnectionRefusedError" in stats["error"]
    assert provider.warnings and "AlphaVantage serves every symbol" in provider.warnings[0]


def test_the_meta_portfolio_options_of_ib_prices():
    m = MetaPortfolio([Sleeve("a", 1.0)], fresh_prices=["ibkr", "alphavantage"], fresh_close_grace={"av_equity": 90},
                      fresh_ib={"client_id": 958, "concurrency": 20})
    assert m.fresh_prices == "ibkr,alphavantage"
    assert m.fresh_config()["sources"] == ["ibkr", "alphavantage"] and m.fresh_config()["grace"] == {"av_equity": 90.0}
    for bad in ({"fresh_ib": {"clientid": 1}}, {"fresh_ib": {"concurrency": 0}}, {"fresh_close_grace": {"fx": -1}}):
        with pytest.raises(ValueError):
            MetaPortfolio([Sleeve("a", 1.0)], **bad)
    assert universe("/nonexistent", SESSION, None, [], ["SPY"]) == {"SPY": "equity"}


def test_the_report_says_who_served_the_prices():
    from tradewind.live.daily.session import prices_line

    p = {"sources": ["ibkr", "alphavantage"], "n": 1224, "ibkr": 1199, "alphavantage": 21, "stale": 4,
         "shares": {"ibkr": 1199 / 1224, "alphavantage": 21 / 1224, "stale": 4 / 1224},
         "ib": {"asked": 1203, "served": 1199, "seconds": 41.2}}
    assert prices_line(p) == ("fresh prices (ibkr,alphavantage): 1224 symbol(s), IB 1199 (98.0%), AlphaVantage 21 "
                              "(1.7%), stale 4 (0.3%); IB collection 41 s (1199/1203 asked)")
    down = dict(p, ib={"error": "ConnectionRefusedError: refused"})
    assert prices_line(down).endswith("; IB not reached: ConnectionRefusedError: refused")


def test_a_relative_run_directory_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    provider = PwbProvider(MetaPortfolio([Sleeve("a", 1.0)]), "run/books", log=lambda *a: None)
    assert provider.cache_dir == str(tmp_path / "run" / "books") and os.path.isabs(provider.fresh_cache)
