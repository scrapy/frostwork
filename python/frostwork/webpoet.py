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

**Field processors** work too, including the ones a zyte-common-items base page attaches for you: because
a processor's input contract is an lxml/parsel node, a **bare-element** field (whose value is the
element's outer HTML) hands the processor the parsed *element* rather than its raw source, while
``::text``/``::attr()`` fields stay the strings they are. See ``docs/PYTHON.md`` ("Field processors") for
the rule and its one dependency.

Requires web-poet:  ``pip install frostwork[webpoet]``.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

try:
    from web_poet import BrowserPage, Extractor, WebPage, cached_method
    from web_poet import field as _wp_field
    # documented public API (referenced from `web_poet.field`'s own docstring) but not re-exported at the
    # package top level, so it is imported from the module that defines it
    from web_poet.fields import get_fields_dict as _wp_fields_dict
except ImportError as exc:  # pragma: no cover - exercised only without web-poet installed
    raise ImportError(
        "frostwork.webpoet requires web-poet; install it with `pip install frostwork[webpoet]`"
    ) from exc

from ._frostwork import Plan as _Plan
from ._frostwork import selector_terminals as _terminals
from .page import _shape  # single source of cardinality shaping (first/all/join + transforms)
from .page import SchemaReport, check  # schema audit / strict validation

__all__ = ["FrostPage", "FrostBrowserPage", "FrostFields", "field", "Many", "One"]

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


def _require_frost_owner(owner, name: str, what: str) -> None:
    """A marker is inert unless something converts it, and the only thing that does is
    :meth:`FrostFields.__init_subclass__`. Declared on a class that does not inherit it — web-poet's own
    ``BrowserPage``, an ``Extractor``, a plain ``WebPage`` — the marker just sat in the class dict and
    ``to_item()`` returned an item with those fields ABSENT: no error, no empty column, nothing to notice.

    Python calls ``__set_name__`` on every descriptor in a class body before the parent's
    ``__init_subclass__`` runs, and the class object already exists at that point, so this fires at class
    definition — i.e. at import — instead of turning up as a quietly incomplete item in production."""
    if not issubclass(owner, FrostFields):
        raise TypeError(
            f"Frostwork {what} {name!r} is declared on {owner.__name__}, which does not inherit a "
            f"Frostwork page base, so nothing would convert it and to_item() would silently omit the "
            f"field. Inherit FrostPage (for HttpResponse) or FrostBrowserPage (for BrowserResponse); for "
            f"any other input, inherit FrostFields and override frostwork_input()."
        )


class _FrostField:
    """Marker left in a class body by :func:`field`; :meth:`FrostFields.__init_subclass__` turns it
    into a real ``web_poet.field`` bound to the shared batched extract.

    :meth:`map` / :meth:`re_first` attach pure-Python transforms that run on the **shaped** value
    (after cardinality) — never in the scan — so declaring a transformed field stays a one-liner
    instead of a separate ``@web_poet.field`` method. They return a new marker (chainable)."""

    __slots__ = ("selector", "card", "transforms")

    def __init__(self, selector: str, card: _Card, transforms: Tuple[Callable, ...] = ()) -> None:
        self.selector = selector
        self.card = card
        self.transforms = transforms

    def __set_name__(self, owner, name: str) -> None:
        _require_frost_owner(owner, name, "field")

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

    def __set_name__(self, owner, name: str) -> None:
        _require_frost_owner(owner, name, "Many/One group")


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


def _as_wp_field(name: str, getter):
    """Wrap ``getter`` as a real ``web_poet.field`` under ``name``. The naming is load-bearing, not
    cosmetic: web-poet keys its registration off the function's name, so a getter left called
    ``getter`` registers every field under that one name."""
    getter.__name__ = name
    getter.__qualname__ = name
    return _wp_field(getter)


