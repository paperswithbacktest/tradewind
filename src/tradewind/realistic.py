"""Realistic reality models (spec/05 § 5, chantier 4), opt-in next to the ``Compat*`` ones.

Each class implements one protocol of ``tradewind.models`` -- the decomposition is QuantConnect
Lean's (``IFeeModel``, ``IFillModel``, ``ISlippageModel``, ``IBuyingPowerModel``,
``ISettlementModel``, ``IShortableProvider``, ``IMarginInterestRateModel``; Apache 2.0, read as a
specification, see ``NOTICE``); the code and the rules are Tradewind's own:

* ``CostTable``: a measured spread per symbol **and per year** (PWB's
  ``Stocks-Yearly-TransactionCostParams``: ``symbol``, ``year``, ``spread_bps``, ``lambda_bps``),
  nearest vintage when a year is missing, a default for an unknown symbol;
* ``SpreadFeeModel``: half the spread of the fill's year on the traded notional (the PWB
  harness's convention), plus an optional per-share commission with a minimum per fill and an
  optional linear-book impact;
* ``SpreadSlippage``: the same half-spread moved into the fill price instead;
* ``VolumeShareFillModel``: the oracle's fill prices, the quantity capped at a share of the
  bar's volume, the rest left open for the next bars (status ``Partial``);
* ``BorrowRates``: a ``ShortableProvider`` with an annual borrow rate (one for all, or per
  symbol) and an optional easy-to-borrow list or shortable quantities;
* ``MarginAccount`` / ``CashAccount``: Reg T buying power (50 % initial, 25 % maintenance,
  configurable), no carry of a refused order's cost, foreign securities valued in the account
  currency; ``MarginCallModel`` liquidates pro rata when equity falls below maintenance;
* ``DelayedSettlement`` / ``UsEquitySettlement``: sale proceeds available after T+n business
  days (US equities: T+3, T+2 from 2017-09-05, T+1 from 2024-05-28);
* ``FlatMarginInterest``: interest on debit (and optionally credit) balances.

``ModelSet.realistic(...)`` assembles them; ``broker.set_profile("realistic", ...)`` installs
the set as the broker's default, with the margin-call model. Nothing here runs unless asked:
the default profile stays ``ModelSet.oracle()`` (spec/05 § 10, Q1).
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np

from .models import BuyingPowerCheck, FillProposal, NextBarFillModel, order_fee, slipped
from .order import Order
from .securities import CashAmount

_MARKET = Order.Market
_NS_PER_DAY = 86_400_000_000_000
_NS_PER_YEAR = 365.0 * _NS_PER_DAY
_EPOCH = _dt.date(1970, 1, 1)

# one basis point each way: what PWB's harness charges when nothing else answers
DEFAULT_HALF_SPREAD = 1e-4


def _day(ns) -> _dt.date:
    """The UTC calendar date of a time in ns (daily bars are stamped at midnight UTC)."""
    return _EPOCH + _dt.timedelta(days=int(ns) // _NS_PER_DAY)


def _year(ns) -> int:
    return _day(ns).year


def _now(security, time) -> int:
    """The time of the slice: the one given, or the broker's when a caller passed none
    (the commission recorded on an order is priced without a time)."""
    if time:
        return int(time)
    broker = getattr(security, "_broker", None)
    return int(getattr(broker, "_now", 0) or 0)


def _fx(security) -> float:
    """Value of one unit of the security's quote currency in the account currency."""
    broker = security._broker
    ccy = security.properties.quote_currency
    book = broker.cashbook
    if ccy == book.account_currency:
        return 1.0
    return book[ccy].conversion_rate


# -- costs ------------------------------------------------------------------------------------


