"""Differential gate for the **web-poet integration** — Parsel is the oracle, `to_item()` is the unit.

`diff_lxml.py` gates the engine: does a selector return the same COLUMN as lxml. Nothing gated the layer
above it, so `frostwork.webpoet` shipped five defects that the 100%-green engine gate could not see. Each
was a hand-written list that omitted something, which is the same mistake AGENTS.md records four times in
the engine's own rule tables ("a rule with no name to probe cannot fail a gate"):

  * the universe of CLASS SHAPES a page object can have omitted `@attrs.define` — which recreates the
    class, so `__init_subclass__` re-runs after the markers are gone and the original is not in the new
    MRO. Own fields drop out of the plan and `to_item()` raises `KeyError`.
  * the universe of RESPONSE INPUTS omitted `BrowserResponse` (raises) and web-poet's own `BrowserPage`
    (silently returns `{}`, because it does not inherit `FrostPage.__init_subclass__`).
  * the universe of VALUE TYPES a field processor accepts omitted `Selector`/`SelectorList`/`HtmlElement`.
    web-poet attaches processors BY NAME through a nested `Processors` class, which every
    zyte-common-items base page declares, so this fires with no `out=` written anywhere: the processor
    receives Frostwork's `str`, matches none of its `isinstance` gates, and returns it UNCHANGED
    ("Other inputs are returned as is"). A raw-HTML string lands in a field typed `List[Breadcrumb]`.

So this gate compares the WHOLE ITEM, not a few fields. That is deliberate and load-bearing: a missing
KEY is the failure mode of two of the three defects above, and a per-field sweep that only checks the
fields it knows about cannot see a field that vanished. It is the same lesson as `make gate-seq`
comparing the whole tree instead of a few `::text` columns.

WHAT "EQUIVALENT" MEANS (the oracle's shape)
--------------------------------------------
For each generated schema this builds two page objects with identical field names, selectors and nested
`Processors`, and diffs `await to_item()`:

  * a `frostwork.webpoet.FrostPage` subclass using `field()` / `Many` / `One`
  * a `web_poet.WebPage` subclass whose `@web_poet.field` getters call `self.css()` / `self.xpath()`

The oracle getter's shape follows the field's terminal, because Frostwork's value contract does:

  * `::text` / `::attr(x)`      -> parsel `.get()` / `.getall()`; compared directly.
  * bare element, NO processor  -> parsel `.get()` / `.getall()`; Frostwork returns the element's RAW
    SOURCE, a documented divergence from lxml's reflow, so this is compared by re-parse on non-whitespace
    text (the same rule as `diff_lxml.verdict`, imported rather than restated).
  * bare element, WITH processor -> parsel returns the `SelectorList` itself, because that is what a real
    page object hands a processor. The processor consumes it and yields a normalized value (a
    `List[Breadcrumb]`, an `AggregateRating`, cleaned HTML), so the comparison is direct equality on the
    processed result. This column is the one that is silently wrong today.

DISCRIMINATION
--------------
A processor that returns `None` on both sides proves nothing, so `gen_page` emits markup the zyte
processors can actually parse (real breadcrumb trails, `"3.8 out of 5 stars"`, prices, descriptions with a
`<script>` and a relative href to strip/resolve) and the run PRINTS how many pairs carried a non-empty
expected value. Read that number before believing a PASS: `--show-discrimination` breaks it down per
processor. A family whose expected values are all empty is a family that cannot go red.

Run:  .venv/bin/python tools/diff_webpoet.py
Gate: DIVERGE + CRASH = 0.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import gc
import os
import random
import sys
import warnings
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import attrs
import oracle
import parsel
import webpoet_cases
from diff_lxml import is_node_query
from web_poet import (
    BrowserHtml,
    BrowserPage,
    BrowserResponse,
    HttpResponse,
    HttpResponseBody,
    HttpResponseHeaders,
    ResponseUrl,
    WebPage,
)
from web_poet import field as wp_field

import frostwork
from frostwork.webpoet import FrostBrowserPage, FrostPage, Many, One, field

URL = "http://example.com/p/1"


# --------------------------------------------------------------------------- the processor universe
# Derived from `tools/webpoet_cases.py`, which both this gate and `tools/webpoet_surface.py` read. Keyed by
# the FIELD NAME web-poet resolves the processor under (`getattr(owner.Processors, name)`) — zyte's own
# names, because that is the exact wiring a page object inherits from `ProductPage` for free.
_GENERATED = webpoet_cases.cases_for("generated")

# {processor key -> (callable, is it gated on isinstance(Selector|HtmlElement))}. The node-gated ones are
# the ones that silently pass a `str` through, which is the defect this gate exists to catch; the
# str-tolerant ones stay in the sweep so a fix for the first group cannot regress them.
PROCESSORS = {c.field_name: (c.callable, c.takes_node) for c in _GENERATED}

# (field name, selector, processor key or None). The node-taking processors get a BARE ELEMENT selector,
# because a node is what they are gated on. `images_processor` is the exception and the registry says so:
# it takes URL STRINGS and has no Selector branch, so its faithful declaration is `::attr(src)` with
# `all=True`. Asking it for a node would make BOTH sides wrong and grade the harness's mistake as a defect.
PROCESSOR_FIELDS = [(c.field_name, c.selector, c.field_name) for c in _GENERATED]

# The processors that take URL strings rather than a node, so cardinality must stay `all=True` for them.
LIST_INPUT_PROCESSORS = {c.field_name for c in _GENERATED if not c.takes_node}

# One node-taking case declared a SECOND time as `all=True`, i.e. the SelectorList branch of the node
# handoff. It is here because `tools/mutate_webpoet.py` proved the branch was unreachable: downgrading it to
# a plain `list` SURVIVED the whole differential, since nothing generated the combination. zyte's
# `_handle_selectorlist` gates on `SelectorList` exactly, so a plain list falls through to their
# "returned as is" path.
_ALL_VARIANT_OF = "breadcrumbs"
NODE_LIST_PROCESSORS = {f"{_ALL_VARIANT_OF}All"}
PROCESSOR_FIELDS.append((
    f"{_ALL_VARIANT_OF}All",
    next(c.selector for c in _GENERATED if c.field_name == _ALL_VARIANT_OF),
    _ALL_VARIANT_OF,
))

# Transforms, so `.map()` / `.re_first()` are not a surface the differential never emits — the other thing
# the mutation sweep caught: making `_shape` drop its transforms entirely SURVIVED, because no generated
# field had one. Keyed by cardinality, since a transform sees the SHAPED value. Applied only to fields
# with no processor, so "transform then processor" ordering stays a unit-vector question with one answer
# rather than a generated one with two plausible ones.
def _t_first(v):
    return (v or "<none>").upper()


def _t_all(v):
    return [x.upper() for x in v]


def _t_join(v):
    return v.strip().replace("  ", " ")


TRANSFORMS = {"first": _t_first, "all": _t_all, "join": _t_join}


# A transform on a field that ALSO has a node-taking processor — the combination the differential used to
# exclude on purpose ("ordering stays a unit-vector question"), which left the composition order asserted by
# nothing generated: converting to a node BEFORE the transform handed user code a `Selector` where the
# contract promises the shaped string, so the documented `field(".desc").map(...)` raised only when a
# processor happened to be attached. The transform edits TEXT rather than markup so it survives the one
# asymmetry in this column (see `_parsel_ns`): our side transforms RAW SOURCE, the oracle transforms lxml's
# serialization of the same element.
def _t_node_source(html):
    return html.replace("Cat", "KAT")


def _t_node_source_all(htmls):
    return [h.replace("Cat", "KAT") for h in htmls]

# Plain value fields: no processor, so these exercise the terminals rather than the node handoff.
VALUE_FIELDS = [
    ("name", "h1::text", None),
    ("sku", ".sku::text", None),
    ("href", ".crumbs a::attr(href)", None),
    ("descRaw", ".desc", None),  # bare element, no processor -> raw-source outer HTML
    ("title", "title::text", None),
    ("metaBrand", "//meta[@itemprop='brand']/@content", None),
]


# --------------------------------------------------------------------------- page generation
def _crumbs(rng):
    depth = rng.randint(2, 4)
    parts = [f'<a href="/c{i}">Cat {i}</a>' for i in range(depth)]
    parts.append("<span>Leaf item</span>")
    sep = rng.choice([" &gt; ", " / ", " › "])
    return f'<nav class="crumbs">{sep.join(parts)}</nav>'


def _desc(rng):
    # a <script> to strip, a relative href to resolve, and collapsible whitespace — all three are
    # differences clear-html actually makes, so a str passed through instead of a node is visible.
    bits = [
        "<p>A   roomy   bag.</p>",
        "<script>track({a:1});</script>",
        '<p>See <a href="/more">more</a>.</p>',
    ]
    if rng.random() < 0.4:
        bits.append("<p>Unclosed paragraph")  # implied close, in-document vs fragment
    if rng.random() < 0.3:
        bits.insert(1, "<style>.x{color:red}</style>")
    return f'<div class="desc">{"".join(bits)}</div>'


def _rating(rng):
    v = rng.choice(["3.8", "4.5", "2.0", "5"])
    n = rng.randint(2, 900)
    return f'<span class="rating">{v} out of 5 stars</span><a class="reviews">See all {n} reviews</a>'


def _cards(rng):
    out = []
    for i in range(rng.randint(1, 4)):
        out.append(
            f'<div class="card"><h3><a href="/p{i}">Item {i}</a></h3>'
            f'<p class="price">${i + 1}.99</p><span class="sku">SKU-{i}</span></div>'
        )
    return "".join(out)


def _malform(rng, body):
    """Sprinkle the malformations that move tree construction, so the gate is not only about clean pages."""
    r = rng.random()
    if r < 0.25:
        return body.replace('<div class="desc">', '<p><div class="desc">', 1)  # <div> closes the <p>
    if r < 0.45:
        return f'<table><tr><td>cell</td></tr>{body}</table>'  # foster-parenting around the lot
    if r < 0.6:
        return body.replace("</nav>", "", 1)  # dropped end tag
    return body


def gen_page(rng) -> bytes:
    head = "<head><title>Product page</title>"
    if rng.random() < 0.7:
        head += '<meta itemprop="brand" content="Acme">'
    head += "</head>"
    body = (
        "<h1>Roomy Bag</h1>"
        + _crumbs(rng)
        + _desc(rng)
        + _rating(rng)
        + f'<span class="price">${rng.randint(5, 99)}.50</span>'
        + f'<span class="regular-price">${rng.randint(100, 199)}.00</span>'
        + '<span class="gtin">5901234123457</span>'
        + '<span class="brand">Acme</span>'
        + f'<img class="hero" src="/i/{rng.randint(1, 9)}.jpg">'
        + f'<span class="sku">SKU-{rng.randint(100, 999)}</span>'
        + _cards(rng)
    )
    if rng.random() < 0.5:
        body = _malform(rng, body)
    return f"<html>{head}<body>{body}</body></html>".encode()


# --------------------------------------------------------------------------- schema generation
def gen_schema(rng) -> dict:
    """A page-object schema: flat fields (some processor-bearing), optionally a `Many`/`One` group."""
    n_proc = rng.randint(1, len(PROCESSOR_FIELDS))
    n_val = rng.randint(1, len(VALUE_FIELDS))
    fields = rng.sample(PROCESSOR_FIELDS, n_proc) + rng.sample(VALUE_FIELDS, n_val)
    rng.shuffle(fields)
    cards = {}
    transforms = {}
    # Fields that carry a nested `Processors` entry AND `out=[]` to cancel it. web-poet resolves `out` with
    # `out is not None`, so an empty list is the documented per-field opt-out — and the one thing every page
    # object inheriting zyte's `ProductPage` needs, since that base attaches processors to nine field NAMES
    # whether the page wants them or not. Frostwork read it as a truthiness test, decided a processor was
    # coming, and returned a re-parsed `Selector` where web-poet returns the raw HTML. Nothing generated it.
    out_off = set()
    for name, sel, _proc in fields:
        if _proc is not None and rng.random() < 0.25:
            out_off.add(name)
        if name in NODE_LIST_PROCESSORS:
            cards[name] = ("all", None)  # exercises the SelectorList branch of the node handoff
        elif _proc in LIST_INPUT_PROCESSORS:
            cards[name] = ("all", None)  # this processor's input is a LIST of strings
        elif _proc is not None or is_node_query(sel):
            cards[name] = ("first", None)  # a node-taking processor takes ONE node; raw source is scalar
        else:
            cards[name] = rng.choice([("first", None), ("all", None), ("join", " ")])
        # a transform on roughly half the no-processor fields...
        if _proc is None and not is_node_query(sel) and rng.random() < 0.5:
            transforms[name] = TRANSFORMS[cards[name][0]]
        # ...and on some fields that DO have a node-taking processor, which pins the composition order
        elif (
            _proc is not None
            and name not in out_off
            and _proc not in LIST_INPUT_PROCESSORS
            and rng.random() < 0.4
        ):
            transforms[name] = _t_node_source_all if cards[name][0] == "all" else _t_node_source
    group = None
    if rng.random() < 0.4:
        group = {
            "name": "products",
            "one": rng.random() < 0.3,
            "container": ".card",
            "subs": [("title", "h3 a::text"), ("href", "h3 a::attr(href)"), ("price", ".price::text")],
        }
    return {
        "fields": fields,
        "cards": cards,
        "transforms": transforms,
        "out_off": out_off,
        "group": group,
    }


# --------------------------------------------------------------------------- class construction
# Every way a page-object class can be built that this integration might see. Enumerated as a MATRIX
# rather than spot-checked because defect 1 was an ORDER bug — a decorator running after
# `__init_subclass__` — and because `attrs.define(slots=False)` mutates in place while the default
# rebuilds the class, so a single hand vector had even odds of picking the shape that worked. It picked
# that one.
#
# `dataclass` and `attrs_frozen` are expected to fail, and they are IN the sweep for that reason: both
# fail on the parsel oracle too (a frozen instance rejects web-poet's `cached_method` write; `@dataclass`
# generates an `__init__` that does not accept web-poet's attrs-declared `response`), so they land in
# ORACLE-SKIP and the gate records that they were probed and are upstream incompatibilities rather than
# leaving them unexamined. A shape that starts passing on the oracle will start being graded here.
SHAPES = (
    "plain",
    "attrs_slots",
    "attrs_noslots",
    "attrs_frozen",
    "dataclass",
    "dataclass_slots",
    "inherit_plain",
    "inherit_attrs",
    "rebuilt_metaclass",
)


def _processors_cls(schema):
    ns = {name: [PROCESSORS[proc][0]] for name, _sel, proc in schema["fields"] if proc}
    return type("Processors", (), ns) if ns else None


def _frost_ns(fields, schema):
    ns = {}
    for name, sel, _proc in fields:
        card, sep = schema["cards"][name]
        # `out=[]` is passed as a real keyword, so this exercises `field()`'s forwarding as well as the
        # resolution: `field()` only forwards keywords it was GIVEN, and an empty list has to survive that.
        kw = {"out": []} if name in schema.get("out_off", ()) else {}
        if card == "all":
            f = field(sel, all=True, **kw)
        elif card == "join":
            f = field(sel, join=sep, **kw)
        else:
            f = field(sel, **kw)
        fn = schema["transforms"].get(name)
        ns[name] = f.map(fn) if fn is not None else f
    return ns


def _group_ns(schema, frost: bool):
    g = schema["group"]
    if not g:
        return {}
    if frost:
        maker = One if g["one"] else Many
        return {g["name"]: maker(g["container"], **{n: field(s) for n, s in g["subs"]})}

    def getter(self, g=g):
        rows = []
        for node in self.css(g["container"]):
            rows.append({n: node.css(s).get() for n, s in g["subs"]})
        return (rows[0] if rows else None) if g["one"] else rows

    getter.__name__ = getter.__qualname__ = g["name"]
    return {g["name"]: wp_field(getter)}


def _reparse(html: str):
    """An HTML fragment back into a `parsel.Selector` over that element — the oracle's own two lines, not an
    import of the handoff being tested. Only reached by the transform-plus-processor column below, and only
    for `<div>`/`<nav>`/`<span>` fields, where `lxml.html.fromstring` is faithful; it is NOT faithful for
    document-frame elements, which is why that contract is asserted by a unit sweep over the whole element
    universe (`tests/test_python.py`) instead of here."""
    from lxml.html import fromstring

    return parsel.Selector(root=fromstring(html))


def _parsel_ns(fields, schema):
    """Oracle getters. A processor-bearing field returns the `SelectorList`/list-of-strings the processor
    actually expects; everything else returns the value, matching Frostwork's terminal contract."""
    ns = {}
    for name, sel, proc in fields:
        card, sep = schema["cards"][name]

        fn = schema["transforms"].get(name)
        # `out=[]` on this field cancels the nested `Processors` entry, so the oracle must produce the
        # NO-processor shape — and be declared with the same keyword, so web-poet does the cancelling on
        # both sides rather than the harness pretending the entry is absent.
        off = name in schema.get("out_off", ())

        def getter(self, sel=sel, card=card, sep=sep, proc=proc, fn=fn, off=off):
            sub = self.xpath(sel) if sel.startswith(("/", "(")) else self.css(sel)
            if proc is not None and not off:
                # Keyed on the PROCESSOR's input contract, not on cardinality: `images_processor`
                # consumes URL STRINGS and has no Selector branch (see PROCESSOR_FIELDS), while the
                # node-taking ones consume the SelectorList. Conflating the two would silently pick the
                # wrong oracle the moment a node field is declared `all=True`.
                if proc in LIST_INPUT_PROCESSORS:
                    value = sub.getall()
                    return fn(value) if fn is not None else value
                if fn is None:
                    return sub
                # A transform on a node-taking processor field. The contract says transforms run on the
                # SHAPED value (the element's HTML) and the node conversion belongs to the processor
                # handoff, so the oracle spells that out: transform the source, then re-parse.
                #
                # This is the one column where the oracle mirrors our composition rather than deriving the
                # answer independently — "transform the source, then process it" has no parsel-native
                # spelling. What it discriminates is ORDER, and an order error does not need an independent
                # value to show up: converting first hands a `Selector` to a string transform, which raises
                # on our side alone (a CRASH), or silently changes the value if the transform tolerates it.
                if card == "all":
                    return parsel.selector.SelectorList([_reparse(h) for h in fn(sub.getall()) if h])
                first = sub.get()
                return _reparse(fn(first)) if first else None
            if card == "all":
                value = sub.getall()
            elif card == "join":
                value = sep.join(sub.getall())
            else:
                value = sub.get()
            # the same transform Frostwork attaches with `.map()`, applied to the same shaped value
            return fn(value) if fn is not None else value

        getter.__name__ = getter.__qualname__ = name
        ns[name] = wp_field(out=[])(getter) if off else wp_field(getter)
    return ns


