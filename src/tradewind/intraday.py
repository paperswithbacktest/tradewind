"""Minute data: a bounded loader and stock splits on an unadjusted minute stream (spec/06 § 3-4).

``load_minute_bars`` reads monthly Parquet files (one file per month, every symbol in it,
the layout of PWB's ``Stocks-1Min-Price`` and ``Cryptocurrencies-1Min-Price``) for a few
symbols and a date window only: it opens only the months the window touches, pushes the
symbol and date filters down to Parquet, reads only the price columns, and stops with an
error before a ``max_rows`` cap is crossed. The engine then holds the window in memory
(about 56 bytes per bar and feed): choosing the window is how a minute backtest stays
bounded.

Splits: a minute series that is not adjusted for splits (the case of ``Stocks-1Min-Price``,
whose ``adj_*`` columns are not split-adjusted) either gets its prices adjusted backward
(``adjust_for_splits``: the series a daily adjusted feed would give) or keeps its raw
prices and the engine applies each split to holdings and open orders at the first bar of
the ex-date (``split_actions``: phase 3, spec/05 § 5.8). Split tables are read from a frame
with one row per split (``symbol``, ex-date, ``split_from``, ``split_to``), the layout of
PWB's ``Stocks-Daily-Splits``.
"""

from __future__ import annotations

import datetime as _dt
import os
from collections.abc import Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .models import Split

NS_PER_DAY = 86_400_000_000_000

#: column names of PWB's minute files -> the feed's
PWB_MINUTE_COLUMNS = {"datetime": "datetime", "open": "adj_open", "high": "adj_high",
                      "low": "adj_low", "close": "adj_close", "volume": "volume"}


def _months(start: pd.Timestamp, end: pd.Timestamp) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_files(directory: str, pattern: str = "train-{month}.parquet") -> Callable[[str], str | None]:
    """A resolver ``month -> path`` over a directory of monthly files (None when absent)."""

    def resolve(month: str) -> str | None:
        path = os.path.join(directory, pattern.format(month=month))
        return path if os.path.exists(path) else None

    return resolve


def hub_month_files(repo_id: str, token: str | None = None, revision: str | None = None,
                    pattern: str = "data/train-{month}.parquet") -> Callable[[str], str | None]:
    """A resolver that downloads one monthly file of a Hugging Face dataset on demand (the
    Hub's cache keeps it). Needs ``huggingface_hub``, which Tradewind does not depend on."""
    from huggingface_hub import hf_hub_download  # optional dependency

    def resolve(month: str) -> str | None:
        return hf_hub_download(repo_id, pattern.format(month=month), repo_type="dataset",
                               token=token, revision=revision)

    return resolve


