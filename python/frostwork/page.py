"""Declarative page-object API over the one-pass :func:`frostwork.extract` primitive — a pure-Python
mirror of the Rust ``Page``/``Item``. A page object is a ``{field: selector}`` schema; one
``extract`` call fills the whole item with one shared document scan; matching work still grows with
the number and complexity of fields.

    >>> from frostwork import Page
    >>> html = b"<div class=product><h1>Widget</h1><span class=price>$9</span>" \\
    ...        b"<img src=/a.png><img src=/b.png></div>"
    >>> page = (Page()
    ...         .field("title", "h1::text")             # first match -> value or None
    ...         .field("price", ".price::text")
    ...         .field_all("images", "img::attr(src)"))  # every match -> list
    >>> item = page.extract(html)                        # ONE streaming pass fills every field
    >>> item.get("title")
    'Widget'
    >>> item.get_all("images")
    ['/a.png', '/b.png']
    >>> item.to_dict() == {"title": "Widget", "price": "$9", "images": ["/a.png", "/b.png"]}
    True
"""

from __future__ import annotations

import codecs
import json
from collections.abc import Mapping
from dataclasses import dataclass, field as _dc_field
from functools import lru_cache
from typing import Any, Callable, Iterable, Iterator, List, Optional, Tuple, Union

from ._frostwork import Plan as _Plan
from ._frostwork import audit_schema as _audit_schema
from ._frostwork import extract as _extract
from ._frostwork import extract_grouped as _extract_grouped
from ._frostwork import resolve_label as _resolve_label

__all__ = [
    "extract", "extract_grouped", "check", "Page", "Item",
    "SchemaReport", "FieldReport", "GroupReport", "UnsupportedSelector",
]

# A field's cardinality: ("first", None) | ("all", None) | ("join", separator).
_Card = Tuple[str, Optional[str]]

Bytesish = Union[bytes, bytearray, memoryview, str]


def _as_bytes(html: Bytesish) -> bytes:
    """Frostwork tokenizes raw bytes. `str` is encoded as UTF-8 (already-decoded text)."""
    if isinstance(html, str):
        return html.encode("utf-8")
    return bytes(html)


def _query_list(queries) -> List[str]:
    """Reject a bare selector string before ``list()`` explodes it into characters, and a
    ``{name: selector}`` dict before ``list()`` silently keeps its *keys* as the selectors."""
    if isinstance(queries, (str, bytes)):
        raise TypeError(
            f"frostwork: `queries` must be an iterable of selector strings, got a single "
            f"{type(queries).__name__} — wrap it in a list: extract(html, [{queries!r}])"
        )
    if isinstance(queries, Mapping):
        raise TypeError(
            "frostwork: `queries` must be an iterable of selector strings, got a Mapping — "
            "iterating it would use its KEYS (the field names) as selectors. `extract` returns "
            "positional columns, so pass `list(queries.values())`; for a named "
            "`{name: selector}` schema use `frostwork.Page` (or `frostwork.check` to audit one)."
        )
    return list(queries)


