"""Kraken, crypto spot (spec/11-live-kraken.md), without the network.

The answers are Kraken's own documentation examples (``tests/fixtures/kraken``) and a small
in-memory venue speaking the same REST shapes (``Venue``). What is checked: the signature on
the documentation's worked example; the nonce; the REST counter; the credentials (environment
only, redacted, scrubbed from errors); the documented answers parsed; the symbols (``BTC`` is
``XBT``); volume rounding and per-pair minimums; the order mapping; **validation mode by
default** and the two-key arming; the guards; executions and statuses from the polls; the
snapshot; a ``LiveEngine`` session; ``KrakenBars`` never passing a forming bar; the daily
account; the fresh-price cache; the secret in no output. One test calls Kraken's public
endpoints and is skipped unless ``TRADEWIND_NETWORK_TESTS=1``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import pickle
import urllib.parse

import pandas as pd
import pytest

import tradewind as tw
from tradewind.live import LiveEngine, LiveFeed, RiskLimits
from tradewind.live.adapter import AdapterError
from tradewind.live.kraken import (
    DAY_NS,
    LIVE_ENV,
    CallCounter,
    Credentials,
    FractionalCommInfo,
    KrakenAdapter,
    KrakenBars,
    KrakenError,
    KrakenREST,
    NonceSource,
    PairTable,
    cl_ord_id,
    order_fields,
    parse_ohlc,
    sign,
)
from tradewind.live.messages import Ack, Canceled, Execution, Notice, OrderRequest, Rejected

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "kraken")
# a key pair that is not a real one: the secret is the documentation's example private key
DOC_SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
SENTINEL_KEY = "SENTINELKEY-not-a-real-key-7f3a"
SENTINEL_SECRET = base64.b64encode(b"sentinel-secret-never-to-be-seen-anywhere").decode()


def fixture(name: str) -> dict:
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        payload = json.load(fh)
    payload.pop("_source", None)
    return payload


# --------------------------------------------------------------------------------------------- #
# Authentication                                                                                #
# --------------------------------------------------------------------------------------------- #
def test_signature_reproduces_the_documentation_example():
    """docs.kraken.com, REST authentication guide: this input must give this API-Sign."""
    got = sign("/0/private/AddOrder",
               "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25",
               1616492376594, DOC_SECRET)
    assert got == "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="


def test_nonce_strictly_increases_in_milliseconds():
    t = [1_700_000_000_000_000_000]
    nonce = NonceSource(lambda: t[0])
    a, b = nonce.next(), nonce.next()
    assert a == 1_700_000_000_000 and b == a + 1          # same millisecond: +1
    t[0] += 5_000_000
    assert nonce.next() == a + 5
    t[0] -= 60_000_000_000                                # the clock steps back: never lower
    assert nonce.next() == a + 6


def test_call_counter_waits_before_overflowing():
    now = [0.0]
    waits = []

    def sleep(s):
        waits.append(s)
        now[0] += s

    counter = CallCounter(15, 0.33, now=lambda: now[0], sleep=sleep)
    for _ in range(15):
        counter.acquire(1)
    assert not waits
    counter.acquire(2)                                     # a trade-history call: 2
    assert waits and waits[0] == pytest.approx(2 / 0.33)


def test_credentials_come_from_the_environment_only_and_never_show():
    with pytest.raises(AdapterError, match="KRAKEN_API_KEY and KRAKEN_API_SECRET"):
        Credentials.from_env({})
    with pytest.raises(AdapterError, match="not a base64"):
        Credentials.from_env({"KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": "not base64 !!"})
    c = Credentials.from_env({"KRAKEN_API_KEY": SENTINEL_KEY, "KRAKEN_API_SECRET": SENTINEL_SECRET})
    for text in (repr(c), str(c), f"{c}", repr([c])):
        assert SENTINEL_KEY not in text and SENTINEL_SECRET not in text
    with pytest.raises(TypeError):
        pickle.dumps(c)
    assert c.scrub(f"bad key {SENTINEL_KEY} / {SENTINEL_SECRET}") == "bad key *** / ***"


# --------------------------------------------------------------------------------------------- #
# A venue speaking Kraken's REST shapes                                                         #
# --------------------------------------------------------------------------------------------- #
class Venue:
    """Kraken in memory: ``transport`` answers public and private calls in the documented
    shapes, checks every private call's signature, and fills armed market orders at the
    last close. ``frames``: daily bars by altname; ``now``: a callable, ns."""

    def __init__(self, now, *, balances=None, frames=None, secret=SENTINEL_SECRET):
        self.now = now
        self.pairs = fixture("asset_pairs.json")["result"]
        self.balances = dict(balances or {"ZUSD": 10_000.0})
        self.frames = frames or {}
        self.secret = secret
        self.requests: list[tuple[str, str, dict, dict]] = []
        self.orders: dict[str, dict] = {}
        self.trades: dict[str, dict] = {}
        self.fill_mode = "full"         # "full", "partial" (half now), "none"
        self.reject_next: str | None = None
        self.answers: dict[str, list] = {}   # method -> queued raw answers (errors, ...)
        self.prices = {"EURUSD": 1.08, "ETHUSD": 3_000.0, "ETHEUR": 2_800.0}
        self._n = 0

    def price(self, altname: str) -> float:
        frame = self.frames.get(altname)
        if frame is None:
            return self.prices.get(altname, 30303.2)
        t = pd.Timestamp(self.now(), unit="ns")
        return float(frame[frame.index <= t]["close"].iloc[-1])

    def _pair(self, name):
        for k, p in self.pairs.items():
            if name in (k, p["altname"], p["wsname"]):
                return k, p
        return None, None

    def ok(self, result):
        return 200, json.dumps({"error": [], "result": result})

    def transport(self, method, url, headers, body, timeout):
        path = urllib.parse.urlparse(url).path
        name = path.rsplit("/", 1)[-1]
        if body is not None:
            data = dict(urllib.parse.parse_qsl(body.decode()))
            assert headers["API-Sign"] == sign(path, body.decode(), data["nonce"], self.secret)
        else:
            data = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.requests.append((method, name, dict(headers), data))
        if self.answers.get(name):
            return self.answers[name].pop(0)
        return getattr(self, "do_" + name)(data)

    # public
    def do_AssetPairs(self, d):
        return self.ok(self.pairs)

    def do_Ticker(self, d):
        out = {}
        for alt in d["pair"].split(","):
            k, p = self._pair(alt)
            out[k] = {"c": [f"{self.price(p['altname']):.5f}", "0.1"]}
        return self.ok(out)

    def do_OHLC(self, d):
        k, p = self._pair(d["pair"])
        frame = self.frames[p["altname"]]
        now = pd.Timestamp(self.now(), unit="ns")
        rows = []
        for ts, r in frame[frame.index < now.normalize()].iterrows():   # committed days
            rows.append([int(ts.timestamp()), str(r.open), str(r.high), str(r.low), str(r.close), str(r.close),
                         str(r.volume), 10])
        rows.append([int(now.normalize().timestamp()), "1", "1", "1", "1", "1", "0", 1])   # forming
        return self.ok({k: rows[-720:], "last": rows[-2][0] if len(rows) > 1 else 0})

    # private
    def do_BalanceEx(self, d):
        return self.ok({a: {"balance": f"{b:.10f}", "hold_trade": "0"} for a, b in self.balances.items()})

    def do_AddOrder(self, d):
        if self.reject_next:
            err, self.reject_next = self.reject_next, None
            return 200, json.dumps({"error": [err]})
        descr = {"order": f"{d['type']} {d['volume']} {d['pair']} @ {d['ordertype']}"}
        if d.get("validate") == "true":
            return self.ok({"descr": descr})
        self._n += 1
        txid = f"O{self._n:05d}-AAAAA-BBBBBB"
        k, _p = self._pair(d["pair"])
        self.orders[txid] = {"status": "open", "vol": d["volume"], "vol_exec": "0", "cl_ord_id": d["cl_ord_id"],
                             "descr": {"pair": d["pair"], "type": d["type"], "ordertype": d["ordertype"]},
                             "trades": [], "key": k}
        return self.ok({"descr": descr, "txid": [txid]})

    def execute(self, txid, fraction=1.0):
        o = self.orders[txid]
        k, p = self._pair(o["descr"]["pair"])
        vol = float(o["vol"]) * fraction if fraction < 1 else float(o["vol"]) - float(o["vol_exec"])
        px = self.price(p["altname"])
        fee = vol * px * 0.0026
        tid = f"T{len(self.trades) + 1:05d}-CCCCC-DDDDDD"
        sign_ = 1 if o["descr"]["type"] == "buy" else -1
        self.trades[tid] = {"ordertxid": txid, "pair": k, "time": self.now() / 1e9, "type": o["descr"]["type"],
                            "ordertype": "market", "price": f"{px:.5f}", "cost": f"{vol * px:.5f}",
                            "fee": f"{fee:.5f}", "vol": f"{vol:.8f}"}
        o["trades"].append(tid)
        o["vol_exec"] = f"{float(o['vol_exec']) + vol:.8f}"
        self.balances[p["base"]] = self.balances.get(p["base"], 0.0) + sign_ * vol
        self.balances[p["quote"]] -= sign_ * vol * px + fee
        if float(o["vol_exec"]) >= float(o["vol"]) - 1e-12:
            o["status"] = "closed"
        return tid

    def do_QueryOrders(self, d):
        for txid in d["txid"].split(","):
            o = self.orders.get(txid)
            if o and o["status"] == "open" and self.fill_mode != "none" and o["descr"]["ordertype"] == "market":
                self.execute(txid, 0.5 if self.fill_mode == "partial" and not o["trades"] else 1.0)
        return self.ok({t: {k: v for k, v in self.orders[t].items() if k != "key"}
                        for t in d["txid"].split(",") if t in self.orders})

    def do_QueryTrades(self, d):
        return self.ok({t: self.trades[t] for t in d["txid"].split(",")})

    def do_TradesHistory(self, d):
        start = float(d.get("start", 0))
        return self.ok({"trades": {t: v for t, v in self.trades.items() if v["time"] > start}, "count": 0})

    def do_OpenOrders(self, d):
        return self.ok({"open": {t: {k: v for k, v in o.items() if k != "key"} for t, o in self.orders.items()
                                 if o["status"] == "open"}})

    def do_CancelOrder(self, d):
        self.orders[d["txid"]]["status"] = "canceled"
        return self.ok({"count": 1})

    def calls(self, name):
        return [r for r in self.requests if r[1] == name]


class Clock:
    def __init__(self, t: pd.Timestamp):
        self.ns = int(t.value)

    def __call__(self):
        return self.ns

    def sleep(self, s):
        self.ns += int(s * 1e9)

    def mono(self):
        return self.ns / 1e9


def make(venue_kwargs=None, *, t="2026-11-20 00:10", live=False, env=None, **kw):
    clock = Clock(pd.Timestamp(t))
    venue = Venue(clock, **(venue_kwargs or {}))
    rest = KrakenREST(Credentials(SENTINEL_KEY, SENTINEL_SECRET), transport=venue.transport, now=clock.mono,
                      sleep=clock.sleep, nonce=NonceSource(clock), public_interval=0.0)
    adapter = KrakenAdapter(rest, live=live, environ=env if env is not None else {}, clock_ns=clock,
                            monotonic=clock.mono, sleep=clock.sleep, poll_every=0.0, **kw)
    return adapter, venue, clock


# --------------------------------------------------------------------------------------------- #
# Documented answers, symbols, minimums, mapping                                                #
# --------------------------------------------------------------------------------------------- #
def test_documented_answers_are_parsed():
    table = PairTable.from_result(fixture("asset_pairs.json")["result"])
    btc = table.pairs["XXBTZUSD"]
    assert (btc.altname, btc.base_name, btc.quote_name, btc.lot_decimals, btc.ordermin, btc.costmin) == \
        ("XBTUSD", "XBT", "USD", 8, 0.0001, 0.5)
    rows = parse_ohlc(fixture("ohlc.json")["result"])
    assert len(rows) == 3 and rows[-1][0] == 1763596800          # the forming row is dropped
    assert rows[0] == (1763424000, 92000.0, 93500.0, 91100.0, 93010.5, 1520.12345678)

    adapter, venue, _ = make()
    venue.answers["BalanceEx"] = [(200, json.dumps(fixture("balance_ex.json")))]
    venue.answers["Ticker"] = [(200, json.dumps(fixture("ticker.json")))]
    adapter.pairs = table
    bal = adapter.balances()
    assert bal["ZUSD"] == (25435.21, pytest.approx(25435.21 - 8249.76))
    assert bal["XXBT"] == (1.2435, pytest.approx(1.2435 - 0.8423))
    assert adapter.last_prices([btc]) == {"XXBTZUSD": 30303.2}
    venue.answers["AddOrder"] = [(200, json.dumps(fixture("add_order.json")))]
    res = adapter.rest.private("AddOrder", {"pair": "XBTUSD"})
    assert res["txid"] == ["OUF4EM-FRGI2-MQMWZD"]
    trades = fixture("trades_history.json")["result"]["trades"]
    ex = adapter._execution("THVRQM-33VKH-UCI7BS", trades["THVRQM-33VKH-UCI7BS"], "tw-1", "BTC")
    assert (ex.quantity, ex.price, ex.commission, ex.time) == (0.02, 30010.0, 0.0, 1688667796880200000)


def test_symbols_resolve_to_kraken_pairs():
    table = PairTable.from_result(fixture("asset_pairs.json")["result"])
    assert table.resolve("BTC").key == "XXBTZUSD"
    assert table.resolve("BTCUSD").key == "XXBTZUSD"
    assert table.resolve("BTC/USD").key == "XXBTZUSD"
    assert table.resolve("XBT").key == "XXBTZUSD"
    assert table.resolve("BTC", "EUR").key == "XXBTZEUR"
    assert table.resolve("BTCUSD", "EUR").key == "XXBTZEUR"    # the books' USD name, traded against EUR
    assert table.resolve("DOGE").key == "XDGUSD"
    assert table.resolve("ETH").key == "XETHZUSD"
    with pytest.raises(AdapterError, match="no USD pair"):
        table.resolve("NOTACOIN")
    assert table.by_base_asset("XXBT", "USD").key == "XXBTZUSD"
    assert table.quote_asset("EUR") == "ZEUR"


def test_volume_rounding_and_minimums():
    table = PairTable.from_result(fixture("asset_pairs.json")["result"])
    btc, eth = table.pairs["XXBTZUSD"], table.pairs["XETHZUSD"]
    assert btc.round_volume(0.123456789) == 0.12345678            # toward zero, 8 decimals
    assert btc.round_volume(-0.123456789) == -0.12345678
    assert btc.round_volume(4e-9) == 0.0
    assert btc.fmt_price(37500.123) == "37500.1"
    assert eth.minimum_refusal(0.001, 3000.0).startswith("volume 0.001 under ETHUSD's ordermin")
    assert btc.minimum_refusal(0.0001, 3000.0).startswith("cost 0.30 under XBTUSD's costmin")
    assert btc.minimum_refusal(0.001, 90_000.0) == ""


def test_order_mapping():
    table = PairTable.from_result(fixture("asset_pairs.json")["result"])
    btc = table.pairs["XXBTZUSD"]
    now = int(pd.Timestamp("2026-11-20 13:00").value)
    cl = cl_ord_id("tw-abc-000001")
    assert len(cl) == 18 and cl.isascii() and cl == cl_ord_id("tw-abc-000001")
    f = order_fields(OrderRequest("tw-abc-000001", "BTC", 0.123456789, "MKT", tif="OPG"), btc, cl_ord=cl,
                     validate=True, now_ns=now)
    assert f == {"pair": "XBTUSD", "type": "buy", "ordertype": "market", "volume": "0.12345678",
                 "oflags": "fciq", "cl_ord_id": cl, "validate": True}
    f = order_fields(OrderRequest("x", "BTC", -0.5, "LMT", limit_price=95000.04, tif="DAY"), btc, cl_ord=cl,
                     validate=False, now_ns=now)
    assert (f["type"], f["ordertype"], f["price"], f["timeinforce"]) == ("sell", "limit", "95000.0", "GTD")
    assert f["expiretm"] == str(int(pd.Timestamp("2026-11-21").timestamp())) and "validate" not in f
    f = order_fields(OrderRequest("x", "BTC", -0.5, "STP LMT", limit_price=80000, stop_price=81000, tif="GTC"),
                     btc, cl_ord=cl, validate=False, now_ns=now)
    assert (f["ordertype"], f["price"], f["price2"], f["timeinforce"]) == ("stop-loss-limit", "81000.0",
                                                                           "80000.0", "GTC")
    good = int(pd.Timestamp("2026-11-25").value)
    f = order_fields(OrderRequest("x", "BTC", -0.5, "STP", stop_price=81000, tif="GTD", good_till=good), btc,
                     cl_ord=cl, validate=False, now_ns=now)
    assert (f["ordertype"], f["expiretm"]) == ("stop-loss", str(good // 10**9))
    with pytest.raises(ValueError, match="no Kraken counterpart"):
        order_fields(OrderRequest("x", "BTC", 1, "MOC"), btc, cl_ord=cl, validate=True, now_ns=now)


# --------------------------------------------------------------------------------------------- #
# Validation mode, arming, guards                                                               #
# --------------------------------------------------------------------------------------------- #
def _place(adapter, qty=0.01, symbol="BTC", cid="tw-s-000001", **kw):
    adapter.place(OrderRequest(cid, symbol, qty, kw.pop("order_type", "MKT"), **kw))
    return adapter.drain()


def test_validation_mode_is_the_default():
    adapter, venue, _ = make()
    adapter.connect()
    notes = adapter.drain()
    assert any("validation" in getattr(m, "message", "") for m in notes)
    out = _place(adapter)
    sent = venue.calls("AddOrder")
    assert len(sent) == 1 and sent[0][3]["validate"] == "true" and sent[0][3]["oflags"] == "fciq"
    assert len(out) == 1 and isinstance(out[0], Rejected)
    assert out[0].reason.startswith("validated by Kraken (validate=true), not sent: buy 0.01000000 XBTUSD")
    assert adapter.validated and not venue.orders


@pytest.mark.parametrize("live, env, armed", [
    (True, {}, False),                              # the flag alone
    (False, {LIVE_ENV: "1"}, False),                # the environment alone
    (True, {LIVE_ENV: "true"}, False),              # only "1" arms
    (True, {LIVE_ENV: "1"}, True),                  # both
])
def test_two_keys_arm_the_adapter(live, env, armed):
    adapter, venue, _ = make(live=live, env=env)
    adapter.connect()
    notes = [m.message for m in adapter.drain() if isinstance(m, Notice)]
    assert adapter.armed is armed
    if live and not armed:
        assert any(LIVE_ENV in n for n in notes)
    out = _place(adapter)
    assert ("validate" in venue.calls("AddOrder")[0][3]) is (not armed)
    assert isinstance(out[0], Ack if armed else Rejected)


def test_guards_refuse_before_any_call():
    limits = RiskLimits(allowed_symbols=frozenset({"BTC", "ETH", "DOGE", "SOL"}), max_order_notional=5_000)
    adapter, venue, _ = make({"balances": {"ZUSD": 1_000.0, "XETH": 0.5}}, live=True, env={LIVE_ENV: "1"},
                             limits=limits)
    adapter.connect()
    adapter.drain()
    cases = [
        ("XRP", 10, "Kraken has no USD pair"),
        ("ADA", 10, "no USD pair"),
        ("BTC", 0.2, "exceeds max_order_notional"),                 # 6 061 USD
        ("BTC", 0.04, "over the 1,000.00 available"),               # 1 217 USD with the fee
        ("ETH", -1.0, "over the 0.5 available"),                    # spot: no short
        ("ETH", 0.001, "under ETHUSD's ordermin"),
        ("SOL", 1.0, "SOLUSD is cancel_only"),
    ]
    for i, (sym, q, why) in enumerate(cases):
        out = _place(adapter, q, sym, cid=f"tw-g-{i}")
        assert isinstance(out[0], Rejected) and why in out[0].reason, (sym, out)
    assert not venue.calls("AddOrder")
    # a sell within the balance passes the guards
    out = _place(adapter, -0.25, "ETH", cid="tw-g-ok")
    assert isinstance(out[0], Ack) and len(venue.calls("AddOrder")) == 1


def test_kraken_refusal_is_a_rejection_and_orders_are_never_retried():
    adapter, venue, _ = make(live=True, env={LIVE_ENV: "1"})
    adapter.connect()
    adapter.drain()
    venue.reject_next = "EOrder:Insufficient funds"
    out = _place(adapter)
    assert isinstance(out[0], Rejected) and "EOrder:Insufficient funds" in out[0].reason
    venue.answers["AddOrder"] = [(200, json.dumps({"error": ["EAPI:Rate limit exceeded"]}))]
    out = _place(adapter, cid="tw-s-000002")
    assert isinstance(out[0], Rejected) and len(venue.calls("AddOrder")) == 2      # tried once each
    venue.answers["BalanceEx"] = [(200, json.dumps({"error": ["EAPI:Rate limit exceeded"]}))]
    assert adapter.balances()["ZUSD"][0] == 10_000.0                               # a read is retried
    assert len(venue.calls("BalanceEx")) >= 3


# --------------------------------------------------------------------------------------------- #
# Executions, statuses, snapshot                                                                #
# --------------------------------------------------------------------------------------------- #
def test_polls_turn_into_executions_and_statuses():
    adapter, venue, _ = make(live=True, env={LIVE_ENV: "1"})
    adapter.connect()
    adapter.drain()
    venue.fill_mode = "partial"
    out = _place(adapter, 0.0100000049, cid="tw-p-1")           # rounded to 0.01 at Kraken
    assert isinstance(out[0], Ack) and out[0].broker_id.startswith("O00001")
    first = out[1:]                                             # the first poll: half executed
    assert [type(m) for m in first] == [Execution]
    assert first[0].quantity == pytest.approx(0.005) and first[0].commission > 0
    second = adapter.drain()                                    # the rest, then the sub-lot remainder
    assert [type(m) for m in second] == [Execution, Canceled]
    assert "under the lot size" in second[1].reason
    assert adapter.drain() == []                                # nothing working: no more calls
    # a cancel the strategy asked for, and an expiry
    venue.fill_mode = "none"
    assert isinstance(_place(adapter, cid="tw-p-2")[0], Ack)
    adapter.cancel("tw-p-2")
    out = adapter.drain()
    assert len(out) == 1 and isinstance(out[0], Canceled) and not out[0].expired
    _place(adapter, cid="tw-p-3")
    venue.orders[adapter._working["tw-p-3"].txid]["status"] = "expired"
    out = adapter.drain()
    assert isinstance(out[0], Canceled) and out[0].expired


def test_snapshot_reads_the_wallet():
    adapter, venue, clock = make({"balances": {"ZUSD": 5_000.0, "XXBT": 0.2, "XETH.S": 3.0, "ZEUR": 100.0}},
                                 live=True, env={LIVE_ENV: "1"}, symbols=["BTCUSD"])
    adapter.connect()
    adapter.drain()
    snap = adapter.snapshot(full_sync=True)
    held = snap.position_map()
    assert snap.cash == 5_000.0 and held["BTCUSD"] == (0.2, 30303.2)   # the strategy's name, last price as cost
    assert held["EUR"] == (100.0, 1.08)                                # another fiat: a foreign position
    assert adapter.ignored_balances == {"XETH.S": 3.0}
    assert snap.net_liquidation == pytest.approx(5_000.0 + 0.2 * 30303.2 + 108.0)
    # our working order is listed; a manual one (no cl_ord_id of ours) is not
    venue.fill_mode = "none"
    _place(adapter, cid="tw-s-1", symbol="BTCUSD")
    venue.orders["OMANUAL"] = {"status": "open", "vol": "1", "vol_exec": "0", "cl_ord_id": "",
                               "descr": {"pair": "XBTUSD", "type": "sell"}, "trades": []}
    snap = adapter.snapshot()
    assert [(o.client_id, o.symbol, o.quantity) for o in snap.open_orders] == [("tw-s-1", "BTCUSD", 0.01)]
    # executions missed while away: TradesHistory, tied to our orders through cl_ord_id
    txid = adapter._working["tw-s-1"].txid
    tid = venue.execute(txid)
    fresh, _, _ = make({"balances": venue.balances}, live=True, env={LIVE_ENV: "1"}, symbols=["BTCUSD"])
    fresh.rest.transport = venue.transport
    fresh._client = dict(adapter._client)                       # as the state file would give it
    fresh.since = clock() - DAY_NS
    fresh.connect()
    snap = fresh.snapshot(full_sync=True)
    assert [(e.client_id, e.exec_id, e.symbol) for e in snap.executions] == [("tw-s-1", tid, "BTCUSD")]


# --------------------------------------------------------------------------------------------- #
# Bars                                                                                          #
# --------------------------------------------------------------------------------------------- #
def crypto_frame(start="2024-01-01", days=800, seed=7):
    import numpy as np

    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=days, freq="D")
    close = 40_000 * np.exp(np.cumsum(rng.normal(0.001, 0.03, days)))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                         "volume": 100.0}, index=idx)


def test_kraken_bars_never_pass_a_forming_bar():
    frame = crypto_frame()
    clock = Clock(pd.Timestamp("2026-01-10 00:03"))
    venue = Venue(clock, frames={"XBTUSD": frame})
    bars = KrakenBars(KrakenREST(transport=venue.transport, public_interval=0.0, sleep=clock.sleep),
                      grace=5.0, retry=10.0)
    after = int(pd.Timestamp("2026-01-08").value)
    assert bars.poll("BTC", after, clock()) == []                # Jan 9 complete at 00:00 + 5 min: not yet
    assert not venue.calls("OHLC")                               # nothing asked while nothing can be due
    clock.ns = int(pd.Timestamp("2026-01-10 00:06").value)
    got = bars.poll("BTC", after, clock())
    assert [pd.Timestamp(r.time, unit="ns") for r in got] == [pd.Timestamp("2026-01-09")]
    assert got[0].close == pytest.approx(frame.loc["2026-01-09", "close"])
    assert bars.poll("BTC", got[-1].time, clock()) == []         # the forming 01-10 row is never passed
    hist = bars.history("BTC", now=clock())
    assert hist.index[-1] == pd.Timestamp("2026-01-09") and len(hist) <= 720
    # the public client carries no key
    assert all("API-Key" not in r[2] for r in venue.requests)


# --------------------------------------------------------------------------------------------- #
# A LiveEngine session                                                                          #
# --------------------------------------------------------------------------------------------- #
class Alternate(tw.Strategy):
    """Half the account in BTC on one bar, nothing on the next: orders every bar."""

    def __init__(self):
        self.sma = tw.ind.SMA(self.data.close, period=20)
        self.events = []

    def next(self):
        self.order_target_percent(target=0.5 if len(self) % 2 else 0.0)

    def notify_order(self, order):
        if order.status in (order.Completed, order.Rejected, order.Partial, order.Canceled):
            self.events.append((order.getstatusname(), order.info.get("tw_reason", ""), order.executed.size))


def _engine(adapter, venue, clock, journal, frame):
    bars = KrakenBars(KrakenREST(transport=venue.transport, public_interval=0.0, sleep=clock.sleep))
    engine = LiveEngine(adapter, bars, journal=journal, fsync=False, poll_interval=3600,
                        limits=RiskLimits(max_order_notional=20_000, allowed_symbols=frozenset({"BTC"}),
                                          allow_short=False))
    engine.broker.addcommissioninfo(FractionalCommInfo())
    engine.adddata(LiveFeed(dataname=frame[frame.index < pd.Timestamp(clock(), unit="ns").normalize()
                                           - pd.Timedelta(days=1)]), name="BTC")
    engine.addstrategy(Alternate)
    return engine


@pytest.mark.parametrize("armed", [False, True])
def test_live_engine_session(tmp_path, armed, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    frame = crypto_frame()
    env = {LIVE_ENV: "1"} if armed else {}
    adapter, venue, clock = make({"frames": {"XBTUSD": frame}, "balances": {"ZUSD": 20_000.0}}, t="2026-01-10 00:30",
                                 live=True, env=env, state_path=str(tmp_path / "kraken_state.json"))
    journal = str(tmp_path / "session.jsonl")
    engine = _engine(adapter, venue, clock, journal, frame)
    strat = engine.run(max_slices=6)[0]
    assert engine.live_slices == 6
    adds = venue.calls("AddOrder")
    assert adds, "the strategy placed orders"
    if not armed:
        assert all(r[3]["validate"] == "true" for r in adds) and not venue.orders and not venue.trades
        assert strat.events and all(e[0] == "Rejected" and e[1].startswith("validated by Kraken") for e in strat.events)
    else:
        assert all("validate" not in r[3] for r in adds) and venue.trades
        assert any(e[0] == "Completed" for e in strat.events)
        held = engine.broker.getposition(engine.datas[0]).size
        assert held == pytest.approx(venue.balances.get("XXBT", 0.0), abs=1e-8)
    # the secret is nowhere: journal, state file, logs, printed output, the requests' bodies
    def read(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    outputs = [read(journal), caplog.text, "".join(capsys.readouterr())]
    if os.path.exists(tmp_path / "kraken_state.json"):
        outputs.append(read(tmp_path / "kraken_state.json"))
    outputs += [json.dumps(r[3]) for r in venue.requests]
    for text in outputs:
        assert SENTINEL_SECRET not in text and SENTINEL_KEY not in text
    # only the request headers carry the key, never the secret
    assert all(SENTINEL_SECRET not in json.dumps(r[2]) for r in venue.requests)


def test_the_key_never_shows_in_errors(tmp_path):
    adapter, venue, _ = make()
    venue.answers["BalanceEx"] = [(200, json.dumps({"error": [f"EAPI:Invalid key {SENTINEL_KEY}"]}))]
    with pytest.raises(KrakenError) as info:
        adapter.connect()
    assert SENTINEL_KEY not in str(info.value) and "***" in str(info.value)

    def broken(*a, **k):
        raise OSError(f"connection reset while sending {SENTINEL_SECRET}")

    rest = KrakenREST(Credentials(SENTINEL_KEY, SENTINEL_SECRET), transport=broken, retries=0)
    with pytest.raises(KrakenError) as info:
        rest.private("BalanceEx")
    assert SENTINEL_SECRET not in str(info.value)


# --------------------------------------------------------------------------------------------- #
# The daily runner                                                                              #
# --------------------------------------------------------------------------------------------- #
def test_daily_account(tmp_path):
    from tradewind.live.daily import KrakenAccount
    from tradewind.live.daily.aggregate import ProposedOrder

    clock = Clock(pd.Timestamp("2026-11-20 03:30"))       # Thursday's session, after the US close
    venue = Venue(clock, balances={"ZEUR": 9_000.0, "XXBT": 0.1, "XETH": 2.0})
    rest = KrakenREST(Credentials(SENTINEL_KEY, SENTINEL_SECRET), transport=venue.transport, now=clock.mono,
                      sleep=clock.sleep, nonce=NonceSource(clock), public_interval=0.0)

    def account(send, env=None, live=False, **kw):
        ad = KrakenAdapter(rest, quote="EUR", live=live and send, environ=env or {}, clock_ns=clock,
                           monotonic=clock.mono, sleep=clock.sleep, limits=RiskLimits(allow_short=False))
        return KrakenAccount(quote="EUR", send=send, live=live, adapter=ad, **kw)

    acct = account(send=False)
    assert acct.currency_info() == {"currency": "EUR", "published": None, "fallback": None}
    acct.set_fx(1.08)                                       # USD per EUR
    closes = {"BTC": 90_000.0, "ETH": 3_000.0}
    snap = acct.snapshot(closes)
    assert snap.positions == {"BTC": 0.1, "ETH": 2.0} and snap.base_currency == "EUR"
    assert snap.cash == pytest.approx(9_000.0 * 1.08) and snap.base_cash == 9_000.0
    assert snap.nav == pytest.approx(9_000 * 1.08 + 9_000 + 6_000)
    orders = [ProposedOrder("BTC", 0.05, 90_000.0, 0.1, 0.15, 0.15),
              ProposedOrder("ETH", -1.5, 3_000.0, 2.0, 0.5, 0.5),
              ProposedOrder("DOGE", 10.0, 0.1, 0.0, 10, 10),
              ProposedOrder("ETH", -5.0, 3_000.0, 0.5, -4.5, -4.5)]
    with pytest.raises(AdapterError, match="read-only"):
        acct.place(orders, "2026-11-19")
    assert not venue.calls("AddOrder")

    acct = account(send=True, allowed=["BTC", "ETH", "DOGE"], max_order_notional=20_000.0)
    acct.set_fx(1.08)
    sent = acct.place(orders, "2026-11-19")
    st = {(s["symbol"], s["quantity"]): s for s in sent}
    assert [s["id"] for s in sent] == [f"twk-20261119-{i:04d}" for i in range(1, 5)]
    assert st[("BTC", 0.05)]["status"] == "validated" and st[("ETH", -1.5)]["status"] == "validated"
    assert st[("DOGE", 10.0)]["status"] == "refused" and "no EUR pair" in st[("DOGE", 10.0)]["reason"]
    assert st[("ETH", -5.0)]["status"] == "refused" and "short" in st[("ETH", -5.0)]["reason"]
    adds = venue.calls("AddOrder")
    assert [a[3]["type"] for a in adds] == ["sell", "buy"]  # sells first
    assert all(a[3]["validate"] == "true" for a in adds) and adds[1][3]["pair"] == "XBTEUR"
    assert acct.estimated_commission(orders[0]) == pytest.approx(0.05 * 90_000 * 0.004)

    # armed: the same orders trade; the next session settles them in USD
    acct = account(send=True, env={LIVE_ENV: "1"}, live=True)
    acct.set_fx(1.08)
    sent = acct.place(orders[:2], "2026-11-19")
    assert [s["status"] for s in sent] == ["sent", "sent"]
    for txid in list(venue.orders):
        venue.execute(txid)
    clock.ns = int(pd.Timestamp("2026-11-21 03:30").value)
    fills = acct.settle("2026-11-20", {"BTC": 91_000.0}, closes)
    assert sorted((f.symbol, round(f.quantity, 8)) for f in fills) == [("BTC", 0.05), ("ETH", -1.5)]
    assert all(f.costs > 0 for f in fills)


def test_daily_account_from_the_command_line_needs_the_environment(monkeypatch, tmp_path):
    from tradewind.live.daily.__main__ import make_account, parser

    monkeypatch.delenv("KRAKEN_API_KEY", raising=False)
    monkeypatch.delenv("KRAKEN_API_SECRET", raising=False)
    args = parser().parse_args(["--meta", "x.yaml", "--broker", "kraken", "--kraken-quote", "EUR"])
    with pytest.raises(AdapterError, match="KRAKEN_API_KEY and KRAKEN_API_SECRET"):
        make_account(args, str(tmp_path), send=False)


def test_fresh_crypto_closes_from_kraken(tmp_path):
    from tradewind.live.daily.fresh import FreshPrices
    from tradewind.live.daily.kraken_prices import KrakenDailyPrices, KrakenPriceCache, kraken_kind

    frame = crypto_frame()
    session = "2026-01-09"
    clock = Clock(pd.Timestamp("2026-01-10 03:30"))
    venue = Venue(clock, frames={"XBTUSD": frame, "ETHUSD": frame * 0.05})
    rest = KrakenREST(transport=venue.transport, public_interval=0.0, sleep=clock.sleep)
    collector = KrakenDailyPrices(str(tmp_path), session, rest=rest, log=lambda *a: None,
                                  clock=lambda: clock() / 1e9)
    stats = collector.collect({"BTC": "crypto", "ETH": None, "EURUSD": None, "AAPL": "equity", "NOPE": "crypto"})
    assert (stats["served"], stats["asked"], stats["unknown"]) == (2, 2, 1)
    assert not kraken_kind("EURUSD") and not kraken_kind("AAPL", "equity")
    cache = KrakenPriceCache(str(tmp_path), session)
    bars, why = cache.bars("crypto", "BTC")
    assert why == "" and bars["date"].iloc[-1] == session
    assert bars["close"].iloc[-1] == pytest.approx(frame.loc[session, "close"])
    # fetched before the bar was final: the session's bar is dropped and the symbol falls back
    early = KrakenDailyPrices(str(tmp_path / "early"), session, rest=rest, log=lambda *a: None,
                              clock=lambda: pd.Timestamp("2026-01-09 23:00").timestamp())
    early.collect({"BTC": "crypto"})
    assert KrakenPriceCache(str(tmp_path / "early"), session).bars("crypto", "BTC")[0] is None
    # the worker side: Kraken serves the crypto loads
    fresh = FreshPrices.from_env(str(tmp_path), session, log=lambda *a: None, sources=("kraken",))
    rows = frame[frame.index <= "2026-01-05"].reset_index(names="date")
    rows["date"] = rows["date"].dt.strftime("%Y-%m-%d")
    rows.insert(0, "symbol", "BTC")
    out = fresh.extend("paperswithbacktest/Cryptocurrencies-Daily-Price", rows)
    assert out["date"].max() == session and fresh.served == {"BTC": "kraken"}
    assert fresh.summary()["served"]["kraken"] == ["BTC"]


def test_provider_collects_kraken_once(tmp_path):
    from tradewind.live.daily import MetaPortfolio, PwbProvider, Sleeve

    asked = []

    class Collector:
        def __init__(self, session, cache):
            self.session = session

        def collect(self, wanted):
            asked.append(self.session)
            return {"served": 1, "asked": 1}

    meta = MetaPortfolio(sleeves=[Sleeve("x", 1.0)])
    p = PwbProvider(meta, str(tmp_path / "books"), fresh_prices="kraken", kraken_factory=Collector)
    assert p.collect_kraken("2026-01-09")["served"] == 1
    p.collect_kraken("2026-01-09")
    assert asked == ["2026-01-09"]
    assert p.price_report()["kraken"] == {"served": 1, "asked": 1}


# --------------------------------------------------------------------------------------------- #
# Kraken's public endpoints, for real (skipped by default)                                       #
# --------------------------------------------------------------------------------------------- #
@pytest.mark.network
@pytest.mark.skipif(os.environ.get("TRADEWIND_NETWORK_TESTS") != "1",
                    reason="calls Kraken's public API: set TRADEWIND_NETWORK_TESTS=1")
def test_public_endpoints_for_real():
    rest = KrakenREST()                                     # no credentials: public calls only
    table = PairTable.from_result(rest.public("AssetPairs", {"pair": "XBTUSD,ETHUSD,XBTEUR"}))
    btc = table.resolve("BTC")
    assert btc.altname == "XBTUSD" and btc.lot_decimals > 0 and btc.ordermin > 0
    bars = KrakenBars(rest, pairs=table)
    hist = bars.history("BTC")
    assert len(hist) > 300 and hist.index[-1] >= pd.Timestamp.now(tz=None).normalize() - pd.Timedelta(days=2)
    assert (hist.index.hour == 0).all()
    last = rest.public("Ticker", {"pair": "XBTUSD"})
    assert float(next(iter(last.values()))["c"][0]) > 0
    with pytest.raises(AdapterError):
        rest.private("BalanceEx")                         # never signed without a key


def test_example_public_mode_and_validation_session(tmp_path, monkeypatch, capsys):
    """``docs/examples/live_kraken.py``: ``--public`` on recorded bars; the account mode, in
    validation, against the in-memory venue."""
    import runpy

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ex = runpy.run_path(os.path.join(root, "docs", "examples", "live_kraken.py"), run_name="example")
    frame = crypto_frame()
    rise = 30_000 * 1.002 ** pd.Series(range(len(frame)), index=frame.index, dtype=float)
    for col in ("open", "high", "low", "close"):
        frame[col] = rise                                  # a steady rise: above its average
    clock = Clock(pd.Timestamp("2026-01-10 00:30"))
    venue = Venue(clock, frames={"XBTUSD": frame, "ETHUSD": frame * 0.05}, balances={"ZUSD": 2_000.0})
    public = KrakenREST(transport=venue.transport, public_interval=0.0, sleep=clock.sleep)
    args = type("A", (), {"quote": "USD"})()
    last = ex["run_public"](args, rest=public)
    assert last == pd.Timestamp("2026-01-09")
    assert "BTC: 719 complete daily bars to 2026-01-09 (00:00 UTC), close 131,331.07 USD, 50-day average 125,109.07: target 45%" in capsys.readouterr().out
    # the account mode builds its adapter from the environment: give it ours
    monkeypatch.setenv("KRAKEN_API_KEY", SENTINEL_KEY)
    monkeypatch.setenv("KRAKEN_API_SECRET", SENTINEL_SECRET)
    monkeypatch.delenv(LIVE_ENV, raising=False)
    real_init = KrakenAdapter.__init__

    def init(self, rest=None, **kw):
        rest = KrakenREST(Credentials.from_env(), transport=venue.transport, now=clock.mono, sleep=clock.sleep,
                          nonce=NonceSource(clock), public_interval=0.0)
        real_init(self, rest, clock_ns=clock, monotonic=clock.mono, sleep=clock.sleep, poll_every=0.0, **kw)

    monkeypatch.setattr(KrakenAdapter, "__init__", init)
    monkeypatch.chdir(tmp_path)
    ex["main"](["--live", "--slices", "3"])               # --live without the variable: still validation
    out = capsys.readouterr().out
    assert "mode validation (validate=true)" in out and "3 live slices" in out
    assert venue.calls("AddOrder") and all(r[3]["validate"] == "true" for r in venue.calls("AddOrder"))
    assert not venue.orders
    with open(tmp_path / "live" / "kraken.jsonl", encoding="utf-8") as fh:
        text = fh.read()
    assert SENTINEL_KEY not in text and SENTINEL_SECRET not in text


def test_fractional_scheme_sizes_in_satoshis():
    c = FractionalCommInfo()
    assert c.getsize(91_234.5, 10_000) == 0.10960765            # toward zero, 8 decimals
    assert c.getcommission(0.1, 90_000) == pytest.approx(36.0)  # 0.40 % of the value
    assert FractionalCommInfo(decimals=4, commission=0.0026).getsize(3_000.0, 1_000) == 0.3333


def test_session_alerts_and_reconciliation_for_kraken_statuses(tmp_path):
    """A validated order traded nothing: no fills_mismatch the next day; a refused one (a
    local guard) raises orders_rejected; a skipped one (dust) raises nothing."""
    from tradewind.live.daily.session import AlertConfig, DailySession, check_day

    cfg = AlertConfig(max_gap=1.0, max_gap_day=1.0, max_loss=1.0)
    sent = [{"symbol": "BTC", "quantity": 0.05, "status": "validated"},
            {"symbol": "ETH", "quantity": 0.0001, "status": "skipped", "reason": "volume 0.0001 under ETHUSD's ordermin"},
            {"symbol": "DOGE", "quantity": 10.0, "status": "refused", "reason": "Kraken has no EUR pair for 'DOGE'"}]
    alerts = check_day({"nav_real": 10_000.0}, sent, cfg)
    assert [a.code for a in alerts] == ["orders_rejected"] and "DOGE" in alerts[0].message
    d = tmp_path / "day"
    session = DailySession(None, str(d), provider_factory=None, account_factory=None, log=lambda *a: None)
    with open(d / "state.json", "w") as fh:
        json.dump({"days": {"2026-11-19": {"held": {"BTC": 0.1}, "sent": True},
                            "2026-11-20": {"held": {"BTC": 0.1}}}}, fh)
    session._mark("2026-11-19", phase="sent", sent=sent)
    assert session._reconcile("2026-11-20", {"working": []})["mismatches"] == []


def test_the_kraken_schedule_runs_after_the_crypto_bar_and_on_the_nyse_session():
    """``docs/examples/schedule/tradewind-daily-kraken.cron``: 00:15 UTC, Tuesday to Saturday,
    is after the day's crypto bar is final and works on the NYSE session of the evening
    before, every weekday of a year (daylight saving both sides)."""
    import datetime as _dt

    from tradewind.calendars import NYSE
    from tradewind.live.daily.kraken_prices import crypto_bar_complete_at
    from tradewind.live.daily.session import closed_session

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "docs", "examples", "schedule", "tradewind-daily-kraken.cron")) as fh:
        line = next(x for x in fh if x.strip() and not x.startswith("#"))
    minute, hour, _dom, _mon, dow = line.split()[:5]
    assert (int(minute), int(hour), dow) == (15, 0, "2-6") and "BROKER=kraken FRESH_PRICES=kraken" in line
    for k in range(365):
        d = _dt.date(2026, 1, 1) + _dt.timedelta(days=k)
        if not NYSE.is_trading_day(d):
            continue
        run = _dt.datetime.combine(d + _dt.timedelta(days=1), _dt.time(0, 15), tzinfo=_dt.timezone.utc)
        assert run.isoweekday() in range(2, 7)
        assert closed_session(run) == d.isoformat()
        assert run.timestamp() >= crypto_bar_complete_at(d.isoformat())
