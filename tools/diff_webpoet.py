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
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import attrs
import oracle
import parsel
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
from zyte_common_items.processors import (
    brand_processor,
    breadcrumbs_processor,
    description_html_processor,
    images_processor,
    price_processor,
    rating_processor,
    simple_price_processor,
)

import frostwork
from frostwork.webpoet import FrostBrowserPage, FrostPage, Many, One, field

URL = "http://example.com/p/1"


# --------------------------------------------------------------------------- the processor universe
# Keyed by the FIELD NAME web-poet looks the processor up under (`getattr(owner.Processors, name)`), which
# is why using zyte's own names matters: this is the exact wiring a real page object gets for free by
# inheriting `ProductPage`. `needs_node` records which ones are gated on isinstance(Selector|HtmlElement)
# and therefore silently pass a `str` through — the defect this gate exists to catch. The str-tolerant
# ones are kept in the sweep so a fix for the first group cannot regress them.
PROCESSORS = {
    "breadcrumbs": (breadcrumbs_processor, True),
    "descriptionHtml": (description_html_processor, True),
    "aggregateRating": (rating_processor, True),
    "price": (price_processor, True),
    "simplePrice": (simple_price_processor, True),
    "brand": (brand_processor, False),
    "images": (images_processor, False),
}

# (field name, selector, processor name or None). The node-taking processors get a BARE ELEMENT selector,
# because a node is what they are gated on — that is the whole point of the column. `images_processor` is
# the exception and is written as one: it takes URL STRINGS (`isinstance(value, str)` -> `[Image(url=...)]`,
# or an iterable of them) and has no Selector branch at all, so the faithful oracle for it is
# `::attr(src)` with `all=True`. Asking it for a node would make BOTH sides wrong and grade the harness's
# own mistake as a defect.
PROCESSOR_FIELDS = [
    ("breadcrumbs", ".crumbs", "breadcrumbs"),
    ("descriptionHtml", ".desc", "descriptionHtml"),
    ("aggregateRating", ".rating", "aggregateRating"),
    ("price", ".price", "price"),
    ("simplePrice", ".price", "simplePrice"),
    ("brand", ".brand", "brand"),
    ("images", "img.hero::attr(src)", "images"),
]

# The processors that take URL strings rather than a node, so cardinality must stay `all=True` for them.
LIST_INPUT_PROCESSORS = {"images"}

# A node-taking processor on an `all=True` bare-element field, i.e. the SelectorList branch of the node
# handoff. Added because `tools/mutate_webpoet.py` proved it was unreachable: downgrading that branch to a
# plain `list` SURVIVED the whole differential, since nothing generated the combination. zyte's
# `_handle_selectorlist` gates on `SelectorList` exactly, so a plain list falls through to the
# "returned as is" path — defect 5 again, in the one shape the gate could not see.
NODE_LIST_PROCESSORS = {"breadcrumbsAll"}
PROCESSOR_FIELDS.append(("breadcrumbsAll", ".crumbs", "breadcrumbs"))
PROCESSORS["breadcrumbsAll"] = (breadcrumbs_processor, True)

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
    for name, sel, _proc in fields:
        if name in NODE_LIST_PROCESSORS:
            cards[name] = ("all", None)  # exercises the SelectorList branch of the node handoff
        elif _proc in LIST_INPUT_PROCESSORS:
            cards[name] = ("all", None)  # this processor's input is a LIST of strings
        elif _proc is not None or is_node_query(sel):
            cards[name] = ("first", None)  # a node-taking processor takes ONE node; raw source is scalar
        else:
            cards[name] = rng.choice([("first", None), ("all", None), ("join", " ")])
        # a transform on roughly half the no-processor fields
        if _proc is None and not is_node_query(sel) and rng.random() < 0.5:
            transforms[name] = TRANSFORMS[cards[name][0]]
    group = None
    if rng.random() < 0.4:
        group = {
            "name": "products",
            "one": rng.random() < 0.3,
            "container": ".card",
            "subs": [("title", "h3 a::text"), ("href", "h3 a::attr(href)"), ("price", ".price::text")],
        }
    return {"fields": fields, "cards": cards, "transforms": transforms, "group": group}


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
        if card == "all":
            f = field(sel, all=True)
        elif card == "join":
            f = field(sel, join=sep)
        else:
            f = field(sel)
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


