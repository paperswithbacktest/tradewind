"""The trading day of the paper run, as a scheduler starts it: one command, safe to repeat.

    python -m tradewind.live.daily.session --meta meta.yaml --dir ~/pwb-paper --broker sim \\
        --fresh-prices alphavantage [--send] [--notify-cmd "~/bin/notify-me"]

One call does the whole day, in this order, and writes the report of the day:

1. **The session.** The NYSE calendar (``calendars.NYSE``, holidays and early closes
   included, in New York time so daylight saving does not matter) gives the last session whose
   close is at least ``close_margin`` behind. A run before the close works on the previous
   session; a run on a holiday or a weekend finds the last session already done and does
   nothing.
2. **One run per session.** A lock (``daily.lock``) keeps two runs apart, and a marker per
   session (``sessions/SESSION.json``) says how far the day went. A session already proposed
   (or sent) is not run again: the stored result is printed and its exit code returned. A
   proposed session can be sent by a second run with ``--send``. Orders are sent once: the
   marker says ``sending`` just before the first order goes out and ``sent`` once the broker
   has answered, and a session in either state never sends again (an interrupted send is an
   alert, to be checked at the broker by hand).
3. **Fresh prices, signals, proposal** -- ``DailyRunner.run_day`` on the session's close
   (``as_of``). A sleeve in error (its run failed, too much of it stale, no close for the
   session) is set aside for the day under ``sleeve_error_policy: skip`` (the default): what
   the account holds for it does not move, nothing new is bought for it, the other sleeves are
   proposed and a ``sleeve_skipped`` alert names it, with its cause and its weight. When the
   sleeves in error weigh more than ``sleeve_error_max_weight`` (50 % by default) of the summed
   weights, or under ``block``, nothing is proposed (stale data).
4. **Send** only with ``--send``, before the market-on-open cut-off of the next session
   (09:28 New York); later, the orders would fill a day late, so they are proposed only and
   the run raises an alert.
5. **Reconciliation.** The positions the account holds against the previous session's
   positions plus the orders sent then, and the orders still working at the broker.
6. **Report and alerts** -- ``reports/SESSION.md`` and ``reports/SESSION.json`` (and
   ``reports/latest.*``): the portfolio's value, the orders proposed, sent and filled, the gap
   to the backtest and its terms, the sleeves in error, the stale symbols, the age of the
   point-in-time universe the sleeves use, the alerts, and a one-line summary. The exit code says what happened (``EXIT_*`` below); an optional command
   (``--notify-cmd``) is run with the summary when there is an alert.

Exit codes: 0 done (or nothing to do), 2 usage, 3 stale prices or too many sleeves in error
(nothing proposed), 4 done with alerts (proposed, stale symbols or sleeves set aside
included), 5 run failed, 6 broker unreachable, 7 another run holds the lock.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import math
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from ...calendars import NYSE, ExchangeCalendar
from ..guards import KillSwitch
from .borrow import alert_lines
from .borrow import markdown as borrow_markdown
from .instruments import is_fx
from .runner import DailyRunner, StaleData

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_STALE = 3
EXIT_ALERT = 4
EXIT_FAILED = 5
EXIT_BROKER = 6
EXIT_BUSY = 7

_NS = 1_000_000_000
FX_RECONCILE_TOLERANCE = 0.03       # relative: a forex position against the previous one plus the orders


# --------------------------------------------------------------------------------------------- #
# The calendar                                                                                  #
# --------------------------------------------------------------------------------------------- #
def _from_ns(ns) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(int(ns) / _NS, tz=_dt.timezone.utc)


def _aware(now: _dt.datetime | None) -> _dt.datetime:
    if now is None:
        return _dt.datetime.now(_dt.timezone.utc)
    return now if now.tzinfo is not None else now.astimezone()


def closed_session(now: _dt.datetime | None = None, *, calendar: ExchangeCalendar = NYSE,
                   margin: _dt.timedelta = _dt.timedelta(minutes=30)) -> str:
    """The last session of ``calendar`` whose close (early closes included) is at least
    ``margin`` before ``now``, as ``YYYY-MM-DD``."""
    now = _aware(now)
    day = now.astimezone(calendar.tz).date()
    for _ in range(30):
        hours = calendar.session(day)
        if hours is not None and _from_ns(hours[1]) + margin <= now:
            return day.isoformat()
        day -= _dt.timedelta(days=1)
    raise RuntimeError(f"no {calendar.name} session in the 30 days before {now}")


def next_open(session: str, *, calendar: ExchangeCalendar = NYSE) -> _dt.datetime:
    """The open of the first session after ``session`` (UTC)."""
    day = _dt.date.fromisoformat(session)
    for _ in range(30):
        day += _dt.timedelta(days=1)
        hours = calendar.session(day)
        if hours is not None:
            return _from_ns(hours[0])
    raise RuntimeError(f"no {calendar.name} session in the 30 days after {session}")


# --------------------------------------------------------------------------------------------- #
# Alerts                                                                                        #
# --------------------------------------------------------------------------------------------- #
@dataclass
class AlertConfig:
    """The thresholds of the alerts, as fractions of the account's NAV."""

    max_gap: float = 0.01               # |cumulative gap to the backtest| / NAV
    max_gap_day: float = 0.005          # |the day's gap| / NAV
    max_loss: float = 0.03              # the day's loss / the previous NAV
    close_margin_minutes: float = 30.0  # a session counts as closed this long after its close
    moo_cutoff_minutes: float = 2.0     # market-on-open orders are sent this long before the open


