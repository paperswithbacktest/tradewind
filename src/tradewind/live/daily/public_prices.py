"""Public daily series for the signals the fresh-price top-up has no market source for.

Two catalogue datasets have no daily source in ``fresh.py``: ``Bonds-Daily-Price`` (bond
price indices, ``US10Y`` ...) and the indices without a tracking ETF (``VIX``). A strategy
that only *reads* them -- a yield differential, a volatility gate -- runs on a series frozen at
the dataset's last monthly refresh. Some of them have a public daily equivalent at FRED
(Federal Reserve Bank of St. Louis, ``fredgraph.csv``, no key):

=============  ==========================  ==================================================
catalogue      FRED series                 conversion
=============  ==========================  ==================================================
``US10Y``      ``DGS10`` (10-year CMT, %)  the dataset's price index: ``100 / (1 + y)^10``
``VIX``        ``VIXCLS`` (close)          none: the same index level
=============  ==========================  ==================================================

FRED has no daily 10-year yield for the other countries of the catalogue (``DE10Y``,
``JP10Y``, ``GB10Y``, ``AU10Y``, ``CA10Y``, ``CH10Y``): only the monthly OECD series
(``IRLTLT01DEM156N`` ...), which cannot continue a daily series. They stay stale, and the
stale reason says so (``NO_PUBLIC_DAILY``).

**The junction.** The public values are put in the catalogue's unit (a yield becomes the
dataset's price index), then chained on the dataset like the other fresh sources: the anchor
is the last common day before the dataset's last bar, the factor ``k`` is the *median* of the
ratio catalogue / public over the last ``check_days`` common days up to it (a median: one bad
day in either series does not move the junction), and the bars after the anchor are the
public values times ``k``. The check, on those days:

* ``offset`` = ``|k - 1|``: the two series must be the same series. Above ``max_offset``
  (0.5 % for an index level, 2 % for a 10-year price, about 20 basis points of yield) the
  splice is refused and the symbol stays stale, cause ``diverged``;
* ``median gap`` = the median of ``|ratio / k - 1|``: above ``tolerance`` (0.5 %) the splice
  is refused the same way;
* ``max gap``: the largest ``|ratio / k - 1|``. Above ``alert`` (2 %) the splice is kept --
  the median says the series agree -- but ``PublicSplice.alert`` is set with the days, and
  the run raises a ``public_series_gap`` alert: one of the two series has a bad day.

For a yield, the gap is also given in basis points of yield (``yield_gap_bp``, median and
largest, from the catalogue's price turned back into a yield): 1 bp of a 10-year yield is
about 0.095 % of its price.

**Timing.** FRED publishes a day's value the next business day: at the evening run the last
value is usually the session before. The symbol is then still stale, cause ``lagged`` -- the
signal is one day old instead of a month. Responses are cached per session
(``CACHE/SESSION/public/ID.csv``); a cached answer that does not reach the session is asked
again after ``REFRESH_SECONDS``.
"""

from __future__ import annotations

import csv
import io
import math
import os
import statistics
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

import pandas as pd

URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"
REFRESH_SECONDS = 3600.0         # a cached answer short of the session is asked again after this
CHECK_DAYS = 20                  # common days, up to the anchor, the junction is checked on
TOLERANCE = 0.005                # the median gap above which the splice is refused
ALERT = 0.02                     # the largest gap above which the run raises an alert
HISTORY_DAYS = 120               # calendar days of the public series asked before the dataset's end


@dataclass(frozen=True)
class PublicSeries:
    """A catalogue symbol and its public daily equivalent. ``unit``: ``level`` (the same
    number) or ``yield`` (a yield in percent, turned into ``100 / (1 + y)^tenor``)."""

    dataset: str
    symbol: str
    series: str
    unit: str = "level"
    tenor: float = 0.0
    max_offset: float = 0.005

    def to_catalogue(self, value: float) -> float:
        if self.unit == "yield":
            return 100.0 / (1.0 + value / 100.0) ** self.tenor
        return value

    def yield_of(self, price: float) -> float:
        """A catalogue price back into a yield in percent (``yield`` only)."""
        return ((100.0 / price) ** (1.0 / self.tenor) - 1.0) * 100.0


