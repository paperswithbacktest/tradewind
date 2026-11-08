"""A position: signed size and average entry price.

The broker's positions are also mirrored, row by row, in ``PortfolioState`` (the arrays the
mark of phase 8 multiplies): a position the broker owns carries its row and writes it on
every change. A clone (the batch pre-check's pseudo positions) carries none.
"""

from __future__ import annotations


class Position:
    __slots__ = ("_book", "_sid", "adjbase", "price", "size", "upclosed", "upopened")

    def __init__(self, size: float = 0, price: float = 0.0):
        # an int 0, as the oracle's: sizes stay ints through integer fills (probe
        # ``checksubmit_off`` prints a close of 500 shares as -500, not -500.0)
        self.size = size
        self.price = price if size else 0.0
        self.adjbase = None
        self.upopened = 0.0
        self.upclosed = 0.0
        self._book = None
        self._sid = -1

    def __bool__(self):
        return self.size != 0

    def __len__(self):
        return int(abs(self.size))

    def __repr__(self):
        return f"Position(size={self.size}, price={self.price})"

    def clone(self):
        return Position(self.size, self.price)

    def _attach(self, book, sid: int) -> None:
        """Mirror this position into row ``sid`` of ``book`` (a ``PortfolioState``)."""
        self._book = book
        self._sid = sid
        self._tw_sync()

    def _tw_sync(self) -> None:
        book = self._book
        if book is not None:
            book.quantity[self._sid] = self.size
            book.average_price[self._sid] = self.price

    def set(self, size: float, price: float) -> None:
        """Replace size and average price (corporate actions)."""
        self.size = size
        self.price = price
        self._tw_sync()

    def update(self, size: float, price: float):
        """Apply a fill of ``size`` at ``price``; return (size, price, opened, closed)."""
        oldsize = self.size
        newsize = oldsize + size
        if not oldsize:
            opened, closed = size, 0.0
            newprice = price
        elif (oldsize > 0) == (size > 0):
            # same direction: average in
            opened, closed = size, 0.0
            newprice = (oldsize * self.price + size * price) / newsize
        elif abs(size) <= abs(oldsize):
            # reducing or closing
            opened, closed = 0.0, size
            newprice = self.price if newsize else 0.0
        else:
            # reversing: close the old, open the rest at the new price
            closed, opened = -oldsize, newsize
            newprice = price
        self.size = newsize
        self.price = newprice
        self.upopened, self.upclosed = opened, closed
        if self._book is not None:
            self._tw_sync()
        return newsize, newprice, opened, closed
