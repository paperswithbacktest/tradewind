"""The cash book: one balance per currency (spec/05 § 4.5, Lean ``CashBook`` / ``Cash``).

The oracle default is a single ``Cash("USD")`` at conversion rate 1.0: ``getcash()`` is its
amount and every fill and fee is in USD. Money is float64 (spec/05 § 10, Q3); a conversion
between equal currencies returns the amount untouched, so the single-currency path is
bit-identical to a plain float.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from .securities import ACCOUNT_CURRENCY, CashAmount, Currency, SecurityId


class Cash:
    __slots__ = ("amount", "conversion_rate", "conversion_source", "currency")

    def __init__(self, currency: Currency, amount: float = 0.0, conversion_rate: float = 1.0,
                 conversion_source: SecurityId | None = None):
        self.currency = currency
        self.amount = float(amount)
        self.conversion_rate = float(conversion_rate)   # value of 1 unit in the account currency
        self.conversion_source = conversion_source       # the forex security that prices it

    def __repr__(self):
        return f"Cash({self.currency!r}, amount={self.amount}, rate={self.conversion_rate})"


class CashBook:
    def __init__(self, account_currency: Currency = ACCOUNT_CURRENCY, amount: float = 0.0):
        self.account_currency = account_currency
        self.account = Cash(account_currency, amount, 1.0)
        self._cash: dict[Currency, Cash] = {account_currency: self.account}
        self._unsettled: list[Callable[[], CashAmount]] = []

    def __getitem__(self, currency: Currency) -> Cash:
        return self._cash[currency]

    def __contains__(self, currency: Currency) -> bool:
        return currency in self._cash

    def __iter__(self) -> Iterator[Cash]:
        return iter(self._cash.values())

    def __len__(self) -> int:
        return len(self._cash)

    def ensure(self, currency: Currency, conversion_rate: float = float("nan"),
               conversion_source: SecurityId | None = None) -> Cash:
        """The balance of ``currency``, created at zero if it has none yet. A foreign
        balance has no conversion rate (NaN) until its source prices it."""
        cash = self._cash.get(currency)
        if cash is None:
            cash = self._cash[currency] = Cash(currency, 0.0, conversion_rate, conversion_source)
        return cash

    def add(self, currency: Currency, amount: float) -> None:
        cash = self._cash.get(currency)
        if cash is None:
            cash = self.ensure(currency)
        cash.amount += amount

    def convert(self, amount: float, src: Currency, dst: Currency) -> float:
        if src == dst:
            return amount
        return amount * self._cash[src].conversion_rate / self._cash[dst].conversion_rate

    def total_in_account_currency(self) -> float:
        """Sum of every balance in the account currency, in insertion order (account
        currency first, so a single-currency book returns its amount exactly)."""
        total = 0.0
        for cash in self._cash.values():
            total += cash.amount if cash is self.account else cash.amount * cash.conversion_rate
        return total

    def register_unsettled(self, source: Callable[[], CashAmount]) -> None:
        """A settlement model's ``unsettled`` callable, summed by ``unsettled()``."""
        if source not in self._unsettled:
            self._unsettled.append(source)

    def unsettled(self) -> CashAmount:
        total = 0.0
        for source in self._unsettled:
            amount = source()
            total += self.convert(amount.amount, amount.currency, self.account_currency)
        return CashAmount(total, self.account_currency)

    def __repr__(self):
        return f"CashBook({list(self._cash.values())!r})"