@dataclass
class Alert:
    code: str           # stale_prices, run_failed, broker_unreachable, tracking_gap, daily_loss,
    message: str        # orders_rejected, missed_open, send_interrupted, kill_switch,
    #                     fills_mismatch, orders_working, notify_failed, symbol_watch, short_borrow,
    #                     stale_symbol, out_of_universe, universe_age, sleeve_skipped,
    #                     public_series_gap, fx_policy


def check_day(row: dict, sent: list[dict], cfg: AlertConfig) -> list[Alert]:
    """The alerts a completed day raises from its tracking row and its sent orders."""
    out = []
    nav = row.get("nav_real") or float("nan")
    if math.isfinite(nav) and nav > 0:
        gap, gap_day = row.get("gap_cum", 0.0) / nav, row.get("gap_day", 0.0) / nav
        if abs(gap) > cfg.max_gap:
            out.append(Alert("tracking_gap", f"gap to the backtest {row['gap_cum']:+,.2f} ({gap:+.2%} of NAV), "
                                             f"above {cfg.max_gap:.2%}"))
        if abs(gap_day) > cfg.max_gap_day:
            out.append(Alert("tracking_gap", f"the day's gap {row['gap_day']:+,.2f} ({gap_day:+.2%} of NAV), "
                                             f"above {cfg.max_gap_day:.2%}"))
        change = row.get("real_change", 0.0)
        before = nav - change
        if before > 0 and change / before < -cfg.max_loss:
            out.append(Alert("daily_loss", f"the day's loss {change:+,.2f} ({change / before:+.2%}), "
                                           f"beyond {cfg.max_loss:.2%}"))
    bad = [s for s in sent if s.get("status") in ("rejected", "cancelled", "refused")]   # refused: a local guard
    if bad:
        out.append(Alert("orders_rejected", f"{len(bad)} order(s) refused by the broker: " + ", ".join(
            f"{s['symbol']} {s['quantity']:+g} {s['status']} {s.get('reason', '')}".strip() for s in bad[:10])))
    return out


# --------------------------------------------------------------------------------------------- #
# The session                                                                                   #
# --------------------------------------------------------------------------------------------- #
@dataclass
class SessionResult:
    code: int
    session: str = ""
    status: str = ""                    # done, alert, stale, failed, nothing_to_do, busy
    summary: str = ""
    report: dict = field(default_factory=dict)
    report_md: str = ""
    report_json: str = ""


