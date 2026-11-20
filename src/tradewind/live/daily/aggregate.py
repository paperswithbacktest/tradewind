"""From the sleeves' target books to the meta-portfolio's orders.

**Units.** The meta-portfolio holds ``k_s`` units of each sleeve ``s``: ``k_s`` times the
sleeve's shares, symbol by symbol. A sleeve's book is its strategy run on its own capital
(``nav_s``), so ``k_s = weight_s x leverage x capital / nav_s`` puts ``weight_s x leverage``
of the capital in it. Between two resets the units stay fixed, and the meta-portfolio's
value then moves by ``sum_s k_s x (change of nav_s)``: the backtest the live book is tracked
against (``tracking.py``). They are reset on the rebalance schedule, when the set of sleeves
or the weights change, and whenever the summed book would exceed ``max_gross``.

**Gross cap.** The exact book ``E = sum_s k_s x target_s`` (shares, floats) is netted across
sleeves; if its gross value exceeds ``max_gross x capital`` every unit is scaled down by the
same factor (IB's limit is on the account, not on each sleeve).

**Refused symbols.** A symbol the meta-portfolio never trades (``MetaPortfolio.refusal``:
``exclude_symbols``, an index without a substitute, a currency without an IDEALPRO pair) is
left out of the book and of the orders, with its reason (``Plan.refused``).

**Rounding.** ``E`` is rounded to the lot (to the nearest; ``lot=0`` keeps fractional
shares); an order smaller than ``min_order_notional`` is dropped unless it closes a line.

**Stale symbols.** A symbol whose price stayed stale after the fresh-price top-up
(``frozen``, from the runner) is not traded on the strategies' word: under ``hold`` its target
is what the account holds (no order, no new line), under ``close`` it is zero (the holding is
sold at the open). The exact book keeps the strategies' intent: the difference shows in the
tracking as untraded.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field

from .books import SleeveBook
from .meta import MetaPortfolio


@dataclass
class ProposedOrder:
    symbol: str
    quantity: float                     # signed
    price: float                        # the reference close
    current: float                      # the position before the order
    target: float                       # the rounded target
    exact: float                        # the unrounded target

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.price


@dataclass
class Plan:
    """What a day's run decides."""

    date: str
    capital: float
    units: dict[str, float]
    rebalanced: bool
    reason: str
    cap_scale: float                    # 1.0 when the gross cap did not bind
    exact: dict[str, float]             # unrounded target shares
    target: dict[str, float]            # rounded target shares
    orders: list[ProposedOrder]
    skipped: list[ProposedOrder] = field(default_factory=list)   # under min_order_notional
    excluded: dict[str, float] = field(default_factory=dict)     # excluded symbols' exact shares
    refused: dict[str, str] = field(default_factory=dict)        # why each excluded symbol is not traded
    missing_price: list[str] = field(default_factory=list)
    frozen: dict[str, str] = field(default_factory=dict)        # stale symbols: hold or close
    gross_exact: float = 0.0            # gross / capital of the exact book
    gross_target: float = 0.0           # gross / capital of the rounded book
    net_target: float = 0.0


def due_for_reset(policy: str, last: str | None, today: str) -> bool:
    """Whether the units are reset at ``today``'s close under ``policy`` (dates ISO)."""
    if last is None:
        return True
    if policy == "daily":
        return today != last
    if policy == "never":
        return False
    a, b = _dt.date.fromisoformat(last), _dt.date.fromisoformat(today)
    if policy == "weekly":
        return a.isocalendar()[:2] != b.isocalendar()[:2]
    return (a.year, a.month) != (b.year, b.month)            # monthly: the first close of a month


def sleeve_units(meta: MetaPortfolio, books: dict[str, SleeveBook], capital: float) -> dict[str, float]:
    out = {}
    for sleeve in meta.sleeves:
        book = books.get(sleeve.slug)
        if book is None or not (book.nav > 0):
            out[sleeve.slug] = 0.0
            continue
        out[sleeve.slug] = sleeve.weight * meta.leverage * capital / book.nav
    return out


def combine(units: dict[str, float], books: dict[str, SleeveBook], attr: str = "target") -> dict[str, float]:
    """``sum_s units_s x book_s.<attr>``, symbol by symbol (shares)."""
    out: dict[str, float] = {}
    for slug, k in units.items():
        book = books.get(slug)
        if book is None or not k:
            continue
        for sym, q in getattr(book, attr).items():
            out[sym] = out.get(sym, 0.0) + k * q
    return {s: q for s, q in out.items() if q}


