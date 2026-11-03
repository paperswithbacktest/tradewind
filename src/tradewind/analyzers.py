"""``bt.analyzers``: the analyzers a strategy file can add by name.

``TradeAnalyzer``, ``DrawDown`` and ``SharpeRatio`` are written from their public
documentation (parameters, keys of ``get_analysis``) and checked against the oracle on
synthetic runs (``tests/oracle/strategies.py``, probe ``analyzers``; scratch probes with
no trade, an open trade only, one winning trade, a reversal, a commission, weekly bars and
eleven parameter sets of ``SharpeRatio``). What those probes fixed beyond the documentation
is said where it is implemented.
"""

from __future__ import annotations

import collections
import math
import sys

from .analyzer import Analyzer, TimeReturn
from .feeds import TimeFrame

__all__ = ["Analyzer", "AutoOrderedDict", "DrawDown", "SharpeRatio", "TimeReturn", "TradeAnalyzer"]


class AutoOrderedDict(collections.OrderedDict):
    """A dict whose keys read as attributes (``analysis.total.closed``).

    Observed on the oracle: once the run is over, reading a key that is not there, as an
    attribute or as an item, raises ``KeyError`` (not ``AttributeError``).
    """

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        try:
            return self[name]
        except KeyError:
            raise KeyError(name) from None

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            self[name] = value


def _node(*pairs) -> AutoOrderedDict:
    out = AutoOrderedDict()
    for key, value in pairs:
        out[key] = value
    return out


class TradeAnalyzer(Analyzer):
    """Statistics of the closed trades, from ``notify_trade``.

    Observed on the oracle: with no trade the analysis is ``{total: {total: 0}}``; with
    open trades only, ``{total: {total, open}}``; the full tree appears with the first
    closed trade. A reversing order closes one trade and opens another. A trade is won
    when its net P&L (``pnlcomm``) is positive. ``lost.pnl.max`` is the largest loss
    (the most negative). Empty buckets hold zeros, except the ``min`` of the long/short
    length buckets, which starts at ``sys.maxsize``, and the ``min`` of ``len.won`` /
    ``len.lost``, which is absent until a trade lands there.
    """

    def start(self):
        self._closed = []  # (pnl, pnlcomm, barlen, long)
        self._open = 0
        self._total = 0
        self._build()

    def notify_trade(self, trade):
        if trade.justopened:
            self._total += 1
            self._open += 1
        elif trade.isclosed:
            self._open -= 1
            self._closed.append((float(trade.pnl), float(trade.pnlcomm), int(trade.barlen),
                                 bool(trade.long)))
        self._build()

    def stop(self):
        self._build()

    # -- the analysis tree ---------------------------------------------------------------
    def _build(self):
        total = _node(("total", self._total))
        if self._total and not self._closed:
            total["open"] = self._open
        rets = _node(("total", total))
        if self._closed:
            total["open"] = self._open
            total["closed"] = len(self._closed)
            rets.update(self._closed_stats())
        self.rets = rets

    def _closed_stats(self):
        closed = self._closed
        n = len(closed)
        streak = {"won": [0, 0], "lost": [0, 0]}
        for _pnl, net, _len, _long in closed:
            win, other = ("won", "lost") if net > 0 else ("lost", "won")
            streak[win][0] += 1
            streak[win][1] = max(streak[win][1], streak[win][0])
            streak[other][0] = 0

        def pnl_block(trades):
            values = [t[1] for t in trades]
            tot = sum(values) if values else 0.0
            avg = tot / len(values) if values else 0.0
            return tot, avg

        def side_pnl(trades, won):
            values = [t[1] for t in trades if (t[1] > 0) == won]
            tot = sum(values) if values else 0.0
            avg = tot / len(values) if values else 0.0
            mx = (max(values) if won else min(values)) if values else 0.0
            return _node(("total", tot), ("average", avg), ("max", mx))

        gross = sum(t[0] for t in closed)
        net = sum(t[1] for t in closed)
        won = [t for t in closed if t[1] > 0]
        lost = [t for t in closed if not t[1] > 0]
        out = AutoOrderedDict()
        out["streak"] = _node(
            ("won", _node(("current", streak["won"][0]), ("longest", streak["won"][1]))),
            ("lost", _node(("current", streak["lost"][0]), ("longest", streak["lost"][1]))),
        )
        out["pnl"] = _node(
            ("gross", _node(("total", gross), ("average", gross / n))),
            ("net", _node(("total", net), ("average", net / n))),
        )
        out["won"] = _node(("total", len(won)), ("pnl", side_pnl(closed, True)))
        out["lost"] = _node(("total", len(lost)), ("pnl", side_pnl(closed, False)))
        for side, is_long in (("long", True), ("short", False)):
            trades = [t for t in closed if t[3] == is_long]
            tot, avg = pnl_block(trades)
            out[side] = _node(
                ("total", len(trades)),
                ("pnl", _node(("total", tot), ("average", avg),
                              ("won", side_pnl(trades, True)), ("lost", side_pnl(trades, False)))),
                ("won", sum(1 for t in trades if t[1] > 0)),
                ("lost", sum(1 for t in trades if not t[1] > 0)),
            )

        def len_block(trades, with_min_default):
            lens = [t[2] for t in trades]
            tot = sum(lens)
            node = _node(("total", tot), ("average", tot / len(lens) if lens else 0.0),
                         ("max", max(lens) if lens else 0))
            if lens:
                node["min"] = min(lens)
            elif with_min_default:
                node["min"] = sys.maxsize
            return node

        lens = len_block(closed, True)
        lens["won"] = len_block(won, False)
        lens["lost"] = len_block(lost, False)
        for side, is_long in (("long", True), ("short", False)):
            trades = [t for t in closed if t[3] == is_long]
            node = len_block(trades, True)
            node["won"] = len_block([t for t in trades if t[1] > 0], True)
            node["lost"] = len_block([t for t in trades if not t[1] > 0], True)
            lens[side] = node
        out["len"] = lens
        return out