def _check_encoding(html: Bytesish, encoding: Optional[str]) -> Optional[str]:
    """Validate a caller charset label instead of letting the engine silently ignore it.

    The engine accepts WHATWG charset labels; Python codec spellings (``latin-1``, ``utf_8``) are
    normalized through :mod:`codecs`. A label that names no encoding at all raises rather than
    silently falling through to BOM/``<meta>`` sniffing, and a non-UTF-8 label combined with
    already-decoded ``str`` input raises rather than silently double-transcoding.

    A label that IS a real encoding but not a WHATWG one is a third case, and it must not raise: the
    documented input here is what Scrapy passes from ``Content-Type``, i.e. whatever
    ``w3lib.encoding.resolve_encoding`` returned, and that resolves against Python's codec set — which
    has ``utf-7`` and ``utf-32`` where WHATWG deliberately does not. A crawled page whose HTTP header
    said ``charset=UTF-7`` (its own ``<meta>`` said UTF-8) therefore made ``extract`` raise on
    documented usage. WHATWG's rule for such a label is *failure, continue* — ignore it and go on
    sniffing, which is what browsers do, what the Rust core already did, and what reads this page
    correctly. Raising on publisher-controlled input is the one thing the no-fallback contract rules
    out: never an error, never a wrong value.
    """
    if encoding is None:
        return None
    canonical = _resolve_label(encoding)
    if canonical is None:
        try:
            python_name = codecs.lookup(encoding).name
        except LookupError:
            raise ValueError(
                f"frostwork: unknown encoding label {encoding!r} — pass a WHATWG charset label "
                "(e.g. 'utf-8', 'windows-1252', 'shift_jis') or None to sniff from BOM/<meta>"
            ) from None
        canonical = _resolve_label(python_name)
        if canonical is None:
            return None  # real encoding, not a WHATWG one -> failure, continue (sniff)
    if isinstance(html, str) and canonical != "UTF-8":
        raise ValueError(
            f"frostwork: `html` is already-decoded str (tokenized as UTF-8), but "
            f"encoding={encoding!r} was given — pass the original bytes with the label, "
            "or drop the label"
        )
    return canonical


def _group_list(groups) -> list:
    """Normalize group shapes (tuples or lists, e.g. straight from JSON) and fail with a clear
    message on the wrong shape instead of an opaque unhashable/unpack error downstream."""
    norm = []
    for g in groups:
        try:
            container, subfields = g
        except (TypeError, ValueError):
            raise TypeError(
                "frostwork: each group must be (container_selector, [(name, selector), ...]); "
                f"got {g!r}"
            ) from None
        subs = []
        for sf in subfields:
            sub = tuple(sf)
            if len(sub) != 2:
                raise TypeError(
                    "frostwork: each group sub-field must be a (name, selector) pair; "
                    f"got {sf!r}"
                )
            subs.append(sub)
        norm.append((container, subs))
    return norm


@lru_cache(maxsize=256)
def _validate_flat(selectors: Tuple[str, ...]) -> None:
    """Cache successful primitive-schema validation; selectors are commonly reused per response."""
    check(selectors).raise_for_status()


@lru_cache(maxsize=128)
def _validate_grouped(
    selectors: Tuple[str, ...], groups: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]
) -> None:
    check(selectors, groups).raise_for_status()


def extract(
    html: Bytesish,
    queries: Iterable[str],
    encoding: Optional[str] = None,
    *,
    strict: bool = True,
) -> List[List[str]]:
    """One streaming pass over ``html``: return one value-column per query, in query order.

    ``html`` is bytes (preferred — Frostwork tokenizes raw bytes) or ``str`` (encoded UTF-8).
    ``queries`` is an iterable of CSS/XPath selectors. ``encoding`` is an optional charset label
    (as Scrapy passes from ``Content-Type``); ``None`` sniffs (BOM → ``<meta>`` → UTF-8).
    Unsupported queries raise :class:`UnsupportedSelector` before scanning. Pass ``strict=False``
    to use the engine's permissive empty-column behavior. There is never a parser fallback.
    """
    query_list = _query_list(queries)
    encoding = _check_encoding(html, encoding)
    if strict:
        _validate_flat(tuple(query_list))
    return _extract(_as_bytes(html), query_list, encoding)


def extract_grouped(
    html: Bytesish,
    queries: Iterable[str],
    groups: Iterable[Tuple[str, Iterable[Tuple[str, str]]]],
    encoding: Optional[str] = None,
    *,
    strict: bool = True,
) -> Tuple[List[List[str]], list]:
    """One streaming pass returning ``(flat_columns, grouped)``. ``groups`` is a list of
    ``(container_selector, [(subfield_name, subfield_selector), ...])``; for every element matching
    ``container_selector`` (document order) each sub-field is extracted **scoped to it**
    (descendant-or-self). ``grouped[g]`` is that group's rows, each a list of sub-field value-columns
    (``[group][row][subfield][value]``). Unsupported selectors raise by default; pass
    ``strict=False`` for permissive empty columns. Same no-DOM, no-fallback semantics as
    :func:`extract`."""
    query_list = _query_list(queries)
    group_list = _group_list(groups)
    encoding = _check_encoding(html, encoding)
    if strict:
        group_key = tuple((container, tuple(subfields)) for container, subfields in group_list)
        _validate_grouped(tuple(query_list), group_key)
    return _extract_grouped(_as_bytes(html), query_list, group_list, encoding)


