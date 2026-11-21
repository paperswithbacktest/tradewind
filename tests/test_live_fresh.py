"""Fresh closes for the daily run (tradewind.live.daily.fresh), without the network: the
AlphaVantage answers are built here in the API's own layout.

What is checked: a series is continued on the last common day before the dataset's last bar,
rescaled so that a dividend between the two cut-offs does not move the history; a dataset
whose last bar was partial gets AlphaVantage's; two sources that disagree are refused; a live
symbol still behind, or one of a dataset with no source, stops the strategy with the list;
delisted names are left alone; the answers are cached per session, throttling and network
errors are retried, the key never shows in a message; the provider turns a stale worker into
``StaleData`` and nothing is proposed.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import types
import urllib.error

import numpy as np
import pandas as pd
import pytest

from tradewind.live.daily import MetaPortfolio, PwbProvider, Sleeve, StaleData
from tradewind.live.daily.fresh import (
    AlphaVantage,
    FetchError,
    FreshPrices,
    StalePrices,
    install,
    last_close_session,
    parse_daily,
    splice,
)

KEY = "SECRETKEY123"
SESSION = "2026-11-18"


def sessions(start: str, end: str) -> list[str]:
    return [d.date().isoformat() for d in pd.bdate_range(start, end)]


def av_equity(closes: dict[str, float], dividend: tuple[str, float] | None = None) -> dict:
    """A TIME_SERIES_DAILY_ADJUSTED answer: raw closes, and adjusted closes that take out
    ``dividend`` (ex-date, amount) on every earlier day."""
    series = {}
    for day, c in closes.items():
        adj = c
        if dividend and day < dividend[0]:
            prev = closes[max(d for d in closes if d < dividend[0])]
            adj = c * (1 - dividend[1] / prev)
        series[day] = {"1. open": f"{c * 0.99:.4f}", "2. high": f"{c * 1.01:.4f}", "3. low": f"{c * 0.98:.4f}",
                       "4. close": f"{c:.4f}", "5. adjusted close": f"{adj:.4f}", "6. volume": "1000",
                       "7. dividend amount": "0.0000", "8. split coefficient": "1.0"}
    return {"Meta Data": {"1. Information": "Daily Time Series with Splits and Dividend Events"},
            "Time Series (Daily)": dict(sorted(series.items(), reverse=True))}


def price_path(days: list[str], start: float, seed: int) -> dict[str, float]:
    rng = np.random.default_rng(seed)
    p = start * np.cumprod(1 + rng.normal(0, 0.01, len(days)))
    return {d: float(round(x, 4)) for d, x in zip(days, p)}


def pwb_rows(symbol: str, closes: dict[str, float], until: str, kind: str = "CS") -> pd.DataFrame:
    days = [d for d in closes if d <= until]
    return pd.DataFrame({"symbol": symbol, "date": [_dt.date.fromisoformat(d) for d in days],
                         "volume": 500, "close": [closes[d] for d in days], "type": kind,
                         "open": [closes[d] * 0.99 for d in days], "high": [closes[d] * 1.01 for d in days],
                         "low": [closes[d] * 0.98 for d in days]})


class FakeAPI:
    """``url -> text``: the answers by symbol, every URL kept."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        sym = q.get("symbol") or q["from_symbol"] + q["to_symbol"]
        ans = self.answers.get(sym, {"Error Message": "Invalid API call."})
        if callable(ans):
            ans = ans()
        return json.dumps(ans)


def client(tmp_path, api, **kw) -> AlphaVantage:
    kw.setdefault("sleep", lambda s: None)
    return AlphaVantage(str(tmp_path / "prices"), SESSION, key=KEY, get=api, log=lambda *a: None, **kw)


DAYS = sessions("2026-07-01", SESSION)
SPY = price_path(DAYS, 600.0, 1)


