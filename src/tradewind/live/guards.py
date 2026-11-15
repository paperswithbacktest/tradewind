"""Guards of a live session (spec/07 § 9): order limits, the kill switch, request pacing.

``RiskLimits`` is checked by ``LiveBroker.submit`` on every order before anything is sent; a
breach turns the order ``Rejected`` locally (and, with ``trip_on_breach``, trips the kill
switch). ``KillSwitch`` stops a session: open orders are cancelled, nothing more is sent,
the run ends; it trips from code or when a file appears. ``RateLimiter`` and ``KeyedInterval``
keep an adapter under its venue's request limits (IB: 50 messages a second, 60 historical
requests in ten minutes, no identical historical request within 15 seconds).
"""

from __future__ import annotations

import collections
import math
import os
import time
from dataclasses import dataclass


@dataclass
class RiskLimits:
    """Hard limits on what a strategy may send. ``None`` means no limit."""

    max_order_quantity: float | None = None      # |shares| of one order
    max_order_notional: float | None = None      # |shares| x reference price of one order
    max_position_notional: float | None = None   # |position after the order| x price
    max_orders_per_slice: int | None = None
    allowed_symbols: frozenset[str] | None = None
    allow_short: bool = True
    trip_on_breach: bool = False                 # a breach also trips the kill switch

    def check(self, symbol: str, quantity: float, price: float | None, position: float,
              orders_this_slice: int) -> str:
        """The reason the order breaks a limit, or ``""``."""
        if self.allowed_symbols is not None and symbol not in self.allowed_symbols:
            return f"{symbol} is not in the allowed symbols"
        q = abs(quantity)
        if self.max_order_quantity is not None and q > self.max_order_quantity:
            return f"order of {q:g} exceeds max_order_quantity {self.max_order_quantity:g}"
        if self.max_orders_per_slice is not None and orders_this_slice >= self.max_orders_per_slice:
            return f"more than {self.max_orders_per_slice} orders in one slice"
        after = position + quantity
        if not self.allow_short and after < 0:
            return f"would leave {symbol} short ({after:g}) and allow_short is off"
        if price is not None and not math.isnan(price):
            if self.max_order_notional is not None and q * abs(price) > self.max_order_notional:
                return (f"order notional {q * abs(price):,.2f} exceeds max_order_notional "
                        f"{self.max_order_notional:,.2f}")
            if self.max_position_notional is not None and abs(after) * abs(price) > self.max_position_notional:
                return (f"position notional {abs(after) * abs(price):,.2f} would exceed "
                        f"max_position_notional {self.max_position_notional:,.2f}")
        return ""


class KillSwitch:
    """Trips once, from code (``trip``) or when the file ``path`` exists; stays tripped."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = None if path is None else os.fspath(path)
        self.reason = ""

    def trip(self, reason: str) -> None:
        if not self.reason:
            self.reason = reason or "kill switch"

    @property
    def tripped(self) -> bool:
        if not self.reason and self.path is not None and os.path.exists(self.path):
            self.reason = f"kill file {self.path} present"
        return bool(self.reason)


class RateLimiter:
    """At most ``max_calls`` in any window of ``period`` seconds (a sliding window)."""

    def __init__(self, max_calls: int, period: float, now=time.monotonic, sleep=time.sleep):
        if max_calls < 1 or period <= 0:
            raise ValueError("RateLimiter needs max_calls >= 1 and period > 0")
        self.max_calls = int(max_calls)
        self.period = float(period)
        self._now = now
        self._sleep = sleep
        self._calls: collections.deque = collections.deque()

    def _trim(self, t: float) -> None:
        calls = self._calls
        while calls and calls[0] <= t - self.period:
            calls.popleft()

    def wait_time(self) -> float:
        """Seconds to wait before the next call is allowed (0 when it is)."""
        t = self._now()
        self._trim(t)
        if len(self._calls) < self.max_calls:
            return 0.0
        return max(0.0, self._calls[0] + self.period - t)

    def acquire(self) -> float:
        """Wait as needed, then count a call; return the seconds waited."""
        waited = 0.0
        while True:
            w = self.wait_time()
            if w <= 0:
                break
            self._sleep(w)
            waited += w
        self._calls.append(self._now())
        return waited


class KeyedInterval:
    """At least ``interval`` seconds between two calls with the same key."""

    def __init__(self, interval: float, now=time.monotonic, sleep=time.sleep):
        self.interval = float(interval)
        self._now = now
        self._sleep = sleep
        self._last: dict = {}

    def acquire(self, key) -> float:
        last = self._last.get(key)
        waited = 0.0
        if last is not None:
            w = last + self.interval - self._now()
            if w > 0:
                self._sleep(w)
                waited = w
        self._last[key] = self._now()
        return waited


__all__ = ["KeyedInterval", "KillSwitch", "RateLimiter", "RiskLimits"]
