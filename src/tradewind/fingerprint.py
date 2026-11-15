"""The reproducibility fingerprint of a run: what went in, hashed.

After ``Engine.run()``, ``engine.fingerprint`` is a ``RunFingerprint``: the engine (version
and a hash of its own source), the environment (Python, numpy, pandas, platform, hash
seed), the data read through the feeds (a content hash per feed, its first and last
timestamps, and any declared provenance such as a dataset revision), each strategy (a hash
of the source of every module its class comes from, its parameters), the broker's
configuration (cash, commission schemes, models, flags), the clock, and the random state
(the engine's ``seed``, and the state of Python's and numpy's global generators at the
start when the run drew from them).

``digest`` is the SHA-256 of all of it in canonical JSON. Two runs with the same digest
read the same inputs, so they produce the same NAV (``tests/test_fingerprint.py``); the
converse does not hold (a change of environment changes the digest without moving a
deterministic NAV). ``diff`` says which entries differ.

What it cannot see: data a strategy loads by itself (files, network) unless declared with
``Engine.add_provenance``; a generator the strategy creates without a seed
(``np.random.default_rng()``), and anything else outside the engine's inputs.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import inspect
import json
import math
import os
import platform
import random
import sys
import types
from dataclasses import dataclass, field
from typing import Any

import numpy as np

SCHEMA = 1
#: The sections of a fingerprint, in the order they are documented; ``digest`` hashes them.
_SECTIONS = ("schema", "engine", "environment", "data", "provenance", "strategies", "broker",
             "clock", "rng")
_MAX_DEPTH = 4


# -- hashing helpers ---------------------------------------------------------------------------
def _sha(*chunks: bytes) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return h.hexdigest()


def _array_digest(a: np.ndarray) -> str:
    a = np.ascontiguousarray(a)
    return _sha(str(a.dtype).encode(), str(a.shape).encode(), a.tobytes())


def _code_digest(code: types.CodeType) -> str:
    """Bytecode, constants and names of a code object, nested code included."""
    h = hashlib.sha256()

    def walk(co):
        h.update(co.co_code)
        h.update(repr(co.co_names).encode())
        for c in co.co_consts:
            if isinstance(c, types.CodeType):
                walk(c)
            else:
                h.update(repr(c).encode())

    walk(code)
    return h.hexdigest()


def _callable_entry(fn) -> dict:
    code = getattr(fn, "__code__", None)
    name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", type(fn).__name__)
    return {"callable": f"{getattr(fn, '__module__', '')}.{name}",
            "code": _code_digest(code) if code is not None else None}


def canonical(obj: Any, depth: int = 0, seen: set | None = None) -> Any:
    """A JSON-ready, deterministic description of ``obj`` (floats kept exact, arrays and
    frames hashed, objects described by type and state down to a few levels)."""
    if seen is None:
        seen = set()
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else repr(obj)
    if isinstance(obj, np.generic):
        return canonical(obj.item(), depth, seen)
    if isinstance(obj, np.ndarray):
        return {"ndarray": _array_digest(obj), "shape": list(obj.shape), "dtype": str(obj.dtype)}
    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, _dt.timedelta):
        return {"timedelta": obj.total_seconds()}
    if isinstance(obj, type):
        return {"type": f"{obj.__module__}.{obj.__qualname__}"}
    if isinstance(obj, (types.FunctionType, types.MethodType, types.BuiltinFunctionType)):
        return _callable_entry(obj)
    try:
        import pandas as pd

        if isinstance(obj, (pd.DataFrame, pd.Series, pd.Index)):
            hashed = pd.util.hash_pandas_object(obj, index=not isinstance(obj, pd.Index)).to_numpy()
            names = list(map(str, obj.columns)) if isinstance(obj, pd.DataFrame) else [str(obj.name)]
            return {"pandas": type(obj).__name__, "shape": list(obj.shape),
                    "columns": names, "content": _array_digest(hashed)}
    except ImportError:  # pragma: no cover
        pass
    if isinstance(obj, dict):
        return {str(k): canonical(v, depth + 1, seen) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [canonical(v, depth + 1, seen) for v in obj]
    if isinstance(obj, (set, frozenset)):
        items = [canonical(v, depth + 1, seen) for v in obj]
        return sorted(items, key=lambda x: json.dumps(x, sort_keys=True, default=str))
    # an object: its type and, a few levels down, its state
    entry: dict = {"type": f"{type(obj).__module__}.{type(obj).__qualname__}"}
    if id(obj) in seen or depth >= _MAX_DEPTH:
        return entry
    seen = seen | {id(obj)}
    params = getattr(obj, "p", None)
    if params is not None and hasattr(params, "_getkwargs"):
        entry["params"] = canonical(params._getkwargs(), depth + 1, seen)
    state = {}
    if hasattr(obj, "__dict__"):
        state.update(vars(obj))
    for klass in type(obj).__mro__:
        for name in getattr(klass, "__slots__", ()):
            if hasattr(obj, name):
                state.setdefault(name, getattr(obj, name))
    state = {k: v for k, v in state.items()
             if not k.startswith("_tw_") and k not in ("p", "params", "owner", "_broker", "broker")}
    if state:
        entry["state"] = canonical(state, depth + 1, seen)
    return entry


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


# -- sections -------------------------------------------------------------------------------------
def _engine_section() -> dict:
    from . import __version__

    here = os.path.dirname(os.path.abspath(__file__))
    h = hashlib.sha256()
    for root, dirs, files in os.walk(here):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(root, name)
                h.update(os.path.relpath(path, here).replace(os.sep, "/").encode())
                with open(path, "rb") as fh:
                    h.update(fh.read())
    return {"name": "tradewind", "version": __version__, "source_sha256": h.hexdigest()}


def _environment_section() -> dict:
    import pandas as pd

    seed = os.environ.get("PYTHONHASHSEED")
    if seed is None:
        seed = "random" if sys.flags.hash_randomization else "0"
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "numpy": np.__version__, "pandas": pd.__version__,
            "system": platform.system(), "machine": platform.machine(), "hash_seed": seed}


def _module_source_digest(module_name: str) -> tuple[str, str]:
    module = sys.modules.get(module_name)
    if module is not None:
        try:
            return _sha(inspect.getsource(module).encode()), "module-source"
        except (OSError, TypeError):
            pass
    return "", "unavailable"


def _class_digest(cls) -> str:
    """Bytecode of every function defined on the class (the fallback when the module's
    source cannot be read: a class defined in a notebook or by ``exec``)."""
    h = hashlib.sha256()
    for name, value in sorted(vars(cls).items()):
        fn = value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
        code = getattr(fn, "__code__", None)
        if code is not None:
            h.update(name.encode())
            h.update(_code_digest(code).encode())
        elif not name.startswith("__"):
            h.update(name.encode())
            h.update(_dump(canonical(value)).encode())
    return h.hexdigest()


def _package_version(module_name: str) -> str | None:
    top = module_name.split(".")[0]
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version(top)
        except PackageNotFoundError:
            return None
    except ImportError:  # pragma: no cover
        return None


def strategy_code(cls) -> list[dict]:
    """The code a strategy class runs, outside the engine: one entry per class of its MRO
    defined outside ``tradewind`` -- the source of its whole module (helpers included),
    or its bytecode when the source cannot be read."""
    out = []
    for klass in cls.__mro__:
        module = klass.__module__ or ""
        if module == "builtins" or module == "tradewind" or module.startswith("tradewind."):
            continue
        digest, kind = _module_source_digest(module)
        if not digest or module == "__main__":
            # ``__main__`` can be a notebook or stdin: the class' own bytecode is reliable
            digest, kind = _class_digest(klass), "class-bytecode"
        out.append({"class": f"{module}.{klass.__qualname__}", "kind": kind, "sha256": digest,
                    "package_version": _package_version(module) if module != "__main__" else None})
    return out


def feed_entry(data) -> dict:
    """One feed as the run read it: content hash of its timestamps and lines, extent and
    declared provenance."""
    dtnum = data._dtnum if data._dtnum is not None else np.empty(0)
    h = hashlib.sha256()
    h.update(_array_digest(np.asarray(dtnum, dtype=np.float64)).encode())
    arrays = getattr(data, "_arrays", {}) or {}
    for name in sorted(arrays):
        h.update(name.encode())
        h.update(_array_digest(np.asarray(arrays[name])).encode())
    from .dateutils import num2date

    entry = {
        "name": getattr(data, "_name", ""),
        "feed": f"{type(data).__module__}.{type(data).__qualname__}",
        "rows": len(dtnum),
        "first": num2date(float(dtnum[0])).isoformat() if len(dtnum) else None,
        "last": num2date(float(dtnum[-1])).isoformat() if len(dtnum) else None,
        "content_sha256": h.hexdigest(),
        "params": canonical({k: v for k, v in data.p._getkwargs().items() if k != "dataname"}),
    }
    provenance = getattr(data, "provenance", None)
    if provenance:
        entry["provenance"] = canonical(dict(provenance))
    normalization = getattr(data, "normalization", None)
    if normalization is not None:
        entry["normalization"] = normalization
    return entry


def provider_entry(provider) -> dict:
    """A corporate-action or universe provider: its class and, when it can say, the digest
    of its tables (``describe()``); several combined providers are listed in order."""
    out = {"class": f"{type(provider).__module__}.{type(provider).__qualname__}"}
    inner = getattr(provider, "providers", None)
    if inner is not None:
        out["providers"] = [provider_entry(p) for p in inner]
    describe = getattr(provider, "describe", None)
    if describe is not None:
        out.update(canonical(describe()))
    return out


def table_digest(*arrays) -> str:
    """SHA-256 of a few columns of a table (numbers by their bytes, text by its values)."""
    h = hashlib.sha256()
    for a in arrays:
        if a is None:
            h.update(b"none")
            continue
        a = np.asarray(a)
        if a.dtype.kind in "biufcmM":
            h.update(_array_digest(a).encode())
        else:
            h.update("\x1f".join(map(str, a.tolist())).encode())
    return h.hexdigest()


def rng_states() -> dict:
    """Digests of the global generators' states (Python's ``random``, numpy's legacy)."""
    kind, keys, pos, has_gauss, gauss = np.random.get_state()
    np_digest = _sha(kind.encode(), np.ascontiguousarray(keys).tobytes(),
                     repr((int(pos), int(has_gauss), float(gauss))).encode())
    py_digest = _sha(repr(random.getstate()).encode())
    return {"numpy": np_digest, "random": py_digest}


