"""The daily run from the command line.

    # every evening after the US close: propose (default), read orders/DATE.md, then send
    python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --propose
    python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --send

    # the same on a simulated account, and a replay of the last 20 closes on one
    python -m tradewind.live.daily --meta meta.yaml --dir /tmp/sim --broker sim --send
    python -m tradewind.live.daily --meta meta.yaml --dir /tmp/replay --broker sim --replay 20

    # the PWB price datasets are monthly: complete them up to the last close (AlphaVantage,
    # key in ALPHAVANTAGE_API_KEY)
    python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --propose \
        --fresh-prices alphavantage

    # the whole day in one command, safe to repeat, for a scheduler (report, alerts, exit code)
    python -m tradewind.live.daily.session --meta meta.yaml --dir ~/pwb-paper --broker sim --send

Nothing is sent without ``--send``. ``--broker ibkr`` attaches to a TWS / IB Gateway session
already logged in to a **paper** account (port 7497 or 4002; anything else is refused).
"""

from __future__ import annotations

import argparse
import os
import sys

from .borrow import SOURCES as BORROW_SOURCES
from .borrow import borrow_source
from .brokers import IBPaperAccount, SimAccount
from .meta import REBALANCE, MetaPortfolio
from .providers import PwbProvider, ReplayProvider
from .runner import AccountCurrencyError, DailyRunner, StaleData


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
    p.add_argument("--fresh-prices", choices=["alphavantage", "none"], default=None,
                   help="complete the monthly price datasets up to the last close (key in ALPHAVANTAGE_API_KEY)")
    g = p.add_argument_group("meta-portfolio overrides")
    g.add_argument("--capital", type=float)
    g.add_argument("--capital-currency", help="the currency of --capital / capital: USD, or the account's (default)")
    g.add_argument("--account-currency",
                   help="check: the account's currency must be this one, or nothing is sized (default: read off "
                        "the account; a simulated account is in USD unless --sim-currency)")
    g.add_argument("--leverage", type=float)
    g.add_argument("--max-gross", type=float)
    g.add_argument("--rebalance", choices=REBALANCE)
    g.add_argument("--borrow-source", choices=BORROW_SOURCES,
                   help="the shorts' borrow fee and availability: auto (the PWB copy of IB's short-stock file, "
                        "then IB's shortable tick), hub, ib, none (default: auto at IBKR, none on the simulation)")
    g.add_argument("--lot", type=float)
    g.add_argument("--min-order", type=float, dest="min_order_notional")
    g.add_argument("--python", help="the Python that has pwb-toolbox")
    g.add_argument("--papers-dir")
    i = p.add_argument_group("IBKR paper account")
    i.add_argument("--host", default="127.0.0.1")
    i.add_argument("--port", type=int, default=7497, help="7497 TWS paper (default), 4002 IB Gateway paper")
    i.add_argument("--client-id", type=int, default=31)
    i.add_argument("--account", default="", help="DU... when the login manages several accounts")
    i.add_argument("--base-fx-rate", type=float, default=None,
                   help="an account whose currency is not USD: the price of 1 unit of it in USD (EURUSD), "
                        "the last resort after IB's own rate and PWB's forex series")
    s = p.add_argument_group("simulated account")
    s.add_argument("--sim-cash", type=float, default=110_000.0, help="in the account's currency")
    s.add_argument("--sim-currency", default=None,
                   help="the simulated account's currency (default USD, or --account-currency)")
    s.add_argument("--sim-costs", choices=["harness", "none"], default="harness",
                   help="harness: the half-spread the strategies' backtests charge (default)")
    s.add_argument("--sim-commission", choices=["ibkr_tiered", "none"], default="none")
    return p


#: the command-line options that override the meta-portfolio file
OVERRIDES = ("capital", "capital_currency", "account_currency", "leverage", "max_gross", "rebalance", "lot",
             "min_order_notional", "python", "papers_dir", "borrow_source")


def make_provider(args, meta: MetaPortfolio, directory: str) -> PwbProvider:
    """The catalogue strategies' provider the command line asks for."""
    fresh = None if args.fresh_prices is None else ("" if args.fresh_prices == "none" else args.fresh_prices)
    return PwbProvider(meta, os.path.join(directory, "books"), fresh_prices=fresh)


def make_borrow(meta: MetaPortfolio, broker: str):
    """The shorts' borrow source: the meta-portfolio's ``borrow_source`` (or ``--borrow-source``);
    unset, ``auto`` on an IBKR account and none on the simulated one."""
    return borrow_source(meta.borrow_source or ("auto" if broker == "ibkr" else "none"))


def make_account(args, directory: str, send: bool):
    """The account the command line asks for (an IBKR paper account opens read-only unless
    ``send``)."""
    if args.broker == "ibkr":
        return IBPaperAccount(args.host, args.port, args.client_id, account=args.account, send=send,
                              base_fx=args.base_fx_rate)
    return SimAccount(os.path.join(directory, "sim_account.json"), cash=args.sim_cash,
                      use_book_spreads=args.sim_costs == "harness", commission=args.sim_commission,
                      currency=args.sim_currency or args.account_currency, base_fx=args.base_fx_rate)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    overrides = {k: getattr(args, k) for k in OVERRIDES}
    meta = MetaPortfolio.load(args.meta, **overrides)
    directory = os.path.expanduser(args.dir)
    os.makedirs(directory, exist_ok=True)
    try:
        provider = make_provider(args, meta, directory)
    except (RuntimeError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.broker == "ibkr" and args.replay:
        print("--replay runs on the simulated account only", file=sys.stderr)
        return 2
    account = make_account(args, directory, args.send)
    try:
        if args.replay:
            replay = ReplayProvider(provider, args.as_of, args.replay + 1)
            runner = DailyRunner(meta, replay, account, directory, allow_stale=args.allow_stale,
                                 borrow=make_borrow(meta, args.broker))
            dates = replay.dates()[-args.replay:]
            reports = runner.replay(dates, send=True)
            last = reports[-1].row if reports else {}
            print(f"replayed {len(reports)} closes {dates[0] if dates else ''}..{dates[-1] if dates else ''}: "
                  f"NAV {last.get('nav_real', float('nan')):,.2f} against backtest "
                  f"{last.get('nav_expected', float('nan')):,.2f}; journal {runner.journal.csv}")
            return 0
        runner = DailyRunner(meta, provider, account, directory, allow_stale=args.allow_stale,
                             borrow=make_borrow(meta, args.broker))
        report = runner.run_day(args.as_of, send=args.send)
    except StaleData as exc:
        print(f"stale data: {exc}", file=sys.stderr)
        return 3
    except AccountCurrencyError as exc:
        print(f"account currency: {exc}", file=sys.stderr)
        return 2
    finally:
        account.close()
    r = report.row
    print(f"{report.date}: {len(report.plan.orders)} order(s) {'SENT' if args.send else 'proposed'}; "
          f"NAV {r['nav_real']:,.2f}, backtest {r['nav_expected']:,.2f}, gap {r['gap_cum']:+,.2f} (USD)")
    fx = report.fx
    if fx and fx["currency"] != "USD":
        print(f"account in {fx['currency']} ({fx['origin']}), {fx['rate']:.6g} USD per {fx['currency']} "
              f"({fx['source']}): NAV {r['nav_base']:,.2f} {fx['currency']}, gap {r['gap_cum_base']:+,.2f} "
              f"{fx['currency']}")
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
