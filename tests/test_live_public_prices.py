"""Public daily series for the signals (tradewind.live.daily.public_prices), without the network:
the FRED answers are built here in ``fredgraph.csv``'s layout.

What is checked: the csv is read with its missing values dropped; a 10-year yield is turned
into the catalogue's price index and chained on it with an exact junction (the history is
kept, the new values are the public ones times the median factor); two series that are not
the same are refused; one bad day keeps the splice but raises an alert; through
``FreshPrices``, US10Y and VIX are continued, a value published the next day leaves the
symbol stale with the cause ``lagged``, a country without a public daily series says so, a
FRED that cannot be read falls back on the other sources; the session turns the alert into
``public_series_gap``; answers are cached per session.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import types

import pandas as pd
import pytest

from tradewind.live.daily.fresh import FreshPrices
from tradewind.live.daily.public_prices import (
    PUBLIC_SERIES,
    FredClient,
    parse_csv,
    splice_public,
)
from tradewind.live.daily.session import prices_line

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_live_fresh import DAYS, SESSION, SPY, av_equity, price_path
from test_live_stale import API, client

US10Y = PUBLIC_SERIES[("Bonds-Daily-Price", "US10Y")]
VIX = PUBLIC_SERIES[("Indices-Daily-Price", "VIX")]


def yields(days, start=4.5, seed=7) -> dict[str, float]:
    p = price_path(days, start, seed)
    return {d: round(v, 2) for d, v in p.items()}


def fred_csv(series: str, values: dict[str, float], missing=()) -> str:
    lines = [f"observation_date,{series}"]
    for d, v in sorted(values.items()):
        lines.append(f"{d}," + ("" if d in missing else f"{v}"))
    return "\n".join(lines) + "\n"


def bond_rows(symbol: str, ylds: dict[str, float], until: str, basis_bp: float = 0.0) -> pd.DataFrame:
    """The catalogue's bond price index from yields (plus a constant basis in bp)."""
    days = [d for d in ylds if d <= until]
    px = [100.0 / (1 + (ylds[d] + basis_bp / 100) / 100) ** 10 for d in days]
    return pd.DataFrame({"symbol": symbol, "date": [_dt.date.fromisoformat(d) for d in days], "open": px,
                         "high": px, "low": px, "close": px, "volume": 0})


class Fred:
    """``url -> text`` for fredgraph.csv: the answers by series id, every URL kept."""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.urls: list[str] = []

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        series = url.split("id=", 1)[1].split("&", 1)[0]
        if series not in self.answers:
            raise OSError("HTTP Error 404")
        return self.answers[series]


def fresh_with(tmp_path, fred: Fred, av: dict | None = None, **kw) -> FreshPrices:
    return FreshPrices(client(tmp_path, API(av or {"SPY": av_equity(SPY)})), SESSION, log=lambda *a: None,
                       strict=False, fred=FredClient(str(tmp_path / "prices"), SESSION, get=fred,
                                                     sleep=lambda s: None, log=lambda *a: None), **kw)


# -- the csv and the splice ------------------------------------------------------------------------------
def test_the_csv_drops_missing_values():
    text = "observation_date,DGS10\n2026-11-19,5.04\n2026-11-20,.\n2026-11-23,\n2026-11-24,4.92\n"
    f = parse_csv(text)
    assert f["date"].tolist() == ["2026-11-19", "2026-11-24"] and f["value"].tolist() == [5.04, 4.92]
    with pytest.raises(ValueError):
        parse_csv("<!DOCTYPE html><html>")


def test_a_ten_year_yield_continues_the_price_index_with_an_exact_junction():
    y = yields(DAYS)
    rows = bond_rows("US10Y", y, "2026-10-30", basis_bp=1.0)          # a constant 1 bp basis
    pub = parse_csv(fred_csv("DGS10", y))
    out, done = splice_public(rows, pub, US10Y, SESSION)
    s = out.set_index(out["date"].astype(str))["close"]
    assert done.status == "filled" and done.anchor == "2026-10-29" and s.index.max() == SESSION
    assert done.offset < 0.002 and done.median_gap < 1e-3 and not done.alert
    assert done.yield_gap_bp["median"] == pytest.approx(1.0, abs=0.05)
    hist = rows.set_index(rows["date"].astype(str))["close"]
    assert s["2026-10-29"] == hist["2026-10-29"] and s["2026-10-01"] == hist["2026-10-01"]   # history kept
    k = done.factor
    assert s[SESSION] == pytest.approx(k * 100 / (1 + y[SESSION] / 100) ** 10, rel=1e-12)
    assert s["2026-10-30"] == pytest.approx(k * 100 / (1 + y["2026-10-30"] / 100) ** 10)   # last bar replaced
    assert out["volume"].dtype == rows["volume"].dtype and (out["open"] == out["close"]).all()