def _rebuild(cls):
    """Rebuild a class from its own `__dict__`, the way `attrs.define` does — but through a bare
    `type()` call, so this probes the MECHANISM rather than one library's use of it. Any decorator or
    metaclass that reconstructs a class lands here, and the spec recovery has to survive all of them."""
    return type(cls)(cls.__name__, cls.__bases__, dict(cls.__dict__))


def _apply_shape(cls, shape):
    if shape in ("attrs_slots", "inherit_attrs"):
        return attrs.define(cls)
    if shape == "attrs_noslots":
        return attrs.define(slots=False)(cls)
    if shape == "attrs_frozen":
        return attrs.frozen(cls)
    if shape == "dataclass":
        return dataclasses.dataclass(cls)
    if shape == "dataclass_slots":
        return dataclasses.dataclass(slots=True)(cls)
    if shape == "rebuilt_metaclass":
        return _rebuild(cls)
    return cls


def build_page(schema, shape, *, frost: bool, kind: str = "http"):
    """Build one side of the pair in one of the SHAPES.

    The shape axis is the whole point of defect 1: the decorator runs AFTER `__init_subclass__`, so it is
    an ORDER bug that no (field x selector) sweep can reach — the same reason `make gate-seq` exists for
    the engine.

    It is applied to BOTH sides, and that symmetry is load-bearing. `attrs.frozen` breaks the parsel
    oracle too (web-poet's `cached_method` writes to the instance, which a frozen class forbids), so
    decorating only the Frostwork side would file a web-poet/attrs incompatibility as a Frostwork CRASH
    and leave a bucket that never empties — the over-attribution mistake AGENTS.md records for the
    truncated-tag bug. With both sides decorated, a shape that fails on both is ORACLE-SKIP and a shape
    that fails only here is a real defect."""
    ns_maker = _frost_ns if frost else _parsel_ns
    # The response type picks the BASE on both sides, so the browser column compares like with like:
    # FrostBrowserPage against web-poet's own BrowserPage, not against a WebPage that happens to work.
    if frost:
        root = FrostBrowserPage if kind == "browser" else FrostPage
    else:
        root = BrowserPage if kind == "browser" else WebPage
    fields = schema["fields"]
    procs = _processors_cls(schema)
    if shape in ("inherit_plain", "inherit_attrs"):
        split = max(1, len(fields) // 2)
        base_ns = ns_maker(fields[:split], schema)
        if procs is not None:
            base_ns["Processors"] = procs
        base = type("GenBase", (root,), base_ns)
        ns = ns_maker(fields[split:], schema)
        ns.update(_group_ns(schema, frost=frost))
        return _apply_shape(type("GenPage", (base,), ns), shape)
    ns = ns_maker(fields, schema)
    ns.update(_group_ns(schema, frost=frost))
    if procs is not None:
        ns["Processors"] = procs
    return _apply_shape(type("GenPage", (root,), ns), shape)


# ------------------------------------------------- the REAL zyte-common-items composition
# Every processor column above attaches zyte's real processor functions through a `Processors` class this
# harness SYNTHESIZES. That is faithful to the isinstance gate but not to the WIRING, which in production
# arrives through zyte's own MRO: `class MyPage(FrostPage, ProductPage)` inherits a `Processors` covering
# nine field NAMES, `Returns[Product]`, and a typed item that does NOT validate types — which is why the
# original defect put a raw-HTML string in a `List[Breadcrumb]` field with nothing raised.
#
# So this pair inherits the real `ProductPage` on both sides. Which of the fields carries a processor is
# DERIVED from `ProductPage.Processors` rather than listed here: the whole point is that nobody wrote the
# wiring down, so nobody here should either.
ZYTE_FIELDS = [("name", "h1::text")] + [
    (c.field_name, c.selector) for c in webpoet_cases.cases_for("productpage")
]


def _zyte_processor_fields() -> dict:
    """`{field name: does zyte's own ProductPage attach a processor to it}`, read off the class — so the
    column asserts the WIRING rather than repeating this harness's belief about it. `name` is in
    `ZYTE_FIELDS` precisely because it is a field zyte attaches nothing to."""
    wired = webpoet_cases.product_page_processors()
    return {name: name in wired for name, _sel in ZYTE_FIELDS}


def build_zyte_pages():
    """`(frost_cls, oracle_cls)` — the same fields on a real `ProductPage`, once through Frostwork and once
    through parsel. Built once; both are plain classes with no per-page state."""
    from zyte_common_items.pages import ProductPage

    has_proc = _zyte_processor_fields()

    frost_ns = {}
    oracle_ns = {}
    for name, sel in ZYTE_FIELDS:
        list_input = name in LIST_INPUT_PROCESSORS
        frost_ns[name] = field(sel, all=True) if list_input else field(sel)

        def getter(self, sel=sel, proc=has_proc[name], list_input=list_input):
            sub = self.css(sel)
            if proc:
                return sub.getall() if list_input else sub
            return sub.getall() if list_input else sub.get()

        getter.__name__ = getter.__qualname__ = name
        oracle_ns[name] = wp_field(getter)

    # `FrostPage` FIRST: the markers are converted by its `__init_subclass__`, and a `field()` on a class
    # that does not inherit a Frostwork base raises at class definition (see `_require_frost_owner`).
    return (
        type("FrostProductPage", (FrostPage, ProductPage), frost_ns),
        type("ParselProductPage", (ProductPage,), oracle_ns),
    )


def _zyte_item_fields(item) -> dict:
    """The declared fields of a `Product`, as a dict. `metadata` is excluded deliberately: it carries a
    download TIMESTAMP, so the two sides can never be equal and comparing it would make every pair red."""
    return {n: getattr(item, n, "<MISSING>") for n, _sel in ZYTE_FIELDS}


def _zyte_item_rest(item) -> dict:
    """Every OTHER field of the item, so this column is a whole-item comparison like the rest of the gate
    rather than a check of the fields the harness happens to declare.

    It is one comparison rather than thirty rows because most of a `Product` is `None` here and thirty
    vacuous AGREEs per page would drown the discrimination count — the number the run prints to say whether
    a PASS means anything. What it adds is real: `description` lands in here, and zyte computes it from
    `descriptionHtml` through a side channel (`_descriptionHtml_node`), so this column grades the node the
    handoff produced a second time, through a field nobody declared."""
    declared = {n for n, _sel in ZYTE_FIELDS} | {"metadata"}
    return {k: v for k, v in attrs.asdict(item).items() if k not in declared}


def _zyte_schema() -> dict:
    """A `schema`-shaped view of the zyte pair, so the same verdict functions grade it."""
    has_proc = _zyte_processor_fields()
    return {
        "fields": [(n, sel, f"zyte:{n}" if has_proc[n] else None) for n, sel in ZYTE_FIELDS],
        "cards": {n: ("all", None) if n in LIST_INPUT_PROCESSORS else ("first", None) for n, _ in ZYTE_FIELDS},
        "transforms": {},
        "out_off": (),
        "group": None,
    }


# --------------------------------------------------------------------------- running a pair
def _http(html: bytes):
    return HttpResponse(
        url=ResponseUrl(URL),
        body=HttpResponseBody(html),
        headers=HttpResponseHeaders({"Content-Type": "text/html; charset=utf-8"}),
    )


def _browser(html: bytes):
    return BrowserResponse(url=ResponseUrl(URL), html=BrowserHtml(html.decode()))


def _to_item(cls, response):
    """`(item, None)` or `(None, "ExcName: msg")`. A raise is data here, not a harness failure: two of the
    defects surface as an exception from `to_item()` and the gate must count them, not die."""
    try:
        return asyncio.run(cls(response=response).to_item()), None
    except Exception as exc:  # noqa: BLE001 - any raise is a verdict
        # A raise part-way through web-poet's field gather can leave an ASYNC field's coroutine unawaited
        # (zyte's `description` is one), and Python reports that at GC time — in the middle of an unrelated
        # line of output, under `tools/mutate_webpoet.py` where raising is the point. Collect it here with
        # the warning suppressed so a mutation run reports mutations rather than their fallout.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            gc.collect()
        return None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- verdicts
def _structure(html: str):
    """A normalized structural signature of one HTML fragment: tag, attributes, text and tails (whitespace
    collapsed), and the same recursively for each child element.

    This is what "the raw-source divergence is acceptable" has to mean. Comparing non-whitespace TEXT was
    too weak by a mile — `<p class=a>same</p>` and `<section id=wrong>same</section>` have identical text,
    so the allowance excused a different tag, lost attributes and a reshaped subtree. Serialization
    differences (quoting, entity spelling, an implied end tag) disappear when both sides are parsed;
    structure does not, and structure is what a processor reads."""
    from lxml.html import fromstring  # noqa: PLC0415

    def walk(el):
        return (
            el.tag,
            tuple(sorted((k, " ".join(v.split())) for k, v in el.attrib.items())),
            " ".join((el.text or "").split()),
            tuple(walk(c) for c in el if isinstance(c.tag, str)),
            " ".join((el.tail or "").split()),
        )

    return walk(fromstring(html))


def field_verdict(mine, theirs, selector: str, has_processor: bool) -> str:
    """AGREE | WS | DIVERGE for one field. Pure and importable so `tests/test_gates.py` can seed it."""
    if mine == theirs:
        return "AGREE"
    if has_processor:
        # A processor's output is normalized (a list of items, an AggregateRating, cleaned HTML): there is
        # no raw-source allowance to make, so any difference is real. This is the column where a `str`
        # passing through the isinstance gates shows up.
        return "DIVERGE"
    if isinstance(mine, str) and isinstance(theirs, str):
        if mine.strip() == theirs.strip():
            return "WS"
        if is_node_query(selector):
            try:
                if _structure(mine) == _structure(theirs):
                    return "AGREE"  # raw source vs lxml's reflow — documented, local
            except Exception:  # noqa: BLE001
                return "DIVERGE"
        return "DIVERGE"
    if isinstance(mine, list) and isinstance(theirs, list):
        if [x.strip() if isinstance(x, str) else x for x in mine] == [
            x.strip() if isinstance(x, str) else x for x in theirs
        ]:
            return "WS"
        # The raw-source allowance, per item: an `all=True` bare-element field is a LIST of outer HTML, so
        # the same documented divergence (raw source vs lxml's reflow) applies element by element. Without
        # this the list branch could only ever say DIVERGE for such a field — which is why no generated
        # field was one until `out=[]` made an `all=True` node field processor-free.
        if is_node_query(selector) and len(mine) == len(theirs):
            try:
                if all(_structure(a) == _structure(b) for a, b in zip(mine, theirs)):
                    return "AGREE"
            except Exception:  # noqa: BLE001
                return "DIVERGE"
    return "DIVERGE"


def item_verdicts(mine_item, theirs_item, schema) -> list:
    """Per-field verdicts over the UNION of both items' keys, so a field that vanished from one side is a
    DIVERGE rather than an unchecked absence. That union is what makes this a whole-item comparison."""
    out = []
    # `out=[]` cancels the field's processor, so its value is the plain terminal one and the raw-source
    # allowance applies again. Grading it as "processed" would demand byte equality on outer HTML and file
    # the documented reflow divergence as a defect — the mirror image of the mistake this gate exists for.
    off = schema.get("out_off", ())
    procs = {name: (None if name in off else proc) for name, _sel, proc in schema["fields"]}
    sels = {name: sel for name, sel, _proc in schema["fields"]}
    for key in sorted(set(mine_item) | set(theirs_item)):
        if key not in mine_item or key not in theirs_item:
            out.append((key, "DIVERGE", mine_item.get(key, "<MISSING>"), theirs_item.get(key, "<MISSING>")))
            continue
        v = field_verdict(mine_item[key], theirs_item[key], sels.get(key, ""), bool(procs.get(key)))
        out.append((key, v, mine_item[key], theirs_item[key]))
    return out


def _bucket(name: str, schema) -> str:
    """The discrimination row this field's comparison belongs in.

    Not just the processor name: the FEATURE matters, because the two holes `tools/mutate_webpoet.py` found
    were combinations rather than processors (`all=True` with a processor, and a `.map()` alongside one).
    Naming them here is what makes `coverage_failures` able to notice that a column stopped being generated
    — a hole in the generator reads exactly like a passing gate otherwise."""
    procs = {n: p for n, _s, p in schema["fields"]}
    proc = procs.get(name)
    if proc is None:
        return "(no processor)"
    if name in schema.get("out_off", ()):
        return f"{proc} (out=[])"
    marks = ""
    if schema.get("cards", {}).get(name, ("first", None))[0] == "all" and name in NODE_LIST_PROCESSORS:
        marks += " (all=True)"
    if name in schema.get("transforms", {}):
        marks += " +map"
    return f"{proc}{marks}"


# Class shapes whose ORACLE cannot be built, with the upstream reason. They are in the sweep so that they
# are recorded as PROBED rather than left unexamined, and listed here so "0 graded pairs" can be told apart
# from a shape that silently stopped producing comparisons — which is how a gate reads green while covering
# nothing. A shape that starts building on the oracle must be moved OUT of this dict; that is a failure too,
# because a stale expectation is how the distinction rots.
EXPECTED_ORACLE_SKIP = {
    "attrs_frozen": "web-poet's `cached_method` writes to the instance; a frozen attrs class forbids it",
    "dataclass": "@dataclass generates an __init__ that does not accept web-poet's attrs-declared response",
    "dataclass_slots": "the same, through the slots rebuild",
}

# Every (class shape x response input) CELL the sweep is expected to grade. Per cell, not per shape: the two
# inputs go through different bases (`FrostPage`/`WebPage` vs `FrostBrowserPage`/`BrowserPage`), so every
# BrowserResponse pair could turn into an ORACLE-SKIP while the HTTP half kept the shape's total non-zero —
# a whole input type silently untested. Plus the zyte composition, which is its own cell.
INPUT_KINDS = ("http", "browser")


def expected_cells(shapes=SHAPES, browser: bool = True, zyte: bool = True) -> list:
    kinds = INPUT_KINDS if browser else ("http",)
    cells = [
        f"{shape}/{kind}" for shape in shapes for kind in kinds if shape not in EXPECTED_ORACLE_SKIP
    ]
    return cells + (["zyte_productpage/http"] if zyte else [])


def required_columns() -> list:
    """The discrimination columns a run must contain, as ``(name, why, exact)`` — DERIVED, not listed.

    Two kinds, and the difference matters. Every non-declined `ProcessorCase` must appear as an EXACT
    bucket: a substring test would be satisfied by `breadcrumbs (out=[])` alone, i.e. by the variant while
    the plain column went missing. The feature COMBINATIONS are substring markers, since they are suffixes
    attached to whichever processor drew them that run."""
    exact = [(f"{c.field_name}", f"the {c.processor} column", True)
             for c in webpoet_cases.cases_for("generated")]
    exact += [(f"zyte:{c.field_name}", f"{c.processor} through zyte's real ProductPage wiring", True)
              for c in webpoet_cases.cases_for("productpage")]
    exact.append(("(no processor)", "plain value fields, where the terminal contract answers", True))
    markers = [
        ("(out=[])", "an explicit out=[] cancelling an inherited Processors entry", False),
        ("+map", "a .map() on a processor-bearing node field — the shape -> map -> processor ORDER", False),
        ("(all=True)", "a node-taking processor on an all=True field — the SelectorList branch", False),
    ]
    return exact + markers


def coverage_failures(by_shape, by_proc, meaningful, cells=None) -> list:
    """Reasons this run proves less than its PASS suggests. Pure and importable so `tests/test_gates.py`
    can seed each one.

    A differential can be green because everything agreed or because nothing ran, and the run's own output
    is the only place that difference shows. Four ways it hid here: a (shape, input) cell that produced only
    ORACLE-SKIPs still exited 0; a processor row whose expected value was empty on every page could not go
    red; a combination the generator never emitted (`out=[]`, `.map()` beside a processor) was reported by
    nothing; and a processor family removed from the registry simply stopped appearing."""
    failures = []
    graded: dict = defaultdict(int)
    skipped: dict = defaultdict(int)
    for key, d in by_shape.items():
        # `.get`, not `[...]`: a pure function a test seeds with plain dicts, not only the sweep's defaultdict
        cell = "/".join(key.split("/")[:2])  # drop the "/(build)" suffix the build-skip bucket carries
        graded[cell] += sum(d.get(v, 0) for v in ("AGREE", "WS", "DIVERGE", "CRASH"))
        skipped[cell] += d.get("ORACLE-SKIP", 0)
    for cell in expected_cells() if cells is None else cells:
        if graded.get(cell, 0) == 0:
            failures.append(
                f"cell {cell!r} graded 0 pairs — every pair was an ORACLE-SKIP or never ran, so this "
                f"(class shape, response input) is not being tested. Fix the oracle side, or record it in "
                f"EXPECTED_ORACLE_SKIP with the upstream reason."
            )
        elif skipped.get(cell, 0):
            failures.append(
                f"cell {cell!r} graded {graded[cell]} pairs but skipped {skipped[cell]}: the oracle failed "
                f"to build or answer for some of them, which is a hole this run reported as a pass."
            )
    for shape in SHAPES:
        shape_graded = sum(v for cell, v in graded.items() if cell.split("/")[0] == shape)
        if shape_graded and shape in EXPECTED_ORACLE_SKIP:
            failures.append(
                f"class shape {shape!r} is listed in EXPECTED_ORACLE_SKIP ({EXPECTED_ORACLE_SKIP[shape]}) "
                f"but graded {shape_graded} pairs — upstream changed; remove the entry so it is gated."
            )
    for bucket, counts in by_proc.items():
        if sum(counts.values()) and not meaningful.get(bucket, 0):
            failures.append(
                f"column {bucket!r} never carried a non-empty expected value, so it cannot go red — the "
                f"generated markup does not feed it. Fix `gen_page`, do not delete the column."
            )
    for marker, why, exact in required_columns():
        present = marker in by_proc if exact else any(marker in bucket for bucket in by_proc)
        if not present:
            failures.append(
                f"no column matched {marker!r} — the run stopped covering {why}. Each of these either "
                f"survived the differential once because nothing generated it, or is a processor the "
                f"registry (tools/webpoet_cases.py) says is covered."
            )
    return failures


def _expected_is_meaningful(value) -> bool:
    """Does the ORACLE's value carry information? A column that is None/empty on both sides cannot go red."""
    if value is None:
        return False
    if isinstance(value, (str, list, dict, tuple)):
        return len(value) > 0
    return True


# --------------------------------------------------------------------------- main
def sweep(
    seed: int = 0,
    schemas: int = 120,
    browser: bool = True,
    show: int = 8,
    shapes=SHAPES,
    zyte: bool = True,
):
    """Run the differential and return `(stat, by_shape, by_proc, meaningful, examples)`.

    Split out of `main` so it can be used as a DETECTOR: `tools/mutate_webpoet.py` breaks one load-bearing
    line in `frostwork.webpoet` and asks whether this goes red. A gate nobody has tried to fool is a
    guess about its own coverage."""
    rng = random.Random(seed)
    stat = defaultdict(int)
    by_shape = defaultdict(lambda: defaultdict(int))
    by_proc = defaultdict(lambda: defaultdict(int))
    meaningful = defaultdict(int)
    examples = []

    inputs = [("http", _http)] + ([("browser", _browser)] if browser else [])
    args = argparse.Namespace(show=show)

    for shape in shapes:
        for _ in range(schemas):
            html = gen_page(rng)
            schema = gen_schema(rng)
            for kind, make_response in inputs:
                key = f"{shape}/{kind}"
                try:
                    oracle_cls = build_page(schema, shape, frost=False, kind=kind)
                except Exception:  # noqa: BLE001 - oracle unbuildable in this shape; not our verdict
                    stat["ORACLE-SKIP"] += 1
                    by_shape[f"{key}/(build)"]["ORACLE-SKIP"] += 1
                    continue
                try:
                    frost_cls = build_page(schema, shape, frost=True, kind=kind)
                except Exception as exc:  # noqa: BLE001 - a schema that will not build is a CRASH
                    stat["CRASH"] += 1
                    stat["pairs"] += 1
                    by_shape[key]["CRASH"] += 1
                    if len(examples) < args.show:
                        examples.append(
                            (key, "<class construction>", f"{type(exc).__name__}: {exc}", "", "")
                        )
                    continue
                mine, mine_err = _to_item(frost_cls, make_response(html))
                theirs, theirs_err = _to_item(oracle_cls, make_response(html))
                if theirs_err is not None:
                    # the oracle itself could not answer; nothing to compare (parsel has no such input)
                    stat["ORACLE-SKIP"] += 1
                    by_shape[key]["ORACLE-SKIP"] += 1
                    continue
                if mine_err is not None:
                    stat["CRASH"] += 1
                    stat["pairs"] += 1
                    by_shape[key]["CRASH"] += 1
                    if len(examples) < args.show:
                        examples.append((key, "<to_item raised>", mine_err, "", html.decode()[:120]))
                    continue
                for name, v, got, want in item_verdicts(dict(mine), dict(theirs), schema):
                    stat[v] += 1
                    stat["pairs"] += 1
                    by_shape[key][v] += 1
                    bucket = _bucket(name, schema)
                    by_proc[bucket][v] += 1
                    if _expected_is_meaningful(want):
                        meaningful[bucket] += 1
                    if v == "DIVERGE" and len(examples) < args.show:
                        examples.append((key, name, repr(got)[:150], repr(want)[:150], html.decode()[:120]))

    if zyte:
        # The real composition, over the same generated pages. One pair per page rather than one per class
        # shape: the shape axis is about decorators and is covered above, while what this adds is zyte's own
        # MRO doing the processor wiring — the thing a synthesized `Processors` class cannot prove.
        zschema = _zyte_schema()
        frost_cls, oracle_cls = build_zyte_pages()
        for _ in range(schemas):
            html = gen_page(rng)
            key = "zyte_productpage/http"
            mine, mine_err = _to_item(frost_cls, _http(html))
            theirs, theirs_err = _to_item(oracle_cls, _http(html))
            if theirs_err is not None:
                stat["ORACLE-SKIP"] += 1
                by_shape[key]["ORACLE-SKIP"] += 1
                continue
            if mine_err is not None:
                stat["CRASH"] += 1
                stat["pairs"] += 1
                by_shape[key]["CRASH"] += 1
                if len(examples) < args.show:
                    examples.append((key, "<to_item raised>", mine_err, "", html.decode()[:120]))
                continue
            pairs = list(item_verdicts(_zyte_item_fields(mine), _zyte_item_fields(theirs), zschema))
            # ...plus every undeclared field of the item, as one pair (see `_zyte_item_rest`)
            rest_mine, rest_theirs = _zyte_item_rest(mine), _zyte_item_rest(theirs)
            pairs.append((
                "(other item fields)",
                "AGREE" if rest_mine == rest_theirs else "DIVERGE",
                rest_mine,
                rest_theirs,
            ))
            for name, v, got, want in pairs:
                stat[v] += 1
                stat["pairs"] += 1
                by_shape[key][v] += 1
                bucket = _bucket(name, zschema) if name != "(other item fields)" else "zyte:(other fields)"
                by_proc[bucket][v] += 1
                # for the rest-bucket, "meaningful" means at least one of those fields carried a value —
                # a dict of thirty `None`s has a length and would otherwise read as information
                carries = (
                    any(x is not None for x in want.values())
                    if name == "(other item fields)"
                    else _expected_is_meaningful(want)
                )
                if carries:
                    meaningful[bucket] += 1
                if v == "DIVERGE" and len(examples) < args.show:
                    examples.append((key, name, repr(got)[:150], repr(want)[:150], html.decode()[:120]))

    return stat, by_shape, by_proc, meaningful, examples


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schemas", type=int, default=120, help="schemas per class shape")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--show", type=int, default=8)
    ap.add_argument("--show-discrimination", action="store_true",
                    help="per-processor count of pairs whose expected value was non-empty")
    ap.add_argument("--no-browser", action="store_false", dest="browser",
                    help="skip the BrowserResponse input (it is fed by default)")
    oracle.add_argument(ap)
    args = ap.parse_args()
    oracle.require(args.allow_old_libxml2)

    print(f"frostwork {frostwork.__version__ if hasattr(frostwork, '__version__') else ''} "
          f"web-poet differential")
    print(oracle.banner())

    stat, by_shape, by_proc, meaningful, examples = sweep(
        seed=args.seed, schemas=args.schemas, browser=args.browser, show=args.show
    )

    print(f"\n  pairs (field comparisons) {stat['pairs']:>8}")
    print(f"  AGREE                     {stat['AGREE']:>8}")
    print(f"  WS (whitespace only)      {stat['WS']:>8}")
    print(f"  ORACLE-SKIP               {stat['ORACLE-SKIP']:>8}   (parsel cannot take this input)")
    print(f"  DIVERGE                   {stat['DIVERGE']:>8}   <-- gate: must be 0")
    print(f"  CRASH (to_item raised)    {stat['CRASH']:>8}   <-- gate: must be 0\n")

    print("  by class shape / input:    pairs   AGREE   WS  DIVERGE  CRASH  ORACLE-SKIP")
    for key in sorted(by_shape):
        d = by_shape[key]
        p = d["AGREE"] + d["WS"] + d["DIVERGE"] + d["CRASH"]
        print(f"    {key:<24}{p:>6}{d['AGREE']:>8}{d['WS']:>5}{d['DIVERGE']:>9}{d['CRASH']:>7}"
              f"{d['ORACLE-SKIP']:>13}")

    print("\n  by processor:             pairs   AGREE   WS  DIVERGE   non-empty expected")
    for key in sorted(by_proc):
        d = by_proc[key]
        p = sum(d.values())
        print(f"    {key:<24}{p:>6}{d['AGREE']:>8}{d['WS']:>5}{d['DIVERGE']:>9}{meaningful[key]:>16}")
    total_meaningful = sum(meaningful.values())
    print(f"\n  DISCRIMINATION: {total_meaningful} of {stat['pairs']} pairs had a non-empty expected value."
          f" A processor row with 0 here cannot go red.")
    for shape, why in sorted(EXPECTED_ORACLE_SKIP.items()):
        print(f"    expected ORACLE-SKIP: {shape} — {why}")

    if examples:
        print("\n  divergences (first few):")
        for key, name, got, want, snip in examples:
            print(f"    [{key}] {name}\n        frostwork={got}\n        parsel   ={want}\n        html: {snip!r}")

    # A green run is only worth the columns it actually graded, and that used to be reported rather than
    # gated: a shape that produced nothing but ORACLE-SKIPs, a processor row whose expected value was empty
    # everywhere, and a combination the generator never emitted all exited 0.
    coverage = coverage_failures(
        by_shape, by_proc, meaningful, cells=expected_cells(browser=args.browser)
    )
    if coverage:
        print("\n  COVERAGE FAILURES (the run proves less than a PASS suggests):")
        for line in coverage:
            print(f"    - {line}")

    gate = stat["DIVERGE"] + stat["CRASH"]
    ok = gate == 0 and not coverage
    print(f"\n  GATE: DIVERGE+CRASH = {gate}, coverage failures = {len(coverage)}  ->  "
          f"{'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
