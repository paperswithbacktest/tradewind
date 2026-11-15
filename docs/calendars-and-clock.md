# Calendars and clock

By default a run's clock is the union of its feeds' timestamps, and the engine knows nothing
of exchanges: daily bars carry dates only. That is backtrader's behaviour, and every
strategy written for it runs on it unchanged. Exchange calendars, a clock in the hours of
one place and timers in the exchange's time are **opt-in**.

## The default clock

Before the first bar the engine merges the dates of every feed into one sorted clock; each
entry is a *slice*. A feed with no bar on a slice keeps its previous bar. A date that a
single feed has a bar on is a slice for every feed: a stray holiday row in one symbol adds
a bar to the whole run, so clean a panel before building its feeds. The details are in
[How a backtest runs](concepts.md#the-clock).

## Calendars

Four calendars ship, written from the exchanges' published rules (no data file, no network):

| calendar | hours | notes |
|---|---|---|
| `NYSE` | 09:30-16:00 America/New_York, 13:00 early closes | holidays from 1971 with the special closures since, early closes from 1993 |
| `NASDAQ` | as NYSE | the few historical differences are not encoded |
| `CME` | equity index futures: 17:00 Chicago the day before to 16:00 | closed on New Year's Day, Good Friday and Christmas; halts at 12:00 on the other US holidays; one-off changes not encoded |
| `CRYPTO` | every day, 00:00-24:00 UTC | |

```python
from tradewind.calendars import NYSE, get_calendar

NYSE.is_trading_day("2024-11-28")           # False: Thanksgiving
NYSE.session("2024-11-29")                  # (open, close) in ns UTC: 09:30 to 13:00 New York
NYSE.trading_days("2024-01-01", "2024-12-31")   # 252 sessions, as a datetime64 array
NYSE.holidays(2025), NYSE.early_closes(2025)
NYSE.bar_times("2024-11-29", "2024-11-29", "5min")  # bar ends, 09:35 to 13:00
NYSE.is_open(t), NYSE.local(t)              # t: a timestamp; local() in New York time
get_calendar("nyse") is NYSE                # by name
```

`trading_days` is also the quickest way to clean a daily panel: keep the rows whose date is
a session of the place the instruments trade on.

## A clock in exchange hours

```python
import tradewind as tw
from tradewind.calendars import NYSE

engine = tw.Engine()
engine.set_clock(tw.ExchangeClock(NYSE, interval="1min", fill_forward=True))
engine.set_hours(NYSE)          # data.security.hours: is_open() and friends
```

`ExchangeClock` never drops or moves a bar: it keeps the union of the feeds' timestamps and
adds what the calendar knows.

- With `interval` and `fill_forward=True` it adds one slice at the end of every bar of that
  length in every session, so a strategy steps on minutes no feed traded (the fill-forward
  of Lean's minute resolution).
- Each slice belongs to a session; timers use it to fire in the place's time.
- Bars are expected **stamped at their end**: 09:31 New York is the first NYSE minute bar.
  Feeds whose stamps are all midnight are taken as daily bars, each in the session of its
  date.

For minute data, consolidated bars and the bounded loader, see
[Intraday data](intraday.md).

## Timers in exchange time

Timers are declared in the strategy, as in the compatible API:

```python
import datetime

def __init__(self):
    self.add_timer(when=tw.Timer.SESSION_START, offset=datetime.timedelta(minutes=30))
    self.add_timer(when=datetime.time(15, 45), repeat=datetime.timedelta(minutes=30))

def notify_timer(self, timer, when, *args, **kwargs):
    ...
```

Under an exchange clock, `SESSION_START` fires at the open plus `offset`, `SESSION_END` at
the close plus `offset` (13:00 on an early-close day), `when=datetime.time(15, 45)` at
15:45 New York (or in the zone given as `tzdata`), each once per session at the first
slice at or after that time; `repeat` fires again until the close, and `when` in
`notify_timer` is the exchange's wall-clock time. Without an exchange clock, timers keep
backtrader's daily semantics (`monthdays`, `weekdays`, carry): see
[Strategies: timers](strategies.md#timers).

## What changes, what does not

- Nothing changes until you call `set_clock` or `set_hours`: the oracle NAVs are identical
  with and without the calendar module.
- The clock is recorded in the run's [fingerprint](reproducibility.md): two runs on the same
  data with different clocks have different digests.
- Reference: [Calendars and clock](reference/calendars.md).
