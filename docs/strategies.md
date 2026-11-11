# Strategies

A strategy is a subclass of `tw.Strategy`. The engine builds it once (its `__init__`), then
calls one of its hooks on every slice.

```python
class MyStrategy(tw.Strategy):
    params = (("lookback", 60), ("top", 10))

    def __init__(self):
        ...                     # build indicators, initialise state

    def next(self):
        ...                     # decide, place orders
```

## Parameters

`params` is a tuple of `(name, default)` pairs or a dict. Values given to
`engine.addstrategy(MyStrategy, lookback=120)` override the defaults; both are read as
`self.p.lookback` (or `self.params.lookback`). Parameters are set before `__init__` runs.

## Lifecycle

| hook | when |
|---|---|
| `__init__` | once, before the run; feeds, broker and parameters are already bound |
| `start()` | once, just before the first slice |
| `prenext()` | on each slice before every feed has a bar and every indicator is warm |
| `nextstart()` | once, on the first eligible slice; calls `next()` by default |
| `next()` | on every slice after that |
| `stop()` | once, after the last slice |
| `notify_order(order)` | when an order changes state |
| `notify_trade(trade)` | when a trade opens or closes |
| `notify_timer(timer, when, *args, **kwargs)` | when a timer fires |

Attributes whose names start with `_tw_` belong to the engine; any other name, `_private`
ones included, is free for the strategy.

## Data

| expression | meaning |
|---|---|
| `self.datas` | every feed, in the order they were added |
| `self.data`, `self.data0`, `self.data1`... | the first feed, then by position |
| `self.getdatabyname("SPY")`, `self.dnames.SPY` | a feed by the name given to `adddata` |
| `data._name` | that name |
| `data.close[0]`, `data.close[-k]` | this bar's close, `k` bars back |
| `len(data)` | bars that feed has delivered so far |
| `self.datetime.date(0)` | the date of the current slice |
| `len(self)` | slices run so far |

## Orders

| method | what it does |
|---|---|
| `order_target_percent(data, target)` | trade to a position worth `target` × portfolio value |
| `order_target_value(data, target)` | trade to a position worth `target` |
| `order_target_size(data, target)` | trade to a position of `target` units |
| `buy(data, size, ...)` / `sell(data, size, ...)` | a new order; market by default |
| `close(data)` | an order for the opposite of the current position |
| `cancel(order)` | cancel an accepted order that has not filled |

`data` defaults to `self.data`, and may be a feed or its name. Targets are converted to
whole units, rounding toward zero; the order is for the difference with the current
position, and nothing is placed when there is no difference.

`order_target_percent` reads the portfolio value marked at this slice's closes. When a
rebalance sells some names and buys others, the batch of orders is checked in the order
you placed them, against a running cash: placing the sells first lets them fund the buys.
See [Orders and the broker](orders-and-broker.md).

## Positions and the account

```python
pos = self.getposition(data)        # .size (signed), .price (average entry)
self.broker.getvalue()              # portfolio value at this slice's close
self.broker.getcash()               # cash
self.broker.get_value(datas=[data]) # the value of that position alone
self.broker.add_cash(-fee)          # queued; lands on the next slice (phase 7)
```

## Notifications

```python
def notify_order(self, order):
    if order.status == order.Completed:
        print(order.data._name, order.executed.size, order.executed.price, order.executed.comm)
    elif order.status in (order.Margin, order.Rejected, order.Canceled):
        print(order.data._name, order.getstatusname())

def notify_trade(self, trade):
    if trade.isclosed:
        print(trade.data._name, trade.pnl, trade.pnlcomm, trade.barlen)
```

An order goes `Submitted`, then `Accepted` and `Completed` (or `Margin` when the cash does
not cover it, `Canceled`, `Expired`, `Partial`). Each change is delivered once, with the
state the order had at that moment. All the order notifications of a slice are delivered
before its trade notifications. An order placed inside `notify_order` is first notified on
the next slice.

## Timers

```python
def __init__(self):
    self.add_timer(when=tw.Timer.SESSION_START, monthdays=[1], monthcarry=True)

def notify_timer(self, timer, when, *args, **kwargs):
    self.rebalance()
```

`monthdays=[1]` fires on the first day of the month; with `monthcarry=True` (the default) a
month whose 1st has no bar fires on its first bar instead. `weekdays=[1]` (Monday is 1)
and `weekcarry` work the same way per week. `SESSION_START` timers fire before `next()`,
`SESSION_END` timers after it. See
[`examples/03_timer_and_limit.py`](examples/03_timer_and_limit.py).

## Reproducibility

The engine is deterministic: the same data and the same strategy give the same values, to
the last bit. Two things in a strategy break that: drawing from `np.random` (or `random`)
without a seed, and iterating over a `set` of strings, whose order changes from one Python
process to the next unless `PYTHONHASHSEED` is fixed. Seed the first, sort the second.
