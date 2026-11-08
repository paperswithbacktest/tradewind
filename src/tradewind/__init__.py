"""Tradewind: an Apache 2.0 backtesting engine compatible with the backtrader API.

Specification first, code after. See ``spec/`` and ``CLEANROOM.md`` at the root
of the repository: the engine is written from the public API contract and from
black-box tests, and no third-party engine source is ever consulted.
"""

from . import analyzers, indicators
from .analyzer import Analyzer, TimeReturn
from .broker import BackBroker, BrokerBase, SimulatedBroker
from .cerebro import Cerebro
from .comminfo import CommInfoBase, CommissionInfo
from .dateutils import date2num, num2date, num2dt
from .feeds import DataBase, PandasData, TimeFrame, feeds
from .indicator import Indicator
from .lineops import If, LineIterator, LineRoot, LinesOperation
from .lines import LineBuffer, Lines
from .models import ModelSet
from .order import BuyOrder, Order, SellOrder
from .position import Position
from .strategy import Strategy
from .timer import Timer
from .trade import Trade

ind = indicators  # backtrader's short alias

__version__ = "0.0.2"

__all__ = [
    "Analyzer",
    "BackBroker",
    "BrokerBase",
    "BuyOrder",
    "Cerebro",
    "CommInfoBase",
    "CommissionInfo",
    "DataBase",
    "If",
    "Indicator",
    "LineBuffer",
    "LineIterator",
    "LineRoot",
    "Lines",
    "LinesOperation",
    "ModelSet",
    "Order",
    "PandasData",
    "Position",
    "SellOrder",
    "SimulatedBroker",
    "Strategy",
    "TimeFrame",
    "TimeReturn",
    "Timer",
    "Trade",
    "__version__",
    "analyzers",
    "date2num",
    "feeds",
    "ind",
    "indicators",
    "num2date",
    "num2dt",
]