class CostTable:
    """A full bid-ask spread per (symbol, year), in basis points, and its lookups.

    ``spreads``: ``{(symbol, year): spread_bps}``; ``impact``: ``{(symbol, year): lambda_bps}``
    (Kyle's slope, basis points per million of notional), optional. A year the table does
    not hold takes the nearest year it holds for that symbol (``max_gap`` years at most;
    ``None``: any distance, as PWB's harness does); a symbol it does not hold, or a year too
    far, takes ``default_half_spread``. ``aliases`` maps a feed's name to the table's symbol;
    ``max_spread_bps`` caps a spread (PWB's table has a few rows above 10 000 bp, a spread
    wider than the price).

    Non-positive or non-finite spreads are dropped (absent, not free).
    """

    def __init__(self, spreads: Mapping | None = None, impact: Mapping | None = None,
                 default_half_spread: float = DEFAULT_HALF_SPREAD, max_gap: int | None = None,
                 aliases: Mapping[str, str] | None = None, max_spread_bps: float | None = None):
        self.default_half_spread = float(default_half_spread)
        self.max_gap = max_gap
        self.aliases = dict(aliases or {})
        by_symbol: dict[str, dict[int, float]] = {}
        for (symbol, year), bps in (spreads or {}).items():
            bps = float(bps)
            if math.isfinite(bps) and bps > 0:
                if max_spread_bps is not None:
                    bps = min(bps, float(max_spread_bps))
                by_symbol.setdefault(str(symbol), {})[int(year)] = bps / 2.0 / 1e4
        lam: dict[str, dict[int, float]] = {}
        for (symbol, year), bps in (impact or {}).items():
            bps = float(bps)
            if math.isfinite(bps) and bps > 0:
                lam.setdefault(str(symbol), {})[int(year)] = bps
        self._tw_half = by_symbol
        self._tw_lambda = lam
        self._tw_cache: dict = {}
        # what the run fingerprint records: the content, not a dump of the table
        rows = sorted((s, y, v) for s, ys in by_symbol.items() for y, v in ys.items())
        self.rows = len(rows)
        self.digest = _digest(rows)

    @classmethod
    def from_frame(cls, frame, symbol: str = "symbol", year: str = "year", spread: str = "spread_bps",
                   impact: str | None = "lambda_bps", **kwargs) -> CostTable:
        """From a DataFrame shaped like PWB's cost-parameters datasets."""
        spreads = dict(zip(zip(frame[symbol], frame[year]), frame[spread]))
        lam = None
        if impact is not None and impact in frame.columns:
            sub = frame[frame[impact].notna()]
            lam = dict(zip(zip(sub[symbol], sub[year]), sub[impact]))
        return cls(spreads, lam, **kwargs)

    @classmethod
    def flat(cls, spread_bps: float, **kwargs) -> CostTable:
        """One spread for every symbol and year."""
        return cls({}, default_half_spread=float(spread_bps) / 2.0 / 1e4, **kwargs)

    def _nearest(self, table: dict, symbol: str, year: int):
        years = table.get(symbol)
        if not years:
            return None, None
        if year in years:
            return years[year], year
        best = min(years, key=lambda y: (abs(y - year), y))
        if self.max_gap is not None and abs(best - year) > self.max_gap:
            return None, None
        return years[best], best

    def lookup(self, symbol: str, year: int) -> tuple[float, str]:
        """``(half_spread, source)``: ``measured`` (the year itself), ``nearest`` (another
        year of the symbol) or ``default``."""
        symbol = self.aliases.get(symbol, symbol)
        value, found = self._nearest(self._tw_half, symbol, int(year))
        if value is None:
            return self.default_half_spread, "default"
        return value, "measured" if found == int(year) else "nearest"

    def half_spread(self, symbol: str, year: int) -> float:
        """Half the spread, as a fraction of the price (cached per symbol and year)."""
        key = (symbol, year)
        value = self._tw_cache.get(key)
        if value is None:
            value = self._tw_cache[key] = self.lookup(symbol, year)[0]
        return value

    def lambda_bps(self, symbol: str, year: int) -> float:
        value, _ = self._nearest(self._tw_lambda, self.aliases.get(symbol, symbol), int(year))
        return 0.0 if value is None else value

    def __repr__(self):
        return f"CostTable({self.rows} rows, default half-spread {self.default_half_spread:g})"


def _digest(rows) -> str:
    import hashlib

    h = hashlib.sha256()
    for row in rows:
        h.update(repr(row).encode())
    return h.hexdigest()


