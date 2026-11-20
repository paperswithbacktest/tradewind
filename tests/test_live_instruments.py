"""The daily run's instruments (tradewind.live.daily.instruments): spot forex on IDEALPRO and
indices with their substitutes, on the simulated account and on a stubbed IBKR account.

What is checked: a PWB forex symbol maps to IB's pair in IB's direction, its quantity in the
pair's base currency and back; currencies without an IDEALPRO pair are never traded, with
their reason; an index is refused unless substituted, and an ETF substitute is held at the
same notional (a future at 1 / multiplier, its expiry checked); the IBKR account sends forex
on IDEALPRO and refuses a batch that holds an index; the day's warnings name odd lots,
delisted or frozen names, notes and hard-to-borrow shorts.
"""

from __future__ import annotations

import os
import sys
import types

import pandas as pd
import pytest

import tradewind as tw
from tradewind.live import AdapterError, IBAdapter
from tradewind.live.daily import (
    DailyRunner,
    EngineProvider,
    IBPaperAccount,
    MetaPortfolio,
    SimAccount,
    Sleeve,
    plan_day,
)
from tradewind.live.daily.aggregate import ProposedOrder
from tradewind.live.daily.books import SleeveBook
from tradewind.live.daily.instruments import (
    FX_UNAVAILABLE,
    Substitute,
    fx_pair,
    fx_refusal,
    ibkr_fx_commission,
    is_index,
    substitute_books,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "docs", "examples"))

from _data import bars


# -- forex ---------------------------------------------------------------------------------------------
def test_pwb_forex_symbols_map_to_ib_pairs_in_ibs_direction():
    direct = {"AUDUSD": "AUD.USD", "EURUSD": "EUR.USD", "GBPUSD": "GBP.USD", "NZDUSD": "NZD.USD"}
    inverted = {"JPYUSD": "USD.JPY", "CADUSD": "USD.CAD", "CHFUSD": "USD.CHF", "NOKUSD": "USD.NOK",
                "SEKUSD": "USD.SEK", "PLNUSD": "USD.PLN", "CZKUSD": "USD.CZK", "TRYUSD": "USD.TRY",
                "MXNUSD": "USD.MXN", "ZARUSD": "USD.ZAR", "KRWUSD": "USD.KRW"}
    for sym, ib in {**direct, **inverted}.items():
        pair = fx_pair(sym)
        assert pair is not None and pair.ib_pair == ib and pair.usd_quoted, sym
        assert pair.inverted == (sym in inverted)
        assert fx_refusal(sym) is None
    assert fx_pair("USD.JPY").ib_pair == "USD.JPY"
    for not_fx in ("SPY", "AAPL", "KVUE", "GOOGL", "BRK.B", "EURGBP", "SPX"):
        assert fx_pair(not_fx) is None, not_fx


def test_quantities_go_to_the_pairs_base_currency_and_back():
    jpy = fx_pair("JPYUSD")
    p = 0.0065                                       # USD per JPY
    # 1,000,000 JPY bought = 6,500 USD sold on USD.JPY
    assert jpy.ib_quantity(1_000_000, p) == pytest.approx(-6_500)
    # a USD.JPY position of -6,500 USD at 153.85 JPY per USD holds 1,000,025 JPY
    assert jpy.book_quantity(-6_500, 153.85) == pytest.approx(1_000_025)
    assert jpy.book_price(153.85) == pytest.approx(1 / 153.85)
    aud = fx_pair("AUDUSD")
    assert aud.ib_quantity(-25_000, 0.70) == -25_000 and aud.book_quantity(-25_000, 0.70) == -25_000


def test_currencies_without_idealpro_are_refused_with_their_reason():
    for cur in ("BRL", "CLP", "ISK", "INR", "IDR", "RUB"):
        assert fx_refusal(f"{cur}USD") == FX_UNAVAILABLE[cur]
    assert "RUB" in FX_UNAVAILABLE and "sanctions" in FX_UNAVAILABLE["RUB"]
    assert "not an IDEALPRO currency" in fx_refusal("TWDUSD")
    assert "use the JPYUSD series" in fx_refusal("USDJPY")           # quoted in JPY: not traded
    assert ibkr_fx_commission(10_000) == 2.0 and ibkr_fx_commission(1_000_000) == pytest.approx(20.0)