def _processors_for(cls, name: str):
    """The processors web-poet WILL apply to field ``name`` on ``cls`` — resolved exactly the way
    ``web_poet.fields.field.__get__`` resolves them, because a different answer here means handing the
    wrong TYPE to a processor that silently accepts anything.

    web-poet's order is: an explicit ``out=`` wins, else a nested ``Processors`` class looked up BY FIELD
    NAME. That second route is the one that matters and the one that is easy to miss: every
    zyte-common-items base page declares a ``Processors``, so inheriting ``ProductPage`` attaches
    ``breadcrumbs_processor`` to a field merely called ``breadcrumbs`` — with no ``out=`` written anywhere
    in the page object."""
    info = _wp_fields_dict(cls).get(name)
    out = getattr(info, "out", None) if info is not None else None
    if out:
        return list(out)
    procs = getattr(cls, "Processors", None)
    if procs is not None:
        return list(getattr(procs, name, ()) or ())
    return []


def _as_node(raw: str):
    """One captured element's RAW SOURCE re-parsed into a ``parsel.Selector`` wrapping that element.

    ``lxml.html.fromstring`` rather than ``parsel.Selector(text=...)``: the latter wraps the fragment in a
    synthetic ``<html><body>`` and its ``.root`` is the ``<html>``, so a processor would receive the
    document instead of the element it asked for. ``fromstring`` on the raw source of a single element
    returns that element, tag intact (checked for ``<div>``, ``<p>``, ``<td>`` and a ``<tr>``).

    Frostwork's outer HTML is raw source, which is the RIGHT input here: unlike lxml's re-serialization it
    round-trips, so re-parsing it reconstructs the subtree rather than a reflowed copy of it. Implied
    closes are already applied by the engine (``<p class=x>a<div>`` captures ``<p class=x>a``), so the
    fragment ends where the tree says it does."""
    from lxml.html import fromstring  # noqa: PLC0415 - see _NODE_DEPENDENCY
    from parsel import Selector  # noqa: PLC0415

    return Selector(root=fromstring(raw))


# The node handoff is the ONE place this integration needs lxml/parsel, and the imports are function-local
# for that reason: a page object with no processors never reaches them, so Frostwork's core stays
# tree-free and the dependency stays optional. It is also unavoidable rather than a shortcut — a field
# processor's input contract IS an lxml/parsel node (`isinstance(value, (Selector, HtmlElement))`), so
# there is no way to satisfy it without them. Anyone using processors already has both installed:
# `zyte_common_items.processors` itself imports `from lxml.html import HtmlElement`.
#
# The cost is honest and worth stating: a processor-bearing field parses that ONE subtree. That is far
# less than the whole-document parse the integration exists to avoid, but it is not free, and it does not
# apply to `::text`/`::attr()` fields at all — those are genuinely strings and are handed over untouched.
_NODE_DEPENDENCY = "lxml + parsel, imported lazily and only for a processor-bearing outer-HTML field"


def _as_nodes(col: List[str], card: _Card):
    """Shape a raw-source column into the node type the processor expects, mirroring `_shape`'s cardinality.

    ``all`` yields a ``SelectorList`` rather than a plain list because that is what the processors branch
    on (zyte's ``_handle_selectorlist`` takes ``value[0]``); a plain list of ``Selector`` would fall
    through to the "returned as is" path and reintroduce the bug in a new shape."""
    from parsel.selector import SelectorList  # noqa: PLC0415

    kind = card[0]
    if kind == "all":
        return SelectorList([_as_node(v) for v in col if v])
    if kind == "join":
        # A joined string is not a node and cannot be made into one without inventing a wrapper element.
        # Leave it a string: `join=` on a processor-bearing field is the caller asking for text.
        return _shape(col, card, ())
    for v in col:
        if v:
            return _as_node(v)
    return None


