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
from typing import Any, Callable, Iterable, Iterator, List, Optional, Protocol, Tuple, Union

from .validation import FieldProcessingError, ValidationReport, validate_item

from ._frostwork import Plan as _Plan
from ._frostwork import audit_schema as _audit_schema
from ._frostwork import detect_encoding as _detect_encoding
from ._frostwork import extract as _extract
from ._frostwork import extract_grouped as _extract_grouped
from ._frostwork import resolve_label as _resolve_label

__all__ = [
    "extract", "extract_grouped", "check", "detect_encoding", "Page", "Item",
    "SchemaReport", "FieldReport", "GroupReport", "UnsupportedSelector",
]

# A field's cardinality: ("first", None) | ("all", None) | ("join", separator).
_Card = Tuple[str, Optional[str]]
_Subfields = Union[Mapping[str, str], Iterable[Tuple[str, str]]]

Bytesish = Union[bytes, bytearray, memoryview, str]


class Response(Protocol):
    """The byte-response surface used by :meth:`Page.extract_response`; no Scrapy dependency."""

    @property
    def body(self) -> bytes: ...

    @property
    def encoding(self) -> Optional[str]: ...


def _as_scan_input(html: Bytesish) -> Union[bytes, str]:
    """What the engine scans. `bytes` and `str` both cross the FFI boundary as-is — the native layer
    borrows a `str`'s UTF-8 view instead of allocating a second copy of the document (see the `Html`
    enum in `src/python.rs`), so a caller holding already-decoded text should NOT pre-encode it. Only
    the remaining bytes-likes need materializing."""
    if isinstance(html, (bytes, str)):
        return html
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
        container, subfields = _group_body(g, _EXTRACT_GROUP_SHAPES)
        norm.append((container, _subfields(subfields, _EXTRACT_GROUP_SHAPES)))
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


def detect_encoding(html: Bytesish, encoding: Optional[str] = None) -> str:
    """The encoding :func:`extract` would scan ``html`` with, as a WHATWG name (``"windows-1252"``).

    BOM → BOM-less UTF-16 prefix → ``encoding`` label → 4096-byte ``<meta>``/XML-declaration prescan →
    UTF-8. Exposed on its own because nothing else in a scraper's stack answers this the way a browser
    does: ``parsel.Selector(body=…)`` never sniffs (it defaults to UTF-8), and w3lib — what Scrapy
    uses — stops at ``<body>`` and at the first declaration it cannot resolve. See the Encoding section
    of docs/COMPATIBILITY.md for the enumerated differences.

    Always returns a real encoding. A label naming none is ignored rather than propagated (WHATWG's
    "failure, continue"), matching what :func:`extract` then does with the document; the stricter
    validation :func:`extract` applies to a *caller's* label is deliberately not repeated here, since
    the question this answers is "what will be used", not "is this input acceptable".
    """
    label = encoding
    if label is not None and _resolve_label(label) is None:
        try:  # a Python codec spelling (`latin-1`, `utf_8`) is normalized the way `extract` does
            label = codecs.lookup(label).name
        except LookupError:
            label = None
    return _detect_encoding(_as_scan_input(html), label)


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
    return _extract(_as_scan_input(html), query_list, encoding)


def extract_grouped(
    html: Bytesish,
    queries: Iterable[str],
    groups: Iterable[Tuple[str, _Subfields]],
    encoding: Optional[str] = None,
    *,
    strict: bool = True,
) -> Tuple[List[List[str]], list]:
    """One streaming pass returning ``(flat_columns, grouped)``. ``groups`` is a list of
    ``(container_selector, [(subfield_name, subfield_selector), ...])`` (a subfield mapping is also
    accepted); for every element matching
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
    return _extract_grouped(_as_scan_input(html), query_list, group_list, encoding)


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


def _field_reports(fields, tuples) -> List[FieldReport]:
    return [
        FieldReport(n, sel, sup, reason)
        for (n, sel), (sup, reason) in zip(fields, tuples)
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
    named_fields = _named_fields(queries or [])
    named_groups = _named_groups(groups or [])
    flat_t, groups_t, (members, max_members, sib_bits, max_sib_bits) = _audit_schema(
        [sel for _name, sel in named_fields], [(c, subs) for _name, c, subs in named_groups]
    )
    fields = _field_reports(named_fields, flat_t)
    group_reports = []
    for (gn, gc, subs), (ctuple, subtuples) in zip(named_groups, groups_t):
        container = FieldReport(f"{gn}<container>", gc, ctuple[0], ctuple[1])
        subfields = _field_reports(subs, subtuples)
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
_EXTRACT_GROUP_SHAPES = (
    "each group must be (container, subfields), where subfields is {sub: sel} or [(sub, sel), ...]"
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


def _named_fields(queries):
    """Accept `{name: selector}`, `[selector, ...]`, or `[(name, selector), ...]`; keep each
    name paired with its selector through normalization and report construction.

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
    return [(f"[{i}]", q) if isinstance(q, str) else _pair(q, _QUERY_SHAPES)
            for i, q in enumerate(queries)]


