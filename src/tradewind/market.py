"""Feeds and the state arrays of the engine (spec/05 § 6.4, § 7.1).

``MarketState`` holds one row per security: how many bars each feed has delivered, whether
a bar arrived at the current slice, and the whole history of every feed concatenated into
one array per field, so the per-bar reads the engine needs across securities (the closes the
mark multiplies) are one numpy gather. The compat feeds keep their line buffers, which
become views on those arrays: nothing is copied twice, and a strategy reading
``data.close[0]`` reads the same memory.

``PortfolioState`` holds the quantity and average price of every security, mirrored from the
compat ``Position`` objects at each fill, plus the accumulators of ``SecurityHolding``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from .clock import TimeSlice, delivered_rows
from .dateutils import num2ns
from .securities import Symbol, Timestamp

FIELDS = ("datetime", "open", "high", "low", "close", "volume", "openinterest")


@dataclass(frozen=True, slots=True)
class FeedBlock:
    timestamps: np.ndarray                # int64 ns, sorted, unique
    columns: Mapping[str, np.ndarray]     # "open" "high" "low" "close" "volume" "openinterest"


@runtime_checkable
class DataFeed(Protocol):                 # one security (the compat PandasData is one)
    def load(self) -> FeedBlock: ...      # backtest: the whole history at start
    def is_live(self) -> bool: ...
    def poll(self, until: Timestamp) -> FeedBlock | None: ...   # live: bars closed since last poll


@runtime_checkable
class PanelFeed(Protocol):                # many securities at once (T × N), the fast path
    symbols: Sequence[Symbol]

    def load(self) -> tuple[np.ndarray, Mapping[str, np.ndarray]]: ...


def feed_block(dtnum: np.ndarray, arrays: Mapping[str, np.ndarray]) -> FeedBlock:
    return FeedBlock(num2ns(dtnum), {k: v for k, v in arrays.items()})


class MarketState:
    """One row per security; built at the start of a run from the started feeds."""

    def __init__(self, feeds: Sequence, has_bar: np.ndarray | None = None):
        self.feeds = list(feeds)
        n = len(self.feeds)
        lengths = np.array([len(f._dtnum) for f in self.feeds], dtype=np.int64)
        self.offsets = np.concatenate(([0], np.cumsum(lengths)[:-1])).astype(np.int64) if n else \
            np.zeros(0, dtype=np.int64)
        self.flat: dict[str, np.ndarray] = {}
        for k, alias in enumerate(FIELDS):
            parts = [f.lines[k].array for f in self.feeds]
            flat = np.concatenate(parts) if parts else np.zeros(0)
            self.flat[alias] = flat
            # the feed's line buffers become views on the concatenated array
            for f, off, ln in zip(self.feeds, self.offsets, lengths):
                f.lines[k].array = flat[off:off + ln]
        for f in self.feeds:
            f._dtnum = f.lines.datetime.array
            f._arrays = {alias: f.lines[k].array for k, alias in enumerate(FIELDS) if k}
        self.length = np.array([len(f) for f in self.feeds], dtype=np.int64)
        self.has_bar = np.zeros(n, dtype=bool) if has_bar is None else has_bar
        self._bufs_of = [tuple(f.lines) for f in self.feeds]
        self._all_bufs = [b for bufs in self._bufs_of for b in bufs]
        self.listed_all = bool(n == 0 or (self.length > 0).all())

    # -- phase 2 -----------------------------------------------------------------------
    def step(self, s: TimeSlice) -> None:
        """Load the slice's bars: each feed with a bar moves to it, the others keep their
        last bar and their length does not grow."""
        row = s.has_bar
        self.has_bar = row
        if s.all_have_bar:
            self.length += 1
            for b in self._all_bufs:
                b.idx += 1
        else:
            self.length += row
            bufs_of = self._bufs_of
            for j in np.flatnonzero(row).tolist():
                for b in bufs_of[j]:
                    b.idx += 1
        if not self.listed_all:
            self.listed_all = bool((self.length > 0).all())

    # -- reads ---------------------------------------------------------------------------
    @property
    def cursor(self) -> np.ndarray:
        """Row of the current bar in each feed's buffer (-1 before the first)."""
        return self.length - 1

    def gather(self, field: str, ids: np.ndarray | None = None) -> np.ndarray:
        """The current value of ``field`` for ``ids`` (all securities by default); NaN for
        a security that has not delivered a bar."""
        length = self.length if ids is None else self.length[ids]
        offsets = self.offsets if ids is None else self.offsets[ids]
        flat = self.flat[field]
        listed = length > 0
        out = np.full(len(length), np.nan)
        out[listed] = flat[offsets[listed] + length[listed] - 1]
        return out

    @property
    def open(self) -> np.ndarray:
        return self.gather("open")

    @property
    def high(self) -> np.ndarray:
        return self.gather("high")

    @property
    def low(self) -> np.ndarray:
        return self.gather("low")

    @property
    def close(self) -> np.ndarray:
        return self.gather("close")

    @property
    def volume(self) -> np.ndarray:
        return self.gather("volume")


class PortfolioState:
    """Quantities, average prices and accumulators, one row per security."""

    _FIELDS = ("quantity", "average_price", "multiplier", "total_fees", "realized_pnl",
               "total_dividends", "total_borrow_fees")

    def __init__(self, n: int = 0):
        self.n = n                                    # rows in use; the arrays may be longer
        self.quantity = np.zeros(n)
        self.average_price = np.zeros(n)
        self.multiplier = np.ones(n)
        self.total_fees = np.zeros(n)
        self.realized_pnl = np.zeros(n)
        self.total_dividends = np.zeros(n)
        self.total_borrow_fees = np.zeros(n)
        self.currency = np.zeros(n, dtype=np.int64)   # index into the CashBook

    def __len__(self) -> int:
        return self.n

    def grow(self, n: int) -> None:
        """Make room for ``n`` securities, keeping the rows already there (capacity
        doubles, so registering securities one by one stays linear)."""
        if n <= self.n:
            return
        cap = len(self.quantity)
        if n > cap:
            new_cap = max(n, 2 * cap, 16)
            for name in self._FIELDS:
                arr = getattr(self, name)
                fill = 1.0 if name == "multiplier" else 0.0
                setattr(self, name, np.concatenate((arr, np.full(new_cap - cap, fill))))
            self.currency = np.concatenate((self.currency, np.zeros(new_cap - cap, dtype=np.int64)))
        self.n = n


__all__ = ["FIELDS", "DataFeed", "FeedBlock", "MarketState", "PanelFeed", "PortfolioState",
           "delivered_rows", "feed_block"]
