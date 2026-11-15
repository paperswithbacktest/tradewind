"""Tradewind: an Apache 2.0 backtesting engine compatible with the backtrader API.

Specification first, code after. See ``spec/`` and ``CLEANROOM.md`` at the root
of the repository: the engine is written from the public API contract and from
black-box tests, and no third-party engine source is ever consulted.
"""

from . import (
    analyzers,
    calendars,
    consolidators,
    corporate,
    indicators,
    intraday,
    metrics,
    options,
    universe,
)
from .analyzer import Analyzer, TimeReturn
from .broker import BackBroker, BrokerBase, SimulatedBroker
from .calendars import ExchangeCalendar
from .clock import AlwaysOpen, ExchangeClock, UnionClock
from .comminfo import CommInfoBase, CommissionInfo
from .dateutils import date2num, num2date, num2dt
from .engine import Cerebro, Engine
from .feeds import DataBase, PandasData, TimeFrame, feeds
from .fingerprint import RunFingerprint
from .indicator import Indicator
from .lineops import DivByZero, If, LineIterator, LineRoot, LinesOperation
from .lines import LineBuffer, Lines
from .models import ModelSet
from .order import BuyOrder, Order, SellOrder
from .position import Position
from .strategy import Strategy
from .timer import Timer
from .trade import Trade
from .typed import (
    BarEvent,
    BarSeries,
    Fill,
    Holding,
    NoParams,
    OrderStatus,
    OrderTicket,
    OrderUpdate,
    Portfolio,
    SecurityChanges,
    Series,
    TimerEvent,
    TypedStrategy,
)

ind = indicators  # backtrader's short alias

__version__ = "0.0.2"

__all__ = [
    "AlwaysOpen",
    "Analyzer",
    "BackBroker",
    "BarEvent",
    "BarSeries",
    "BrokerBase",
    "BuyOrder",
    "Cerebro",
    "CommInfoBase",
    "CommissionInfo",
    "DataBase",
    "DivByZero",
    "Engine",
    "ExchangeCalendar",
    "ExchangeClock",
    "Fill",
    "Holding",
    "If",
    "Indicator",
    "LineBuffer",
    "LineIterator",
    "LineRoot",
    "Lines",
    "LinesOperation",
    "ModelSet",
    "NoParams",
    "Order",
    "OrderStatus",
    "OrderTicket",
    "OrderUpdate",
    "PandasData",
    "Portfolio",
    "Position",
    "RunFingerprint",
    "SecurityChanges",
    "SellOrder",
    "Series",
    "SimulatedBroker",
    "Strategy",
    "TimeFrame",
    "TimeReturn",
    "Timer",
    "TimerEvent",
    "Trade",
    "TypedStrategy",
    "UnionClock",
    "__version__",
    "analyzers",
    "calendars",
    "consolidators",
    "corporate",
    "date2num",
    "feeds",
    "ind",
    "indicators",
    "intraday",
    "metrics",
    "num2date",
    "num2dt",
    "options",
    "universe",
]
