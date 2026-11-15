"""A user's view of the typed API, checked by mypy in CI (``[tool.mypy]`` in pyproject).

Not run by pytest. The lines marked ``type: ignore[...]`` are mistakes mypy must catch:
with ``warn_unused_ignores`` an ignore that is not needed fails the check, so the file
proves both that correct code type-checks and that wrong code does not.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

import tradewind as tw
from tradewind.performance import PerformanceReport


@dataclass(frozen=True)
class Params:
    lookback: int = 60
    weight: float = 0.5


class Momentum(tw.TypedStrategy[Params]):
    def initialize(self) -> None:
        self.sma: tw.Series = self.indicator(tw.ind.SMA, "SPY", period=self.params.lookback)
        self.schedule("monthly", month_days=(1,))

    def on_bar(self, bar: tw.BarEvent) -> None:
        spy: tw.BarSeries = self.bars["SPY"]
        last: float = spy.close[0]
        past: npt.NDArray[np.float64] = spy.close.history(self.params.lookback)
        when: dt.datetime = bar.time
        lookback: int = self.params.lookback
        holding: tw.Holding = self.portfolio["SPY"]
        value: float = self.portfolio.value
        if last > self.sma[0] and holding.quantity == 0:
            ticket: tw.OrderTicket | None = self.order_target_weight("SPY", self.params.weight)
            if ticket is not None:
                status: tw.OrderStatus = ticket.status
                assert status.is_open or not status.is_open
        del past, when, lookback, value
        missing = self.params.nope  # type: ignore[attr-defined]
        del missing
        self.order_target_weight(0, 0.5)  # type: ignore[arg-type]
        spy.close["0"]  # type: ignore[index]
        self.params.lookback = 3  # type: ignore[misc]

    def on_fill(self, fill: tw.Fill) -> None:
        quantity: float = fill.quantity
        symbol: str = fill.symbol
        del quantity, symbol

    def on_timer(self, event: tw.TimerEvent) -> None:
        name: str = event.name
        del name

    def on_universe_change(self, change: tw.SecurityChanges) -> None:
        added: tuple[str, ...] = change.added
        del added


def read(report: PerformanceReport) -> float:
    sharpe: float = report.sharpe
    capacity: float | None = report.capacity
    del capacity
    return sharpe + tw.metrics.sharpe_ratio([1.0, 1.1, 1.05]) + tw.metrics.max_drawdown([1.0]).depth