# --------------------------------------------------------------------------- schema audit / validation
#
# Frostwork has NO fallback: the engine represents an unsupported selector as an empty column,
# indistinguishable from a field that is legitimately empty (or a page whose layout changed). Public
# Python extraction fails fast by default; :func:`check` exposes the same audit as a structured report
# for inspection and CI. The
# supported/unsupported *decision* is authoritative (it is the real compiler); each ``reason`` is a
# best-effort explanation.


class UnsupportedSelector(ValueError):
    """Raised by default when a schema contains unsupported selectors or exceeds the engine budget.
    Pass ``strict=False`` to extraction APIs to opt into permissive empty results."""


@dataclass(frozen=True)
class FieldReport:
    """Audit of one selector: its ``name`` (field/sub-field name, or ``"<container>"``), the
    ``selector`` string, whether it is ``supported``, and an advisory ``reason`` when it is not."""

    name: str
    selector: str
    supported: bool
    reason: Optional[str] = None

    def __str__(self) -> str:
        if self.supported:
            return f"OK        {self.name} = {self.selector!r}"
        return f"UNSUPPORTED {self.name} = {self.selector!r}\n              -> {self.reason}"


@dataclass(frozen=True)
class GroupReport:
    """Audit of a ``many``/``one`` group: its ``container`` selector and each ``subfield``."""

    name: str
    container: FieldReport
    subfields: List[FieldReport] = _dc_field(default_factory=list)

    def unsupported(self) -> List[FieldReport]:
        out = [] if self.container.supported else [self.container]
        out.extend(sf for sf in self.subfields if not sf.supported)
        return out


@dataclass(frozen=True)
class SchemaReport:
    """The result of auditing a whole schema: per-selector support plus budget usage. Use
    :attr:`ok`, :attr:`unsupported`, and :meth:`raise_for_status`."""

    fields: List[FieldReport]
    groups: List[GroupReport]
    members: int
    max_members: int
    sib_bits: int
    max_sib_bits: int

    @property
    def over_budget(self) -> bool:
        """True if the schema needs more member selectors / sibling-combinator bits than the engine's
        fixed-width budget allows (a *caller* bug — too many selectors — distinct from unsupported)."""
        return self.members > self.max_members or self.sib_bits > self.max_sib_bits

    @property
    def unsupported(self) -> List[FieldReport]:
        """Every unsupported selector across flat fields, group containers, and sub-fields."""
        out = [f for f in self.fields if not f.supported]
        for g in self.groups:
            out.extend(g.unsupported())
        return out

    @property
    def ok(self) -> bool:
        """True iff every selector is supported and the schema fits the budget."""
        return not self.over_budget and not self.unsupported

    def raise_for_status(self) -> "SchemaReport":
        """Raise :class:`UnsupportedSelector` if the schema is not :attr:`ok`; else return ``self``."""
        if self.ok:
            return self
        raise UnsupportedSelector(self._problem_message())

    def _problem_message(self) -> str:
        lines = []
        if self.over_budget:
            lines.append(
                f"schema over budget: {self.members}/{self.max_members} member selectors, "
                f"{self.sib_bits}/{self.max_sib_bits} sibling-combinator bits"
            )
        for f in self.unsupported:
            lines.append(f"unsupported selector {f.name!r} = {f.selector!r}: {f.reason}")
        return (
            "Frostwork schema has problems:\n  - "
            + "\n  - ".join(lines)
            + "\nPass strict=False explicitly to allow permissive empty results."
        )

    def __str__(self) -> str:
        head = (
            f"SchemaReport: {'OK' if self.ok else 'PROBLEMS'} "
            f"(members {self.members}/{self.max_members}, sib-bits {self.sib_bits}/{self.max_sib_bits})"
        )
        rows = [str(f) for f in self.fields]
        for g in self.groups:
            rows.append(f"group {g.name!r} container: {str(g.container)}")
            rows.extend("  " + str(sf) for sf in g.subfields)
        return head + "\n" + "\n".join(rows) if rows else head


