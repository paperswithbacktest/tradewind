"""The day's cost of borrowing the shorts.

Every short of the day, proposed (the target book) or held (the account), gets the broker's
borrow fee and the shares it can lend, and its cost of carry:
``fee (% a year) x short value / 365`` a day. A short is flagged when it is hard to borrow
(a fee at or above ``borrow_fee_alert``, 1 % a year by default: general collateral costs
0.25-0.5 % in USD) or when the broker has nothing, or not enough, to lend.

Sources, in the order ``auto`` tries them:

- ``hub``: Interactive Brokers' own short-stock file (the indicative fee, the rebate and the
  shares available, one row per contract), as the private dataset
  ``paperswithbacktest/InteractiveBrokers-Daily-ShortAvailability`` keeps it, one snapshot a
  US business day (the latest is used). It works without TWS / IB Gateway. The file lists
  every stock the broker can lend: a short that is not in it has no availability.
- ``ib``: IB's shortable tick (236) read on the IBKR account (read-only, delayed data
  accepted). It gives a level, not a fee: above 2.5 easy to borrow, 1.5-2.5 hard (a
  locate), 1.5 or less none. Its costs are then unknown.

``auto`` uses the hub, and IB's tick when the hub cannot be read (no network, no access to
the dataset, no ``huggingface_hub``). Neither: every short is ``unknown`` and the run goes on.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

HUB_REPO = "paperswithbacktest/InteractiveBrokers-Daily-ShortAvailability"
DEFAULT_FEE_ALERT = 1.0          # % a year: at or above it, a short is hard to borrow
SNAPSHOT_MAX_AGE_DAYS = 4        # an older snapshot is used, with a note
SOURCES = ("auto", "hub", "ib", "none")
ALERT_STATUSES = ("hard_to_borrow", "no_availability", "insufficient")


def _key(symbol: str) -> str:
    """One spelling for a share class: ``BRK B`` (IB), ``BRK.B``, ``BRK-B`` and ``BRK/B``."""
    return re.sub(r"[ ./\-]+", ".", str(symbol).strip().upper())


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


@dataclass
class BorrowQuote:
    symbol: str
    fee_rate: float | None = None       # indicative borrow fee, % a year
    rebate_rate: float | None = None    # indicative rebate, % a year
    available: float | None = None      # shares the broker can lend
    available_capped: bool = False      # published as ">10000000"
    shortable: float | None = None      # IB's shortable tick (236)


@dataclass
class BorrowSnapshot:
    source: str                          # where the quotes come from, said in the report
    as_of: str = ""                      # the snapshot's date
    quotes: dict = field(default_factory=dict)  # {symbol as asked: BorrowQuote}
    listed: bool = False                 # True: a symbol absent from the quotes has no availability
    notes: list = field(default_factory=list)


# -- sources -----------------------------------------------------------------------------------------
class HubBorrowSource:
    """The latest snapshot of IB's short-stock file in the PWB dataset (``country``: the file,
    ``usa`` for US stocks and ETFs). ``list_files`` and ``read`` replace the Hub in tests:
    ``list_files() -> [path]``, ``read(path) -> [row dict]``."""

    name = "hub"

    def __init__(self, repo: str = HUB_REPO, country: str = "usa", *, list_files=None, read=None):
        self.repo = repo
        self.country = country
        self._list_files = list_files or self._hub_files
        self._read = read or self._hub_read
        self._cache: tuple[str, dict] | None = None

    def _hub_files(self) -> list[str]:
        from huggingface_hub import HfApi

        return list(HfApi().list_repo_files(self.repo, repo_type="dataset"))

    def _hub_read(self, path: str) -> list[dict]:
        import pandas as pd
        from huggingface_hub import hf_hub_download

        return pd.read_parquet(hf_hub_download(self.repo, path, repo_type="dataset")).to_dict("records")

    def latest(self) -> tuple[str, str]:
        pat = re.compile(rf"^data/{re.escape(self.country)}/\d{{4}}/(\d{{4}}-\d{{2}}-\d{{2}})\.parquet$")
        dated = sorted((m.group(1), f) for f in self._list_files() if (m := pat.match(f)))
        if not dated:
            raise RuntimeError(f"no {self.country} snapshot in {self.repo}")
        return dated[-1]

    def table(self) -> tuple[str, dict]:
        """``(date, {key: BorrowQuote})`` of the latest snapshot, read once."""
        if self._cache is None:
            date, path = self.latest()
            best: dict[str, BorrowQuote] = {}
            for r in self._read(path):
                if str(r.get("currency") or "USD").upper() != "USD":
                    continue
                q = BorrowQuote(str(r["symbol"]), fee_rate=_num(r.get("fee_rate")),
                                rebate_rate=_num(r.get("rebate_rate")), available=_num(r.get("available")),
                                available_capped=bool(r.get("available_capped") or False))
                k = _key(q.symbol)
                if k not in best or (q.available or 0) > (best[k].available or 0):   # two lines: the larger
                    best[k] = q
            self._cache = (date, best)
        return self._cache

    def quotes(self, symbols, day: str, account=None) -> BorrowSnapshot:
        date, table = self.table()
        notes = []
        try:
            import datetime as _dt

            age = (_dt.date.fromisoformat(day) - _dt.date.fromisoformat(date)).days
            if age > SNAPSHOT_MAX_AGE_DAYS:
                notes.append(f"the borrow snapshot is from {date}, {age} days before {day}")
        except ValueError:
            pass
        found = {s: table[_key(s)] for s in symbols if _key(s) in table}
        return BorrowSnapshot(f"IB short-stock file ({self.repo}, {self.country})", date, found, listed=True,
                              notes=notes)


class IBShortableSource:
    """IB's shortable tick (236) on the account of the run (``account.shortable``)."""

    name = "ib"

    def quotes(self, symbols, day: str, account=None) -> BorrowSnapshot:
        check = getattr(account, "shortable", None)
        if check is None:
            raise RuntimeError("this account cannot read IB's shortable tick")
        levels = check(list(symbols)) if symbols else {}
        found = {s: BorrowQuote(s, shortable=float(v)) for s, v in levels.items() if _num(v) is not None}
        return BorrowSnapshot("IB shortable tick 236 (a level, no fee)", day, found, listed=False)