class _Lock:
    """An exclusive, non-blocking lock on a file (released when the process dies)."""

    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.fh = open(self.path, "a+")  # noqa: SIM115 - held for the run
        try:
            import fcntl
        except ImportError:  # pragma: no cover - not POSIX: no lock, the markers still hold
            return True
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        return True

    def release(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None


class DailySession:
    """The day of the paper run (module doc). ``provider_factory()`` gives the books' provider;
    ``account_factory(send)`` opens the account (read-only when ``send`` is false)."""

    def __init__(self, meta, directory: str, *, provider_factory: Callable, account_factory: Callable,
                 send: bool = False, now: _dt.datetime | None = None, session: str | None = None,
                 alerts: AlertConfig | None = None, allow_stale: bool = False, force: bool = False,
                 notify_cmd: str = "", notify: str = "alerts", calendar: ExchangeCalendar = NYSE,
                 log=print, borrow=None):
        if notify not in ("alerts", "always", "never"):
            raise ValueError("notify is 'alerts', 'always' or 'never'")
        self.meta = meta
        self.dir = os.path.expanduser(directory)
        self.provider_factory = provider_factory
        self.account_factory = account_factory
        self.send = bool(send)
        self.now = _aware(now)
        self.session_override = session
        self.cfg = alerts or AlertConfig()
        self.allow_stale = allow_stale
        self.force = force
        self.notify_cmd = notify_cmd
        self.notify = notify
        self.calendar = calendar
        self.log = log
        self.borrow = borrow                # the shorts' borrow source (borrow.py), None: no section
        for sub in ("sessions", "reports"):
            os.makedirs(os.path.join(self.dir, sub), exist_ok=True)

    # -- markers ---------------------------------------------------------------------------------
    def _marker_path(self, session: str) -> str:
        return os.path.join(self.dir, "sessions", session + ".json")

    def marker(self, session: str) -> dict:
        path = self._marker_path(session)
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _mark(self, session: str, **values) -> dict:
        m = self.marker(session)
        m.update(values, session=session, updated=_dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"))
        path = self._marker_path(session)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=1, sort_keys=True, default=str)
        os.replace(tmp, path)
        return m

    # -- the run ---------------------------------------------------------------------------------
    def run(self) -> SessionResult:
        lock = _Lock(os.path.join(self.dir, "daily.lock"))
        if not lock.acquire():
            msg = f"another run holds {lock.path}: nothing done"
            self.log(msg)
            return SessionResult(EXIT_BUSY, status="busy", summary=msg)
        try:
            return self._run()
        finally:
            lock.release()

    def _run(self) -> SessionResult:
        margin = _dt.timedelta(minutes=self.cfg.close_margin_minutes)
        session = self.session_override or closed_session(self.now, calendar=self.calendar, margin=margin)
        today_ny = self.now.astimezone(self.calendar.tz).date()
        holiday = not self.calendar.is_trading_day(today_ny)
        marker = self.marker(session)
        phase = marker.get("phase", "")

        # one run per session, one send per session
        if phase in ("sent", "sending") or (phase == "proposed" and not self.send and not self.force):
            why = (f"{today_ny} is not a {self.calendar.name} session; " if holiday else "")
            msg = f"{why}session {session} already {phase}: nothing done ({marker.get('summary', '')})"
            if phase == "sending" and self.send:
                alert = Alert("send_interrupted", f"session {session}: a send started and did not finish; "
                                                  "check the orders at the broker, nothing is sent again")
                return self._finish(session, None, [alert], EXIT_ALERT, "alert", errors={}, stale={},
                                    extra={"phase": phase}, keep_phase=True)
            self.log(msg)
            return SessionResult(int(marker.get("code", EXIT_OK)), session, "nothing_to_do", msg,
                                 report=marker.get("report", {}),
                                 report_md=marker.get("report_md", ""), report_json=marker.get("report_json", ""))

        alerts: list[Alert] = []
        send = self.send
        if send:
            cutoff = next_open(session, calendar=self.calendar) - _dt.timedelta(minutes=self.cfg.moo_cutoff_minutes)
            if self.now >= cutoff:
                alerts.append(Alert("missed_open", f"now {self.now.isoformat(timespec='minutes')} is past the "
                                                   f"market-on-open cut-off {cutoff.isoformat(timespec='minutes')} "
                                                   f"after {session}: proposed only, nothing sent"))
                send = False
            kill = KillSwitch(os.path.join(self.dir, "KILL"))
            if send and kill.tripped:
                alerts.append(Alert("kill_switch", f"kill switch: {kill.reason}; proposed only, nothing sent"))
                send = False
        self._mark(session, phase="running", started=self.now.isoformat(timespec="seconds"))

        try:
            provider = self.provider_factory()
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            alerts.append(Alert("run_failed", f"the strategies' provider: {type(exc).__name__}: {exc}"))
            return self._finish(session, None, alerts, EXIT_FAILED, "failed", errors={}, stale={})
        try:
            account = self.account_factory(send)
        except Exception as exc:  # noqa: BLE001
            alerts.append(Alert("broker_unreachable", f"{type(exc).__name__}: {exc}"))
            return self._finish(session, None, alerts, EXIT_BROKER, "failed", errors={}, stale={})

        seen: dict = {}
        self._watch(account, session, seen)
        runner = DailyRunner(self.meta, provider, account, self.dir, allow_stale=self.allow_stale, log=self.log,
                             borrow=self.borrow)
        code, status, report = EXIT_OK, "done", None
        try:
            report = runner.run_day(session, send=send)
        except StaleData as exc:
            alerts.append(Alert("stale_prices", str(exc)))
            code, status = EXIT_STALE, "stale"
        except Exception as exc:  # noqa: BLE001
            from ..adapter import AdapterError

            broker = isinstance(exc, (AdapterError, ConnectionError, TimeoutError))
            alerts.append(Alert("broker_unreachable" if broker else "run_failed", f"{type(exc).__name__}: {exc}"))
            code, status = (EXIT_BROKER if broker else EXIT_FAILED), "failed"
        finally:
            with contextlib.suppress(Exception):
                account.close()
        errors = dict(getattr(provider, "errors", {}) or {})
        stale = {k: dict(v) for k, v in (getattr(provider, "stale", {}) or {}).items()}
        universe = universe_status(getattr(provider, "universe_last", None), session,
                                   self.meta.universe_max_age_days)
        if universe is not None and universe["old"]:
            alerts.append(Alert("universe_age", universe_message(universe)))
        prices = None
        if callable(getattr(provider, "price_report", None)) and getattr(provider, "fresh_prices", None):
            with contextlib.suppress(Exception):    # a report line, never a reason to fail
                prices = provider.price_report()
        for _sym, msg in sorted(((prices or {}).get("public_alerts") or {}).items()):
            alerts.append(Alert("public_series_gap", msg))
        if report is None:
            return self._finish(session, None, alerts, code, status, errors=errors, stale=stale,
                                extra={"universe": universe, "prices": prices})

        alerts += check_day(report.row, report.sent, self.cfg)
        for x in report.skipped:
            alerts.append(Alert("sleeve_skipped", skipped_message(x)))
        if report.stale:
            alerts.append(Alert("stale_symbol", stale_message(report.stale)))
        capped = [x for x in report.out_of_universe if x["capped"]]
        if capped:
            alerts.append(Alert("out_of_universe", out_of_universe_message(capped)))
        recon = self._reconcile(session, seen)
        if recon["mismatches"]:
            alerts.append(Alert("fills_mismatch", f"{len(recon['mismatches'])} position(s) differ from the previous "
                                                  "session's positions plus the orders sent then: " + ", ".join(
                f"{m['symbol']} held {m['held']:g} expected {m['expected']:g}" for m in recon["mismatches"][:10])))
        if recon["working"]:
            alerts.append(Alert("orders_working", f"{len(recon['working'])} order(s) still working at the broker: "
                                + ", ".join(f"{s} {q:+g}" for s, q in recon["working"][:10])))
        for w in report.warnings:                   # traded on stale data (allow_stale)
            if w.startswith("the data's last close") or ": last close " in w:
                alerts.append(Alert("stale_prices", w))
            elif w.startswith("watch: "):           # a delisted name, a hard-to-borrow short, a note
                alerts.append(Alert("symbol_watch", w[len("watch: "):]))
            elif w.startswith("fx: "):              # the currency policy: a short rate not read, an exposure unexplained
                alerts.append(Alert("fx_policy", w[len("fx: "):]))
        for x in alert_lines(report.borrow):        # the day's borrow data (borrow.py)
            alerts.append(Alert("short_borrow", f"{x['symbol']}: {x['status'].replace('_', ' ')} ({x['why']}); "
                                                f"short {x['short_quantity']:,.0f}"))
        if alerts:
            code, status = EXIT_ALERT, "alert"
        return self._finish(session, report, alerts, code, status, errors=errors, stale=stale,
                            extra={"reconciliation": recon, "fills": seen.get("fills", []), "cash": seen.get("cash"),
                                   "universe": universe, "prices": prices,
                                   "sent_requested": self.send, "sent": bool(send) and report is not None},
                            phase="sent" if send else "proposed")

    def _watch(self, account, session: str, seen: dict) -> None:
        """Record the fills the account settles, and mark the session around the send."""
        settle, place, snapshot = account.settle, account.place, account.snapshot

        def settle_and_keep(*a, **kw):
            fills = settle(*a, **kw)
            seen["fills"] = [asdict(f) if hasattr(f, "__dataclass_fields__") else dict(f.__dict__) for f in fills]
            return fills

        def place_once(orders, date):
            self._mark(session, phase="sending", orders=len(orders))
            out = place(orders, date)
            self._mark(session, phase="sent", sent=out)
            return out

        def snapshot_and_keep(*a, **kw):
            snap = snapshot(*a, **kw)
            if "cash" not in seen:                  # the account before the day's orders
                seen["cash"], seen["working"] = snap.cash, list(snap.open_orders)
            return snap

        account.settle, account.place, account.snapshot = settle_and_keep, place_once, snapshot_and_keep

    def _reconcile(self, session: str, seen: dict) -> dict:
        """Today's positions against the previous session's positions plus its sent orders."""
        out: dict = {"previous": None, "mismatches": [], "working": []}
        state_path = os.path.join(self.dir, "state.json")
        if not os.path.exists(state_path):
            return out
        with open(state_path, encoding="utf-8") as fh:
            days = json.load(fh).get("days", {})
        today = days.get(session, {})
        earlier = sorted(d for d in days if d < session)
        out["working"] = list(seen.get("working", []))     # before the day's orders were placed
        if not earlier:
            return out
        prev = earlier[-1]
        out["previous"] = prev
        prev_marker = self.marker(prev)
        if not days[prev].get("sent") or prev_marker.get("phase") != "sent":
            return out
        expected = dict(days[prev].get("held", {}))
        for s in prev_marker.get("sent", []):
            if s.get("status") in ("rejected", "cancelled", "skipped", "refused", "validated"):  # validated: nothing traded
                continue
            expected[s["symbol"]] = expected.get(s["symbol"], 0.0) + float(s["quantity"])
        held = today.get("held", {})
        working = {s for s, _q in out["working"]}
        for sym in sorted(set(expected) | set(held)):
            e, h = expected.get(sym, 0.0), held.get(sym, 0.0)
            # a forex order on an inverted pair is sized in USD at the reference close: the
            # currency it brings differs by the move to the fill (FX_RECONCILE_TOLERANCE)
            tol = FX_RECONCILE_TOLERANCE if is_fx(sym) else 1e-6
            if abs(e - h) > tol * max(1.0, abs(e)) and sym not in working:
                out["mismatches"].append({"symbol": sym, "held": h, "expected": e})
        return out

    # -- the report --------------------------------------------------------------------------------
    def _finish(self, session: str, report, alerts: list[Alert], code: int, status: str, *, errors: dict,
                stale: dict, extra: dict | None = None, phase: str = "failed", keep_phase: bool = False
                ) -> SessionResult:
        extra = dict(extra or {})
        state_day: dict = {}
        if report is not None:
            with contextlib.suppress(OSError, ValueError), \
                    open(os.path.join(self.dir, "state.json"), encoding="utf-8") as fh:
                state_day = json.load(fh)["days"].get(session, {})
        data = self._data(session, report, alerts, code, status, errors, stale, extra, state_day)
        base = os.path.join(self.dir, "reports", session)
        md = render_markdown(data)
        for path, text in ((base + ".md", md), (base + ".json", json.dumps(data, indent=1, default=str)),
                           (os.path.join(self.dir, "reports", "latest.md"), md),
                           (os.path.join(self.dir, "reports", "latest.json"), json.dumps(data, indent=1, default=str))):
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, path)
        result = SessionResult(code, session, status, data["summary"], data, base + ".md", base + ".json")
        note = self._notify(result) if self.notify_cmd else None
        if note is not None:
            data["notify"] = note
            with open(base + ".json", "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1, default=str)
        values = {"code": code, "status": status, "summary": data["summary"], "report_md": result.report_md,
                  "report_json": result.report_json, "report": {"summary": data["summary"], "code": code}}
        if not keep_phase and self.marker(session).get("phase") not in ("sending", "sent"):
            values["phase"] = phase                 # a send that went out stays recorded: never twice
        self._mark(session, **values)
        self.log(data["summary"])
        for a in alerts:
            self.log(f"ALERT {a.code}: {a.message.splitlines()[0]}")
        self.log(f"report: {result.report_md}")
        return result

    def _data(self, session, report, alerts, code, status, errors, stale, extra, state_day) -> dict:
        row = dict(report.row) if report is not None else {}
        plan = report.plan if report is not None else None
        sent = list(report.sent) if report is not None else []
        fills = extra.get("fills", [])
        nav = row.get("nav_real")
        change = row.get("real_change", 0.0) or 0.0
        day_ret = change / (nav - change) if nav and nav - change else 0.0
        proposed = [{"symbol": o.symbol, "quantity": o.quantity, "reference_close": o.price,
                     "notional": o.notional, "current": o.current, "target": o.target} for o in plan.orders] if plan else []
        skipped = {x["slug"]: x for x in (report.skipped if report is not None else [])}
        sleeves = []
        for s in self.meta.sleeves:
            info = (state_day.get("sleeves") or {}).get(s.slug, {})
            sleeves.append({"slug": s.slug, "weight": s.weight, "units": (plan.units.get(s.slug) if plan else None),
                            "close": info.get("date"), "nav": info.get("nav"),
                            "error": errors.get(s.slug, ""), "stale": sorted(stale.get(s.slug, {})),
                            "skipped": s.slug in skipped})
        stale_symbols = sorted({sym for v in stale.values() for sym in v})
        stale_detail = list(report.stale) if report is not None else []
        codes = sorted({a.code for a in alerts})
        mode = "send" if extra.get("sent") else ("propose (send refused)" if self.send else "propose")
        if report is not None:
            summary = (f"{session} {status.upper()} | NAV {nav:,.0f} ({day_ret:+.2%}) | gap to backtest "
                       f"{row.get('gap_cum', 0.0):+,.0f} ({(row.get('gap_cum', 0.0) / nav if nav else 0.0):+.2%}"
                       + (f"; without fx {row['gap_cum_ex_fx']:+,.0f} ({row['gap_cum_ex_fx'] / nav:+.2%})"
                          if nav and _finite(row.get("gap_cum_ex_fx")) and row.get("fx_cum") else "")
                       + f") | orders "
                       f"{len(proposed)} proposed, {len(sent)} sent, {len(fills)} filled | alerts: "
                       f"{', '.join(codes) or 'none'}"
                       + (f" | {len(skipped)} sleeve(s) skipped ("
                          f"{sum(x['weight'] for x in skipped.values()) / (sum(self.meta.weights.values()) or 1.0):.0%}"
                          " of the weights)" if skipped else ""))
        else:
            summary = (f"{session} {status.upper()} | no proposal | alerts: {', '.join(codes) or 'none'}"
                       + (f" | {len(stale_symbols)} stale symbol(s)" if stale_symbols else "")
                       + (f" | {len(errors)} sleeve(s) in error" if errors else ""))
        return {
            "session": session, "generated": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "run_at": self.now.isoformat(timespec="seconds"), "status": status, "exit_code": code, "mode": mode,
            "summary": summary,
            "alerts": [asdict(a) for a in alerts],
            "portfolio": {"nav": nav, "day_change": change, "day_return": day_ret,
                          "nav_expected": row.get("nav_expected"), "gross": row.get("gross"),
                          "net": row.get("net"), "positions": row.get("positions"),
                          "cash": extra.get("cash")},
            "tracking": {k: row.get(k) for k in ("gap_day", "gap_cum", "rounding", "untraded", "timing", "costs",
                                                 "fx", "residual", "expected_change", "costs_paid", "costs_backtest",
                                                 "gap_day_base", "gap_cum_base", "fx_cum", "gap_day_ex_fx",
                                                 "gap_cum_ex_fx")},
            "account": dict(report.fx, nav_base=row.get("nav_base")) if report is not None and report.fx else {},
            "orders": {"proposed": proposed, "sent": sent, "filled": fills,
                       "skipped": len(plan.skipped) if plan else 0},
            "sleeves": sleeves,
            "sleeves_in_error": errors,
            "sleeves_skipped": list(skipped.values()),
            "stale_symbols": stale_symbols,
            "stale_detail": stale_detail,
            "out_of_universe": list(report.out_of_universe) if report is not None else [],
            "universe": extra.get("universe"),
            "prices": extra.get("prices"),
            "warnings": list(report.warnings) if report is not None else [],
            "borrow": dict(report.borrow) if report is not None else {},
            "reconciliation": extra.get("reconciliation", {}),
            "proposal": report.proposal_md if report is not None else "",
        }

    def _notify(self, result: SessionResult) -> dict | None:
        if self.notify == "never" or (self.notify == "alerts" and result.code == EXIT_OK):
            return None
        env = dict(os.environ, TW_DAILY_SESSION=result.session, TW_DAILY_STATUS=result.status,
                   TW_DAILY_EXIT_CODE=str(result.code), TW_DAILY_SUMMARY=result.summary,
                   TW_DAILY_REPORT_MD=result.report_md, TW_DAILY_REPORT_JSON=result.report_json)
        try:
            proc = subprocess.run(shlex.split(os.path.expanduser(self.notify_cmd)), input=result.summary + "\n",
                                  text=True, env=env, capture_output=True, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"command": self.notify_cmd, "error": f"{type(exc).__name__}: {exc}"}
        return {"command": self.notify_cmd, "exit": proc.returncode, "stderr": (proc.stderr or "")[-500:]}


def skipped_message(x: dict) -> str:
    """The ``sleeve_skipped`` alert of one sleeve: its weight, the cause, what is kept."""
    kept = (f"{len(x['kept'])} line(s) kept as of {x['book_date']}" if x.get("kept") else "nothing held for it")
    return (f"{x['slug']} (weight {x['weight']:.4f}) set aside for the day: {x['cause']}; {kept}, no order from it, "
            "nothing new")


def stale_message(rows: list[dict]) -> str:
    """The ``stale_symbol`` alert: one line per symbol (last bar, cause, what was done)."""
    head = f"{len(rows)} symbol(s) still stale after the fresh-price top-up, set aside ({rows[0].get('policy', '')}):"
    return "\n".join([head] + [f"{x['symbol']}: last bar {x.get('last') or '?'}, {x.get('cause') or 'stale'} "
                                f"({x.get('reason', '')}); {x.get('action', '')}" for x in rows])


def universe_status(lasts: dict | None, session: str, max_age_days: float) -> dict | None:
    """The point-in-time universe the sleeves use, seen from ``session``: ``last`` (its last
    published date; the oldest when the sleeves' runs saw different ones), ``age_days``
    (calendar days from it to the session), ``max_age_days``, ``old`` (the age is above it) and
    the ``sleeves`` that use one. None when no sleeve does: no line, no alert.

    The harness carries the last known composition forward past the end of the table, so
    nothing breaks when it ages; the names that entered or left since are just not seen. The
    table is rebuilt once a month, so a month and a few days is its normal age at worst."""
    lasts = {str(k): str(v)[:10] for k, v in (lasts or {}).items() if v}
    if not lasts:
        return None
    last = min(lasts.values())
    age = (_dt.date.fromisoformat(session[:10]) - _dt.date.fromisoformat(last)).days
    return {"last": last, "age_days": age, "max_age_days": max_age_days, "old": age > max_age_days,
            "sleeves": sorted(lasts)}


def universe_line(u: dict) -> str:
    """``universe as of DD/MM (N days)``, the report's line."""
    day = _dt.date.fromisoformat(u["last"])
    return f"universe as of {day:%d/%m} ({u['age_days']} days)"


def universe_message(u: dict) -> str:
    """The ``universe_age`` alert."""
    return (f"the point-in-time universe (Stocks-Daily-LiquidUniverse) ends on {u['last']}, {u['age_days']} days "
            f"before the session (above {u['max_age_days']:g}, universe_max_age_days): its last composition is "
            f"carried forward, names that entered or left since are not seen; sleeves: " + ", ".join(u["sleeves"]))


def out_of_universe_message(rows: list[dict]) -> str:
    """The ``out_of_universe`` alert: the names whose book target was capped because the
    sleeves wanting them hold them outside their point-in-time universe."""
    head = (f"{len(rows)} name(s) held outside a point-in-time universe, not bought live "
            f"(out_of_universe_policy {rows[0].get('policy', '')}):")
    return "\n".join([head] + [
        f"{x['symbol']}: legs " + ", ".join(f"{s} {q:+.4g}" for s, q in sorted(x["sleeves"].items()))
        + f"; books want {x['wanted_total']:+.4g}, held {x['held']:+g}, target {x['target']:+g}" for x in rows])


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def prices_line(p: dict) -> str:
    """Who served the fresh prices: ``fresh prices (ibkr,alphavantage): 1 224 symbols, IB 1 199
    (98.0%), AlphaVantage 21 (1.7%), stale 4 (0.3%); IB collection 41 s``."""
    sh = p.get("shares") or {}

    def pct(k):
        v = sh.get(k)
        return f" ({v:.1%})" if isinstance(v, float) and math.isfinite(v) else ""

    ib = p.get("ib") or {}
    if ib.get("error"):
        tail = f"; IB not reached: {ib['error']}"
    elif ib:
        tail = (f"; IB collection {ib.get('seconds', 0):.0f} s ({ib.get('served', 0)}/{ib.get('asked', 0)} asked"
                + (f", {ib.get('kept')} already cached" if ib.get("kept") else "") + ")")
    else:
        tail = ""
    kr = p.get("kraken") or {}
    if kr.get("error"):
        tail += f"; Kraken not reached: {kr['error']}"
    kraken = f", Kraken {p['kraken_served']}{pct('kraken')}" if p.get("kraken_served") else ""
    public = f", public {p['public']}{pct('public')}" if p.get("public") else ""
    pub = p.get("public_series") or {}
    if pub:
        tail += "; signals from FRED: " + ", ".join(
            f"{s} ({v.get('series')}, {v.get('status')}, to {v.get('last_after')}"
            + (f", median gap {v['median_gap']:.2%}" if isinstance(v.get("median_gap"), float)
               and math.isfinite(v["median_gap"]) else "") + ")" for s, v in sorted(pub.items()))
    return (f"fresh prices ({','.join(p.get('sources') or [])}): {p.get('n', 0)} symbol(s), IB {p.get('ibkr', 0)}"
            f"{pct('ibkr')}{kraken}, AlphaVantage {p.get('alphavantage', 0)}{pct('alphavantage')}{public}, "
            f"stale {p.get('stale', 0)}{pct('stale')}{tail}")


def render_markdown(d: dict) -> str:
    """The report of the day as Markdown."""
    p, t, o = d["portfolio"], d["tracking"], d["orders"]

    def money(x):
        return "n/a" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:,.2f}"

    md = [f"# Paper run, session {d['session']}", "", f"**{d['summary']}**", "",
          f"- status: **{d['status']}** (exit code {d['exit_code']}), mode {d['mode']}, run at {d['run_at']}"]
    a = d.get("account") or {}
    if a:
        cur = a["currency"]
        md.append(f"- account currency **{cur}** ({a['origin']}); rate {a['rate']:.6g} USD per {cur} ({a['source']})")
        if cur != "USD" and p["nav"] is not None:
            md.append(f"- in {cur}: NAV {money(a.get('nav_base'))}, gap to the backtest day "
                      f"{money(t.get('gap_day_base'))}, cumulative {money(t.get('gap_cum_base'))}")
    if p["nav"] is not None:
        md += [(f"- portfolio value (NAV) {money(p['nav'])}, day {money(p['day_change'])} "
                f"({p['day_return']:+.2%}); backtest {money(p['nav_expected'])}"),
               f"- gross {p['gross']:.2f} x NAV, net {p['net']:+.2f}, {p['positions']} position(s)",
               (f"- gap to the backtest: day {money(t['gap_day'])}, cumulative {money(t['gap_cum'])} = rounding "
                f"{money(t['rounding'])} + untraded {money(t['untraded'])} + timing {money(t['timing'])} + costs "
                f"{money(t['costs'])} + fx {money(t.get('fx'))} + residual {money(t['residual'])} (USD)"),
               (f"- gap to the backtest without the account's currency (the fx term taken out): day "
                f"{money(t.get('gap_day_ex_fx'))}, cumulative {money(t.get('gap_cum_ex_fx'))} (USD; fx cumulative "
                f"{money(t.get('fx_cum'))})"),
               (f"- orders: {len(o['proposed'])} proposed, {len(o['sent'])} sent, {len(o['filled'])} filled at "
                f"today's open, {o['skipped']} under the minimum"),
               f"- proposal: `{d['proposal']}`"]
    if d.get("universe"):
        md.append(f"- {universe_line(d['universe'])}, {len(d['universe']['sleeves'])} sleeve(s) on a "
                  f"point-in-time universe" + (f", **older than {d['universe']['max_age_days']:g} days**"
                                               if d["universe"]["old"] else ""))
    if d.get("prices"):
        md.append(f"- {prices_line(d['prices'])}")
    md.append("")
    md += ["## Alerts", ""]
    md += [f"- **{a['code']}**: " + a["message"].replace("\n", "\n  ") for a in d["alerts"]] or ["- none"]
    md += ["", "## Sleeves", "", "| sleeve | weight | units | close | strategy NAV | status |", "|---|---:|---:|---|---:|---|"]
    for s in d["sleeves"]:
        if s.get("skipped"):
            state = "skipped (holdings kept): " + s["error"]
        elif s["error"]:
            state = "error: " + s["error"]
        else:
            state = "stale: " + ", ".join(s["stale"][:10]) if s["stale"] else "ok"
        units = "" if s["units"] is None else f"{s['units']:.6g}"
        md.append(f"| `{s['slug']}` | {s['weight']:.4f} | {units} | {s['close'] or ''} | {money(s['nav'])} | {state} |")
    if d.get("stale_detail"):
        md += ["", "## Stale symbols", "", "| symbol | last bar | cause | sleeves | policy | done |",
               "|---|---|---|---|---|---|"]
        for x in d["stale_detail"]:
            sym = x["symbol"] + (f" ({x['plan_symbol']})" if x.get("plan_symbol", x["symbol"]) != x["symbol"] else "")
            md.append(f"| {sym} | {x.get('last') or ''} | {x.get('cause') or ''}: {x.get('reason', '')} | "
                      + ", ".join(f"`{s}` {x.get('shares', {}).get(s, 0.0):.1%}" for s in x.get("sleeves", []))
                      + f" | {x.get('policy', '')} | {x.get('action', '')} |")
    elif d["stale_symbols"]:
        md += ["", "## Stale symbols", "", ", ".join(d["stale_symbols"])]
    if d.get("out_of_universe"):
        md += ["", "## Held outside a point-in-time universe", "",
               "| symbol | legs (units x target) | books want | held | target | policy |", "|---|---|---:|---:|---:|---|"]
        for x in d["out_of_universe"]:
            md.append(f"| {x['symbol']} | " + ", ".join(f"`{s}` {q:+.4g}" for s, q in sorted(x["sleeves"].items()))
                      + f" | {x['wanted_total']:+.4g} | {x['held']:+g} | {x['target']:+g} | {x['policy']}: "
                      + ("capped" if x["capped"] else "followed") + " |")
    if o["sent"]:
        md += ["", "## Orders sent", "", "| symbol | quantity | status |", "|---|---:|---|"]
        md += [f"| {s['symbol']} | {s['quantity']:+g} | {s.get('status', '')} {s.get('reason', '')} |" for s in o["sent"]]
    if o["filled"]:
        md += ["", "## Filled at today's open", "", "| symbol | quantity | price | open | costs |", "|---|---:|---:|---:|---:|"]
        md += [f"| {f['symbol']} | {f['quantity']:+g} | {f['price']:,.4f} | {f['reference']:,.4f} | {f['costs']:,.2f} |"
               for f in o["filled"]]
    r = d.get("reconciliation") or {}
    if r.get("mismatches") or r.get("working"):
        md += ["", "## Reconciliation", ""]
        md += [f"- {m['symbol']}: held {m['held']:g}, expected {m['expected']:g}" for m in r.get("mismatches", [])]
        md += [f"- working at the broker: {s} {q:+g}" for s, q in r.get("working", [])]
    if d.get("borrow", {}).get("lines"):
        md += [""] + borrow_markdown(d["borrow"])[:-1]
    if d["warnings"]:
        md += ["", "## Warnings", ""] + [f"- {w}" for w in d["warnings"]]
    md.append("")
    return "\n".join(md)