def test_two_series_that_are_not_the_same_are_refused():
    y = yields(DAYS)
    other = {d: v + 1.0 for d, v in y.items()}                         # 100 bp away: another series
    out, done = splice_public(bond_rows("US10Y", y, "2026-10-30"), parse_csv(fred_csv("DGS10", other)),
                              US10Y, SESSION)
    assert done.status == "refused" and "disagree" in done.reason and out["date"].max() == _dt.date(2026, 10, 30)
    vix = price_path(DAYS, 18.0, 3)
    noisy = {d: v * (1.02 if i % 2 else 0.98) for i, (d, v) in enumerate(vix.items())}   # 2 % every day
    rows = pd.DataFrame({"symbol": "VIX", "date": [_dt.date.fromisoformat(d) for d in vix if d <= "2026-10-21"],
                         "close": [v for d, v in vix.items() if d <= "2026-10-21"]})
    _, done = splice_public(rows, parse_csv(fred_csv("VIXCLS", noisy)), VIX, SESSION)
    assert done.status == "refused" and "median gap" in done.reason


def test_one_bad_day_is_spliced_with_an_alert():
    vix = price_path(DAYS, 18.0, 3)
    pub = dict(vix)
    pub["2026-10-15"] = vix["2026-10-15"] * 0.82                       # one day off by 18 %
    rows = pd.DataFrame({"symbol": "VIX", "date": [_dt.date.fromisoformat(d) for d in vix if d <= "2026-10-21"],
                         "close": [v for d, v in vix.items() if d <= "2026-10-21"]})
    out, done = splice_public(rows, parse_csv(fred_csv("VIXCLS", pub)), VIX, SESSION)
    assert done.status == "filled" and done.factor == pytest.approx(1.0) and done.median_gap == pytest.approx(0.0)
    assert "2026-10-15" in done.alert and done.max_gap == pytest.approx(1 / 0.82 - 1)
    s = out.set_index(out["date"].astype(str))["close"]
    assert s[SESSION] == pytest.approx(vix[SESSION])                    # the same level, not rescaled


# -- through FreshPrices -----------------------------------------------------------------------------------
def test_bonds_and_vix_are_continued_and_a_next_day_value_is_lagged(tmp_path):
    y = yields(DAYS)
    vix = price_path(DAYS, 18.0, 3)
    lag = {d: v for d, v in y.items() if d < SESSION}                   # tomorrow's publication
    fred = Fred({"DGS10": fred_csv("DGS10", lag), "VIXCLS": fred_csv("VIXCLS", vix)})
    f = fresh_with(tmp_path, fred)
    bonds = pd.concat([bond_rows("US10Y", y, "2026-10-30"), bond_rows("DE10Y", yields(DAYS, 2.6, 8), "2026-10-30")])
    out = f.extend("Bonds-Daily-Price", bonds)
    last = out.groupby("symbol")["date"].max().astype(str)
    assert last["US10Y"] == "2026-11-17" and last["DE10Y"] == "2026-10-30"
    assert f.stale_info["US10Y"]["cause"] == "lagged" and "2026-11-17" in f.stale["US10Y"]
    assert f.stale_info["DE10Y"]["cause"] == "no_source" and "only a monthly series" in f.stale["DE10Y"]
    vixrows = pd.DataFrame({"symbol": "VIX", "date": [_dt.date.fromisoformat(d) for d in vix if d <= "2026-10-21"],
                            "close": [v for d, v in vix.items() if d <= "2026-10-21"], "volume": 0})
    out = f.extend("Indices-Daily-Price", vixrows)
    assert str(out["date"].max()) == SESSION and "VIX" not in f.stale
    s = f.summary()
    assert s["served"]["public"] == ["US10Y", "VIX"] and s["public"]["US10Y"]["series"] == "DGS10"
    assert s["public"]["VIX"]["status"] == "filled" and not s["public_alerts"]
    assert sum("DGS10" in u for u in fred.urls) == 1
    g = fresh_with(tmp_path, fred)                                      # another strategy: the cache
    g.extend("Bonds-Daily-Price", bond_rows("US10Y", y, "2026-10-30"))
    assert sum("DGS10" in u for u in fred.urls) == 1 and g.stale_info["US10Y"]["cause"] == "lagged"