# -- indices -------------------------------------------------------------------------------------------
def test_indices_are_refused_unless_substituted():
    assert is_index("SPX") and is_index("VIX") and is_index("INDU:IDX") and is_index("SPX.IDX")
    assert not is_index("SPY")
    meta = MetaPortfolio([Sleeve("a", 1.0)])
    assert set(meta.substitutes) == {"SPX"} and meta.substitutes["SPX"].symbol == "SPY"
    assert meta.tradable("SPY") and not meta.tradable("VIX") and "index" in meta.refusal("VIX")
    meta = MetaPortfolio.from_dict({"sleeves": [{"slug": "a", "weight": 1}],
                                    "substitutes": {"SPX": None, "VIX": "VIXY"}, "notes": {"ONDS": "htb"}})
    assert set(meta.substitutes) == {"VIX"} and meta.notes == {"ONDS": "htb"}
    with pytest.raises(ValueError, match="index itself"):
        MetaPortfolio([Sleeve("a", 1.0)], substitutes={"VIX": "SPX"})
    with pytest.raises(ValueError, match="multiplier, expiry and exchange"):
        Substitute.parse({"symbol": "MES", "kind": "future"})


def _book(date, target, close, nav=100_000.0, **kw):
    return SleeveBook(date=date, nav=nav, positions=dict(target), target=dict(target), open=dict(close),
                      close=dict(close), **kw)


def test_plan_never_orders_a_refused_symbol():
    books = {"a": _book("2026-01-05", {"SPX": 10, "BRLUSD": -50_000, "AUDUSD": 40_000, "X": 100},
                        {"SPX": 7000.0, "BRLUSD": 0.18, "AUDUSD": 0.70, "X": 50.0})}
    meta = MetaPortfolio([Sleeve("a", 1.0)], substitutes={"SPX": None}, lot=1)
    plan = plan_day(meta, books, {"SPX": 3, "RUBUSD": 1000}, 100_000.0, "2026-01-05")
    assert {o.symbol for o in plan.orders} == {"AUDUSD", "X"}
    assert set(plan.refused) == {"SPX", "BRLUSD", "RUBUSD"}
    assert "non-deliverable" in plan.refused["BRLUSD"] and "sanctions" in plan.refused["RUBUSD"]


def test_an_etf_substitute_is_held_at_the_same_notional_and_a_future_at_one_over_the_multiplier():
    hist = {"spx": {"2026-10-21": _book("2026-10-21", {"SPX": 74.0}, {"SPX": 7757.17})},
            "etf": {"2026-10-21": _book("2026-10-21", {"SPY": 5.0}, {"SPY": 770.0})}}
    out, warnings = substitute_books(hist, {"SPX": Substitute("SPY")}, {})
    b = out["spx"]["2026-10-21"]
    assert "SPX" not in b.target and b.target["SPY"] == pytest.approx(74 * 7757.17 / 770.0)
    assert b.close["SPY"] == 770.0 and any("SPX -> SPY" in w for w in warnings)
    assert hist["spx"]["2026-10-21"].target == {"SPX": 74.0}             # the input is not changed
    # a price from the loader when no sleeve holds the substitute
    out, _ = substitute_books({"v": {"d": _book("d", {"VIX": 177.0}, {"VIX": 16.62})}},
                              {"VIX": Substitute("VIXY")}, {"VIXY": {"d": (30.0, 31.0)}})
    assert out["v"]["d"].target["VIXY"] == pytest.approx(177 * 16.62 / 31.0) and out["v"]["d"].open["VIXY"] == 30.0
    # no price: the index stays (and the plan refuses it)
    out, warnings = substitute_books({"v": {"d": _book("d", {"VIX": 177.0}, {"VIX": 16.62})}},
                                     {"VIX": Substitute("VIXY")}, {})
    assert out["v"]["d"].target == {"VIX": 177.0} and any("no price of VIXY" in w for w in warnings)
    mes = Substitute.parse({"symbol": "MES", "kind": "future", "multiplier": 5, "expiry": "20261218",
                            "exchange": "CME"})
    out, _ = substitute_books(hist, {"SPX": mes}, {})
    b = out["spx"]["2026-10-21"]
    assert b.target["MES"] == pytest.approx(74 / 5) and b.close["MES"] == pytest.approx(7757.17 * 5)
    assert b.target["MES"] * b.close["MES"] == pytest.approx(74 * 7757.17)   # same notional
    mes.check_expiry("2026-12-01")
    with pytest.raises(ValueError, match="next expiry"):
        mes.check_expiry("2026-12-15")
    assert Substitute.parse({"symbol": "MES", "kind": "future", "multiplier": 5, "expiry": "202612",
                             "exchange": "CME"}).last_day().isoformat() == "2026-12-18"   # third Friday


