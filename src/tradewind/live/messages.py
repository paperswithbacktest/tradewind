"""What the engine asks of a broker adapter, and what an adapter reports back (spec/07 § 3).

Every message is a small frozen record with plain fields (numbers, strings, tuples), so the
journal can write it as one JSON object and read it back identical: a session is replayed
from these records alone (spec/05 § 8, spec/07 § 6). Times are int64 nanoseconds since the
Unix epoch, UTC, as everywhere in the engine.

Adapter -> engine (put on the adapter's queue, applied by ``LiveBroker`` at the next slice):

* ``Ack``         the broker holds the order (Tradewind ``Accepted``);
* ``Execution``   one execution, signed quantity, price and the commission charged;
* ``Canceled``    the order is no longer working (``expired`` when its time in force ran out);
* ``Rejected``    the broker refused the order;
* ``Connection``  the link to the broker went down or came back;
* ``AccountSnapshot`` cash, positions, open orders and the executions of the session, as the
  broker sees them (the basis of reconciliation);
* ``Kill``        the kill switch, injected by the engine itself;
* ``Notice``      anything worth journalling that changes nothing (an informational code).

Engine -> adapter: ``OrderRequest`` (and a cancel by ``client_id``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields

#: generic order types; the adapter maps them to its venue's
ORDER_TYPES = ("MKT", "LMT", "STP", "STP LMT", "MOC")
#: generic times in force: "OPG" is "at the next open", what a backtest assumes for a market
#: order placed on a daily bar's close
TIME_IN_FORCE = ("DAY", "GTC", "GTD", "OPG")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    client_id: str                      # the engine's id, durable across restarts (IB: orderRef)
    symbol: str
    quantity: float                     # signed: > 0 buys, < 0 sells
    order_type: str = "MKT"
    limit_price: float | None = None
    stop_price: float | None = None
    tif: str = "DAY"
    good_till: int | None = None        # ns, with tif "GTD"


@dataclass(frozen=True, slots=True)
class Ack:
    client_id: str
    broker_id: str = ""
    time: int = 0


@dataclass(frozen=True, slots=True)
class Execution:
    client_id: str
    exec_id: str                        # unique per execution: replays and resyncs are deduplicated on it
    symbol: str
    quantity: float                     # signed
    price: float
    commission: float = 0.0
    time: int = 0


@dataclass(frozen=True, slots=True)
class Canceled:
    client_id: str
    reason: str = ""
    expired: bool = False
    time: int = 0


@dataclass(frozen=True, slots=True)
class Rejected:
    client_id: str
    reason: str = ""
    code: int = 0
    time: int = 0


@dataclass(frozen=True, slots=True)
class Connection:
    connected: bool
    message: str = ""
    code: int = 0
    time: int = 0


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """An order the broker lists as working."""

    client_id: str
    symbol: str
    quantity: float                     # signed, the whole order
    filled: float = 0.0                 # unsigned


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    cash: float | None                  # the account currency's cash balance
    net_liquidation: float | None = None
    currency: str = "USD"
    positions: tuple[tuple[str, float, float], ...] = ()     # (symbol, quantity, average cost)
    open_orders: tuple[BrokerOrder, ...] = ()
    executions: tuple[Execution, ...] = ()
    full_sync: bool = False             # open_orders is the complete list (after a (re)connect)
    time: int = 0

    def position_map(self) -> dict[str, tuple[float, float]]:
        return {sym: (float(q), float(avg)) for sym, q, avg in self.positions}


@dataclass(frozen=True, slots=True)
class Kill:
    reason: str
    time: int = 0


@dataclass(frozen=True, slots=True)
class Notice:
    message: str
    code: int = 0
    client_id: str = ""
    time: int = 0


MESSAGES = {cls.__name__: cls for cls in (OrderRequest, Ack, Execution, Canceled, Rejected, Connection,
                                          BrokerOrder, AccountSnapshot, Kill, Notice)}


def encode(message) -> dict:
    """A message as a JSON-ready dict, its type under ``"type"``."""
    out = {"type": type(message).__name__}
    out.update(asdict(message))
    return out


def decode(record: dict):
    """The message ``encode`` wrote (tuples restored, nested records rebuilt)."""
    record = dict(record)
    cls = MESSAGES[record.pop("type")]
    if cls is AccountSnapshot:
        record["positions"] = tuple(tuple(p) for p in record.get("positions", ()))
        record["open_orders"] = tuple(BrokerOrder(**o) for o in record.get("open_orders", ()))
        record["executions"] = tuple(Execution(**e) for e in record.get("executions", ()))
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in record.items() if k in names})


AdapterMessage = Ack | Execution | Canceled | Rejected | Connection | AccountSnapshot | Kill | Notice

__all__ = [
    "MESSAGES",
    "ORDER_TYPES",
    "TIME_IN_FORCE",
    "AccountSnapshot",
    "Ack",
    "AdapterMessage",
    "BrokerOrder",
    "Canceled",
    "Connection",
    "Execution",
    "Kill",
    "Notice",
    "OrderRequest",
    "Rejected",
    "decode",
    "encode",
]