def _named_groups(groups):
    """Accept `{name: (container, subfields)}`, `(name, container, subfields)`, or the bare
    `(container, subfields)` shape `extract_grouped` takes (auto-named `group[i]`), where `subfields`
    is `{sub: sel}` or `[(sub, sel), ...]`; return `(name, container, [(sub, sel)])` records.

    A Mapping is read as `{name: (container, subfields)}` — what `FrostPage.frost_schema()["groups"]`
    returns — for the same reason `_named_fields` reads one as `{name: selector}`: iterating it would
    audit the group names instead of the schema."""
    if isinstance(groups, Mapping):
        groups = [(str(name), *_group_body(body)) for name, body in groups.items()]
    named = []
    for i, g in enumerate(groups):
        parts = _as_tuple(g)
        if parts is not None and len(parts) == 3:
            name, body = str(parts[0]), parts[1:]
        else:
            name, body = f"group[{i}]", parts
        container, sub = _group_body(body)
        named.append((name, container, _subfields(sub)))
    return named


def _group_body(body, shapes=_GROUP_SHAPES) -> tuple:
    """The `(container, subfields)` shape shared by auditing and extraction."""
    parts = _as_tuple(body)
    if parts is None or len(parts) != 2 or not isinstance(parts[0], str):
        raise TypeError(f"frostwork: {shapes}; got {body!r}")
    return parts


def _subfields(sub, shapes=_GROUP_SHAPES):
    """A group's sub-fields — `{sub: sel}` or `[(sub, sel), ...]` — as `(name, selector)` pairs."""
    items = tuple(sub.items()) if isinstance(sub, Mapping) else _as_tuple(sub)
    if items is None:
        raise TypeError(f"frostwork: {shapes}; got subfields {sub!r}")
    return [_pair(sf, shapes) for sf in items]


_Transforms = Tuple[Callable, ...]


@dataclass(frozen=True, slots=True)
class _Field:
    selector: str
    card: _Card
    transforms: _Transforms = ()
    index: int = 0  # position in the native result columns, assigned when the schema is built

    def value(self, name: str, col: List[str]):
        try:
            return _shape(col, self.card, self.transforms)
        except Exception as exc:
            raise FieldProcessingError(name, self.selector, exc) from exc