# -- end to end on the simulated account ---------------------------------------------------------------
_IDX = pd.bdate_range("2025-01-01", periods=120)


def _frame(symbol, scale, seed):
    f = bars(symbol, start="2025-01-01", years=120 / 252, seed=seed)
    f.index = _IDX[: len(f)]
    f[["open", "high", "low", "close"]] *= scale
    return f


FX = {"JPYUSD": _frame("JPYUSD", 0.0065 / 100, 3), "AUDUSD": _frame("AUDUSD", 0.70 / 100, 4),
      "BRLUSD": _frame("BRLUSD", 0.18 / 100, 5)}
IDX = {"SPX": _frame("SPX", 75.0, 6)}
ETF = {"SPY": _frame("SPY", 7.5, 7)}


class Carry(tw.Strategy):
    """Long JPY and BRL, short AUD, a fixed notional each; daily."""

    def next(self):
        for d, w in zip(self.datas, (0.4, -0.3, 0.2), strict=True):
            self.order_target_percent(d, w)


class IndexHolder(tw.Strategy):
    def next(self):
        self.order_target_percent(self.datas[0], 0.9)


def _builder(strategy, frames):
    def build(until):
        cerebro = tw.Cerebro()
        for name, frame in frames.items():
            cerebro.adddata(tw.feeds.PandasData(dataname=frame if until is None else frame.loc[:until]), name=name)
        cerebro.addstrategy(strategy)
        cerebro.broker.setcash(100_000.0)
        return cerebro
    return build


def _etf_prices(symbols, start, end):
    out = {}
    for s in symbols:
        f = ETF[s].loc[start:end]
        out[s] = {str(d.date()): (float(r.open), float(r.close)) for d, r in f.iterrows()}
    return out


def test_forex_and_a_substituted_index_on_the_simulated_account(tmp_path):
    provider = EngineProvider({"fx": _builder(Carry, FX), "spx": _builder(IndexHolder, IDX)}, prices=_etf_prices)
    meta = MetaPortfolio([Sleeve("fx", 0.5), Sleeve("spx", 0.5)], leverage=1.0, lot=1, min_order_notional=50,
                         notes={"SPY": "substitute of SPX"})
    account = SimAccount(str(tmp_path / "sim.json"), cash=110_000.0, commission="ibkr_tiered")
    runner = DailyRunner(meta, provider, account, str(tmp_path / "run"), log=lambda *a: None)
    days = [str(d.date()) for d in _IDX[-4:]]
    reports = [runner.run_day(d, send=True) for d in days]
    first = reports[0]
    symbols = {o.symbol for o in first.plan.orders}
    assert symbols == {"JPYUSD", "AUDUSD", "SPY"}                         # no SPX, no BRL
    assert "BRLUSD" in first.plan.refused
    orders = {o.symbol: o for o in first.plan.orders}
    assert orders["JPYUSD"].quantity > 1e6 and orders["AUDUSD"].quantity < 0      # units of JPY / AUD
    assert orders["JPYUSD"].notional == pytest.approx(0.5 * 110_000 * 0.4, rel=0.1)    # notional in USD
    text = "\n".join(first.warnings)
    assert "forex odd lot: JPYUSD" in text and "not traded: BRLUSD" in text and "note: SPY" in text
    assert "SPX -> SPY" in text
    with open(first.proposal_md, encoding="utf-8") as fh:
        md = fh.read()
    assert "| JPYUSD |" in md and "USD.JPY | SELL" in md and "AUD.USD | SELL" in md
    # the account holds what was planned, and the forex commission is IDEALPRO's minimum
    held = account.snapshot().positions
    assert set(held) == {"JPYUSD", "AUDUSD", "SPY"}
    assert held["JPYUSD"] == reports[-2].plan.target["JPYUSD"]
    assert all(r.row["costs_paid"] >= 0 for r in reports)


