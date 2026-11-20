"""The daily run: sleeves' books -> the meta-portfolio's target -> orders -> tracking.

One call of ``DailyRunner.run_day`` after the US close:

1. each strategy runs in signal mode up to the last close (``providers.py``) and gives its
   target book;
2. the account settles the day (the simulated account fills yesterday's orders at today's
   open) and is read: positions, cash, NAV;
3. the day is tracked against the backtest (``tracking.py``);
4. the plan: units of each sleeve (kept, or reset on the schedule / the gross cap), the exact
   book, the rounded book, the orders (``aggregate.py``);
5. the proposal is written (``orders/DATE.csv`` and ``orders/DATE.md``); only with
   ``send=True`` are the orders placed -- market-on-open, filled at the next open;
6. the state of the day is saved (``state.json``) and the journal updated.

Running the same day again (propose, read, then send) recomputes it from the previous day's
state: nothing is counted twice.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
from dataclasses import dataclass, field

from ..guards import KillSwitch
from .aggregate import Plan, combine, gross, merged_prices, plan_day
from .books import SleeveBook
from .brokers import estimated_commission
from .instruments import FX_CAUTION, fx_pair, is_fx, substitute_books
from .meta import MetaPortfolio
from .tracking import Journal, track_day


class StaleData(RuntimeError):
    """A strategy's last close is older than the day asked for."""


@dataclass
class DayReport:
    date: str
    plan: Plan
    row: dict
    sent: list = field(default_factory=list)
    proposal_csv: str = ""
    proposal_md: str = ""
    chart: str | None = None
    warnings: list = field(default_factory=list)


