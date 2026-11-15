"""Corporate actions from symbol tables: splits, dividends, delistings, ticker changes
(spec/05 § 5.8), and readers for PWB's datasets.

``SymbolCorporateActions`` is a ``CorporateActionProvider`` built from plain tables keyed by
symbol -- one row per split, per dividend, per delisting, per ticker change -- that resolves
its symbols onto the run's feeds when the run starts (``bind``) and hands each action to the
slice it belongs to:

* a split or a dividend at the feed's **first bar on or after the ex-date** (a feed with no
  bar that day takes it at its next bar, so a position is never scaled while its price is
  still the old one); one dated before the feed's second bar or after its last is dropped;
* a delisting at the first slice on or after the delisting date, provided the feed stopped
  trading by then (a feed with bars well past the date is another instrument under a reused
  ticker: the delisting is set aside in ``conflicts``);
* a ticker change at the first slice on or after its date.

What an action does to the holdings is the security's corporate-action model's business
(``models.OracleCorporateActions``), gated by the feed's declared normalization: on raw
prices a split scales the position and a dividend pays cash; on split-adjusted prices only
the dividend pays (and the amount must be in today's shares: ``split_adjusted_cash_amount``,
or the cash amount divided by the later splits); on adjusted prices neither moves the
holdings. Concepts read as a specification from QuantConnect Lean (Apache 2.0, ``NOTICE``):
``Split``, ``Dividend``, ``Delisting``, ``SymbolChangedEvent``, ``DataNormalizationMode``.

The ``pwb_*`` readers load the tables from the Hugging Face Hub (``huggingface_hub`` and
``pyarrow``, not dependencies of Tradewind: installed by the caller) with the symbol and
date filters and the column list pushed down to Parquet, so a run reads a few kilobytes
rather than whole datasets.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence

import numpy as np
import pandas as pd

from .dateutils import num2ns
from .models import Delisting, Dividend, Split, SymbolChange
from .universe import NS_PER_DAY, MembershipUniverse

# -- table readers -----------------------------------------------------------------------------


def _dates(values) -> np.ndarray:
    """Calendar days (``datetime64[D]``) of a column of dates; missing or "None" -> NaT."""
    s = pd.Series(pd.to_datetime(pd.Series(values, dtype=object), errors="coerce"))
    if getattr(s.dt, "tz", None) is not None:
        s = s.dt.tz_convert("UTC").dt.tz_localize(None)
    return s.dt.normalize().to_numpy(dtype="datetime64[ns]").astype("datetime64[D]")


def split_table(frame: pd.DataFrame, *, symbol: str = "symbol", date: str = "date",
                split_from: str = "split_from", split_to: str = "split_to") -> pd.DataFrame:
    """``(symbol, ex_date, ratio)`` rows from a split table, ``ratio`` = new shares per old
    (``split_to / split_from``, PWB's ``Stocks-Daily-Splits`` layout); a frame that already
    has ``ratio`` keeps it. Ratios that are not finite, not positive or equal to 1 are
    dropped."""
    if "ratio" in frame.columns:
        ratio = frame["ratio"].astype(float).to_numpy()
    else:
        ratio = (frame[split_to].astype(float) / frame[split_from].astype(float)).to_numpy()
    col = date if date in frame.columns else "ex_date"
    out = pd.DataFrame({"symbol": frame[symbol].astype(str).to_numpy(), "ex_date": _dates(frame[col]),
                        "ratio": ratio})
    out = out[np.isfinite(out["ratio"]) & (out["ratio"] > 0) & (out["ratio"] != 1.0) & out["ex_date"].notna()]
    return out.sort_values(["symbol", "ex_date"], kind="stable").reset_index(drop=True)


def dividend_table(frame: pd.DataFrame, *, symbol: str = "symbol", date: str = "date",
                   amount: str = "cash_amount", adjusted_amount: str = "split_adjusted_cash_amount",
                   pay_date: str = "pay_date", currency: str | None = "USD") -> pd.DataFrame:
    """``(symbol, ex_date, amount, adjusted_amount, pay_date)`` rows from a dividend table
    (PWB's ``Stocks-Daily-Dividends`` layout: ``date`` is the ex-date). ``currency`` keeps
    the rows paid in it when the table has a ``currency`` column (None: all)."""
    f = frame
    if {"ex_date", "amount"} <= set(f.columns):         # already in this layout
        date, amount, adjusted_amount = "ex_date", "amount", "adjusted_amount"
    if currency is not None and "currency" in f.columns:
        f = f[f["currency"].astype(str) == currency]
    n = len(f)
    adj = f[adjusted_amount].astype(float).to_numpy() if adjusted_amount in f.columns else np.full(n, np.nan)
    pay = _dates(f[pay_date]) if pay_date in f.columns else np.full(n, np.datetime64("NaT"), "datetime64[D]")
    out = pd.DataFrame({"symbol": f[symbol].astype(str).to_numpy(), "ex_date": _dates(f[date]),
                        "amount": f[amount].astype(float).to_numpy(), "adjusted_amount": adj,
                        "pay_date": pay})
    out = out[out["ex_date"].notna() & np.isfinite(out["amount"]) & (out["amount"] != 0.0)]
    return out.sort_values(["symbol", "ex_date"], kind="stable").reset_index(drop=True)


def delisting_table(frame: pd.DataFrame, *, symbol: str = "symbol", date: str | None = None,
                    last_price: str | None = None) -> pd.DataFrame:
    """``(symbol, date, last_price)`` rows of the securities delisted; ``date`` defaults to
    ``delistingDate`` (PWB's ListingStatus layout) or ``date``. Rows without a date (still
    listed) are dropped."""
    if date is None:
        date = "delistingDate" if "delistingDate" in frame.columns else "date"
    if last_price is None and "last_price" in frame.columns:
        last_price = "last_price"
    lp = frame[last_price].astype(float).to_numpy() if last_price else np.full(len(frame), np.nan)
    out = pd.DataFrame({"symbol": frame[symbol].astype(str).to_numpy(), "date": _dates(frame[date]),
                        "last_price": lp})
    out = out[out["date"].notna()]
    return out.sort_values(["symbol", "date"], kind="stable").reset_index(drop=True)


def symbol_change_table(frame: pd.DataFrame, *, date: str = "date", old: str = "old",
                        new: str = "new") -> pd.DataFrame:
    """``(date, old, new)`` rows: the ticker ``old`` trades as ``new`` from ``date``."""
    out = pd.DataFrame({"date": _dates(frame[date]), "old": frame[old].astype(str).to_numpy(),
                        "new": frame[new].astype(str).to_numpy()})
    return out[out["date"].notna()].sort_values("date", kind="stable").reset_index(drop=True)


# -- the provider ------------------------------------------------------------------------------


def _feed_times(feed) -> np.ndarray:
    """The feed's bar times, int64 ns UTC, as the clock stamps its slices."""
    dtnum = getattr(feed, "_dtnum", None)
    if dtnum is None:
        return np.empty(0, dtype=np.int64)
    return num2ns(dtnum)


def _is_daily(times: np.ndarray) -> bool:
    return bool(len(times)) and not (times % NS_PER_DAY).any()


def _bar_days(times: np.ndarray, tz: str) -> np.ndarray:
    """The trading day of each bar: its UTC date for a daily feed (stamped at midnight), its
    day in ``tz`` for an intraday feed (bars stamped at their end)."""
    if _is_daily(times):
        return (times // NS_PER_DAY).astype("datetime64[D]")
    from .intraday import _local_days

    return _local_days(pd.DatetimeIndex(times.astype("datetime64[ns]")).tz_localize("UTC"), tz)


def _day_start(day: np.datetime64, daily: bool, tz: str) -> int:
    """The instant a calendar day starts: midnight UTC for daily feeds, midnight in ``tz``."""
    if daily:
        return int(np.datetime64(day, "D").astype("datetime64[ns]").astype(np.int64))
    ts = pd.Timestamp(np.datetime64(day, "D")).tz_localize(tz).tz_convert("UTC")
    return int(ts.as_unit("ns").value)


class SymbolCorporateActions:
    """A ``CorporateActionProvider`` over tables keyed by symbol (see the module).

    ``splits``, ``dividends``, ``delistings``, ``symbol_changes``: DataFrames in the layouts
    of ``split_table``, ``dividend_table``, ``delisting_table``, ``symbol_change_table`` (or
    raw PWB tables, read through them). ``tz``: the time zone of intraday feeds' trading
    days. ``liquidate``: a delisting closes the position (True, default) at its last price
    or the feed's last close, less ``haircut``; False leaves it carried at the last close
    (the oracle). ``grace``: how long after the delisting date a feed may still trade before
    its bars are taken for another instrument under the same ticker. ``delist_ended``: also
    delist, at the slice after its last bar, a feed that stops before the end of the run
    without a delisting row (spec/02 decision 4; careful with feeds that stop because the
    data stops).

    After the run starts: ``unresolved`` (symbols without a feed), ``dropped`` (actions
    outside their feed's bars), ``conflicts`` (delistings set aside), ``actions`` (the
    resolved actions, in time order).
    """

    def __init__(self, splits: pd.DataFrame | None = None, dividends: pd.DataFrame | None = None,
                 delistings: pd.DataFrame | None = None, symbol_changes: pd.DataFrame | None = None,
                 *, tz: str = "America/New_York", liquidate: bool = True, haircut: float = 0.0,
                 grace="7D", delist_ended: bool = False):
        # every table goes through its reader (they are idempotent on their own output)
        self.splits = split_table(splits) if splits is not None else None
        self.dividends = dividend_table(dividends) if dividends is not None else None
        self.delistings = delisting_table(delistings) if delistings is not None else None
        self.symbol_changes = symbol_change_table(symbol_changes) if symbol_changes is not None else None
        self.tz = tz
        self.liquidate = bool(liquidate)
        self.haircut = float(haircut)
        self.grace = int(pd.Timedelta(grace).as_unit("ns").value)
        self.delist_ended = bool(delist_ended)
        self.unresolved: list[str] = []
        self.dropped: list[tuple] = []
        self.conflicts: list[tuple] = []
        self.actions: list = []
        self._queue: list[tuple[int, int, int, object]] = []
        self._pos = 0

    def describe(self) -> dict:
        from .fingerprint import table_digest

        out: dict = {"liquidate": self.liquidate, "haircut": self.haircut, "grace_ns": self.grace,
                     "delist_ended": self.delist_ended, "tz": self.tz}
        for name in ("splits", "dividends", "delistings", "symbol_changes"):
            t = getattr(self, name)
            if t is not None:
                out[name] = {"rows": len(t), "sha256": table_digest(*(t[c].to_numpy() for c in t.columns))}
        return out

    # -- binding ---------------------------------------------------------------------------
    def bind(self, securities) -> None:
        by_name: dict[str, list] = {}
        for security in securities:
            by_name.setdefault(str(security.symbol), []).append(security)
        named: set[str] = set()
        queue: list[tuple[int, int, int, object]] = []
        self.dropped, self.conflicts = [], []
        times = {s.id: _feed_times(s.feed) for s in securities}
        run_end = max((int(t[-1]) for t in times.values() if len(t)), default=None)
        split_groups = ({k: g for k, g in self.splits.groupby("symbol", sort=False)}
                        if self.splits is not None else {})

        for sym, rows in split_groups.items():
            named.add(sym)
            for security in by_name.get(sym, ()):
                t = times[security.id]
                days = _bar_days(t, self.tz)
                for ex, ratio in zip(rows["ex_date"].to_numpy(dtype="datetime64[D]"), rows["ratio"]):
                    k = int(np.searchsorted(days, ex, side="left"))
                    if k == 0 or k >= len(t):
                        self.dropped.append(("split", sym, str(ex)))
                        continue
                    queue.append((int(t[k]), security.id, 1, Split(security.id, np.int64(t[k]), float(ratio))))

        if self.dividends is not None:
            for sym, rows in self.dividends.groupby("symbol", sort=False):
                named.add(sym)
                splits = split_groups.get(sym)
                for security in by_name.get(sym, ()):
                    t = times[security.id]
                    days = _bar_days(t, self.tz)
                    daily = _is_daily(t)
                    for ex, amount, adj, pay in zip(rows["ex_date"].to_numpy(dtype="datetime64[D]"),
                                                    rows["amount"], rows["adjusted_amount"],
                                                    rows["pay_date"].to_numpy(dtype="datetime64[D]")):
                        k = int(np.searchsorted(days, ex, side="left"))
                        if k == 0 or k >= len(t):
                            self.dropped.append(("dividend", sym, str(ex)))
                            continue
                        per_share = self._dividend_amount(security, ex, float(amount), float(adj), splits)
                        pay_time = None if np.isnat(pay) else np.int64(_day_start(pay, daily, self.tz))
                        queue.append((int(t[k]), security.id, 2,
                                      Dividend(security.id, np.int64(t[k]), per_share, pay_time)))

        delisted: set[int] = set()
        if self.delistings is not None:
            for sym, day, last in zip(self.delistings["symbol"], self.delistings["date"].to_numpy(
                    dtype="datetime64[D]"), self.delistings["last_price"]):
                named.add(sym)
                for security in by_name.get(sym, ()):
                    t = times[security.id]
                    if not len(t):
                        continue
                    when = _day_start(day, _is_daily(t), self.tz)
                    if int(t[-1]) > when + self.grace:
                        self.conflicts.append((sym, str(day), str(t[-1].astype("datetime64[ns]"))))
                        continue
                    if security.id in delisted:
                        continue
                    delisted.add(security.id)
                    # never before the feed's last bar: its close is the price it leaves at
                    when = max(when, int(t[-1]) + 1)
                    lp = None if not np.isfinite(last) else float(last)
                    queue.append((when, security.id, 3, (lp,)))

        if self.delist_ended and run_end is not None:
            for security in securities:
                t = times[security.id]
                if len(t) and int(t[-1]) < run_end and security.id not in delisted:
                    delisted.add(security.id)
                    queue.append((int(t[-1]) + 1, security.id, 3, (None,)))

        if self.symbol_changes is not None:
            for day, old, new in zip(self.symbol_changes["date"].to_numpy(dtype="datetime64[D]"),
                                     self.symbol_changes["old"], self.symbol_changes["new"]):
                named.update((old, new))
                for security in by_name.get(old, []) + by_name.get(new, []):
                    t = times[security.id]
                    if not len(t):
                        continue
                    when = _day_start(day, _is_daily(t), self.tz)
                    if when > int(t[-1]):
                        self.dropped.append(("symbol_change", old, str(day)))
                        continue
                    queue.append((when, security.id, 0, (old, new)))

        queue.sort(key=lambda q: (q[0], q[1], q[2]))
        self._queue, self._pos = queue, 0
        self.actions = []
        self.unresolved = sorted(s for s in named if s not in by_name)

    @staticmethod
    def _dividend_amount(security, ex, amount: float, adjusted: float, splits) -> float:
        """Per share, in the basis of the feed's prices: the cash amount on raw prices; on
        split-adjusted (or adjusted) prices the amount in today's shares -- the table's
        split-adjusted amount, or the cash amount divided by the ratios of the later splits."""
        if security.normalization in (None, "raw"):
            return amount
        if np.isfinite(adjusted):
            return adjusted
        if splits is None or not len(splits):
            return amount
        later = splits["ex_date"].to_numpy(dtype="datetime64[D]") > ex
        return amount / float(np.prod(splits["ratio"].to_numpy(dtype=np.float64)[later]))

    # -- the provider protocol ------------------------------------------------------------
    def events(self, time) -> Sequence:
        queue, pos = self._queue, self._pos
        now = int(time)
        if pos >= len(queue) or queue[pos][0] > now:
            return ()
        out = []
        while pos < len(queue) and queue[pos][0] <= now:
            _, sid, rank, payload = queue[pos]
            pos += 1
            stamp = np.int64(now)
            if rank == 3:
                action = Delisting(sid, stamp, payload[0], self.liquidate, self.haircut)
            elif rank == 0:
                action = SymbolChange(sid, stamp, payload[0], payload[1])
            else:
                action = payload
            out.append(action)
        self._pos = pos
        self.actions.extend(out)
        return out


# -- PWB datasets on the Hub --------------------------------------------------------------------

PWB_REPO = "paperswithbacktest"
PWB_SPLITS = "Stocks-Daily-Splits"
PWB_DIVIDENDS = "Stocks-Daily-Dividends"
PWB_LISTING_STATUS = "AlphaVantage-Stocks-Daily-ListingStatus"
PWB_LIQUID_UNIVERSE = "Stocks-Daily-LiquidUniverse"


def _token(token):
    return token or os.environ.get("HF_ACCESS_TOKEN") or os.environ.get("HF_TOKEN")


def load_hub_table(dataset: str, *, symbols: Sequence[str] | None = None, start=None, end=None,
                   columns: Sequence[str] | None = None, date: str | None = "date",
                   token: str | None = None, repo: str = PWB_REPO, revision: str | None = None,
                   max_rows: int | None = 5_000_000) -> pd.DataFrame:
    """A bounded read of a Parquet dataset on the Hub: only the files of the years or months
    the window touches when the dataset is sharded so (``train-2017.parquet``,
    ``train-2017-06.parquet``), only ``columns``, the ``symbols`` and the ``[start, end]``
    window pushed down to Parquet.
    ``date=None`` reads no date filter. Raises before returning more than ``max_rows``."""
    import pyarrow.dataset as pads
    from huggingface_hub import HfApi, hf_hub_download

    token = _token(token)
    repo_id = f"{repo}/{dataset}"
    files = sorted(f for f in HfApi(token=token).list_repo_files(repo_id, repo_type="dataset",
                                                                 revision=revision)
                   if f.endswith(".parquet"))
    lo = None if start is None else pd.Timestamp(start)
    hi = None if end is None else pd.Timestamp(end)

    def wanted(name: str) -> bool:
        # shards by year (``train-2017``) or by month (``train-2017-06``): only those the
        # window touches; any other file is read
        m = re.search(r"-((?:19|20)\d{2})(?:-(\d{2}))?$", os.path.basename(name).rsplit(".", 1)[0])
        if m is None:
            return True
        y = int(m.group(1))
        first = pd.Timestamp(y, int(m.group(2) or 1), 1)
        last = first + (pd.offsets.MonthEnd(1) if m.group(2) else pd.offsets.YearEnd(1))
        return (lo is None or last >= lo.normalize()) and (hi is None or first <= hi)

    frames = []
    for name in filter(wanted, files):
        path = hf_hub_download(repo_id, name, repo_type="dataset", token=token, revision=revision)
        data = pads.dataset(path, format="parquet")
        expr = None
        if symbols is not None:
            expr = pads.field("symbol").isin([str(s) for s in symbols])
        if date is not None and (lo is not None or hi is not None):
            kind = data.schema.field(date).type
            # ISO dates stored as text compare as text
            as_text = str(kind) in ("string", "large_string")
            if lo is not None:
                e = pads.field(date) >= (lo.strftime("%Y-%m-%d") if as_text else lo)
                expr = e if expr is None else expr & e
            if hi is not None:
                e = pads.field(date) <= (hi.strftime("%Y-%m-%d") if as_text else hi)
                expr = e if expr is None else expr & e
        cols = list(columns) if columns is not None else None
        table = data.to_table(columns=cols, filter=expr)
        if max_rows is not None and table.num_rows + sum(len(f) for f in frames) > max_rows:
            raise MemoryError(f"{dataset}: more than {max_rows} rows for this window; narrow it")
        frames.append(table.to_pandas())
    if not frames:
        return pd.DataFrame(columns=list(columns) if columns is not None else None)
    return pd.concat(frames, ignore_index=True)


def pwb_corporate_actions(symbols: Sequence[str], start=None, end=None, *, splits: bool = True,
                          dividends: bool = True, delistings: bool = True, token: str | None = None,
                          **kwargs) -> SymbolCorporateActions:
    """``SymbolCorporateActions`` for ``symbols`` from PWB's ``Stocks-Daily-Splits``,
    ``Stocks-Daily-Dividends`` and ListingStatus, read for the ``[start, end]`` window (the
    delistings: those dated in it). ``kwargs`` go to ``SymbolCorporateActions``."""
    sp = dv = dl = None
    if splits:
        sp = load_hub_table(PWB_SPLITS, symbols=symbols, start=start, end=end, token=token,
                            columns=["symbol", "date", "split_from", "split_to"])
    if dividends:
        dv = load_hub_table(PWB_DIVIDENDS, symbols=symbols, start=start, end=end, token=token,
                            columns=["symbol", "date", "pay_date", "cash_amount",
                                     "split_adjusted_cash_amount", "currency"])
    if delistings:
        dl = load_hub_table(PWB_LISTING_STATUS, symbols=symbols, date="delistingDate", start=start,
                            end=end, token=token, columns=["symbol", "delistingDate"])
    return SymbolCorporateActions(
        split_table(sp) if sp is not None else None,
        dividend_table(dv) if dv is not None else None,
        delisting_table(dl) if dl is not None else None, **kwargs)


def pwb_liquid_universe(start, end, *, top: int = 500, symbols: Sequence[str] | None = None,
                        token: str | None = None, **kwargs) -> MembershipUniverse:
    """A ``MembershipUniverse`` from PWB's ``Stocks-Daily-LiquidUniverse`` (ranks by 60-session
    median dollar volume, one row per member and session) for ``[start, end]``, ranks 1 to
    ``top``. ``kwargs`` go to ``MembershipUniverse`` (``tolerance="7D"`` as pwb-toolbox)."""
    table = load_hub_table(PWB_LIQUID_UNIVERSE, symbols=symbols, start=start, end=end, token=token,
                           columns=["date", "symbol", "rank"])
    table = table[table["rank"] <= top]
    return MembershipUniverse(table, **kwargs)


__all__ = [
    "PWB_DIVIDENDS", "PWB_LIQUID_UNIVERSE", "PWB_LISTING_STATUS", "PWB_SPLITS", "SymbolCorporateActions",
    "delisting_table", "dividend_table", "load_hub_table", "pwb_corporate_actions", "pwb_liquid_universe",
    "split_table", "symbol_change_table",
]