def test_stale_and_frozen_names_notes_and_hard_to_borrow_shorts_are_flagged(tmp_path):
    hist = {"a": {d: _book(d, {"KVUE": 100.0, "ONDS": -1000.0, "X": 10.0},
                           {"KVUE": 19.04, "ONDS": 5.79 + i, "X": 50.0 + i}, stale_bars={"KVUE": "2025-11-03"})
                  for i, d in enumerate(["2026-11-02", "2026-11-03", "2026-11-04"])},
            "b": {d: _book(d, {"Y": 10.0}, {"Y": 10.0}) for d in ["2026-11-02", "2026-11-03", "2026-11-04"]}}

    class Fixed:
        def books(self, as_of=None, last_n=2):
            return hist

    class Borrow(SimAccount):
        def shortable(self, symbols):
            return {"ONDS": 2.0}

    meta = MetaPortfolio([Sleeve("a", 0.5), Sleeve("b", 0.5)], notes={"ONDS": "hard to borrow on 2026-11-20"},
                         borrow_check=True)
    runner = DailyRunner(meta, Fixed(), Borrow(str(tmp_path / "s.json")), str(tmp_path / "r"), today="2026-11-04",
                         log=lambda *a: None)
    w = "\n".join(runner.run_day().warnings)
    assert "watch: KVUE: last price 2025-11-03" in w
    assert "watch: Y: close unchanged at 10 over the last 3 closes" in w
    assert "watch: X" not in w
    assert "note: ONDS: hard to borrow on 2026-11-20" in w
    assert "watch: ONDS: hard to borrow (IB shortable 2)" in w


def test_a_held_feed_whose_data_stopped_is_recorded_as_stale():
    frames = {"A": _frame("A", 1.0, 1), "K": _frame("K", 1.0, 2).iloc[:100]}

    class Hold(tw.Strategy):
        def next(self):
            if len(self) == 5:
                self.order_target_percent(self.datas[1], 0.5)

    provider = EngineProvider({"h": _builder(Hold, frames)})
    books = provider.books(None, last_n=2)["h"]
    last = books[max(books)]
    assert last.stale_bars == {"K": str(frames["K"].index[-1].date())}


# -- the IBKR paper account (stubbed) ------------------------------------------------------------------
class _Event:
    def connect(self, h):
        pass


class _Ticker:
    shortable = float("nan")


class _StubIB:
    def __init__(self):
        for name in ("orderStatusEvent", "execDetailsEvent", "commissionReportEvent", "errorEvent",
                     "disconnectedEvent"):
            setattr(self, name, _Event())
        self.connected = False
        self.placed = []
        self.cancelled = []
        self.tickers = {}
        self.pos = []
        self.fill_list = []

    def connect(self, host, port, clientId, readonly=False, account=""):
        self.connected = True

    def disconnect(self):
        self.connected = False

    def isConnected(self):
        return self.connected

    def managedAccounts(self):
        return ["DU1234567"]

    def qualifyContracts(self, contract):
        return [contract]

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))

    def openTrades(self):
        return []

    def accountValues(self, account=""):
        v = types.SimpleNamespace
        return [v(tag="TotalCashValue", value="100000", currency="USD"),
                v(tag="NetLiquidation", value="110000", currency="USD")]

    def positions(self, account=""):
        return self.pos

    def fills(self):
        return self.fill_list

    def sleep(self, seconds):
        for sym, t in self.tickers.items():
            t.shortable = {"ONDS": 2.0, "AAPL": 3.0}.get(sym, float("nan"))

    def reqMarketDataType(self, kind):
        self.data_type = kind

    def reqMktData(self, contract, ticks, snapshot, regulatory):
        assert ticks == "236"
        t = self.tickers[contract.symbol] = _Ticker()
        return t

    def cancelMktData(self, contract):
        self.cancelled.append(contract.symbol)


def _ibm():
    ns = types.SimpleNamespace
    return ns(Order=lambda **kw: ns(**kw),
              Stock=lambda s, e, c: ns(symbol=s, exchange=e, currency=c, secType="STK"),
              Forex=lambda pair, exchange: ns(symbol=pair[:3], currency=pair[3:], exchange=exchange, secType="CASH"),
              Future=lambda s, expiry, exchange, currency, multiplier: ns(
                  symbol=s, lastTradeDateOrContractMonth=expiry, exchange=exchange, currency=currency,
                  multiplier=multiplier, secType="FUT"))


def _account(stub, send=True):
    adapter = IBAdapter("127.0.0.1", 4002, 941, ib=stub, ib_module=_ibm(), readonly=not send)
    return IBPaperAccount(port=4002, adapter=adapter, send=send, ack_wait=0.0)


