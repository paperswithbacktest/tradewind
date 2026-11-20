"""Run one corpus ``strategy.py`` through the papers harness, as the reference campaign did.

    cd <papers tree> && python measure_one.py published/<slug>/strategy.py --engine tradewind --nav-out X.json

The measurement is the campaign's (``measure_one_nav_0147.py`` of the 2026-11 grand
recalcul): the papers ``backtester.py`` harness, pwb-toolbox, the strategy file unchanged,
``PYTHONHASHSEED`` fixed by the caller. Two engines:

* ``tradewind``: the name ``backtrader`` (and the submodules the corpus imports) resolves to
  Tradewind for this process only; an ``ENGINE`` line at exit says how many modules were
  loaded from a backtrader package (must be 0);
* ``backtrader``: the reference engine, only ever in its own environment, to regenerate a
  reference set.

With ``TW_ORACLE_EPOCH`` and ``TW_ORACLE_HUB_COMMITS`` set, every dataset is read at the
revision it had at that time (``pinned_hub.py``), and a ``PINNED`` line lists them.
Prints one ``RESULT {json}`` line.
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

HERE = os.path.dirname(os.path.abspath(__file__))


def alias_backtrader_to_tradewind():
    if os.environ.get("TRADEWIND_SRC"):
        sys.path.insert(0, os.environ["TRADEWIND_SRC"])
    import tradewind

    sys.modules["backtrader"] = tradewind
    utils = types.ModuleType("backtrader.utils")
    date = types.ModuleType("backtrader.utils.date")
    date.date2num, date.num2date, date.num2dt = tradewind.date2num, tradewind.num2date, tradewind.num2dt
    utils.date = date
    sys.modules["backtrader.utils"] = utils
    sys.modules["backtrader.utils.date"] = date
    sys.modules["backtrader.feeds"] = tradewind.feeds
    sys.modules["backtrader.indicators"] = tradewind.indicators
    return tradewind


def leaked_backtrader_modules():
    return sorted({n for n, m in list(sys.modules.items())
                   if m is not None and os.sep + "backtrader" + os.sep in (getattr(m, "__file__", "") or "")})


def _plain(x):
    return json.loads(json.dumps(x, default=lambda o: o.item() if hasattr(o, "item") else str(o)))


def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("strategy", help="path of strategy.py, relative to the papers tree (the cwd)")
    p.add_argument("--engine", choices=("tradewind", "backtrader"), default="tradewind")
    p.add_argument("--nav-out", required=True)
    args = p.parse_args()

    os.environ.setdefault("TQDM_DISABLE", "1")
    os.environ["PWB_BACKTEST_ENGINE"] = args.engine
    papers = os.getcwd()
    sys.path.insert(0, papers)
    engine_mod = alias_backtrader_to_tradewind() if args.engine == "tradewind" else None

    pinned = None
    if os.environ.get("TW_ORACLE_EPOCH"):
        sys.path.insert(0, HERE)
        import pinned_hub

        pinned_hub.install()
        pinned = pinned_hub

    import tqdm as _tqdm

    with contextlib.suppress(Exception):
        _tqdm.tqdm.__del__ = lambda self: None   # papers/CLAUDE.md trap 1: segfault at shutdown
    import matplotlib

    matplotlib.use("Agg")
    import warnings

    import pwb_toolbox

    try:
        from pwb_toolbox.datasets import UnboundedMinuteLoadWarning

        warnings.simplefilter("error", UnboundedMinuteLoadWarning)
    except ImportError:
        pass
    import backtester as harness  # the papers harness

    out = {"strategy": args.strategy, "engine": args.engine}
    with contextlib.suppress(Exception):
        import importlib.metadata as md

        out["toolbox_version"] = md.version("pwb-toolbox")
    if engine_mod is not None:
        out["tradewind_version"] = engine_mod.__version__
        out["tradewind_file"] = engine_mod.__file__
    t0 = time.time()
    buf = io.StringIO()
    try:
        with open(args.strategy, encoding="utf-8") as fh:
            code = fh.read()
        b = harness.Backtester()
        with contextlib.redirect_stdout(buf):
            b.backtest(code)
        log = b.strategy.log_data
        st = b.strategy
        os.makedirs(os.path.dirname(os.path.abspath(args.nav_out)), exist_ok=True)
        with open(args.nav_out, "w") as fh:
            json.dump({"slug": os.path.basename(os.path.dirname(args.strategy)),
                       "log": [{"date": str(x["date"]), "value": float(x["value"])} for x in log],
                       "exposure": {"run_report": _plain(getattr(st, "run_report", None)),
                                    "exposure_cap": _num(getattr(st, "exposure_cap", None))}}, fh)
        out.update(status="ok", sharpe=float(b.metrics["sharpeRatio"]), n_nav=len(log),
                   start=str(log[0]["date"]), end=str(log[-1]["date"]), final_nav=float(log[-1]["value"]))
    except BaseException as exc:  # noqa: BLE001 - a strategy may SystemExit
        out.update(status="error", error=f"{type(exc).__name__}: {str(exc)[:300]}")
    text = buf.getvalue()
    out["cost_lines"] = [ln.strip() for ln in text.splitlines() if "[costs]" in ln or "cost source" in ln.lower()][:12]
    out["seconds"] = round(time.time() - t0, 1)
    out["pwb_toolbox"] = pwb_toolbox.__file__
    if args.engine == "tradewind":
        out["leaked_backtrader_modules"] = leaked_backtrader_modules()
    if pinned is not None:
        out["pinned"] = dict(sorted(pinned.PINNED.items()))
        out["unpinned"] = sorted(pinned.UNPINNED)
    sys.stdout.write("RESULT " + json.dumps(out, default=str) + "\n")
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