# -- parsing -------------------------------------------------------------------------------------
def test_parse_equity_fx_and_crypto_answers():
    eq = parse_daily("equity", av_equity({"2026-11-17": 10.0, "2026-11-18": 11.0}, ("2026-11-18", 0.5)))
    assert list(eq["date"]) == ["2026-11-17", "2026-11-18"]
    assert eq["close"].tolist() == [10.0, 11.0] and eq["adj_close"].iloc[0] == pytest.approx(9.5)
    fx = parse_daily("fx", {"Time Series FX (Daily)": {"2026-11-18": {"1. open": "1.1", "2. high": "1.2",
                                                                      "3. low": "1.0", "4. close": "1.15"}}})
    assert fx["close"].iloc[0] == fx["adj_close"].iloc[0] == 1.15 and np.isnan(fx["volume"].iloc[0])
    old = {"Time Series (Digital Currency Daily)": {"2026-11-18": {
        "1a. open (USD)": "1", "1b. open (USD)": "1", "2a. high (USD)": "2", "3a. low (USD)": "0.5",
        "4a. close (USD)": "1.5", "5. volume": "10"}}}
    new = {"Time Series (Digital Currency Daily)": {"2026-11-18": {
        "1. open": "1", "2. high": "2", "3. low": "0.5", "4. close": "1.5", "5. volume": "10"}}}
    for payload in (old, new):
        c = parse_daily("crypto", payload)
        assert c[["open", "high", "low", "close", "volume"]].iloc[0].tolist() == [1, 2, 0.5, 1.5, 10]
    assert parse_daily("equity", {"Error Message": "x"}) is None


# -- the splice ----------------------------------------------------------------------------------
def test_a_dividend_between_the_cutoffs_rescales_the_tail_not_the_history():
    closes = price_path(DAYS, 100.0, 2)
    rows = pwb_rows("XYZ", closes, "2026-10-30")
    fresh = parse_daily("equity", av_equity(closes, ("2026-11-10", 1.0)))
    out, done = splice(rows, fresh, SESSION)
    assert done.status == "filled" and done.last_after == SESSION and done.anchor == "2026-10-29"
    # the history up to the anchor is the dataset's, divided by one factor: its returns are kept
    hist = out[out["date"] <= _dt.date(2026, 10, 29)].reset_index(drop=True)
    base = rows[rows["date"] <= _dt.date(2026, 10, 29)].reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        np.testing.assert_allclose(hist[col] * done.factor, base[col], rtol=1e-12)
    pd.testing.assert_frame_equal(hist.drop(columns=["open", "high", "low", "close"]),
                                  base.drop(columns=["open", "high", "low", "close"]))
    # after it, AlphaVantage's prices: the last close is the traded price, and the return
    # across the ex-date is the dividend-adjusted one (nothing "un-adjusted" at the junction)
    s = out.set_index(out["date"].astype(str))["close"]
    assert s[SESSION] == pytest.approx(closes[SESSION], rel=1e-6)
    prev = closes["2026-11-09"]                                 # the session before the ex-date
    assert s["2026-11-10"] / s["2026-11-09"] == pytest.approx(closes["2026-11-10"] / (prev - 1.0), rel=1e-4)
    assert s["2026-10-30"] / s["2026-10-29"] == pytest.approx(closes["2026-10-30"] / closes["2026-10-29"], rel=1e-4)
    assert done.factor == pytest.approx(1 / (1 - 1.0 / prev), rel=1e-4) and done.spread < 1e-4
    assert out["date"].map(type).eq(_dt.date).all() and out["type"].eq("CS").all()
    assert out["volume"].dtype == rows["volume"].dtype


def test_a_partial_last_bar_is_replaced_and_reported():
    closes = price_path(DAYS, 50.0, 3)
    rows = pwb_rows("XYZ", closes, "2026-11-02")
    rows.loc[rows.index[-1], "close"] *= 1.03                  # downloaded during the session
    out, done = splice(rows, parse_daily("equity", av_equity(closes)), SESSION)
    assert done.anchor == "2026-10-30" and done.last_bar_gap == pytest.approx(0.03, rel=1e-3)
    s = out.set_index(out["date"].astype(str))["close"]
    assert s["2026-11-02"] == pytest.approx(closes["2026-11-02"], rel=1e-6)


