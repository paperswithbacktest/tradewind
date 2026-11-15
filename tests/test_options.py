"""Options (spec/06 § 5): contracts, Black-Scholes-Merton, chains, the multiplier in the
broker, and the expiry of contracts (exercise, assignment, cash settlement)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

import tradewind as tw
from tradewind.options import (
    CALL,
    CASH,
    PUT,
    OptionChain,
    OptionContract,
    OptionExpiry,
    add_options,
    bsm_greeks,
    bsm_price,
    implied_volatility,
    occ_symbol,
    option_table,
    parse_occ,
    synthetic_option_table,
)

# -- Black-Scholes-Merton --------------------------------------------------------------------------


def test_bsm_textbook_values_and_parity():
    # S = K = 100, one year, r = 5 %, no dividend, 20 % vol: the textbook 10.4506 / 5.5735
    c = float(bsm_price(100, 100, 1.0, 0.05, 0.0, 0.2, CALL))
    p = float(bsm_price(100, 100, 1.0, 0.05, 0.0, 0.2, PUT))
    assert c == pytest.approx(10.450584, abs=1e-6)
    assert p == pytest.approx(5.573526, abs=1e-6)
    # put-call parity with a dividend yield, on a grid
    s, k = 100.0, np.linspace(60, 140, 17)
    t, r, q, v = 0.37, 0.03, 0.015, 0.31
    lhs = bsm_price(s, k, t, r, q, v, CALL) - bsm_price(s, k, t, r, q, v, PUT)
    assert np.allclose(lhs, s * np.exp(-q * t) - k * np.exp(-r * t), atol=1e-12)


def test_bsm_at_expiry_is_intrinsic():
    assert float(bsm_price(105, 100, 0.0, 0.05, 0.0, 0.2, CALL)) == pytest.approx(5.0)
    assert float(bsm_price(95, 100, 0.0, 0.05, 0.0, 0.2, CALL)) == 0.0
    assert float(bsm_price(95, 100, 0.5, 0.0, 0.0, 0.0, PUT)) == pytest.approx(5.0)


@pytest.mark.parametrize("right", [CALL, PUT])
def test_greeks_match_finite_differences(right):
    s, k, t, r, q, v = 103.0, 100.0, 0.4, 0.02, 0.01, 0.27
    g = bsm_greeks(s, k, t, r, q, v, right)

    def f(**kw):
        a = {"spot": s, "strike": k, "t": t, "rate": r, "div": q, "vol": v}
        a.update(kw)
        return float(bsm_price(a["spot"], a["strike"], a["t"], a["rate"], a["div"], a["vol"], right))

    h = 1e-4
    assert float(g["delta"]) == pytest.approx((f(spot=s + h) - f(spot=s - h)) / (2 * h), rel=1e-6)
    assert float(g["gamma"]) == pytest.approx((f(spot=s + h) - 2 * f() + f(spot=s - h)) / h**2, rel=1e-4)
    assert float(g["vega"]) == pytest.approx((f(vol=v + h) - f(vol=v - h)) / (2 * h), rel=1e-6)
    assert float(g["rho"]) == pytest.approx((f(rate=r + h) - f(rate=r - h)) / (2 * h), rel=1e-6)
    # theta per year: the price's change as time to expiry *shrinks*
    assert float(g["theta"]) == pytest.approx(-(f(t=t + h) - f(t=t - h)) / (2 * h), rel=1e-6)


def test_implied_volatility_round_trip_and_bounds():
    k = np.array([80, 90, 100, 110, 120.0])
    vols = np.array([0.15, 0.25, 0.35, 0.45, 0.8])
    right = np.array([PUT, PUT, CALL, CALL, CALL])
    px = bsm_price(100, k, 0.25, 0.01, 0.0, vols, right)
    assert np.allclose(implied_volatility(px, 100, k, 0.25, 0.01, 0.0, right), vols, atol=1e-10)
    # below intrinsic, above the spot, zero, expired: no volatility
    bad = implied_volatility([10.0, 120.0, 0.0, 2.0], 100, [100, 90, 100, 100], [0.5, 0.5, 0.5, 0.0],
                             0.0, 0.0, [PUT, CALL, CALL, CALL])
    assert np.isnan(bad[0]) or bad[0] > 0          # a 10 put at the money is a plausible price
    assert np.isnan(bad[1:]).all()


# -- contracts and chains ---------------------------------------------------------------------------


def test_occ_symbols_round_trip():
    c = parse_occ("SPY190118P00250000")
    assert (c.underlying, c.expiry, c.strike, c.right, c.multiplier) == ("SPY", dt.date(2019, 1, 18), 250.0,
                                                                       PUT, 100.0)
    assert parse_occ("SPY   190118P00250000").strike == 250.0
    assert parse_occ("OPT:SPXW240621C05432500").strike == 5432.5
    assert occ_symbol("SPY", dt.date(2019, 1, 18), PUT, 250) == "SPY190118P00250000"
    assert OptionContract("SPY", dt.date(2019, 1, 18), 250.0, PUT).symbol == "SPY190118P00250000"
    with pytest.raises(ValueError):
        parse_occ("SPY")
    with pytest.raises(ValueError):
        OptionContract("SPY", dt.date(2019, 1, 18), 250.0, "straddle")


def _chain():
    rows = []
    for exp in ("2019-01-18", "2019-01-25", "2019-02-15", "2019-03-15"):
        for k in range(90, 111, 5):
            for r in (CALL, PUT):
                t = (pd.Timestamp(exp) - pd.Timestamp("2019-01-02")).days / 365
                px = float(bsm_price(101.0, k, t, 0.0, 0.0, 0.3, r))
                rows.append((occ_symbol("X", exp, r, k), "2019-01-02", "X", exp, float(k), r, px, 10))
    t = option_table(pd.DataFrame(rows, columns=["symbol", "date", "underlying", "expiration", "strike",
                                                 "option_type", "close", "volume"]))
    return OptionChain(t, spot=101.0, date="2019-01-02")


def test_chain_filters():
    ch = _chain()
    assert len(ch) == 4 * 5 * 2
    assert set(ch.puts_only().frame["option_type"]) == {PUT}
    assert set(ch.strikes(-1, 1).frame["strike"]) == {95.0, 100.0, 105.0}     # 100 is nearest 101
    assert set(ch.expiration(10, 30).frame["expiration"].dt.day) == {18, 25}  # 16 and 23 days
    assert set(ch.front_month().frame["expiration"].dt.day) == {18}
    assert set(ch.standards_only().frame["expiration"].dt.strftime("%m-%d")) == {"01-18", "02-15", "03-15"}
    m = ch.moneyness(0.9, 1.0).frame["strike"]
    assert m.min() >= 0.9 * 101 and m.max() <= 101
    pick = ch.puts_only().nearest(strike=96, days=30)
    assert len(pick) == 1 and pick.contracts()[0].strike == 95.0 and pick.contracts()[0].expiry == dt.date(2019, 1, 25)
    # delta from the computed implied volatility
    d = ch.calls_only().front_month().delta(0.3, 0.7)
    assert set(d.frame["strike"]) == {100.0}          # 105 is below 0.3 at 16 days
    assert np.allclose(d.frame["implied_volatility"], 0.3, atol=1e-9)


# -- the engine: multiplier and expiry ---------------------------------------------------------------


def _underlying(closes, start="2019-01-02"):
    idx = pd.bdate_range(start, periods=len(closes))
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1e6}, index=idx)


def _table(und, contracts, prices):
    """One row per (contract, day) of the underlying's dates up to expiry, price from ``prices``."""
    rows = []
    for c, px in zip(contracts, prices):
        for d, p in zip(und.index, px):
            if d.date() > c.expiry:
                break
            rows.append((c.symbol, d, c.underlying, pd.Timestamp(c.expiry), c.strike, c.right, p, p, p, p, 5.0))
    return pd.DataFrame(rows, columns=["symbol", "date", "underlying", "expiration", "strike", "option_type",
                                       "open", "high", "low", "close", "volume"])