def load_minute_bars(files, symbols: Sequence[str], start, end, *, tz: str = "America/New_York",
                     columns: Mapping[str, str] | None = None, stamped: str = "start",
                     bar: str = "1min", session: tuple[_dt.time, _dt.time] | None = None,
                     max_rows: int = 5_000_000) -> dict[str, pd.DataFrame]:
    """One OHLCV frame per symbol (UTC index, bars stamped at their **end**) for the window
    ``[start, end]`` (dates or datetimes in ``tz``; a bare ``end`` date includes that day).

    ``files``: a resolver ``month -> path`` (``month_files``, ``hub_month_files``) or a
    mapping ``"YYYY-MM" -> path``. The files' timestamps are naive wall-clock times of ``tz``
    (``Stocks-1Min-Price``: New York, 04:00-20:00) stamped at the bar's start (``stamped``,
    the vendor convention) or end; they are shifted to the end and converted to UTC.
    ``session`` keeps only the bars inside (open, close], e.g. ``(time(9, 30), time(16))``
    for the regular session. ``max_rows`` caps the rows read across all months: the loader
    raises instead of exhausting the memory.
    """
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    cols = dict(PWB_MINUTE_COLUMNS if columns is None else columns)
    resolve = files.get if isinstance(files, Mapping) else files
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end)
    if hi == hi.normalize() and not (isinstance(end, str) and len(end) > 10):
        hi = hi + pd.Timedelta(days=1)               # a bare end date includes its day
    step = pd.Timedelta(bar)
    # filter on the files' own (start-stamped) wall-clock strings: lexicographic = chronological
    shift = step if stamped == "start" else pd.Timedelta(0)
    lo_s = (lo - shift).strftime("%Y-%m-%d %H:%M:%S")
    hi_s = (hi - shift).strftime("%Y-%m-%d %H:%M:%S")
    wanted = [cols[k] for k in ("datetime", "open", "high", "low", "close", "volume") if k in cols]
    parts, rows = [], 0
    # files are cut by UTC month while their stamps are wall-clock times (PWB's crypto file
    # of 2020-06 starts at 2020-05-31 20:00 New York): a day of margin on each side
    for month in _months(lo - shift - pd.Timedelta(days=1), hi - shift + pd.Timedelta(days=1)):
        path = resolve(month)
        if path is None:
            continue
        table = pq.read_table(path, columns=["symbol", *wanted],
                              filters=[("symbol", "in", list(symbols))])
        dtcol = table.column(cols["datetime"])
        if str(dtcol.type) in ("string", "large_string"):
            # the date window pushed down too (ISO strings sort chronologically)
            mask = pc.and_(pc.greater_equal(dtcol, lo_s), pc.less(dtcol, hi_s))
            table = table.filter(mask)
        rows += table.num_rows
        if rows > max_rows:
            raise MemoryError(f"load_minute_bars: more than {max_rows:,} rows for {len(symbols)} symbols "
                              f"up to {month}; narrow the window or raise max_rows")
        parts.append(table.to_pandas())
    frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["symbol", *wanted])
    stamps = pd.to_datetime(frame[cols["datetime"]])
    if stamps.dt.tz is None:
        stamps = stamps.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    local_end = stamps + shift
    keep = (local_end > lo.tz_localize(tz)) & (local_end <= hi.tz_localize(tz)) & local_end.notna()
    if session is not None:
        tod = local_end.dt.hour * 60 + local_end.dt.minute + local_end.dt.second / 60
        open_m = session[0].hour * 60 + session[0].minute
        close_m = session[1].hour * 60 + session[1].minute
        keep &= (tod > open_m) & (tod <= close_m)
    frame = frame.loc[keep]
    index = pd.DatetimeIndex(local_end[keep].dt.tz_convert("UTC"))
    out = {}
    for sym in symbols:
        sel = (frame["symbol"] == sym).to_numpy()
        f = pd.DataFrame({k: frame.loc[sel, cols[k]].to_numpy(dtype=np.float64)
                          for k in ("open", "high", "low", "close", "volume") if k in cols},
                         index=index[sel])
        f = f[~f.index.duplicated(keep="last")].sort_index()
        out[sym] = f
    return out


# -- splits -------------------------------------------------------------------------------------


def read_splits(frame: pd.DataFrame, symbol: str | None = None, *, date: str = "date",
                split_from: str = "split_from", split_to: str = "split_to") -> pd.DataFrame:
    """Splits as ``(ex_date, ratio)`` rows, ``ratio`` = new shares per old (``split_to /
    split_from``: 10 for NVDA's 10-for-1, 0.125 for a 1-for-8 reverse split), sorted."""
    f = frame if symbol is None else frame[frame["symbol"] == symbol]
    ratio = f[split_to].astype(float) / f[split_from].astype(float)
    out = pd.DataFrame({"ex_date": pd.to_datetime(f[date]).dt.normalize().to_numpy(),
                        "ratio": ratio.to_numpy()})
    out = out[np.isfinite(out["ratio"]) & (out["ratio"] > 0) & (out["ratio"] != 1.0)]
    return out.sort_values("ex_date").reset_index(drop=True)


def _local_days(index: pd.DatetimeIndex, tz: str) -> np.ndarray:
    """Each end-stamped bar's trading day in ``tz`` (a bar ending at midnight is the day before)."""
    idx = index if index.tz is not None else index.tz_localize("UTC")
    local = (idx - pd.Timedelta(1, "ns")).tz_convert(tz).tz_localize(None)
    return local.normalize().to_numpy().astype("datetime64[D]")


