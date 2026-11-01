"""A position: signed size and average entry price."""

from __future__ import annotations


class Position:
    __slots__ = ("adjbase", "price", "size", "upclosed", "upopened")

    def __init__(self, size: float = 0.0, price: float = 0.0):
        self.size = size
        self.price = price if size else 0.0
        self.adjbase = None
        self.upopened = 0.0
        self.upclosed = 0.0

    def __bool__(self):
        return self.size != 0

    def __len__(self):
        return int(abs(self.size))

    def __repr__(self):
        return f"Position(size={self.size}, price={self.price})"

    def clone(self):
        return Position(self.size, self.price)

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
        return newsize, newprice, opened, closed
