"""Securities: the engine's record of each tradable instrument (spec/05 § 4).

A ``Security`` is a thin object over one row of the market and portfolio arrays; the engine
never loops over ``Security`` objects inside a phase. The feed a strategy holds stays its
handle and exposes the security read-only as ``data.security`` (spec/05 § 10, Q6).

Interfaces read as a specification from QuantConnect Lean (Apache 2.0, see ``NOTICE``):
``Security``, ``SymbolProperties``, ``SecurityHolding``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    from .clock import ExchangeHours
    from .market import MarketState, PortfolioState
    from .models import ModelSet

SecurityId = int            # dense index 0..n-1 in registration (adddata) order; array row
Symbol = str                # the feed's name ("AAPL", "EURUSD", "BTC.CRP")
Currency = str              # ISO-like code, "USD"
Timestamp = np.int64        # ns since epoch, UTC; dates are midnight UTC (compat feeds)

ACCOUNT_CURRENCY: Currency = "USD"


class CashAmount(NamedTuple):
    """An amount of money in one currency (immutable; a tuple, cheap to build per fill)."""

    amount: float
    currency: Currency = ACCOUNT_CURRENCY


@dataclass(frozen=True, slots=True)
class SymbolProperties:
    """Contract terms of one symbol (Lean ``SymbolProperties``).

    The default is the oracle's: USD, multiplier 1, no tick rounding, lot 1 applied only
    where the oracle truncates (the ``order_target_*`` helpers); a raw ``buy(size=0.5)``
    keeps its size. A ``CommInfoBase`` with ``mult`` keeps its own multiplier (compat path).
    """

    description: str = ""
    quote_currency: Currency = ACCOUNT_CURRENCY
    contract_multiplier: float = 1.0        # value of one unit = price × multiplier
    minimum_price_variation: float = 0.0    # tick size; 0 = prices are not rounded
    lot_size: float = 1.0
    minimum_order_size: float | None = None
    price_magnifier: float = 1.0            # quoted price / real price (pence quotes)
    market_ticker: str | None = None


class Bar(NamedTuple):
    """One bar of one security: open, high, low, close, volume and its time."""

    time: float          # float days of the compat ``datetime`` line
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class SecurityHolding:
    """A view on row ``id`` of ``PortfolioState`` (Lean ``SecurityHolding``).

    ``getposition()`` keeps returning the compat ``Position`` (size and average price
    under the rule already reproduced); this view adds the accumulators.
    """

    __slots__ = ("_id", "_portfolio", "_security")

    def __init__(self, security: Security, portfolio: PortfolioState):
        self._security = security
        self._portfolio = portfolio
        self._id = security.id

    @property
    def quantity(self) -> float:
        return float(self._portfolio.quantity[self._id])

    @property
    def average_price(self) -> float:
        return float(self._portfolio.average_price[self._id])

    @property
    def total_fees(self) -> float:
        return float(self._portfolio.total_fees[self._id])

    @property
    def realized_pnl(self) -> float:
        return float(self._portfolio.realized_pnl[self._id])

    @property
    def total_dividends(self) -> float:
        return float(self._portfolio.total_dividends[self._id])

    @property
    def total_borrow_fees(self) -> float:
        return float(self._portfolio.total_borrow_fees[self._id])

    def _price(self, price):
        return self._security.price if price is None else float(price)

    def value(self, price: float | None = None) -> float:
        """quantity × price × multiplier, at the last close unless ``price`` is given."""
        return self.quantity * self._price(price) * self._security.properties.contract_multiplier

    def unrealized_pnl(self, price: float | None = None) -> float:
        q = self.quantity
        if not q:
            return 0.0
        return (self._price(price) - self.average_price) * q * self._security.properties.contract_multiplier

    def __repr__(self):
        return f"SecurityHolding({self._security.symbol!r}, quantity={self.quantity}, average_price={self.average_price})"


class Security:
    """The engine's record of one instrument (Lean ``Security``), read-only for a strategy."""

    __slots__ = ("_broker", "_delisted", "_holding", "_market", "_portfolio", "feed", "hours", "id",
                 "models", "properties", "symbol")

    def __init__(self, sid: SecurityId, symbol: Symbol, feed, models: ModelSet, hours: ExchangeHours,
                 properties: SymbolProperties | None = None, broker=None):
        self.id = sid
        self.symbol = symbol
        self.feed = feed
        self.models = models
        self.hours = hours
        self.properties = properties if properties is not None else SymbolProperties()
        self._broker = broker
        self._market: MarketState | None = None
        self._portfolio: PortfolioState | None = None
        self._holding: SecurityHolding | None = None
        self._delisted = False

    # -- market views ------------------------------------------------------------------
    @property
    def price(self) -> float:
        """The last close: the bar being processed, or the last one the feed delivered."""
        feed = self.feed
        if len(feed) == 0:
            return float("nan")
        return feed.close[0]

    @property
    def bar(self) -> Bar | None:
        feed = self.feed
        if len(feed) == 0:
            return None
        return Bar(feed.datetime[0], feed.open[0], feed.high[0], feed.low[0], feed.close[0], feed.volume[0])

    @property
    def has_bar(self) -> bool:
        """A bar arrived at the current slice."""
        market = self._market
        return bool(market is not None and market.has_bar[self.id])

    # -- portfolio views ---------------------------------------------------------------
    @property
    def holding(self) -> SecurityHolding | None:
        if self._holding is None and self._portfolio is not None:
            self._holding = SecurityHolding(self, self._portfolio)
        return self._holding

    @property
    def comminfo(self):
        """The commission scheme the broker applies to this security now (compat)."""
        return self._broker.getcommissioninfo(self.feed) if self._broker is not None else None

    @property
    def is_tradable(self) -> bool:
        return not self._delisted and len(self.feed) > 0

    @property
    def is_delisted(self) -> bool:
        return self._delisted

    def __repr__(self):
        return f"Security(id={self.id}, symbol={self.symbol!r})"
