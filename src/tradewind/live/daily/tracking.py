"""Live against backtest: the day's realised NAV, the NAV the backtest expects, and the gap.

**The expected NAV** is a backtest of the meta-portfolio built from the sleeves' own
backtests: it holds ``k_s`` units of each sleeve (``aggregate.py``), and like the sleeves it
decides at a close and trades at the next open, so a change of units takes effect at the open
that follows it. Over day ``t``, with ``g_s`` a sleeve's shares at a close, ``O`` the opens of
``t`` and ``C`` the closes::

    expected change = sum_s k_s(t) x (nav_s(t) - nav_s(t-1))
                    + sum_s (k_s(t-1) - k_s(t)) x g_s(t-1) . (O(t) - C(t-1))

(the second line keeps the old units over the night before a reset). Nothing in it depends on
the live account: it is what the sleeves' backtests earned at the units the plan used.

**The gap** (realised change minus expected change) splits into terms computed from the same
prices, so that with no other effect they add up to the gap exactly:

* ``rounding`` -- the held book ``h`` against the exact book the plan asked for (``P``, after
  the gross cap), overnight and over the session: ``(h(t-1) - P(t-1)) . (O - C(t-1)) +
  (h(t) - P(t)) . (C(t) - O)``; it holds the lots, the orders under the minimum, the orders
  not sent or not filled;
* ``untraded`` -- the plan against the sleeves' backtests, ``(P(t-1) - E_pre) . (O - C(t-1)) +
  (P(t) - E_post) . (C(t) - O)`` with ``E_pre = sum k_s(t-1) g_s(t-1)`` and ``E_post =
  sum k_s(t) g_s(t)``: an order a sleeve's backtest refused at the open (its cash check) that
  the account executed, and the excluded symbols;
* ``timing`` -- execution against the open the backtest fills at: ``sum fills q x (open -
  fill price)``;
* ``costs`` -- the backtest's costs at the units minus the costs paid:
  ``sum_s k_s(t) x costs_s(t) - sum fills costs``;
* ``fx`` -- an account whose currency is not USD: the cash it holds in its own currency,
  valued in USD, moves with the rate: ``base_cash(t-1) x (rate(t) - rate(t-1))``;
* ``residual`` -- the rest: interest, dividends and corporate actions the account books, data
  revised between two runs, sleeves whose fills are not at the open, prices missing.
"""

from __future__ import annotations

import contextlib
import math
import os

import pandas as pd

from .aggregate import combine, merged_prices
from .books import SleeveBook

COLUMNS = ["date", "nav_real", "nav_expected", "gap_day", "gap_cum", "rounding", "untraded", "timing", "costs",
           "fx", "residual", "real_change", "expected_change", "costs_paid", "costs_backtest", "gross", "net",
           "positions", "orders", "fills", "rebalanced", "cap_scale", "data_date", "notes",
           "account_currency", "fx_rate", "fx_source", "nav_base", "gap_day_base", "gap_cum_base"]


def _dot(book: dict[str, float], move: dict[str, float]) -> tuple[float, list[str]]:
    total, missing = 0.0, []
    for sym, q in book.items():
        if not q:
            continue
        m = move.get(sym)
        if m is None or not math.isfinite(m):
            missing.append(sym)
            continue
        total += q * m
    return total, missing


def _sub(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) - v
    return out


# a currency policy declared (spec/13, fxpolicy.py): the reference and the gap in the base currency
COLUMNS += ["base_currency", "fx_policy", "nav_expected_base", "rounding_base", "untraded_base", "timing_base",
            "costs_base", "carry_base", "fx_base", "residual_base", "fx_exposure", "fx_exposure_expected",
            "fx_hedge_order", "fx_hedge_order_expected", "fx_hedge_units", "fx_hedge_fee_expected"]