class SpreadFeeModel:
    """Fee = |quantity| × price × multiplier × half-spread × ``spread_fraction`` (the
    fill's year, from the ``CostTable``) + max(``per_share`` × |quantity|, ``minimum``) +
    the optional impact ``impact_factor`` × lambda × notional / 1e6 × notional / 1e4.

    The fee is in the security's quote currency. The minimum applies to each fill (a
    reversal is two fills, a volume-capped order one per bar). ``impact_factor`` 0 (the
    default) leaves impact out: it depends on the capital, which a backtest often invents.
    """

    __slots__ = ("costs", "impact_factor", "minimum", "per_share", "spread_fraction")

    def __init__(self, costs: CostTable | None = None, spread_fraction: float = 1.0, per_share: float = 0.0,
                 minimum: float = 0.0, impact_factor: float = 0.0):
        self.costs = costs if costs is not None else CostTable()
        self.spread_fraction = float(spread_fraction)
        self.per_share = float(per_share)
        self.minimum = float(minimum)
        self.impact_factor = float(impact_factor)

    def fee(self, security, quantity: float, price: float, time) -> float:
        q = abs(quantity)
        if not q:
            return 0.0
        year = _year(_now(security, time))
        notional = q * abs(price) * security.properties.contract_multiplier
        cost = notional * self.costs.half_spread(security.symbol, year) * self.spread_fraction
        if self.per_share or self.minimum:
            cost += max(self.per_share * q, self.minimum)
        if self.impact_factor:
            lam = self.costs.lambda_bps(security.symbol, year)
            if lam:
                cost += notional * (self.impact_factor * lam * notional / 1e6) / 1e4
        return cost

    def get_order_fee(self, p) -> CashAmount:
        security = p.security
        return CashAmount(self.fee(security, p.fill_quantity, p.fill_price, p.time),
                          security.properties.quote_currency)


class SpreadSlippage:
    """The fill price moved against the order by half the spread of the year (from the
    ``CostTable``) times ``fraction``, at the open as anywhere else."""

    __slots__ = ("costs", "fraction")

    def __init__(self, costs: CostTable | None = None, fraction: float = 1.0):
        self.costs = costs if costs is not None else CostTable()
        self.fraction = float(fraction)

    def get_slippage(self, security, order, reference_price, at_open=False) -> float:
        year = _year(_now(security, 0))
        return abs(reference_price) * self.costs.half_spread(security.symbol, year) * self.fraction


# -- fills ------------------------------------------------------------------------------------


class VolumeShareFillModel:
    """The oracle's fill prices (``NextBarFillModel``: market at the open, limit and stop
    against the bar's range, ``Close`` at the close), with the quantity of each bar capped
    at ``participation`` × the bar's volume. The rest of the order stays open and fills on
    the next bars of its feed (``Partial`` until the last part, then ``Completed``); give
    the order a ``valid`` date to let the rest expire.

    A bar with no volume (zero) fills nothing; a feed without volume (NaN) is not capped.
    With ``whole_units`` the cap is rounded down to a whole share or contract.
    """

    __slots__ = ("coc", "participation", "whole_units")

    def __init__(self, participation: float = 0.1, coc: bool = False, whole_units: bool = True):
        if not participation > 0:
            raise ValueError("participation is a positive share of the bar's volume")
        self.participation = float(participation)
        self.coc = coc
        self.whole_units = whole_units

    @property
    def needs_placed_bar(self) -> bool:
        return self.coc

    def expired(self, order: Order, bar) -> bool:
        return order.valid is not None and bar.time > order.valid

    def fill(self, p) -> FillProposal | None:
        order = p.order
        if self.coc and order.exectype == _MARKET and p.placed_bar is not None:
            price = slipped(p.slippage, p.security, order, p.placed_bar.close, True)
        else:
            price = NextBarFillModel.fill_price(order, p.bar, p.slippage, p.security)
        if price is None:
            return None
        rest = order.executed.remsize
        if not rest:
            return None
        volume = p.bar.volume
        if math.isnan(volume):                    # no volume on this feed: not capped
            return FillProposal(rest, price)
        cap = self.participation * volume
        if self.whole_units:
            cap = math.floor(cap)
        if cap <= 0:
            return None
        if abs(rest) <= cap:
            return FillProposal(rest, price)
        return FillProposal(cap if rest > 0 else -cap, price)


# -- shorting ---------------------------------------------------------------------------------