class _Script(tw.Strategy):
    """Places the orders of ``script`` (bar index -> list of (feed name, size)); records."""

    params = (("script", None),)

    def __init__(self):
        self.values, self.cash, self.actions = [], [], []

    def notify_corporate_action(self, action):
        self.actions.append(action)

    def next(self):
        for name, size in self.p.script.get(len(self) - 1, ()):
            data = self.getdatabyname(name)
            (self.buy if size > 0 else self.sell)(data, size=abs(size))
        self.values.append(self.broker.getvalue())
        self.cash.append(self.broker.getcash())


def _run(und, contracts, prices, script, settlement="physical", cash=100_000.0):
    engine = tw.Engine()
    stock = engine.adddata(tw.feeds.PandasData(dataname=und), name="X")
    universe = add_options(engine, stock, _table(und, contracts, prices), settlement=settlement)
    engine.addstrategy(_Script, script=script)
    engine.broker.setcash(cash)
    strat = engine.run()[0]
    return engine, strat, universe


def test_multiplier_moves_cash_value_and_pnl():
    und = _underlying([100] * 6)
    c = OptionContract("X", dt.date(2019, 1, 31), 100.0, CALL)
    engine, s, u = _run(und, [c], [[1.0, 1.5, 2.0, 2.5, 3.0, 3.5]], {0: [(c.symbol, 2)], 3: [(c.symbol, -2)]})
    feed = u.feeds[c.symbol]
    assert feed.security.properties.contract_multiplier == 100.0
    # bought 2 at the open of bar 1 (1.5): 300 out; marked at 2.0 then 2.5; sold at 3.0 on bar 4
    assert s.cash[1] == pytest.approx(100_000 - 300)
    assert s.values[2] == pytest.approx(100_000 - 300 + 2 * 2.0 * 100)
    assert s.cash[4] == pytest.approx(100_000 - 300 + 600)
    assert engine.broker.portfolio.realized_pnl[feed.security.id] == pytest.approx(300)
    assert engine.broker.portfolio.multiplier[feed.security.id] == 100.0
    assert engine.broker.portfolio.multiplier[0] == 1.0