# --------------------------------------------------------------------------------------------- #
# The command                                                                                   #
# --------------------------------------------------------------------------------------------- #
def parser() -> argparse.ArgumentParser:
    from .__main__ import parser as daily_parser

    p = daily_parser()
    p.prog = "python -m tradewind.live.daily.session"
    p.description = __doc__
    a = p.add_argument_group("the day (scheduler)")
    a.add_argument("--now", default=None, help="the time of the run, ISO (default: now); for tests and dry runs")
    a.add_argument("--force", action="store_true", help="propose again a session already proposed (never resends)")
    a.add_argument("--alert-gap", type=float, default=AlertConfig.max_gap,
                   help="alert when |cumulative gap| / NAV exceeds this (default 0.01)")
    a.add_argument("--alert-gap-day", type=float, default=AlertConfig.max_gap_day,
                   help="alert when |the day's gap| / NAV exceeds this (default 0.005)")
    a.add_argument("--alert-loss", type=float, default=AlertConfig.max_loss,
                   help="alert when the day's loss exceeds this fraction of the NAV (default 0.03)")
    a.add_argument("--close-margin", type=float, default=AlertConfig.close_margin_minutes,
                   help="minutes after the close before a session counts as closed (default 30)")
    a.add_argument("--notify-cmd", default=os.environ.get("TRADEWIND_NOTIFY_CMD", ""),
                   help="a command run with the summary on stdin and TW_DAILY_* in its environment "
                        "(default $TRADEWIND_NOTIFY_CMD)")
    a.add_argument("--notify", choices=["alerts", "always", "never"], default="alerts")
    return p