class BorrowRates:
    """Borrow cost and availability of short sales.

    ``rate``: the annual fee on the short value for every symbol (0.25 %: general
    collateral); ``rates``: per-symbol overrides (hard-to-borrow names); ``easy_to_borrow``:
    if given, only these symbols can be shorted; ``shortable``: ``{symbol: quantity}``, the
    most that may be short. The fee accrues at each slice on the short value at the last
    close (phase 4), in the security's quote currency, and adds to the holding's
    ``total_borrow_fees``.
    """

    __slots__ = ("easy_to_borrow", "rate", "rates", "rebate", "shortable")

    def __init__(self, rate: float = 0.0025, rates: Mapping[str, float] | None = None,
                 easy_to_borrow: Iterable[str] | None = None, shortable: Mapping[str, float] | None = None,
                 rebate: float = 0.0):
        self.rate = float(rate)
        self.rates = dict(rates or {})
        self.easy_to_borrow = None if easy_to_borrow is None else frozenset(easy_to_borrow)
        self.shortable = dict(shortable or {})
        self.rebate = float(rebate)

    def shortable_quantity(self, symbol, time) -> float | None:
        if self.easy_to_borrow is not None and symbol not in self.easy_to_borrow:
            return 0.0
        return self.shortable.get(symbol)

    def fee_rate(self, symbol, time) -> float:
        return self.rates.get(symbol, self.rate) - self.rebate

    def rebate_rate(self, symbol, time) -> float:
        return self.rebate


# -- buying power -----------------------------------------------------------------------------


def _book(broker):
    """``(cash, unsettled, ids, values)``: the cash of every currency and the unsettled
    proceeds in the account currency, and the signed value in the account currency of every
    open position, at this slice's open for a security with a bar now (the price a fill
    knows) and at its last close otherwise."""
    book = broker.cashbook
    cash = book.total_in_account_currency()
    unsettled = book.unsettled().amount
    pf = broker.portfolio
    q = pf.quantity[:pf.n]
    ids = np.flatnonzero(q)
    market = broker.market
    if not ids.size or market is None:
        return cash, unsettled, ids, np.zeros(0)
    price = market.gather("close", ids)
    has = market.has_bar[ids]
    if has.any():
        price = np.where(has, market.gather("open", ids), price)
    values = q[ids] * price * pf.multiplier[ids]
    if broker._foreign:
        values = values * broker.fx_rates(ids)
    return cash, unsettled, ids, values