def test_order_target_value_sizes_contracts():
    und = _underlying([100] * 4)
    c = OptionContract("X", dt.date(2019, 1, 31), 100.0, CALL)

    class Target(tw.Strategy):
        def next(self):
            if len(self) == 1:
                self.order_target_value(self.getdatabyname(c.symbol), target=1_050.0)

    engine = tw.Engine()
    stock = engine.adddata(tw.feeds.PandasData(dataname=und), name="X")
    u = add_options(engine, stock, _table(und, [c], [[2.0] * 4]))
    engine.addstrategy(Target)
    engine.run()
    # 1 050 of value at 2.00 x 100 a contract: 5 contracts, not 525
    assert engine.broker.getposition(u.feeds[c.symbol]).size == 5


def test_short_put_assigned_delivers_shares_at_the_strike():
    # expiry Friday 2019-01-04 (bar 2); the underlying closes at 90 that day
    und = _underlying([100, 95, 90, 91, 92])
    p = OptionContract("X", dt.date(2019, 1, 4), 95.0, PUT)
    engine, s, u = _run(und, [p], [[2.0, 3.0, 5.0]], {0: [(p.symbol, -3)]})
    (action,) = [a for a in s.actions if isinstance(a, OptionExpiry)]
    assert action.exercised and action.quantity == -3 and action.settle_price == 90.0
    assert action.delivered == 300 and action.cash == pytest.approx(-28_500)
    stock = engine.datas[0]
    pos = engine.broker.getposition(stock)
    assert (pos.size, pos.price) == (300, 95.0)
    assert engine.broker.getposition(u.feeds[p.symbol]).size == 0
    # sold 3 puts at 3.00 (bar 1): +900; assigned on Monday (bar 3) before its fills
    assert s.cash[3] == pytest.approx(100_000 + 900 - 28_500)
    assert s.values[3] == pytest.approx(s.cash[3] + 300 * 91)
    # the option's realised P&L: sold at 3.00, closed at its intrinsic value 5.00
    assert engine.broker.portfolio.realized_pnl[u.feeds[p.symbol].security.id] == pytest.approx(-600)
    assert u.feeds[p.symbol].security.is_delisted


