"""Daily opens and closes of a few symbols from the PWB datasets (executed by ``PwbProvider.prices``).

    python -m tradewind.live.daily.pwb_prices OUT.json SYM[,SYM...] --start D --end D
        [--fresh-prices CACHE [--session D] [--fresh-config JSON]]

In the environment that has pwb-toolbox: ``pwb_toolbox.datasets.get_pricing`` (completed with
AlphaVantage's bars with ``--fresh-prices``, as the strategies' loads are). Used for the
prices of an index's substitute that no sleeve holds (``SPX`` -> ``SPY``, ``VIX`` -> ``VIXY``).
Writes ``{"prices": {symbol: {date: [open, close]}}}``, or ``{"error": ...}``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys


def load(symbols: list[str], start: str, end: str) -> dict[str, dict[str, list[float]]]:
    import pwb_toolbox.datasets as pwb_ds

    frame = pwb_ds.get_pricing(symbols, fields=["open", "close"], start_date=start, end_date=end)
    out: dict[str, dict[str, list[float]]] = {}
    for sym in symbols:
        if sym not in frame.columns.get_level_values(0):
            continue
        sub = frame[sym].dropna(subset=["close"])
        rows = {}
        for when, row in sub.iterrows():
            c = float(row["close"])
            o = float(row["open"]) if math.isfinite(float(row["open"])) else c
            if c > 0:
                rows[str(when)[:10]] = [o if o > 0 else c, c]
        out[sym] = rows
    return out


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("out")
    p.add_argument("symbols")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--fresh-prices", default=None, metavar="CACHE")
    p.add_argument("--session", default=None)
    p.add_argument("--fresh-config", default=None)
    args = p.parse_args(argv)
    os.environ.setdefault("TQDM_DISABLE", "1")
    payload: dict = {}
    try:
        if args.fresh_prices:
            import pwb_toolbox.datasets as pwb_ds

            from tradewind.live.daily.fresh import FreshPrices, install
            from tradewind.live.daily.pwb_worker import fresh_options

            fresh = FreshPrices.from_env(args.fresh_prices, args.session or args.end,
                                         log=lambda *a: print(*a, file=sys.stderr),
                                         **fresh_options(args.fresh_config))
            install(fresh, pwb_ds)
        payload["prices"] = load([s for s in args.symbols.split(",") if s], args.start, args.end)
    except Exception as exc:  # noqa: BLE001 - the caller reads the error from the file
        payload["error"] = f"{type(exc).__name__}: {str(exc)[:2000]}"
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.replace(tmp, args.out)


if __name__ == "__main__":
    main()
