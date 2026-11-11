"""Synthetic daily bars for the examples: no download, no account, the same numbers every run."""

import numpy as np
import pandas as pd


def bars(symbol: str, start: str = "2015-01-01", years: float = 8.0, drift: float = 0.0003,
         vol: float = 0.015, seed: int | None = None) -> pd.DataFrame:
    """One instrument's OHLCV on business days, from a seeded random walk."""
    rng = np.random.default_rng(seed if seed is not None else abs(hash(symbol)) % 2**32)
    index = pd.bdate_range(start, periods=round(252 * years))
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, len(index))))
    open_ = close * (1 + rng.normal(0, vol / 4, len(index)))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, vol / 3, len(index))))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, vol / 3, len(index))))
    volume = rng.integers(100_000, 1_000_000, len(index)).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume},
                        index=index)