def test_two_sources_that_disagree_are_refused():
    closes = price_path(DAYS, 20.0, 4)
    rows = pwb_rows("XYZ", closes, "2026-10-30")
    other = {d: c * (2.0 if d < "2026-10-28" else 1.0) for d, c in closes.items()}   # a split one source missed
    out, done = splice(rows, parse_daily("equity", av_equity(other)), SESSION)
    assert done.status == "refused" and "disagree" in done.reason
    assert out["date"].max() == _dt.date(2026, 10, 30)


def test_bars_after_the_session_are_not_taken():
    days = sessions("2026-07-01", "2026-11-19")
    closes = price_path(days, 20.0, 5)
    out, done = splice(pwb_rows("XYZ", closes, "2026-10-30"), parse_daily("equity", av_equity(closes)), SESSION)
    assert done.last_after == SESSION and out["date"].max() == _dt.date(2026, 11, 18)


# -- whole loads ---------------------------------------------------------------------------------
def frame_of(**series) -> pd.DataFrame:
    return pd.concat(series.values(), ignore_index=True)


def test_extend_fills_live_symbols_and_leaves_delisted_ones(tmp_path):
    a, b = price_path(DAYS, 30.0, 6), price_path(DAYS, 70.0, 7)
    dead = pwb_rows("DEAD", price_path(sessions("2020-01-01", "2021-06-30"), 5.0, 8), "2021-06-30")
    api = FakeAPI({"SPY": av_equity(SPY), "AAA": av_equity(a), "BBB": av_equity(b)})
    fresh = FreshPrices(client(tmp_path, api), SESSION, log=lambda *x: None)
    frame = frame_of(a=pwb_rows("AAA", a, "2026-10-30"), b=pwb_rows("BBB", b, "2026-11-04"), d=dead)
    out = fresh.extend("Stocks-Daily-Price", frame)
    lasts = out.groupby("symbol")["date"].max().astype(str).to_dict()
    assert lasts == {"AAA": SESSION, "BBB": SESSION, "DEAD": "2021-06-30"}
    assert not fresh.stale and fresh.inactive == {"DEAD"}
    assert sorted(p.split("symbol=")[1].split("&")[0] for p in api.urls) == ["AAA", "BBB", "SPY"]
    # a second load of the day reads nothing more, even in a new process (the disk cache)
    again = FreshPrices(client(tmp_path, api), SESSION, log=lambda *x: None)
    again.extend("paperswithbacktest/Stocks-Daily-Price", frame)
    assert len(api.urls) == 3
    s = fresh.summary()
    assert s["filled"] == 2 and s["last"] == SESSION and s["refused"] == {}
    assert all(KEY not in u.replace(f"apikey={KEY}", "") for u in api.urls)
    # other datasets pass through
    other = pd.DataFrame({"symbol": ["AAA"], "date": ["2020-01-01"], "eps": [1.0]})
    assert fresh.extend("Stocks-Quarterly-Earnings", other) is other


def test_a_symbol_still_behind_stops_the_strategy_with_the_list(tmp_path):
    a = price_path(DAYS, 30.0, 6)
    api = FakeAPI({"SPY": av_equity(SPY), "AAA": av_equity(a)})       # ZZZ unknown to AlphaVantage
    fresh = FreshPrices(client(tmp_path, api), SESSION, log=lambda *x: None)
    frame = frame_of(a=pwb_rows("AAA", a, "2026-10-30"), z=pwb_rows("ZZZ", a, "2026-10-30"))
    with pytest.raises(StalePrices, match="ZZZ"):
        fresh.extend("Stocks-Daily-Price", frame)
    assert list(fresh.stale) == ["ZZZ"] and "unknown" in fresh.stale["ZZZ"]
    assert fresh.summary()["refused"] == {"ZZZ": "AlphaVantage does not know the symbol"}


