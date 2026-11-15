"""Paper trading on Interactive Brokers: a catalogue-style monthly rebalance, run live.

The strategy is an ordinary Tradewind strategy -- the same class backtests unchanged: each
month it holds, in equal weights, the two of four ETFs with the best six-month return.

    python live_ibkr_paper.py --fake     a simulated venue (FakeIB), no account, no network:
                                         three years of warm-up, one year live, in a second
    python live_ibkr_paper.py            TWS or IB Gateway (paper) on 127.0.0.1:7497,
                                         READ-ONLY: orders are computed and journalled,
                                         nothing is sent
    python live_ibkr_paper.py --trade    the same, sending paper orders

Before the first IB run (spec/07-live-ibkr.md § 11): ``pip install 'tradewind[ibkr]'``; log
into TWS or IB Gateway with the **paper** account; in Global Configuration > API > Settings,
tick "Enable ActiveX and Socket Clients", set the socket port (TWS paper 7497, Gateway paper
4002), keep "Allow connections from localhost only", and untick "Read-Only API" only when
you want ``--trade``. The adapter refuses live ports and non-paper accounts.

The session runs until Ctrl-C or until the kill file appears (``touch live/KILL``: open
orders are cancelled, nothing more is sent). Start it again with the same ``--journal``: it
replays the journal, reconciles with the account and carries on.
"""

from __future__ import annotations

import argparse
import os

import tradewind as tw

SYMBOLS = ["SPY", "TLT", "GLD", "EFA"]


class TopTwoMomentum(tw.Strategy):
    params = (("lookback", 126), ("hold", 2), ("weight", 0.48))

    def __init__(self):
        self.roc = {d: tw.ind.ROC(d.close, period=self.p.lookback) for d in self.datas}
        self.month = None

    def next(self):
        month = self.datetime.date(0).month
        if month == self.month:
            return
        self.month = month
        ranked = sorted(self.datas, key=lambda d: self.roc[d][0], reverse=True)
        chosen = set(ranked[: self.p.hold])
        for data in self.datas:                       # sells first: they fund the buys
            if data not in chosen:
                self.order_target_percent(data, target=0.0)
        for data in ranked[: self.p.hold]:
            self.order_target_percent(data, target=self.p.weight)

    def notify_order(self, order):
        if order.status in (order.Completed, order.Canceled, order.Expired, order.Margin, order.Rejected):
            why = order.info.get("tw_reason", "")
            print(f"{self.datetime.date(0)} {order.data._name:4s} {order.getstatusname():9s} "
                  f"size {order.executed.size:+g} at {order.executed.price:.2f} {why}")


def run_fake(args):
    import pandas as pd
    from _data import bars

    from tradewind.live import FakeIB, FakeMarket, LiveEngine, LiveFeed, RiskLimits

    frames = {s: bars(s, seed=40 + i, years=4) for i, s in enumerate(SYMBOLS)}
    start = frames["SPY"].index[756] + pd.Timedelta(hours=21)     # after three years of bars
    market = FakeMarket(frames, start)
    venue = FakeIB(market, cash=100_000, commission=0.005, min_commission=1.0)
    engine = LiveEngine(venue, journal=args.journal, poll_interval=6 * 3600, fsync=False,
                        limits=RiskLimits(max_order_notional=60_000, allowed_symbols=frozenset(SYMBOLS)))
    for s in SYMBOLS:
        engine.adddata(LiveFeed(dataname=market.history(s)), name=s)
    engine.addstrategy(TopTwoMomentum)
    engine.run()
    print(f"{engine.live_slices} live slices ({engine.stopped_by}), "
          f"final value {engine.broker.getvalue():,.2f}, account cash {venue.cash:,.2f}")


def run_ib(args):
    from tradewind.live import IBAdapter, IBBars, KillSwitch, LiveEngine, LiveFeed, RiskLimits

    adapter = IBAdapter(args.host, args.port, client_id=args.client_id, readonly=not args.trade)
    adapter.connect()                                  # refuses a live port or a non-paper account
    source = IBBars(adapter)
    engine = LiveEngine(adapter, source, journal=args.journal, read_only=not args.trade,
                        capital=args.capital, kill_switch=KillSwitch(args.kill_file),
                        limits=RiskLimits(max_order_notional=0.6 * args.capital,
                                          allowed_symbols=frozenset(SYMBOLS), allow_short=False))
    for s in SYMBOLS:
        engine.adddata(LiveFeed(dataname=source.history(s, "2 Y")), name=s)
    engine.addstrategy(TopTwoMomentum)
    try:
        engine.run(connect=False)
    finally:
        adapter.disconnect()
    print(f"{engine.live_slices} live slices ({engine.stopped_by}); final value "
          f"{engine.broker.getvalue():,.2f}; journal {args.journal}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="simulated venue, no account")
    parser.add_argument("--trade", action="store_true", help="send paper orders (default: read-only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7497, help="TWS paper 7497, IB Gateway paper 4002")
    parser.add_argument("--client-id", type=int, default=17)
    parser.add_argument("--capital", type=float, default=100_000.0, help="the sleeve the strategy trades")
    parser.add_argument("--journal", default=None, help="default: live/<mode>.jsonl")
    parser.add_argument("--kill-file", default=os.path.join("live", "KILL"))
    args = parser.parse_args(argv)
    if args.fake:
        run_fake(args)
    else:
        args.journal = args.journal or os.path.join("live", "ibkr-paper.jsonl")
        run_ib(args)


if __name__ == "__main__":
    main()
