"""Live trading (chantier 7, spec/07-live-ibkr.md).

``LiveEngine`` runs unchanged strategies against a ``BrokerAdapter``: ``IBAdapter``
(Interactive Brokers through ib_async, ``pip install tradewind[ibkr]``) or ``FakeIB`` (a
simulated venue for tests and dry runs). Nothing here is imported by the backtest engine,
and nothing here needs a dependency beyond numpy and pandas until ``IBAdapter.connect``.
"""

from .adapter import AdapterError, BarRecord, BarSource, BrokerAdapter
from .broker import LiveBroker, LiveDivergence, order_request
from .clock import LiveClock, LiveTick
from .engine import LiveEngine
from .fake import FakeBars, FakeIB, FakeMarket
from .feed import CapacityError, LiveFeed
from .guards import KeyedInterval, KillSwitch, RateLimiter, RiskLimits
from .ibkr import IBAdapter, IBBars
from .journal import Journal
from .messages import (
    AccountSnapshot,
    Ack,
    BrokerOrder,
    Canceled,
    Connection,
    Execution,
    Kill,
    Notice,
    OrderRequest,
    Rejected,
)

__all__ = [
    "AccountSnapshot",
    "Ack",
    "AdapterError",
    "BarRecord",
    "BarSource",
    "BrokerAdapter",
    "BrokerOrder",
    "Canceled",
    "CapacityError",
    "Connection",
    "Execution",
    "FakeBars",
    "FakeIB",
    "FakeMarket",
    "IBAdapter",
    "IBBars",
    "Journal",
    "KeyedInterval",
    "Kill",
    "KillSwitch",
    "LiveBroker",
    "LiveClock",
    "LiveDivergence",
    "LiveEngine",
    "LiveFeed",
    "LiveTick",
    "Notice",
    "OrderRequest",
    "RateLimiter",
    "Rejected",
    "RiskLimits",
    "order_request",
]
