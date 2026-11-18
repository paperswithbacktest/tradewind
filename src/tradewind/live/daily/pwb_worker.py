"""Run one catalogue strategy in signal mode and write its books (executed by ``PwbProvider``).

    python -m tradewind.live.daily.pwb_worker STRATEGY.py OUT.json --since D [--until D]
        [--last-n N] [--papers DIR] [--slug SLUG]

In the environment that has pwb-toolbox: the strategy runs through the papers harness
(``backtester.py`` of ``--papers``), on Tradewind -- pwb-toolbox is bound to it with
``PWB_BACKTEST_ENGINE=tradewind`` and the name ``backtrader`` that the harness and the
strategies import is an alias of ``tradewind`` in this process only. ``--until`` cuts the
prices that ``pwb_toolbox.datasets.get_pricing`` returns at that date (a replay of past
days; other datasets are not cut). The process ends with ``os._exit`` once the file is
written: some strategies register exit hooks that would otherwise run.
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


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("strategy")
    p.add_argument("out")
    p.add_argument("--since", required=True)
    p.add_argument("--until", default=None)
    p.add_argument("--last-n", type=int, default=10)
    p.add_argument("--papers", default=os.environ.get("PAPERS_DIR", "."))
    p.add_argument("--slug", default="")
    args = p.parse_args(argv)

    os.environ["PWB_BACKTEST_ENGINE"] = "tradewind"
    os.environ.setdefault("TQDM_DISABLE", "1")
    t0 = time.time()
    payload: dict = {"slug": args.slug, "strategy": args.strategy, "since": args.since, "until": args.until}
    try:
        tradewind = install_alias()
        import matplotlib

        matplotlib.use("Agg")
        if args.until:
            cut_pricing(args.until)
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
        payload.update(books=[], error=f"{type(exc).__name__}: {str(exc)[:1000]}")
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
