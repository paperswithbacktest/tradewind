"""The daily run from the command line.

    # every evening after the US close: propose (default), read orders/DATE.md, then send
    python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --propose
    python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --send

    # the same on a simulated account, and a replay of the last 20 closes on one
    python -m tradewind.live.daily --meta meta.yaml --dir /tmp/sim --broker sim --send
    python -m tradewind.live.daily --meta meta.yaml --dir /tmp/replay --broker sim --replay 20

Nothing is sent without ``--send``. ``--broker ibkr`` attaches to a TWS / IB Gateway session
already logged in to a **paper** account (port 7497 or 4002; anything else is refused).
"""

from __future__ import annotations

import argparse
import os
import sys

from .brokers import IBPaperAccount, SimAccount
from .meta import REBALANCE, MetaPortfolio
from .providers import PwbProvider, ReplayProvider
from .runner import DailyRunner, StaleData


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tradewind.live.daily", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--meta", required=True, help="the meta-portfolio (YAML, JSON or CSV)")
    p.add_argument("--dir", default="paper", help="state, proposals and journal (default ./paper)")
    p.add_argument("--broker", choices=["sim", "ibkr"], default="sim")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--propose", action="store_true", help="write the proposal only (the default)")
    mode.add_argument("--send", action="store_true", help="place the orders (market-on-open)")
    mode.add_argument("--replay", type=int, metavar="N",
                      help="simulated account only: run the last N closes, orders sent on the simulation")
    p.add_argument("--as-of", default=None, help="the close to trade from (default: the data's last close)")
    p.add_argument("--allow-stale", action="store_true", help="trade a sleeve whose last close is older")
    g = p.add_argument_group("meta-portfolio overrides")
    g.add_argument("--capital", type=float)
    g.add_argument("--leverage", type=float)
    g.add_argument("--max-gross", type=float)
    g.add_argument("--rebalance", choices=REBALANCE)
    g.add_argument("--lot", type=float)
    g.add_argument("--min-order", type=float, dest="min_order_notional")
    g.add_argument("--python", help="the Python that has pwb-toolbox")
    g.add_argument("--papers-dir")
    i = p.add_argument_group("IBKR paper account")
    i.add_argument("--host", default="127.0.0.1")
    i.add_argument("--port", type=int, default=7497, help="7497 TWS paper (default), 4002 IB Gateway paper")
    i.add_argument("--client-id", type=int, default=31)
    i.add_argument("--account", default="", help="DU... when the login manages several accounts")
    s = p.add_argument_group("simulated account")
    s.add_argument("--sim-cash", type=float, default=110_000.0)
    s.add_argument("--sim-costs", choices=["harness", "none"], default="harness",
                   help="harness: the half-spread the strategies' backtests charge (default)")
    s.add_argument("--sim-commission", choices=["ibkr_tiered", "none"], default="none")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    overrides = {k: getattr(args, k) for k in ("capital", "leverage", "max_gross", "rebalance", "lot",
                                                "min_order_notional", "python", "papers_dir")}
    meta = MetaPortfolio.load(args.meta, **overrides)
    directory = os.path.expanduser(args.dir)
    os.makedirs(directory, exist_ok=True)
    provider = PwbProvider(meta, os.path.join(directory, "books"))
    if args.broker == "ibkr":
        if args.replay:
            print("--replay runs on the simulated account only", file=sys.stderr)
            return 2
        account = IBPaperAccount(args.host, args.port, args.client_id, account=args.account, send=args.send)
    else:
        account = SimAccount(os.path.join(directory, "sim_account.json"), cash=args.sim_cash,
                             use_book_spreads=args.sim_costs == "harness", commission=args.sim_commission)
    try:
        if args.replay:
            replay = ReplayProvider(provider, args.as_of, args.replay + 1)
            runner = DailyRunner(meta, replay, account, directory, allow_stale=args.allow_stale)
            dates = replay.dates()[-args.replay:]
            reports = runner.replay(dates, send=True)
            last = reports[-1].row if reports else {}
            print(f"replayed {len(reports)} closes {dates[0] if dates else ''}..{dates[-1] if dates else ''}: "
                  f"NAV {last.get('nav_real', float('nan')):,.2f} against backtest "
                  f"{last.get('nav_expected', float('nan')):,.2f}; journal {runner.journal.csv}")
            return 0
        runner = DailyRunner(meta, provider, account, directory, allow_stale=args.allow_stale)
        report = runner.run_day(args.as_of, send=args.send)
    except StaleData as exc:
        print(f"stale data: {exc}", file=sys.stderr)
        return 3
    finally:
        account.close()
    r = report.row
    print(f"{report.date}: {len(report.plan.orders)} order(s) {'SENT' if args.send else 'proposed'}; "
          f"NAV {r['nav_real']:,.2f}, backtest {r['nav_expected']:,.2f}, gap {r['gap_cum']:+,.2f}")
    for w in report.warnings:
        print("warning:", w)
    print("proposal:", report.proposal_md)
    if args.send:
        bad = [s for s in report.sent if s.get("status") in ("rejected", "cancelled")]
        for s in bad:
            print(f"  {s['symbol']} {s['quantity']:+g}: {s['status']} {s.get('reason', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
