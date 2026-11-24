"""The daily run from the command line.

    # every evening after the US close: propose (default), read orders/DATE.md, then send
    python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --propose
    python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --send

    # the same on a simulated account, and a replay of the last 20 closes on one
    python -m tradewind.live.daily --meta meta.yaml --dir /tmp/sim --broker sim --send
    python -m tradewind.live.daily --meta meta.yaml --dir /tmp/replay --broker sim --replay 20

    # the PWB price datasets are monthly: complete them up to the last close (IB first, on a
    # read-only connection of its own; AlphaVantage for the rest, key in ALPHAVANTAGE_API_KEY)
    python -m tradewind.live.daily --meta meta.yaml --dir ~/pwb-paper --broker ibkr --propose \
        --fresh-prices ibkr,alphavantage

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
from .brokers import IBPaperAccount, KrakenAccount, SimAccount
from .meta import REBALANCE, MetaPortfolio
from .providers import PwbProvider, ReplayProvider
from .runner import AccountCurrencyError, DailyRunner, StaleData


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tradewind.live.daily", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--meta", required=True, help="the meta-portfolio (YAML, JSON or CSV)")
    p.add_argument("--dir", default="paper", help="state, proposals and journal (default ./paper)")
    p.add_argument("--broker", choices=["sim", "ibkr", "kraken"], default="sim")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--propose", action="store_true", help="write the proposal only (the default)")
    mode.add_argument("--send", action="store_true", help="place the orders (market-on-open)")
    mode.add_argument("--replay", type=int, metavar="N",
                      help="simulated account only: run the last N closes, orders sent on the simulation")
    p.add_argument("--as-of", default=None, help="the close to trade from (default: the data's last close)")
    p.add_argument("--allow-stale", action="store_true", help="trade a sleeve whose last close is older")
    p.add_argument("--fresh-prices", default=None, metavar="SOURCES",
                   help="complete the monthly price datasets up to the last close: ibkr,alphavantage (IB first, "
                        "AlphaVantage for the rest), alphavantage, ibkr, or none (AlphaVantage's key in "
                        "ALPHAVANTAGE_API_KEY); kraken: crypto closes from Kraken's public data (no key)")
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
    g.add_argument("--stale-symbol-policy", choices=["hold", "close", "block"],
                   help="a symbol still stale after the fresh-price top-up: hold (no order on it), close (sold), "
                        "block (nothing proposed); default: the meta-portfolio's, hold")
    g.add_argument("--out-of-universe-policy", choices=["no_new", "follow"],
                   help="a name a sleeve holds outside its point-in-time universe: no_new (kept, reduced or "
                        "sold, never bought; default) or follow (as the books say)")
    g.add_argument("--sleeve-error-policy", choices=["skip", "block"],
                   help="a sleeve in error: skip (set aside for the day, its holdings kept, the others "
                        "proposed; default) or block (nothing proposed); default: the meta-portfolio's")
    g.add_argument("--lot", type=float)
    g.add_argument("--min-order", type=float, dest="min_order_notional")
    g.add_argument("--python", help="the Python that has pwb-toolbox")
    g.add_argument("--papers-dir")
    i = p.add_argument_group("IBKR paper account")
    i.add_argument("--host", default="127.0.0.1")
    i.add_argument("--port", type=int, default=7497, help="7497 TWS paper (default), 4002 IB Gateway paper")
    i.add_argument("--client-id", type=int, default=31)
    i.add_argument("--account", default="", help="DU... when the login manages several accounts")
    i.add_argument("--ib-data-client-id", type=int, default=None,
                   help="--fresh-prices ibkr: the client id of the read-only price connection (default the "
                        "meta-portfolio's fresh_ib client_id, 957); never the account's --client-id")
    i.add_argument("--base-fx-rate", type=float, default=None,
                   help="an account whose currency is not USD: the price of 1 unit of it in USD (EURUSD), "
                        "the last resort after IB's own rate and PWB's forex series")
    k = p.add_argument_group("Kraken spot account (spec/11; key in KRAKEN_API_KEY / KRAKEN_API_SECRET)")
    k.add_argument("--kraken-quote", choices=["USD", "EUR"], default="USD", help="the pairs' quote currency")
    k.add_argument("--kraken-live", action="store_true",
                   help="with --send AND TRADEWIND_KRAKEN_LIVE=1 in the environment, orders trade; otherwise "
                        "every order is sent with validate=true (checked by Kraken, never traded)")
    k.add_argument("--kraken-allowed", default=None, metavar="SYMBOLS",
                   help="comma-separated whitelist of the books' crypto symbols (BTC,ETH); others are skipped")
    k.add_argument("--kraken-max-order", type=float, default=None, metavar="USD",
                   help="refuse any order whose notional exceeds this (USD)")
    k.add_argument("--kraken-taker-fee", type=float, default=0.004, help="taker fee for the checks (0.004)")
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
             "min_order_notional", "python", "papers_dir", "borrow_source", "stale_symbol_policy",
             "out_of_universe_policy", "sleeve_error_policy")


def make_provider(args, meta: MetaPortfolio, directory: str) -> PwbProvider:
    """The catalogue strategies' provider the command line asks for (the IB price connection
    on the account's host and port, its own client id)."""
    fresh = None if args.fresh_prices is None else ("" if args.fresh_prices == "none" else args.fresh_prices)
    ib = {"host": args.host, "port": args.port, "client_id": args.ib_data_client_id,
          "account_client_id": args.client_id if args.broker == "ibkr" else None}
    return PwbProvider(meta, os.path.join(directory, "books"), fresh_prices=fresh, ib=ib)