def _field_reports(names, selectors, tuples) -> List[FieldReport]:
    return [
        FieldReport(n, sel, sup, reason)
        for n, sel, (sup, reason) in zip(names, selectors, tuples)
    ]


def check(queries=None, groups=None) -> SchemaReport:
    """Audit a schema without parsing any HTML: report which selectors the engine supports (with an
    advisory reason for those it does not) and the budget usage.

    ``queries`` is a ``{name: selector}`` mapping, an iterable of flat selectors (labelled ``[i]`` by
    position), or an iterable of ``(name, selector)`` pairs. ``groups`` is a
    ``{name: (container, subfields)}`` mapping, an iterable of ``(name, container, subfields)``
    triples, or the bare ``(container, subfields)`` shape that :func:`extract_grouped` takes
    (auto-named ``group[i]``) — with ``subfields`` given as ``{subname: sel}`` or
    ``[(subname, sel), ...]`` in any of them. Anything else raises :class:`TypeError` naming these
    shapes rather than auditing the wrong strings: a mapping is destructured, never iterated, since
    auditing its *keys* would report a green schema that was never looked at. Returns a
    :class:`SchemaReport`; call :meth:`SchemaReport.raise_for_status` for strict validation.

        >>> import frostwork
        >>> [(f.name, f.supported) for f in frostwork.check({"blurb": ":contains(x)::text"}).fields]
        [('blurb', False)]
    """
    fnames, fsels = _split_named(queries or [])
    gnames, gcontainers, gsub_names, gsub_sels, native_groups = _split_groups(groups or [])
    flat_t, groups_t, (members, max_members, sib_bits, max_sib_bits) = _audit_schema(fsels, native_groups)
    fields = _field_reports(fnames, fsels, flat_t)
    group_reports = []
    for gn, gc, subn, subs, (ctuple, subtuples) in zip(
        gnames, gcontainers, gsub_names, gsub_sels, groups_t
    ):
        container = FieldReport(f"{gn}<container>", gc, ctuple[0], ctuple[1])
        subfields = _field_reports(subn, subs, subtuples)
        group_reports.append(GroupReport(gn, container, subfields))
    return SchemaReport(fields, group_reports, members, max_members, sib_bits, max_sib_bits)


# The shapes `check` accepts, quoted verbatim in the TypeError so a wrong one names its way out. A
# schema shape that is *misread* rather than rejected is the worst outcome here: auditing the field
# names of a `{name: selector}` dict reports every field supported (a bare `title` is a valid type
# selector), so the schema comes back green without ever having been looked at.
_QUERY_SHAPES = "`queries` must be {name: selector}, [selector, ...], or [(name, selector), ...]"
_GROUP_SHAPES = (
    "each group must be {name: (container, subfields)}, (name, container, subfields), or "
    "(container, subfields), where subfields is {sub: sel} or [(sub, sel), ...]"
)


def _as_tuple(x):
    """``tuple(x)`` for a shape check — ``None`` if ``x`` is a string, a Mapping, or not iterable at
    all. Those are all wrong shapes *where this is called*, and each would otherwise "unpack" into
    something plausible-looking: a string into its characters, a 2-key Mapping into its two keys."""
    if isinstance(x, (str, bytes, bytearray, Mapping)):
        return None
    try:
        return tuple(x)
    except TypeError:
        return None


def _pair(item, shapes):
    """Unpack one ``(name, selector)`` pair, naming the accepted ``shapes`` instead of failing with an
    opaque index error (or silently splitting a 2-character string into a name and a selector)."""
    parts = _as_tuple(item)
    if parts is None or len(parts) != 2 or not isinstance(parts[1], str):
        raise TypeError(f"frostwork: {shapes}; got {item!r}")
    return str(parts[0]), parts[1]