class AutoBorrowSource:
    """The first source that answers; what failed before it goes into the notes."""

    name = "auto"

    def __init__(self, sources):
        self.sources = list(sources)

    def quotes(self, symbols, day: str, account=None) -> BorrowSnapshot:
        failed = []
        for src in self.sources:
            try:
                snap = src.quotes(symbols, day, account)
            except Exception as exc:  # noqa: BLE001 - the next source is tried
                failed.append(f"{src.name}: {type(exc).__name__}: {exc}")
                continue
            snap.notes = [f"borrow source {f} (next source used)" for f in failed] + list(snap.notes)
            return snap
        raise RuntimeError("no borrow source answered: " + "; ".join(failed))


def borrow_source(choice: str):
    """The source named in the meta-portfolio (``borrow_source``) or on the command line."""
    choice = (choice or "none").lower()
    if choice not in SOURCES:
        raise ValueError(f"borrow_source is one of {SOURCES}, not {choice!r}")
    if choice == "none":
        return None
    if choice == "hub":
        return HubBorrowSource()
    if choice == "ib":
        return IBShortableSource()
    return AutoBorrowSource([HubBorrowSource(), IBShortableSource()])


# -- the day's report --------------------------------------------------------------------------------
def _status(q: BorrowQuote | None, listed: bool, qty: float, fee_alert: float) -> tuple[str, str]:
    if q is None:
        if listed:
            return "no_availability", "not in the broker's list of stocks it can lend"
        return "unknown", "no borrow data"
    if q.available is not None and q.available <= 0:
        return "no_availability", "0 shares available"
    if q.shortable is not None and q.shortable <= 1.5:
        return "no_availability", f"IB shortable {q.shortable:g} (none to borrow)"
    if q.available is not None and not q.available_capped and qty > q.available:
        return "insufficient", f"{q.available:,.0f} shares available for a short of {qty:,.0f}"
    if q.fee_rate is not None and q.fee_rate >= fee_alert:
        return "hard_to_borrow", f"fee {q.fee_rate:.2f} %/year, at or above {fee_alert:g} %"
    if q.shortable is not None and q.shortable <= 2.5:
        return "hard_to_borrow", f"IB shortable {q.shortable:g} (a locate is needed)"
    return "ok", ""