def test_datasets_without_a_source_are_listed_as_not_covered(tmp_path):
    api = FakeAPI({"SPY": av_equity(SPY)})
    fresh = FreshPrices(client(tmp_path, api), SESSION, log=lambda *x: None)
    cl = pwb_rows("CL1", price_path(DAYS, 60.0, 9), "2026-10-30", kind="")
    with pytest.raises(StalePrices, match="CL1"):
        fresh.extend("Commodities-Daily-Price", cl)
    assert fresh.not_covered == {"CL1": "Commodities-Daily-Price"}


def test_a_holiday_is_read_off_the_market_not_the_calendar(tmp_path):
    a = price_path(DAYS, 30.0, 6)
    spy = {d: c for d, c in SPY.items() if d != SESSION}      # no bar on the session: a holiday
    a_av = {d: c for d, c in a.items() if d != SESSION}
    fresh = FreshPrices(client(tmp_path, FakeAPI({"SPY": av_equity(spy), "AAA": av_equity(a_av)})), SESSION,
                        log=lambda *x: None)
    out = fresh.extend("ETFs-Daily-Price", pwb_rows("AAA", a, "2026-10-30", kind="ETF"))
    assert out["date"].max() == _dt.date(2026, 11, 17) and not fresh.stale


def test_forex_pairs_are_completed_from_fx_daily(tmp_path):
    rates = price_path(DAYS, 1.1, 10)
    fx = {"Time Series FX (Daily)": {d: {"1. open": str(r), "2. high": str(r), "3. low": str(r), "4. close": str(r)}
                                     for d, r in rates.items()}}
    api = FakeAPI({"SPY": av_equity(SPY), "EURUSD": fx})
    fresh = FreshPrices(client(tmp_path, api), SESSION, log=lambda *x: None)
    rows = pwb_rows("EURUSD", rates, "2026-10-30").drop(columns="type")
    rows["volume"] = 0
    out = fresh.extend("Forex-Daily-Price", rows)
    assert out["date"].max() == _dt.date(2026, 11, 18) and out["volume"].eq(0).all()
    assert any("from_symbol=EUR&to_symbol=USD" in u for u in api.urls)


def test_install_wraps_load_dataset_and_so_get_pricing(tmp_path):
    a = price_path(DAYS, 30.0, 6)
    module = types.SimpleNamespace(load_dataset=lambda path, symbols=None, **kw: pwb_rows("AAA", a, "2026-10-30"))
    module.get_pricing = lambda syms: module.load_dataset("ETFs-Daily-Price", syms)   # a module global lookup
    fresh = FreshPrices(client(tmp_path, FakeAPI({"SPY": av_equity(SPY), "AAA": av_equity(a)})), SESSION,
                        log=lambda *x: None)
    install(fresh, module)
    assert module.get_pricing(["AAA"])["date"].max() == _dt.date(2026, 11, 18)


def test_the_loads_load_dataset_makes_for_itself_pass_through(tmp_path):
    a = price_path(DAYS, 30.0, 6)
    fx = pwb_rows("AFNUSD", price_path(DAYS, 0.01, 11), "2026-10-30")        # unknown to AlphaVantage
    module = types.SimpleNamespace()

    def load_dataset(path, symbols=None, **kw):
        if path == "Forex-Daily-Price":
            return fx
        module.load_dataset("Forex-Daily-Price")                           # to_usd's conversion table
        return pwb_rows("AAA", a, "2026-10-30")

    module.load_dataset = load_dataset
    api = FakeAPI({"SPY": av_equity(SPY), "AAA": av_equity(a)})
    fresh = FreshPrices(client(tmp_path, api), SESSION, log=lambda *x: None)
    install(fresh, module)
    assert module.load_dataset("ETFs-Daily-Price", ["AAA"])["date"].max() == _dt.date(2026, 11, 18)
    assert not fresh.stale and not any("AFN" in u for u in api.urls)