def make_borrow(meta: MetaPortfolio, broker: str):
    """The shorts' borrow source: the meta-portfolio's ``borrow_source`` (or ``--borrow-source``);
    unset, ``auto`` on an IBKR account and none on the simulated one."""
    return borrow_source(meta.borrow_source or ("auto" if broker == "ibkr" else "none"))  # spot crypto: none


def make_account(args, directory: str, send: bool):
    """The account the command line asks for (an IBKR paper account opens read-only unless
    ``send``)."""
    if args.broker == "kraken":
        allowed = [x.strip() for x in args.kraken_allowed.split(",") if x.strip()] if args.kraken_allowed else None
        return KrakenAccount(quote=args.kraken_quote, send=send, live=args.kraken_live, allowed=allowed,
                             max_order_notional=args.kraken_max_order, taker_fee=args.kraken_taker_fee,
                             base_fx=args.base_fx_rate)
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
    if args.broker in ("ibkr", "kraken") and args.replay:
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
          f"NAV {r['nav_real']:,.2f}, backtest {r['nav_expected']:,.2f}, gap {r['gap_cum']:+,.2f} (USD), "
          f"without fx {r.get('gap_cum_ex_fx', r['gap_cum']):+,.2f}")
    fx = report.fx
    if fx and fx["currency"] != "USD":
        print(f"account in {fx['currency']} ({fx['origin']}), {fx['rate']:.6g} USD per {fx['currency']} "
              f"({fx['source']}): NAV {r['nav_base']:,.2f} {fx['currency']}, gap {r['gap_cum_base']:+,.2f} "
              f"{fx['currency']}")
    if callable(getattr(provider, "price_report", None)) and provider.fresh_prices:
        from .session import prices_line

        print(prices_line(provider.price_report()))
    for w in report.warnings:
        print("warning:", w)
    print("proposal:", report.proposal_md)
    if args.send:
        bad = [s for s in report.sent if s.get("status") in ("rejected", "cancelled", "refused")]
        for s in bad:
            print(f"  {s['symbol']} {s['quantity']:+g}: {s['status']} {s.get('reason', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
