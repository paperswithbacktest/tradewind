"""Exchange calendars, the exchange clock and timers in exchange time (spec/05 § 6.1)."""

import datetime as dt

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.calendars import CME, CRYPTO, NASDAQ, NYSE, easter, get_calendar, nth_weekday
from tradewind.clock import ALWAYS_OPEN, ExchangeClock, ExchangeHours, UnionClock

D = dt.date


def test_date_rules():
    assert easter(2024) == D(2024, 3, 31) and easter(2025) == D(2025, 4, 20) and easter(2000) == D(2000, 4, 23)
    assert nth_weekday(2024, 11, 3, 4) == D(2024, 11, 28)     # Thanksgiving
    assert nth_weekday(2024, 5, 0, -1) == D(2024, 5, 27)      # Memorial Day


@pytest.mark.parametrize("year, holidays", [
    (2024, ["01-01", "01-15", "02-19", "03-29", "05-27", "06-19", "07-04", "09-02", "11-28", "12-25"]),
    (2025, ["01-01", "01-09", "01-20", "02-17", "04-18", "05-26", "06-19", "07-04", "09-01", "11-27",
            "12-25"]),
    (2026, ["01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03", "09-07", "11-26", "12-25"]),
    (2022, ["01-17", "02-21", "04-15", "05-30", "06-20", "07-04", "09-05", "11-24", "12-26"]),
])
def test_nyse_holidays(year, holidays):
    assert sorted(NYSE.holidays(year)) == [D.fromisoformat(f"{year}-{h}") for h in holidays]


@pytest.mark.parametrize("year, sessions", [
    (2001, 248), (2012, 250), (2021, 252), (2022, 251), (2023, 250), (2024, 252), (2025, 250),
])
def test_nyse_session_counts(year, sessions):
    assert len(NYSE.trading_days(f"{year}-01-01", f"{year}-12-31")) == sessions


def test_nyse_new_year_on_saturday_is_not_observed():
    assert NYSE.is_trading_day("2021-12-31") and not NYSE.is_trading_day("2022-01-01")


def test_nyse_early_closes_and_time_zone():
    assert sorted(NYSE.early_closes(2024)) == [D(2024, 7, 3), D(2024, 11, 29), D(2024, 12, 24)]
    assert sorted(NYSE.early_closes(2026)) == [D(2026, 11, 27), D(2026, 12, 24)]  # July 4 on a Saturday
    o, c = NYSE.session("2024-11-29")
    assert NYSE.local(o) == dt.datetime(2024, 11, 29, 9, 30) and NYSE.local(c) == dt.datetime(2024, 11, 29, 13, 0)
    # 09:30 New York is 14:30 UTC in winter, 13:30 UTC in summer
    assert np.datetime64(int(NYSE.session("2024-01-03")[0]), "ns") == np.datetime64("2024-01-03T14:30")
    assert np.datetime64(int(NYSE.session("2024-07-03")[0]), "ns") == np.datetime64("2024-07-03T13:30")
    assert NYSE.session("2024-12-25") is None and NYSE.session("2024-12-28") is None


def test_is_open():
    assert NYSE.is_open(np.datetime64("2024-11-29T17:59"))          # 12:59 New York
    assert not NYSE.is_open(np.datetime64("2024-11-29T18:00"))      # the early close
    assert not NYSE.is_open(np.datetime64("2024-11-28T16:00"))      # Thanksgiving
    assert NYSE.is_open(dt.datetime(2024, 3, 11, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=-4))))
    assert CRYPTO.is_open(np.datetime64("2024-12-25T03:00")) and CRYPTO.is_trading_day("2024-12-28")
    with pytest.raises(ValueError):
        NYSE.is_open(dt.datetime(2024, 3, 11, 10, 0))   # naive: ambiguous


def test_nasdaq_follows_nyse():
    days = ("2000-01-01", "2026-12-31")
    assert np.array_equal(NASDAQ.trading_days(*days), NYSE.trading_days(*days))


def test_cme_sessions():
    # trade date D runs from 17:00 Chicago the day before to 16:00 on D
    o, c = CME.session("2024-03-05")
    assert CME.local(o) == dt.datetime(2024, 3, 4, 17, 0) and CME.local(c) == dt.datetime(2024, 3, 5, 16, 0)
    o, _ = CME.session("2024-03-04")                     # Monday opens on Sunday evening
    assert CME.local(o) == dt.datetime(2024, 3, 3, 17, 0)
    assert CME.local(CME.session("2024-01-15")[1]) == dt.datetime(2024, 1, 15, 12, 0)    # MLK halt
    assert CME.local(CME.session("2024-11-29")[1]) == dt.datetime(2024, 11, 29, 12, 15)  # day after Thanksgiving
    assert CME.session("2024-03-29") is None and CME.session("2024-12-25") is None   # Good Friday, Christmas
    assert not CME.is_open(np.datetime64("2024-03-05T22:30"))   # 16:30 Chicago: the maintenance hour
    assert CME.is_open(np.datetime64("2024-03-05T23:30"))       # 17:30 Chicago: the next trade date


def test_bar_times():
    t = NYSE.bar_times("2024-11-29", "2024-11-29", "1min")
    assert len(t) == 210 and NYSE.local(t[0]) == dt.datetime(2024, 11, 29, 9, 31)
    assert NYSE.local(t[-1]) == dt.datetime(2024, 11, 29, 13, 0)
    assert len(NYSE.bar_times("2024-11-25", "2024-11-29", "1min")) == 390 * 3 + 210
    assert len(CRYPTO.bar_times("2024-01-01", "2024-01-01", "1h")) == 24
    assert len(NYSE.bar_times("2024-11-25", "2024-11-25", "7min")) == 56   # 55 full bars and a last one