# -- the fingerprint ------------------------------------------------------------------------------
@dataclass(frozen=True)
class RunFingerprint:
    """What a run read, hashed (see the module's docstring). Compare with ``==`` (digest)
    or ``diff``; store with ``to_json`` / ``from_json``."""

    engine: dict
    environment: dict
    data: list
    provenance: dict
    strategies: list
    broker: dict
    clock: dict
    rng: dict
    schema: int = SCHEMA
    digest: str = field(default="")

    def __post_init__(self):
        if not self.digest:
            object.__setattr__(self, "digest", _sha(_dump(self._body()).encode()))

    def _body(self) -> dict:
        return {name: getattr(self, name) for name in _SECTIONS}

    @property
    def short(self) -> str:
        return self.digest[:12]

    def to_dict(self) -> dict:
        out = self._body()
        out["digest"] = self.digest
        return out

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, indent=indent, allow_nan=False)

    @classmethod
    def from_dict(cls, d: dict) -> RunFingerprint:
        body = {k: d[k] for k in _SECTIONS if k != "schema"}
        fp = cls(schema=d.get("schema", SCHEMA), **body)
        if d.get("digest") and d["digest"] != fp.digest:
            raise ValueError("fingerprint digest does not match its content")
        return fp

    @classmethod
    def from_json(cls, text: str) -> RunFingerprint:
        return cls.from_dict(json.loads(text))

    def verify(self) -> bool:
        """The digest is the hash of the content (nothing was edited by hand)."""
        return self.digest == _sha(_dump(self._body()).encode())

    def diff(self, other: RunFingerprint) -> list[str]:
        """The dotted paths whose values differ between two fingerprints."""
        out: list[str] = []

        def walk(a, b, path):
            if isinstance(a, dict) and isinstance(b, dict):
                for k in sorted(set(a) | set(b)):
                    walk(a.get(k, "<missing>"), b.get(k, "<missing>"), f"{path}.{k}" if path else k)
            elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
                for i, (x, y) in enumerate(zip(a, b)):
                    walk(x, y, f"{path}[{i}]")
            elif a != b:
                out.append(path)

        walk(self._body(), other._body(), "")
        return out

    def __eq__(self, other):
        return isinstance(other, RunFingerprint) and self.digest == other.digest

    def __hash__(self):
        return hash(self.digest)

    def __repr__(self):
        return f"RunFingerprint({self.short}, engine={self.engine.get('version')}, feeds={len(self.data)})"


