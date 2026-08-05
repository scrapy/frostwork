"""Frostwork × web-poet — author page objects whose selector fields are answered in **one**
streaming pass.

Each :func:`field` you declare becomes a real ``web_poet.field``, so everything web-poet gives you
still works — attribute access, ``async to_item()``, ``@handle_urls`` routing, ``Returns[Item]``,
and mixing in hand-written ``@web_poet.field`` methods for computed fields. The difference is that
*all* of a page object's Frostwork selectors share a **single cached** :func:`frostwork.extract`
call, instead of one lxml parse + one query per field. The document scan is shared; matching work
still grows with field count and selector complexity.

    from frostwork.webpoet import FrostPage, field

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
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    TypeVar,
    overload,
)

try:
    from web_poet import BrowserPage, ItemPage, WebPage, cached_method
    from web_poet import field as _wp_field
    # documented public API (referenced from `web_poet.field`'s own docstring) but not re-exported at the
    # package top level, so it is imported from the module that defines it
    from web_poet.fields import get_fields_dict as _wp_fields_dict
except ImportError as exc:  # pragma: no cover - exercised only without a usable web-poet
    # The version floor lives in `pyproject.toml`'s `webpoet` extra and nowhere else — a constant here
    # would be a second one to keep in step, and it could not enforce anything either (an older web-poet
    # imports fine right up to the release that drops a name). What this adds is the DIAGNOSIS: "install
    # web-poet" is useless advice to someone who has it installed and too old.
    try:
        from importlib.metadata import version as _version  # noqa: PLC0415

        _installed: Optional[str] = _version("web-poet")
    except Exception:  # pragma: no cover - web-poet is not installed at all
        _installed = None
    raise ImportError(
        f"frostwork.webpoet: the installed web-poet {_installed} is incompatible ({exc}); reinstall with "
        "`pip install -U 'frostwork[webpoet]'`"
        if _installed
        else "frostwork.webpoet requires web-poet; install it with `pip install frostwork[webpoet]`"
    ) from exc

from ._frostwork import Plan as _Plan
from ._frostwork import selector_terminals as _terminals
from .page import _shape  # single source of cardinality shaping (first/all/join + transforms)
from .page import SchemaReport, check  # schema audit / strict validation

__all__ = ["FrostPage", "FrostBrowserPage", "FrostFields", "field", "Many", "One"]

# ("first", None) | ("all", None) | ("join", separator)
_Card = Tuple[str, Optional[str]]
_Spec = Tuple[str, _Card, Tuple[Callable, ...]]  # (selector, card, transforms)

# `T` is a field's VALUE type as a type checker sees it through the descriptor: `str | None` for a plain
# field, `list[str]` for `all=True`, `str` for `join=`. `U` is a `.map()` result.
T = TypeVar("T")
U = TypeVar("U")
_ItemT = TypeVar("_ItemT")

# web-poet's own `field()` keyword surface, forwarded verbatim by `field()` below. Enumerated here rather
# than accepted as `**kwargs` so a keyword web-poet adds is a visible gap rather than a silent no-op —
# `tools/webpoet_surface.py` gates this tuple against `inspect.signature(web_poet.field)`.
_WP_FIELD_KWARGS = ("cached", "meta", "out")


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
    """A marker is inert unless :meth:`FrostFields.__init_subclass__` converts it, so declaring one on a
    class that does not inherit that base leaves it in the class dict and drops the field from
    ``to_item()`` — no error, nothing to notice. Python calls ``__set_name__`` before the parent's
    ``__init_subclass__``, so this fires at class definition instead."""
    if not issubclass(owner, FrostFields):
        raise TypeError(
            f"Frostwork {what} {name!r} is declared on {owner.__name__}, which does not inherit a Frostwork "
            f"page base, so nothing converts it and to_item() would omit the field. Inherit FrostPage (for "
            f"HttpResponse), FrostBrowserPage (for BrowserResponse), or FrostFields with your own "
            f"frostwork_input()."
        )


class _FrostField(Generic[T]):
    """Marker left in a class body by :func:`field`; :meth:`FrostFields.__init_subclass__` turns it
    into a real ``web_poet.field`` bound to the shared batched extract.

    :meth:`map` / :meth:`re_first` attach pure-Python transforms that run on the **shaped** value
    (after cardinality) — never in the scan — so declaring a transformed field stays a one-liner
    instead of a separate ``@web_poet.field`` method. They return a new marker (chainable).

    The ``Generic[T]`` parameter is the field's VALUE type. It exists so a type checker reads
    ``page.name`` as ``str | None`` rather than as this marker class: the runtime swaps the marker for a
    ``web_poet.field`` descriptor in ``__init_subclass__``, and a checker cannot see that happen, so
    before this the annotation said ``_FrostField`` and correct code (``x: str = page.name``) was a type
    error. Since the package ships ``py.typed``, that wrong answer propagated into users' own CI."""

    __slots__ = ("selector", "card", "transforms", "wp_kwargs")

    if TYPE_CHECKING:
        # Descriptor protocol for the CHECKER only — at runtime this class is never accessed as a
        # descriptor, because `__init_subclass__` has already replaced it with the real `web_poet.field`.
        # Declaring it under TYPE_CHECKING keeps that honest: no dead `__get__` in the shipped object, and
        # nothing that could quietly answer if the replacement ever failed to happen.
        @overload
        def __get__(self, instance: None, owner: Any) -> "_FrostField[T]": ...
        @overload
        def __get__(self, instance: Any, owner: Any) -> T: ...
        def __get__(self, instance: Any, owner: Any = None) -> Any: ...

    def __init__(
        self,
        selector: str,
        card: _Card,
        transforms: Tuple[Callable, ...] = (),
        wp_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.selector = selector
        self.card = card
        self.transforms = transforms
        self.wp_kwargs = wp_kwargs or {}

    def __set_name__(self, owner, name: str) -> None:
        _require_frost_owner(owner, name, "field")

    def map(self, fn: Callable[[T], U]) -> "_FrostField[U]":
        """Apply ``fn`` to the field's shaped value (a str/``None`` for a plain field, a ``list`` for
        ``all=True``, a str for ``join=``). Chainable, and the value type follows ``fn``'s return."""
        return _FrostField(self.selector, self.card, self.transforms + (fn,), self.wp_kwargs)

    def typed_as(self, tp: "Type[U]") -> "_FrostField[U]":
        """Re-annotate this field as producing ``tp``. A **no-op at runtime**; it exists for the type checker.

        The types the overloads on :func:`field` give you describe the value BEFORE web-poet's processors
        run — ``str | None``, ``list[str]``, ``str`` — because a processor is an opaque callable attached by
        name from a base page's ``Processors``, and nothing static can tell what it returns. On a
        processor-bearing field that answer is wrong rather than imprecise: the field really produces a
        ``list[Breadcrumb]``, an ``AggregateRating``, a ``Selector``. Say so where you declare it::

            breadcrumbs = field(".crumbs").typed_as(List[Breadcrumb])

        Takes a class or a generic alias (``List[Breadcrumb]``, ``dict``); for anything a checker will not
        accept as a type argument, annotate the attribute instead."""
        return self  # type: ignore[return-value]

    def re_first(self, pattern: str) -> "_FrostField[Optional[str]]":
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


@overload
def field(
    selector: str,
    *,
    all: Literal[True],
    cached: bool = ...,
    meta: Optional[dict] = ...,
    out: Optional[List[Callable]] = ...,
) -> _FrostField[List[str]]: ...


@overload
def field(
    selector: str,
    *,
    join: str,
    cached: bool = ...,
    meta: Optional[dict] = ...,
    out: Optional[List[Callable]] = ...,
) -> _FrostField[str]: ...


# The default shape, and the two ways of SPELLING the default explicitly. `all=False` and `join=None` are
# valid calls that a checker rejected until they were written down — the kind of gap `py.typed` turns into
# an error in someone else's CI.
@overload
def field(
    selector: str,
    *,
    all: Literal[False] = ...,
    join: None = ...,
    cached: bool = ...,
    meta: Optional[dict] = ...,
    out: Optional[List[Callable]] = ...,
) -> _FrostField[Optional[str]]: ...


# The fallback for cardinality decided at RUNTIME (`field(sel, all=some_bool)`): the value type genuinely
# is not known statically, and `Any` says so rather than picking one of the two and being wrong half the time.
@overload
def field(
    selector: str,
    *,
    all: bool = ...,
    join: Optional[str] = ...,
    cached: bool = ...,
    meta: Optional[dict] = ...,
    out: Optional[List[Callable]] = ...,
) -> _FrostField[Any]: ...


def field(
    selector: str,
    *,
    all: bool = False,
    join: Optional[str] = None,
    cached: bool = False,
    meta: Optional[dict] = None,
    out: Optional[List[Callable]] = None,
) -> "_FrostField[Any]":
    """Declare a Frostwork selector field on a :class:`FrostPage`.

    Default: the **first** match (or ``None``). ``all=True``: a **list** of every match, in document
    order. ``join=sep``: every match **joined** into one string with ``sep``. ``all`` and ``join``
    are mutually exclusive. ``selector`` is any Frostwork-supported CSS or downward XPath query.
    Chain :meth:`_FrostField.map` / :meth:`_FrostField.re_first` to transform the extracted value.

    ``cached``, ``meta`` and ``out`` are ``web_poet.field``'s own keywords, forwarded verbatim — the
    declaration is a real ``web_poet.field``, so there is no reason for its options to be unreachable
    just because Frostwork built it. ``out`` in particular was previously impossible to pass here, which
    forced anyone wanting a processor into a hand-written ``@web_poet.field`` method (and out of the
    shared scan) or into a nested ``Processors`` class.

    ``out`` vs :meth:`_FrostField.map` — related but not the same thing, and they compose in this order:

    1. the selector's column is shaped by ``all``/``join``,
    2. ``.map()`` / ``.re_first()`` transforms run (plain callables, value in and value out),
    3. web-poet's processors run — ``out`` if given, else a nested ``Processors`` entry for this field
       name. These take ``(value, page)``, so they can read the response, which a ``.map()`` cannot.

    Reach for ``.map()`` for a local value tweak and ``out=`` for an ecosystem processor.

    On a **bare-element** field with a processor, the handoff that turns the (transformed) raw source into a
    node belongs to step 3 — so a ``.map()`` there sees HTML source and must return HTML source. ``out=[]``
    means "no processors on this field", cancelling a nested ``Processors`` entry inherited from a base page,
    and with no processor left to hand over to the value stays the raw-source string.
    """
    if all and join is not None:
        raise ValueError("field(): `all` and `join` are mutually exclusive")
    if all:
        card: _Card = ("all", None)
    elif join is not None:
        card = ("join", join)
    else:
        card = ("first", None)
    # Only carry the keywords actually given, so a plain `field()` keeps the bare
    # `web_poet.field(getter)` construction path instead of routing every declaration through the
    # decorator-factory form for a set of defaults.
    wp_kwargs: Dict[str, Any] = {}
    if cached:
        wp_kwargs["cached"] = True
    if meta is not None:
        wp_kwargs["meta"] = meta
    if out is not None:
        wp_kwargs["out"] = out
    return _FrostField(selector, card, (), wp_kwargs)


class _FrostGroup(Generic[T]):
    """Marker for a `Many`/`One` grouped field; turned into a real ``web_poet.field`` that reads the
    shared batched :func:`extract_grouped` run. ``T`` is the value type a checker sees — see
    :class:`_FrostField` for why the descriptor protocol below is TYPE_CHECKING-only."""

    __slots__ = ("container", "subfields", "one", "item")

    if TYPE_CHECKING:
        @overload
        def __get__(self, instance: None, owner: Any) -> "_FrostGroup[T]": ...
        @overload
        def __get__(self, instance: Any, owner: Any) -> T: ...
        def __get__(self, instance: Any, owner: Any = None) -> Any: ...

    def __init__(self, container: str, subfields: Dict[str, _FrostField], one: bool, item):
        self.container = container
        self.subfields = subfields
        self.one = one
        self.item = item

    def __set_name__(self, owner, name: str) -> None:
        _require_frost_owner(owner, name, "Many/One group")


@overload
def Many(
    container: str, *, item: Callable[..., _ItemT], **subfields: _FrostField
) -> _FrostGroup[List[_ItemT]]: ...


@overload
def Many(container: str, **subfields: _FrostField) -> _FrostGroup[List[Dict[str, Any]]]: ...


def Many(container: str, *, item=None, **subfields: _FrostField) -> "_FrostGroup[Any]":
    """A repeated nested field: for every element matching ``container`` (in document order), extract
    each keyword ``subfield`` — a :func:`field` — **scoped to that container** (descendant-or-self),
    all in the same streaming pass. Yields a ``list`` of rows. Each row is a ``dict`` of the sub-field
    values, or ``item(**row)`` if an ``item`` callable/class is given (e.g. an ``attrs``/zyte type).

        images = Many(".thumb", item=Image, url=field("img::attr(src)"))
    """
    for n, f in subfields.items():
        if not isinstance(f, _FrostField):
            raise TypeError(f"Many(): subfield {n!r} must be a field(...), got {type(f).__name__}")
        if f.wp_kwargs:
            # A subfield is one COLUMN of a row: the GROUP is the single `web_poet.field`, so there is no
            # descriptor for `cached`/`meta` and no name for web-poet to resolve a processor under. Refused
            # at declaration rather than accepted and dropped.
            raise TypeError(
                f"Many()/One(): subfield {n!r} was given {sorted(f.wp_kwargs)}, which cannot apply to a "
                f"subfield — web-poet only sees the group as a field. Use .map() for a per-value transform; "
                f"for a processor over the whole group, write the group as a @web_poet.field method."
            )
    return _FrostGroup(container, subfields, one=False, item=item)


@overload
def One(
    container: str, *, item: Callable[..., _ItemT], **subfields: _FrostField
) -> _FrostGroup[Optional[_ItemT]]: ...


@overload
def One(container: str, **subfields: _FrostField) -> _FrostGroup[Optional[Dict[str, Any]]]: ...


def One(container: str, *, item=None, **subfields: _FrostField) -> "_FrostGroup[Any]":
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


def _resolve_attr(cls, name: str):
    """The class attribute Python itself will find for ``name`` — the first hit along the MRO.

    Walked rather than ``getattr``ed so the answer is the descriptor object, whatever it is, without
    invoking the descriptor protocol."""
    for klass in cls.__mro__:
        if name in vars(klass):
            return vars(klass)[name]
    return None


def _resolved_schema(cls) -> Tuple[Dict[str, _Spec], Dict[str, "_FrostGroup"]]:
    """The page object's schema: every declaration reachable in the MRO that ``cls`` still RESOLVES to a
    Frostwork descriptor, taken from that descriptor.

    Merging the inherited declarations is not enough, because a subclass can replace one — with a
    hand-written ``@web_poet.field``, a flat field over an inherited group, a plain method, a constant, or
    a mixin listed before the Frostwork base. web-poet answers with whatever the MRO resolves, so a schema
    built by merging alone kept selectors the page object no longer answers with: they scanned columns
    nothing reads and could fail strict validation for a field the class had replaced. Popping the
    overridden names off the merged dict fixed the direct case only — the declaration is still in an
    ancestor's `_frostwork_own_specs`, so the next generation down merged it back in.

    So the merge supplies candidate NAMES (and their order) and the MRO supplies the answer. One pass, no
    tombstones, and it holds for any number of generations because it never asks what a parent decided."""
    specs: Dict[str, _Spec] = {}
    groups: Dict[str, "_FrostGroup"] = {}
    for name in (*_merge_mro(cls, "_frostwork_own_specs"), *_merge_mro(cls, "_frostwork_own_groups")):
        if name in specs or name in groups:
            continue
        resolved = _resolve_attr(cls, name)
        spec = getattr(resolved, "_frostwork_spec", None)
        group = getattr(resolved, "_frostwork_group", None)
        if spec is not None:
            specs[name] = spec
        elif group is not None:
            groups[name] = group
    return specs, groups


def _as_wp_field(name: str, getter, wp_kwargs: Optional[Dict[str, Any]] = None):
    """Wrap ``getter`` as a real ``web_poet.field`` under ``name``. The naming is load-bearing, not
    cosmetic: web-poet keys its registration off the function's name, so a getter left called
    ``getter`` registers every field under that one name.

    ``wp_kwargs`` carries `field()`'s pass-through of web-poet's own keywords (``cached``/``meta``/``out``)."""
    getter.__name__ = name
    getter.__qualname__ = name
    if not wp_kwargs:
        return _wp_field(getter)
    return _wp_field(**wp_kwargs)(getter)


def _processors_for(cls, name: str):
    """The processors web-poet WILL apply to field ``name`` on ``cls``.

    Resolved exactly as ``web_poet.fields.field.__get__`` resolves them — an explicit ``out=`` wins, else a
    nested ``Processors`` class looked up BY FIELD NAME — because a different answer means handing a
    different TYPE to something documented to accept anything and return it unchanged. Two details carry
    that: the by-name route is how every zyte-common-items base page arms nine fields with no ``out=``
    written anywhere, and the test is ``out is not None`` rather than ``if out``, because ``out=[]`` is
    web-poet's per-field opt-out and must CANCEL an inherited entry instead of falling through to it."""
    info = _wp_fields_dict(cls).get(name)
    out = getattr(info, "out", None) if info is not None else None
    if out is not None:
        return list(out)
    procs = getattr(cls, "Processors", None)
    if procs is not None:
        return list(getattr(procs, name, ()) or ())
    return []


# The element name a fragment opens with, after insignificant leading whitespace. Frostwork's own outer
# HTML starts at the start tag, but a `.map()` on a processor-bearing field runs BEFORE the handoff, so
# what arrives here is whatever that transform returned.
_START_TAG_NAME = re.compile(r"\s*<([a-zA-Z][^\s/>]*)")

# lxml unwraps these when it decides a fragment is a document, so the count of top-level fragments in the
# source describes their CHILDREN rather than them (`<head><title>t</title><meta>` -> two fragments, one
# element). They are exempt from the single-element check below for that reason, not because they are safe.
_FRAME_NAMES = frozenset({"html", "head", "body", "frameset"})


def _as_node(raw: str, what: str = "the node handoff", verify: bool = False):
    """One element's HTML source re-parsed into a ``parsel.Selector`` wrapping **that** element.

    Three invariants, all load-bearing:

    * ``lxml.html.fromstring``, not ``parsel.Selector(text=...)``: the latter wraps the fragment in a
      synthetic ``<html><body>`` and hands the processor the document instead of the element.
    * ``fromstring`` alone is not enough either — it applies a document-vs-fragment heuristic that answers
      with a DIFFERENT element for the document frame (a ``<body>``'s lone child; the synthesised ``<html>``
      for ``<head>``/``<title>``/``<meta>``/``<link>``/``<base>``). So the tag name is read off the source
      and the element is looked up in whatever tree came back. `tests/test_python.py` sweeps every name in
      the shared element universe; a handoff that is right for four tags says nothing about the rest.
    * It fails CLOSED. ``.map()`` runs before this, so ``raw`` is whatever a transform returned: anything
      that is not one recoverable element raises and names the field. Falling back to lxml's root would
      hand a processor an element the selector never matched, without a word. ``verify`` adds the
      is-it-one-element check, and is set only for a TRANSFORMED value — the engine's own outer HTML is one
      element by construction, and the check costs a second parse of the fragment.

    Frostwork's outer HTML is raw source, which round-trips where lxml's re-serialization does not, so the
    re-parse reconstructs the subtree rather than a reflowed copy. The contract is subtree-LOCAL: own
    attributes and descendants, but no ancestors, siblings or ``base_url`` (docs/PYTHON.md says what that
    costs and which processors are unaffected)."""
    from lxml.html import fragments_fromstring, fromstring  # noqa: PLC0415
    from parsel import Selector  # noqa: PLC0415

    m = _START_TAG_NAME.match(raw) if isinstance(raw, str) else None
    if m is None:
        raise TypeError(
            f"{what} needs one element's HTML source, got {type(raw).__name__} {str(raw)[:40]!r}. A "
            f".map()/.re_first() on a processor-bearing bare-element field runs on the source, so it must "
            f"return source; to transform what a processor produces, put it in `out=` instead."
        )
    want = m.group(1).lower()
    root = fromstring(raw)
    if root.tag != want:
        # the whole tree, not `root`'s subtree: an unwrapped lone child has the element we want as its PARENT
        root = next((el for el in root.getroottree().getroot().iter(want)), None)
    if root is None:
        raise ValueError(f"{what}: re-parsing this source produced no <{want}> to hand over: {raw[:60]!r}")
    if verify and want not in _FRAME_NAMES:
        tops = [f for f in fragments_fromstring(raw.strip()) if not isinstance(f, str)]
        if len(tops) > 1:
            raise ValueError(
                f"{what}: this source holds {len(tops)} top-level elements "
                f"({', '.join('<%s>' % f.tag for f in tops[:4])}) and a processor takes one node."
            )
    return Selector(root=root)


def _as_nodes(value, card: _Card, name: str = "<field>", verify: bool = False):
    """An already-SHAPED (and already-transformed) raw-source value as the node type the processor expects.

    Last step of ``shape -> .map()/.re_first() -> processors``, so it must run AFTER the transforms: doing it
    first handed a `Selector` to user code that the contract promises the shaped string.

    ``all`` yields a ``SelectorList`` rather than a plain list because that is what the processors branch on
    (zyte's ``_handle_selectorlist`` takes ``value[0]``); a plain list of `Selector` falls through to their
    "returned as is" path."""
    from parsel.selector import SelectorList  # noqa: PLC0415

    kind = card[0]
    what = f"field {name!r}"
    if kind == "all":
        if not isinstance(value, (list, tuple)):
            raise TypeError(
                f"{what} is all=True with a processor attached, so its value is a list of HTML sources "
                f"re-parsed into a SelectorList — but a .map() on it returned {type(value).__name__}."
            )
        return SelectorList([_as_node(v, what, verify) for v in value if v])
    if kind == "join":
        # A joined string is not a node and could only be made one by inventing a wrapper element. Leave it
        # a string: `join=` on a processor-bearing field is the caller asking for text.
        return value
    return _as_node(value, what, verify) if value else None


def _make_field(
    name: str,
    card: _Card,
    transforms: Tuple[Callable, ...],
    node: bool = False,
    wp_kwargs: Optional[Dict[str, Any]] = None,
):
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
            # Insurance, not an expected path: the descriptor is installed but its spec never reached the
            # plan, which means something rebuilt the class in a way `__init_subclass__`'s spec recovery did
            # not recognise. Diagnosed here because the symptom would otherwise be a bare `KeyError`.
            raise RuntimeError(
                f"field {name!r} is installed on {type(self).__name__} but is not in its compiled plan. "
                "Something rebuilt the class after Frostwork processed it (a class-recreating decorator or "
                "metaclass). `@attrs.define` is recovered from; for anything else, declare the field on a "
                "plain base class and inherit it."
            )
        col = cols[name]
        value = _shape(col, card, transforms)
        if node and _processors_for(type(self), name):
            # LAST, after `.map()`: the documented pipeline is shape -> transforms -> processors, and the
            # node conversion is the first half of handing over to a processor, not a replacement for the
            # value. Doing it before the transforms handed user code a `Selector` where the annotation and
            # the docstring both promise the shaped string.
            # `verify` only when a transform ran: the engine's outer HTML is one element by construction,
            # and the check is a second parse of the fragment.
            return _as_nodes(value, card, name, verify=bool(transforms))
        return value

    return _as_wp_field(name, getter, wp_kwargs)


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


class FrostFields(ItemPage):
    """All of the field machinery, with **no** response contract — the piece both page bases share.

    Subclass it (overriding :meth:`frostwork_input`) when the input is neither of the two responses below;
    it brings ``to_item()``, ``Returns[...]`` and web-poet's input validation with it.

    ``ItemPage``, not ``Extractor``: scrapy-poet builds only what ``web_poet.pages.is_injectable`` accepts,
    and ``Extractor`` is deliberately not injectable (it is web-poet's shape for a field bundle composed
    into a page). A callback annotated with a non-injectable class is dropped from andi's plan and the
    argument never arrives, silently. ``ItemPage`` is ``Extractor`` + ``Injectable``, so this gives up
    nothing; `tools/webpoet_surface.py` asserts every shipped base passes ``is_injectable``."""

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
    # Resolved `strict=` for THIS class, carried on the class so a rebuild cannot lose it (see
    # `__init_subclass__`). Read from `cls.__dict__` there, never inherited.
    _frostwork_strict: bool = True

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
                wp = _make_field(
                    name, val.card, val.transforms, node=is_node, wp_kwargs=val.wp_kwargs
                )
                # Leave the spec ON the descriptor. This is what makes the class survive being REBUILT —
                # see the `_frostwork_spec` recovery below.
                wp._frostwork_spec = spec
            elif isinstance(val, _FrostGroup):
                own_groups[name] = val
                wp = _make_group_field(name, val)
                wp._frostwork_group = val
            else:
                # An already-converted field means this class has been through here before, i.e. something
                # REBUILT it — `@attrs.define` (slots=True) constructs a new class from the old `__dict__`.
                # The markers are gone by then and the original class is not in the new MRO, so the spec is
                # recovered from the descriptor it was left on; without that, a rebuild silently empties the
                # plan. An ORDER bug: the decorator runs after `__init_subclass__`, not before it.
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
        # Resolve the whole schema NOW, once per class, so nothing is rebuilt per page. This is also where
        # an override of any shape (manual field, flat-over-group, mixin, constant) drops out — see
        # `_resolved_schema`, which asks the MRO instead of trusting the merge.
        cls._frostwork_specs, cls._frostwork_groups = _resolved_schema(cls)
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
        # Validate at class-definition time, so an unsupported selector fails loudly at import rather than
        # becoming a silently empty field in production. `strict=False` explicitly opts out.
        #
        # The default comes from the class's OWN `__dict__` because a class keyword exists only in the
        # `class` statement, and a rebuild (`@attrs.define`) throws that statement away while copying the
        # dict — so carrying the resolved value on the class is what makes the opt-out survive one. Own
        # dict, not inherited, is the deliberate half: a rebuild keeps it, a fresh subclass does not.
        strict = kwargs.pop("strict", cls.__dict__.get("_frostwork_strict", True))
        cls._frostwork_strict = strict
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