def merged_prices(books: dict[str, SleeveBook], attr: str = "close") -> dict[str, float]:
    out: dict[str, float] = {}
    for book in books.values():
        for sym, p in getattr(book, attr).items():
            out.setdefault(sym, p)
    return out


def gross(book: dict[str, float], prices: dict[str, float]) -> float:
    return sum(abs(q) * prices[s] for s, q in book.items() if s in prices)


def round_lot(q: float, lot: float) -> float:
    if not lot:
        return q
    n = abs(q) / lot
    whole = math.floor(n + 0.5)                              # half away from zero
    return math.copysign(whole * lot, q) if whole else 0.0


def plan_day(meta: MetaPortfolio, books: dict[str, SleeveBook], current: dict[str, float], capital: float,
             date: str, units: dict[str, float] | None = None, last_reset: str | None = None,
             weights_seen: dict[str, float] | None = None, frozen: dict[str, str] | None = None) -> Plan:
    """The day's plan: units (kept or reset), exact and rounded targets, the orders.
    ``frozen``: ``{symbol: "hold" | "close"}``, the stale symbols (module doc)."""
    prices = merged_prices(books)
    reason = ""
    if units is None:
        reason = "first run"
    elif set(units) != {s.slug for s in meta.sleeves} or (weights_seen is not None and weights_seen != meta.weights):
        reason = "sleeves or weights changed"
    elif due_for_reset(meta.rebalance, last_reset, date):
        reason = f"{meta.rebalance} rebalance"
    if reason:
        units = sleeve_units(meta, books, capital)
    assert units is not None
    exact_all = combine(units, books)
    excluded = {s: q for s, q in exact_all.items() if not meta.tradable(s)}
    exact = {s: q for s, q in exact_all.items() if meta.tradable(s)}
    g = gross(exact, prices) / capital if capital > 0 else 0.0
    scale = 1.0
    if g > meta.max_gross:
        scale = meta.max_gross / g
        units = {k: v * scale for k, v in units.items()}
        exact = {s: q * scale for s, q in exact.items()}
        excluded = {s: q * scale for s, q in excluded.items()}
        reason = (reason + "; " if reason else "") + f"gross cap {meta.max_gross:g} (book at {g:.2f})"
    target = {s: round_lot(q, meta.lot) for s, q in exact.items()}
    if meta.lot and capital > 0 and gross(target, prices) / capital > meta.max_gross + 1e-12:
        # rounding to the nearest lot crossed the cap: round toward zero instead, which
        # never adds gross to the exact book
        target = {s: math.copysign(math.floor(abs(q) / meta.lot) * meta.lot, q) for s, q in exact.items()}
    frozen = dict(frozen or {})
    for sym, how in frozen.items():
        if how not in ("hold", "close"):
            raise ValueError(f"a stale symbol is held or closed, not {how!r}")
        target[sym] = current.get(sym, 0.0) if how == "hold" else 0.0
    target = {s: q for s, q in target.items() if q}
    orders, skipped, missing = [], [], []
    for sym in sorted(set(target) | {s for s, q in current.items() if q}):
        want, have = target.get(sym, 0.0), current.get(sym, 0.0)
        if not meta.tradable(sym):
            continue                                        # held outside the sleeves' reach: left alone
        delta = want - have
        if abs(delta) < 1e-9:
            continue
        price = prices.get(sym)
        if price is None:
            missing.append(sym)
            price = float("nan")
        order = ProposedOrder(sym, delta, price, have, want, exact.get(sym, 0.0))
        if want != 0 and math.isfinite(price) and order.notional < meta.min_order_notional:
            skipped.append(order)
        else:
            orders.append(order)
    refused = {s: str(meta.refusal(s)) for s in set(excluded) | {s for s, q in current.items() if q}
               if not meta.tradable(s)}
    plan = Plan(date=date, capital=capital, units=units, rebalanced=bool(reason), reason=reason,
                cap_scale=scale, exact=exact, target=target, orders=orders, skipped=skipped,
                excluded=excluded, refused=refused, missing_price=missing, frozen=frozen)
    plan.gross_exact = gross(exact, prices) / capital if capital > 0 else 0.0
    plan.gross_target = gross(target, prices) / capital if capital > 0 else 0.0
    plan.net_target = (sum(q * prices[s] for s, q in target.items() if s in prices) / capital
                       if capital > 0 else 0.0)
    return plan


__all__ = ["Plan", "ProposedOrder", "combine", "due_for_reset", "gross", "merged_prices", "plan_day",
           "round_lot", "sleeve_units"]