def _split_named(queries):
    """Accept `{name: selector}`, `[selector, ...]`, or `[(name, selector), ...]`; return
    (names, selectors).

    A Mapping is read as `{name: selector}` — the shape `Page`/`FrostPage` schemas are written in.
    Iterating it instead would audit the field NAMES as selectors and report the whole schema
    supported (see `_QUERY_SHAPES`), so a Mapping is destructured explicitly, never iterated."""
    if isinstance(queries, Mapping):
        queries = list(queries.items())
    elif isinstance(queries, (str, bytes, bytearray)):
        raise TypeError(
            f"frostwork: {_QUERY_SHAPES}; got a single {type(queries).__name__} "
            f"{queries!r} — wrap it in a list"
        )
    names, sels = [], []
    for i, q in enumerate(queries):
        if isinstance(q, str):
            names.append(f"[{i}]")
            sels.append(q)
        else:
            name, sel = _pair(q, _QUERY_SHAPES)
            names.append(name)
            sels.append(sel)
    return names, sels


def _split_groups(groups):
    """Accept `{name: (container, subfields)}`, `(name, container, subfields)`, or the bare
    `(container, subfields)` shape `extract_grouped` takes (auto-named `group[i]`), where `subfields`
    is `{sub: sel}` or `[(sub, sel), ...]`; return the parallel name lists plus the native
    `(container, [(sub, sel)])` shape `_audit_schema` expects.

    A Mapping is read as `{name: (container, subfields)}` — what `FrostPage.frost_schema()["groups"]`
    returns — for the same reason `_split_named` reads one as `{name: selector}`: iterating it would
    audit the group names instead of the schema."""
    if isinstance(groups, Mapping):
        groups = [_named_group(name, body) for name, body in groups.items()]
    gnames, gcontainers, gsub_names, gsub_sels, native = [], [], [], [], []
    for i, g in enumerate(groups):
        parts = _as_tuple(g)
        if parts is not None and len(parts) == 3:
            name, container, sub = str(parts[0]), parts[1], parts[2]
        elif parts is not None and len(parts) == 2:
            name, container, sub = f"group[{i}]", parts[0], parts[1]
        else:
            raise TypeError(f"frostwork: {_GROUP_SHAPES}; got {g!r}")
        subn, subs = _split_subfields(sub)
        gnames.append(name)
        gcontainers.append(container)
        gsub_names.append(subn)
        gsub_sels.append(subs)
        native.append((container, list(zip(subn, subs))))
    return gnames, gcontainers, gsub_names, gsub_sels, native


def _named_group(name, body) -> tuple:
    """One `{name: (container, subfields)}` entry as the `(name, container, subfields)` triple."""
    parts = _as_tuple(body)
    if parts is None or len(parts) != 2:
        raise TypeError(f"frostwork: {_GROUP_SHAPES}; got {{{name!r}: {body!r}}}")
    return (name, parts[0], parts[1])


def _split_subfields(sub):
    """A group's sub-fields — `{sub: sel}` or `[(sub, sel), ...]` — as parallel (names, selectors)."""
    items = tuple(sub.items()) if isinstance(sub, Mapping) else _as_tuple(sub)
    if items is None:
        raise TypeError(f"frostwork: {_GROUP_SHAPES}; got subfields {sub!r}")
    pairs = [_pair(sf, _GROUP_SHAPES) for sf in items]
    return [n for n, _s in pairs], [s for _n, s in pairs]


_Transforms = Tuple[Callable, ...]


def _shape(col: List[str], card: _Card, transforms: _Transforms = ()) -> Any:
    """Shape a raw column by cardinality, then apply transforms. The return type is genuinely `Any`: it is
    `list[str]`, `str` or `str | None` depending on `card`, and a transform can make it anything at all.
    Callers that know their cardinality statically get the precise type from `webpoet.field`'s overloads."""
    kind, arg = card
    value: Any
    if kind == "all":
        value = list(col)
    elif kind == "join":
        # `card` is built only by `field()`/`Page.field_join`, which always pair "join" with a separator
        value = (arg or "").join(col)
    else:
        value = col[0] if col else None  # "first"
    for fn in transforms:
        value = fn(value)
    return value