@dataclass(frozen=True, slots=True)
class _Group:
    container: str
    subfields: dict[str, _Field]
    one: bool


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

    __slots__ = ("_fields", "_groups", "_plan", "_strict", "_validated")

    def __init__(self, *, strict: bool = True) -> None:
        self._fields: dict[str, _Field] = {}
        self._groups: dict[str, _Group] = {}
        self._plan = None  # native compiled Plan, built lazily on first extract, reused after
        self._strict = strict
        self._validated = False

    def _add(self, name: str, selector: str, card: _Card, transforms: _Transforms) -> "Page":
        self._ensure_new_name(name)
        # Replace rather than mutate: extracted Items can share this schema without per-response
        # metadata copies, and keep their original declarations if the Page is extended later.
        self._fields = {**self._fields, name: _Field(selector, card, transforms, len(self._fields))}
        self._invalidate()
        return self

    def _invalidate(self) -> None:
        """The schema changed: drop the compiled plan and the cached strict-validation result, so
        neither an old plan nor an old green verdict can outlive the selectors they were built from."""
        self._plan = None
        self._validated = False

    def _ensure_new_name(self, name: str) -> None:
        """Reject ambiguous flat/group collisions before they can overwrite in ``Item.to_dict``."""
        if name in self._fields or name in self._groups:
            raise ValueError(f"duplicate Page field/group name: {name!r}")

    def _get_plan(self):
        """The schema compiled to a native ``Plan`` ONCE, cached across pages (rebuilt only if the
        schema changed). This is what turns per-page recompilation into per-schema compilation.

        Per-column cardinality goes down with it, because that is what makes EARLY EXIT sound: a schema
        of nothing but single-valued fields is finished as soon as each has a value, and the engine can
        stop tokenizing rather than run to EOF. One ``field_all``/``field_join`` — or one group, or one
        deferred selector — leaves early exit unarmed, since those consumers read the whole column.
        Immediate first-value text/attribute fields can still stop retaining later matches in a mixed
        schema; deferred/normalized/mixed deferred columns disable that optimization.
        """
        if self._plan is None:
            garg = [
                (g.container, [(sn, sub.selector) for sn, sub in g.subfields.items()])
                for g in self._groups.values()
            ]
            first_only = [f.card[0] == "first" for f in self._fields.values()]
            self._plan = _Plan([f.selector for f in self._fields.values()], garg, first_only)
        return self._plan

    def field(self, name: str, selector: str, *, map: Optional[Callable] = None) -> "Page":
        """Single-valued field: :meth:`Item.value` returns its first match (or ``None``). ``map``
        is an optional transform applied to that shaped value. :meth:`Item.get_all` returns at most
        that first raw match; use :meth:`field_all` to request every match."""
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
        subs = {sn: _Field(*_sub_spec(spec), index=i) for i, (sn, spec) in enumerate(subfields.items())}
        self._groups = {**self._groups, name: _Group(container, subs, one)}
        self._invalidate()
        return self

    @property
    def field_names(self) -> List[str]:
        return [*self._fields, *self._groups]

    def check(self) -> SchemaReport:
        """Audit this page's whole schema (flat fields + ``many``/``one`` groups) without touching any
        HTML: which selectors are supported, advisory reasons for those that are not, and budget usage.
        See :class:`SchemaReport`."""
        schema = self.frost_schema()
        return check(schema["fields"], schema["groups"])

    def frost_schema(self) -> dict:
        """Named selectors in the same audit format as ``FrostFields.frost_schema()``."""
        return {
            "fields": {name: f.selector for name, f in self._fields.items()},
            "groups": {name: (g.container, {sn: sub.selector for sn, sub in g.subfields.items()})
                       for name, g in self._groups.items()},
        }

    def extract_response(self, response: Response, *, strict: Optional[bool] = None) -> "Item":
        """Extract original response bytes with its encoding (Scrapy or web-poet, without imports).

        Never accesses ``response.text`` or its selector. To use Frostwork's own sniffing instead,
        call ``page.extract(response.body)`` explicitly.
        """
        return self.extract(response.body, encoding=response.encoding, strict=strict)

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
        body = _as_scan_input(html)
        plan = self._get_plan()  # compiled once, reused across pages
        if not self._groups:
            return Item._from_columns(self._fields, plan.extract(body, encoding))
        flat_cols, grouped = plan.extract_grouped(body, encoding)
        gout: dict = {}
        for (name, g), rows in zip(self._groups.items(), grouped):
            shaped = [{sn: _shape(col, sub.card) for (sn, sub), col in zip(g.subfields.items(), row)}
                      for row in (rows[:1] if g.one else rows)]
            gout[name] = (shaped[0] if shaped else None) if g.one else shaped
        return Item._from_columns(self._fields, flat_cols, gout,
                                  {name: (g.one, tuple(g.subfields)) for name, g in self._groups.items()})

    def __len__(self) -> int:
        return len(self._fields) + len(self._groups)

    def __repr__(self) -> str:
        fields = ", ".join(f"{n}={f.selector!r}" for n, f in self._fields.items())
        return f"Page({fields})"


