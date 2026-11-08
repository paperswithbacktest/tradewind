"""The interfaces of spec/05, one group of tests per interface.

The oracle defaults are checked against the arithmetic they replace; a non-default model is
plugged in through ``broker.set_models`` to check that the engine actually routes through
the interface.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.cashbook import CashBook
from tradewind.clock import ALWAYS_OPEN, Phase, TimeSlice, UnionClock, delivered_rows
from tradewind.comminfo import CommInfoBase
from tradewind.dateutils import date2num, num2date, num2ns
from tradewind.events import CashEvent, EventBus, OrderEvent, TradeEvent, UniverseEvent
from tradewind.market import FeedBlock, MarketState, PortfolioState
from tradewind.models import (
    AlwaysShortable,
    BatchState,
    CommInfoFeeModel,
    CompatCashBuyingPower,
    CompatSlippage,
    Delisting,
    Dividend,
    FeeModel,
    FeeParameters,
    FillModel,
    FillParameters,
    FillProposal,
    ImmediateSettlement,
    ModelSet,
    NextBarFillModel,
    NoInterest,
    OracleCorporateActions,
    Split,
    StaticCorporateActions,
    SymbolChange,
    slipped,
)
from tradewind.order import BuyOrder, Order, SellOrder
from tradewind.securities import Bar, CashAmount, Security, SymbolProperties

# -- helpers -----------------------------------------------------------------------------------


def frames(n_sym=2, n_bars=30, seed=3, late=None):
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2022-01-03", periods=n_bars)
    out = {}
    for i in range(n_sym):
        close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n_bars)))
        f = pd.DataFrame({"open": close * 0.999, "high": close * 1.01, "low": close * 0.99, "close": close,
                          "volume": 1e5}, index=index)
        out[f"S{i}"] = f
    if late is not None:
        out[f"S{n_sym}"] = out["S0"].iloc[late:].copy()
    return out


def cerebro_with(fr, strategy, cash=100_000, **kw):
    cerebro = tw.Cerebro(stdstats=False)
    for name, f in fr.items():
        cerebro.adddata(tw.feeds.PandasData(dataname=f), name=name)
    cerebro.addstrategy(strategy, **kw)
    cerebro.broker.set_cash(cash)
    return cerebro


class Plan(tw.Strategy):
    """Places the orders of ``plan`` = {bar: [(data index, size), ...]} and records."""

    params = (("plan", None),)

    def __init__(self):
        self.values, self.events, self.fills = [], [], []

    def next(self):
        self.values.append(self.broker.getvalue())
        for i, size in (self.p.plan or {}).get(len(self), ()):
            if size > 0:
                self.buy(data=self.datas[i], size=size)
            else:
                self.sell(data=self.datas[i], size=-size)

    def notify_order(self, order):
        self.events.append(("order", order.data._name, order.getstatusname()))
        if order.status == order.Completed:
            self.fills.append((order.data._name, order.executed.size, order.executed.price, order.executed.comm))

    def notify_trade(self, trade):
        self.events.append(("trade", trade.data._name, trade.isclosed))


def run_plan(plan, fr=None, setup=None, cash=100_000):
    fr = fr or frames()
    cerebro = cerebro_with(fr, Plan, cash=cash, plan=plan)
    if setup is not None:
        setup(cerebro)
    strat = cerebro.run()[0]
    return cerebro, strat


def a_bar(o=10.0, h=11.0, lo=9.0, c=10.5, t=738000.0):
    return Bar(t, o, h, lo, c, 1e5)


class _Feed:
    _name = "X"


def an_order(size, exectype=Order.Market, price=None, plimit=None, comminfo=None):
    cls = BuyOrder if size > 0 else SellOrder
    order = cls(owner=None, data=_Feed(), size=size, price=price, plimit=plimit, exectype=exectype)
    order.comminfo = comminfo or CommInfoBase(commission=0.001, percabs=True)
    return order


# -- 4.1 / 4.2 securities, symbol properties --------------------------------------------------


def test_symbol_properties_default_is_the_oracle():
    sp = SymbolProperties()
    assert (sp.quote_currency, sp.contract_multiplier, sp.minimum_price_variation, sp.lot_size) == ("USD", 1.0, 0.0, 1.0)
    assert CashAmount(1.5) == (1.5, "USD")


def test_security_is_reachable_read_only_from_the_feed():
    fr = frames()
    cerebro, strat = run_plan({3: [(0, 10)]}, fr)
    d0, d1 = strat.datas
    s0, s1 = d0.security, d1.security
    assert isinstance(s0, Security) and (s0.id, s1.id) == (0, 1)
    assert s0.symbol == "S0" and s0.feed is d0
    assert s0.price == fr["S0"]["close"].iloc[-1]
    assert s0.bar.close == s0.price and s0.bar.open == fr["S0"]["open"].iloc[-1]
    assert s0.has_bar and s0.is_tradable and not s0.is_delisted
    assert s0.models is cerebro.broker.models
    h = s0.holding
    assert h.quantity == 10 == cerebro.broker.getposition(d0).size
    assert h.average_price == fr["S0"]["open"].iloc[3]
    assert h.value() == 10 * s0.price
    assert h.unrealized_pnl() == (s0.price - h.average_price) * 10
    assert s1.holding.quantity == 0 and s1.holding.unrealized_pnl() == 0.0


def test_holding_accumulates_fees_and_realised_pnl():
    def setup(c):
        c.broker.setcommission(commission=0.001)
    _, strat = run_plan({3: [(0, 10)], 8: [(0, -10)]}, setup=setup)
    h = strat.datas[0].security.holding
    comms = [f[3] for f in strat.fills]
    assert h.total_fees == comms[0] + comms[1]
    (_, _, p_in, _), (_, _, p_out, _) = strat.fills
    assert h.realized_pnl == pytest.approx(10 * (p_out - p_in))
    assert h.quantity == 0


# -- 4.5 cash book ---------------------------------------------------------------------------


def test_cashbook_single_currency_is_a_plain_float():
    book = CashBook("USD", 1000.0)
    book.add("USD", -0.1)
    book.add("USD", 0.3)
    assert book["USD"].amount == 1000.0 - 0.1 + 0.3
    assert book.convert(12.34, "USD", "USD") == 12.34
    assert book.total_in_account_currency() == book["USD"].amount
    assert book.unsettled() == CashAmount(0.0, "USD")


def test_cashbook_converts_through_rates_and_sums_unsettled():
    book = CashBook("USD", 100.0)
    book.ensure("EUR", conversion_rate=1.1).amount = 10.0
    assert book.convert(10.0, "EUR", "USD") == pytest.approx(11.0)
    assert book.total_in_account_currency() == pytest.approx(111.0)
    book.register_unsettled(lambda: CashAmount(5.0, "EUR"))
    assert book.unsettled().amount == pytest.approx(5.5)
    assert math.isnan(book.ensure("JPY").conversion_rate) and len(book) == 3


def test_broker_cash_is_the_account_balance():
    from tradewind.broker import Broker

    broker = tw.BackBroker()
    assert isinstance(broker, Broker) and broker.open_orders() == []
    broker.set_cash(5000)
    assert broker.getcash() == broker.cashbook["USD"].amount == 5000.0
    broker.cash -= 1.25
    assert broker.cashbook.account.amount == 4998.75


# -- 5.1 fee model ---------------------------------------------------------------------------


def test_comminfo_fee_model_is_the_scheme_charged_at_the_fill():
    order = an_order(10)
    fee = CommInfoFeeModel().get_order_fee(FeeParameters(None, order, 10, 50.0, 0))
    assert fee == CashAmount(order.comminfo.getcommission(10, 50.0), "USD")
    assert isinstance(CommInfoFeeModel(), FeeModel)


class FlatFee:
    def get_order_fee(self, p):
        return CashAmount(7.0, "USD")


def test_a_fee_model_plugged_per_security_prices_its_fills():
    def setup(c):
        c.broker.set_models(c.datas[0], fee=FlatFee())
    cerebro, strat = run_plan({3: [(0, 10), (1, 10)]}, setup=setup)
    base_c, _ = run_plan({3: [(0, 10), (1, 10)]})
    # S0 pays 7 more than with the default (zero) scheme, S1 is untouched
    assert cerebro.broker.getcash() == pytest.approx(base_c.broker.getcash() - 7.0)
    assert strat.datas[1].security.models.fee is cerebro.broker.models.fee


# -- 5.2 slippage ----------------------------------------------------------------------------


def test_compat_slippage_perc_fixed_and_open():
    buy, sell = an_order(1), an_order(-1)
    perc = CompatSlippage(perc=0.01)
    assert perc.get_slippage(None, buy, 100.0) == 1.0
    assert perc.get_slippage(None, buy, 100.0, at_open=True) == 0.0       # slip_open off
    assert CompatSlippage(perc=0.01, slip_open=True).get_slippage(None, buy, 100.0, at_open=True) == 1.0
    assert CompatSlippage(fixed=0.5).get_slippage(None, sell, 100.0) == 0.5
    assert CompatSlippage().get_slippage(None, buy, 100.0) == 0.0
    assert slipped(perc, None, buy, 100.0, False) == 101.0
    assert slipped(perc, None, sell, 100.0, False) == 99.0
    assert slipped(perc, None, sell, 100.0, True) == 100.0


def test_set_slippage_reconfigures_the_compat_model():
    broker = tw.BackBroker()
    broker.set_slippage_perc(0.002)
    assert (broker.models.slippage.perc, broker.models.slippage.fixed) == (0.002, 0.0)
    broker.set_slippage_fixed(0.1)
    assert (broker.models.slippage.perc, broker.models.slippage.fixed) == (0.0, 0.1)


# -- 5.3 fill model --------------------------------------------------------------------------


def _fill(order, bar, model=None, placed=None, slip=None):
    model = model or NextBarFillModel()
    return model.fill(FillParameters(None, order, bar, placed, 0, slip or CompatSlippage()))


def test_next_bar_fill_model_prices():
    bar = a_bar(o=10.0, h=11.0, lo=9.0, c=10.5)
    assert _fill(an_order(5), bar) == FillProposal(5, 10.0)
    assert _fill(an_order(5, Order.Close), bar).price == 10.5
    assert _fill(an_order(5, Order.Limit, price=9.5), bar).price == 9.5           # traded through
    assert _fill(an_order(5, Order.Limit, price=10.2), bar).price == 10.0         # gapped: the open
    assert _fill(an_order(5, Order.Limit, price=8.0), bar) is None                # not reached
    assert _fill(an_order(-5, Order.Limit, price=10.8), bar).price == 10.8
    assert _fill(an_order(5, Order.Stop, price=10.8), bar).price == 10.8
    assert _fill(an_order(5, Order.Stop, price=9.5), bar).price == 10.0           # gapped: the open
    assert _fill(an_order(-5, Order.Stop, price=9.5), bar).price == 9.5
    assert _fill(an_order(-5, Order.Stop, price=8.0), bar) is None
    sl = an_order(5, Order.StopLimit, price=10.8, plimit=10.9)
    assert _fill(sl, bar).price == 10.9 and sl.triggered


def test_next_bar_fill_model_cheat_on_close_and_expiry():
    placed = a_bar(c=12.0, t=737999.0)
    model = NextBarFillModel(coc=True)
    assert model.needs_placed_bar and not NextBarFillModel().needs_placed_bar
    assert _fill(an_order(5), a_bar(), model, placed).price == 12.0
    assert _fill(an_order(5, Order.Limit, price=9.5), a_bar(), model, placed).price == 9.5
    order = an_order(5)
    order.valid = 737999.5
    assert model.expired(order, a_bar(t=738000.0)) and not model.expired(order, a_bar(t=737999.0))
    assert isinstance(model, FillModel)


class FillAtClose:
    needs_placed_bar = False

    def expired(self, order, bar):
        return False

    def fill(self, p):
        return FillProposal(p.order.size, p.bar.close)


def test_a_fill_model_plugged_per_security_decides_the_price():
    fr = frames()

    def setup(c):
        c.broker.set_models(c.datas[1], fill=FillAtClose())
    _, strat = run_plan({3: [(0, 10), (1, 10)]}, fr, setup)
    by_name = {f[0]: f[2] for f in strat.fills}
    assert by_name["S0"] == fr["S0"]["open"].iloc[3]
    assert by_name["S1"] == fr["S1"]["close"].iloc[3]


# -- 5.4 buying power ------------------------------------------------------------------------


class _Sec:
    """A security stub for the buying-power model: an id, its feed, a broker, its models."""

    def __init__(self, broker, sid=0, size=0.0):
        self.id = sid
        self.feed = _Feed()
        self._broker = broker
        self.models = ModelSet.oracle()
        broker.positions_stub = {self.feed: tw.Position(size, 10.0 if size else 0.0)}

    @property
    def comminfo(self):
        return CommInfoBase(commission=0.0, percabs=True)


class _StubBroker:
    def getposition(self, data):
        return self.positions_stub[data]


def test_batch_check_carries_a_refused_orders_deficit():
    bp = CompatCashBuyingPower()
    broker = _StubBroker()
    sec = _Sec(broker)
    state = BatchState(1000.0, {})
    first = bp.check_order(sec, an_order(150, comminfo=CommInfoBase()), 10.0, state)
    assert not first.accepted and first.cash_consumed == 1500.0
    state.running_cash -= first.cash_consumed
    # a small buy after it is refused too: the deficit is carried (observed)
    second = bp.check_order(sec, an_order(1, comminfo=CommInfoBase()), 10.0, state)
    assert not second.accepted
    state.running_cash -= second.cash_consumed
    assert state.running_cash == 1000.0 - 1500.0 - 10.0 and state.pseudo_positions[0] == 151
    bp.checksubmit = False
    assert bp.check_order(sec, an_order(1, comminfo=CommInfoBase()), 10.0, state).accepted
    unpriced = bp.check_order(sec, an_order(1, comminfo=CommInfoBase()), None, state)
    assert unpriced.accepted and unpriced.cash_consumed == 0.0


def test_fill_check_reducing_opening_and_reversal():
    bp = CompatCashBuyingPower()
    broker = _StubBroker()
    long_sec = _Sec(broker, size=100)
    sell = an_order(-50, comminfo=CommInfoBase())
    assert bp.check_fill(long_sec, sell, FillProposal(-50, 10.0), cash=-1e9) == FillProposal(-50, 10.0)
    flat = _Sec(broker, size=0)
    buy = an_order(100, comminfo=CommInfoBase())
    assert bp.check_fill(flat, buy, FillProposal(100, 10.0), cash=999.0) is None
    assert bp.check_fill(flat, buy, FillProposal(100, 10.0), cash=1000.0) == FillProposal(100, 10.0)
    short = _Sec(broker, size=-100)
    reverse = an_order(300, comminfo=CommInfoBase())
    # closing 100 costs 1000, opening 200 more costs 2000: 2500 covers only the closing part
    assert bp.check_fill(short, reverse, FillProposal(300, 10.0), cash=2500.0) == FillProposal(100, 10.0)
    assert bp.check_fill(short, reverse, FillProposal(300, 10.0), cash=3000.0) == FillProposal(300, 10.0)


def test_quantity_for_target_value_truncates_the_gap():
    bp = CompatCashBuyingPower()
    sec = _Sec(_StubBroker())
    assert bp.quantity_for_target_value(sec, 1000.0, 0.0, 30.0) == 33
    assert bp.quantity_for_target_value(sec, 1000.0, 1090.0, 30.0) == -3
    assert bp.quantity_for_target_value(sec, 50.0, 50.0, 30.0) == 0
    assert bp.leverage(sec) == 1.0 and bp.maintenance_margin(sec, 10, 5.0) == 0.0
    assert bp.initial_margin(sec, 10, 5.0) == 50.0


# -- 5.5 settlement --------------------------------------------------------------------------


class DelayedSettlement:
    """T+n for the test: sale proceeds land ``days`` slices later; purchases at once."""

    def __init__(self, days=2):
        self.days = days
        self.queue = []
        self.scans = 0

    def apply_funds(self, security, amount, time, cashbook):
        if amount.amount > 0:
            self.queue.append([self.days, amount])
        else:
            cashbook.add(amount.currency, amount.amount)

    def scan(self, time, cashbook):
        self.scans += 1
        for item in self.queue:
            item[0] -= 1
            if item[0] < 0:
                cashbook.add(item[1].currency, item[1].amount)
        self.queue = [i for i in self.queue if i[0] >= 0]

    def unsettled(self):
        return CashAmount(sum(i[1].amount for i in self.queue), "USD")


def test_immediate_settlement_is_t_plus_zero():
    book = CashBook("USD", 10.0)
    ImmediateSettlement().apply_funds(None, CashAmount(5.0), 0, book)
    assert book["USD"].amount == 15.0 and ImmediateSettlement().unsettled().amount == 0.0


def test_a_settlement_model_holds_proceeds_until_its_scan():
    seen = {}

    class Watch(Plan):
        def next(self):
            super().next()
            seen[len(self)] = (self.broker.getcash(), self.broker.cashbook.unsettled().amount)

    model = DelayedSettlement(days=2)
    fr = frames()
    cerebro = cerebro_with(fr, Watch, plan={2: [(0, 10)], 5: [(0, -10)]})
    cerebro.broker.set_models(cerebro.datas[0], settlement=model)
    cerebro.run()
    proceeds = 10 * fr["S0"]["open"].iloc[5]
    assert seen[6][1] == pytest.approx(proceeds)          # sold at bar 6's open, unsettled
    assert seen[9][1] == 0.0 and seen[9][0] == pytest.approx(seen[6][0] + proceeds)
    assert model.scans == 30


# -- 5.6 shortable, 5.7 margin interest ------------------------------------------------------


class HardToBorrow:
    def shortable_quantity(self, symbol, time):
        return None

    def fee_rate(self, symbol, time):
        return 0.365

    def rebate_rate(self, symbol, time):
        return 0.0


class ChargeOne:
    def accrue(self, cashbook, portfolio, time):
        return 1.0


def test_default_shortable_and_interest_are_free():
    sh = AlwaysShortable()
    assert sh.shortable_quantity("X", 0) is None and sh.fee_rate("X", 0) == 0.0 == sh.rebate_rate("X", 0)
    assert sh.fee_rates(np.arange(3), 0).tolist() == [0.0, 0.0, 0.0]
    assert NoInterest().accrue(None, None, 0) == 0.0


def test_borrow_fees_and_margin_interest_accrue_at_phase_4():
    fr = frames()

    def setup(c):
        c.broker.set_models(c.datas[0], shortable=HardToBorrow())
    cerebro, strat = run_plan({3: [(0, -10)]}, fr, setup)
    base, _ = run_plan({3: [(0, -10)]}, fr)
    fees = strat.datas[0].security.holding.total_borrow_fees
    assert fees > 0
    assert cerebro.broker.getcash() == pytest.approx(base.broker.getcash() - fees)

    def setup2(c):
        c.broker.set_models(c.datas[1], margin_interest=ChargeOne())
    c2, _ = run_plan({}, fr, setup2)
    assert c2.broker.getcash() == 100_000 - 30.0     # one unit per slice, 30 slices


def test_default_models_skip_the_accrual_phase():
    cerebro, _ = run_plan({3: [(0, -10)]})
    assert cerebro.broker._accruals is False


# -- 5.8 corporate actions -------------------------------------------------------------------


def _ns(frame, i):
    return int(num2ns(date2num(frame.index[i].to_pydatetime())))


def test_split_dividend_symbol_change_at_the_start_of_the_bar():
    fr = frames()
    f0 = fr["S0"]
    actions = StaticCorporateActions([
        Dividend(0, _ns(f0, 6), 0.5),
        Split(0, _ns(f0, 6), 2.0),
        SymbolChange(1, _ns(f0, 6), "S1", "S1B"),
    ])

    class Limit(Plan):
        def next(self):
            super().next()
            if len(self) == 5:
                self.lim = self.buy(data=self.datas[0], size=4, exectype=tw.Order.Limit, price=1.0)

    cerebro = cerebro_with(fr, Limit, plan={3: [(0, 10)]})
    cerebro.set_corporate_actions(actions)
    strat = cerebro.run()[0]
    pos = cerebro.broker.getposition(strat.datas[0])
    assert pos.size == 20 and pos.price == f0["open"].iloc[3] / 2
    # the split ran before the dividend (symbol change -> split -> dividend): 20 shares paid
    assert strat.datas[0].security.holding.total_dividends == 20 * 0.5
    assert strat.lim.size == 8 and strat.lim.price == 0.5
    assert strat.datas[1].security.symbol == "S1B"


def test_delisting_carry_and_liquidate():
    fr = frames()
    when = _ns(fr["S0"], 10)
    carry, _ = run_plan({3: [(0, 10)]}, fr, lambda c: c.set_corporate_actions(
        StaticCorporateActions([Delisting(0, when)])))
    assert carry.broker.getposition(carry.datas[0]).size == 10 and carry.datas[0].security.is_delisted

    def setup(c):
        c.set_corporate_actions(StaticCorporateActions([Delisting(0, when, last_price=50.0)]))
        c.broker.set_models(c.datas[0], corporate_actions=OracleCorporateActions(delisting="liquidate"))
    liq, _ = run_plan({3: [(0, 10)]}, fr, setup)
    assert liq.broker.getposition(liq.datas[0]).size == 0
    assert liq.broker.getcash() == pytest.approx(100_000 - 10 * fr["S0"]["open"].iloc[3] + 500.0)
    with pytest.raises(ValueError):
        OracleCorporateActions(delisting="sell")


# -- ModelSet --------------------------------------------------------------------------------


def test_model_set_oracle_realistic_replace():
    ms = ModelSet.oracle(coc=True, checksubmit=False, slip_perc=0.01)
    assert ms.fill.coc and not ms.buying_power.checksubmit and ms.slippage.perc == 0.01
    assert len(list(ms)) == 8
    with pytest.raises(NotImplementedError):
        ModelSet.realistic()
    other = ms.replace(fee=FlatFee())
    assert isinstance(other.fee, FlatFee) and other.fill is ms.fill and not isinstance(ms.fee, FlatFee)
    broker = tw.BackBroker()
    broker.set_coc(True)
    broker.set_checksubmit(False)
    assert broker.models.fill.coc and broker._coc and not broker.models.buying_power.checksubmit


# -- 6.1 clock -------------------------------------------------------------------------------


def test_union_clock_aligns_feeds_and_stops_on_a_repeated_date():
    a = np.array([1.0, 2.0, 3.0, 5.0])
    b = np.array([2.0, 4.0, 4.0, 6.0])     # 4.0 repeated: the feed stops at its first 4.0
    assert delivered_rows(b) == 2 and delivered_rows(a) == 4
    clock = UnionClock([a, b])
    assert clock.times.tolist() == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert clock.has_bar[:, 0].tolist() == [True, True, True, False, True, False]
    assert clock.has_bar[:, 1].tolist() == [False, True, False, True, False, False]
    slices = list(clock)
    assert [s.position for s in slices] == list(range(6))
    assert isinstance(slices[1], TimeSlice) and slices[1].all_have_bar and not slices[0].all_have_bar
    assert slices[0].dtnum == 1.0 and clock.now() == slices[-1].time


def test_timestamps_are_ns_and_hours_always_open():
    day = dt.datetime(2024, 3, 1, 15, 30)
    ns = int(num2ns(date2num(day)))
    assert ns == int(pd.Timestamp(day).value)
    assert num2date(date2num(day)) == day
    assert ALWAYS_OPEN.is_open(ns) and ALWAYS_OPEN.is_trading_day(np.datetime64("2024-03-02"))
    start, end = ALWAYS_OPEN.session(np.datetime64("2024-03-01"))
    assert start == pd.Timestamp("2024-03-01").value and end - start == 86_400_000_000_000 - 1


# -- 6.3 event bus ---------------------------------------------------------------------------


def test_bus_stamps_sorts_by_kind_then_seq_and_defers_events_published_while_delivering():
    bus = EventBus(journal=[])
    seen = []
    bus.publish(TradeEvent(0, 0, 1, "t1"))
    bus.publish(OrderEvent(0, 0, 1, 1, Order.Submitted))
    bus.publish(UniverseEvent(0, 0, "u"))
    bus.publish(OrderEvent(0, 0, 2, 1, Order.Accepted))
    bus.publish(CashEvent(0, 0, 1.0, 2.0))

    def on_order(e):
        seen.append(("order", e.order_ref, e.seq))
        if e.order_ref == 1:
            bus.publish(OrderEvent(0, 0, 3, 1, Order.Submitted))     # waits for the next drain

    bus.subscribe(OrderEvent, on_order)
    bus.subscribe(TradeEvent, lambda e: seen.append(("trade", e.trade, e.seq)))
    assert bus.deliver() == 5
    assert seen == [("order", 1, 2), ("order", 2, 4), ("trade", "t1", 1)]
    assert len(bus) == 1 and [e.seq for e in bus.journal] == [1, 2, 3, 4, 5, 6]
    seen.clear()
    bus.unsubscribe(OrderEvent, on_order)
    assert bus.deliver() == 1 and seen == []


def test_bus_inbox_is_moved_on_at_the_slice_boundary():
    bus = EventBus()
    bus.inbox.put(OrderEvent(0, 0, 9, 0, Order.Completed))
    assert len(bus) == 0 and bus.pump() == 1 and len(bus) == 1
    assert bus.drain()[0].seq == 1 and bus.drain() == []


def test_notifications_reach_the_strategy_orders_first_then_trades():
    _, strat = run_plan({3: [(0, 10), (1, 10)]})
    kinds = [e[0] for e in strat.events]
    assert kinds == ["order"] * 6 + ["trade"] * 2


# -- 6.4 feeds and the state arrays ----------------------------------------------------------


def test_feed_is_a_datafeed():
    f = frames()["S0"]
    feed = tw.feeds.PandasData(dataname=f)
    block = feed.load()
    assert isinstance(block, FeedBlock)
    assert block.timestamps[0] == pd.Timestamp(f.index[0]).value
    assert np.array_equal(block.columns["close"], f["close"].to_numpy())
    assert not feed.is_live() and feed.poll(0) is None


def test_market_state_steps_and_gathers():
    fr = frames(n_sym=2, n_bars=10, late=4)
    feeds = []
    for f in fr.values():
        d = tw.feeds.PandasData(dataname=f)
        d._start()
        feeds.append(d)
    clock = UnionClock([d._dtnum for d in feeds])
    market = MarketState(feeds)
    for d in feeds:   # the line buffers are views on the concatenated arrays
        assert np.shares_memory(d.close.array, market.flat["close"])
    it = iter(clock)
    market.step(next(it))
    assert market.length.tolist() == [1, 1, 0] and not market.listed_all
    assert np.isnan(market.close[2]) and market.close[0] == fr["S0"]["close"].iloc[0]
    for _ in range(4):
        market.step(next(it))
    assert market.length.tolist() == [5, 5, 1] and market.listed_all
    assert market.cursor.tolist() == [4, 4, 0]
    assert market.gather("open", np.array([2])).tolist() == [fr["S2"]["open"].iloc[0]]
    assert feeds[0].close[0] == fr["S0"]["close"].iloc[4] and len(feeds[2]) == 1


def test_portfolio_state_grows_and_keeps_rows():
    pf = PortfolioState(2)
    pf.quantity[1] = 5.0
    pf.grow(40)
    assert len(pf) == 40 and pf.quantity[1] == 5.0 and pf.multiplier[39] == 1.0
    pf.grow(3)
    assert len(pf) == 40


# -- § 3 the sixteen phases ------------------------------------------------------------------


def test_each_slice_runs_the_sixteen_phases_in_order():
    fr = frames(n_bars=5)
    cerebro = cerebro_with(fr, Plan, plan={})
    cerebro._trace = []
    cerebro.run()
    assert len(Phase) == 16
    assert cerebro._trace == list(Phase) * 5


# -- § 7.3 order-preserving reductions -------------------------------------------------------


class Futures(CommInfoBase):
    params = (("stocklike", False), ("margin", 2.0), ("mult", 10.0), ("commtype", CommInfoBase.COMM_FIXED))


@pytest.mark.parametrize("seed", range(6))
def test_vectorised_mark_equals_the_scalar_loop_bit_for_bit(seed):
    rng = np.random.default_rng(seed)
    n = 40
    fr = {}
    index = pd.bdate_range("2023-01-02", periods=20)
    for i in range(n):
        close = 10 ** rng.uniform(-1, 3) * np.exp(np.cumsum(rng.normal(0, 0.03, 20)))
        fr[f"S{i}"] = pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0},
                                   index=index)
    plan = {2: [(i, int(rng.integers(-500, 500)) or 1) for i in rng.permutation(n)[:30]],
            6: [(i, int(rng.integers(-500, 500)) or 1) for i in rng.permutation(n)[:30]]}
    cerebro = cerebro_with(fr, Plan, cash=1e9, plan=plan)
    cerebro.broker.addcommissioninfo(Futures(), name="S3")
    cerebro.broker.setcommission(commission=0.0, mult=3.0, name="S4")
    strat = cerebro.run()[0]
    broker = cerebro.broker
    from tradewind.broker import _mark_terms

    terms = 0.0
    for data, pos in broker.positions.items():
        if pos.size and len(data):
            e, u = _mark_terms(broker.getcommissioninfo(data), pos, data.close[0])
            terms += e
            terms += u
    assert broker._mark_value() == broker.cash + terms
    assert strat.values[-1] == broker.getvalue()


def test_sequential_accumulate_is_the_python_loop():
    rng = np.random.default_rng(0)
    for _ in range(50):
        x = rng.normal(0, 1, 200) * 10.0 ** rng.integers(-8, 8, 200)
        total = 0.0
        for v in x.tolist():
            total += v
        assert float(np.add.accumulate(x)[-1]) == total


# -- cancel (probe ``cancel_timing``) ---------------------------------------------------------


def test_cancel_reaches_only_accepted_orders():
    class Cancel(Plan):
        def next(self):
            super().next()
            if len(self) == 3:
                self.first = self.buy(data=self.datas[0], size=5)
                self.refused = self.cancel(self.first)          # still Submitted: no effect
            if len(self) == 5:
                self.second = self.buy(data=self.datas[1], size=5, exectype=tw.Order.Limit, price=1.0)
            if len(self) == 6:
                self.cancelled = self.cancel(self.second)       # Accepted: cancelled

    cerebro = cerebro_with(frames(), Cancel)
    strat = cerebro.run()[0]
    assert strat.refused is False and strat.first.status == Order.Completed
    assert strat.cancelled is True and strat.second.status == Order.Canceled
    assert ("order", "S1", "Canceled") in strat.events