def test_a_fred_that_cannot_be_read_falls_back_on_the_proxy(tmp_path):
    vix = price_path(DAYS, 18.0, 3)
    vixy = price_path(DAYS, 40.0, 31)
    f = fresh_with(tmp_path, Fred({}), av={"SPY": av_equity(SPY), "VIXY": av_equity(vixy)},
                   proxies={"VIX": "VIXY"}, approximate={"VIX"})
    rows = pd.DataFrame({"symbol": "VIX", "date": [_dt.date.fromisoformat(d) for d in vix if d <= "2026-10-21"],
                         "close": [v for d, v in vix.items() if d <= "2026-10-21"]})
    f.extend("Indices-Daily-Price", rows)
    assert f.splices["VIX"].source == "VIXY" and "VIX" not in f.public
    h = FreshPrices(client(tmp_path, API({"SPY": av_equity(SPY)})), SESSION, log=lambda *a: None, strict=False)
    h.extend("Bonds-Daily-Price", bond_rows("US10Y", yields(DAYS), "2026-10-30"))     # no FRED client: as before
    assert h.stale_info["US10Y"]["reason"] == "no fresh source for this dataset"


def test_the_session_raises_the_public_alert(tmp_path):
    from tradewind.live.daily.providers import PwbProvider

    provider = PwbProvider.__new__(PwbProvider)
    provider.fresh = {"a": {"served": {"public": ["VIX"]}, "public": {"VIX": {"series": "VIXCLS", "status": "filled",
                                                                               "last_after": SESSION,
                                                                               "median_gap": 0.0}},
                            "public_alerts": {"VIX": "VIX against FRED VIXCLS: gap up to 21.4% on 2026-10-15"}}}
    provider.fresh_sources, provider.ib_stats, provider.kraken_stats = ("ibkr",), {}, {}
    report = provider.price_report()
    assert report["public"] == 1 and report["public_alerts"]["VIX"].startswith("VIX against FRED")
    assert "public 1" in prices_line(report) and "VIX (VIXCLS, filled" in prices_line(report)

    from test_live_daily import UNTIL, _meta, _provider
    from test_live_stale import WithStale, session

    from tradewind.calendars import NYSE
    from tradewind.live.daily import ReplayProvider

    replay = ReplayProvider(_provider(), UNTIL, 4)
    d = [x for x in replay.dates() if NYSE.is_trading_day(x)][-1]
    wrapped = WithStale(replay, {}, {})
    wrapped.fresh_prices = "ibkr"
    wrapped.price_report = lambda: report
    out = session(tmp_path, wrapped, _meta(), d)
    alerts = {a["code"]: a["message"] for a in out.report["alerts"]}
    assert alerts["public_series_gap"] == report["public_alerts"]["VIX"] and out.code == 4
    with open(out.report_md) as fh:
        md = fh.read()
    assert "signals from FRED: VIX (VIXCLS" in md and "without the account's currency" in md


def test_the_client_asks_again_when_the_cache_is_short_of_the_session(tmp_path):
    y = yields(DAYS)
    fred = Fred({"DGS10": fred_csv("DGS10", {d: v for d, v in y.items() if d < SESSION})})
    clock = types.SimpleNamespace(t=1e9)
    c = FredClient(str(tmp_path), SESSION, get=fred, wall=lambda: clock.t, sleep=lambda s: None)
    c.daily("DGS10", "2026-07-01")
    assert len(fred.urls) == 1
    path = os.path.join(str(tmp_path), SESSION, "public", "DGS10.csv")
    os.utime(path, (clock.t, clock.t))
    c.daily("DGS10", "2026-07-01")
    assert len(fred.urls) == 1                                          # fresh enough
    clock.t += 7200
    fred.answers["DGS10"] = fred_csv("DGS10", y)
    assert c.daily("DGS10", "2026-07-01")["date"].iloc[-1] == SESSION and len(fred.urls) == 2
    clock.t += 7200
    c.daily("DGS10", "2026-07-01")
    assert len(fred.urls) == 2                                          # reaches the session: kept
