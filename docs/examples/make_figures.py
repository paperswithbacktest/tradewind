"""Regenerate the documentation's figures from real backtests on Papers With Backtest data.

    pip install -e '.[plot]' pwb-toolbox          # and, for the tear sheet: '.[tearsheets]'
    export HF_ACCESS_TOKEN=...                    # or PWB_API_KEY: the datasets' access key
    python docs/examples/make_figures.py          # about a minute, under 1 GB of memory

For each example of ``docs/examples/pwb/`` it runs the backtest once and writes
``docs/images/<name>.png`` (``Performance.plot()``) and ``docs/examples/pwb/<name>_metrics.md``
(the caption and the statistics the pages include, so the numbers in the documentation are
the ones of the figure). With pyfolio-reloaded installed it also writes
``docs/images/momentum_sectors_tearsheet.png`` (``Performance.tearsheet("simple")``).
"""

from __future__ import annotations

import argparse
import importlib
import pathlib
import sys
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import tradewind as tw

HERE = pathlib.Path(__file__).resolve().parent
IMAGES = HERE.parent / "images"
EXAMPLES = ["momentum_sectors", "risk_parity", "trend_following"]
sys.path.insert(0, str(HERE / "pwb"))


def _pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def metrics_markdown(module, perf) -> str:
    r = perf.report
    first_fill = perf.transactions.index.min()
    bench = perf.benchmark_nav
    bench = bench[bench > 0]
    b_cagr = tw.metrics.cagr(bench.to_list())
    b_sharpe = tw.metrics.sharpe_ratio(bench.to_list())
    b_dd = tw.metrics.max_drawdown(bench.to_list()).depth
    caption = (f"{module.TITLE}. Daily bars {r.start:%Y-%m-%d} to {r.end:%Y-%m-%d} "
               f"(first trade {first_fill:%Y-%m-%d}, after the warm-up); universe: "
               f"{module.UNIVERSE}. Costs: a commission of {module.COMMISSION * 1e4:.0f} bp "
               f"of the traded value on every fill, market orders filled at the next open, "
               f"no other slippage. Prices: Papers With Backtest ETF dataset, total return. "
               f"Starting cash 1,000,000. Statistics over every bar shown, the warm-up included.")
    rows = [
        ("CAGR", _pct(r.cagr), _pct(b_cagr)),
        ("Volatility (annualised)", _pct(r.volatility),
         _pct(tw.metrics.annualized_volatility(bench.to_list()))),
        ("Sharpe ratio (no risk-free rate)", f"{r.sharpe:.2f}", f"{b_sharpe:.2f}"),
        ("Max drawdown", _pct(r.max_drawdown), _pct(b_dd)),
        ("Beta to SPY", f"{r.beta:.2f}" if r.beta is not None else "n/a", "1.00"),
        ("Average gross / net exposure", f"{r.gross_exposure:.2f} / {r.net_exposure:.2f}",
         "1.00 / 1.00"),
        ("Turnover (one-way, per year)", f"{r.turnover:.2f}", ""),
        ("Fills", f"{r.fills:,}", ""),
    ]
    lines = [f"*{caption}*", "", "| | strategy | SPY, same dates |", "|---|---|---|"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="*", default=EXAMPLES, help="examples to run")
    parser.add_argument("--no-tearsheet", action="store_true")
    args = parser.parse_args(argv)
    from _pwb_data import daily_bars

    IMAGES.mkdir(exist_ok=True)
    for name in args.only:
        module = importlib.import_module(name)
        symbols = list(dict.fromkeys(getattr(module, "SECTORS", []) + getattr(module, "ASSETS", [])
                                     + [module.BENCHMARK]))
        began = time.perf_counter()
        frames = daily_bars(symbols, module.START, module.END)
        strategy = module.build(frames).run()[0]
        perf = strategy.analyzers.performance
        fig = perf.plot(title=module.TITLE)
        fig.savefig(IMAGES / f"{name}.png", dpi=100)
        plt.close(fig)
        (HERE / "pwb" / f"{name}_metrics.md").write_text(metrics_markdown(module, perf))
        r = perf.report
        print(f"{name}: {r.start:%Y-%m-%d}..{r.end:%Y-%m-%d} CAGR {r.cagr:.2%} "
              f"Sharpe {r.sharpe:.2f} maxDD {r.max_drawdown:.1%} "
              f"({time.perf_counter() - began:.1f} s)")
        if name == "momentum_sectors" and not args.no_tearsheet:
            try:
                figures = perf.tearsheet("simple")
            except ImportError as err:
                print(f"tear sheet skipped: {err}")
            else:
                figures[0].savefig(IMAGES / f"{name}_tearsheet.png", dpi=55, bbox_inches="tight")
                plt.close("all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