def _make_field(name: str, card: _Card, transforms: Tuple[Callable, ...], node: bool = False):
    """A ``web_poet.field``-decorated getter that reads its column from the shared batched extract.

    ``node`` is true only for a BARE-ELEMENT (outer-HTML) selector, as answered by the engine's own
    compiler via `selector_terminals` — never by re-deriving the terminal from the query string here. When
    such a field also has a processor attached, the processor gets the parsed element instead of its raw
    source. Both halves of that condition are load-bearing: converting on processor presence ALONE would
    break `images_processor`, which takes URL strings and has no `Selector` branch at all, so handing it a
    node would return the node unchanged and turn a working field into a broken one."""

    def getter(self):
        cols = self._frostwork_columns()
        if name not in cols:
            # Insurance, not an expected path: a field whose descriptor is installed but whose spec never
            # reached the plan. `@attrs.define` used to land here (it rebuilds the class, so
            # `__init_subclass__` re-ran with the markers gone) and the symptom was a bare `KeyError`,
            # which says nothing about the cause. Any other decorator or metaclass that rebuilds a class
            # in a way the recovery in `__init_subclass__` does not recognise would land here too, so it
            # explains itself rather than leaving a mystery in a scraper's logs.
            raise RuntimeError(
                f"field {name!r} is installed on {type(self).__name__} but is not in its compiled plan. "
                "Something rebuilt the class after Frostwork processed it (a class-recreating decorator "
                "or metaclass). Frostwork recovers from `@attrs.define`; if you hit this with something "
                "else, declaring the field on a plain base class and inheriting it is the workaround."
            )
        col = cols[name]
        if node and _processors_for(type(self), name):
            value = _as_nodes(col, card)
            for fn in transforms:
                value = fn(value)
            return value
        return _shape(col, card, transforms)

    return _as_wp_field(name, getter)


def _make_group_field(name: str, grp: _FrostGroup):
    """A ``web_poet.field`` getter for a `Many`/`One`: shape each row's sub-columns (per-subfield
    cardinality + transforms), optionally build an ``item``, from the shared grouped run."""
    subs = list(grp.subfields.items())

    def build(row):
        d = {sn: _shape(col, sub.card, sub.transforms) for (sn, sub), col in zip(subs, row)}
        return grp.item(**d) if grp.item is not None else d

    def getter(self):
        rows = self._frostwork_run()[1][name]
        if grp.one:
            return build(rows[0]) if rows else None
        return [build(r) for r in rows]

    return _as_wp_field(name, getter)