def test_ibkr_account_sends_forex_on_idealpro_in_ibs_direction():
    stub = _StubIB()
    account = _account(stub)
    out = account.place([ProposedOrder("JPYUSD", 4_000_000, 0.0065, 0, 4_000_000, 4e6),
                         ProposedOrder("AUDUSD", -30_000, 0.70, 0, -30_000, -3e4),
                         ProposedOrder("SPY", 10, 700.0, 0, 10, 10.0)], "2026-12-15")
    assert [o["status"] for o in out] == ["sent"] * 3
    (c1, o1), (c2, o2), (c3, o3) = stub.placed
    assert (c1.secType, c1.symbol, c1.currency, c1.exchange) == ("CASH", "USD", "JPY", "IDEALPRO")
    assert (o1.action, o1.totalQuantity, o1.orderType, o1.tif) == ("SELL", 26_000.0, "MKT", "GTC")
    assert (c2.symbol, c2.currency, o2.action, o2.totalQuantity) == ("AUD", "USD", "SELL", 30_000.0)
    assert (c3.secType, o3.tif) == ("STK", "OPG")
    # under one unit of the pair's base: not sent, and said
    out = account.place([ProposedOrder("JPYUSD", 50, 0.0065, 0, 50, 50.0)], "2026-12-16")
    assert out[0]["status"] == "skipped" and len(stub.placed) == 3


def test_ibkr_account_refuses_a_batch_with_an_index_before_sending_anything():
    stub = _StubIB()
    account = _account(stub)
    with pytest.raises(AdapterError, match="indices"):
        account.place([ProposedOrder("SPY", 10, 700.0, 0, 10, 10.0),
                       ProposedOrder("VIX", 5, 16.0, 0, 5, 5.0)], "2026-12-15")
    assert not stub.placed


def test_ibkr_account_sends_a_future_substitute_on_its_contract():
    stub = _StubIB()
    account = _account(stub)
    mes = Substitute.parse({"symbol": "MES", "kind": "future", "multiplier": 5, "expiry": "20261218",
                            "exchange": "CME"})
    account.set_instruments({"MES": mes.spec()})
    account.place([ProposedOrder("MES", 3, 38_000.0, 0, 3, 3.0)], "2026-12-01")
    contract, order = stub.placed[0]
    assert (contract.secType, contract.lastTradeDateOrContractMonth, contract.exchange, contract.multiplier) == (
        "FUT", "20261218", "CME", "5")
    assert (order.action, order.totalQuantity, order.tif) == ("BUY", 3.0, "DAY")


def test_ibkr_account_reads_forex_positions_and_fills_in_the_books_units():
    stub = _StubIB()
    ns = types.SimpleNamespace
    stub.pos = [ns(contract=ns(symbol="USD", currency="JPY", secType="CASH"), position=-26_000.0, avgCost=153.0),
                ns(contract=ns(symbol="USD", currency="NOK", secType="CASH"), position=5_000.0, avgCost=10.0),
                ns(contract=ns(symbol="AUD", currency="USD", secType="CASH"), position=-30_000.0, avgCost=0.7),
                ns(contract=ns(symbol="SPY", currency="USD", secType="STK"), position=10.0, avgCost=700.0)]
    stub.fill_list = [ns(contract=ns(symbol="USD", currency="JPY", secType="CASH"), time=pd.Timestamp("2026-12-15"),
                         commissionReport=ns(commission=2.0),
                         execution=ns(orderRef="twd-20261215-0001", execId="e1", side="SLD", shares=26_000.0,
                                      price=153.0))]
    account = _account(stub, send=False)
    closes = {"JPYUSD": 0.0065, "NOKUSD": 0.1, "AUDUSD": 0.7, "SPY": 700.0}
    snap = account.snapshot(closes)
    assert snap.positions == pytest.approx({"JPYUSD": 26_000 * 153.0, "NOKUSD": -50_000.0, "AUDUSD": -30_000.0,
                                            "SPY": 10.0})
    fills = account.settle("2026-12-15", closes, closes)
    assert fills[0].symbol == "JPYUSD" and fills[0].quantity == pytest.approx(3_978_000)
    assert fills[0].price == pytest.approx(1 / 153.0) and fills[0].costs == 2.0


def test_ibkr_shortable_reads_tick_236_on_delayed_data():
    stub = _StubIB()
    account = _account(stub, send=False)
    assert account.shortable(["ONDS", "AAPL", "ZZZZ"], wait=0.5) == {"ONDS": 2.0, "AAPL": 3.0}
    assert stub.data_type == 3 and sorted(stub.cancelled) == ["AAPL", "ONDS", "ZZZZ"]