def _parsel_ns(fields, schema):
    """Oracle getters. A processor-bearing field returns the `SelectorList`/list-of-strings the processor
    actually expects; everything else returns the value, matching Frostwork's terminal contract."""
    ns = {}
    for name, sel, proc in fields:
        card, sep = schema["cards"][name]

        fn = schema["transforms"].get(name)

        def getter(self, sel=sel, card=card, sep=sep, proc=proc, fn=fn):
            sub = self.xpath(sel) if sel.startswith(("/", "(")) else self.css(sel)
            if proc is not None:
                # Keyed on the PROCESSOR's input contract, not on cardinality: `images_processor`
                # consumes URL STRINGS and has no Selector branch (see PROCESSOR_FIELDS), while the
                # node-taking ones consume the SelectorList. Conflating the two would silently pick the
                # wrong oracle the moment a node field is declared `all=True`.
                return sub.getall() if proc in LIST_INPUT_PROCESSORS else sub
            if card == "all":
                value = sub.getall()
            elif card == "join":
                value = sep.join(sub.getall())
            else:
                value = sub.get()
            # the same transform Frostwork attaches with `.map()`, applied to the same shaped value
            return fn(value) if fn is not None else value

        getter.__name__ = getter.__qualname__ = name
        ns[name] = wp_field(getter)
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
        return None, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- verdicts
def _nonws_text(html: str):
    """Non-whitespace text of a serialized fragment — the raw-source-vs-reflow rule from `diff_lxml`."""
    return [t.strip() for t in parsel.Selector(text=html).xpath("//text()").getall() if t.strip()]


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
                if _nonws_text(mine) == _nonws_text(theirs):
                    return "AGREE"  # raw source vs lxml's reflow — documented, local
            except Exception:  # noqa: BLE001
                return "DIVERGE"
        return "DIVERGE"
    if isinstance(mine, list) and isinstance(theirs, list):
        if [x.strip() if isinstance(x, str) else x for x in mine] == [
            x.strip() if isinstance(x, str) else x for x in theirs
        ]:
            return "WS"
    return "DIVERGE"


def item_verdicts(mine_item, theirs_item, schema) -> list:
    """Per-field verdicts over the UNION of both items' keys, so a field that vanished from one side is a
    DIVERGE rather than an unchecked absence. That union is what makes this a whole-item comparison."""
    out = []
    procs = {name: proc for name, _sel, proc in schema["fields"]}
    sels = {name: sel for name, sel, _proc in schema["fields"]}
    for key in sorted(set(mine_item) | set(theirs_item)):
        if key not in mine_item or key not in theirs_item:
            out.append((key, "DIVERGE", mine_item.get(key, "<MISSING>"), theirs_item.get(key, "<MISSING>")))
            continue
        v = field_verdict(mine_item[key], theirs_item[key], sels.get(key, ""), bool(procs.get(key)))
        out.append((key, v, mine_item[key], theirs_item[key]))
    return out


def _expected_is_meaningful(value) -> bool:
    """Does the ORACLE's value carry information? A column that is None/empty on both sides cannot go red."""
    if value is None:
        return False
    if isinstance(value, (str, list, dict, tuple)):
        return len(value) > 0
    return True


# --------------------------------------------------------------------------- main
def sweep(seed: int = 0, schemas: int = 120, browser: bool = True, show: int = 8, shapes=SHAPES):
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
                    proc = dict((n, p) for n, _s, p in schema["fields"]).get(name)
                    bucket = proc or "(no processor)"
                    by_proc[bucket][v] += 1
                    if _expected_is_meaningful(want):
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

    if examples:
        print("\n  divergences (first few):")
        for key, name, got, want, snip in examples:
            print(f"    [{key}] {name}\n        frostwork={got}\n        parsel   ={want}\n        html: {snip!r}")

    gate = stat["DIVERGE"] + stat["CRASH"]
    print(f"\n  GATE: DIVERGE+CRASH = {gate}  ->  {'PASS' if gate == 0 else 'FAIL'}")
    sys.exit(1 if gate else 0)


if __name__ == "__main__":
    main()
