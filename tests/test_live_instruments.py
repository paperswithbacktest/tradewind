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


DAYS = ["2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04"]


def test_stale_and_frozen_names_notes_and_hard_to_borrow_shorts_are_flagged(tmp_path):
    hist = {"a": {d: _book(d, {"KVUE": 100.0, "ONDS": -1000.0, "X": 10.0},
                           {"KVUE": 19.04, "ONDS": 5.79 + i, "X": 50.0 + i}, stale_bars={"KVUE": "2025-11-03"})
                  for i, d in enumerate(DAYS)},
            "b": {d: _book(d, {"Y": 10.0, "Z": 5.0}, {"Y": 10.0, "Z": 5.0 if d > DAYS[2] else 4.0}) for d in DAYS}}

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
    assert "watch: Y: close unchanged at 10 over the last 5 closes" in w
    assert "watch: X" not in w and "watch: Z" not in w          # two flat closes: the end of a dataset
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


# -- the account's currency ----------------------------------------------------------------------------
def _eurusd(symbols, start, end):
    from test_live_daily import FRAMES

    idx = FRAMES["AAA"].index
    rate = pd.Series(1.10 * (1 + 0.004 * pd.Series(range(len(idx))).mod(7).sub(3).to_numpy()), index=idx)
    out = {}
    for s in symbols:
        if s in ("EURUSD", "GBPUSD"):
            r = rate.loc[start:end] * (1.0 if s == "EURUSD" else 1.2)
            out[s] = {str(d.date()): (float(v), float(v)) for d, v in r.items()}
    return out


def _currency_run(tmp_path, currency=None, *, prices=True, fallback=None, n=8, name="cur", **meta_kw):
    from test_live_daily import UNTIL, _meta, _provider

    from tradewind.live.daily import ReplayProvider

    inner = _provider()
    if prices:
        inner._prices = _eurusd
    replay = ReplayProvider(inner, UNTIL, n + 1)
    directory = str(tmp_path / name)
    account = SimAccount(os.path.join(directory, "sim.json"), cash=100_000.0, currency=currency, base_fx=fallback)
    runner = DailyRunner(_meta(**meta_kw), replay, account, directory, log=lambda *a: None)
    reports = runner.replay(replay.dates()[-n:])
    return runner, reports, pd.read_csv(os.path.join(directory, "tracking.csv"))


def test_a_usd_account_is_unchanged_by_default(tmp_path):
    _runner, reports, journal = _currency_run(tmp_path, prices=False)
    assert reports[0].fx == {"currency": "USD", "origin": "default", "rate": 1.0, "source": "USD account"}
    assert (journal["fx"] == 0).all() and (journal["account_currency"] == "USD").all()
    nav = journal["nav_real"].to_numpy()
    assert abs(journal["gap_cum"]).max() < 1e-6 * nav.max()                 # as before: no gap at all
    assert (journal["nav_base"] == journal["nav_real"]).all()
    with open(reports[-1].proposal_md, encoding="utf-8") as fh:
        assert "account currency **USD** (default)" in fh.read()


def test_a_eur_account_is_sized_in_usd_and_its_fx_effect_is_isolated(tmp_path):
    _runner, reports, journal = _currency_run(tmp_path, "EUR", capital=100_000.0, name="eur")
    fx = reports[0].fx
    assert fx["currency"] == "EUR" and fx["origin"] == "simulated account" and fx["source"].startswith("PWB EURUSD")
    first = reports[0]
    assert first.plan.capital == pytest.approx(100_000.0 * fx["rate"])       # 100,000 EUR in USD
    assert journal["nav_base"].iloc[0] == pytest.approx(100_000.0)
    # the EUR cash moves with EURUSD: the whole gap is the fx term, nothing in the residual
    nav = journal["nav_real"].to_numpy()
    assert abs(journal["fx"]).max() > 100
    assert journal["gap_cum"].iloc[-1] == pytest.approx(journal["fx"].sum(), abs=1e-6 * nav.max())
    assert abs(journal["residual"]).max() < 1e-6 * nav.max()
    assert journal["gap_cum_base"].iloc[-1] == pytest.approx(journal["gap_cum"].iloc[-1] / journal["fx_rate"].iloc[-1])
    # the gap without the account's currency: the fx terms taken out, nothing left
    assert journal["fx_cum"].to_numpy() == pytest.approx(journal["fx"].cumsum().to_numpy())
    assert abs(journal["gap_cum_ex_fx"]).max() < 1e-6 * nav.max() < abs(journal["gap_cum"]).max()
    assert journal["gap_day_ex_fx"].to_numpy() == pytest.approx((journal["gap_day"] - journal["fx"]).to_numpy())
    with open(reports[-1].proposal_md, encoding="utf-8") as fh:
        text = fh.read()
    assert "account currency **EUR** (simulated account)" in text and "- in EUR: NAV" in text
    assert "without fx: today" in text


