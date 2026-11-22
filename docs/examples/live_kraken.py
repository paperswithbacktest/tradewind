"""Crypto on Kraken (spot): a daily trend rule on BTC and ETH, run live in validation mode.

The strategy is an ordinary Tradewind strategy -- the same class backtests unchanged: each
day it holds, in equal weights, the coins whose close is above their 50-day average.

    python live_kraken.py --public     Kraken's public daily bars only, no key, no order:
                                       prints the last complete bars and today's targets
    python live_kraken.py              the account (key in KRAKEN_API_KEY / KRAKEN_API_SECRET),
                                       VALIDATION MODE: every order goes to AddOrder with
                                       validate=true -- Kraken checks it, nothing trades
    python live_kraken.py --live       orders trade, but only if TRADEWIND_KRAKEN_LIVE=1 is
                                       also set in the environment (two keys); otherwise
                                       still validation mode

Before the first account run (spec/11-live-kraken.md § 7): create an API key in Kraken
(Settings > API) with *Query Funds*, *Query Open Orders & Trades*, *Query Closed Orders &
Trades*, *Create & Modify Orders* and *Cancel/Close Orders* -- **never** *Withdraw Funds* --
and export it in the shell that runs the session (never in a file of this repository)::

    export KRAKEN_API_KEY=...        # the public key
    export KRAKEN_API_SECRET=...     # the private key, as Kraken shows it (base64)

The session decides once a day, after the 00:00 UTC bar is complete, and runs until Ctrl-C
or the kill file (``touch live/KILL``). Start it again with the same ``--journal``: it
replays the journal, reconciles with the account and carries on.
"""

from __future__ import annotations

import argparse
import os

import tradewind as tw

SYMBOLS = ["BTC", "ETH"]
PERIOD, WEIGHT = 50, 0.45


class Fractional(tw.CommInfoBase):
    """Crypto sizes are fractional: ``order_target_percent`` sizes to 1e-8 of a coin (the
    adapter then cuts to each pair's ``lot_decimals``). 0.40 % per trade, Kraken's entry
    taker fee."""

    params = (("stocklike", True), ("commtype", tw.CommInfoBase.COMM_PERC), ("commission", 0.004))

    def getsize(self, price, cash):
        return int(cash / price * 1e8) / 1e8


class TrendCrypto(tw.Strategy):
    params = (("period", PERIOD), ("weight", WEIGHT))

    def __init__(self):
        self.sma = {d: tw.ind.SMA(d.close, period=self.p.period) for d in self.datas}

    def targets(self) -> dict[str, float]:
        return {d._name: (self.p.weight if d.close[0] > self.sma[d][0] else 0.0) for d in self.datas}

    def next(self):
        for name, w in sorted(self.targets().items(), key=lambda kv: kv[1]):   # sells first
            self.order_target_percent(self.getdatabyname(name), target=w)

    def notify_order(self, order):
        if order.status in (order.Completed, order.Canceled, order.Expired, order.Margin, order.Rejected):
            why = order.info.get("tw_reason", "")
            print(f"{self.datetime.date(0)} {order.data._name:4s} {order.getstatusname():9s} "
                  f"size {order.executed.size:+.8f} at {order.executed.price:,.2f} {why}")


def run_public(args, rest=None):
    """Kraken's public bars only: no key, no account, no order."""
    import pandas as pd

    from tradewind.live.kraken import KrakenBars, KrakenREST

    bars = KrakenBars(rest or KrakenREST(), quote=args.quote)
    for s in SYMBOLS:
        hist = bars.history(s)
        sma = hist["close"].rolling(PERIOD).mean()
        last = hist.index[-1]
        target = WEIGHT if hist["close"].iloc[-1] > sma.iloc[-1] else 0.0
        print(f"{s}: {len(hist)} complete daily bars to {last:%Y-%m-%d} (00:00 UTC), close "
              f"{hist['close'].iloc[-1]:,.2f} {args.quote}, 50-day average {sma.iloc[-1]:,.2f}: target {target:.0%}")
    return pd.Timestamp(last)


def run_account(args):
    from tradewind.live import KillSwitch, LiveEngine, LiveFeed, RiskLimits
    from tradewind.live.kraken import KrakenAdapter

    os.makedirs(os.path.dirname(args.journal) or ".", exist_ok=True)
    adapter = KrakenAdapter(quote=args.quote, live=args.live, symbols=SYMBOLS,
                            state_path=os.path.join(os.path.dirname(args.journal) or ".", "kraken_state.json"),
                            limits=RiskLimits(max_order_notional=0.5 * args.capital,
                                              allowed_symbols=frozenset(SYMBOLS), allow_short=False))
    adapter.connect()                          # reads the key from the environment, checks it
    print(f"Kraken {args.quote} spot, mode {adapter.mode}")
    source = adapter.bar_source()
    engine = LiveEngine(adapter, source, journal=args.journal, capital=args.capital, poll_interval=300,
                        kill_switch=KillSwitch(args.kill_file), reconcile="adopt",
                        limits=RiskLimits(max_order_notional=0.5 * args.capital,
                                          allowed_symbols=frozenset(SYMBOLS), allow_short=False))
    engine.broker.addcommissioninfo(Fractional())
    for s in SYMBOLS:
        engine.adddata(LiveFeed(dataname=source.history(s)), name=s)
    engine.addstrategy(TrendCrypto)
    try:
        engine.run(connect=False, max_slices=args.slices)
    finally:
        adapter.disconnect()
    print(f"{engine.live_slices} live slices ({engine.stopped_by}); value {engine.broker.getvalue():,.2f} "
          f"{args.quote}; journal {args.journal}; orders validated, not sent: {len(adapter.validated)}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--public", action="store_true", help="public bars only: no key, no order")
    parser.add_argument("--live", action="store_true",
                        help="trade (needs TRADEWIND_KRAKEN_LIVE=1 too; otherwise validation mode)")
    parser.add_argument("--quote", choices=["USD", "EUR"], default="USD")
    parser.add_argument("--capital", type=float, default=1_000.0, help="the sleeve the strategy trades")
    parser.add_argument("--slices", type=int, default=None, help="stop after this many daily slices")
    parser.add_argument("--journal", default=os.path.join("live", "kraken.jsonl"))
    parser.add_argument("--kill-file", default=os.path.join("live", "KILL"))
    args = parser.parse_args(argv)
    if args.public:
        run_public(args)
    else:
        run_account(args)


if __name__ == "__main__":
    main()