class Item:
    """Values extracted for one page — one column per declared field. Look fields up by name with
    :meth:`get` / :meth:`get_all`, or take the whole item with :meth:`to_dict` / :meth:`to_json`."""

    __slots__ = ("_fields", "_cols", "_grouped", "_group_specs")

    def __init__(
        self, names: List[str], cards: List[_Card], cols: List[List[str]],
        transforms: Optional[List[_Transforms]] = None,
        grouped: Optional[dict] = None,
        *, selectors: Optional[List[str]] = None,
        group_specs: Optional[dict] = None,
    ) -> None:
        selectors = selectors or [""] * len(names)
        transforms = transforms if transforms is not None else [()] * len(names)
        if not (len(names) == len(cards) == len(selectors) == len(transforms)):
            raise ValueError("Item needs one selector, cardinality and transform sequence per field")
        fields = {n: _Field(sel, card, funcs, i) for i, (n, sel, card, funcs) in enumerate(zip(
            names, selectors, cards, transforms
        ))}
        if len(fields) != len(names):
            raise ValueError("Item field names must be unique")
        self._set_columns(fields, cols, grouped, group_specs)

    def _set_columns(self, fields: dict[str, _Field], cols, grouped, group_specs) -> None:
        if len(cols) != len(fields):
            raise ValueError("Item needs exactly one value column per field")
        self._fields = fields
        self._cols = cols
        self._grouped = grouped or {}  # name -> list[dict] (many) | dict|None (one)
        self._group_specs = group_specs or {}

    @classmethod
    def _from_columns(cls, fields: dict[str, _Field], cols, grouped=None, group_specs=None) -> "Item":
        """Attach columns to the Page's stable schema without rebuilding its field definitions."""
        item = cls.__new__(cls)
        item._set_columns(fields, cols, grouped, group_specs)
        return item

    def get(self, name: str):
        """First value for ``name``. For a flat field: its first matched string (or ``None``). For a
        `many`/`one` group: the first row (a ``dict``), or ``None`` if none. Cardinality-independent."""
        if name in self._grouped:
            g = self._grouped[name]
            if isinstance(g, list):  # many -> first row
                return g[0] if g else None
            return g  # one -> the row dict (or None)
        field = self._fields.get(name)
        col = self._cols[field.index] if field is not None else []
        return col[0] if col else None

    def get_all(self, name: str) -> list:
        """Raw values requested by ``name``'s declaration, before joining or ``map=`` transforms.

        A ``field`` returns zero or one match; ``field_all`` and ``field_join`` return every match
        in document order. This does not depend on whether the scan exits early. For a ``many``/``one``
        group, return the row dicts (``one`` yields zero or one row). ``[]`` if absent.
        """
        if name in self._grouped:
            g = self._grouped[name]
            if isinstance(g, list):  # many -> all rows
                return list(g)
            return [g] if g is not None else []  # one -> single-row list (or empty)
        field = self._fields.get(name)
        if field is None:
            return []
        col = self._cols[field.index]
        return col[:1] if field.card[0] == "first" else list(col)

    def value(self, name: str):
        """Cardinality-aware value for ``name`` (respects first/all/join and any ``map=`` transform),
        a `many`/`one` group's rows, or ``None`` if absent. For flat fields, ``get``/``get_all`` return
        untransformed matches within the declared cardinality; for groups they return the first/all
        rows (``one`` retains at most one row)."""
        if name in self._grouped:
            return self._grouped[name]
        field = self._fields.get(name)
        return field.value(name, self._cols[field.index]) if field is not None else None

    def validate(
        self, *, required: Iterable[str] = (),
        counts: Optional[Mapping[str, Tuple[int, Optional[int]]]] = None,
        group_required: Optional[Mapping[str, Iterable[str]]] = None,
    ) -> ValidationReport:
        """Check processed values, raw match counts and required group subfields.

        Returns a report with the processed ``item``, per-field ``states`` and structured ``issues``.
        ``counts={"images": (1, 8)}`` requires ``field_all``/``field_join`` or ``many``: a first-only
        declaration cannot prove an upper bound. ``group_required={"offers": ["price"]}`` checks
        each matched row; also put ``offers`` in ``required`` to require a container.
        """
        return validate_item(self, required=required, counts=counts, group_required=group_required)

    def empty_fields(self) -> List[str]:
        """Declared fields that matched **nothing** on this page (a `many`/`one` group with no rows
        counts as empty; a field that matched an empty string does not — it matched).

        Audit support once with :meth:`Page.check` (or ``frostwork-audit``), then monitor missing
        matches. An empty result can be legitimate optional content, changed markup, or a parsing
        difference; it does not by itself prove the layout changed. Under ``strict=False`` an
        unsupported selector is another possibility. This method does not apply transforms::

            report = page.check()                     # once, at startup / in CI
            item = page.extract(html)
            for name in item.empty_fields():          # per response
                log.warning("selector matched nothing: %s", name)
        """
        out = [n for n, col in zip(self._fields, self._cols) if not col]
        for name, g in self._grouped.items():
            if g is None or isinstance(g, list) and not g:
                out.append(name)
        return out

    def to_dict(self) -> dict:
        """The whole item as a dict: flat fields shaped by cardinality/``map=``, plus `many`/`one`
        groups as row lists / a row / ``None``."""
        d = {n: field.value(n, col) for (n, field), col in zip(self._fields.items(), self._cols)}
        d.update(self._grouped)
        return d

    def to_json(self, **kwargs) -> str:
        """``to_dict()`` serialized to JSON (UTF-8 preserved). Extra kwargs go to ``json.dumps``."""
        kwargs.setdefault("ensure_ascii", False)
        return json.dumps(self.to_dict(), **kwargs)

    def __iter__(self) -> Iterator[Tuple[str, object]]:
        return iter(self.to_dict().items())

    def __len__(self) -> int:
        return len(self._fields) + len(self._grouped)

    def __repr__(self) -> str:
        return f"Item({self.to_dict()!r})"