def main(argv=None) -> int:
    from .__main__ import OVERRIDES, make_account, make_borrow, make_provider
    from .meta import MetaPortfolio

    args = parser().parse_args(argv)
    if args.replay:
        print("--replay is not a day: use python -m tradewind.live.daily", file=sys.stderr)
        return EXIT_USAGE
    overrides = {k: getattr(args, k) for k in OVERRIDES}
    try:
        meta = MetaPortfolio.load(args.meta, **overrides)
        now = _dt.datetime.fromisoformat(args.now) if args.now else None
    except (OSError, ValueError, ImportError) as exc:
        print(exc, file=sys.stderr)
        return EXIT_USAGE
    directory = os.path.expanduser(args.dir)
    os.makedirs(directory, exist_ok=True)
    cfg = AlertConfig(max_gap=args.alert_gap, max_gap_day=args.alert_gap_day, max_loss=args.alert_loss,
                      close_margin_minutes=args.close_margin)
    day = DailySession(meta, directory, provider_factory=lambda: make_provider(args, meta, directory),
                       account_factory=lambda send: make_account(args, directory, send),
                       send=args.send, now=now, session=args.as_of, alerts=cfg, allow_stale=args.allow_stale,
                       force=args.force, notify_cmd=args.notify_cmd, notify=args.notify,
                       borrow=make_borrow(meta, args.broker))
    return day.run().code


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["EXIT_ALERT", "EXIT_BROKER", "EXIT_BUSY", "EXIT_FAILED", "EXIT_OK", "EXIT_STALE", "EXIT_USAGE", "Alert",
           "AlertConfig", "DailySession", "SessionResult", "check_day", "closed_session", "next_open",
           "out_of_universe_message", "prices_line", "render_markdown", "skipped_message", "stale_message", "universe_line", "universe_message",
           "universe_status"]