# -- the client ----------------------------------------------------------------------------------
def test_throttling_and_network_errors_are_retried_and_the_key_never_shows(tmp_path):
    calls = {"n": 0}

    def flaky(url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError(f"connection reset on {url}")
        if calls["n"] == 2:
            return json.dumps({"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is ..."})
        return json.dumps(av_equity(SPY))

    slept: list[float] = []
    c = client(tmp_path, flaky, sleep=slept.append)
    assert c.daily("equity", "SPY")["date"].iloc[-1] == SESSION
    assert 60.0 in slept and calls["n"] == 3

    def down(url):
        raise urllib.error.URLError(f"no route to {url}")

    c2 = AlphaVantage(str(tmp_path / "other"), SESSION, key=KEY, get=down, sleep=lambda s: None, retries=2)
    with pytest.raises(FetchError) as err:
        c2.daily("equity", "SPY")
    assert KEY not in str(err.value) and "***" in str(err.value)
    assert not os.path.exists(tmp_path / "other" / SESSION / "TIME_SERIES_DAILY_ADJUSTED" / "SPY.json")


def test_requests_are_spaced_to_the_rate(tmp_path):
    now = {"t": 0.0}
    waits: list[float] = []

    def sleep(s):
        waits.append(s)
        now["t"] += s

    api = FakeAPI({s: av_equity(SPY) for s in ("A", "B", "C")})
    c = client(tmp_path, api, rpm=60, sleep=sleep, clock=lambda: now["t"])
    for s in ("A", "B", "C"):
        c.daily("equity", s)
    assert waits == [pytest.approx(1.0), pytest.approx(1.0)]


def test_no_key_no_request(tmp_path):
    c = AlphaVantage(str(tmp_path), SESSION, key="", get=FakeAPI({}))
    with pytest.raises(FetchError, match="ALPHAVANTAGE_API_KEY"):
        c.daily("equity", "SPY")


def test_last_close_session_waits_for_the_new_york_close():
    utc = _dt.timezone.utc
    assert last_close_session(_dt.datetime(2026, 11, 18, 20, 0, tzinfo=utc)) == "2026-11-17"   # 15:00 NY
    assert last_close_session(_dt.datetime(2026, 11, 18, 22, 0, tzinfo=utc)) == "2026-11-18"   # 17:00 NY
    assert last_close_session(_dt.datetime(2026, 11, 22, 12, 0, tzinfo=utc)) == "2026-11-20"   # Sunday


# -- the provider --------------------------------------------------------------------------------
def test_a_stale_worker_stops_the_run_and_nothing_is_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", KEY)
    meta = MetaPortfolio([Sleeve("s1", 1.0, path=str(tmp_path / "strategy.py"))], fresh_prices="alphavantage",
                         stale_symbol_policy="block")           # strict: the worker stops on a stale symbol
    (tmp_path / "strategy.py").write_text("")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        out = cmd[cmd.index("-m") + 3]
        with open(out, "w") as fh:
            json.dump({"books": [], "error": "StalePrices: ...", "stale": {"ZZZ": "unknown to AlphaVantage"},
                       "fresh": {"filled": 0}}, fh)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tradewind.live.daily.providers.subprocess.run", fake_run)
    provider = PwbProvider(meta, str(tmp_path / "run" / "books"), log=lambda *a: None)
    with pytest.raises(StaleData, match="ZZZ"):
        provider.books()
    assert seen["cmd"][seen["cmd"].index("--fresh-prices") + 1] == str(tmp_path / "run" / "prices")
    assert not any(f.endswith(".json") for _, _, fs in os.walk(tmp_path / "run" / "books") for f in fs)


def test_fresh_prices_need_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    meta = MetaPortfolio([Sleeve("s1", 1.0)])
    with pytest.raises(RuntimeError, match="ALPHAVANTAGE_API_KEY"):
        PwbProvider(meta, str(tmp_path / "books"), fresh_prices="alphavantage")
    with pytest.raises(ValueError):
        PwbProvider(meta, str(tmp_path / "books"), fresh_prices="yahoo")
    PwbProvider(meta, str(tmp_path / "books"))                  # off by default: no key needed