class DailyRunner:
    """``meta``: the meta-portfolio; ``provider``: where the books come from; ``account``: a
    ``SimAccount`` or an ``IBPaperAccount``; ``directory``: state, proposals and journal."""

    def __init__(self, meta: MetaPortfolio, provider, account, directory: str, *, allow_stale: bool = False,
                 max_age_days: int = 4, today=None, log=print):
        self.meta = meta
        self.provider = provider
        self.account = account
        self.dir = os.path.expanduser(directory)
        os.makedirs(os.path.join(self.dir, "orders"), exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.journal = Journal(self.dir)
        self.kill = KillSwitch(os.path.join(self.dir, "KILL"))
        self.allow_stale = allow_stale
        self.max_age_days = int(max_age_days)        # calendar days between the data and the last session
        self._today = today
        self.log = log

    # -- state -----------------------------------------------------------------------------------
    def _load(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh)
        return {"days": {}}

    def _save(self, state: dict) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.state_path)

    def today(self) -> str:
        """The last weekday session on or before today (or the date given at construction)."""
        return self._today or last_session()

    # -- one day ---------------------------------------------------------------------------------
    def _pick(self, history: dict[str, dict[str, SleeveBook]], as_of: str | None):
        warnings = []
        lasts = {slug: max(h) for slug, h in history.items() if h}
        missing = [s.slug for s in self.meta.sleeves if s.slug not in history]
        if missing:
            raise RuntimeError(f"no book for {missing}: nothing is proposed while a sleeve is missing")
        empty = [s.slug for s in self.meta.sleeves if s.slug not in lasts]
        if empty:                                   # the strategies ran, the data end before
            raise StaleData(f"no close up to {as_of or 'the last session'} for {empty}: the data end earlier "
                            "(monthly datasets: --fresh-prices)")
        day = as_of or max(lasts.values())
        if as_of is None:
            behind = (_dt.date.fromisoformat(self.today()) - _dt.date.fromisoformat(day)).days
            if behind > self.max_age_days:
                msg = (f"the data's last close is {day}, {behind} days before the last session "
                       f"{self.today()}")
                if not self.allow_stale:
                    raise StaleData(msg + ": the datasets are not up to date (pass allow_stale to trade anyway)")
                warnings.append(msg)
        books = {}
        for slug, h in history.items():
            dates = [d for d in h if d <= day]
            if not dates:
                raise StaleData(f"{slug}: no close on or before {day}")
            last = max(dates)
            if last != day:
                msg = f"{slug}: last close {last}, not {day}"
                if not self.allow_stale:
                    raise StaleData(msg + " (the data are behind: rerun later, or pass allow_stale)")
                warnings.append(msg)
            books[slug] = h[last]
        return day, books, warnings

    def _substitute(self, history: dict[str, dict[str, SleeveBook]]):
        """The books with their indices replaced by the meta-portfolio's substitutes."""
        used = {i: s for i, s in self.meta.substitutes.items()
                if any(i in b.close or i in b.target or i in b.positions for h in history.values() for b in h.values())}
        if not used:
            return history, [], {}
        prices: dict = {}
        warnings: list[str] = []
        etfs = sorted({s.symbol for s in used.values() if s.kind == "etf"})
        dates = sorted({d for h in history.values() for d in h})
        loader = getattr(self.provider, "prices", None)
        if etfs and dates and loader is not None:
            try:
                prices = loader(etfs, dates[0], dates[-1])
            except Exception as exc:  # noqa: BLE001 - the substitution then says what is missing
                warnings.append(f"prices of the substitutes {etfs} not loaded: {exc}")
        history, more = substitute_books(history, used, prices)
        return history, warnings + more, used

    def _watch(self, plan, books: dict[str, SleeveBook], history: dict[str, dict[str, SleeveBook]],
               held: dict[str, float], day: str) -> list[str]:
        """Symbols to look at before sending: notes of the meta-portfolio, delisted or frozen
        prices, forex odd lots and caveats, hard-to-borrow shorts."""
        meta = self.meta
        out: list[str] = []
        live = {s for s, q in plan.target.items() if q} | {s for s, q in held.items() if q}
        for sym in sorted(live):
            if sym in meta.notes:
                out.append(f"note: {sym}: {meta.notes[sym]} (target {plan.target.get(sym, 0.0):+g}, "
                           f"held {held.get(sym, 0.0):+g})")
        for slug, b in sorted(books.items()):
            for sym, last in sorted(b.stale_bars.items()):
                if sym in live:
                    out.append(f"watch: {sym}: last price {last}, before the close {b.date} (delisted? data "
                               f"stopped), still in {slug}")
        flagged = {w.split(":")[1].strip() for w in out if w.startswith("watch:")}
        for sym in sorted(live - flagged):
            if is_fx(sym):
                continue
            closes = []
            for h in history.values():
                for d in sorted(x for x in h if x <= day)[-3:]:
                    if sym in h[d].close:
                        closes.append((d, h[d].close[sym]))
            series = dict(sorted(closes))
            if len(series) >= 3 and len(set(series.values())) == 1:
                out.append(f"watch: {sym}: close unchanged at {next(iter(series.values())):g} over the last "
                           f"{len(series)} closes (delisted? frozen data)")
        for o in plan.orders:
            pair = fx_pair(o.symbol)
            if pair is None or not math.isfinite(o.notional):
                continue
            if o.notional < meta.fx_odd_lot:
                out.append(f"forex odd lot: {o.symbol} {o.quantity:+,.0f} ({pair.ib_pair}) is {o.notional:,.0f} USD, "
                           f"under {meta.fx_odd_lot:,.0f}: IDEALPRO fills it as an odd lot, at a wider price")
        for cur in sorted({p.currency for p in map(fx_pair, live) if p is not None} & set(FX_CAUTION)):
            out.append(f"forex: {FX_CAUTION[cur]}")
        if meta.borrow_check:
            shorts = sorted(s for s, q in plan.target.items() if q < 0 and not is_fx(s)
                            and not any(s == sub.symbol and sub.kind == "future" for sub in meta.substitutes.values()))
            check = getattr(self.account, "shortable", None)
            if shorts and check is None:
                out.append("borrow check asked, but this account cannot read the shortable tick")
            elif shorts:
                try:
                    levels = check(shorts)
                except Exception as exc:  # noqa: BLE001 - a check, never a reason to stop
                    out.append(f"borrow check failed: {exc}")
                    levels = {}
                for sym in shorts:
                    v = levels.get(sym)
                    if v is None:
                        continue
                    if v <= 1.5:
                        out.append(f"watch: {sym}: not available to borrow (IB shortable {v:g}); the short "
                                   f"{plan.target[sym]:+g} will be refused")
                    elif v <= 2.5:
                        out.append(f"watch: {sym}: hard to borrow (IB shortable {v:g}): the short "
                                   f"{plan.target[sym]:+g} needs a locate and may be refused")
                missing = [s for s in shorts if levels.get(s) is None]
                if missing:
                    out.append("borrow check: no shortable tick for " + ", ".join(missing[:10]))
        return out

    def run_day(self, as_of: str | None = None, send: bool = False) -> DayReport:
        meta = self.meta
        history = self.provider.books(as_of, last_n=3)
        history, sub_warnings, subs = self._substitute(history)
        day, books, warnings = self._pick(history, as_of)
        for sub in subs.values():
            sub.check_expiry(day)
        specs = {s.symbol: s.spec() for s in subs.values() if s.spec()}
        if specs and hasattr(self.account, "set_instruments"):
            self.account.set_instruments(specs)
        warnings = list(getattr(self.provider, "warnings", [])) + sub_warnings + warnings
        state = self._load()
        earlier = sorted(d for d in state["days"] if d < day)
        prev = state["days"][earlier[-1]] if earlier else None
        books_prev = {}
        if prev is not None:
            for slug, h in history.items():
                if prev["date"] in h:
                    books_prev[slug] = h[prev["date"]]
        opens, closes = merged_prices(books, "open"), merged_prices(books, "close")
        if hasattr(self.account, "set_book_spreads"):
            spreads: dict[str, float] = {}
            for b in books.values():
                spreads.update(b.half_spread)
            self.account.set_book_spreads(spreads)
        fills = self.account.settle(day, opens, closes)
        snap = self.account.snapshot(closes)
        held = {s: q for s, q in snap.positions.items() if q}
        if snap.open_orders:
            warnings.append(f"{len(snap.open_orders)} order(s) still working at the broker: "
                            + ", ".join(f"{s} {q:+g}" for s, q in snap.open_orders[:8]))
        capital = float(meta.capital) if meta.capital else float(snap.nav)
        if not capital > 0:
            raise RuntimeError(f"the account's NAV is {snap.nav}: nothing to allocate")

        if prev is None:
            row = {"nav_real": snap.nav, "nav_expected": snap.nav, "gap_day": 0.0, "gap_cum": 0.0,
                   "rounding": 0.0, "untraded": 0.0, "timing": 0.0, "costs": 0.0, "residual": 0.0, "real_change": 0.0,
                   "expected_change": 0.0, "costs_paid": sum(f.costs for f in fills), "costs_backtest": 0.0,
                   "fills": len(fills), "notes": "first day"}
        else:
            row = track_day(prev, books, books_prev, held, snap.nav, fills)

        plan = plan_day(meta, books, held, capital, day,
                        units=None if prev is None else prev["units"],
                        last_reset=None if prev is None else prev.get("last_reset"),
                        weights_seen=None if prev is None else prev.get("weights"))
        for sym in plan.missing_price:
            warnings.append(f"{sym}: held but no strategy prices it; its order has no reference price")
        if plan.refused:
            warnings.append("not traded: " + "; ".join(
                f"{s} {plan.excluded.get(s, held.get(s, 0.0)):+.2f} ({why})" for s, why in sorted(plan.refused.items())))
        warnings += self._watch(plan, books, history, held, day)
        for b in books.values():
            for c in b.conditional:
                warnings.append(f"conditional order not counted in the target: {c}")

        sent: list = []
        if send:
            if self.kill.tripped:
                raise RuntimeError(f"kill switch: {self.kill.reason}; nothing is sent")
            sent = self.account.place(plan.orders, day)

        report = DayReport(day, plan, row, sent, warnings=warnings)
        self._write_proposal(report, snap, books, capital)

        # the state of the day
        state["days"][day] = {
            "date": day, "nav_real": snap.nav, "nav_expected": row["nav_expected"],
            "units": plan.units, "units_prev": {} if prev is None else prev["units"],
            "last_reset": day if plan.rebalanced else (prev or {}).get("last_reset", day),
            "weights": meta.weights, "held": held, "closes": closes,
            "sleeves": {slug: {"nav": b.nav, "positions": b.positions, "date": b.date} for slug, b in books.items()},
            "exact": plan.exact, "planned": {} if prev is None else prev.get("exact", {}),
            "target": plan.target, "sent": bool(send),
        }
        for later in [d for d in state["days"] if d > day]:
            del state["days"][later]                # a replayed day makes the later ones stale
        self._save(state)

        prices = merged_prices(books)
        row.update(date=day, data_date=min(b.date for b in books.values()),
                   gross=gross(held, prices) / snap.nav if snap.nav else float("nan"),
                   net=(sum(q * prices.get(s, float("nan")) for s, q in held.items()) / snap.nav
                        if snap.nav else float("nan")),
                   positions=len(held), orders=len(plan.orders), rebalanced=plan.rebalanced,
                   cap_scale=plan.cap_scale)
        self.journal.write_row(row)
        self.journal.write_positions(day, held, plan.exact, plan.target, closes)
        report.chart = self.journal.plot()
        return report

    # -- the proposal ------------------------------------------------------------------------------
    def _write_proposal(self, report: DayReport, snap, books: dict[str, SleeveBook], capital: float) -> None:
        plan, day = report.plan, report.date
        base = os.path.join(self.dir, "orders", day)
        status = {s["symbol"]: s for s in report.sent}
        lines_csv = ["symbol,side,quantity,reference_close,notional,current,target,exact,est_commission,status"]
        for o in plan.orders:
            st = status.get(o.symbol, {})
            lines_csv.append(",".join(str(x) for x in (
                o.symbol, "BUY" if o.quantity > 0 else "SELL", f"{abs(o.quantity):g}", f"{o.price:.4f}",
                f"{o.notional:.2f}", f"{o.current:g}", f"{o.target:g}", f"{o.exact:.4f}",
                f"{estimated_commission(o):.2f}" if math.isfinite(o.price) else "",
                (st.get("status", "") + (" " + st["reason"] if st.get("reason") else "")).strip() or "proposed")))
        with open(base + ".csv", "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines_csv) + "\n")

        buys = sum(o.notional for o in plan.orders if o.quantity > 0 and math.isfinite(o.notional))
        sells = sum(o.notional for o in plan.orders if o.quantity < 0 and math.isfinite(o.notional))
        commission = sum(estimated_commission(o) for o in plan.orders if math.isfinite(o.price))
        r = report.row
        md = [f"# Orders for the open after {day}", "",
              (f"- mode: **{'SENT' if report.sent else 'proposal (nothing sent)'}**, account `{snap.account}` "
               f"({type(self.account).__name__}), currency {snap.currency}"),
              f"- data: last close {day}; sleeves dated " + ", ".join(sorted({b.date for b in books.values()})),
              f"- account NAV {snap.nav:,.2f}, cash {snap.cash:,.2f}; capital allocated {capital:,.2f}",
              (f"- backtest NAV {r['nav_expected']:,.2f}; gap today {r['gap_day']:+,.2f}, "
               f"cumulative {r['gap_cum']:+,.2f} (rounding {r['rounding']:+,.2f}, untraded {r['untraded']:+,.2f}, "
               f"timing {r['timing']:+,.2f}, costs {r['costs']:+,.2f}, residual {r['residual']:+,.2f})"),
              (f"- units: {'reset (' + plan.reason + ')' if plan.rebalanced else 'kept'}; "
               f"gross cap scale {plan.cap_scale:.4f}"),
              (f"- target book: gross {plan.gross_target:.2f} x capital (exact {plan.gross_exact:.2f}, cap "
               f"{self.meta.max_gross:g}), net {plan.net_target:+.2f}, {len(plan.target)} lines"),
              (f"- orders: {len(plan.orders)} (buy {buys:,.0f}, sell {sells:,.0f}), est. IBKR commission "
               f"{commission:,.2f}; {len(plan.skipped)} under the minimum of {self.meta.min_order_notional:,.0f}"),
              ""]
        if report.warnings:
            md += ["## Warnings", ""] + [f"- {w}" for w in report.warnings] + [""]
        md += ["## Sleeves", "", "| sleeve | weight | units | strategy NAV | close | gross of the sleeve |",
               "|---|---:|---:|---:|---|---:|"]
        prices = merged_prices(books)
        for s in self.meta.sleeves:
            b = books[s.slug]
            k = plan.units.get(s.slug, 0.0)
            g = gross(combine({s.slug: k}, books), prices) / capital
            md.append(f"| `{s.slug}` | {s.weight:.4f} | {k:.6g} | {b.nav:,.2f} | {b.date} | {g:.3f} |")
        md += ["", "## Orders (market-on-open)", "",
               "| symbol | side | quantity | ref. close | notional | position -> target | status |",
               "|---|---|---:|---:|---:|---|---|"]
        for o in plan.orders:
            st = status.get(o.symbol, {})
            md.append(f"| {o.symbol} | {'BUY' if o.quantity > 0 else 'SELL'} | {abs(o.quantity):g} | {o.price:,.2f} | "
                      f"{o.notional:,.0f} | {o.current:g} -> {o.target:g} | {st.get('status', 'proposed')} "
                      f"{st.get('reason', '')} |")
        fx = [(o, fx_pair(o.symbol)) for o in plan.orders if fx_pair(o.symbol) is not None]
        if fx:
            md += ["", "## Forex orders at IDEALPRO (quantity in the pair's base currency)", "",
                   "| symbol | book quantity | IB pair | IB side | IB quantity | notional USD |", "|---|---:|---|---|---:|---:|"]
            for o, pair in fx:
                q = pair.ib_quantity(o.quantity, o.price)
                md.append(f"| {o.symbol} | {o.quantity:+,.0f} {pair.book_base} | {pair.ib_pair} | "
                          f"{'BUY' if q > 0 else 'SELL'} | {abs(q):,.0f} {pair.ib_base} | {o.notional:,.0f} |")
        if plan.skipped:
            md += ["", "## Not sent (under the minimum order)", ""]
            md += [f"- {o.symbol} {o.quantity:+g} ({o.notional:,.0f})" for o in plan.skipped]
        md.append("")
        with open(base + ".md", "w", encoding="utf-8") as fh:
            fh.write("\n".join(md))
        report.proposal_csv, report.proposal_md = base + ".csv", base + ".md"

    # -- replay ------------------------------------------------------------------------------------
    def replay(self, dates: list[str], send: bool = True) -> list[DayReport]:
        """Run ``run_day`` on each date in order (a provider that serves past closes, such as
        ``ReplayProvider``, and a simulated account)."""
        return [self.run_day(d, send=send) for d in dates]


def last_session(today: _dt.date | None = None) -> str:
    """The last weekday on or before ``today`` (holidays are the data's business)."""
    day = today or _dt.date.today()
    while day.weekday() >= 5:
        day -= _dt.timedelta(days=1)
    return day.isoformat()


__all__ = ["DailyRunner", "DayReport", "StaleData", "last_session"]
