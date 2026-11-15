"""Intraday (spec/06 § 2-4): consolidators, the consolidated feed on the engine's clock, the
bounded minute loader, and splits on an unadjusted minute stream."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.consolidators import (
    TimeBarConsolidator,
    bin_ends,
    consolidate,
    consolidate_count,
)
from tradewind.intraday import (
    adjust_for_splits,
    detect_splits,
    load_minute_bars,
    read_splits,
    split_actions,
    split_factors,
)
from tradewind.models import StaticCorporateActions

NY = "America/New_York"
RTH = (dt.time(9, 30), dt.time(16, 0))


class MiniHours:
    """A minimal calendar implementing spec/05 § 6.1's ``ExchangeHours`` for the tests: fixed
    hours in one zone, a holiday list and early closes. The real calendars come with the
    calendar work (``feat/calendar-and-fingerprint``)."""

    def __init__(self, tz=NY, open_=dt.time(9, 30), close=dt.time(16, 0), holidays=(), early=None):
        self.tz = tz
        self._zone = ZoneInfo(tz)
        self.open_, self.close = open_, close
        self.holidays = {pd.Timestamp(h).date() for h in holidays}
        self.early = {pd.Timestamp(k).date(): v for k, v in (early or {}).items()}

    def is_trading_day(self, day) -> bool:
        d = pd.Timestamp(day).date()
        return d.weekday() < 5 and d not in self.holidays

    def session(self, day):
        d = pd.Timestamp(day).date()
        if not self.is_trading_day(d):
            return None
        close = self.early.get(d, self.close)

        def ns(t):
            return np.int64(int(dt.datetime.combine(d, t, self._zone).timestamp()) * 1_000_000_000)

        return ns(self.open_), ns(close)

    def is_open(self, time) -> bool:
        t = pd.Timestamp(int(time), tz="UTC")
        s = self.session(t.tz_convert(self.tz).date())
        return s is not None and s[0] < int(time) <= s[1]


def minute_index(days, start="04:01", end="20:00", tz=NY):
    idx = []
    for d in days:
        idx += list(pd.date_range(f"{d} {start}", f"{d} {end}", freq="1min", tz=tz))
    return pd.DatetimeIndex(idx).tz_convert("UTC")


def minute_frame(days, seed=0, drop=0.0, **kw):
    idx = minute_index(days, **kw)
    rng = np.random.default_rng(seed)
    if drop:
        idx = idx[rng.random(len(idx)) > drop]
    n = len(idx)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 5e-4, n)))
    openp = close * (1 + rng.normal(0, 1e-4, n))
    return pd.DataFrame({"open": openp, "high": np.maximum(openp, close) * 1.0002,
                         "low": np.minimum(openp, close) * 0.9998, "close": close,
                         "volume": rng.integers(1, 1000, n).astype(float)}, index=idx)


def _cols(frame):
    return {k: frame[k].to_numpy() for k in ("open", "high", "low", "close", "volume")}


def _ny(ts):
    return pd.DatetimeIndex(np.asarray(ts).astype("datetime64[ns]")).tz_localize("UTC").tz_convert(NY)


# -- consolidators -----------------------------------------------------------------------------------


def test_five_minute_bars_anchored_at_the_open():
    f = minute_frame(["2024-03-08"])
    b = consolidate(f.index.as_unit("ns").asi8, _cols(f), "5min", NY, RTH)
    stamps = _ny(b.timestamps)
    assert len(b.timestamps) == 78 and (b.count == 5).all()
    assert stamps[0].strftime("%H:%M") == "09:35" and stamps[-1].strftime("%H:%M") == "16:00"
    # the first bar is the minutes ending 09:31 ... 09:35
    first = f.loc[(f.index > pd.Timestamp("2024-03-08 09:30", tz=NY)) &
                  (f.index <= pd.Timestamp("2024-03-08 09:35", tz=NY))]
    assert b.open[0] == first.open.iloc[0] and b.close[0] == first.close.iloc[-1]
    assert b.high[0] == first.high.max() and b.low[0] == first.low.min()
    assert b.volume[0] == first.volume.sum()


def test_hourly_bars_clip_at_the_close_and_daily_at_the_session():
    f = minute_frame(["2024-03-08", "2024-03-11"])        # the DST change is on the Sunday between
    h = consolidate(f.index.as_unit("ns").asi8, _cols(f), "1h", NY, RTH)
    assert _ny(h.timestamps).strftime("%d %H:%M").tolist()[:7] == [
        "08 10:30", "08 11:30", "08 12:30", "08 13:30", "08 14:30", "08 15:30", "08 16:00"]
    assert h.count.tolist()[:7] == [60] * 6 + [30]
    d = consolidate(f.index.as_unit("ns").asi8, _cols(f), "D", NY, RTH)
    # 16:00 New York is 21:00 UTC in winter time, 20:00 UTC after the change
    assert pd.DatetimeIndex(d.timestamps.astype("datetime64[ns]")).strftime("%m-%d %H:%M").tolist() == [
        "03-08 21:00", "03-11 20:00"]
    assert d.count.tolist() == [390, 390]


def test_midnight_anchor_keeps_extended_hours():
    f = minute_frame(["2024-03-08"])
    b = consolidate(f.index.as_unit("ns").asi8, _cols(f), "1h", NY)
    stamps = _ny(b.timestamps)
    assert stamps[0].strftime("%H:%M") == "05:00" and b.count[0] == 60
    assert stamps[-1].strftime("%H:%M") == "20:00"
    assert b.count.sum() == len(f)


def test_hours_calendar_early_close_and_holiday():
    days = ["2024-11-28", "2024-11-29", "2024-12-02"]    # Thanksgiving, its early close, Monday
    f = minute_frame(days)
    hours = MiniHours(holidays=["2024-11-28"], early={"2024-11-29": dt.time(13, 0)})
    d = consolidate(f.index.as_unit("ns").asi8, _cols(f), "D", hours=hours)
    assert _ny(d.timestamps).strftime("%m-%d %H:%M").tolist() == ["11-29 13:00", "12-02 16:00"]
    assert d.count.tolist() == [210, 390]
    h = consolidate(f.index.as_unit("ns").asi8, _cols(f), "1h", hours=hours)
    assert _ny(h.timestamps).strftime("%m-%d %H:%M").tolist()[:4] == [
        "11-29 10:30", "11-29 11:30", "11-29 12:30", "11-29 13:00"]


def test_shipped_nyse_calendar_drives_the_bins():
    from tradewind.calendars import NYSE

    days = ["2024-11-27", "2024-11-28", "2024-11-29"]    # Thanksgiving, then its early close
    f = minute_frame(days)
    d = consolidate(f.index.as_unit("ns").asi8, _cols(f), "D", hours=NYSE)
    assert _ny(d.timestamps).strftime("%m-%d %H:%M").tolist() == ["11-27 16:00", "11-29 13:00"]
    h = consolidate(f.index.as_unit("ns").asi8, _cols(f), "1h", hours="NYSE")
    assert _ny(h.timestamps).strftime("%m-%d %H:%M").tolist()[-1] == "11-29 13:00"


def test_streaming_equals_vectorised():
    f = minute_frame(["2024-03-08", "2024-03-11"], seed=3, drop=0.3)
    t = f.index.as_unit("ns").asi8
    for period, session in (("5min", RTH), ("1h", RTH), ("D", RTH), ("15min", None), ("1h", None)):
        want = consolidate(t, _cols(f), period, NY, session)
        cons = TimeBarConsolidator(period, NY, session)
        got = []
        for i, ts in enumerate(t):
            got += cons.update(ts, f.open.iloc[i], f.high.iloc[i], f.low.iloc[i], f.close.iloc[i],
                               f.volume.iloc[i])
            got += cons.scan(ts)                       # then the clock's time (a no-op here)
            got += cons.scan(ts + 59_000_000_000)      # a pulse between two minutes
        got += cons.scan(t[-1] + 86_400_000_000_000)
        assert [g.time for g in got] == want.timestamps.tolist(), period
        for k in ("open", "high", "low", "close", "volume", "count"):
            assert np.array_equal([getattr(g, k) for g in got], getattr(want, k)), (period, k)


def test_count_consolidation_and_bin_ends_outside_session():
    f = minute_frame(["2024-03-08"])
    b = consolidate_count(f.index.as_unit("ns").asi8, _cols(f), 7)
    assert len(b.timestamps) == len(f) // 7 and b.timestamps[0] == f.index.as_unit("ns").asi8[6]
    ends = bin_ends(f.index.as_unit("ns").asi8, "5min", NY, RTH)
    assert (ends[: 5 * 60 + 29] == -1).all()            # 04:01 ... 09:29 end before the open


# -- the consolidated feed on the clock ---------------------------------------------------------------


def test_consolidated_feed_sees_only_completed_bins():
    f = minute_frame(["2024-03-08", "2024-03-11"], seed=5, start="09:31", end="16:00")
    reads = []

    class Probe(tw.Strategy):
        def next(self):
            now = pd.Timestamp(self.datas[0].datetime.datetime(0), tz="UTC")
            reads.append((now, self.datas[1].close[0], len(self.datas[1])))

    engine = tw.Engine()
    minute = engine.adddata(tw.feeds.PandasData(dataname=f), name="M")
    five = engine.consolidate(minute, "5min", tz=NY, session=RTH)
    assert five._name == "M_5min"
    engine.addstrategy(Probe)
    engine.run()
    # every read is the close of the last minute at or before the latest 5-minute boundary
    ends = consolidate(f.index.as_unit("ns").asi8, _cols(f), "5min", NY, RTH)
    stamps = pd.DatetimeIndex(ends.timestamps.astype("datetime64[ns]")).tz_localize("UTC")
    checked = 0
    for now, close5, n in reads:
        k = stamps.searchsorted(now, side="right") - 1
        assert n == k + 1
        assert close5 == ends.close[k]
        assert close5 == f.close[f.index <= stamps[k]].iloc[-1]
        checked += 1
    assert checked == len(f) - 4                          # next starts once the first bin closed


def test_consolidate_is_bit_identical_to_a_prebuilt_feed():
    f = minute_frame(["2024-03-08"], seed=9, start="09:31", end="16:00")

    class Buy(tw.Strategy):
        def next(self):
            if len(self.datas[-1]) == 3 and not self.position:
                self.buy(self.datas[-1], size=10)

    def run(prebuilt):
        engine = tw.Engine()
        m = engine.adddata(tw.feeds.PandasData(dataname=f), name="M")
        if prebuilt:
            from tradewind.consolidators import frame_from_bars
            b = consolidate(f.index.as_unit("ns").asi8, _cols(f), "5min", NY, RTH)
            engine.adddata(tw.feeds.PandasData(dataname=frame_from_bars(b)), name="M5")
        else:
            engine.consolidate(m, "5min", name="M5", tz=NY, session=RTH)
        engine.addstrategy(Buy)
        engine.run()
        return engine.broker.getvalue()

    assert run(True) == run(False)


# -- the bounded loader ----------------------------------------------------------------------------------


def _write_months(tmp_path):
    """Two monthly files in the PWB layout: start-stamped naive New York strings, 04:00-19:59."""
    pytest.importorskip("pyarrow")
    paths = {}
    for month, days in (("2024-05", ["2024-05-30", "2024-05-31"]), ("2024-06", ["2024-06-03", "2024-06-04"])):
        parts = []
        for sym, seed in (("AAA", 1), ("BBB", 2)):
            f = minute_frame(days, seed=seed, start="04:00", end="19:59")
            local = f.index.tz_convert(NY).tz_localize(None)
            parts.append(pd.DataFrame({"symbol": sym, "datetime": local.strftime("%Y-%m-%d %H:%M:%S"),
                                       "adj_open": f.open, "adj_high": f.high, "adj_low": f.low,
                                       "adj_close": f.close, "volume": f.volume.astype("int64")}))
        p = tmp_path / f"train-{month}.parquet"
        pd.concat(parts, ignore_index=True).to_parquet(p, index=False)
        paths[month] = str(p)
    return paths


def test_loader_reads_only_the_window(tmp_path):
    paths = _write_months(tmp_path)
    opened = []

    def resolve(month):
        opened.append(month)
        return paths.get(month)

    out = load_minute_bars(resolve, ["AAA"], "2024-06-03", "2024-06-03", tz=NY, session=RTH)
    assert opened == ["2024-06"]
    f = out["AAA"]
    local = f.index.tz_convert(NY)
    assert len(f) == 390
    assert local[0].strftime("%H:%M") == "09:31" and local[-1].strftime("%H:%M") == "16:00"  # end-stamped
    assert f.index.tz is not None and str(f.index.tz) == "UTC"
    # a window across the month boundary opens both months, extended hours kept
    opened.clear()
    out = load_minute_bars(resolve, ["AAA", "BBB"], "2024-05-31", "2024-06-03", tz=NY)
    assert opened == ["2024-05", "2024-06"]
    assert len(out["AAA"]) == len(out["BBB"]) == 2 * 960
    with pytest.raises(MemoryError):
        load_minute_bars(resolve, ["AAA", "BBB"], "2024-05-01", "2024-06-30", tz=NY, max_rows=1000)


def test_loaded_bars_run_on_the_engine(tmp_path):
    paths = _write_months(tmp_path)
    out = load_minute_bars(paths, ["AAA"], "2024-06-03", "2024-06-04", tz=NY, session=RTH)
    engine = tw.Engine()
    m = engine.adddata(tw.feeds.PandasData(dataname=out["AAA"]), name="AAA")
    d = engine.consolidate(m, "D", tz=NY, session=RTH)
    engine.addstrategy(tw.Strategy)
    engine.run()
    assert len(d) == 2 and d.close[0] == out["AAA"].close.iloc[-1]


# -- splits ------------------------------------------------------------------------------------------


def _split_table():
    return pd.DataFrame({"symbol": ["NVX", "NVX", "OTH"], "date": ["2024-06-10", "2024-07-01", "2024-06-10"],
                         "split_from": [1.0, 8.0, 1.0], "split_to": [10.0, 1.0, 2.0]})


def _split_minutes():
    """Raw minute prices: 10-for-1 on 2024-06-10, then 1-for-8 on 2024-07-01."""
    days = ["2024-06-07", "2024-06-10", "2024-06-28", "2024-07-01"]
    f = minute_frame(days, seed=4, start="09:31", end="16:00")
    local_day = f.index.tz_convert(NY).normalize().tz_localize(None)
    raw = np.where(local_day < pd.Timestamp("2024-06-10"), 10.0, 1.0)
    raw = raw * np.where(local_day < pd.Timestamp("2024-07-01"), 1.0, 8.0)
    out = f.copy()
    for c in ("open", "high", "low", "close"):
        out[c] = f[c] * raw
    out["volume"] = f["volume"] / raw
    return f, out           # f: a continuous series, out: its raw quotes


def test_read_splits_and_backward_adjustment():
    s = read_splits(_split_table(), "NVX")
    assert s["ratio"].tolist() == [10.0, 0.125]
    adjusted, raw = _split_minutes()
    adj = adjust_for_splits(raw, s, NY)
    # the adjusted series is the raw one in the last shares: equal to ``adjusted`` x 8
    assert np.allclose(adj["close"].to_numpy(), adjusted["close"].to_numpy() * 8)
    assert np.allclose(adj["volume"].to_numpy(), adjusted["volume"].to_numpy() / 8)
    f = split_factors(raw.index, s, NY)
    assert set(np.round(f, 6)) == {1.25, 0.125, 1.0}
    found = detect_splits(raw, NY)
    assert found["ex_date"].dt.strftime("%Y-%m-%d").tolist() == ["2024-06-10", "2024-07-01"]
    assert found["ratio"].tolist() == [10.0, 0.125]


def test_split_actions_scale_the_position_on_raw_prices():
    _, raw = _split_minutes()
    s = read_splits(_split_table(), "NVX")
    values = []

    class Hold(tw.Strategy):
        def next(self):
            if len(self) == 1:
                self.buy(size=10)
                self.sell(exectype=tw.Order.Limit, price=raw.close.iloc[0] * 2, size=10)
            values.append((self.data.datetime.datetime(0), self.broker.getvalue(),
                           self.getposition(self.data).size))

    engine = tw.Engine()
    data = engine.adddata(tw.feeds.PandasData(dataname=raw), name="NVX")
    actions = split_actions(data, s, NY)
    assert [a.ratio for a in actions] == [10.0, 0.125]
    first_day2 = raw.index[raw.index.tz_convert(NY).normalize() == pd.Timestamp("2024-06-10", tz=NY)][0]
    assert int(actions[0].time) == first_day2.value
    engine.set_corporate_actions(StaticCorporateActions(actions))
    engine.addstrategy(Hold)
    engine.broker.setcash(1_000_000)
    engine.run()
    sizes = [v[2] for v in values]
    assert sizes[5] == 10 and 100 in sizes and sizes[-1] == 12.5
    # the value does not jump on the split days: largest bar-to-bar move stays a price move
    v = np.array([x[1] for x in values])
    assert np.max(np.abs(np.diff(v) / v[:-1])) < 0.01
    # the open limit order was rescaled with the position (size x 10 / 8, price / 10 x 8)
    order = next(o for o in engine.broker.orders if o.exectype == tw.Order.Limit)
    assert order.size == pytest.approx(-12.5) and order.price == pytest.approx(raw.close.iloc[0] * 2 / 1.25)
