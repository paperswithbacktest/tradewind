"""The meta-portfolio: which strategies, at which weights, with which leverage and limits.

Read from YAML (needs PyYAML), JSON or CSV. A CSV is a list of sleeves, one row each, with at
least ``slug`` and ``weight`` -- the Phase 1 selection file (``selection_final_*.csv``) reads as
it is, its ``leverage_meta`` column giving the leverage. YAML / JSON carry the options too::

    leverage: 4.0              # every weight is multiplied by it
    max_gross: 4.0             # the summed book is scaled down to this gross / NAV
    rebalance: monthly         # when the sleeves' units are reset: daily, weekly, monthly, never
    lot: 1                     # shares are rounded to this lot (0: fractional shares)
    min_order_notional: 200    # smaller orders are not sent (an order closing a line always is)
    papers_dir: ~/dev/pwb/papers
    python: /path/to/venv/bin/python      # the environment that has pwb-toolbox
    exclude_symbols: [VIX]     # never traded (an index, a spot commodity series...)
    sleeves:
      - {slug: protective_asset_allocation_paa_..., weight: 0.075}
      - {slug: decomposing_the_price_earnings_ratio, weight: 0.109,
         path: ~/dev/pwb/phase1/concentrated/decomposing_the_price_earnings_ratio/strategy.py}
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import pandas as pd

REBALANCE = ("daily", "weekly", "monthly", "never")


@dataclass
class Sleeve:
    slug: str
    weight: float
    path: str = ""                      # the strategy file; default <papers_dir>/published/<slug>/strategy.py

    def strategy_path(self, papers_dir: str) -> str:
        if self.path:
            return os.path.expanduser(self.path)
        return os.path.join(os.path.expanduser(papers_dir), "published", self.slug, "strategy.py")


@dataclass
class MetaPortfolio:
    sleeves: list[Sleeve]
    leverage: float = 1.0
    max_gross: float = 4.0
    rebalance: str = "monthly"
    lot: float = 1.0
    min_order_notional: float = 0.0
    capital: float | None = None        # the capital the weights apply to; None: the account's NAV
    papers_dir: str = "~/dev/pwb/papers"
    python: str = ""
    exclude_symbols: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.rebalance not in REBALANCE:
            raise ValueError(f"rebalance must be one of {REBALANCE}, not {self.rebalance!r}")
        if self.leverage <= 0 or self.max_gross <= 0:
            raise ValueError("leverage and max_gross must be positive")
        slugs = [s.slug for s in self.sleeves]
        if len(set(slugs)) != len(slugs):
            raise ValueError("a sleeve is listed twice")
        if any(s.weight < 0 for s in self.sleeves):
            raise ValueError("weights must be non-negative")

    @property
    def weights(self) -> dict[str, float]:
        return {s.slug: s.weight for s in self.sleeves}

    def tradable(self, symbol: str) -> bool:
        return symbol not in self.exclude_symbols

    # -- loading ---------------------------------------------------------------------------------
    @classmethod
    def from_dict(cls, d: dict) -> MetaPortfolio:
        d = dict(d)
        sleeves = [s if isinstance(s, Sleeve) else Sleeve(slug=str(s["slug"]), weight=float(s["weight"]),
                                                         path=str(s.get("path") or ""))
                   for s in d.pop("sleeves")]
        known = {f for f in cls.__dataclass_fields__ if f != "sleeves"}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown meta-portfolio options: {sorted(unknown)}")
        return cls(sleeves=sleeves, **d)

    @classmethod
    def load(cls, path: str, **overrides) -> MetaPortfolio:
        path = os.path.expanduser(path)
        ext = os.path.splitext(path)[1].lower()
        if ext == ".csv":
            frame = pd.read_csv(path)
            if not {"slug", "weight"} <= set(frame.columns):
                raise ValueError(f"{path}: a sleeve CSV needs the columns slug and weight")
            d: dict = {"sleeves": [{"slug": r.slug, "weight": float(r.weight),
                                    "path": (r.path if "path" in frame.columns and isinstance(r.path, str) else "")}
                                   for r in frame.itertuples()]}
            if "leverage_meta" in frame.columns:
                d["leverage"] = float(frame["leverage_meta"].iloc[0])
        else:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            if ext == ".json":
                d = json.loads(text)
            else:
                try:
                    import yaml
                except ImportError as exc:  # pragma: no cover - depends on the environment
                    raise ImportError("a YAML meta-portfolio needs PyYAML (pip install pyyaml), "
                                      "or write it as JSON or CSV") from exc
                d = yaml.safe_load(text)
        d.update({k: v for k, v in overrides.items() if v is not None})
        return cls.from_dict(d)


__all__ = ["REBALANCE", "MetaPortfolio", "Sleeve"]