def track_day(prev: dict, books: dict[str, SleeveBook], books_prev: dict[str, SleeveBook],
              held: dict[str, float], nav_real: float, fills: list, fx_rate: float | None = None) -> dict:
    """The tracking row of the day of ``books`` against ``prev`` (the previous day's state).

    ``books_prev``: the sleeves' books at ``prev["date"]`` as today's run sees them (better
    than the stored ones when the data were revised); ``held``/``nav_real``: the account at
    today's close; ``fills``: today's executions (``brokers.Fill``)."""
    k_cur: dict[str, float] = prev.get("units", {})
    k_old: dict[str, float] = prev.get("units_prev", {})
    stored = {slug: SleeveBook(date=prev["date"], nav=s["nav"], positions=s["positions"],
                               close=prev.get("closes", {}))
              for slug, s in prev.get("sleeves", {}).items()}
    before = {slug: books_prev.get(slug) or stored.get(slug) for slug in set(stored) | set(books_prev)}
    before = {k: v for k, v in before.items() if v is not None}
    opens = merged_prices(books, "open")
    closes = merged_prices(books, "close")
    closes_prev = dict(prev.get("closes", {}))
    closes_prev.update(merged_prices(before, "close"))
    overnight = {s: opens[s] - closes_prev[s] for s in opens if s in closes_prev}
    session = {s: closes[s] - opens[s] for s in closes if s in opens}
    notes = []

    # the expected change
    expected = 0.0
    for slug, k in k_cur.items():
        b, a = books.get(slug), before.get(slug)
        if b is None or a is None:
            if k:
                notes.append(f"{slug}: no book on both days")
            continue
        expected += k * (b.nav - a.nav)
    for slug in set(k_cur) | set(k_old):
        dk = k_old.get(slug, 0.0) - k_cur.get(slug, 0.0)
        a = before.get(slug)
        if dk and a is not None:
            move, _ = _dot(a.positions, overnight)
            expected += dk * move
    real = nav_real - prev["nav_real"]

    # the terms of the gap: held against planned (rounding), planned against the sleeves'
    # backtests (untraded)
    e_pre = combine(k_old, before, "positions")
    e_post = combine(k_cur, books, "positions")
    planned_pre = prev.get("planned", {})          # the exact book planned for the day before
    planned_post = prev.get("exact", {})           # the exact book planned for today
    r1, m1 = _dot(_sub(prev.get("held", {}), planned_pre), overnight)
    r2, m2 = _dot(_sub(held, planned_post), session)
    u1, m3 = _dot(_sub(planned_pre, e_pre), overnight)
    u2, m4 = _dot(_sub(planned_post, e_post), session)
    rounding = r1 + r2
    untraded = u1 + u2
    timing = sum(f.quantity * (f.reference - f.price) for f in fills)
    costs_paid = sum(f.costs for f in fills)
    costs_backtest = sum(k * books[slug].costs for slug, k in k_cur.items() if slug in books)
    costs = costs_backtest - costs_paid
    # an account in another currency than the books' USD: its cash in that currency moves
    # with the rate (prev["base_cash"] units, from prev["fx_rate"] to today's)
    fx = 0.0
    if fx_rate is not None and prev.get("fx_rate") and prev.get("base_cash"):
        fx = float(prev["base_cash"]) * (float(fx_rate) - float(prev["fx_rate"]))
    gap = real - expected
    missing = sorted(set(m1) | set(m2) | set(m3) | set(m4))
    if missing:
        notes.append("no price for " + ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else ""))
    nav_expected = prev["nav_expected"] + expected
    return {"nav_real": nav_real, "nav_expected": nav_expected, "gap_day": gap,
            "gap_cum": nav_real - nav_expected, "rounding": rounding, "untraded": untraded, "timing": timing,
            "costs": costs, "fx": fx, "residual": gap - rounding - untraded - timing - costs - fx, "real_change": real, "expected_change": expected,
            "costs_paid": costs_paid, "costs_backtest": costs_backtest, "fills": len(fills),
            "notes": "; ".join(notes)}