def test_protocol_and_registry():
    for hours in (NYSE, CME, CRYPTO, ALWAYS_OPEN):
        assert isinstance(hours, ExchangeHours)
    assert get_calendar("nyse") is NYSE
    with pytest.raises(KeyError):
        get_calendar("LSE")


# -- the clock -------------------------------------------------------------------------------
def minute_frame(times_ns, start=100.0):
    idx = pd.to_datetime(times_ns, utc=True).tz_localize(None)
    close = start + np.arange(len(idx)) * 0.01
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0}, index=idx)


class TimerLog(tw.Strategy):
    params = (("timers", ()),)

    def __init__(self):
        self.log, self.bars = [], 0
        for kwargs in self.p.timers:
            self.add_timer(**kwargs)

    def notify_timer(self, timer, when, *args, **kwargs):
        self.log.append((timer.tid, when))

    def next(self):
        self.bars += 1


def run_minutes(frame, clock, timers=()):
    engine = tw.Engine()
    engine.adddata(tw.feeds.PandasData(dataname=frame), name="X")
    if clock is not None:
        engine.set_clock(clock)
    engine.addstrategy(TimerLog, timers=timers)
    return engine, engine.run()[0]


def test_exchange_clock_fill_forward_and_timers():
    sparse = NYSE.bar_times("2024-11-27", "2024-12-02", "1min")[::7]     # a bar every 7 minutes
    timers = ({"when": tw.Timer.SESSION_START},
              {"when": tw.Timer.SESSION_END, "offset": dt.timedelta(minutes=-5)},
              {"when": dt.time(10, 0)},
              {"when": dt.time(15, 0), "tzdata": "UTC"})
    engine, strat = run_minutes(minute_frame(sparse), ExchangeClock(NYSE, "1min", fill_forward=True), timers)
    assert len(engine.clock) == strat.bars == 390 + 210 + 390 - 2   # every minute, up to the last feed bar
    by_day = {}
    for tid, when in strat.log:
        by_day.setdefault(when.date(), []).append((tid, when.time()))
    assert by_day[D(2024, 11, 29)] == [(0, dt.time(9, 31)), (2, dt.time(10, 0)), (3, dt.time(10, 0)),
                                       (1, dt.time(12, 55))]                      # early close
    assert by_day[D(2024, 11, 27)][-1] == (1, dt.time(15, 55))
    assert D(2024, 11, 28) not in by_day                                          # Thanksgiving
    # without fill-forward the timers fire at the first bar at or after their time
    engine, strat = run_minutes(minute_frame(sparse), ExchangeClock(NYSE), timers[2:3])
    assert strat.bars == len(sparse)
    assert [w.time() for _, w in strat.log] == [dt.time(10, 6), dt.time(10, 1), dt.time(10, 1)]


def test_exchange_timer_repeat_and_monthdays():
    t = NYSE.bar_times("2024-11-27", "2024-12-03", "1min")
    timers = ({"when": dt.time(10, 0), "repeat": dt.timedelta(hours=2)},
              {"when": tw.Timer.SESSION_START, "monthdays": [1]})
    _, strat = run_minutes(minute_frame(t), ExchangeClock(NYSE), timers)
    repeat = [w for tid, w in strat.log if tid == 0]
    assert [w.time() for w in repeat if w.date() == D(2024, 11, 27)] == [dt.time(10), dt.time(12), dt.time(14),
                                                                         dt.time(16)]
    assert [w.time() for w in repeat if w.date() == D(2024, 11, 29)] == [dt.time(10), dt.time(12)]
    monthly = [w for tid, w in strat.log if tid == 1]
    assert monthly == [dt.datetime(2024, 11, 27, 9, 31), dt.datetime(2024, 12, 2, 9, 31)]  # carried to the 2nd


def test_exchange_clock_daily_bars_map_to_their_session():
    idx = pd.bdate_range("2024-11-25", "2024-12-06")
    close = np.linspace(100, 110, len(idx))
    frame = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0}, index=idx)
    _, strat = run_minutes(frame, ExchangeClock(NYSE), ({"when": tw.Timer.SESSION_START, "weekdays": [5]},))
    # Friday 29th, and Friday 6th; Thanksgiving's bar maps to the Wednesday session
    assert [w.date() for _, w in strat.log] == [D(2024, 11, 29), D(2024, 12, 6)]


def test_crypto_clock_runs_every_day():
    t = CRYPTO.bar_times("2024-03-08", "2024-03-11", "1h")[::5]
    engine, strat = run_minutes(minute_frame(t), ExchangeClock(CRYPTO, "1h", fill_forward=True))
    assert strat.bars == len(engine.clock) == 4 * 24   # 01:00 on the 8th to midnight after the 11th


def test_default_clock_is_union_and_always_open():
    idx = pd.bdate_range("2024-01-01", periods=10)
    frame = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    engine, _ = run_minutes(frame, None)
    assert type(engine.clock) is UnionClock and engine.broker.securities[0].hours is ALWAYS_OPEN
    engine.set_hours("NYSE")
    assert engine.broker.securities[0].hours is NYSE
    with pytest.raises(TypeError):
        engine.set_clock(object())
    with pytest.raises(ValueError):
        ExchangeClock(NYSE, fill_forward=True)