class MarginAccount:
    """Reg T buying power (Lean's ``SecurityMarginModel`` in spirit): an order may add
    initial margin only while ``equity - initial margin of the book`` stays non-negative.

    * equity = cash of every currency + unsettled proceeds + the positions, valued in the
      account currency at this slice's open (last close for a security without a bar);
    * initial margin = ``initial`` × |long value| + ``short_initial`` × |short value|
      (50 % each by default); maintenance likewise with ``maintenance`` (25 %);
    * the batch of a slice is checked in submission order against the margin left; an
      order that reduces a position always passes; a refused order consumes nothing (no
      carry, the corrected default of ``02-semantics``); fees count against the margin;
    * re-checked at the fill price: the closing part of a reversal always fills, an opening
      part only if the margin left covers it (else ``Partial`` then ``Margin``);
    * a short sale beyond the ``ShortableProvider``'s quantity is refused (``Margin``);
    * ``settled_cash_only``: unsettled proceeds do not count toward buying power (a cash
      account's good-faith rule); ``allow_short``: false refuses every opening short.

    ``order_target_*`` sizes the value gap in the account currency, toward zero, in lots.
    """

    __slots__ = (
        "_tw_fill",
        "allow_short",
        "checksubmit",
        "initial",
        "maintenance",
        "settled_cash_only",
        "short_initial",
        "short_maintenance",
        "shortcash",
    )

    def __init__(self, initial: float = 0.5, maintenance: float = 0.25, short_initial: float | None = None,
                 short_maintenance: float | None = None, settled_cash_only: bool = False,
                 allow_short: bool = True):
        if not 0 < initial <= 1 or not 0 <= maintenance <= 1:
            raise ValueError("margins are fractions of the position's value: 0 < initial <= 1, 0 <= maintenance <= 1")
        self.initial = float(initial)
        self.maintenance = float(maintenance)
        self.short_initial = float(initial if short_initial is None else short_initial)
        self.short_maintenance = float(maintenance if short_maintenance is None else short_maintenance)
        self.settled_cash_only = settled_cash_only
        self.allow_short = allow_short
        self.checksubmit = True
        self.shortcash = True
        self._tw_fill = None

    # -- margin arithmetic ----------------------------------------------------------------
    def _im(self, quantity, unit) -> float:
        """Initial margin of ``quantity`` units worth ``unit`` each (account currency)."""
        rate = self.initial if quantity > 0 else self.short_initial
        return rate * abs(quantity) * unit

    def _remaining(self, broker) -> float:
        cash, unsettled, _ids, values = _book(broker)
        equity = cash + unsettled + float(values.sum())
        used = float(self.initial * values[values > 0].sum() - self.short_initial * values[values < 0].sum())
        left = equity - used
        if self.settled_cash_only:
            left -= unsettled
        return left

    def _opening_refusal(self, security, new, size, time) -> str:
        if new >= 0 or size >= 0:
            return ""
        if not self.allow_short:
            return "short sales are not allowed in this account"
        limit = security.models.shortable.shortable_quantity(security.symbol, time)
        if limit is not None and -new > limit:
            return f"not shortable beyond {limit:g}"
        return ""

    # -- protocol -------------------------------------------------------------------------
    def check_order(self, security, order, check_price, state):
        broker = security._broker
        sid = security.id
        pseudo = state.pseudo_positions
        pos = pseudo.get(sid)
        if pos is None:
            pos = pseudo[sid] = broker.getposition(security.feed).size
        size = order.size
        new = pos + size
        reason = self._opening_refusal(security, new, size, state.time)
        if reason:
            return _check(False, 0.0, reason)
        if check_price is None:
            pseudo[sid] = new
            return _check(True, 0.0)
        scratch = state.scratch
        if scratch is None:
            scratch = state.scratch = {}
        left = scratch.get(id(self))
        if left is None:
            left = self._remaining(broker)
        unit = check_price * security.properties.contract_multiplier * _fx(security)
        fee = order_fee(security.models.fee, security, order, size, check_price, state.time) * _fx(security)
        need = self._im(new, unit) - self._im(pos, unit) + fee
        if order._check and self.checksubmit and need > 0 and left - need < 0:
            scratch[id(self)] = left
            return _check(False, 0.0, "initial margin exceeds the buying power left")
        scratch[id(self)] = left - need
        pseudo[sid] = new
        return _check(True, size * unit + fee)

    def check_fill(self, security, order, fill, cash, time=0):
        broker = security._broker
        size, price = fill.quantity, fill.price
        pos = broker.getposition(security.feed).size
        new = pos + size
        unit = price * security.properties.contract_multiplier * _fx(security)
        fee = order_fee(security.models.fee, security, order, size, price, time) * _fx(security)
        need = self._im(new, unit) - self._im(pos, unit) + fee
        left = self._left_at_fill(broker)
        reducing = pos and (pos > 0) == (size < 0) and abs(size) <= abs(pos)
        if reducing or left - need >= 0:
            # a reducing fill always passes, and the margin it frees funds the fills after
            # it in the same slice (the sells of a rebalance pay for its buys)
            self._spend(broker, need)
            return fill
        if pos and (pos > 0) != (size > 0):
            # a reversal: the closing part always fills, the opening part is refused
            closing_fee = order_fee(security.models.fee, security, order, -pos, price, time) * _fx(security)
            self._spend(broker, closing_fee - self._im(pos, unit))
            return FillProposal(-pos, price)
        return None

    def _left_at_fill(self, broker) -> float:
        cache = self._tw_fill
        if cache is None or cache[0] is not broker or cache[1] != broker._now:
            cache = self._tw_fill = [broker, broker._now, self._remaining(broker)]
        return cache[2]

    def _spend(self, broker, amount) -> None:
        cache = self._tw_fill
        if cache is not None and cache[0] is broker and cache[1] == broker._now:
            cache[2] -= amount

    def quantity_for_target_value(self, security, target_value, current_value, price) -> float:
        props = security.properties
        unit = price * props.contract_multiplier * _fx(security)
        if not unit or target_value == current_value:
            return 0
        size = (target_value - current_value) / unit
        lot = props.lot_size or 1.0
        if security.comminfo is not None and not security.comminfo.stocklike:
            return size
        lots = math.trunc(size / lot)
        return lots * lot if lot != 1.0 else lots

    def leverage(self, security) -> float:
        return 1.0 / self.initial

    def initial_margin(self, security, quantity, price) -> float:
        return self._im(quantity, price * security.properties.contract_multiplier)

    def maintenance_margin(self, security, quantity, price) -> float:
        rate = self.maintenance if quantity > 0 else self.short_maintenance
        return rate * abs(quantity) * price * security.properties.contract_multiplier


def _check(accepted, consumed, reason=""):
    return BuyingPowerCheck(accepted, consumed, reason)


