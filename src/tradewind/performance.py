"""``tw.analyzers.Performance``: the statistics of a run in one analyzer (spec/08 § 3).

It records the portfolio value marked at every slice (``prenext`` slices included, as the
catalogue's NAV does), the gross and net exposure, the traded value, and for each fill the
security's dollar volume; ``stop`` turns them into the statistics of ``tradewind.metrics``
(the catalogue's and pwb-toolbox's definitions). The list follows the statistics Lean's
backtest report gives (Apache 2.0, see ``NOTICE``): return, volatility, Sharpe, Sortino,
alpha and beta against a benchmark, probabilistic Sharpe ratio, drawdown, exposure,
turnover and an estimated capacity. The code and the capacity method are Tradewind's own.

It reads the engine, it never changes it: a run with or without it gives the same values.
"""

from __future__ import annotations

import collections
import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt

from . import metrics
from .analyzer import Analyzer
from .analyzers import AutoOrderedDict

__all__ = ["Performance", "PerformanceReport"]


@dataclass(frozen=True)
class PerformanceReport:
    """What ``Performance`` measured; ``as_dict()`` for a plain dict.

    Fractions, not percentages (0.12 is 12 %). Exposures are averages over the slices of
    the run, as a fraction of the portfolio value; ``turnover`` is annualised and one-way
    (a full switch of the book counts 1.0 once). ``alpha`` / ``beta`` / ``capacity`` are
    None when there is nothing to compute them from."""

    start: Any
    end: Any
    bars: int
    periods_per_year: float
    total_return: float
    cagr: float
    volatility: float
    sharpe: float
    sortino: float
    probabilistic_sharpe: float
    max_drawdown: float
    max_drawdown_duration: int
    alpha: float | None
    beta: float | None
    gross_exposure: float
    net_exposure: float
    long_exposure: float
    short_exposure: float
    turnover: float
    capacity: float | None
    fills: int
    exposure_by_currency: dict[str, float] = field(default_factory=dict)
    exposure_by_asset_class: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: the parameters of ``Performance`` and their defaults (documented on the class)
_DEFAULTS: dict[str, Any] = {
    "benchmark": None, "periods_per_year": 252, "risk_free_rate": 0.0, "psr_benchmark": 0.0,
    "participation": 0.01, "adv_window": 20, "capacity_quantile": 0.05,
}


def _asset_class(feed: Any) -> str:
    """The asset class of a feed: its ``asset_class`` attribute when set, ``option`` for an
    option contract, else ``unclassified``."""
    label = getattr(feed, "asset_class", None)
    if label:
        return str(label)
    if getattr(feed, "contract", None) is not None and type(feed).__name__ == "OptionData":
        return "option"
    return "unclassified"