class DrawDown(Analyzer):
    """Current and maximum drawdown of the portfolio value, marked once per bar.

    ``drawdown`` is in percent of the running peak, ``moneydown`` in cash, ``len`` in bars
    since the peak; ``max`` holds the largest of each. Observed on the oracle: every bar
    counts, including the strategy's ``prenext`` bars.
    """

    params = (("fund", None),)

    def start(self):
        self._peak = -math.inf
        self.rets = AutoOrderedDict()
        self.rets["len"] = 0
        self.rets["drawdown"] = 0.0
        self.rets["moneydown"] = 0.0
        self.rets["max"] = _node(("len", 0.0), ("drawdown", 0.0), ("moneydown", 0.0))

    def _update(self):
        value = self.strategy.broker.getvalue()
        self._peak = max(self._peak, value)
        r = self.rets
        r["moneydown"] = moneydown = self._peak - value
        r["drawdown"] = drawdown = 100.0 * moneydown / self._peak
        r["len"] = r["len"] + 1 if drawdown else 0
        m = r["max"]
        m["moneydown"] = max(m["moneydown"], moneydown)
        m["drawdown"] = max(m["drawdown"], drawdown)
        m["len"] = max(m["len"], r["len"])

    def prenext(self):
        self._update()

    def next(self):
        self._update()


_FACTORS = {TimeFrame.Days: 252, TimeFrame.Weeks: 52, TimeFrame.Months: 12, TimeFrame.Years: 1}


def _period_key(date, timeframe):
    if timeframe == TimeFrame.Days:
        return date
    if timeframe == TimeFrame.Weeks:
        iso = date.isocalendar()
        return (iso[0], iso[1])
    if timeframe == TimeFrame.Months:
        return (date.year, date.month)
    if timeframe == TimeFrame.Years:
        return date.year
    raise NotImplementedError(f"SharpeRatio timeframe {TimeFrame.getname(timeframe)} is not supported")


class SharpeRatio(Analyzer):
    """Sharpe ratio of the period returns of the portfolio value.

    Returns are taken per ``timeframe`` period (the value at the period's last bar over the
    value at the previous period's last bar, the first period over the starting cash).
    With ``convertrate`` the risk-free rate is turned into a per-period rate,
    ``(1 + rf) ** (1 / factor) - 1``, and ``annualize`` multiplies the ratio by
    ``sqrt(factor)``; without it the returns are annualised, ``(1 + r) ** factor - 1``, and
    ``annualize`` has no effect. ``factor`` defaults to 252 / 52 / 12 / 1 for days / weeks
    / months / years. The deviation is the population one unless ``stddev_sample``. The
    ratio is ``None`` when the deviation is zero (one period, or a flat value).
    """

    params = (
        ("timeframe", TimeFrame.Years),
        ("compression", 1),
        ("riskfreerate", 0.01),
        ("factor", None),
        ("convertrate", True),
        ("annualize", False),
        ("stddev_sample", False),
        ("daysfactor", None),
        ("legacyannual", False),
        ("fund", None),
    )

    def start(self):
        self._returns = collections.OrderedDict()
        self._key = None
        self._base = self.strategy.broker.getvalue()
        self.rets = collections.OrderedDict(sharperatio=None)

    def _update(self):
        value = self.strategy.broker.getvalue()
        key = _period_key(self.data.datetime.date(0), self.p.timeframe)
        if self._key is not None and key != self._key:
            self._base = self._last
        self._key = key
        self._last = value
        self._returns[key] = value / self._base - 1.0

    def prenext(self):
        self._update()

    def next(self):
        self._update()

    def stop(self):
        returns = list(self._returns.values())
        factor = self.p.factor
        if factor is None:
            factor = _FACTORS.get(self.p.timeframe)
        rate = self.p.riskfreerate
        if factor is not None:
            if self.p.convertrate:
                rate = (1.0 + rate) ** (1.0 / factor) - 1.0
            else:
                returns = [(1.0 + r) ** factor - 1.0 for r in returns]
        excess = [r - rate for r in returns]
        ratio = None
        if excess:
            mean = sum(excess) / len(excess)
            ddof = 1 if self.p.stddev_sample else 0
            if len(excess) > ddof:
                var = sum((x - mean) ** 2 for x in excess) / (len(excess) - ddof)
                std = math.sqrt(var)
                if std > 0:
                    ratio = mean / std
                    if factor is not None and self.p.convertrate and self.p.annualize:
                        ratio *= math.sqrt(factor)
        # observed on the oracle: a plain OrderedDict here (``["sharperatio"]``, no
        # attribute access), where TradeAnalyzer and DrawDown return attribute dicts
        self.rets = collections.OrderedDict(sharperatio=ratio)