def _sub_spec(spec) -> Tuple[str, _Card]:
    """Normalize a `many`/`one` sub-field spec into ``(selector, cardinality)``. Accepts a bare
    selector string (first match — the back-compatible default the demo uses) or a tuple:
    ``(sel,)`` / ``(sel, "first")`` → first, ``(sel, "all")`` → list, ``(sel, "join", sep)`` → joined.
    This is what lets ``Page.many`` express the same per-subfield cardinality as ``webpoet.Many``."""
    if isinstance(spec, str):
        return spec, ("first", None)
    sel = spec[0]
    kind = spec[1] if len(spec) > 1 else "first"
    if kind == "join":
        return sel, ("join", spec[2] if len(spec) > 2 else "")
    if kind in ("all", "first"):
        return sel, (kind, None)
    raise ValueError(f"many/one sub-spec {spec!r}: cardinality must be 'first', 'all', or 'join'")


class Page:
    """An ordered ``{name -> (selector, cardinality)}`` schema. Build it once with the ``field*``
    methods (chainable), then call :meth:`extract` per page; reuse one ``Page`` across responses."""

    __slots__ = (
        "_names", "_queries", "_cards", "_transforms", "_groups", "_plan", "_strict", "_validated"
    )

    def __init__(self, *, strict: bool = True) -> None:
        self._names: List[str] = []
        self._queries: List[str] = []
        self._cards: List[_Card] = []
        self._transforms: List[_Transforms] = []
        self._groups: List[dict] = []  # {name, container, subfields: {subname: selector}, one}
        self._plan = None  # native compiled Plan, built lazily on first extract, reused after
        self._strict = strict
        self._validated = False

    def _add(self, name: str, selector: str, card: _Card, transforms: _Transforms) -> "Page":
        self._ensure_new_name(name)
        self._names.append(name)
        self._queries.append(selector)
        self._cards.append(card)
        self._transforms.append(transforms)
        self._invalidate()
        return self

    def _invalidate(self) -> None:
        """The schema changed: drop the compiled plan and the cached strict-validation result, so
        neither an old plan nor an old green verdict can outlive the selectors they were built from."""
        self._plan = None
        self._validated = False

    def _ensure_new_name(self, name: str) -> None:
        """Reject ambiguous flat/group collisions before they can overwrite in ``Item.to_dict``."""
        if name in self._names or any(g["name"] == name for g in self._groups):
            raise ValueError(f"duplicate Page field/group name: {name!r}")

    def _get_plan(self):
        """The schema compiled to a native ``Plan`` ONCE, cached across pages (rebuilt only if the
        schema changed). This is what turns per-page recompilation into per-schema compilation."""
        if self._plan is None:
            garg = [
                (g["container"], [(sn, sel) for sn, (sel, _c) in g["subfields"].items()])
                for g in self._groups
            ]
            self._plan = _Plan(self._queries, garg)
        return self._plan

    def field(self, name: str, selector: str, *, map: Optional[Callable] = None) -> "Page":
        """Single-valued field: :meth:`Item.value` returns its first match (or ``None``). ``map``
        is an optional transform applied to that shaped value."""
        return self._add(name, selector, ("first", None), (map,) if map else ())

    def field_all(self, name: str, selector: str, *, map: Optional[Callable] = None) -> "Page":
        """Multi-valued field: :meth:`Item.value` returns every match in document order. ``map`` (if
        given) is applied to the whole list."""
        return self._add(name, selector, ("all", None), (map,) if map else ())

    def field_join(
        self, name: str, selector: str, separator: str = "", *, map: Optional[Callable] = None
    ) -> "Page":
        """Field that joins every match with ``separator`` into one string (empty column -> ``""``).
        ``map`` (if given) is applied to the joined string."""
        return self._add(name, selector, ("join", separator), (map,) if map else ())

    def many(self, name: str, container: str, subfields: dict) -> "Page":
        """Add a repeated nested field: for every element matching ``container`` (document order),
        extract each ``subfields`` entry **scoped to it** (descendant-or-self). :meth:`Item.value`
        returns a ``list`` of ``dict`` rows. All in the same streaming pass.

        Each ``subfields`` value is a bare selector string (first match — the default) OR a tuple
        carrying per-subfield cardinality — ``(sel, "all")`` for a list, ``(sel, "join", sep)`` for a
        joined string — so ``Page.many`` matches ``webpoet.Many``'s expressiveness::

            .many("offers", ".offer", {"price": ".p::text", "tags": (".tag::text", "all")})
        """
        return self._add_group(name, container, subfields, one=False)

    def one(self, name: str, container: str, subfields: dict) -> "Page":
        """Like :meth:`many`, but :meth:`Item.value` returns the **first** container's ``dict`` row, or
        ``None`` if none match. Same rich sub-specs as :meth:`many`."""
        return self._add_group(name, container, subfields, one=True)

    def _add_group(self, name: str, container: str, subfields: dict, *, one: bool) -> "Page":
        self._ensure_new_name(name)
        subs = {sn: _sub_spec(spec) for sn, spec in subfields.items()}
        self._groups.append({"name": name, "container": container, "subfields": subs, "one": one})
        self._invalidate()
        return self

    @property
    def field_names(self) -> List[str]:
        return list(self._names) + [g["name"] for g in self._groups]

    def check(self) -> SchemaReport:
        """Audit this page's whole schema (flat fields + ``many``/``one`` groups) without touching any
        HTML: which selectors are supported, advisory reasons for those that are not, and budget usage.
        See :class:`SchemaReport`."""
        named_queries = list(zip(self._names, self._queries))
        named_groups = [
            (g["name"], g["container"], {sn: sel for sn, (sel, _c) in g["subfields"].items()})
            for g in self._groups
        ]
        return check(named_queries, named_groups)

    def extract(
        self, html: Bytesish, encoding: Optional[str] = None, *, strict: Optional[bool] = None
    ) -> "Item":
        """Fill an :class:`Item` from ``html`` in one streaming pass. ``encoding`` is an optional
        charset label (as Scrapy passes from ``Content-Type``); ``None`` sniffs. Unsupported selectors
        raise :class:`UnsupportedSelector` by default. Construct with ``Page(strict=False)`` or pass
        ``strict=False`` here for permissive empty results. A successful default validation is cached
        until the schema changes."""
        use_strict = self._strict if strict is None else strict
        if use_strict and not self._validated:
            self.check().raise_for_status()
            self._validated = True
        encoding = _check_encoding(html, encoding)
        body = _as_bytes(html)
        plan = self._get_plan()  # compiled once, reused across pages
        if not self._groups:
            cols = plan.extract(body, encoding)
            return Item(list(self._names), list(self._cards), cols, list(self._transforms))
        flat_cols, grouped = plan.extract_grouped(body, encoding)
        gout: dict = {}
        for g, rows in zip(self._groups, grouped):
            subitems = list(g["subfields"].items())  # [(subname, (selector, card))]
            shaped = [{sn: _shape(col, card) for (sn, (_s, card)), col in zip(subitems, row)} for row in rows]
            gout[g["name"]] = (shaped[0] if shaped else None) if g["one"] else shaped
        return Item(list(self._names), list(self._cards), flat_cols, list(self._transforms), gout)

    def __len__(self) -> int:
        return len(self._names) + len(self._groups)

    def __repr__(self) -> str:
        fields = ", ".join(f"{n}={q!r}" for n, q in zip(self._names, self._queries))
        return f"Page({fields})"


