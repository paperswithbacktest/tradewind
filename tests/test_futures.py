"""Futures (spec/09): multiplier, daily settlement, margin, margin calls, expiry with forced
close, explicit rolls paid as two orders, continuous series by ratio or difference, negative
prices. Synthetic data only."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.futures import (
    ContinuousData,
    FutureContract,
    FutureExpiry,
    FuturesMargin,
    FutureSpec,
    Roller,
    RollOnDate,
    RollOnVolume,
    add_futures,
    continuous,
    future_table,
)

START = 100_000.0


def _table(contracts, start="2020-01-02", days=None):
    """``contracts``: {symbol: (last_trade_date, closes, volumes or None)}, one row per
    business day from ``start`` while the closes last; open = high = low = close."""
    rows = []
    idx = pd.bdate_range(start, periods=days or max(len(v[1]) for v in contracts.values()))
    for sym, (ltd, closes, vols) in contracts.items():
        for k, (d, c) in enumerate(zip(idx, closes)):
            if c is None:
                continue
            v = vols[k] if vols is not None else 100.0
            rows.append((d, sym, "CL", c, c + 1.0, c - 1.0, c, v, pd.Timestamp(ltd)))
    return pd.DataFrame(rows, columns=["date", "symbol", "root", "open", "high", "low", "close", "volume",
                                       "last_trade_date"])


class _Script(tw.Strategy):
    """Places ``script`` (bar index -> [(feed, size)]); records value and cash each bar."""

    params = (("script", None),)

    def __init__(self):
        self.values, self.cash, self.actions, self.statuses = [], [], [], []

    def notify_corporate_action(self, action):
        self.actions.append(action)

    def notify_order(self, order):
        self.statuses.append((order.data._name, order.getstatusname()))

    def next(self):
        for name, size in self.p.script.get(len(self) - 1, ()):
            data = self.getdatabyname(name)
            if size > 0:
                self.buy(data=data, size=size)
            else:
                self.sell(data=data, size=-size)
        self.values.append(self.broker.getvalue())
        self.cash.append(self.broker.getcash())


def _run(table, script, spec, cash=START, strategy=_Script, **kw):
    engine = tw.Engine()
    chains = add_futures(engine, table, {"CL": spec}, **kw)
    engine.addstrategy(strategy, script=script) if strategy is _Script else engine.addstrategy(strategy)
    engine.broker.setcash(cash)
    strat = engine.run()[0]
    return engine, strat, chains["CL"]


SPEC = FutureSpec(multiplier=1000.0, tick_size=0.01, initial_margin=6000.0, maintenance_margin=5000.0,
                  commission=2.5)


# -- the contract ------------------------------------------------------------------------------------


def test_contract_terms_and_tick():
    c = FutureContract("CL", "CLM0", dt.date(2020, 5, 19), 1000.0, 0.01, 6000.0)
    assert c.maintenance_margin == 6000.0
    assert c.round_price(20.1234) == 20.12
    assert c.symbol_properties().contract_multiplier == 1000.0
    with pytest.raises(ValueError):
        FutureContract("CL", "X", dt.date(2020, 5, 19), 1000.0, initial_margin=1.0, maintenance_margin=2.0)
    with pytest.raises(ValueError):
        future_table(pd.DataFrame({"date": [], "symbol": []}))


# -- P&L, settlement, margin ----------------------------------------------------------------------------


def test_pnl_with_multiplier_and_daily_settlement():
    closes = [50.0, 50.0, 51.0, 49.5, 52.0, 53.0]
    t = _table({"CLM0": ("2020-03-31", closes, None)})
    engine, s, chain = _run(t, {0: [("CLM0", 3)], 3: [("CLM0", -3)]}, SPEC)
    feed = chain.feed("CLM0")
    m, fee = 1000.0, 2.5 * 3
    # bought 3 at the open of bar 1 (50.0): only the fee leaves the cash, no notional
    assert s.cash[1] == pytest.approx(START - fee)
    # each settlement moves q x change x m to the cash; the value equals the cash after it
    assert s.cash[2] == pytest.approx(START - fee + 3 * 1.0 * m)
    assert s.cash[3] == pytest.approx(START - fee + 3 * -0.5 * m)
    assert s.values == pytest.approx(s.cash)
    # sold 3 at the open of bar 4 (52.0): realised P&L = (52 - 50) x 3 x 1000
    pf = engine.broker.portfolio
    assert pf.realized_pnl[feed.security.id] == pytest.approx(6000.0)
    assert s.cash[4] == pytest.approx(START + 6000.0 - 2 * fee)
    assert s.cash[-1] == s.cash[4]
    # the variation paid over the position's life adds up to its realised P&L
    book = engine.broker.futures
    paid = sum(v.amount for v in book.variation)
    assert paid + 3 * (52.0 - 49.5) * m == pytest.approx(6000.0)
    # executed value in money (notional), commission per contract
    buy = engine.broker.orders[0]
    assert buy.executed.value == pytest.approx(3 * 50.0 * m)
    assert buy.executed.comm == pytest.approx(fee)


def test_settlement_column_is_the_mark():
    t = _table({"CLM0": ("2020-03-31", [50.0, 50.0, 50.0, 50.0], None)})
    t["settlement"] = [50.0, 50.0, 50.5, 51.0]          # the close is the last trade, settle differs
    _engine, s, _ = _run(t, {0: [("CLM0", 1)]}, SPEC)
    assert s.values[2] == pytest.approx(START - 2.5 + 500.0)
    assert s.values[3] == pytest.approx(START - 2.5 + 1000.0)


def test_initial_margin_refuses_an_order_it_does_not_cover():
    t = _table({"CLM0": ("2020-03-31", [50.0] * 4, None)})
    # 13 000 of cash: two contracts (12 000 of initial margin) fit, three do not
    engine, s, chain = _run(t, {0: [("CLM0", 3)], 1: [("CLM0", 2)]}, SPEC, cash=13_000.0)
    assert ("CLM0", "Margin") in s.statuses
    assert engine.broker.getposition(chain.feed("CLM0")).size == 2
    assert engine.broker.futures.requirement(engine.broker, "initial") == pytest.approx(12_000.0)


@pytest.mark.parametrize("policy", ["liquidate", "record"])
def test_margin_call_after_the_settlement(policy):
    # long 2 at 50 with 13 000: maintenance 10 000; a fall of 2.0 leaves 9 000 of equity
    closes = [50.0, 50.0, 49.0, 48.0, 48.0, 48.0]
    t = _table({"CLM0": ("2020-03-31", closes, None)})
    spec = FutureSpec(1000.0, 0.01, 6000.0, 5000.0, commission=0.0)
    engine, _s, chain = _run(t, {0: [("CLM0", 2)]}, spec, cash=13_000.0, on_margin_call=policy)
    book = engine.broker.futures
    # called again at every settlement while the equity stays under the maintenance margin
    assert len(book.margin_calls) == (1 if policy == "liquidate" else 3)
    call = book.margin_calls[0]
    assert call.equity == pytest.approx(9_000.0)
    assert call.maintenance == pytest.approx(10_000.0)
    assert call.deficit == pytest.approx(12_000.0 - 9_000.0)
    pos = engine.broker.getposition(chain.feed("CLM0")).size
    if policy == "liquidate":
        # cut so that the initial margin of the rest fits in the equity: keep 1 (6 000 <= 9 000)
        assert len(call.orders) == 1 and call.orders[0].info.get("margin_call")
        assert pos == 1
    else:
        assert not call.orders and pos == 2


def test_order_target_value_sizes_on_the_notional():
    t = _table({"CLM0": ("2020-03-31", [50.0] * 4, None)})

    class Target(tw.Strategy):
        def next(self):
            if len(self) == 1:
                self.order_target_value(data=self.datas[0], target=160_000.0)

    engine, _s, chain = _run(t, None, SPEC, cash=START, strategy=Target)
    # 50 x 1000 = 50 000 per contract: 3 contracts
    assert engine.broker.getposition(chain.feed("CLM0")).size == 3


def test_fill_prices_round_to_the_tick():
    t = _table({"CLM0": ("2020-03-31", [50.0] * 4, None)})
    engine = tw.Engine()
    add_futures(engine, t, {"CL": FutureSpec(1000.0, 0.25, 0.0)})
    engine.broker.set_slippage_fixed(0.1, slip_open=True)
    engine.addstrategy(_Script, script={0: [("CLM0", 1)]})
    engine.run()
    assert engine.broker.orders[0].executed.price == 50.0      # 50.1 on a 0.25 grid


# -- expiry ---------------------------------------------------------------------------------------------


def test_forced_close_at_the_last_trading_date():
    # last trading date 2020-01-08 (bar 4); held through it, no roll
    closes = [50.0, 50.0, 51.0, 52.0, 53.0]
    t = _table({"CLG0": ("2020-01-08", closes, None),
                "CLH0": ("2020-02-20", [60.0] * 8, None)})
    engine, s, chain = _run(t, {0: [("CLG0", 2)], 4: [("CLG0", 1)]}, SPEC)
    feed = chain.feed("CLG0")
    exp = [a for a in s.actions if isinstance(a, FutureExpiry) and a.security == feed.security.id]
    assert len(exp) == 1 and exp[0].forced
    assert exp[0].quantity == 2 and exp[0].settle_price == 53.0
    assert exp[0].fee == pytest.approx(5.0)
    assert engine.broker.getposition(feed).size == 0
    assert feed.security.is_delisted
    assert engine.broker.portfolio.realized_pnl[feed.security.id] == pytest.approx(2 * 3.0 * 1000.0)
    # the order placed on the last trading day had no bar left to fill on
    assert s.statuses[-1] == ("CLG0", "Expired")
    # the cash: entry fee, P&L, forced-close fee
    assert s.cash[-1] == pytest.approx(START - 5.0 + 6000.0 - 5.0)
    # the trade list sees the position closed
    assert all(not tr.isopen for tr in engine.broker.trades.values())


# -- rolls ----------------------------------------------------------------------------------------------


class _Roll(tw.Strategy):
    params = (("rule", None), ("chain", None), ("n", 2))

    def __init__(self):
        self.roller = Roller(self, self.p.chain, self.p.rule)
        self.values, self.cash = [], []

    def next(self):
        self.roller.target(self.p.n)
        self.values.append(self.broker.getvalue())
        self.cash.append(self.broker.getcash())


def _roll_run(t, rule, slippage=0.0, n=2, spec=SPEC):
    engine = tw.Engine()
    chain = add_futures(engine, t, {"CL": spec})["CL"]
    if slippage:
        engine.broker.set_slippage_fixed(slippage, slip_open=True)
    engine.addstrategy(_Roll, rule=rule, chain=chain, n=n)
    engine.broker.setcash(START)
    return engine, engine.run()[0], chain


def test_roll_on_date_is_paid_as_two_orders():
    # front CLG0 expires 2020-01-10; with 3 days the roll is decided on 01-07, filled 01-08
    t = _table({"CLG0": ("2020-01-10", [50.0, 50.0, 51.0, 52.0, 53.0, 54.0, 55.0], None),
                "CLH0": ("2020-02-20", [55.0 + k for k in range(10)], None)})
    engine, s, chain = _roll_run(t, RollOnDate(3), slippage=0.02)
    rolls = s.roller.rolls
    assert len(rolls) == 1 and (rolls[0].old, rolls[0].new, rolls[0].quantity) == ("CLG0", "CLH0", 2)
    sell, buy = rolls[0].orders
    assert sell.status == sell.Completed and buy.status == buy.Completed
    # both legs filled at the next open (01-08) moved against the trade by the slippage
    assert sell.executed.price == pytest.approx(53.0 - 0.02)
    assert buy.executed.price == pytest.approx(59.0 + 0.02)
    # the roll paid two fees and two slippages: 4 x 2.5 + 2 x 2 x 0.02 x 1000
    roll_cost = 4 * 2.5 + 2 * 2 * 0.02 * 1000.0
    # long 2 from 50.02 (bought at 50 + slippage): +2 x (52.98 - 50.02) on the old, then
    # the new from 59.02 to its last close
    first_fee = 2 * 2.5
    old_leg = 2 * (52.98 - 50.02) * 1000.0
    new_leg = 2 * (64.0 - 59.02) * 1000.0
    assert s.values[-1] == pytest.approx(START - first_fee + old_leg + new_leg - 4 * 2.5)
    assert roll_cost == pytest.approx(90.0)
    assert engine.broker.getposition(chain.feed("CLG0")).size == 0
    assert engine.broker.getposition(chain.feed("CLH0")).size == 2
    # no forced close: the roll came first
    assert not any(e.forced for e in engine.broker.futures.expiries)


def test_roll_on_volume_moves_when_the_next_contract_trades_more():
    vol_g = [1000, 1000, 900, 400, 300, 200, 100, 50]
    vol_h = [100, 200, 500, 800, 900, 1000, 1000, 1000, 1000, 1000]
    t = _table({"CLG0": ("2020-01-13", [50.0] * 8, vol_g),
                "CLH0": ("2020-02-20", [51.0] * 10, vol_h)})
    _engine, s, _chain = _roll_run(t, RollOnVolume(days_before=1))
    rolls = s.roller.rolls
    assert len(rolls) == 1
    # CLH0 first trades more on bar 3 (2020-01-07): decided there, filled on 01-08
    assert tw.num2date(rolls[0].time).date() == dt.date(2020, 1, 7)


# -- continuous series ------------------------------------------------------------------------------------


def test_continuous_ratio_keeps_returns_and_difference_keeps_points():
    g = [50.0, 50.0, 51.0, 52.0, 53.0]
    h = [55.0, 55.5, 56.0, 57.0, 58.0, 59.0, 60.0]
    t = _table({"CLG0": ("2020-01-09", g, None), "CLH0": ("2020-02-20", h, None)})
    rule = RollOnDate(3)               # decided 01-06 (3 days before 01-09), held from 01-07
    ratio = continuous(t, rule, "ratio")
    diff = continuous(t, rule, "difference")
    none = continuous(t, rule, "none")
    assert list(ratio["symbol"]) == ["CLG0", "CLG0", "CLH0", "CLH0", "CLH0", "CLH0"]
    roll_day = pd.Timestamp("2020-01-07")
    assert bool(ratio.loc[roll_day, "rolled"])
    # the last segment is the contract itself
    assert ratio["close"].iloc[-1] == 60.0 and diff["close"].iloc[-1] == 60.0
    # ratio: the return across the roll is the new contract's own (57 / 56 - 1)
    r = ratio["close"].pct_change()
    assert r.loc[roll_day] == pytest.approx(57.0 / 56.0 - 1.0)
    assert r.loc[pd.Timestamp("2020-01-06")] == pytest.approx(51.0 / 50.0 - 1.0)
    # difference: the point change across the roll is the new contract's own (+1.0)
    d = diff["close"].diff()
    assert d.loc[roll_day] == pytest.approx(1.0)
    assert diff.loc[pd.Timestamp("2020-01-03"), "close"] == pytest.approx(50.0 + 5.0)
    # none: the gap stays
    assert none["close"].diff().loc[roll_day] == pytest.approx(57.0 - 51.0)


def test_continuous_feed_is_for_signals_only():
    t = _table({"CLM0": ("2020-03-31", [50.0] * 5, None)})
    engine = tw.Engine()
    chain = add_futures(engine, t, {"CL": SPEC}, continuous_adjust="difference")["CL"]
    assert isinstance(chain.continuous, ContinuousData)
    engine.addstrategy(_Script, script={1: [("CL_cont", 1)]})
    s = engine.run()[0]
    assert ("CL_cont", "Margin") in s.statuses
    assert isinstance(chain.feed("CLM0").security.models.buying_power, FuturesMargin)


# -- negative prices (WTI, 20 April 2020) ----------------------------------------------------------------


def _wti_april_2020():
    # the May contract (CLK0) expires 2020-04-21 and settles at -37.63 on the 20th; the
    # June contract (CLM0) stays positive. Business days from 2020-04-14.
    k = [20.11, 19.87, 19.87, 18.27, -37.63, 10.01]
    m = [25.0, 25.5, 25.6, 25.0, 20.43, 11.57, 13.78, 16.50, 15.06, 12.78]
    return _table({"CLK0": ("2020-04-21", k, None), "CLM0": ("2020-06-22", m, None)}, start="2020-04-14")


def test_negative_price_difference_adjustment_and_backtest():
    t = _wti_april_2020()
    # rolled 1 day before the last trade: decided on the 20th, at -37.63
    rule = RollOnDate(1)
    diff = continuous(t, rule, "difference")
    # the roll gap is measured on the 20th: 20.43 - (-37.63) moves the May segment up
    assert diff.loc[pd.Timestamp("2020-04-20"), "close"] == pytest.approx(-37.63 + (20.43 + 37.63))
    assert np.isfinite(diff["close"]).all()
    assert diff["close"].iloc[-1] == 12.78
    with pytest.raises(ValueError, match="difference"):
        continuous(t, rule, "ratio")

    class Hold(tw.Strategy):
        """Long 1 of the front through the negative print; an SMA on the continuous series."""

        def __init__(self):
            self.sma = tw.indicators.SMA(self.getdatabyname("CL_cont"), period=2)
            self.values = []

        def next(self):
            if not self.values:
                self.buy(data=self.getdatabyname("CLK0"), size=1)
            self.values.append(self.broker.getvalue())

    engine = tw.Engine()
    add_futures(engine, t, {"CL": FutureSpec(1000.0, 0.01, 0.0, commission=0.0)}, continuous_adjust="difference",
                continuous_rule=rule)
    engine.addstrategy(Hold)
    engine.broker.setcash(START)
    s = engine.run()[0]
    assert np.isfinite(s.values).all()
    # the first ``next`` is on the 16th (the continuous series starts on the 15th, its SMA
    # needs two bars): bought at the open of the 17th (18.27), marked at -37.63 on the 20th,
    # closed by force at the settlement of its last trading day, the 21st (10.01)
    assert engine.broker.orders[0].executed.price == 18.27
    exp = [e for e in engine.broker.futures.expiries if e.forced]
    assert len(exp) == 1 and exp[0].settle_price == 10.01
    assert min(s.values) == pytest.approx(START + (-37.63 - 18.27) * 1000.0)
    assert s.values[-1] == pytest.approx(START + (10.01 - 18.27) * 1000.0)
