"""``params = (("name", default), ...)`` resolved into ``self.p`` / ``self.params``.

Strategies, indicators, feeds, analyzers and commission schemes declare parameters this
way, and user subclasses write ``__init__(self)`` without arguments: the keyword
arguments given at construction are parameters, consumed **before** ``__init__`` runs.
``MetaParams.__call__`` does that split, so ``Scheme(half_spread=1e-4)`` works on a
class whose ``__init__`` takes nothing, as the corpus and pwb-toolbox expect.

Declarations are inherited and overridable: a subclass' tuple is merged over its bases'.
"""

from __future__ import annotations


class _Params:
    def __init__(self, values: dict):
        self.__dict__.update(values)

    def _getkwargs(self):
        return dict(self.__dict__)

    def _getitems(self):
        return list(self.__dict__.items())

    def _getkeys(self):
        return list(self.__dict__)

    def _getvalues(self):
        return list(self.__dict__.values())

    def __getitem__(self, name):
        return self.__dict__[name]

    def __contains__(self, name):
        return name in self.__dict__

    def __repr__(self):
        return f"Params({self.__dict__})"


def declared_params(cls) -> dict:
    """Merged ``params`` declarations along the MRO, bases first."""
    merged: dict = {}
    for klass in reversed(cls.__mro__):
        decl = klass.__dict__.get("params")
        if decl is None:
            continue
        if isinstance(decl, dict):
            items = decl.items()
        else:
            items = ((d[0], d[1] if len(d) > 1 else None) for d in decl)
        for name, default in items:
            merged[name] = default
    return merged


class MetaParams(type):
    """Split construction kwargs into parameters (set as ``p``) and the rest."""

    def __call__(cls, *args, **kwargs):
        values = declared_params(cls)
        for name in list(kwargs):
            if name in values:
                values[name] = kwargs.pop(name)
        obj = cls.__new__(cls)
        obj.p = obj.params = _Params(values)
        pre = getattr(cls, "_before_init", None)
        if pre is not None:
            pre(obj)
        obj.__init__(*args, **kwargs)
        post = getattr(cls, "_after_init", None)
        if post is not None:
            post(obj)
        return obj


class ParamsBase(metaclass=MetaParams):
    params: tuple = ()

    @classmethod
    def _params_declared(cls) -> dict:
        return declared_params(cls)
