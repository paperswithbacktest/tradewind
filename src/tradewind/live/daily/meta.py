"""The meta-portfolio: which strategies, at which weights, with which leverage and limits.

Read from YAML (needs PyYAML), JSON or CSV. A CSV is a list of sleeves, one row each, with at
least ``slug`` and ``weight`` -- the Phase 1 selection file (``selection_final_*.csv``) reads as
it is, its ``leverage_meta`` column giving the leverage. YAML / JSON carry the options too::

    leverage: 4.0              # every weight is multiplied by it
    max_gross: 4.0             # the summed book is scaled down to this gross / NAV
    rebalance: monthly         # when the sleeves' units are reset: daily, weekly, monthly, never
    lot: 1                     # shares are rounded to this lot (0: fractional shares)
    capital: 110000            # the capital the weights apply to (default: the account's NAV) ...
    capital_currency: USD      # ... in USD, or by default in the account's currency
    account_currency: EUR      # optional check: the run stops unless the account is in EUR
    base_currency: EUR         # the currency the investor counts in (default USD: nothing changes);
                               #   the reference backtest is then computed in it (spec/13)
    fx_policy: hedged          # unhedged (default): the dollar exposure is suffered; hedged: the
                               #   net value's dollar exposure is sold against the base currency
    fx_hedge_rebalance: monthly    # hedged: daily, weekly or monthly (default)
    fx_hedge_half_spread: 0.000025 # the pair's half-spread, a fraction of the notional
    fx_rates: {USD: fred:DFF, EUR: fred:ECBESTRVOLWGTTRMDMNRT}   # the carry's short rates (defaults)
    min_order_notional: 200    # smaller orders are not sent (an order closing a line always is)
    papers_dir: ~/dev/pwb/papers
    python: /path/to/venv/bin/python      # the environment that has pwb-toolbox
    exclude_symbols: [GC1]     # never traded (a spot commodity series...)
    substitutes:               # indices are not tradable: what is traded instead (instruments.py)
      SPX: SPY                 # the default: SPY at the same notional
      VIX: VIXY                # an ETP at the same notional (a proxy: VIXY is not the VIX)
      # SPX: {symbol: MES, kind: future, multiplier: 5, expiry: "20261218", exchange: CME}
    notes:                     # said in the day's warnings while the symbol is held or targeted
      ONDS: hard to borrow (IB shortable 2.0 on 2026-11-20)
    borrow_check: false        # IBKR account: check the shortable tick of every short (read-only)
    borrow_source: auto        # the shorts' fee and availability: auto (hub, then IB), hub, ib, none
    borrow_fee_alert: 1.0      # % a year: a fee at or above it flags the short as hard to borrow
    fresh_prices: ibkr,alphavantage  # complete the monthly price datasets up to the last close:
                               #   IB first (ib_prices.py), AlphaVantage for the rest; or alphavantage
    fresh_close_grace:         # minutes after the close before the day's bar is taken (both
      equity: 15               #   sources; av_equity / av_fx: AlphaVantage's alone)
      fx: 15
    fresh_ib:                  # the IB price connection (read-only, its own client id)
      client_id: 957           #   never the account's; concurrency 40, timeout 90, duration "3 M"
    fresh_price_tolerance:     # the chain factor's largest move, by kind or symbol (fresh.py)
      fx_minor: 0.015          # defaults: equity 0.005, fx 0.005 (majors), fx_minor 0.015,
      CHGG: 0.01               #   crypto 0.005, index 0.01; a number: every kind
    fresh_index_proxies:       # an index continued with an ETF's returns (default: the ETF
      SPX: SPY                 #   substitutes); {} for none
    fresh_index_proxy_approximate: [VIX]   # chained although VIXY does not track the VIX (unchecked)
    stale_symbol_policy: hold  # a symbol still stale after the top-up: hold (default), close, block
    stale_symbol_max_weight: 0.2   # a sleeve with more of its gross on stale symbols stays in error
    out_of_universe_policy: no_new # a name a sleeve holds outside its point-in-time universe: kept,
                                   #   reduced or sold, never bought (default); follow: as the books say
    universe_max_age_days: 35      # alert universe_age when the point-in-time universe a sleeve
                                   #   uses ends more than this many days before the session
    sleeve_error_policy: skip      # a sleeve in error (its run failed, too much of it stale, no
                                   #   close for the session): skip (default) sets it aside for
                                   #   the day -- what the account holds for it is kept, nothing
                                   #   new -- and the others are proposed; block: nothing proposed
    sleeve_error_max_weight: 0.5   # skip: when the sleeves in error weigh more than this part of
                                   #   the summed weights, nothing is proposed after all
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

from .instruments import DEFAULT_SUBSTITUTES, Substitute, fx_refusal, is_index

REBALANCE = ("daily", "weekly", "monthly", "never")
#: what the run does with a symbol still stale after the fresh-price top-up (runner.py)
STALE_POLICIES = ("hold", "close", "block")
#: what the run does with a name a sleeve holds outside its point-in-time universe (aggregate.py)
OUT_OF_UNIVERSE_POLICIES = ("no_new", "follow")
#: what the run does with a sleeve in error (runner.py)
SLEEVE_ERROR_POLICIES = ("skip", "block")


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
    capital_currency: str = ""          # the currency of `capital`: "" the account's, or USD
    account_currency: str = ""          # "": read off the account; set: the run stops if the account differs
    base_currency: str = ""             # "": USD (the books'); else the account's currency (spec/13)
    fx_policy: str = "unhedged"         # unhedged or hedged (spec/13)
    fx_hedge_rebalance: str = "monthly" # hedged: when the dollar exposure is sold again
    fx_hedge_half_spread: float = 0.000025
    fx_rates: dict = field(default_factory=dict)        # {currency: fred:ID | ecb:FLOW/KEY | path | number}
    papers_dir: str = "~/dev/pwb/papers"
    python: str = ""
    exclude_symbols: list[str] = field(default_factory=list)
    fresh_prices: str = ""              # "alphavantage", "ibkr,alphavantage": the loads completed to the last close
    fresh_close_grace: dict = field(default_factory=dict)  # {equity, fx, index, crypto, av_equity, av_fx: minutes}
    fresh_ib: dict = field(default_factory=dict)            # ib_prices.IBDataConfig: client_id, concurrency...
    substitutes: dict = field(default_factory=dict)     # {index: Substitute | "ETF" | {...} | None}
    indices: list[str] = field(default_factory=list)   # more symbols that are indices (not tradable)
    notes: dict = field(default_factory=dict)          # {symbol: text} said while held or targeted
    borrow_check: bool = False          # IBKR: read the shortable tick of the shorts (warnings only)
    borrow_source: str = ""             # the shorts' fee and availability: auto, hub, ib, none ("": auto at IBKR)
    borrow_fee_alert: float = 1.0       # % a year: a short whose fee is at or above it is hard to borrow
    fx_odd_lot: float = 25_000.0        # an IDEALPRO order below this USD notional is an odd lot
    fresh_price_tolerance: dict = field(default_factory=dict)   # {kind or symbol: fraction}, or a number
    fresh_index_proxies: dict | None = None     # {index: ETF}; None: the ETF substitutes
    fresh_index_proxy_approximate: list[str] = field(default_factory=list)
    stale_symbol_policy: str = "hold"   # hold: no order on it; close: target 0; block: nothing proposed
    stale_symbol_max_weight: float = 0.2    # of a sleeve's gross: above it, the sleeve stays in error
    out_of_universe_policy: str = "no_new"  # no_new: never opened nor added to live; follow: the books
    universe_max_age_days: float = 35.0     # the point-in-time universe's last date may lag the session this much
    sleeve_error_policy: str = "skip"   # skip: a sleeve in error is set aside for the day; block: nothing proposed
    sleeve_error_max_weight: float = 0.5    # skip: above this part of the summed weights in error, nothing proposed

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
        table: dict = dict(DEFAULT_SUBSTITUTES)
        table.update(self.substitutes or {})            # None (or false) for an index: no substitute
        self.substitutes = {k: v for k, v in ((k, Substitute.parse(v)) for k, v in table.items()) if v is not None}
        for idx, sub in self.substitutes.items():
            if self.is_index(sub.symbol):
                raise ValueError(f"the substitute of {idx} is an index itself: {sub.symbol}")
        self.notes = {str(k): str(v) for k, v in (self.notes or {}).items()}
        self.borrow_source = (self.borrow_source or "").lower()
        if self.borrow_source not in ("", "auto", "hub", "ib", "none"):
            raise ValueError(f"borrow_source is auto, hub, ib or none, not {self.borrow_source!r}")
        self.stale_symbol_policy = str(self.stale_symbol_policy or "hold").lower()
        if self.stale_symbol_policy not in STALE_POLICIES:
            raise ValueError(f"stale_symbol_policy is one of {STALE_POLICIES}, not {self.stale_symbol_policy!r}")
        self.out_of_universe_policy = str(self.out_of_universe_policy or "no_new").lower()
        if self.out_of_universe_policy not in OUT_OF_UNIVERSE_POLICIES:
            raise ValueError(f"out_of_universe_policy is one of {OUT_OF_UNIVERSE_POLICIES}, "
                             f"not {self.out_of_universe_policy!r}")
        self.sleeve_error_policy = str(self.sleeve_error_policy or "skip").lower()
        if self.sleeve_error_policy not in SLEEVE_ERROR_POLICIES:
            raise ValueError(f"sleeve_error_policy is one of {SLEEVE_ERROR_POLICIES}, not {self.sleeve_error_policy!r}")
        self.sleeve_error_max_weight = float(self.sleeve_error_max_weight)
        if not 0.0 <= self.sleeve_error_max_weight <= 1.0:
            raise ValueError("sleeve_error_max_weight is a fraction of the summed weights, between 0 and 1")
        self.stale_symbol_max_weight = float(self.stale_symbol_max_weight)
        self.universe_max_age_days = float(self.universe_max_age_days)
        if not self.universe_max_age_days > 0:
            raise ValueError("universe_max_age_days is a number of calendar days, above 0")
        if not 0.0 <= self.stale_symbol_max_weight <= 1.0:
            raise ValueError("stale_symbol_max_weight is a fraction of a sleeve's gross, between 0 and 1")
        if isinstance(self.fresh_prices, (list, tuple)):
            self.fresh_prices = ",".join(str(x) for x in self.fresh_prices)
        self.fresh_prices = ",".join(x.strip().lower() for x in str(self.fresh_prices or "").split(",") if x.strip())
        self.fresh_close_grace = {str(k): float(v) for k, v in dict(self.fresh_close_grace or {}).items()}
        if any(not 0.0 <= v <= 24 * 60 for v in self.fresh_close_grace.values()):
            raise ValueError("fresh_close_grace: minutes after the close, between 0 and 1440")
        from .ib_prices import IBDataConfig

        IBDataConfig.from_dict(self.fresh_ib)           # checked here, used by the provider
        tol = self.fresh_price_tolerance
        if isinstance(tol, (int, float)) and not isinstance(tol, bool):
            from .fresh import DEFAULT_TOLERANCE

            tol = dict.fromkeys(DEFAULT_TOLERANCE, float(tol))
        self.fresh_price_tolerance = {str(k): float(v) for k, v in dict(tol or {}).items()}
        if any(not 0.0 < v < 0.5 for v in self.fresh_price_tolerance.values()):
            raise ValueError("fresh_price_tolerance: fractions between 0 and 0.5 (0.005 is 0.5 %)")
        if self.fresh_index_proxies is not None:
            self.fresh_index_proxies = {str(k): str(v) for k, v in dict(self.fresh_index_proxies).items() if v}
        self.fresh_index_proxy_approximate = [str(x) for x in (self.fresh_index_proxy_approximate or [])]
        self.account_currency = (self.account_currency or "").upper()
        self.capital_currency = (self.capital_currency or "").upper()
        self.base_currency = (self.base_currency or "").upper()
        self.fx_rates = dict(self.fx_rates or {})
        self.fx_policy = str(self.fx_policy or "unhedged").lower()
        self.fx_hedge_rebalance = str(self.fx_hedge_rebalance or "monthly").lower()
        if self.fx_policy == "hedged" and self.base_currency in ("", "USD"):
            raise ValueError("fx_policy hedged needs a base_currency other than USD (the books' currency)")
        self.currency_policy()                          # checked here, used by the runner
        for name in ("account_currency", "capital_currency", "base_currency"):
            v = getattr(self, name)
            if v and not (len(v) == 3 and v.isalpha()):
                raise ValueError(f"{name} is a currency code (EUR, USD, GBP ...), not {v!r}")

    def currency_policy(self):
        """The ``fxpolicy.FxPolicy`` this meta-portfolio declares (USD and unhedged by
        default: inactive)."""
        from .fxpolicy import FxPolicy

        return FxPolicy(base_currency=self.base_currency or "USD", policy=self.fx_policy,
                        rebalance=self.fx_hedge_rebalance, half_spread=self.fx_hedge_half_spread,
                        rates=self.fx_rates, min_notional=self.min_order_notional)

    @property
    def weights(self) -> dict[str, float]:
        return {s.slug: s.weight for s in self.sleeves}

    @property
    def index_proxies(self) -> dict[str, str]:
        """``{index: ETF}`` whose returns continue an index behind (``fresh_index_proxies``, by
        default the ETF substitutes)."""
        if self.fresh_index_proxies is not None:
            return dict(self.fresh_index_proxies)
        return {i: s.symbol for i, s in self.substitutes.items() if s.kind == "etf"}

    def fresh_config(self) -> dict:
        """What the strategies' workers need to complete their prices (``pwb_worker.py``)."""
        return {"strict": self.stale_symbol_policy == "block", "tolerance": dict(self.fresh_price_tolerance),
                "proxies": self.index_proxies, "approximate": list(self.fresh_index_proxy_approximate),
                "sources": [x for x in self.fresh_prices.split(",") if x] or ["alphavantage"],
                "grace": dict(self.fresh_close_grace)}

    def is_index(self, symbol: str) -> bool:
        return is_index(symbol, self.indices)

    def refusal(self, symbol: str) -> str | None:
        """Why ``symbol`` is never traded, or None."""
        if symbol in self.exclude_symbols:
            return "excluded (exclude_symbols)"
        if self.is_index(symbol):
            return "an index is not tradable, and it has no substitute (substitutes)"
        return fx_refusal(symbol)

    def tradable(self, symbol: str) -> bool:
        return self.refusal(symbol) is None

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


__all__ = ["OUT_OF_UNIVERSE_POLICIES", "REBALANCE", "SLEEVE_ERROR_POLICIES", "STALE_POLICIES", "MetaPortfolio",
           "Sleeve"]
