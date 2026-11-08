"""Analyzers: observers of a strategy's run that return a dict from ``get_analysis``."""

from __future__ import annotations

import collections

from .params import ParamsBase


class Analyzer(ParamsBase):
    def _tw_before_init(self):
        # set by Cerebro before the user's __init__: strategy, datas, data aliases
        self.rets = collections.OrderedDict()
        self._tw_children = []
        if not hasattr(self, "strategy"):
            self.strategy = None
            self.datas = []
            self.data = None

    def _tw_bind(self, strategy):
        self.strategy = strategy
        self.datas = strategy.datas
        self.data = strategy.datas[0] if strategy.datas else None
        for i, d in enumerate(strategy.datas):
            setattr(self, f"data{i}", d)

    def __len__(self):
        return len(self.strategy)

    # hooks
    def start(self):
        pass

    def prenext(self):
        pass

    def nextstart(self):
        self.next()

    def next(self):
        pass

    def stop(self):
        pass

    def notify_order(self, order):
        pass

    def notify_trade(self, trade):
        pass

    def notify_cashvalue(self, cash, value):
        pass

    def notify_fund(self, cash, value, fundvalue, shares):
        pass

    def create_analysis(self):
        self.rets = collections.OrderedDict()

    def get_analysis(self):
        return self.rets

    def print(self, *args, **kwargs):
        print(self.get_analysis())


class TimeReturn(Analyzer):
    """Period returns of the portfolio value, keyed by bar date (daily)."""

    def start(self):
        self._last = None

    def next(self):
        value = self.strategy.broker.getvalue()
        if self._last:
            self.rets[self.data.datetime.datetime(0)] = value / self._last - 1.0
        self._last = value