class CashAccount(MarginAccount):
    """A cash account: purchases paid in full from settled cash (unsettled proceeds do not
    count), no short sales, no refused-order carry."""

    __slots__ = ()

    def __init__(self, settled_cash_only: bool = True):
        super().__init__(initial=1.0, maintenance=1.0, settled_cash_only=settled_cash_only, allow_short=False)


class MarginCallModel:
    """Phase 9: when equity falls below the maintenance margin of the book (each security's
    ``BuyingPowerModel.maintenance_margin``, in the account currency, at the last close),
    every position is cut pro rata so that the requirement falls back to the equity, by
    market orders flagged ``info["margin_call"]`` (accepted from phase 5 of the next slice,
    filled at its open). No new call while one is still open."""

    def __init__(self):
        self._tw_open: list = []

    def check(self, broker, s) -> None:
        if any(o.alive() for o in self._tw_open):
            return
        self._tw_open = []
        pf = broker.portfolio
        q = pf.quantity[:pf.n]
        ids = np.flatnonzero(q)
        if not ids.size or broker.market is None:
            return
        equity = broker._value
        price = broker.market.gather("close", ids)
        rates = broker.fx_rates(ids) if broker._foreign else np.ones(len(ids))
        required = 0.0
        for k, sid in enumerate(ids.tolist()):
            security = broker.securities[sid]
            required += security.models.buying_power.maintenance_margin(security, float(q[sid]),
                                                                        float(price[k])) * float(rates[k])
        if required <= 0 or equity >= required:
            return
        keep = max(0.0, equity / required)
        for sid in ids.tolist():
            security = broker.securities[sid]
            qty = float(q[sid])
            cut = math.ceil(abs(qty) * (1.0 - keep)) if float(qty).is_integer() else abs(qty) * (1.0 - keep)
            cut = min(cut, abs(qty))
            if not cut:
                continue
            side = broker.sell if qty > 0 else broker.buy
            order = side(None, security.feed, cut, margin_call=True)
            self._tw_open.append(order)


# -- settlement -------------------------------------------------------------------------------


class DelayedSettlement:
    """Proceeds (a positive cash flow: a sale, a dividend) become available ``days``
    business days after the trade date; payments (a purchase, a fee) leave at once. Business
    days skip weekends and, with ``calendar`` (an ``ExchangeCalendar``), its holidays.

    The money waiting is ``unsettled()`` (in the account currency): it counts in the
    portfolio value, not in ``getcash()``.
    """

    def __init__(self, days: int = 1, calendar=None):
        self.days = int(days)
        self.calendar = calendar
        self._tw_pending: list = []          # [settle_ns, currency, amount], in time order
        self._tw_book = None

    def days_for(self, day: _dt.date) -> int:
        return self.days

    def settle_date(self, day: _dt.date) -> _dt.date:
        n = self.days_for(day)
        while n > 0:
            day = day + _dt.timedelta(days=1)
            if day.weekday() >= 5:
                continue
            if self.calendar is not None and not self.calendar.is_trading_day(day):
                continue
            n -= 1
        return day

    def apply_funds(self, security, amount, time, cashbook) -> None:
        self._tw_book = cashbook
        value = amount.amount
        if value <= 0 or self.days_for(_day(time)) <= 0:
            cashbook.add(amount.currency, value)
            return
        settle = self.settle_date(_day(time))
        when = (settle - _EPOCH).days * _NS_PER_DAY
        pending = self._tw_pending
        pending.append([when, amount.currency, value])
        if len(pending) > 1 and pending[-2][0] > when:
            pending.sort(key=lambda row: row[0])

    def scan(self, time, cashbook) -> None:
        self._tw_book = cashbook
        pending = self._tw_pending
        k = 0
        while k < len(pending) and pending[k][0] <= time:
            _, currency, value = pending[k]
            cashbook.add(currency, value)
            k += 1
        if k:
            del pending[:k]

    def unsettled(self) -> CashAmount:
        book = self._tw_book
        total = 0.0
        for _, currency, value in self._tw_pending:
            total += value if book is None else book.convert(value, currency, book.account_currency)
        return CashAmount(total, book.account_currency if book is not None else "USD")