def borrow_report(target: dict, held: dict, closes: dict, snapshot: BorrowSnapshot | None, *,
                  fee_alert: float = DEFAULT_FEE_ALERT, skip=None, error: str = "") -> dict:
    """Each short, proposed or held, with its fee, availability and cost of carry (USD).

    The short carried is the target's when the target is short, otherwise the held short
    (closed at the next open: it is still carried one more night)."""
    skip = skip or (lambda s: False)
    shorts = sorted(s for s in set(target) | set(held)
                    if (target.get(s, 0.0) < 0 or held.get(s, 0.0) < 0) and not skip(s))
    lines = []
    for s in shorts:
        t, h = float(target.get(s, 0.0)), float(held.get(s, 0.0))
        qty = -t if t < 0 else -h
        px = _num(closes.get(s))
        value = qty * px if px is not None else None
        q = snapshot.quotes.get(s) if snapshot is not None else None
        status, why = _status(q, snapshot is not None and snapshot.listed, qty, fee_alert)
        fee = q.fee_rate if q is not None else None
        daily = value * fee / 100.0 / 365.0 if fee is not None and value is not None else None
        lines.append({"symbol": s, "target": t, "held": h, "short_quantity": qty, "close": px,
                      "short_value": value, "fee_rate": fee, "fee_bps_year": None if fee is None else fee * 100.0,
                      "rebate_rate": None if q is None else q.rebate_rate,
                      "available": None if q is None else q.available,
                      "available_capped": False if q is None else q.available_capped,
                      "shortable": None if q is None else q.shortable,
                      "daily_cost": daily, "status": status, "why": why})
    costs = [x["daily_cost"] for x in lines if x["daily_cost"] is not None]
    return {"source": snapshot.source if snapshot is not None else "none",
            "as_of": snapshot.as_of if snapshot is not None else "",
            "fee_alert_pct": fee_alert, "notes": (list(snapshot.notes) if snapshot is not None else [])
            + ([error] if error else []),
            "lines": lines, "daily_cost": sum(costs), "annual_cost": 365.0 * sum(costs),
            "priced": len(costs), "shorts": len(lines)}


def alert_lines(borrow: dict) -> list[dict]:
    return [x for x in (borrow or {}).get("lines", []) if x["status"] in ALERT_STATUSES]


def markdown(borrow: dict) -> list[str]:
    """The section of the proposal and of the report of the day."""
    if not borrow or not borrow.get("lines"):
        return []
    md = ["## Short borrow (cost of carry)", "",
          (f"- source: {borrow['source']}" + (f", snapshot {borrow['as_of']}" if borrow.get("as_of") else "")
           + f"; hard to borrow at a fee of {borrow['fee_alert_pct']:g} %/year or more"),
          (f"- {borrow['shorts']} short(s), {borrow['priced']} priced: {borrow['daily_cost']:,.2f} USD a day, "
           f"{borrow['annual_cost']:,.0f} USD a year at today's book")]
    md += [f"- {n}" for n in borrow.get("notes", [])]
    md += ["", "| symbol | short (target / held) | short value | fee bps/yr | available | cost a day | status |",
           "|---|---:|---:|---:|---:|---:|---|"]

    def f(x, fmt):
        return "n/a" if x is None else format(x, fmt)

    for x in borrow["lines"]:
        avail = ("> 10,000,000" if x["available_capped"] else f(x["available"], ",.0f"))
        if x["available"] is None and x["shortable"] is not None:
            avail = f"IB level {x['shortable']:g}"
        state = x["status"] + (f": {x['why']}" if x["why"] else "")
        md.append(f"| {x['symbol']} | {x['target']:+g} / {x['held']:+g} | {f(x['short_value'], ',.0f')} | "
                  f"{f(x['fee_bps_year'], ',.0f')} | {avail} | {f(x['daily_cost'], ',.2f')} | {state} |")
    md.append("")
    return md


__all__ = ["ALERT_STATUSES", "DEFAULT_FEE_ALERT", "HUB_REPO", "SOURCES", "AutoBorrowSource", "BorrowQuote",
           "BorrowSnapshot", "HubBorrowSource", "IBShortableSource", "alert_lines", "borrow_report",
           "borrow_source", "markdown"]