def split_factors(index: pd.DatetimeIndex, splits: pd.DataFrame, tz: str = "America/New_York") -> np.ndarray:
    """Per bar, the product of the ratios of the splits whose ex-date is after the bar's day:
    prices / factor and volume * factor give the series in today's shares."""
    if not len(splits):
        return np.ones(len(index))
    days = _local_days(index, tz)
    ex = np.asarray(splits["ex_date"].to_numpy(), dtype="datetime64[D]")
    ratio = np.asarray(splits["ratio"], dtype=np.float64)
    after = np.concatenate((np.cumprod(ratio[::-1])[::-1], [1.0]))  # product of ratios k..end
    k = np.searchsorted(ex, days, side="right")      # first split with ex-date > the bar's day
    return after[k]


def adjust_for_splits(frame: pd.DataFrame, splits: pd.DataFrame, tz: str = "America/New_York") -> pd.DataFrame:
    """The frame with prices divided and volume multiplied by ``split_factors``: backward
    adjustment, the last bar keeps its quoted price."""
    f = split_factors(frame.index, splits, tz)
    out = frame.copy()
    for col in ("open", "high", "low", "close"):
        if col in out:
            out[col] = out[col].to_numpy(dtype=np.float64) / f
    if "volume" in out:
        out["volume"] = out["volume"].to_numpy(dtype=np.float64) * f
    return out


def split_actions(data, splits: pd.DataFrame, tz: str = "America/New_York") -> list[Split]:
    """``Split`` corporate actions for a feed of **unadjusted** prices: each at the feed's
    first bar of its ex-date (pre-market included when the feed has it), where phase 3 scales
    the position (quantity x ratio, average price / ratio) and the open orders before any fill.

    ``data`` must be registered (``engine.adddata``); pass the list to
    ``engine.set_corporate_actions(StaticCorporateActions(actions))``.
    """
    security = getattr(data, "security", None)
    if security is None:
        raise ValueError("split_actions: add the feed to the engine first (adddata)")
    block = data.load()
    t = block.timestamps
    if not len(t) or not len(splits):
        return []
    index = pd.DatetimeIndex(t.astype("datetime64[ns]")).tz_localize("UTC")
    days = _local_days(index, tz)
    out = []
    for ex, ratio in zip(np.asarray(splits["ex_date"].to_numpy(), dtype="datetime64[D]"), splits["ratio"]):
        k = int(np.searchsorted(days, ex, side="left"))
        if k >= len(t) or k == 0:
            continue                                 # the split is outside the feed's window
        out.append(Split(security.id, np.int64(t[k]), float(ratio)))
    return out


def detect_splits(frame: pd.DataFrame, tz: str = "America/New_York", threshold: float = 0.3,
                  ratios: Iterable[float] = (2, 3, 4, 5, 8, 10, 15, 20, 1 / 2, 1 / 3, 1 / 4, 1 / 5,
                                             1 / 8, 1 / 10, 1 / 20)) -> pd.DataFrame:
    """Overnight jumps of a minute series that look like a split: first price of a day over
    the last of the previous day, near ``1 / r`` for a common ratio ``r``. A check on the
    data, not a source of splits."""
    days = _local_days(frame.index, tz)
    close = frame["close"].to_numpy(dtype=np.float64)
    openp = frame["open"].to_numpy(dtype=np.float64)
    new_day = np.flatnonzero(days[1:] != days[:-1]) + 1
    jump = openp[new_day] / close[new_day - 1]
    rows = []
    for i, j in zip(new_day, jump):
        if abs(np.log(j)) < threshold:
            continue
        r = min(ratios, key=lambda x: abs(np.log(j * x)))
        rows.append({"ex_date": pd.Timestamp(days[i]), "jump": float(j), "ratio": float(r)})
    return pd.DataFrame(rows, columns=["ex_date", "jump", "ratio"])


__all__ = ["PWB_MINUTE_COLUMNS", "adjust_for_splits", "detect_splits", "hub_month_files",
           "load_minute_bars", "month_files", "read_splits", "split_actions", "split_factors"]