class UsEquitySettlement(DelayedSettlement):
    """US equities: T+3 until 2017-09-04, T+2 from 2017-09-05, T+1 from 2024-05-28 (SEC
    rules 15c6-1 and its amendments), NYSE holidays skipped by default."""

    T2 = _dt.date(2017, 9, 5)
    T1 = _dt.date(2024, 5, 28)

    def __init__(self, calendar: Any = "NYSE"):
        if calendar == "NYSE":
            from .calendars import NYSE

            calendar = NYSE
        super().__init__(days=1, calendar=calendar)

    def days_for(self, day: _dt.date) -> int:
        if day >= self.T1:
            return 1
        if day >= self.T2:
            return 2
        return 3


# -- interest ---------------------------------------------------------------------------------


class FlatMarginInterest:
    """Interest on the cash balances, per year of elapsed time: ``debit_rate`` on every
    negative balance (borrowed cash), ``credit_rate`` paid on positive ones, both in the
    account currency. Charged at each slice for the time since the previous one."""

    __slots__ = ("_tw_prev", "credit_rate", "debit_rate")

    def __init__(self, debit_rate: float = 0.0, credit_rate: float = 0.0):
        self.debit_rate = float(debit_rate)
        self.credit_rate = float(credit_rate)
        self._tw_prev = None

    def accrue(self, cashbook, portfolio, time) -> float:
        prev, self._tw_prev = self._tw_prev, int(time)
        if prev is None or time <= prev:
            return 0.0
        years = float(int(time) - prev) / _NS_PER_YEAR
        charged = 0.0
        for cash in cashbook:
            value = cash.amount if cash is cashbook.account else cash.amount * cash.conversion_rate
            if value < 0:
                charged += -value * self.debit_rate * years
            elif value > 0:
                charged -= value * self.credit_rate * years
        return charged


# -- the profile ------------------------------------------------------------------------------


def realistic_models(model_set_cls, costs: CostTable | None = None, *, spread_as: str = "fee",
                     spread_fraction: float = 1.0, per_share: float = 0.0, minimum: float = 0.0,
                     impact_factor: float = 0.0, participation: float | None = 0.1,
                     account: str = "margin", initial_margin: float = 0.5, maintenance_margin: float = 0.25,
                     borrow: BorrowRates | float | None = 0.0025, settlement: Any = "us-equity",
                     debit_rate: float = 0.0, credit_rate: float = 0.0, coc: bool = False):
    from .models import (
        AlwaysShortable,
        CompatSlippage,
        ImmediateSettlement,
        NextBarFillModel,
        NoInterest,
        OracleCorporateActions,
    )

    costs = costs if costs is not None else CostTable()
    if spread_as == "fee":
        fee = SpreadFeeModel(costs, spread_fraction, per_share, minimum, impact_factor)
        slippage = CompatSlippage()
    elif spread_as == "slippage":
        fee = SpreadFeeModel(costs, 0.0, per_share, minimum, impact_factor)
        slippage = SpreadSlippage(costs, spread_fraction)
    else:
        raise ValueError("spread_as is 'fee' or 'slippage'")
    fill = NextBarFillModel(coc=coc) if participation is None else VolumeShareFillModel(participation, coc=coc)
    if account == "margin":
        buying_power = MarginAccount(initial_margin, maintenance_margin)
    elif account == "cash":
        buying_power = CashAccount()
    else:
        raise ValueError("account is 'margin' or 'cash'")
    if borrow is None:
        shortable = AlwaysShortable()
    elif isinstance(borrow, (int, float)):
        shortable = BorrowRates(float(borrow))
    else:
        shortable = borrow
    if settlement is None or settlement == 0:
        settle = ImmediateSettlement()
    elif settlement == "us-equity":
        settle = UsEquitySettlement()
    elif isinstance(settlement, int):
        settle = DelayedSettlement(settlement)
    else:
        settle = settlement
    interest = FlatMarginInterest(debit_rate, credit_rate) if (debit_rate or credit_rate) else NoInterest()
    return model_set_cls(fee=fee, fill=fill, slippage=slippage, buying_power=buying_power, settlement=settle,
                         shortable=shortable, margin_interest=interest,
                         corporate_actions=OracleCorporateActions())


__all__ = [
    "DEFAULT_HALF_SPREAD", "BorrowRates", "CashAccount", "CostTable", "DelayedSettlement", "FlatMarginInterest",
    "MarginAccount", "MarginCallModel", "SpreadFeeModel", "SpreadSlippage", "UsEquitySettlement",
    "VolumeShareFillModel", "realistic_models",
]