class Journal:
    """The tracking journal of a run directory: ``tracking.csv`` (and ``.parquet`` when pyarrow
    is there), ``positions.csv`` (one row per day and symbol) and ``tracking.png``."""

    def __init__(self, directory: str):
        self.dir = directory
        self.csv = os.path.join(directory, "tracking.csv")
        self.positions_csv = os.path.join(directory, "positions.csv")

    def frame(self) -> pd.DataFrame:
        if not os.path.exists(self.csv):
            return pd.DataFrame(columns=COLUMNS)
        return pd.read_csv(self.csv, dtype={"date": str, "data_date": str, "notes": str},
                           keep_default_na=False, na_values=[""])

    def write_row(self, row: dict) -> pd.DataFrame:
        frame = self.frame()
        frame = frame[frame["date"].astype(str) != str(row["date"])]
        new = pd.DataFrame([{c: row.get(c) for c in COLUMNS}])
        frame = new if frame.empty else pd.concat([frame, new], ignore_index=True)
        frame = frame.sort_values("date").reset_index(drop=True)
        frame.to_csv(self.csv, index=False)
        with contextlib.suppress(ImportError):         # no parquet engine: the CSV is the journal
            frame.to_parquet(os.path.join(self.dir, "tracking.parquet"), index=False)
        return frame

    def write_positions(self, date: str, held: dict[str, float], exact: dict[str, float],
                        target: dict[str, float], closes: dict[str, float]) -> None:
        rows = [{"date": date, "symbol": s, "held": held.get(s, 0.0), "exact": exact.get(s, 0.0),
                 "target": target.get(s, 0.0), "close": closes.get(s, float("nan")),
                 "value": held.get(s, 0.0) * closes.get(s, float("nan"))}
                for s in sorted(set(held) | set(exact) | set(target))]
        new = pd.DataFrame(rows, columns=["date", "symbol", "held", "exact", "target", "close", "value"])
        if os.path.exists(self.positions_csv):
            old = pd.read_csv(self.positions_csv, dtype={"date": str})
            old = old[old["date"] != date]
            new = pd.concat([old, new], ignore_index=True) if not old.empty else new
        new.to_csv(self.positions_csv, index=False)

    def plot(self, path: str | None = None) -> str | None:
        """Realised against expected NAV (``tradewind.plots.plot_equity``) and the cumulative
        terms of the gap; None when matplotlib is missing or there is nothing to draw."""
        frame = self.frame()
        if len(frame) < 2:
            return None
        try:
            from ... import plots
            plt = plots._pyplot()
        except ImportError:
            return None
        index = pd.to_datetime(frame["date"])
        real = pd.Series(frame["nav_real"].astype(float).to_numpy(), index=index, name="realised")
        expected = pd.Series(frame["nav_expected"].astype(float).to_numpy(), index=index, name="backtest")
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True,
                                       gridspec_kw={"height_ratios": [3, 2]})
        plots.plot_equity(real, benchmark=expected, log=False, ax=ax1, label="account (realised)",
                          benchmark_label="backtest (expected)")
        for name, colour in (("rounding", plots.BENCHMARK), ("untraded", plots.STRATEGY), ("timing", plots.NET),
                             ("costs", plots.LOSS), ("residual", plots.TURNOVER)):
            values = frame[name].astype(float).fillna(0.0).cumsum().to_numpy()
            ax2.plot(index, values, color=colour, lw=1.4, label=name)
        ax2.plot(index, frame["gap_cum"].astype(float).to_numpy(), color=plots.INK, lw=1.8, label="gap")
        ax2.axhline(0.0, color=plots.GRID, lw=1)
        plots._style(ax2, "Cumulative gap and its terms (account currency)")
        ax2.legend(loc="lower left", frameon=False, ncol=6, fontsize=8)
        fig.tight_layout()
        path = path or os.path.join(self.dir, "tracking.png")
        fig.savefig(path, dpi=110)
        plt.close(fig)
        return path


__all__ = ["COLUMNS", "Journal", "track_day"]
