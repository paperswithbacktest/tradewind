"""Run one catalogue strategy in signal mode and write its books (executed by ``PwbProvider``).

    python -m tradewind.live.daily.pwb_worker STRATEGY.py OUT.json --since D [--until D]
        [--last-n N] [--papers DIR] [--slug SLUG] [--fresh-prices CACHE [--session D]
        [--fresh-config JSON]]

In the environment that has pwb-toolbox: the strategy runs through the papers harness
(``backtester.py`` of ``--papers``), on Tradewind -- pwb-toolbox is bound to it with
``PWB_BACKTEST_ENGINE=tradewind`` and the name ``backtrader`` that the harness and the
strategies import is an alias of ``tradewind`` in this process only. ``--until`` cuts the
prices that ``pwb_toolbox.datasets.get_pricing`` returns at that date (a replay of past
days; other datasets are not cut). The process ends with ``os._exit`` once the file is
written: some strategies register exit hooks that would otherwise run.

``--fresh-prices CACHE`` completes every price load with AlphaVantage's bars up to the last
close (``fresh.py``). ``--fresh-config`` (JSON: ``strict``, ``tolerance``, ``proxies``,
``approximate``, from ``MetaPortfolio.fresh_config``) says how. A symbol still stale is listed
in the file (``stale``, ``stale_info``); with ``strict`` (``stale_symbol_policy: block``) it
stops the strategy and the file carries the list instead of books, otherwise the strategy runs
on its rows as they are and the books are written with the list.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
import types


def install_alias():
    import tradewind

    sys.modules["backtrader"] = tradewind
    utils = types.ModuleType("backtrader.utils")
    date = types.ModuleType("backtrader.utils.date")
    date.date2num, date.num2date, date.num2dt = tradewind.date2num, tradewind.num2date, tradewind.num2dt  # type: ignore[attr-defined]
    utils.date = date  # type: ignore[attr-defined]
    sys.modules["backtrader.utils"] = utils
    sys.modules["backtrader.utils.date"] = date
    sys.modules["backtrader.feeds"] = tradewind.feeds
    sys.modules["backtrader.indicators"] = tradewind.indicators
    sys.modules["backtrader.analyzers"] = tradewind.analyzers
    return tradewind


def cut_pricing(until: str) -> None:
    import pwb_toolbox.datasets as pwb_ds

    original = pwb_ds.get_pricing

    def get_pricing(*args, **kwargs):
        end = kwargs.get("end_date")
        kwargs["end_date"] = until if not end or str(end) > until else end
        return original(*args, **kwargs)

    pwb_ds.get_pricing = get_pricing


def fresh_options(text: str | None) -> dict:
    """``FreshPrices.from_env``'s options from ``--fresh-config`` (JSON; none: strict, the
    defaults)."""
    conf = json.loads(text) if text else {}
    return {"strict": bool(conf.get("strict", True)), "tolerance": dict(conf.get("tolerance") or {}),
            "proxies": dict(conf.get("proxies") or {}), "approximate": list(conf.get("approximate") or [])}


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("strategy")
    p.add_argument("out")
    p.add_argument("--since", required=True)
    p.add_argument("--until", default=None)
    p.add_argument("--last-n", type=int, default=10)
    p.add_argument("--papers", default=os.environ.get("PAPERS_DIR", "."))
    p.add_argument("--slug", default="")
    p.add_argument("--fresh-prices", default=None, metavar="CACHE",
                   help="complete the price loads with AlphaVantage (cache directory)")
    p.add_argument("--session", default=None, help="the close the prices are completed to")
    p.add_argument("--fresh-config", default=None, help="JSON: strict, tolerance, proxies, approximate")
    args = p.parse_args(argv)

    os.environ["PWB_BACKTEST_ENGINE"] = "tradewind"
    os.environ.setdefault("TQDM_DISABLE", "1")
    t0 = time.time()
    fresh = None
    payload: dict = {"slug": args.slug, "strategy": args.strategy, "since": args.since, "until": args.until}
    try:
        tradewind = install_alias()
        import matplotlib

        matplotlib.use("Agg")
        if args.until:
            cut_pricing(args.until)
        if args.fresh_prices:
            import pwb_toolbox.datasets as pwb_ds

            from tradewind.live.daily.fresh import FreshPrices, install

            fresh = FreshPrices.from_env(args.fresh_prices, args.session or args.until,
                                         log=lambda *a: print(*a, file=sys.stderr),
                                         **fresh_options(args.fresh_config))
            install(fresh, pwb_ds)
        from tradewind.live.daily.books import record_books

        papers = os.path.abspath(os.path.expanduser(args.papers))
        with open(args.strategy, encoding="utf-8") as fh:
            code = fh.read()
        sys.path.insert(0, papers)
        os.chdir(papers)
        log = io.StringIO()
        with record_books(since=args.since, last_n=args.last_n) as rec, contextlib.redirect_stdout(log):
            if os.path.exists(os.path.join(papers, "backtester.py")):
                import backtester as harness

                harness.Backtester().backtest(code)
            else:                                   # no harness: the strategy's own __main__
                exec(compile(code, args.strategy, "exec"), {"__name__": "__main__"})  # noqa: S102
        books = rec.books
        payload.update(books=[b.to_dict() for b in books.values()], runs=len(rec.runs),
                       tradewind=tradewind.__version__,
                       last=max(books) if books else None,
                       harness_lines=[ln for ln in log.getvalue().splitlines()
                                      if ln.lstrip().startswith(("Exposure:", "Underfunded", "Out of universe:"))][:10])
    except BaseException as exc:  # noqa: BLE001 - the caller reads the error from the file
        payload.update(books=[], error=f"{type(exc).__name__}: {str(exc)[:4000]}")
    if fresh is not None:
        payload["fresh"] = fresh.summary()
        if fresh.stale:
            payload.update(stale=dict(sorted(fresh.stale.items())), stale_info=dict(sorted(fresh.stale_info.items())))
            if fresh.strict:
                # a strategy that caught the error and went on with stale prices gives no book either
                payload.update(books=[], error=payload.get("error") or f"StalePrices: {fresh.message()[:4000]}")
    payload["seconds"] = round(time.time() - t0, 1)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, args.out)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