def test_a_gbp_account_falls_back_on_the_rate_given(tmp_path):
    _runner, reports, journal = _currency_run(tmp_path, "GBP", prices=False, fallback=1.27, name="gbp", n=3)
    assert reports[0].fx["source"] == "--base-fx-rate" and reports[0].fx["rate"] == 1.27
    assert journal["nav_base"].iloc[0] == pytest.approx(100_000.0)
    _runner, reports, _j = _currency_run(tmp_path, "GBP", fallback=1.27, name="gbp2", n=3)
    assert reports[0].fx["source"].startswith("PWB GBPUSD")                   # PWB before the rate given


def test_no_rate_or_a_different_currency_stops_the_run(tmp_path):
    from tradewind.live.daily.runner import AccountCurrencyError

    with pytest.raises(AccountCurrencyError, match="--base-fx-rate"):
        _currency_run(tmp_path, "EUR", prices=False, name="norate", n=2)
    with pytest.raises(AccountCurrencyError, match="account is in GBP"):
        _currency_run(tmp_path, "GBP", fallback=1.27, account_currency="EUR", name="mismatch", n=2)
    _r, reports, _j = _currency_run(tmp_path, "EUR", account_currency="eur", name="forced", n=2)
    assert reports[0].fx["origin"] == "forced"
    with pytest.raises(ValueError, match="is in EUR, not GBP"):
        SimAccount(str(tmp_path / "forced" / "sim.json"), currency="GBP")
    with pytest.raises(AccountCurrencyError, match="capital_currency CHF"):
        _currency_run(tmp_path, "EUR", capital=1000.0, capital_currency="CHF", name="chf", n=2)


def _ib_values(stub, rows):
    v = types.SimpleNamespace
    stub.accountValues = lambda account="": [v(tag=t, value=str(x), currency=c) for t, x, c in rows]


def test_ibkr_account_currency_is_read_off_the_account(tmp_path):
    from tradewind.live.daily.runner import AccountCurrencyError

    hist = {"a": {"2026-11-04": _book("2026-11-04", {"X": 10.0}, {"X": 50.0, "EURUSD": 1.15})}}

    class Fixed:
        def books(self, as_of=None, last_n=2):
            return hist

    def runner(stub, name, **kw):
        account = _account(stub, send=False)
        return DailyRunner(MetaPortfolio([Sleeve("a", 1.0)], **kw), Fixed(), account, str(tmp_path / name),
                           today="2026-11-04", log=lambda *a: None)

    # EUR, IB publishes no USD rate yet: PWB's EURUSD
    stub = _StubIB()
    _ib_values(stub, [("NetLiquidation", 1_000_000, "EUR"), ("TotalCashValue", 1_000_000, "EUR"),
                      ("CashBalance", 1_000_000, "EUR")])
    report = runner(stub, "eur", capital=110_000).run_day()
    assert report.fx == {"currency": "EUR", "origin": "detected", "rate": 1.15, "source": "PWB EURUSD 2026-11-04"}
    assert report.plan.capital == pytest.approx(110_000 * 1.15) and report.row["nav_base"] == pytest.approx(1e6)
    # GBP with IB's own rate (GBP per USD): IB first
    stub = _StubIB()
    _ib_values(stub, [("NetLiquidation", 500_000, "GBP"), ("TotalCashValue", 500_000, "GBP"),
                      ("ExchangeRate", 0.8, "USD")])
    report = runner(stub, "gbp", capital=100_000, capital_currency="USD").run_day()
    assert report.fx["currency"] == "GBP" and report.fx["source"] == "IB" and report.fx["rate"] == pytest.approx(1.25)
    assert report.plan.capital == 100_000 and report.row["nav_real"] == pytest.approx(625_000)
    # forced USD, the account is in EUR: stop
    stub = _StubIB()
    _ib_values(stub, [("NetLiquidation", 1_000_000, "EUR"), ("TotalCashValue", 1_000_000, "EUR")])
    with pytest.raises(AccountCurrencyError, match="says the account is in USD"):
        runner(stub, "forced", account_currency="USD").run_day()
    # a USD account: as before
    report = runner(_StubIB(), "usd").run_day()
    assert report.fx == {"currency": "USD", "origin": "detected", "rate": 1.0, "source": "USD account"}
