"""Frostwork × web-poet — author page objects whose selector fields are answered in **one**
streaming pass.

Each :func:`field` you declare becomes a real ``web_poet.field``, so everything web-poet gives you
still works — attribute access, ``async to_item()``, ``@handle_urls`` routing, ``Returns[Item]``,
and mixing in hand-written ``@web_poet.field`` methods for computed fields. The difference is that
*all* of a page object's Frostwork selectors share a **single cached** :func:`frostwork.extract`
call, instead of one lxml parse + one query per field. The document scan is shared; matching work
still grows with field count and selector complexity.

    from web_poet import handle_urls, Returns
    from frostwork.webpoet import FrostPage, field

    @handle_urls("example.com")
    class ProductPage(FrostPage):
        name   = field("h1::text")
        price  = field(".price::text")
        images = field("img::attr(src)", all=True)
        specs  = field(".spec ::text", join=" ")
        brand  = field("//meta[@itemprop='brand']/@content")   # XPath works too

    # scrapy-poet injects `response`; then:  item = await ProductPage(response=resp).to_item()

The page object is fed by web-poet's ``HttpResponse`` (its ``.body`` bytes are scanned with the
response's resolved ``.encoding``, matching what Parsel would decode).

Requires web-poet:  ``pip install frostwork[webpoet]``.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

try:
    from web_poet import WebPage, cached_method
    from web_poet import field as _wp_field
except ImportError as exc:  # pragma: no cover - exercised only without web-poet installed
    raise ImportError(
        "frostwork.webpoet requires web-poet; install it with `pip install frostwork[webpoet]`"
    ) from exc

from ._frostwork import Plan as _Plan
from .page import _shape  # single source of cardinality shaping (first/all/join + transforms)
from .page import SchemaReport, check  # schema audit / strict validation

__all__ = ["FrostPage", "field", "Many", "One"]

# ("first", None) | ("all", None) | ("join", separator)
_Card = Tuple[str, Optional[str]]
_Spec = Tuple[str, _Card, Tuple[Callable, ...]]  # (selector, card, transforms)


def _re_first(pattern):
    """Transform: on a scalar string, return the first regex match — group 1 if the pattern has
    capture groups, else the whole match; ``None`` if no value or no match."""
    rx = re.compile(pattern)

    def apply(value):
        if not isinstance(value, str):
            return None
        m = rx.search(value)
        if not m:
            return None
        return m.group(1) if m.groups() else m.group(0)

    return apply


class _FrostField:
    """Marker left in a class body by :func:`field`; :meth:`FrostPage.__init_subclass__` turns it
    into a real ``web_poet.field`` bound to the shared batched extract.

    :meth:`map` / :meth:`re_first` attach pure-Python transforms that run on the **shaped** value
    (after cardinality) — never in the scan — so declaring a transformed field stays a one-liner
    instead of a separate ``@web_poet.field`` method. They return a new marker (chainable)."""

    __slots__ = ("selector", "card", "transforms")

    def __init__(self, selector: str, card: _Card, transforms: Tuple[Callable, ...] = ()) -> None:
        self.selector = selector
        self.card = card
        self.transforms = transforms

    def map(self, fn: Callable) -> "_FrostField":
        """Apply ``fn`` to the field's shaped value (a str/``None`` for a plain field, a ``list`` for
        ``all=True``, a str for ``join=``). Chainable."""
        return _FrostField(self.selector, self.card, self.transforms + (fn,))

    def re_first(self, pattern: str) -> "_FrostField":
        """Return the first regex match of ``pattern`` over the field's (first) matched string —
        capture group 1 if present, else the whole match; ``None`` if nothing matches.

        Errors at declaration on an ``all=True`` field: ``re_first`` operates on a scalar string, but
        ``all=True`` yields a ``list`` (the old behavior silently returned ``None`` for every page — a
        field-always-empty foot-gun). Use ``join=`` (then ``re_first`` sees the joined string) or
        ``map()`` for a per-list transform instead. (``join=`` is fine — its shaped value is a str.)"""
        if self.card[0] == "all":
            raise ValueError(
                "re_first() applies to a scalar string, but this field is all=True (a list). "
                "Use join=... so re_first matches the joined string, or map() for a list transform."
            )
        return self.map(_re_first(pattern))


def field(selector: str, *, all: bool = False, join: Optional[str] = None) -> _FrostField:
    """Declare a Frostwork selector field on a :class:`FrostPage`.

    Default: the **first** match (or ``None``). ``all=True``: a **list** of every match, in document
    order. ``join=sep``: every match **joined** into one string with ``sep``. ``all`` and ``join``
    are mutually exclusive. ``selector`` is any Frostwork-supported CSS or downward XPath query.
    Chain :meth:`_FrostField.map` / :meth:`_FrostField.re_first` to transform the extracted value.
    """
    if all and join is not None:
        raise ValueError("field(): `all` and `join` are mutually exclusive")
    if all:
        card: _Card = ("all", None)
    elif join is not None:
        card = ("join", join)
    else:
        card = ("first", None)
    return _FrostField(selector, card)


class _FrostGroup:
    """Marker for a `Many`/`One` grouped field; turned into a real ``web_poet.field`` that reads the
    shared batched :func:`extract_grouped` run."""

    __slots__ = ("container", "subfields", "one", "item")

    def __init__(self, container: str, subfields: Dict[str, _FrostField], one: bool, item):
        self.container = container
        self.subfields = subfields
        self.one = one
        self.item = item


def Many(container: str, *, item=None, **subfields: _FrostField) -> _FrostGroup:
    """A repeated nested field: for every element matching ``container`` (in document order), extract
    each keyword ``subfield`` — a :func:`field` — **scoped to that container** (descendant-or-self),
    all in the same streaming pass. Yields a ``list`` of rows. Each row is a ``dict`` of the sub-field
    values, or ``item(**row)`` if an ``item`` callable/class is given (e.g. an ``attrs``/zyte type).

        images = Many(".thumb", item=Image, url=field("img::attr(src)"))
    """
    for n, f in subfields.items():
        if not isinstance(f, _FrostField):
            raise TypeError(f"Many(): subfield {n!r} must be a field(...), got {type(f).__name__}")
    return _FrostGroup(container, subfields, one=False, item=item)


def One(container: str, *, item=None, **subfields: _FrostField) -> _FrostGroup:
    """Like :func:`Many` but yields the **first** container's row (a ``dict``/``item``), or ``None``
    if no container matches."""
    grp = Many(container, item=item, **subfields)
    grp.one = True
    return grp


def _merge_mro(cls, attr: str) -> dict:
    """Merge a per-class `_frostwork_own_*` dict across ``cls``'s MRO (nearest class wins, insertion
    order preserved). Called once per class at creation time, not per response."""
    out: dict = {}
    for klass in reversed(cls.__mro__):
        out.update(getattr(klass, attr, {}) or {})
    return out


def _make_field(name: str, card: _Card, transforms: Tuple[Callable, ...]):
    """A ``web_poet.field``-decorated getter that reads its column from the shared batched extract."""

    def getter(self):
        return _shape(self._frostwork_columns()[name], card, transforms)

    getter.__name__ = name
    getter.__qualname__ = name
    return _wp_field(getter)


def _make_group_field(name: str, grp: _FrostGroup):
    """A ``web_poet.field`` getter for a `Many`/`One`: shape each row's sub-columns (per-subfield
    cardinality + transforms), optionally build an ``item``, from the shared grouped run."""
    subnames = list(grp.subfields)
    subs = list(grp.subfields.values())

    def build(row):
        d = {sn: _shape(row[i], sub.card, sub.transforms) for i, (sn, sub) in enumerate(zip(subnames, subs))}
        return grp.item(**d) if grp.item is not None else d

    def getter(self):
        rows = self._frostwork_run()[1][name]
        if grp.one:
            return build(rows[0]) if rows else None
        return [build(r) for r in rows]

    getter.__name__ = name
    getter.__qualname__ = name
    return _wp_field(getter)


class FrostPage(WebPage):
    """Base page object. Declare fields with :func:`field` (and nested collections with :func:`Many` /
    :func:`One`); ``to_item()`` returns them all from a single streaming scan of ``self.response.body``.
    Subclass it exactly like a ``web_poet.WebPage`` (it *is* one), optionally with ``Returns[YourItem]``
    and ``@handle_urls(...)``. Schemas are validated at class definition by default; declare
    ``class MyPage(FrostPage, strict=False)`` to allow unsupported selectors as empty fields."""

    # Per-class own declarations, plus the MRO-merged view (subclasses inherit parent fields, nearest
    # class wins). BOTH are computed once at class-creation in __init_subclass__ — no per-response
    # dict rebuilding, and `frost_schema()`/`_frostwork_run` just read the merged dicts.
    _frostwork_own_specs: Dict[str, _Spec] = {}
    _frostwork_own_groups: Dict[str, _FrostGroup] = {}
    _frostwork_specs: Dict[str, _Spec] = {}
    _frostwork_groups: Dict[str, _FrostGroup] = {}
    # The whole schema compiled to ONE native Plan at class-creation (below), reused for every
    # response — no per-page selector recompilation. `_frostwork_flat_names`/`_frostwork_group_names`
    # align the plan's positional columns back to field names.
    _frostwork_plan = None
    _frostwork_flat_names: List[str] = []
    _frostwork_group_names: List[str] = []

    def __init_subclass__(cls, **kwargs) -> None:
        own: Dict[str, _Spec] = {}
        own_groups: Dict[str, _FrostGroup] = {}
        # Convert `field(...)` / `Many(...)` / `One(...)` markers into real web_poet fields. Because we
        # install them via setattr (not in the class body), Python won't auto-call the descriptor's
        # __set_name__ — so we invoke it ourselves to register the field, BEFORE web-poet's own
        # __init_subclass__ (reached through super()) promotes the registration.
        for name, val in list(vars(cls).items()):
            if isinstance(val, _FrostField):
                own[name] = (val.selector, val.card, val.transforms)
                wp = _make_field(name, val.card, val.transforms)
                setattr(cls, name, wp)
                wp.__set_name__(cls, name)
            elif isinstance(val, _FrostGroup):
                own_groups[name] = val
                wp = _make_group_field(name, val)
                setattr(cls, name, wp)
                wp.__set_name__(cls, name)
        cls._frostwork_own_specs = own
        cls._frostwork_own_groups = own_groups
        # merge across the MRO NOW (nearest class wins, order preserved) so it is not rebuilt per page
        cls._frostwork_specs = _merge_mro(cls, "_frostwork_own_specs")
        cls._frostwork_groups = _merge_mro(cls, "_frostwork_own_groups")
        # A subclass may replace an inherited group with a flat field (or vice versa). Treat the nearest
        # declaration as the single field instead of scanning both schemas under the same public name.
        for name in own:
            cls._frostwork_groups.pop(name, None)
        for name in own_groups:
            cls._frostwork_specs.pop(name, None)
        # Compile the whole schema to ONE native Plan, ONCE, here at class creation — reused for every
        # response (an over-budget schema raises `ValueError` here, at import, not per page).
        cls._frostwork_flat_names = list(cls._frostwork_specs)
        cls._frostwork_group_names = list(cls._frostwork_groups)
        flat_queries = [cls._frostwork_specs[n][0] for n in cls._frostwork_flat_names]
        garg = [
            (g.container, [(sn, sf.selector) for sn, sf in g.subfields.items()])
            for g in (cls._frostwork_groups[n] for n in cls._frostwork_group_names)
        ]
        cls._frostwork_plan = _Plan(flat_queries, garg)
        # Validate at class-definition time, so an unsupported selector fails loudly at import rather
        # than becoming a silently empty field in production. `strict=False` explicitly opts out.
        strict = kwargs.pop("strict", True)
        super().__init_subclass__(**kwargs)
        if strict:
            cls.check_schema().raise_for_status()

    @classmethod
    def check_schema(cls) -> SchemaReport:
        """Audit this page object's whole schema (own + inherited fields and groups) without touching
        any HTML: which selectors are supported, advisory reasons for those that are not, and budget
        usage. This runs automatically at class definition unless the class passes ``strict=False``.
        See :class:`frostwork.SchemaReport`."""
        named_queries = [(n, spec[0]) for n, spec in cls._frostwork_specs.items()]
        named_groups = [
            (n, g.container, {sn: sf.selector for sn, sf in g.subfields.items()})
            for n, g in cls._frostwork_groups.items()
        ]
        return check(named_queries, named_groups)

    @classmethod
    def frost_schema(cls) -> dict:
        """The page object's full extraction schema (own + inherited), for benchmarking / parity /
        introspection — the public replacement for reaching into ``_frostwork_own_specs`` (which is
        own-class-only and internal). Returns ``{"fields": {name: selector},
        "groups": {name: (container, {subname: selector})}}``."""
        return {
            "fields": {n: spec[0] for n, spec in cls._frostwork_specs.items()},
            "groups": {
                n: (g.container, {sn: sf.selector for sn, sf in g.subfields.items()})
                for n, g in cls._frostwork_groups.items()
            },
        }

    @cached_method
    def _frostwork_run(self):
        """Run the page object's whole schema — flat fields AND `Many`/`One` groups — in ONE pass
        through the class's pre-compiled `Plan`; cached per instance so every field reads one scan."""
        resp = self.response
        # web-poet's HttpResponseBody subclasses bytes, which the native fn accepts directly —
        # avoid copying the whole body on the hot path.
        body = resp.body if isinstance(resp.body, bytes) else bytes(resp.body)
        flat_cols, grouped = self._frostwork_plan.extract_grouped(body, resp.encoding)
        return (
            dict(zip(self._frostwork_flat_names, flat_cols)),
            dict(zip(self._frostwork_group_names, grouped)),
        )

    def _frostwork_columns(self) -> Dict[str, List[str]]:
        """Flat columns from the shared one-pass run (grouped fields read `_frostwork_run()[1]`)."""
        return self._frostwork_run()[0]
