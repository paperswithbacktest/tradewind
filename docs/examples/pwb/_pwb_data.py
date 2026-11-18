"""Daily bars from the Papers With Backtest datasets, as frames Tradewind's PandasData reads.

Needs ``pip install pwb-toolbox`` and an access key in the environment (``PWB_API_KEY``, or
``HF_ACCESS_TOKEN`` for the Hugging Face copies of the datasets): see
https://paperswithbacktest.com. The ETF prices of these datasets are total-return
(adjusted for dividends and splits).
"""

from __future__ import annotations

import pandas as pd


def daily_bars(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """One open/high/low/close frame per symbol, its missing days dropped."""
    from pwb_toolbox.datasets import get_pricing

    prices = get_pricing(symbols, fields=["open", "high", "low", "close"],
                         start_date=start, end_date=end)
    frames = {}
    for symbol in symbols:
        frame = prices[symbol].dropna(how="any")
        frame.index = pd.DatetimeIndex(frame.index).as_unit("ns")
        frames[symbol] = frame.astype("float64")
    return frames