class Performance(Analyzer):
    """Return, risk, benchmark, exposure, turnover and capacity statistics of the run.

    ``benchmark``: the name of a feed of the run (or the feed) whose closes are the
    benchmark for alpha and beta (None: not computed). ``periods_per_year``: 252 for daily
    bars. ``risk_free_rate``: annual, for Sharpe and Sortino (0, the catalogue's
    convention). ``psr_benchmark``: the annual Sharpe ratio the probabilistic Sharpe ratio
    tests against. ``participation``, ``adv_window``, ``capacity_quantile``: the capacity
    estimate (``metrics.estimated_capacity``; the dollar volume of a fill is the average
    of close × volume over the security's last ``adv_window`` bars, the fill bar included).
    """

    params = tuple(_DEFAULTS.items())

    def start(self) -> None:
        self.times: list[Any] = []
        self.values: list[float] = []
        self.benchmark_values: list[float] = []
        self.gross: list[float] = []
        self.net: list[float] = []
        self.long: list[float] = []
        self.short: list[float] = []
        self.traded: list[float] = []
        self._traded_now = 0.0
        self._fills: list[tuple[float, float, float]] = []
        self._n_fills = 0
        self._seen_bits: dict[int, int] = {}
        self._by_ccy: dict[str, float] = collections.defaultdict(float)
        self._by_class: dict[str, float] = collections.defaultdict(float)
        strategy: Any = self.strategy
        bench = self.p.benchmark
        if isinstance(bench, str):
            bench = strategy.getdatabyname(bench)
        self._bench = bench
        self._labels: tuple[list[str], list[str]] | None = None
        self.report: PerformanceReport | None = None

    # -- per slice -------------------------------------------------------------------------
    def _labels_now(self, broker: Any) -> tuple[list[str], list[str]]:
        labels = self._labels
        n = len(broker.securities)
        if labels is None or len(labels[0]) != n:
            ccys = [s.properties.quote_currency for s in broker.securities]
            classes = [_asset_class(s.feed) for s in broker.securities]
            labels = self._labels = (ccys, classes)
        return labels

    def _exposure(self, broker: Any) -> npt.NDArray[np.float64]:
        """The signed market value of every position (quantity × last close × multiplier),
        in the account currency, one row per security."""
        pf, market = broker.portfolio, broker.market
        n = len(broker.securities)
        q = pf.quantity[:n]
        out = np.zeros(n)
        if market is None or not q.any():
            return out
        length = market.length[:n]
        live = (q != 0) & (length > 0)
        if not live.any():
            return out
        ids = np.flatnonzero(live)
        price = market.flat["close"][market.offsets[ids] + length[ids] - 1]
        out[ids] = q[ids] * price * pf.multiplier[ids]
        cashbook = broker.cashbook
        account = cashbook.account_currency
        ccys, _ = self._labels_now(broker)
        for i in ids:
            ccy = ccys[i]
            if ccy != account and ccy in cashbook:
                out[i] = cashbook.convert(float(out[i]), ccy, account)
        return out

    def _update(self) -> None:
        strategy: Any = self.strategy
        broker = strategy.broker
        value = float(broker.getvalue())
        self.times.append(strategy.datetime.datetime(0))
        self.values.append(value)
        bench = self._bench
        if bench is not None:
            self.benchmark_values.append(float(bench.close[0]) if len(bench) else math.nan)
        exposure = self._exposure(broker)
        if value and exposure.any():
            long_ = float(exposure[exposure > 0].sum())
            short = float(-exposure[exposure < 0].sum())
            ccys, classes = self._labels_now(broker)
            for i in np.flatnonzero(exposure):
                self._by_ccy[ccys[i]] += float(exposure[i]) / value
                self._by_class[classes[i]] += float(exposure[i]) / value
        else:
            long_ = short = 0.0
        scale = 1.0 / value if value else 0.0
        self.long.append(long_ * scale)
        self.short.append(short * scale)
        self.gross.append((long_ + short) * scale)
        self.net.append((long_ - short) * scale)
        self.traded.append(self._traded_now * scale)
        self._traded_now = 0.0

    def prenext(self) -> None:
        self._update()

    def next(self) -> None:
        self._update()

    def notify_order(self, order: Any) -> None:
        if order.status not in (order.Partial, order.Completed):
            return
        bits = order.executed.exbits
        seen = self._seen_bits.get(order.ref, 0)
        if len(bits) <= seen:
            return
        self._seen_bits[order.ref] = len(bits)
        data = order.data
        security = data.security
        mult = float(security.properties.contract_multiplier)
        strategy: Any = self.strategy
        nav = float(strategy.broker.getvalue())
        dollar_volume = self._dollar_volume(data, mult)
        for bit in bits[seen:]:
            traded = abs(float(bit.size) * float(bit.price)) * mult
            self._traded_now += traded
            self._n_fills += 1
            self._fills.append((traded, nav, dollar_volume))

    def _dollar_volume(self, data: Any, mult: float) -> float:
        size = min(int(self.p.adv_window), len(data))
        if size <= 0:
            return 0.0
        close = np.asarray(data.close.get(size=size), dtype=np.float64)
        volume = np.asarray(data.volume.get(size=size), dtype=np.float64)
        dv = np.abs(close * volume) * mult
        dv = dv[np.isfinite(dv)]
        return float(dv.mean()) if len(dv) else 0.0

    # -- the statistics --------------------------------------------------------------------
    def stop(self) -> None:
        self.report = self._build()
        self.rets = AutoOrderedDict()
        for key, value in self.report.as_dict().items():
            self.rets[key] = value

    def _build(self) -> PerformanceReport:
        p = self.p
        ppy = float(p.periods_per_year)
        values = self.values
        n = len(values)
        dd = metrics.max_drawdown(values)
        alpha = beta = None
        if self._bench is not None:
            pairs = [(v, b) for v, b in zip(values, self.benchmark_values) if b > 0]
            if len(pairs) >= 2:
                ab = metrics.alpha_beta([v for v, _ in pairs], [b for _, b in pairs], ppy)
                alpha, beta = ab.alpha, ab.beta

        def mean(xs: list[float]) -> float:
            return sum(xs) / len(xs) if xs else 0.0

        return PerformanceReport(
            start=self.times[0] if self.times else None,
            end=self.times[-1] if self.times else None,
            bars=n,
            periods_per_year=ppy,
            total_return=metrics.total_return(values),
            cagr=metrics.cagr(values, ppy),
            volatility=metrics.annualized_volatility(values, ppy),
            sharpe=metrics.sharpe_ratio(values, p.risk_free_rate, ppy),
            sortino=metrics.sortino_ratio(values, p.risk_free_rate, ppy),
            probabilistic_sharpe=metrics.probabilistic_sharpe_ratio(values, p.psr_benchmark, ppy),
            max_drawdown=dd.depth,
            max_drawdown_duration=dd.duration,
            alpha=alpha,
            beta=beta,
            gross_exposure=mean(self.gross),
            net_exposure=mean(self.net),
            long_exposure=mean(self.long),
            short_exposure=mean(self.short),
            turnover=mean(self.traded) / 2 * ppy,
            capacity=metrics.estimated_capacity(self._fills, p.participation, p.capacity_quantile),
            fills=self._n_fills,
            exposure_by_currency={k: v / n for k, v in sorted(self._by_ccy.items())} if n else {},
            exposure_by_asset_class={k: v / n for k, v in sorted(self._by_class.items())} if n else {},
        )