class FingerprintRecorder:
    """Takes what must be read before the run (cash, configuration, parameters, the random
    state) and builds the fingerprint on demand after it (feed hashes cost a pass over the
    data, paid only when the fingerprint is read)."""

    def __init__(self, engine, seed):
        """Called once the engine seeded the generators, before the strategies are built
        (their ``__init__`` may draw from them)."""
        broker = engine.broker
        self._engine = engine
        self._datas = [sec.feed for sec in broker.securities]
        self._seed = seed
        self._rng_start = rng_states()
        self._strategies: list = []
        self._broker = self._broker_entry(engine, broker)
        spec = engine._clock_spec
        self._clock = spec.describe() if spec is not None and hasattr(spec, "describe") else (
            {"clock": "UnionClock", "hours": "AlwaysOpen"} if spec is None else canonical(spec))
        self._engine_params = canonical(engine.p._getkwargs())
        self._rng_end = None
        self._built: RunFingerprint | None = None

    def set_strategies(self, strategies) -> None:
        """The strategies as built (their parameters resolved)."""
        self._strategies = [self._strategy_entry(s, args, kwargs)
                            for s, (_cls, args, kwargs) in zip(strategies, self._engine.strats)]

    @staticmethod
    def _strategy_entry(strat, args, kwargs) -> dict:
        # a typed strategy runs inside a host built by the engine: record the user's class
        cls = getattr(type(strat), "_tw_fingerprint_class", type(strat))
        return {"class": f"{cls.__module__}.{cls.__qualname__}", "code": strategy_code(cls),
                "params": canonical(strat.p._getkwargs()),
                "args": canonical(list(args)),
                "kwargs": canonical({k: v for k, v in kwargs.items() if k not in strat.p._getkwargs()})}

    @staticmethod
    def _broker_entry(engine, broker) -> dict:
        comminfo = {str(k): canonical(v) for k, v in sorted(broker.comminfo.items(), key=lambda kv: str(kv[0]))}
        models = {"default": canonical(broker.models)}
        overrides = {}
        for sec in broker.securities:
            if sec.models is not broker.models:
                overrides[sec.symbol] = canonical(sec.models)
        if overrides:
            models["per_security"] = overrides
        hours = sorted({getattr(s.hours, "name", type(s.hours).__name__) for s in broker.securities})
        entry = {"class": f"{type(broker).__module__}.{type(broker).__qualname__}",
                 "starting_cash": float(broker.cash), "params": canonical(broker.p._getkwargs()),
                 "coo": bool(getattr(broker, "_coo", False)), "comminfo": comminfo, "models": models,
                 "hours": hours, "pending_cash": canonical(list(getattr(broker, "_cash_addition", [])))}
        # the providers, only when set (a run without them keeps its digest)
        actions = getattr(engine, "_actions_provider", None)
        if actions is not None:
            entry["corporate_actions"] = provider_entry(actions)
        universe = getattr(engine, "_universe_provider", None)
        if universe is not None:
            entry["universe"] = dict(provider_entry(universe), on_removal=engine._universe_removal,
                                     gate=engine._universe_gate)
        return entry

    def finish(self) -> None:
        """The run has ended: read the generators' states again."""
        self._rng_end = rng_states()

    def build(self) -> RunFingerprint:
        if self._built is not None:
            return self._built
        end = self._rng_end or rng_states()
        rng: dict = {"seed": self._seed}
        for name in ("numpy", "random"):
            used = end[name] != self._rng_start[name]
            entry: dict = {"global_state_used": used}
            if used:
                entry["state_at_start"] = self._rng_start[name]
            rng[name] = entry
        engine_entry = _engine_section()
        engine_entry["params"] = self._engine_params
        self._built = RunFingerprint(
            engine=engine_entry,
            environment=_environment_section(),
            data=[feed_entry(d) for d in self._datas],
            provenance=canonical(dict(self._engine._provenance)),
            strategies=self._strategies,
            broker=self._broker,
            clock=self._clock,
            rng=rng,
        )
        return self._built


__all__ = ["RunFingerprint", "canonical", "feed_entry", "rng_states", "strategy_code"]
