"""The realistic models of chantier 4 (``tradewind.realistic``) and multi-currency.

Each model is plugged into a real run on synthetic bars and its effect is checked against
the arithmetic it promises; the last group checks that the default profile is untouched.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.cashbook import CashBook
from tradewind.models import CompatSlippage, ImmediateSettlement, ModelSet, NextBarFillModel
from tradewind.realistic import (
    BorrowRates,
    CashAccount,
    CostTable,
    DelayedSettlement,
    FlatMarginInterest,
    MarginAccount,
    MarginCallModel,
    SpreadFeeModel,
    SpreadSlippage,
    UsEquitySettlement,
    VolumeShareFillModel,
)
from tradewind.securities import SymbolProperties


def frame(n=30, start="2022-01-03", seed=3, volume=1e5, drift=0.0, close=None):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range(start, periods=n)
    if close is None:
        close = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n)))
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({"open": close * 0.999, "high": close * 1.01, "low": close * 0.99, "close": close,
                         "volume": volume}, index=index)


class Plan(tw.Strategy):
    """Places ``plan`` = {bar: [(data index, size), ...]} and records what happens."""

    params = (("plan", None), ("targets", None))

    def __init__(self):
        self.values, self.events, self.fills, self.cash = [], [], [], []

    def next(self):
        self.values.append(self.broker.getvalue())
        self.cash.append(self.broker.getcash())
        for i, size in (self.p.plan or {}).get(len(self), ()):
            if size > 0:
                self.buy(data=self.datas[i], size=size)
            else:
                self.sell(data=self.datas[i], size=-size)
        for i, pct in (self.p.targets or {}).get(len(self), ()):
            self.order_target_percent(self.datas[i], target=pct)

    def notify_order(self, order):
        self.events.append((len(self), order.data._name, order.getstatusname(), order.info.get("margin_call", False)))
        if order.status in (order.Completed, order.Partial):
            self.fills.append((order.data._name, order.executed.size, order.executed.price, order.executed.comm))


def engine_with(frames: dict, plan=None, targets=None, cash=100_000.0):
    engine = tw.Engine(stdstats=False)
    feeds = [engine.adddata(tw.feeds.PandasData(dataname=f), name=name) for name, f in frames.items()]
    engine.addstrategy(Plan, plan=plan, targets=targets)
    engine.broker.setcash(cash)
    return engine, feeds


# -- costs --------------------------------------------------------------------------------------


def test_cost_table_lookups():
    table = CostTable({("AAA", 2010): 20.0, ("AAA", 2014): 8.0, ("BBB", 2012): 0.0, ("CCC", 2012): float("nan")},
                      default_half_spread=3e-4, aliases={"AAA.STK": "AAA"})
    assert table.lookup("AAA", 2010) == (0.001, "measured")
    assert table.lookup("AAA", 2011) == (0.001, "nearest")           # nearest: 2010
    assert table.lookup("AAA", 2012) == (0.001, "nearest")           # tie: the earlier year
    assert table.lookup("AAA", 2013) == (0.0004, "nearest")
    assert table.lookup("AAA", 2030) == (0.0004, "nearest")          # any distance by default
    assert table.lookup("AAA.STK", 2014) == (0.0004, "measured")
    assert table.lookup("BBB", 2012) == (3e-4, "default")            # zero spread: absent, not free
    assert table.lookup("CCC", 2012) == (3e-4, "default")
    assert table.lookup("ZZZ", 2012) == (3e-4, "default")
    bounded = CostTable({("AAA", 2010): 20.0}, max_gap=3)
    assert bounded.lookup("AAA", 2013)[1] == "nearest" and bounded.lookup("AAA", 2014)[1] == "default"
    df = pd.DataFrame({"symbol": ["AAA", "AAA"], "year": [2010, 2011], "spread_bps": [10.0, 30.0],
                       "lambda_bps": [5.0, None]})
    t2 = CostTable.from_frame(df)
    assert t2.half_spread("AAA", 2011) == 0.0015 and t2.lambda_bps("AAA", 2010) == 5.0
    assert t2.lambda_bps("AAA", 2011) == 5.0 and t2.rows == 2
    assert CostTable.flat(10.0).half_spread("anything", 1999) == 0.0005
    assert CostTable({("A", 2020): 50_000.0}, max_spread_bps=1_000.0).half_spread("A", 2020) == 0.05


def test_spread_fee_per_symbol_and_year():
    """The fee of each fill reads the spread of the fill's year: a buy filled on the last
    business day of 2022 and a sell filled in 2023 pay different rates."""
    f = frame(n=12, start="2022-12-26")
    table = CostTable({("A", 2022): 20.0, ("A", 2023): 60.0})
    engine, (feed,) = engine_with({"A": f}, plan={1: [(0, 100)], 8: [(0, -100)]})
    engine.broker.set_models(feed, fee=SpreadFeeModel(table, per_share=0.005, minimum=1.0))
    s = engine.run()[0]
    (_, _q1, p1, c1), (_, _q2, p2, c2) = s.fills
    assert f.index[1].year == 2022 and f.index[8].year == 2023
    assert c1 == pytest.approx(100 * p1 * 0.001 + 1.0)                 # 10 bp half-spread, the 1$ minimum
    assert c2 == pytest.approx(100 * p2 * 0.003 + 1.0)                 # 30 bp in 2023
    assert feed.security.holding.total_fees == pytest.approx(c1 + c2)
    assert engine.broker.getcash() == pytest.approx(100_000 - 100 * p1 - c1 + 100 * p2 - c2)


def test_spread_fee_impact_term():
    model = SpreadFeeModel(CostTable({("A", 2022): 10.0}, impact={("A", 2022): 4.0}), impact_factor=0.5)
    engine, (feed,) = engine_with({"A": frame(n=3)}, plan={1: [(0, 500)]})
    engine.broker.set_models(feed, fee=model)
    s = engine.run()[0]
    _, _q, p, comm = s.fills[0]
    notional = 500 * p
    assert comm == pytest.approx(notional * 0.0005 + notional * (0.5 * 4.0 * notional / 1e6) / 1e4)


def test_spread_slippage_moves_the_price():
    f = frame(n=5)
    table = CostTable({("A", 2022): 40.0})
    engine, (feed,) = engine_with({"A": f}, plan={1: [(0, 10)], 2: [(0, -10)]})
    engine.broker.set_models(feed, slippage=SpreadSlippage(table, fraction=0.5))
    s = engine.run()[0]
    (_, _, buy, _), (_, _, sell, _) = s.fills
    assert buy == pytest.approx(f["open"].iloc[1] * (1 + 0.001))
    assert sell == pytest.approx(f["open"].iloc[2] * (1 - 0.001))


# -- fills ----------------------------------------------------------------------------------------


def test_volume_share_partial_fills_carry_the_rest():
    f = frame(n=8, volume=10_000.0)
    engine, (feed,) = engine_with({"A": f}, plan={1: [(0, 2_500)]}, cash=1e6)
    engine.broker.set_models(feed, fill=VolumeShareFillModel(participation=0.1))
    s = engine.run()[0]
    statuses = [e[2] for e in s.events]
    assert statuses == ["Submitted", "Accepted", "Partial", "Partial", "Completed"]
    assert [round(x[1]) for x in s.fills] == [1_000, 2_000, 2_500]     # cumulative executed size
    assert engine.broker.getposition(feed).size == 2_500
    # each part at the open of its own bar
    opens = f["open"].to_numpy()
    assert s.fills[-1][2] == pytest.approx((1000 * opens[1] + 1000 * opens[2] + 500 * opens[3]) / 2500)


def test_volume_share_zero_and_missing_volume():
    zero = frame(n=6, volume=0.0)
    engine, (feed,) = engine_with({"A": zero}, plan={1: [(0, 10)]})
    engine.broker.set_models(feed, fill=VolumeShareFillModel(participation=0.5))
    engine.run()
    assert engine.broker.getposition(feed).size == 0 and len(engine.broker.pending) == 1
    nan = frame(n=6, volume=float("nan"))
    engine, (feed,) = engine_with({"A": nan}, plan={1: [(0, 10)]})
    engine.broker.set_models(feed, fill=VolumeShareFillModel(participation=0.5))
    engine.run()
    assert engine.broker.getposition(feed).size == 10                  # no volume column: not capped
    with pytest.raises(ValueError):
        VolumeShareFillModel(participation=0)


def test_volume_share_rest_can_be_cancelled():
    class CancelRest(Plan):
        def next(self):
            if len(self) == 1:
                self.order = self.buy(size=500)

        def notify_order(self, order):
            super().notify_order(order)
            if order.status == order.Partial:
                self.cancel(self.order)       # the live order (a notification is a snapshot)

    engine = tw.Engine(stdstats=False)
    feed = engine.adddata(tw.feeds.PandasData(dataname=frame(n=6, volume=1_000.0)), name="A")
    engine.addstrategy(CancelRest)
    engine.broker.setcash(100_000)
    engine.broker.set_models(feed, fill=VolumeShareFillModel(participation=0.1))
    s = engine.run()[0]
    assert engine.broker.getposition(feed).size == 100
    assert [e[2] for e in s.events][-2:] == ["Partial", "Canceled"]


# -- shorting -------------------------------------------------------------------------------------


def test_borrow_fee_accrues_on_shorts():
    f = frame(n=20)
    engine, (feed,) = engine_with({"A": f}, plan={1: [(0, -100)]})
    engine.broker.set_models(feed, shortable=BorrowRates(rate=0.10))
    engine.run()
    times = np.array([d.value for d in f.index], dtype=np.int64)
    closes = f["close"].to_numpy()
    # short from the fill at bar 2 (index 1 in 0-based slices: placed on 1, filled on 2)
    expected = 0.0
    for k in range(2, len(f)):
        years = (times[k] - times[k - 1]) / (365.0 * 86_400e9)
        expected += 100 * closes[k] * 0.10 * years
    assert feed.security.holding.total_borrow_fees == pytest.approx(expected, rel=1e-12)
    fill = f["open"].iloc[1]
    assert engine.broker.getcash() == pytest.approx(100_000 + 100 * fill - expected)


def test_hard_to_borrow_and_easy_to_borrow_list():
    rates = BorrowRates(rate=0.003, rates={"GME": 0.8}, easy_to_borrow={"GME", "AAPL"}, shortable={"AAPL": 50})
    assert rates.fee_rate("GME", 0) == 0.8 and rates.fee_rate("AAPL", 0) == 0.003
    assert rates.shortable_quantity("XYZ", 0) == 0.0
    assert rates.shortable_quantity("AAPL", 0) == 50 and rates.shortable_quantity("GME", 0) is None
    f = frame(n=6)
    engine, (a, x) = engine_with({"AAPL": f, "XYZ": f}, plan={1: [(0, -60), (1, -10)], 3: [(0, -50)]})
    engine.broker.set_profile("realistic", borrow=rates, participation=None, settlement=None)
    s = engine.run()[0]
    refused = {(e[1], e[0]) for e in s.events if e[2] == "Margin"}
    assert ("AAPL", 2) in refused and ("XYZ", 2) in refused          # beyond 50, not on the list
    assert engine.broker.getposition(a).size == -50                   # within the limit
    assert engine.broker.getposition(x).size == 0


# -- buying power ----------------------------------------------------------------------------------


def test_reg_t_initial_margin():
    """50 % initial: 100 000 of equity buys up to 200 000 of stock."""
    f = frame(n=6, close=np.full(6, 100.0))
    engine, (feed,) = engine_with({"A": f}, plan={1: [(0, 1_900)]})
    engine.broker.set_models(feed, buying_power=MarginAccount())
    engine.run()
    assert engine.broker.getposition(feed).size == 1_900
    assert engine.broker.getcash() < 0                                   # borrowed
    engine, (feed,) = engine_with({"A": f}, plan={1: [(0, 2_100)]})
    engine.broker.set_models(feed, buying_power=MarginAccount())
    s = engine.run()[0]
    assert engine.broker.getposition(feed).size == 0
    assert [e[2] for e in s.events] == ["Submitted", "Margin"]


def test_refused_order_consumes_nothing():
    """The compatible check carries a refused buy's cost into the batch (the oracle's
    deficit carry); the margin account does not."""
    f = frame(n=6, close=np.full(6, 100.0))
    plan = {1: [(0, 5_000), (0, 100)]}
    engine, (feed,) = engine_with({"A": f}, plan=plan)
    s = engine.run()[0]
    assert [e[2] for e in s.events][2:4] == ["Margin", "Margin"]       # oracle: both refused
    engine, (feed,) = engine_with({"A": f}, plan=plan)
    engine.broker.set_models(feed, buying_power=CashAccount())
    s = engine.run()[0]
    assert engine.broker.getposition(feed).size == 100                  # the small one passes


def test_cash_account_refuses_shorts_and_leverage():
    f = frame(n=6, close=np.full(6, 100.0))
    engine, (feed,) = engine_with({"A": f}, plan={1: [(0, -10)], 2: [(0, 1_001)]})
    engine.broker.set_models(feed, buying_power=CashAccount())
    s = engine.run()[0]
    assert engine.broker.getposition(feed).size == 0
    assert [e[2] for e in s.events if e[2] == "Margin"] == ["Margin", "Margin"]


def test_margin_call_liquidates_pro_rata():
    """2x long, then the price halves: equity falls below 25 % maintenance and phase 9
    cuts the position back, with orders flagged ``margin_call``."""
    close = np.r_[np.full(4, 100.0), np.full(8, 55.0)]
    f = frame(n=12, close=close)
    engine, (feed,) = engine_with({"A": f}, plan={1: [(0, 1_990)]})
    engine.broker.set_profile("realistic", participation=None, settlement=None, borrow=None)
    assert isinstance(engine.broker.margin_call_model, MarginCallModel)
    s = engine.run()[0]
    calls = [e for e in s.events if e[3]]
    assert calls and calls[-1][2] == "Completed"
    pos = engine.broker.getposition(feed).size
    assert 0 < pos < 1_990
    equity = engine.broker.getvalue()
    assert equity >= 0.25 * pos * 55.0 * 0.999


def test_target_percent_with_margin_account_uses_lots():
    f = frame(n=6, close=np.full(6, 100.0))
    engine, (feed,) = engine_with({"A": f}, targets={1: [(0, 0.5)]})
    engine.broker.set_models(feed, buying_power=MarginAccount())
    engine.broker.set_properties(feed, SymbolProperties(lot_size=100.0))
    engine.run()
    assert engine.broker.getposition(feed).size == 500


# -- settlement ------------------------------------------------------------------------------------


def test_us_equity_settlement_dates():
    s = UsEquitySettlement()
    assert s.settle_date(dt.date(2024, 5, 24)) == dt.date(2024, 5, 29)      # T+2, Memorial Day skipped
    assert s.settle_date(dt.date(2024, 5, 28)) == dt.date(2024, 5, 29)      # T+1 from 2024-05-28
    assert s.settle_date(dt.date(2017, 9, 1)) == dt.date(2017, 9, 7)        # T+3, Labor Day skipped
    assert s.settle_date(dt.date(2017, 9, 5)) == dt.date(2017, 9, 7)        # T+2
    assert DelayedSettlement(2).settle_date(dt.date(2024, 12, 24)) == dt.date(2024, 12, 26)  # no calendar


def test_delayed_settlement_holds_proceeds_but_not_value():
    f = frame(n=10, start="2024-06-03")
    engine, (feed,) = engine_with({"A": f}, plan={1: [(0, 100)], 3: [(0, -100)]})
    engine.broker.set_models(feed, settlement=DelayedSettlement(days=2))
    s = engine.run()[0]
    # sold at bar 4's open (2024-06-06, a Thursday): settles on Monday 2024-06-10 (bar 6)
    cash, values = s.cash, s.values
    sale = 100 * f["open"].iloc[3]
    assert cash[3] == pytest.approx(cash[2])                       # not in the cash yet
    assert cash[4] == pytest.approx(cash[2])
    assert cash[5] == pytest.approx(cash[2] + sale)                # landed on the settlement date
    assert values[3] == pytest.approx(cash[2] + sale)              # but always in the value
    assert values[-1] == pytest.approx(cash[-1])


def test_unsettled_cash_is_not_buying_power_in_a_cash_account():
    f = frame(n=8, start="2024-06-03", close=np.full(8, 100.0))
    plan = {1: [(0, 900)], 3: [(0, -900)], 4: [(0, 500)]}
    engine, (feed,) = engine_with({"A": f}, plan=plan)
    engine.broker.set_models(feed, buying_power=CashAccount(), settlement=DelayedSettlement(days=2))
    s = engine.run()[0]
    assert ("A", "Margin") in {(e[1], e[2]) for e in s.events}     # 10 000 settled: 500 too many
    engine, (feed,) = engine_with({"A": f}, plan=plan)
    engine.broker.set_models(feed, buying_power=MarginAccount(), settlement=DelayedSettlement(days=2))
    engine.run()
    assert engine.broker.getposition(feed).size == 500             # a margin account counts it


# -- interest -------------------------------------------------------------------------------------


def test_debit_interest():
    f = frame(n=10, close=np.full(10, 100.0))
    engine, (feed,) = engine_with({"A": f}, plan={1: [(0, 1_500)]})
    engine.broker.set_models(feed, buying_power=MarginAccount(), margin_interest=FlatMarginInterest(debit_rate=0.05))
    engine.run()
    debit = 100_000 - 1_500 * f["open"].iloc[1]
    days = (f.index[-1] - f.index[1]).days
    assert engine.broker.getcash() == pytest.approx(debit * (1 + 0.05 * days / 365), rel=1e-3)
    interest = FlatMarginInterest(credit_rate=0.01)
    book = CashBook("USD", 1_000.0)
    assert interest.accrue(book, None, 0) == 0.0
    assert interest.accrue(book, None, int(365 * 86_400e9)) == pytest.approx(-10.0)


# -- multi-currency --------------------------------------------------------------------------------


def _fx_frames(n=15):
    stock = frame(n=n, seed=5)                      # quoted in EUR
    rate = 1.10 * np.exp(np.cumsum(np.random.default_rng(9).normal(0, 0.004, n)))
    fx = frame(n=n, close=rate, volume=float("nan"))
    return stock, fx


def test_foreign_security_cash_and_nav_in_account_currency():
    stock, fx = _fx_frames()
    engine, (eu, eurusd) = engine_with({"SAP": stock, "EURUSD": fx}, plan={1: [(0, 100)]})
    engine.broker.set_properties(eu, SymbolProperties(quote_currency="EUR"))
    engine.broker.set_conversion("EUR", eurusd)
    engine.broker.set_models(eu, buying_power=MarginAccount())
    s = engine.run()[0]
    book = engine.broker.cashbook
    price = stock["open"].iloc[1]
    assert book["EUR"].amount == pytest.approx(-100 * price)       # paid in EUR, borrowed
    assert book["USD"].amount == 100_000.0
    rates, closes = fx["close"].to_numpy(), stock["close"].to_numpy()
    for k in range(2, len(stock)):
        expected = 100_000 + (-100 * price + 100 * closes[k]) * rates[k]
        assert s.values[k] == pytest.approx(expected, rel=1e-12)
    assert book["EUR"].conversion_rate == rates[-1]


def test_foreign_target_percent_sizes_in_account_currency():
    stock, fx = _fx_frames()
    engine, (eu, eurusd) = engine_with({"SAP": stock, "EURUSD": fx}, targets={1: [(0, 0.5)]})
    engine.broker.set_properties(eu, SymbolProperties(quote_currency="EUR"))
    engine.broker.set_conversion("EUR", eurusd)
    engine.broker.set_models(eu, buying_power=MarginAccount())
    engine.run()
    unit = stock["close"].iloc[0] * fx["close"].iloc[0]
    assert engine.broker.getposition(eu).size == math.trunc(50_000 / unit)


def test_inverted_quote_and_account_currency():
    stock, fx = _fx_frames()
    usdjpy = frame(n=len(fx), close=np.full(len(fx), 150.0), volume=float("nan"))
    engine, (a, j) = engine_with({"A": stock, "USDJPY": usdjpy}, plan={1: [(0, 10)]})
    engine.broker.set_account_currency("JPY")
    engine.broker.set_conversion("USD", j, invert=False)       # one USD is worth 150 JPY
    engine.broker.set_models(a, buying_power=MarginAccount())
    engine.broker.setcash(10_000_000)
    s = engine.run()[0]
    book = engine.broker.cashbook
    assert book.account_currency == "JPY" and book["USD"].amount == pytest.approx(-10 * stock["open"].iloc[1])
    assert s.values[-1] == pytest.approx(10_000_000 + 150.0 * 10 * (stock["close"].iloc[-1] - stock["open"].iloc[1]))
    engine2, (_a2, j2) = engine_with({"A": stock, "JPYUSD": usdjpy}, plan={})
    engine2.broker.set_conversion("JPY", j2, invert=True)
    engine2.run()
    assert engine2.broker.cashbook["JPY"].conversion_rate == pytest.approx(1 / 150.0)


def test_foreign_fees_and_borrow_in_quote_currency():
    stock, fx = _fx_frames()
    engine, (eu, eurusd) = engine_with({"SAP": stock, "EURUSD": fx}, plan={1: [(0, -100)]})
    engine.broker.set_properties(eu, SymbolProperties(quote_currency="EUR"))
    engine.broker.set_conversion("EUR", eurusd)
    engine.broker.set_models(eu, buying_power=MarginAccount(), fee=SpreadFeeModel(CostTable.flat(20.0)),
                             shortable=BorrowRates(0.05))
    engine.run()
    book = engine.broker.cashbook
    holding = eu.security.holding
    proceeds = 100 * stock["open"].iloc[1]
    assert book["EUR"].amount == pytest.approx(proceeds - proceeds * 0.001 - holding.total_borrow_fees)
    assert book["USD"].amount == 100_000.0 and holding.total_borrow_fees > 0


# -- the profile -----------------------------------------------------------------------------------


def test_realistic_profile_options():
    table = CostTable.flat(10.0)
    ms = ModelSet.realistic(table)
    assert isinstance(ms.fee, SpreadFeeModel) and isinstance(ms.slippage, CompatSlippage)
    assert isinstance(ms.fill, VolumeShareFillModel) and ms.fill.participation == 0.1
    assert isinstance(ms.buying_power, MarginAccount) and ms.buying_power.initial == 0.5
    assert isinstance(ms.settlement, UsEquitySettlement) and ms.shortable.rate == 0.0025
    ms = ModelSet.realistic(table, spread_as="slippage", participation=None, account="cash", settlement=2,
                            borrow=None, debit_rate=0.06)
    assert isinstance(ms.slippage, SpreadSlippage) and ms.fee.spread_fraction == 0.0
    assert type(ms.fill) is NextBarFillModel and isinstance(ms.buying_power, CashAccount)
    assert ms.settlement.days == 2 and ms.margin_interest.debit_rate == 0.06
    ms = ModelSet.realistic(settlement=None)
    assert isinstance(ms.settlement, ImmediateSettlement)
    with pytest.raises(ValueError):
        ModelSet.realistic(spread_as="both")
    with pytest.raises(ValueError):
        ModelSet.realistic(account="prime")


def test_realistic_profile_runs_and_differs():
    frames = {f"S{i}": frame(n=60, seed=i, volume=50_000.0) for i in range(4)}
    targets = {k: [(i, 0.24) for i in range(4)] for k in (1, 21, 41)}
    base, _ = engine_with(frames, targets=targets)
    ref = base.run()[0].values
    eng, _ = engine_with(frames, targets=targets)
    eng.broker.set_profile("realistic", costs=CostTable.flat(30.0))
    real = eng.run()[0].values
    assert real[-1] < ref[-1]                                  # costs paid
    assert eng.fingerprint.digest != base.fingerprint.digest
    eng.broker.set_profile("realistic", spread_as="slippage")
    with pytest.raises(TypeError):
        eng.broker.set_slippage_perc(0.01)


def test_oracle_profile_is_bit_identical_to_the_default():
    frames = {f"S{i}": frame(n=60, seed=i) for i in range(3)}
    targets = {k: [(i, 0.3) for i in range(3)] for k in (1, 15, 30, 45)}
    a, _ = engine_with(frames, targets=targets)
    va = a.run()[0].values
    b, _ = engine_with(frames, targets=targets)
    b.broker.set_profile("oracle")
    vb = b.run()[0].values
    assert [x.hex() for x in va] == [x.hex() for x in vb]
    assert b.broker.margin_call_model is None and not b.broker._multi_cash