class Item:
    """Values extracted for one page — one column per declared field. Look fields up by name with
    :meth:`get` / :meth:`get_all`, or take the whole item with :meth:`to_dict` / :meth:`to_json`."""

    __slots__ = ("_names", "_cards", "_cols", "_transforms", "_grouped")

    def __init__(
        self, names: List[str], cards: List[_Card], cols: List[List[str]],
        transforms: Optional[List[_Transforms]] = None,
        grouped: Optional[dict] = None,
    ) -> None:
        self._names = names
        self._cards = cards
        self._cols = cols
        self._transforms = transforms if transforms is not None else [()] * len(names)
        self._grouped = grouped or {}  # name -> list[dict] (many) | dict|None (one)

    def _index(self, name: str) -> Optional[int]:
        try:
            return self._names.index(name)
        except ValueError:
            return None

    def get(self, name: str):
        """First value for ``name``. For a flat field: its first matched string (or ``None``). For a
        `many`/`one` group: the first row (a ``dict``), or ``None`` if none. Cardinality-independent."""
        if name in self._grouped:
            g = self._grouped[name]
            if isinstance(g, list):  # many -> first row
                return g[0] if g else None
            return g  # one -> the row dict (or None)
        i = self._index(name)
        if i is None:
            return None
        col = self._cols[i]
        return col[0] if col else None

    def get_all(self, name: str) -> list:
        """Every value for ``name``. For a flat field: all matched strings. For a `many`/`one` group:
        the row ``dict``s (a `one` group yields a 0- or 1-element list). ``[]`` if absent."""
        if name in self._grouped:
            g = self._grouped[name]
            if isinstance(g, list):  # many -> all rows
                return list(g)
            return [g] if g is not None else []  # one -> single-row list (or empty)
        i = self._index(name)
        return list(self._cols[i]) if i is not None else []

    def value(self, name: str):
        """Cardinality-aware value for ``name`` (respects first/all/join and any ``map=`` transform),
        a `many`/`one` group's rows, or ``None`` if absent. For flat fields, ``get``/``get_all`` return
        raw untransformed matches; for groups they return the first/all rows."""
        if name in self._grouped:
            return self._grouped[name]
        i = self._index(name)
        return None if i is None else _shape(self._cols[i], self._cards[i], self._transforms[i])

    def empty_fields(self) -> List[str]:
        """Declared fields that matched **nothing** on this page (a `many`/`one` group with no rows
        counts as empty; a field that matched an empty string does not — it matched).

        This is the runtime half of dead-selector detection. Frostwork has no fallback, so an empty
        column under ``strict=False`` can mean two very different things — and they are distinguishable,
        because support is *static*: audit the schema once with :meth:`Page.check` (or
        ``frostwork-audit``) and any field it reports **supported** that is empty here is a selector that
        no longer matches the page, not an engine gap. Under the default ``strict=True`` the unsupported
        case cannot arise at all, so every name returned here is a dead selector::

            report = page.check()                     # once, at startup / in CI
            item = page.extract(html)
            for name in item.empty_fields():          # per response
                log.warning("selector matched nothing: %s", name)
        """
        out = [n for n, col in zip(self._names, self._cols) if not col]
        for name, g in self._grouped.items():
            if not g:  # [] for an empty `many`, None for a `one` with no container
                out.append(name)
        return out

    def to_dict(self) -> dict:
        """The whole item as a dict: flat fields shaped by cardinality/``map=``, plus `many`/`one`
        groups as row lists / a row / ``None``."""
        d = {n: _shape(c, card, tf)
             for n, card, c, tf in zip(self._names, self._cards, self._cols, self._transforms)}
        d.update(self._grouped)
        return d

    def to_json(self, **kwargs) -> str:
        """``to_dict()`` serialized to JSON (UTF-8 preserved). Extra kwargs go to ``json.dumps``."""
        kwargs.setdefault("ensure_ascii", False)
        return json.dumps(self.to_dict(), **kwargs)

    def __iter__(self) -> Iterator[Tuple[str, object]]:
        return iter(self.to_dict().items())

    def __len__(self) -> int:
        return len(self._names) + len(self._grouped)

    def __repr__(self) -> str:
        return f"Item({self.to_dict()!r})"
