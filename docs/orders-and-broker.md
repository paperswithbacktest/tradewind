# Orders and the broker

## Order types

| `exectype` | fills at |
|---|---|
| `tw.Order.Market` (default) | the open of the next bar; with `set_coc(True)`, the close of the bar it was placed on |
| `tw.Order.Close` | the close of the next bar |
| `tw.Order.Limit` | the limit price on the first bar that trades through it; the open if the bar opens beyond it |
| `tw.Order.Stop` | the stop price on the first bar that touches it; the open if the bar gaps through it |
| `tw.Order.StopLimit` | a limit order activated once the stop price is touched |

```python
self.buy(data, size=100)                                            # market
self.sell(data, size=100, exectype=tw.Order.Limit, price=105.0)     # limit
self.buy(data, size=100, exectype=tw.Order.Stop, price=98.0)        # stop
```

An order fills in full or not at all: no partial fills, no cap on the share of the bar's
volume. `StopTrail`, `StopTrailLimit`, brackets, OCO groups and order expiry (`valid`) are
not available yet.

## Order states

```
Created → Submitted → Accepted → Completed
                   ↘ Margin          ↘ Canceled / Expired / Margin
```

| state | meaning |
|---|---|
| `Submitted` | placed during `next()` |
| `Accepted` | passed the cash check at the start of the next slice |
| `Completed` | filled; `order.executed.size`, `.price`, `.value`, `.comm` |
| `Margin` | the cash did not cover it, at the batch check or at the fill |
| `Partial` | a reversing order (long to short or back) filled its closing part only; it then ends `Margin` |
| `Canceled` | cancelled after acceptance |

`cancel()` acts only on an `Accepted` order that has not filled. An order still waiting for
its batch check cannot be cancelled: it goes on to be accepted and filled. This matches
backtrader and is listed among the behaviours to correct in a later version.

## The cash check

The orders a strategy places during one bar are checked **as a batch**, at the start of the
next bar, **in the order they were placed**, against a running cash:

- each order adds its cash flow to the running cash: a sell adds its proceeds, a buy
  subtracts its cost plus commission, priced at the close of the bar it was placed on (at
  its own price for a limit or stop);
- an order is `Accepted` if the running cash is still zero or more after it, `Margin`
  otherwise;
- a refused order's cost still counts in the running cash, so a refusal early in the
  batch can refuse the orders after it.

The accepted orders are then checked again at their fill price. An order accepted at the
batch check can still end `Margin` when the bar gaps.

Two practical rules follow:

1. **Place the sells before the buys** in a rebalance, so their proceeds count.
2. **Keep a cash buffer** (target 95 to 99 % rather than 100 %). Orders are sized at
   today's close and filled at tomorrow's open; a buy target of exactly 100 % is refused
   whenever the price rises overnight.

`cerebro.broker.set_checksubmit(False)` skips the batch check: orders are accepted at once
and checked only at the fill.

## Cash and value

```python
cerebro.broker.setcash(100_000)        # before the run
self.broker.getcash()
self.broker.getvalue()                 # cash + positions at this slice's closes
self.broker.add_cash(amount)           # deposit (or withdrawal if negative), lands next slice
```

## Commissions

```python
cerebro.broker.setcommission(commission=0.001)                 # 0.1 % of the traded value
cerebro.broker.setcommission(commission=0.005, commtype=tw.CommInfoBase.COMM_FIXED,
                             stocklike=True)                   # 0.5 cent per share
cerebro.broker.setcommission(commission=0.001, name="SPY")    # for one feed only
```

When `commtype` is given, give `stocklike=True` too for shares: without it the instrument is
treated as a future (valued through a margin, which is not set), as in backtrader.

For any other rule, subclass `tw.CommInfoBase` and override `_getcommission(size, price,
pseudoexec)`; register it with `cerebro.broker.addcommissioninfo(scheme, name=None)`.

```python
class SpreadCost(tw.CommInfoBase):
    params = (("half_spread_bps", 5.0), ("stocklike", True), ("commtype", tw.CommInfoBase.COMM_PERC))

    def _getcommission(self, size, price, pseudoexec):
        return abs(size) * price * self.p.half_spread_bps / 10_000
```

## Leverage

A scheme's `leverage` applies to long positions in stock-like schemes: buying pays
`size × price / leverage`, and the position counts as its margin plus its open P&L in the
portfolio value. Short positions always move the full price.

```python
cerebro.broker.setcommission(commission=0.0, leverage=2.0)
```

## Slippage

```python
cerebro.broker.set_slippage_perc(0.0005)     # 5 bp against the trader
cerebro.broker.set_slippage_fixed(0.01)      # one cent a share
```

As in backtrader, the default leaves a fill **at the open** unslipped. Market orders fill at
the open, so for slippage on them build the model with `slip_open=True` (next section).

## Reality models, per security

Every fill goes through a set of eight models, `tw.ModelSet`: fee, fill, slippage, buying
power, settlement, shortable, margin interest and corporate actions. The default set,
`ModelSet.oracle()`, reproduces backtrader exactly. Any model can be replaced for one
security without touching the others:

```python
from tradewind.models import CompatSlippage

thin = cerebro.adddata(tw.feeds.PandasData(dataname=frame), name="THIN")
cerebro.broker.set_models(thin, slippage=CompatSlippage(fixed=0.05, slip_open=True))
```

A model is a small class with one or two methods (for example a fee model has
`get_order_fee(params) -> CashAmount`); the protocols are in `tradewind.models` and in
[`spec/05-interfaces.md`](../spec/05-interfaces.md). A second set, `ModelSet.realistic()`
(fills capped by volume, T+1 settlement, borrow costs, a cash check without the
refused-order carry), is planned; it raises `NotImplementedError` today. See
[`examples/05_per_security_models.py`](examples/05_per_security_models.py).
