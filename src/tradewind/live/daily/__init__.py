"""The daily paper-trading run of a meta-portfolio (``python -m tradewind.live.daily``).

Every evening after the US close: each strategy of the meta-portfolio runs in signal mode up
to the last close and gives its target book (``providers``, ``books``); the books are summed
at the meta-portfolio's weights and leverage, capped, rounded to lots and compared with the
account (``aggregate``); the orders are proposed, and placed only on request
(``runner``) on a simulated account or an IBKR **paper** account (``brokers``); the day is
tracked against the backtest (``tracking``), and the shorts' borrow fee and availability are
reported (``borrow``). The monthly price datasets can be completed up
to the last close from AlphaVantage (``fresh``). Guide: ``docs/live-paper.md``.

Nothing here is imported by the backtest engine, and nothing needs more than numpy and
pandas until an IBKR account (``ib_async``), a plot (matplotlib), a YAML file (PyYAML) or a
catalogue strategy (pwb-toolbox, in its own environment) is asked for.
"""

from .aggregate import Plan, ProposedOrder, plan_day
from .books import BookRecorder, SleeveBook, record_books
from .borrow import AutoBorrowSource, HubBorrowSource, IBShortableSource, borrow_report
from .brokers import IBPaperAccount, SimAccount, ibkr_tiered_commission
from .meta import MetaPortfolio, Sleeve
from .providers import EngineProvider, PwbProvider, ReplayProvider
from .runner import DailyRunner, DayReport, StaleData, last_session
from .tracking import Journal, track_day

__all__ = [
    "AutoBorrowSource",
    "BookRecorder",
    "DailyRunner",
    "DayReport",
    "EngineProvider",
    "HubBorrowSource",
    "IBPaperAccount",
    "IBShortableSource",
    "Journal",
    "MetaPortfolio",
    "Plan",
    "ProposedOrder",
    "PwbProvider",
    "ReplayProvider",
    "SimAccount",
    "Sleeve",
    "SleeveBook",
    "StaleData",
    "borrow_report",
    "ibkr_tiered_commission",
    "last_session",
    "plan_day",
    "record_books",
    "track_day",
]