#: the public daily series, by (dataset, symbol)
PUBLIC_SERIES = {
    ("Bonds-Daily-Price", "US10Y"): PublicSeries("Bonds-Daily-Price", "US10Y", "DGS10", "yield", 10.0, 0.02),
    ("Indices-Daily-Price", "VIX"): PublicSeries("Indices-Daily-Price", "VIX", "VIXCLS", "level", 0.0, 0.005),
}
#: catalogue series checked for a public daily equivalent and without one (said in the stale reason)
NO_PUBLIC_DAILY = {
    ("Bonds-Daily-Price", s): "FRED has only a monthly series for it (OECD long-term rate)"
    for s in ("DE10Y", "JP10Y", "GB10Y", "AU10Y", "CA10Y", "CH10Y")
}


def public_series(dataset: str, symbol: str) -> PublicSeries | None:
    return PUBLIC_SERIES.get((dataset, symbol))


def parse_csv(text: str) -> pd.DataFrame:
    """``date, value`` (ISO dates, oldest first) from a ``fredgraph.csv`` answer; missing
    values (``.`` or empty) are dropped."""
    rows = []
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 2 or not str(header[0]).lower().startswith(("observation_date", "date")):
        raise ValueError("not a FRED csv answer")
    for rec in reader:
        if len(rec) < 2:
            continue
        try:
            v = float(rec[1])
        except ValueError:
            continue
        if math.isfinite(v):
            rows.append((rec[0][:10], v))
    frame = pd.DataFrame(rows, columns=["date", "value"])
    return frame.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def _http_get(url: str, timeout: float = 30.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


class FredClient:
    """Daily public series with a per-session disk cache. ``get``: ``url -> text`` (tests pass
    a fake one); ``wall``: the clock the cache's age is read on."""

    def __init__(self, cache_dir: str, session: str, *, get: Callable[[str], str] | None = None,
                 wall: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 log=print, retries: int = 2):
        self.dir = os.path.join(os.path.expanduser(cache_dir), session, "public")
        self.session = session
        self.get = get or _http_get
        self.wall = wall
        self.log = log
        self.retries = retries
        self.sleep = sleep
        self.calls = 0

    def daily(self, series: str, start: str) -> pd.DataFrame:
        path = os.path.join(self.dir, f"{series}.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            frame = parse_csv(text)
            fresh = self.wall() - os.path.getmtime(path) < REFRESH_SECONDS
            covers = not frame.empty and frame["date"].iloc[0] <= start
            if covers and (fresh or frame["date"].iloc[-1] >= self.session):
                return frame
        url = URL.format(series=series, start=start)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                self.calls += 1
                text = self.get(url)
                frame = parse_csv(text)
                break
            except Exception as exc:  # noqa: BLE001 - retried, then said to the caller
                last = exc
                if attempt < self.retries:
                    self.sleep(2.0 * (attempt + 1))
        else:
            raise RuntimeError(f"FRED {series} not read: {type(last).__name__}: {last}")
        os.makedirs(self.dir, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        return frame


@dataclass
class PublicSplice:
    symbol: str
    series: str
    status: str = "current"         # "filled", "refused", "current"
    last_before: str = ""
    last_after: str = ""
    bars: int = 0
    anchor: str = ""
    factor: float = math.nan        # median of catalogue / public on the checked days
    offset: float = math.nan        # |factor - 1|
    median_gap: float = math.nan    # median of |ratio / factor - 1|
    max_gap: float = math.nan       # largest of |ratio / factor - 1|
    checked_days: int = 0
    yield_gap_bp: dict = field(default_factory=dict)   # yield only: {median, max} in basis points
    alert: str = ""                 # set when the largest gap is above the alert threshold
    reason: str = ""


def splice_public(rows: pd.DataFrame, public: pd.DataFrame, spec: PublicSeries, session: str, *,
                  check_days: int = CHECK_DAYS, tolerance: float = TOLERANCE,
                  alert: float = ALERT) -> tuple[pd.DataFrame, PublicSplice]:
    """One catalogue symbol's rows continued with a public series (``parse_csv``) -- the
    module's doc. Returns the new rows and what was done."""
    rows = rows.copy()
    rows = rows.assign(_iso=rows["date"].map(lambda d: str(d)[:10])).sort_values("_iso")
    level_col = "adj_close" if "adj_close" in rows.columns else "close"
    last_before = str(rows["_iso"].iloc[-1])
    out = PublicSplice(spec.symbol, spec.series, last_before=last_before, last_after=last_before)
    pub = public[public["date"] <= session].copy()
    pub["level"] = [spec.to_catalogue(v) for v in pub["value"]]
    pub = pub[pub["level"] > 0]
    if pub.empty or pub["date"].iloc[-1] <= last_before:
        return rows.drop(columns="_iso"), out
    level = rows.set_index("_iso")[level_col]
    level = level[~level.index.duplicated(keep="last")]
    pub = pub.set_index("date")
    common = [d for d in pub.index if d in level.index and level[d] > 0]
    if not common:
        out.status, out.reason = "refused", f"no common day with FRED {spec.series}"
        return rows.drop(columns="_iso"), out
    before_last = [d for d in common if d < last_before]
    anchor = before_last[-1] if before_last else common[-1]
    days = [d for d in common if d <= anchor][-check_days:]
    ratios = {d: float(level[d] / pub.loc[d, "level"]) for d in days}
    k = statistics.median(ratios.values())
    gaps = {d: abs(r / k - 1.0) for d, r in ratios.items()}
    out.anchor, out.factor, out.offset, out.checked_days = anchor, k, abs(k - 1.0), len(days)
    out.median_gap, out.max_gap = statistics.median(gaps.values()), max(gaps.values())
    if spec.unit == "yield":
        bp = [abs(spec.yield_of(float(level[d])) - float(pub.loc[d, "value"])) * 100.0 for d in days]
        out.yield_gap_bp = {"median": statistics.median(bp), "max": max(bp)}
    if out.offset > spec.max_offset or out.median_gap > tolerance:
        out.status = "refused"
        out.reason = (f"{spec.symbol} and FRED {spec.series} disagree over {len(days)} common days: factor "
                      f"{k:.6f} (offset {out.offset:.2%}, at most {spec.max_offset:.2%}), median gap "
                      f"{out.median_gap:.2%} (at most {tolerance:.2%})")
        return rows.drop(columns="_iso"), out
    if out.max_gap > alert:
        bad = sorted((d for d, g in gaps.items() if g > alert), reverse=True)
        out.alert = (f"{spec.symbol} against FRED {spec.series}: gap up to {out.max_gap:.1%} on "
                     + ", ".join(f"{d} ({gaps[d]:.1%})" for d in bad[:5])
                     + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
                     + f" (median {out.median_gap:.2%} over {len(days)} days: spliced)")
    new = pub[pub.index > anchor]
    template = rows.iloc[-1]
    records = []
    for day, bar in new.iterrows():
        rec = {}
        for col in rows.columns:
            if col == "_iso":
                continue
            v = template[col]
            rec[col] = math.nan if isinstance(v, (int, float)) and not isinstance(v, bool) else v
        value = float(bar["level"]) * k
        for col in ("open", "high", "low", "close", "adj_close"):
            if col in rows.columns:
                rec[col] = value                      # a close-only series: one value per day
        if "volume" in rows.columns:
            rec["volume"] = 0.0
        rec["symbol"] = template["symbol"]
        rec["date"] = _like(str(day), template["date"])
        records.append(rec)
    kept = rows[rows["_iso"] <= anchor].drop(columns="_iso")
    added = pd.DataFrame(records, columns=kept.columns)
    for col in ("open", "high", "low", "close", "adj_close", "volume"):
        if col not in added:
            continue
        added[col] = pd.to_numeric(added[col], errors="coerce")
        if pd.api.types.is_integer_dtype(kept[col]):
            added[col] = added[col].fillna(0).round().astype(kept[col].dtype)
    joined = pd.concat([kept, added], ignore_index=True)
    out.status, out.bars, out.last_after = "filled", len(added), str(new.index[-1])
    return joined, out


def _like(value, template):
    from .fresh import _like as like

    return like(value, template)


__all__ = ["ALERT", "CHECK_DAYS", "NO_PUBLIC_DAILY", "PUBLIC_SERIES", "TOLERANCE", "FredClient", "PublicSeries",
           "PublicSplice", "parse_csv", "public_series", "splice_public"]