def test_out_of_the_money_expires_worthless_and_cash_settlement():
    und = _underlying([100, 101, 102, 103, 104])
    otm = OptionContract("X", dt.date(2019, 1, 4), 95.0, PUT)
    itm = OptionContract("X", dt.date(2019, 1, 4), 100.0, CALL, settlement=CASH)
    engine, s, _u = _run(und, [otm, itm], [[1.0, 0.5, 0.1], [1.0, 1.5, 2.0]],
                        {0: [(otm.symbol, -1), (itm.symbol, 2)]}, settlement="cash")
    by = {engine.broker.securities[a.security].symbol: a for a in s.actions}
    assert not by[otm.symbol].exercised and by[otm.symbol].cash == 0.0
    assert by[itm.symbol].exercised and by[itm.symbol].cash == pytest.approx(2 * 2.0 * 100)
    assert engine.broker.getposition(engine.datas[0]).size == 0        # no delivery
    # +50 for the put, -300 for the calls, +400 settled
    assert s.cash[-1] == pytest.approx(100_000 + 50 - 300 + 400)


def test_covered_call_assignment_closes_the_shares():
    und = _underlying([100, 100, 110, 111])
    c = OptionContract("X", dt.date(2019, 1, 4), 105.0, CALL)
    engine, s, _u = _run(und, [c], [[1.0, 2.0, 5.0]], {0: [("X", 100), (c.symbol, -1)]})
    stock = engine.datas[0]
    assert engine.broker.getposition(stock).size == 0
    # shares bought at 100, delivered at 105
    assert engine.broker.portfolio.realized_pnl[stock.security.id] == pytest.approx(500)
    assert s.cash[-1] == pytest.approx(100_000 - 10_000 + 200 + 10_500)


def test_open_orders_on_an_expired_contract_end():
    und = _underlying([100, 100, 100, 100, 100])
    c = OptionContract("X", dt.date(2019, 1, 4), 90.0, CALL)
    statuses = []

    class Late(tw.Strategy):
        def notify_order(self, order):
            statuses.append(order.getstatusname())

        def next(self):
            if len(self) == 3:          # the expiry day: no bar after it to fill on
                self.buy(self.getdatabyname(c.symbol), size=1)

    engine = tw.Engine()
    stock = engine.adddata(tw.feeds.PandasData(dataname=und), name="X")
    add_options(engine, stock, _table(und, [c], [[10.0] * 5]))
    engine.addstrategy(Late)
    engine.run()
    assert statuses[-1] == "Expired"
    assert engine.broker.pending == type(engine.broker.pending)()


def test_late_listing_contract_does_not_hold_next():
    und = _underlying([100] * 10)
    c = OptionContract("X", dt.date(2019, 1, 31), 100.0, CALL)
    rows = _table(und, [c], [[1.0] * 10]).iloc[6:]          # trades from bar 6 only
    seen = []

    class S(tw.Strategy):
        def next(self):
            seen.append(len(self))

    engine = tw.Engine()
    stock = engine.adddata(tw.feeds.PandasData(dataname=und), name="X")
    add_options(engine, stock, rows)
    engine.addstrategy(S)
    engine.run()
    assert seen[0] == 1


def test_select_keeps_the_union_and_the_chain_lists_feeds():
    und = _underlying(100 + np.arange(60) * 0.1)
    table = synthetic_option_table(und, "X", vol=0.25)
    engine = tw.Engine()
    stock = engine.adddata(tw.feeds.PandasData(dataname=und), name="X")
    u = add_options(engine, stock, table, select=lambda ch: ch.puts_only().moneyness(0.9, 1.0))
    assert len(u.feeds) and all(f.contract.right == PUT for f in u.feeds.values())
    ch = u.chain(date=und.index[10], spot=101.0)
    assert len(ch) and set(ch.frame["symbol"]) <= set(u.feeds)
    assert all(f.contract.symbol in u.feeds for f in ch.feeds())


def test_example_runs_on_the_synthetic_table():
    import os
    import runpy
    import sys

    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "examples")
    if here not in sys.path:
        sys.path.insert(0, here)
    out = runpy.run_path(os.path.join(here, "06_cash_secured_put.py"), run_name="not_main")
    und = out["bars"]("XYZ", start="2019-01-01", years=1, seed=7)
    table = synthetic_option_table(und, "XYZ", vol=0.22, seed=7)
    engine, strat, _chain = out["run"](table, und, "XYZ")
    assert strat.premium > 0 and engine.broker.getvalue() > 0