class FrostFields(Extractor):
    """All of the field machinery, with **no** response contract — the piece both page bases share.

    Built on web-poet's ``Extractor`` (its "base class for field support"), so this alone is a usable page
    object: it brings ``to_item()`` and ``Returns[...]`` and needs only :meth:`frostwork_input`. That is
    the shape to reach for when the input is neither of the two responses below.

    It is split out because the response type is the one thing that varies, and getting that wrong was
    silent: `field()` markers are converted by ``__init_subclass__``, so declaring them on a class that
    does not inherit this mixin (web-poet's own ``BrowserPage``, say) converted nothing and ``to_item()``
    returned an item with the fields simply absent. Subclass :class:`FrostPage` or
    :class:`FrostBrowserPage`, or inherit this and override :meth:`frostwork_input`."""

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
                spec = (val.selector, val.card, val.transforms)
                own[name] = spec
                # Ask the COMPILER whether this selector's value is a node, once, here at class creation
                # — not per response, and never by pattern-matching the query string.
                is_node = _terminals([val.selector])[0] == "outer"
                wp = _make_field(name, val.card, val.transforms, node=is_node)
                # Leave the spec ON the descriptor. This is what makes the class survive being REBUILT —
                # see the `_frostwork_spec` recovery below.
                wp._frostwork_spec = spec
            elif isinstance(val, _FrostGroup):
                own_groups[name] = val
                wp = _make_group_field(name, val)
                wp._frostwork_group = val
            else:
                # Already a converted field? Then this class has been through here before and something
                # REBUILT it. `@attrs.define` (slots=True, the default) does exactly that: it does not
                # mutate the class, it constructs a new one from the old `__dict__`. So
                # `__init_subclass__` runs a second time with the markers already gone, and the original
                # class is not in the new MRO for `_merge_mro` to find — which silently emptied the plan
                # and made every field raise `KeyError` at `to_item()`. Recovering the spec from the
                # descriptor we left behind makes the rebuild a no-op instead of a wipe.
                #
                # Note this is an ORDER bug, not a lookup bug: the decorator runs AFTER
                # `__init_subclass__`, so no amount of care inside a single pass could have caught it.
                recovered = getattr(val, "_frostwork_spec", None)
                if recovered is not None:
                    own[name] = recovered
                else:
                    recovered_group = getattr(val, "_frostwork_group", None)
                    if recovered_group is not None:
                        own_groups[name] = recovered_group
                continue
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
        # `frost_schema()` already returns the two named-mapping shapes `check` accepts, so the schema
        # is described in exactly one place — audit and introspection cannot report different selectors.
        schema = cls.frost_schema()
        return check(schema["fields"], schema["groups"])

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

    def frostwork_input(self) -> Tuple[bytes, Optional[str]]:
        """The ``(html_bytes, encoding)`` this page object scans. Override to source a page object from
        something other than the two provided bases — that is the whole extension point, and it is public
        because web-poet's input universe is larger than the shapes shipped here (``Extractor`` subclasses
        can be given any dependency at all).

        ``encoding`` may be ``None``, in which case the engine sniffs (BOM, then a ``<meta>`` prescan)
        exactly as Parsel would. Return the ORIGINAL bytes where you have them: decoding to ``str`` and
        re-encoding can only lose information the sniffer would otherwise use."""
        raise NotImplementedError(
            f"{type(self).__name__} does not define frostwork_input(). Inherit FrostPage (HttpResponse) "
            "or FrostBrowserPage (BrowserResponse), or override frostwork_input() to return "
            "(html_bytes, encoding)."
        )

    @cached_method
    def _frostwork_run(self):
        """Run the page object's whole schema — flat fields AND `Many`/`One` groups — in ONE pass
        through the class's pre-compiled `Plan`; cached per instance so every field reads one scan."""
        body, encoding = self.frostwork_input()
        flat_cols, grouped = self._frostwork_plan.extract_grouped(body, encoding)
        return (
            dict(zip(self._frostwork_flat_names, flat_cols)),
            dict(zip(self._frostwork_group_names, grouped)),
        )

    def _frostwork_columns(self) -> Dict[str, List[str]]:
        """Flat columns from the shared one-pass run (grouped fields read `_frostwork_run()[1]`)."""
        return self._frostwork_run()[0]


class FrostPage(FrostFields, WebPage):
    """Base page object over web-poet's ``HttpResponse``. Declare fields with :func:`field` (and nested
    collections with :func:`Many` / :func:`One`); ``to_item()`` returns them all from a single streaming
    scan of ``self.response.body``. Subclass it exactly like a ``web_poet.WebPage`` (it *is* one),
    optionally with ``Returns[YourItem]`` and ``@handle_urls(...)``. Schemas are validated at class
    definition by default; declare ``class MyPage(FrostPage, strict=False)`` to allow unsupported
    selectors as empty fields."""

    def frostwork_input(self) -> Tuple[bytes, Optional[str]]:
        resp = self.response
        # web-poet's HttpResponseBody subclasses bytes, which the native fn accepts directly —
        # avoid copying the whole body on the hot path.
        body = resp.body if isinstance(resp.body, bytes) else bytes(resp.body)
        return body, resp.encoding


class FrostBrowserPage(FrostFields, BrowserPage):
    """Base page object over web-poet's ``BrowserResponse`` — a browser's DOM snapshot rather than the
    bytes off the wire. Identical to :class:`FrostPage` in every other respect.

    A ``BrowserResponse`` carries ``.html`` (a ``str``) and no bytes, so there is nothing to sniff: the
    text is encoded UTF-8 and scanned with the encoding stated rather than guessed. That is not a
    shortcut — the browser has already resolved the page's encoding, and re-deriving it from a
    re-encoding of the decoded text could only disagree with the browser that produced it."""

    def frostwork_input(self) -> Tuple[bytes, Optional[str]]:
        return str(self.response.html).encode("utf-8"), "utf-8"
