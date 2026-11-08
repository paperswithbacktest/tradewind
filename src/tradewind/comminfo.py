"""Commission schemes: the interface a broker asks for the cost of a fill.

``CommInfoBase`` is the class pwb-toolbox's ``SpreadImpactCommission`` already extends,
so its parameters and the ``_getcommission(size, price, pseudoexec)`` hook are the
contract. Semantics follow the public documentation of the scheme parameters:

* ``commtype`` ``COMM_PERC`` (percentage of the operation value) or ``COMM_FIXED``
  (per unit); when ``None`` it is percentage if ``margin`` is unset, fixed otherwise;
* ``percabs``: with ``COMM_PERC``, ``True`` reads ``commission=0.001`` as 0.1 %;
  ``False`` (the base default) reads ``commission=0.1`` as 0.1 % and divides by 100;
* ``stocklike``: with ``margin`` unset the asset is stock-like (value = size × price,
  integer sizes); with a margin it is futures-like (value = size × margin, P&L through
  ``mult``).
"""

from __future__ import annotations

from .params import ParamsBase


class CommInfoBase(ParamsBase):
    COMM_PERC, COMM_FIXED = 0, 1

    params = (
        ("commission", 0.0),
        ("mult", 1.0),
        ("margin", None),
        ("commtype", None),
        ("stocklike", False),
        ("percabs", False),
        ("interest", 0.0),
        ("interest_long", False),
        ("leverage", 1.0),
        ("automargin", False),
    )

    def __init__(self):
        commtype = self.p.commtype
        if commtype is None:
            if self.p.margin is None:
                self._commtype = self.COMM_PERC
                self._stocklike = True
            else:
                self._commtype = self.COMM_FIXED
                self._stocklike = False
        else:
            self._commtype = commtype
            self._stocklike = self.p.stocklike
        self._commission = self.p.commission
        if self._commtype == self.COMM_PERC and not self.p.percabs:
            self._commission /= 100.0
        self._creditrate = self.p.interest / 365.0

    # -- properties ---------------------------------------------------------------
    @property
    def stocklike(self):
        return self._stocklike

    @property
    def margin(self):
        return self.p.margin

    def get_margin(self, price):
        if not self.p.automargin:
            return self.p.margin
        if self.p.automargin < 0:
            return price * self.p.mult
        return price * self.p.automargin

    def get_leverage(self):
        return self.p.leverage

    # -- sizing and value ------------------------------------------------------------
    def getsize(self, price, cash):
        """How many units ``cash`` buys at ``price``; integer for stock-like assets."""
        if not self._stocklike:
            return int(self.p.leverage * (cash // self.get_margin(price)))
        return int(self.p.leverage * (cash // price))

    def getoperationcost(self, size, price):
        """Cash needed to open ``size`` at ``price``."""
        if not self._stocklike:
            return abs(size) * self.get_margin(price)
        return abs(size) * price

    def getvaluesize(self, size, price):
        """Marked value of ``size`` at ``price``."""
        if not self._stocklike:
            return abs(size) * self.get_margin(price)
        return size * price

    def getvalue(self, position, price):
        if not self._stocklike:
            return abs(position.size) * self.get_margin(price)
        # long or short, ``size * price``: observed on the oracle, a short's value read by
        # ``get_value(datas=[d])`` is its market value, not entry value + P&L, which rounds
        # differently (corpus ``high_dimensional_minimum_variance…`` 2025-05-26: a target of
        # ~0 on a 1 204-share short sized 1 204 on the oracle, 1 203 from entry + P&L)
        return position.size * price

    def profitandloss(self, size, price, newprice):
        return size * (newprice - price) * self.p.mult

    def cashadjust(self, size, price, newprice):
        if not self._stocklike:
            return size * (newprice - price) * self.p.mult
        return 0.0

    # -- commission ---------------------------------------------------------------------
    def _getcommission(self, size, price, pseudoexec):
        if self._commtype == self.COMM_PERC:
            return abs(size) * self._commission * price
        return abs(size) * self._commission

    def getcommission(self, size, price):
        return self._getcommission(size, price, pseudoexec=True)

    def confirmexec(self, size, price):
        return self._getcommission(size, price, pseudoexec=False)

    def get_credit_interest(self, data, pos, dt):
        return 0.0


class CommissionInfo(CommInfoBase):
    """The scheme ``broker.setcommission`` builds: ``commission=0.001`` means 0.1 %."""

    params = (("percabs", True),)
