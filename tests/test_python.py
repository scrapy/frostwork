"""Tests for the Frostwork Python bindings: the `extract` primitive, the declarative `Page`/`Item`
layer, and the web-poet integration. Run with `.venv/bin/python -m pytest tests/test_python.py`.

Correctness of the *engine* is proven by the Rust differential (`tools/diff_lxml.py`); these tests
cover the Python surface (marshalling, cardinality, item assembly, web-poet wiring) plus a small
parsel cross-check that values survive the FFI boundary unchanged.
"""

import asyncio

import pytest

import frostwork
from frostwork import Page

PRODUCT = (
    b"<div class=product><h1>Widget</h1><span class=price>$9</span>"
    b"<a href=/p/1>buy</a><img src=/a.png><img src=/b.png>"
    b"<div class=desc>Warm <b>and</b> dry.</div></div>"
)


def _oracle():
    """The Parsel/lxml oracle for a cross-check, or skip the test.

    Value parity is only defined against **libxml2 >= 2.14** (docs/TESTING.md, tools/oracle.py): an
    older vendored libxml2 parses CR-in-attribute-values and a raw `<` in text differently, so a
    mismatch there is the oracle's behaviour, not Frostwork's. A pinned lxml does not pin the libxml2 it
    carries — the lxml 6.1.1 Windows wheel ships 2.11.9 — so this has to be checked at run time rather
    than assumed from requirements-test.txt.
    """
    parsel = pytest.importorskip("parsel")
    etree = pytest.importorskip("lxml.etree")
    if etree.LIBXML_VERSION < (2, 14):
        got = ".".join(map(str, etree.LIBXML_VERSION))
        pytest.skip(f"oracle libxml2 is {got}; value parity is defined against >= 2.14")
    return parsel


def _count_scans(monkeypatch, cls, calls):
    """Wrap a FrostPage class's pre-compiled native Plan with a proxy that counts each scan, so a test
    can assert every field is answered by ONE pass (the plan is compiled once, at class creation)."""
    inner = cls._frostwork_plan

    class _CountingPlan:
        def extract(self, *a, **k):
            calls["n"] += 1
            return inner.extract(*a, **k)

        def extract_grouped(self, *a, **k):
            calls["n"] += 1
            return inner.extract_grouped(*a, **k)

    monkeypatch.setattr(cls, "_frostwork_plan", _CountingPlan())


# --------------------------------------------------------------------------- primitive
def test_extract_columns_in_query_order():
    cols = frostwork.extract(PRODUCT, ["h1::text", "img::attr(src)", "//a/@href"])
    assert cols == [["Widget"], ["/a.png", "/b.png"], ["/p/1"]]


def test_extract_unsupported_query_fails_fast_by_default():
    query = "div:has(.a .b)::text"
    with pytest.raises(frostwork.UnsupportedSelector, match=":has"):
        frostwork.extract(PRODUCT, [query])
    # The engine remains safely permissive when a caller explicitly asks for it.
    assert frostwork.extract(PRODUCT, [query], strict=False) == [[]]


def test_extract_accepts_str_and_bytearray():
    assert frostwork.extract("<p>hi</p>", ["p::text"]) == [["hi"]]
    assert frostwork.extract(bytearray(b"<p>hi</p>"), ["p::text"]) == [["hi"]]


def test_extract_explicit_encoding_label():
    # windows-1252 e-acute (0xE9), passed as a charset label the way Scrapy would
    cols = frostwork.extract(b"<p>caf\xe9</p>", ["p::text"], "windows-1252")
    assert cols == [["café"]]


def test_extract_over_member_budget_raises_valueerror():
    # >128 member selectors is a CALLER bug (schema too big), distinct from an unsupported query;
    # silence would surface as mysteriously-empty columns, so the binding raises loudly.
    with pytest.raises(ValueError, match="member selectors"):
        frostwork.extract(b"<b class=c0>x</b>", [f".c{i}::text" for i in range(130)])


def test_extract_over_sibling_budget_raises_valueerror():
    # >64 sibling trigger bits would overflow-shift the u64 gate frames (panic/aliasing) — rejected.
    with pytest.raises(ValueError, match="sibling-combinator"):
        frostwork.extract(b"<div></div>", [f".a{i} + .b{i}::text" for i in range(66)])


def test_native_extract_accepts_bytes_only():
    # the *native* function takes `bytes` (PyO3 &[u8]); bytearray/memoryview go through the wrapper.
    assert frostwork._frostwork.extract(b"<p>hi</p>", ["p::text"], None) == [["hi"]]
    with pytest.raises(TypeError):
        frostwork._frostwork.extract(bytearray(b"<p>hi</p>"), ["p::text"], None)


def test_extract_rejects_bare_string_queries():
    # a single selector string would otherwise be exploded into characters; reject it clearly.
    with pytest.raises(TypeError, match="iterable of selector strings"):
        frostwork.extract(b"<h1>x</h1>", "h1::text")
    with pytest.raises(TypeError, match="iterable of selector strings"):
        frostwork.extract_grouped(b"<h1>x</h1>", "h1::text", [])


def test_extract_unknown_encoding_label_raises():
    # a label that names NO encoding must fail loudly, not silently fall through to sniffing.
    with pytest.raises(ValueError, match="unknown encoding label"):
        frostwork.extract(b"<p>x</p>", ["p::text"], "not-a-real-charset")


def test_extract_real_but_non_whatwg_label_is_ignored_not_raised():
    # `utf-7` is a real Python codec and NOT a WHATWG label, so w3lib (and therefore Scrapy's
    # `response.encoding`, the documented input here) can hand it to us off a `Content-Type` header.
    # WHATWG's rule for such a label is "failure, continue" — ignore it and keep sniffing — which is
    # what browsers do. Raising would crash a spider on publisher-controlled input; a crawled page
    # whose header said `charset=UTF-7` while its own <meta> said UTF-8 did exactly that.
    page = b'<meta charset="utf-8"><p>caf\xc3\xa9</p>'
    assert frostwork.extract(page, ["p::text"], "UTF-7") == [["caf\u00e9"]]
    assert frostwork.extract(page, ["p::text"], "UTF-7") == frostwork.extract(page, ["p::text"], None)


def test_extract_normalizes_python_codec_spellings():
    # `latin-1`/`iso-8859-1` are Python codec names, not WHATWG labels; accept them via codecs.
    assert frostwork.extract(b"<p>caf\xe9</p>", ["p::text"], "latin-1") == [["café"]]
    assert frostwork.extract(b"<p>caf\xe9</p>", ["p::text"], "iso-8859-1") == [["café"]]


def test_extract_str_with_non_utf8_label_raises():
    # already-decoded str is tokenized as UTF-8; a conflicting label would double-transcode silently.
    with pytest.raises(ValueError, match="already-decoded str"):
        frostwork.extract("<p>café</p>", ["p::text"], "windows-1252")
    # a UTF-8 label on str is consistent and allowed
    assert frostwork.extract("<p>café</p>", ["p::text"], "utf-8") == [["café"]]


def test_extract_grouped_normalizes_list_shaped_groups():
    # groups loaded from JSON arrive as lists, not tuples; normalize instead of raising opaquely.
    html = b"<div class=c><a>x</a></div>"
    flat, grouped = frostwork.extract_grouped(html, [], [[".c", [["t", "a::text"]]]])
    assert grouped == [[[["x"]]]]


def test_extract_deeply_nested_is_declines_without_crashing():
    # a pathological `:is(:is(:is(...)))` must decline (unsupported), never overflow the stack.
    deep = ":is(" * 5000 + "a" + ")" * 5000
    assert frostwork.extract(b"<a>x</a>", [deep], strict=False) == [[]]
    with pytest.raises(frostwork.UnsupportedSelector):
        frostwork.extract(b"<a>x</a>", [deep])


def test_extract_releases_gil_for_parallel_scans():
    # the scan runs without the GIL, so threads scale; this asserts real (not serialized) parallelism.
    import os
    import threading
    import time

    if (os.cpu_count() or 1) < 2:
        pytest.skip("needs >= 2 cores to observe parallelism")

    html = b"<html><body>" + b"<div class=x><span class=p>v</span></div>" * 20000 + b"</body></html>"
    plan = frostwork.Page().field_all("p", ".p::text")
    plan.extract(html)  # compile once

    ROUNDS, THREADS, REPS = 8, 2, 3

    def work(rounds):
        for _ in range(rounds):
            plan.extract(html)

    def best_of(fn):
        # Take the MINIMUM of a few runs. Scheduler interference on a shared CI runner only ever makes
        # a measurement slower, so the min is the cleanest estimate of each mode's real cost — a single
        # sample of a ~10ms workload is what made this assertion flaky (macos runner: ratio 1.24).
        return min((fn() for _ in range(REPS)), default=0.0)

    def timed_serial():
        t0 = time.perf_counter()
        work(ROUNDS * THREADS)  # same total work as the threaded run below
        return time.perf_counter() - t0

    def timed_parallel():
        threads = [threading.Thread(target=work, args=(ROUNDS,)) for _ in range(THREADS)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return time.perf_counter() - t0

    serial, parallel = best_of(timed_serial), best_of(timed_parallel)

    # Fully serialized would give ~1.0x; require a clear win while tolerating loaded CI machines.
    assert serial / parallel > 1.3, f"no parallel speedup (serial={serial:.3f}s parallel={parallel:.3f}s)"


# --------------------------------------------------------------------------- Page / Item
def test_page_one_call_fills_all_fields():
    page = (
        Page()
        .field("title", "h1::text")
        .field("price", ".price::text")
        .field_all("images", "img::attr(src)")
        .field_join("desc", ".desc ::text", "")
    )
    item = page.extract(PRODUCT)
    assert item.get("title") == "Widget"
    assert item.get_all("images") == ["/a.png", "/b.png"]
    assert item.value("desc") == "Warm and dry."
    assert item.to_dict() == {
        "title": "Widget",
        "price": "$9",
        "images": ["/a.png", "/b.png"],
        "desc": "Warm and dry.",
    }


def test_item_missing_field():
    item = Page().field("title", "h1::text").field("sub", "h2::text").extract(PRODUCT)
    assert item.get("sub") is None  # matched nothing
    assert item.get_all("sub") == []
    assert item.get("absent") is None  # not a declared field
    assert item.value("absent") is None


def test_item_to_json_shapes_and_unicode():
    item = (
        Page()
        .field("title", "h1::text")
        .field("sub", "h2::text")  # -> null
        .field_all("images", "img::attr(src)")
        .extract(PRODUCT)
    )
    assert item.to_json() == (
        '{"title": "Widget", "sub": null, "images": ["/a.png", "/b.png"]}'
    )
    # non-ASCII is preserved, not \u-escaped
    uni = Page().field("p", "p::text").extract(b"<p>caf\xc3\xa9</p>")
    assert uni.to_json() == '{"p": "café"}'


def test_page_many_and_one():
    grid = (
        b"<div class=card><a href=/1>A</a></div>"
        b"<div class=card><a href=/2>B</a></div>"
    )
    page = (
        Page()
        .many("cards", ".card", {"title": "a::text", "href": "a::attr(href)"})
        .one("first", ".card", {"title": "a::text"})
    )
    item = page.extract(grid)
    assert item.value("cards") == [
        {"title": "A", "href": "/1"},
        {"title": "B", "href": "/2"},
    ]
    assert item.value("first") == {"title": "A"}
    assert Page().one("x", ".nope", {"t": "a::text"}).extract(grid).value("x") is None


def test_page_many_rich_subspecs_match_webpoet():
    # Page.many now accepts per-subfield cardinality tuples like webpoet.Many, not just first-match.
    grid = (
        b"<div class=card><a href=/1>A</a><b class=t>x</b><b class=t>y</b></div>"
        b"<div class=card><a href=/2>B</a></div>"
    )
    page = Page().many("cards", ".card", {
        "href": "a::attr(href)",                 # bare string -> first (back-compat)
        "tags": (".t::text", "all"),             # -> list
        "joined": (".t::text", "join", "|"),     # -> joined string
    })
    assert page.extract(grid).value("cards") == [
        {"href": "/1", "tags": ["x", "y"], "joined": "x|y"},
        {"href": "/2", "tags": [], "joined": ""},
    ]


def test_item_get_on_group_name():
    # get()/get_all() now surface group values (previously silently None/[]).
    grid = b"<div class=card><a>A</a></div><div class=card><a>B</a></div>"
    page = (Page()
            .many("cards", ".card", {"t": "a::text"})
            .one("first", ".card", {"t": "a::text"}))
    item = page.extract(grid)
    assert item.get("cards") == {"t": "A"}                       # many -> first row
    assert item.get_all("cards") == [{"t": "A"}, {"t": "B"}]     # many -> all rows
    assert item.get("first") == {"t": "A"}                       # one -> the row
    assert item.get_all("first") == [{"t": "A"}]                 # one -> single-row list


def test_page_reusable_across_pages():
    page = Page().field("t", "h1::text")
    assert page.extract(b"<h1>A</h1>").get("t") == "A"
    assert page.extract(b"<h1>B</h1>").get("t") == "B"


# --------------------------------------------------------------------------- compiled plan (reuse)


def test_native_plan_reuse_matches_oneshot():
    import frostwork
    from frostwork._frostwork import Plan

    queries = ["h1::text", ".price::text", "img::attr(src)"]
    plan = Plan(queries, [])
    for _ in range(3):  # same compiled plan, many pages
        assert plan.extract(PRODUCT) == frostwork.extract(PRODUCT, queries)
    # grouped path is identical to the one-shot extract_grouped too
    body = b"<div class=card><h3><a>A</a></h3></div><div class=card><h3><a>B</a></h3></div>"
    gplan = Plan([], [(".card", [("t", "h3 a::text")])])
    assert gplan.extract_grouped(body) == frostwork.extract_grouped(body, [], [(".card", [("t", "h3 a::text")])])


def test_native_plan_over_budget_raises_at_construction():
    # the budget is validated once, when the plan is compiled — not per page
    from frostwork._frostwork import Plan

    with pytest.raises(ValueError, match="member selectors"):
        Plan([f".c{i}::text" for i in range(130)], [])


def test_page_compiles_plan_once_and_reuses():
    page = Page().field("t", "h1::text").field_all("imgs", "img::attr(src)")
    assert page._plan is None  # not built until first extract
    assert page.extract(PRODUCT).get("t") == "Widget"
    plan1 = page._plan
    assert plan1 is not None
    # a second page reuses the SAME compiled plan object (no recompile)
    assert page.extract(b"<h1>Other</h1>").get("t") == "Other"
    assert page._plan is plan1
    # mutating the schema invalidates the cache; the next extract rebuilds and stays correct
    page.field("p", ".price::text")
    assert page._plan is None
    assert page.extract(PRODUCT).get("p") == "$9"
    assert page._plan is not None and page._plan is not plan1


def test_frostpage_compiles_plan_at_class_definition():
    from frostwork.webpoet import FrostPage, field

    class P(FrostPage):
        a = field("h1::text")
        b = field(".price::text")

    # the plan exists on the class before any instance/response
    assert P._frostwork_plan is not None
    assert P._frostwork_flat_names == ["a", "b"]


# --------------------------------------------------------------------------- web-poet
def _resp(body=PRODUCT, encoding=None, url="http://example.com/p/1"):
    from web_poet import HttpResponse

    kwargs = {"url": url, "body": body}
    if encoding is not None:
        kwargs["encoding"] = encoding
    return HttpResponse(**kwargs)


def test_frostpage_to_item():
    from frostwork.webpoet import FrostPage, field

    class ProductPage(FrostPage):
        name = field("h1::text")
        price = field(".price::text")
        images = field("img::attr(src)", all=True)
        desc = field(".desc ::text", join="")
        link = field("//a/@href")

    page = ProductPage(response=_resp())
    # attribute access works (real web_poet fields)
    assert page.name == "Widget"
    assert page.images == ["/a.png", "/b.png"]
    item = asyncio.run(page.to_item())
    assert item == {
        "name": "Widget",
        "price": "$9",
        "images": ["/a.png", "/b.png"],
        "desc": "Warm and dry.",
        "link": "/p/1",
    }


def test_frostpage_is_one_pass(monkeypatch):
    """Every field on a page object must be answered by a SINGLE scan of the compiled plan."""
    from frostwork import webpoet

    class P(webpoet.FrostPage):
        a = webpoet.field("h1::text")
        b = webpoet.field(".price::text")
        c = webpoet.field("img::attr(src)", all=True)
        d = webpoet.field("//a/@href")

    calls = {"n": 0}
    _count_scans(monkeypatch, P, calls)
    asyncio.run(P(response=_resp()).to_item())
    assert calls["n"] == 1, f"expected one scan, got {calls['n']}"


def test_frostpage_returns_typed_item():
    import attrs
    from web_poet import Returns

    from frostwork.webpoet import FrostPage, field

    @attrs.define
    class Product:
        name: str
        price: str

    class ProductPage(FrostPage, Returns[Product]):
        name = field("h1::text")
        price = field(".price::text")

    item = asyncio.run(ProductPage(response=_resp()).to_item())
    assert item == Product(name="Widget", price="$9")


def test_frostpage_mixes_with_handwritten_field():
    from web_poet import field as wp_field

    from frostwork.webpoet import FrostPage, field

    class P(FrostPage):
        name = field("h1::text")

        @wp_field
        def slug(self):
            return self.name.lower()

    item = asyncio.run(P(response=_resp()).to_item())
    assert item == {"name": "Widget", "slug": "widget"}


def test_frostpage_field_inheritance():
    from frostwork.webpoet import FrostPage, field

    class Base(FrostPage):
        name = field("h1::text")

    class Child(Base):
        price = field(".price::text")

    item = asyncio.run(Child(response=_resp()).to_item())
    assert item == {"name": "Widget", "price": "$9"}


def test_frost_schema_includes_inherited_and_groups():
    from frostwork.webpoet import FrostPage, field, Many

    class Base(FrostPage):
        name = field("h1::text")

    class Child(Base):
        price = field(".price::text")
        offers = Many(".offer", price=field(".p::text"))

    schema = Child.frost_schema()
    # inherited flat field + own field both present (public introspection, no private dict access)
    assert schema["fields"] == {"name": "h1::text", "price": ".price::text"}
    assert schema["groups"] == {"offers": (".offer", {"price": ".p::text"})}


def test_frostpage_encoding_from_response():
    from frostwork.webpoet import FrostPage, field

    class P(FrostPage):
        p = field("p::text")

    # response declares windows-1252; FrostPage must scan with that encoding
    resp = _resp(body=b"<p>caf\xe9</p>", encoding="cp1252")
    item = asyncio.run(P(response=resp).to_item())
    assert item == {"p": "café"}


def test_field_all_and_join_mutually_exclusive():
    from frostwork.webpoet import field

    with pytest.raises(ValueError):
        field("x::text", all=True, join=" ")


# --------------------------------------------------------------------------- .map / .re_first sugar
def test_page_field_map():
    # map= transforms the shaped value; get/get_all stay raw
    item = (
        Page()
        .field("price", ".price::text", map=lambda s: s.lstrip("$"))
        .field_all("images", "img::attr(src)", map=len)
        .extract(PRODUCT)
    )
    assert item.value("price") == "9"
    assert item.get("price") == "$9"          # raw accessor is untransformed
    assert item.value("images") == 2          # map applied to the whole list
    assert item.to_dict() == {"price": "9", "images": 2}


def test_frostpage_field_map_and_re_first():
    from frostwork.webpoet import FrostPage, field

    class P(FrostPage):
        price = field(".price::text").map(lambda s: s.lstrip("$")).map(float)
        symbol = field(".price::text").re_first(r"^\D+")
        n_images = field("img::attr(src)", all=True).map(len)

    item = asyncio.run(P(response=_resp()).to_item())
    assert item == {"price": 9.0, "symbol": "$", "n_images": 2}


def test_frostpage_map_shares_the_one_pass(monkeypatch):
    # transforms are pure post-processing — they must not add extract() calls
    from frostwork.webpoet import FrostPage, field

    class P(FrostPage):
        a = field("h1::text").map(str.upper)
        b = field(".price::text").re_first(r"\d+")

    calls = {"n": 0}
    _count_scans(monkeypatch, P, calls)
    item = asyncio.run(P(response=_resp()).to_item())
    assert item == {"a": "WIDGET", "b": "9"}
    assert calls["n"] == 1


def test_re_first_no_match_is_none():
    from frostwork.webpoet import FrostPage, field

    class P(FrostPage):
        digits = field("h1::text").re_first(r"\d+")   # "Widget" has none

    item = asyncio.run(P(response=_resp()).to_item())
    assert item == {"digits": None}


def test_re_first_on_all_field_errors_at_declaration():
    from frostwork.webpoet import field

    # re_first on a list-valued (all=True) field would silently yield None per page — reject it loudly.
    with pytest.raises(ValueError, match="all=True"):
        field("a::text", all=True).re_first(r"\d+")
    # join= is fine (the shaped value is a scalar string)
    assert field("a::text", join=" ").re_first(r"\d+") is not None


# --------------------------------------------------------------------------- Many / One (grouped)
GRID = (
    b"<div class=grid>"
    b"<div class=card><h3><a href=/1>A</a></h3><span class=price>$1</span></div>"
    b"<div class=card><h3><a href=/2>B</a></h3><span class=price>$2</span></div>"
    b"</div>"
)


def test_many_yields_dict_rows():
    from frostwork.webpoet import FrostPage, Many, field

    class Listing(FrostPage):
        products = Many(".card", title=field("h3 a::text"),
                        href=field("h3 a::attr(href)"), price=field(".price::text"))

    item = asyncio.run(Listing(response=_resp(body=GRID)).to_item())
    assert item == {"products": [
        {"title": "A", "href": "/1", "price": "$1"},
        {"title": "B", "href": "/2", "price": "$2"},
    ]}


def test_many_builds_typed_item_and_map():
    import attrs
    from frostwork.webpoet import FrostPage, Many, field

    @attrs.define
    class Card:
        title: str
        price: float

    class Listing(FrostPage):
        products = Many(".card", item=Card, title=field("h3 a::text"),
                        price=field(".price::text").map(lambda s: float(s.lstrip("$"))))

    products = asyncio.run(Listing(response=_resp(body=GRID)).to_item())["products"]
    assert products == [Card("A", 1.0), Card("B", 2.0)]


def test_one_yields_first_row_or_none():
    from frostwork.webpoet import FrostPage, One, field

    class P(FrostPage):
        first = One(".card", title=field("h3 a::text"))
        missing = One(".nope", x=field("span::text"))

    item = asyncio.run(P(response=_resp(body=GRID)).to_item())
    assert item == {"first": {"title": "A"}, "missing": None}


def test_flat_and_grouped_one_pass(monkeypatch):
    from frostwork.webpoet import FrostPage, Many, field

    class Listing(FrostPage):
        heading = field("h1::text")
        products = Many(".card", title=field("h3 a::text"))

    calls = {"n": 0}
    _count_scans(monkeypatch, Listing, calls)
    body = b"<h1>Shop</h1>" + GRID
    item = asyncio.run(Listing(response=_resp(body=body)).to_item())
    assert item["heading"] == "Shop"
    assert item["products"] == [{"title": "A"}, {"title": "B"}]
    assert calls["n"] == 1  # flat + grouped share ONE pass


def test_many_matches_parsel_per_container():
    parsel = _oracle()
    import frostwork

    body = GRID
    flat, grouped = frostwork.extract_grouped(
        body, [], [(".card", [("t", "h3 a::text"), ("p", ".price::text")])]
    )
    sel = parsel.Selector(body=body)
    oracle = [[c.css("h3 a::text").getall(), c.css(".price::text").getall()]
              for c in sel.css(".card")]
    assert grouped[0] == oracle


# --------------------------------------------------------------------------- parsel parity
def test_matches_parsel_across_selectors():
    """Values crossing the FFI boundary equal Parsel's on a normal page (spot cross-check;
    the exhaustive gate is the Rust differential)."""
    parsel = _oracle()
    html = (
        "<html><body><div class='product'><h1>Nice Widget</h1>"
        "<span class='price'>$9.99</span>"
        "<a href='/p/1'>one</a><a href='/p/2'>two</a>"
        "<ul><li>x</li><li>y</li></ul></div></body></html>"
    )
    sel = parsel.Selector(text=html)
    checks = [
        ("h1::text", sel.css("h1::text").getall()),
        (".price::text", sel.css(".price::text").getall()),
        ("a::attr(href)", sel.css("a::attr(href)").getall()),
        ("li::text", sel.css("li::text").getall()),
        ("//a/@href", sel.xpath("//a/@href").getall()),
        ("//li/text()", sel.xpath("//li/text()").getall()),
    ]
    queries = [q for q, _ in checks]
    cols = frostwork.extract(html.encode("utf-8"), queries)
    for (query, expected), got in zip(checks, cols):
        assert got == expected, f"{query}: frostwork={got} parsel={expected}"


# The bytes Parsel deletes before libxml2 ever sees them, at the document EDGES — where the engine reads
# raw bytes and Parsel reads `text.strip().replace("\x00", "")`. Every case here is a page shape that
# returned a silently EMPTY column, so each is graded against the oracle rather than against a literal.
_FRAME = b"<html a=1><head><title>T</title></head><body><p>p</p></body></html>"
_EDGE_CASES = [
    # a NUL is not whitespace, so it BLOCKS the strip: the space before it survives ...
    ("nul blocks the trailing strip", b"<option>x \x00", None),
    # ... and so does the U+FEFF it keeps away from offset 0, which is then a character, not a BOM
    ("nul blocks the leading strip", b"\x00 \xEF\xBB\xBF" + _FRAME, None),
    # VERTICAL TAB is in Python's strip set and not in Rust's `is_ascii_whitespace`
    ("vertical tab exposes the bom", b"\x0b\xEF\xBB\xBF" + _FRAME, None),
    ("vertical tab alone", b"\x0b" + _FRAME, None),
    ("vertical tab among spaces", b" \x0b\n" + _FRAME, None),
    ("form feed", b"\x0c" + _FRAME, None),
    # the whitespace half of the strip is not UTF-8-gated (Parsel strips the DECODED text) ...
    ("vertical tab, windows-1252", b"\x0b" + _FRAME, "windows-1252"),
    # ... while the BOM half is: in windows-1252 those three bytes are three real characters
    ("bom bytes are content in cp1252", b"\x0b\xEF\xBB\xBF" + _FRAME, "windows-1252"),
    ("trailing vertical tab", b"<select><option>a<option class=c>\x0b", None),
    ("vertical tab inside is content", b"<p>a\x0bb</p>", None),
]


@pytest.mark.parametrize("label,body,encoding", _EDGE_CASES, ids=[c[0] for c in _EDGE_CASES])
def test_input_edges_match_parsel(label, body, encoding):
    """Parsel's own input normalization, at the document edges, against `Selector(text=…)`.

    That path and not `Selector(body=…)`, and the two are NOT interchangeable here: parsel strips before
    deleting NUL for `text=` and after it for `body=`, so they disagree on the first two cases above. The
    text path is what Scrapy's `response.selector` and web-poet's `HttpResponse` are on, so it is the one
    a scraper compares against.
    """
    parsel = _oracle()
    sels = ["option::text", "head title::text", "html::attr(a)", "p::text"]
    text = body.decode(encoding or "utf-8", "replace")
    theirs = [parsel.Selector(text=text, type="html").css(s).getall() for s in sels]
    mine = frostwork.extract(body, sels, encoding)
    for sel, got, want in zip(sels, mine, theirs):
        assert got == want, f"{label}: {sel}: frostwork={got} parsel={want}"


# --------------------------------------------------------------------------- schema audit / strict


def test_check_reports_supported_and_unsupported():
    r = frostwork.check(
        ["h1::text", "div:has(.a .b)::text", "//a[position()<2]/@href"],
        [("offers", ".offer", {"price": ".//span/text()", "sib": "a + b::text", "kid": "./h3/text()"})],
    )
    assert not r.ok
    assert not r.over_budget
    assert r.fields[0].supported
    unsup = {f.name: f.reason for f in r.unsupported}
    assert ":has()" in unsup["[1]"]
    assert "positional" in unsup["[2]"]
    assert "sibling combinator" in unsup["sib"]
    assert "descendant" in unsup["kid"]  # ./ child anchor -> use .//
    assert r.groups[0].container.supported  # .offer is fine


def test_check_reads_a_dict_as_name_to_selector():
    # Regression: `{name: selector}` — the shape Page/FrostPage schemas are written in, and what
    # `FrostPage.frost_schema()` returns — used to be ITERATED, auditing the field NAMES as selectors.
    # A bare name like `title` is a valid type selector, so every field reported supported and
    # `report.ok` was True: a silently green audit of a schema that was never looked at.
    good, bad = "h1::text", "div:has(.a .b)::text"
    for queries in ({"title": good, "bad": bad}, [("title", good), ("bad", bad)]):
        r = frostwork.check(queries)
        assert [(f.name, f.selector, f.supported) for f in r.fields] == [
            ("title", good, True), ("bad", bad, False)
        ]
        assert not r.ok
        assert [f.name for f in r.unsupported] == ["bad"]
        with pytest.raises(frostwork.UnsupportedSelector, match="div:has"):
            r.raise_for_status()
    # unlabelled selectors keep their positional names
    r = frostwork.check([good, bad])
    assert [(f.name, f.selector, f.supported) for f in r.fields] == [
        ("[0]", good, True), ("[1]", bad, False)
    ]
    assert not r.ok


def test_check_reads_a_dict_of_groups_and_of_subfields():
    subs = {"price": ".//span/text()", "kid": "./h3/text()"}  # `./x` child anchor -> unsupported
    for groups in (
        {"offers": (".offer", subs)},                    # {name: (container, subfields)}
        {"offers": (".offer", list(subs.items()))},      # ... with sub-fields as pairs
        [("offers", ".offer", subs)],                    # (name, container, subfields)
        [("offers", ".offer", list(subs.items()))],
    ):
        r = frostwork.check([], groups)
        g = r.groups[0]
        assert g.name == "offers"
        assert (g.container.selector, g.container.supported) == (".offer", True)
        assert [(s.name, s.selector) for s in g.subfields] == list(subs.items())
        assert not r.ok
        assert [s.name for s in r.unsupported] == ["kid"]
    # the bare `extract_grouped` shape still auto-names; a dict of sub-fields is read as one there too
    for groups in ([(".offer", {"price": ".//span/text()"})], [(".offer", [("price", ".//span/text()")])]):
        r = frostwork.check([], groups)
        assert r.ok
        assert r.groups[0].name == "group[0]"
        assert [(s.name, s.selector) for s in r.groups[0].subfields] == [("price", ".//span/text()")]


def test_check_audits_a_frostpage_schema_round_trip():
    # `frost_schema()` hands back exactly the two Mapping shapes `check` now accepts, so a schema
    # exported for tooling can be audited as-is instead of coming back green-but-unread.
    from frostwork.webpoet import FrostPage, Many, field

    class Exported(FrostPage, strict=False):
        title = field("h1::text")
        blurb = field("div:contains('x')::text")
        offers = Many(".offer", price=field(".//span/text()"))

    schema = Exported.frost_schema()
    r = frostwork.check(schema["fields"], schema["groups"])
    assert [f.name for f in r.unsupported] == ["blurb"]
    assert r.groups[0].name == "offers"
    assert [(s.name, s.selector) for s in r.groups[0].subfields] == [("price", ".//span/text()")]
    assert {f.name: f.supported for f in r.fields} == {"title": True, "blurb": False}


@pytest.mark.parametrize(
    "queries",
    ["h1::text", ["h1::text", 5], [("t", "h1::text", "all")], [{"t": "h1::text", "b": ".p::text"}]],
)
def test_check_rejects_unknown_query_shapes(queries):
    # Anything that is not one of the three documented shapes names them, rather than auditing
    # whatever `list()`/`[0]`/`[1]` happens to yield (a 2-key dict would "unpack" into its keys).
    with pytest.raises(TypeError, match="`queries` must be"):
        frostwork.check(queries)


@pytest.mark.parametrize(
    "groups",
    [["offers"], {"offers": ".offer"}, [(".offer", 5)], [(".offer", ["price"])], [(".offer",)]],
)
def test_check_rejects_unknown_group_shapes(groups):
    with pytest.raises(TypeError, match="each group must be"):
        frostwork.check([], groups)


def test_extract_rejects_dict_queries_rather_than_extracting_the_field_names():
    # The extraction twin of the audit bug, and worse: iterating `{name: selector}` used to extract
    # the NAMES (`title` matched <title>), a silently WRONG value. `extract` returns positional
    # columns, so a named schema means `Page`; a dict here is a caller mistake either way.
    html = b"<html><head><title>T</title></head><body><h1>H</h1></body></html>"
    schema = {"title": "h1::text"}
    for call in (lambda: frostwork.extract(html, schema),
                 lambda: frostwork.extract_grouped(html, schema, [])):
        with pytest.raises(TypeError, match="KEYS"):
            call()
    assert frostwork.extract(html, list(schema.values())) == [["H"]]
    assert Page().field("title", "h1::text").extract(html).get("title") == "H"


def test_check_rejects_xpath_variable_and_unquoted_operands():
    # Reported by Jan Seidler: `//*[@id=$pid]` (a parsel XPath-variable query, used in a production PO)
    # passed the audit, then matched an element whose id literally was `$pid` — a WRONG value under a
    # "supported" verdict. Frostwork takes no variable bindings, so the query must be unsupported. Same
    # for other non-literal operands: XPath compares `[@a=2]` numerically and `[@a=b]` against child
    # `<b>` elements, neither of which is a byte compare.
    html = b'<html><body><div id="$pid">LITERAL</div><span x="2">two</span>'
    html += b'<span x="02">oh-two</span><em id="foo">byname</em></body></html>'
    for q in ('//*[@id=$pid]', '//span[contains(@x,$v)]/text()', '//em[@id=foo]/text()',
              '//span[@x=2]/text()'):
        field = frostwork.check([q]).fields[0]
        assert not field.supported, q
        assert frostwork.extract(html, [q], strict=False)[0] == []  # no fallback, no wrong value
        with pytest.raises(frostwork.UnsupportedSelector):
            frostwork.extract(html, [q])
    assert "variable" in frostwork.check(['//*[@id=$pid]']).fields[0].reason
    assert "unquoted" in frostwork.check(['//span[@x=2]/text()']).fields[0].reason
    # a `$` INSIDE a string literal is data, not a variable — still supported, still correct
    r = frostwork.check(['//div[@id="$pid"]/text()', '//div[contains(@id,"$p")]/text()'])
    assert r.ok
    assert frostwork.extract(html, ['//div[@id="$pid"]/text()'])[0] == ["LITERAL"]


def test_check_rejects_non_ident_unquoted_css_attr_values():
    # Found while fixing the XPath case above: cssselect raises SelectorSyntaxError for these, so a
    # non-empty column would be data for a selector Parsel refuses to run. Unsupported (empty) instead;
    # the quoted spelling stays supported and matches.
    html = b'<html><body><a href="/p/1" id="2">A</a></body></html>'
    for q in ("a[href^=/p]::text", "a[id=2]::text", "a[id=$v]::text", "a[id=--v]::text"):
        assert not frostwork.check([q]).fields[0].supported, q
        assert frostwork.extract(html, [q], strict=False)[0] == []
    r = frostwork.check(['a[href^="/p"]::text', 'a[id="2"]::text', "a[href*=p]::text"])
    assert r.ok  # quoted values are unrestricted; an ident-shaped unquoted value is fine
    assert frostwork.extract(html, ['a[id="2"]::text'])[0] == ["A"]


def test_empty_fields_separates_dead_selectors_from_gaps():
    # The runtime half of dead-selector detection: support is static (check), emptiness is per page.
    page = (
        frostwork.Page()
        .field("title", "h1::text")
        .field("gone", ".price::text")
        .many("rows", ".card", {"h": ".//h3/text()"})
        .many("absent", ".nope", {"h": ".//h3/text()"})
    )
    assert page.check().ok  # every selector supported -> emptiness below can only mean "no match"
    item = page.extract(b"<html><body><h1>T</h1><div class='card'><h3>H</h3></div></body></html>")
    assert item.empty_fields() == ["gone", "absent"]
    assert item.get("title") == "T"
    # a matched-but-EMPTY value counts as matched, not empty (an empty attribute value did match)
    attrs = frostwork.Page().field("alt", "img::attr(alt)")
    assert attrs.extract(b'<html><body><img alt="" src="x"></body></html>').empty_fields() == []


def test_check_ok_schema():
    r = frostwork.check(["h1::text", "a::attr(href)"], [("g", ".card", {"t": ".//h3/text()"})])
    assert r.ok
    assert r.raise_for_status() is r  # returns self, no raise
    assert not r.unsupported


def test_check_over_budget_is_flagged_not_raised_by_audit():
    # audit surfaces over-budget as a report flag (extract() still raises ValueError at run time).
    r = frostwork.check([f".c{i}::text" for i in range(130)])
    assert r.over_budget
    assert not r.ok
    assert r.members > r.max_members
    with pytest.raises(frostwork.UnsupportedSelector, match="over budget"):
        r.raise_for_status()


def test_grouped_multi_member_and_deferred_shapes_reject_whole():
    html = b'<div class="root"><p>P<a>x</a></p></div><span><p>S</p></span>'
    groups = [
        ("div, span", [("t", "p::text")]),
        ("div", [("t", "p::text, a::text")]),
        ("div:has(a)", [("t", "div::text")]),
        (".root", [("h", "p:has(a)::text"), ("x", './/p[contains(.,"x")]/text()')]),
    ]
    report = frostwork.check([], [
        ("comma_container", groups[0][0], dict(groups[0][1])),
        ("comma_sub", groups[1][0], dict(groups[1][1])),
        ("has_container", groups[2][0], dict(groups[2][1])),
        ("deferred_subs", groups[3][0], dict(groups[3][1])),
    ])
    assert not report.groups[0].container.supported
    assert not report.groups[1].subfields[0].supported
    assert not report.groups[2].container.supported
    assert all(not s.supported for s in report.groups[3].subfields)
    with pytest.raises(frostwork.UnsupportedSelector):
        frostwork.extract_grouped(html, [], groups)
    assert frostwork.extract_grouped(html, [], groups, strict=False)[1] == [[], [[[]]], [], [[[], []]]]


@pytest.mark.parametrize(
    ("query", "html", "expected"),
    [
        ("div:has(a)::attr(id)", b"<div id=x><a></a></div>", ["x"]),
        ("li:last-child::text", b"<ul><li>x</li></ul>", ["x"]),
        ('//p[.="x"]/text()', b"<p>x</p>", ["x"]),
    ],
)
def test_65_deferred_selectors_remain_live(query, html, expected):
    queries = [query] * 65
    report = frostwork.check(queries)
    assert report.ok
    assert frostwork.extract(html, queries) == [expected] * 65


def test_page_rejects_duplicate_flat_and_group_names():
    with pytest.raises(ValueError, match="duplicate"):
        Page().field("x", "p::text").field("x", "a::text")
    with pytest.raises(ValueError, match="duplicate"):
        Page().field("x", "p::text").many("x", ".row", {"v": "span::text"})
    with pytest.raises(ValueError, match="duplicate"):
        Page().many("x", ".row", {"v": "span::text"}).one("x", ".one", {"v": "b::text"})

    page = Page().field("x", "p::text").many("rows", ".row", {"v": "span::text"})
    assert page.field_names == ["x", "rows"]
    assert len(page) == 2
    assert len(page.extract(b"<p>P</p><div class=row><span>S</span></div>")) == 2


def test_page_fails_fast_by_default_and_allows_permissive_override():
    page = Page().field("title", "h1::text").field("bad", "li:has(.a .b)::text")
    assert not page.check().ok
    with pytest.raises(frostwork.UnsupportedSelector, match=":has"):
        page.extract(b"<h1>Hi</h1>")
    assert page.extract(b"<h1>Hi</h1>", strict=False).get("bad") is None
    permissive = Page(strict=False).field("bad", "li:has(.a .b)::text")
    assert permissive.extract(b"<li>x</li>").get("bad") is None
    # A fully-supported page extracts normally and caches its successful validation.
    good = Page().field("title", "h1::text")
    assert good.extract(b"<h1>Hi</h1>").get("title") == "Hi"


def test_frostpage_check_schema_and_strict_class_def():
    from frostwork.webpoet import FrostPage, field, Many

    class Mixed(FrostPage, strict=False):
        name = field("h1::text")
        bad = field("div:contains('x')::text")
        offers = Many(".offer", price=field(".//span/text()"), oops=field("a ~ b::text"))

    r = Mixed.check_schema()
    assert not r.ok
    names = {f.name for f in r.unsupported}
    assert "bad" in names and "oops" in names

    # Strict validation is the default and fails loudly at definition time.
    with pytest.raises(frostwork.UnsupportedSelector):
        class Broken(FrostPage):
            title = field("h1::text")
            broken = field("div:contains('z')::text")

    # A supported page defines cleanly under the strict default.
    class Good(FrostPage):
        title = field("h1::text")
        links = field("a::attr(href)", all=True)

    assert Good.check_schema().ok


def test_xpath_union_or_descendant_attr_match_parsel():
    parsel = _oracle()
    body = (
        b"<html><body><a href=/1>A</a><b>B</b><a href=/2 x=1>C</a>"
        b"<div id=d href=/self><p><a href=/3>x</a><img src=/i href=/4></p></div></body></html>"
    )
    # value terminals compare exactly; outer-HTML (`//a | //a`) is the documented raw-source-vs-
    # reserialized divergence, so it isn't in this exact-match set (union node-dedup is checked in Rust).
    for q in [
        "//a/text() | //b/text()",
        "//a[@href or @x]/@href",
        "//div//@href",
        "//div//@src",
        "//a[@x]/text() | //b/text()",
    ]:
        mine = frostwork.extract(body, [q])[0]
        theirs = parsel.Selector(body=body, encoding="utf-8").xpath(q).getall()
        assert mine == theirs, f"{q}: frostwork={mine} parsel={theirs}"


def test_positional_matches_parsel():
    parsel = _oracle()
    body = b"<ul><li>a</li>t<li>b</li><span>s</span><li>c</li></ul><ol><li>x</li><li>y</li></ol>"
    css = [
        "li:first-child::text", "li:nth-child(2)::text", "li:nth-of-type(3)::text",
        "li:nth-child(odd)::text", "li:nth-of-type(odd)::text", "*:nth-child(2)::text",
        "li:not(:first-child)::text",
    ]
    xp = ["//li[1]/text()", "//li[2]/text()", "//ul/*[3]/text()"]
    sel = parsel.Selector(body=body, encoding="utf-8")
    for q in css:
        assert frostwork.extract(body, [q])[0] == sel.css(q).getall(), q
    # reverse positions (attached ::text/::attr, CSS + XPath) now MATCH parsel exactly
    for q in [
        "li:last-child::text", "li:last-of-type::text",
        "li:only-child::text", "li:only-of-type::text",
        "li:nth-last-child(1)::text", "li:nth-last-child(2)::text", "li:nth-last-of-type(2)::text",
        "li:nth-last-child(odd)::text",
    ]:
        assert frostwork.extract(body, [q])[0] == sel.css(q).getall(), q
    for q in ["//li[last()]/text()", "//li[last()-1]/text()", "//ul/*[last()]/text()"]:
        assert frostwork.extract(body, [q])[0] == sel.xpath(q).getall(), q
    # SUBTREE reverse terminals are supported too (values recovered by re-scanning the winner's span)
    for q in ["li:last-child ::text", "li:nth-last-child(2) ::text", "li:only-child ::text"]:
        assert frostwork.extract(body, [q])[0] == sel.css(q).getall(), q
    for q in ["//li[last()]//text()", "//li[last()-1]//text()"]:
        assert frostwork.extract(body, [q])[0] == sel.xpath(q).getall(), q
    # Permissive mode exposes the engine's safe empty-column contract for the forms still out of tier
    # (a reverse position on a non-subject/ancestor compound).
    assert frostwork.extract(body, ["li:last-child b::text"], strict=False)[0] == []
    for q in xp:
        # last() is unsupported -> frostwork empty (allowed gap); the rest match exactly
        mine = frostwork.extract(body, [q])[0]
        theirs = sel.xpath(q).getall()
        assert mine == theirs or (mine == [] and "last()" in q), f"{q}: {mine} vs {theirs}"


def test_normalize_space_matches_parsel():
    parsel = _oracle()
    body = (
        b"<html><body><h1>  Hello   <b>big</b>  world </h1><h1> second </h1>"
        b"<a href='  /x '>k</a><p>a\tb\n c</p><ul><li>  </li><li>i2</li></ul></body></html>"
    )
    for q in [
        "normalize-space(//h1)",
        "normalize-space(//p)",
        "normalize-space(//h1/text())",
        "normalize-space(//h1//text())",
        "normalize-space(//a/@href)",
        "normalize-space(//li)",
        "normalize-space(//nope)",       # scalar '' on no match
        "normalize-space(//zzz/@q)",
    ]:
        mine = frostwork.extract(body, [q])[0]
        theirs = parsel.Selector(body=body, encoding="utf-8").xpath(q).getall()
        assert mine == theirs, f"{q}: frostwork={mine} parsel={theirs}"


def test_check_reason_matches_parsel_supported_boundary():
    # The DECISION must agree with the engine: a query parsel accepts but Frostwork doesn't support
    # is reported unsupported; a supported one is reported supported and yields the same column.
    parsel = _oracle()
    html = b"<ul><li class=x>a</li><li><b>b</b></li></ul>"
    # a reverse position is supported on any ONE compound, with the value being the element's own, its
    # subtree, or a DESCENDANT's; a CHILD step into that value tail still isn't (see COMPATIBILITY)
    supported = "li:last-child b::text"
    unsupported = "li:last-child > b::text"
    r = frostwork.check([supported, unsupported])
    assert r.fields[0].supported and not r.fields[1].supported
    # supported one: frostwork column == parsel
    got = frostwork.extract(html, [supported])[0]
    assert got == parsel.Selector(body=html, encoding="utf-8").css(supported).getall()
    # unsupported one: frostwork empty (no fallback), parsel non-empty -> that's the gap check catches
    assert frostwork.extract(html, [unsupported], strict=False)[0] == []


def test_new_axis_and_has_selectors_match_parsel():
    # end-to-end parity for the newly-added coverage: CSS :has(), XPath following-sibling::, and the
    # upward ancestor::/parent:: axes — each cross-checked against parsel through the Python bindings.
    parsel = _oracle()
    # double-quoted attrs so an outer-HTML (raw-source) column equals lxml's re-serialization — the
    # raw-source-vs-reserialized quote style is a documented divergence, out of scope for this parity check
    html = (
        b"<html><body>"
        b"<dl><dt>Price</dt><dd>$10</dd><dt>Size</dt><dd>L</dd></dl>"
        b'<div class="card"><h2>A</h2><a href="/1">buy</a></div>'
        b'<div class="card"><h2>B</h2><span>none</span></div>'
        b'<section><div class="card"><a>deep</a></div></section>'
        b"</body></html>"
    )
    sel = parsel.Selector(body=html, encoding="utf-8")
    css_cases = ["div.card:has(a)", "div.card:has(a)::text", "div.card:has(> a)", "section:has(a)"]
    xpath_cases = [
        "//dt/following-sibling::dd/text()",
        "//a/following-sibling::span",
        "//a/ancestor::div/@class",
        "//a/parent::div",
    ]
    for q in css_cases:
        assert frostwork.extract(html, [q])[0] == sel.css(q).getall(), q
    for q in xpath_cases:
        assert frostwork.extract(html, [q])[0] == sel.xpath(q).getall(), q
    # and they report as supported through the audit
    r = frostwork.check(css_cases + xpath_cases)
    assert r.ok and not r.unsupported


def test_text_content_predicates_match_parsel():
    # XPath text-content predicates on the subject: `.`/`text()` string tests, cross-checked vs parsel.
    parsel = _oracle()
    html = (
        b"<html><body>"
        b"<h2>Price</h2><h2> Price </h2><h2>x<b>bold</b>Price</h2>"
        b"<a class=\"buy\">Buy now</a><a class=\"sell\">Sell</a>"
        b"</body></html>"
    )
    sel = parsel.Selector(body=html, encoding="utf-8")
    cases = [
        '//h2[text()="Price"]/text()',       # existential over direct text nodes
        '//h2[.="Price"]/text()',            # whole string-value equals
        '//h2[contains(text(),"Price")]',    # FIRST direct text node contains
        '//h2[contains(.,"Price")]',         # string-value contains (spans inline children)
        '//a[.="Sell"]/@class',
        '//a[contains(.,"Buy")]/@class',
    ]
    for q in cases:
        assert frostwork.extract(html, [q])[0] == sel.xpath(q).getall(), q
    # a text predicate on a preceding sibling (label->value) is now supported (Case B) — matches parsel
    combo = '//a[.="Buy now"]/following-sibling::a/text()'
    assert frostwork.check([combo]).ok
    assert frostwork.extract(html, [combo])[0] == sel.xpath(combo).getall()


def test_is_where_selectors_match_parsel():
    # CSS :is()/:where() in the supported `[tag|*]:is(...)` shape, cross-checked vs parsel.
    parsel = _oracle()
    html = (
        b"<html><body><h1>A</h1><h2>B</h2><h3>C</h3>"
        b'<div class="a">1</div><div class="b">2</div><div class="c">3</div>'
        b'<a href="/1">L1</a><a data-k="v">L2</a></body></html>'
    )
    sel = parsel.Selector(body=html, encoding="utf-8")
    supported = [":is(h1, h2, h3)::text", "div:is(.a, .b)::text", "div:where(.a, .c)::text",
                 "a:is([href], [data-k])::text"]
    for q in supported:
        assert frostwork.extract(html, [q])[0] == sel.css(q).getall(), q
    assert frostwork.check(supported).ok


def test_nonsubject_sibling_predicate_matches_parsel():
    # Case B: a deferred predicate on a PRECEDING sibling, value from the later sibling — the
    # label->value + filter pattern. XPath (`//C[.="x"]/following-sibling::S`) and CSS (`C:has(..) ~ S`)
    # are both evaluated correctly by lxml/cssselect, so parsel is a valid DIRECT oracle.
    parsel = _oracle()
    html = (
        b"<html><body>"
        b"<dl><dt>Price</dt><dd>$10</dd><dt>Size</dt><dd>L</dd><dt>Price</dt><dd>$20</dd></dl>"
        b"<ul>"
        b'<li class="row"><span class="new">A</span></li><li>a1</li>'
        b'<li class="row"><b>B</b></li><li>a2</li>'
        b"</ul>"
        b"</body></html>"
    )
    sel = parsel.Selector(body=html, encoding="utf-8")
    for q in [
        '//dt[.="Price"]/following-sibling::dd/text()',            # ~ : dd after a Price dt
        '//dt[.="Size"]/following-sibling::dd/text()',
        '//dt[contains(text(),"Pri")]/following-sibling::dd/@class',
        "li:has(.new) ~ li::text",                                 # CSS: li after an li containing .new
        "li:has(.new) + li::text",                                 # adjacent: only the immediate next
        "li:has(b) ~ li::text",                                    # tag inner
    ]:
        got = frostwork.extract(html, [q])[0]
        want = (sel.xpath(q) if q.startswith("//") else sel.css(q)).getall()
        assert got == want, (q, got, want)
    # predicate that fails -> no sibling fires (empty, never a stale/wrong value)
    assert frostwork.extract(html, ["li:has(.absent) ~ li::text"])[0] == []
    assert frostwork.extract(html, ['//dt[.="Nope"]/following-sibling::dd/text()'])[0] == []
    # a positional predicate on the sibling axis stays an unsupported gap (empty), not wrong
    assert frostwork.extract(
        html, ['//dt[.="Price"]/following-sibling::dd[1]/text()'], strict=False
    )[0] == []


def test_has_widened_inners_match_correct_semantics():
    # `:has()` with an id/attribute/`:not` inner is valid CSS that cssselect 1.4.0 REJECTS (raises).
    # Frostwork implements the correct semantics (a documented divergence in our favor). We can't use
    # parsel's `.css(":has(...)")` as the oracle (it errors), so the oracle is a parsel/lxml ancestor
    # walk: `E:has(F)` = the E-nodes that are an ancestor (or, for `:has(> F)`, the parent) of some F-node.
    parsel = _oracle()
    html = (
        b"<html><body>"
        b'<div id="d1"><span data-x="1">a</span></div>'
        b'<div id="d2"><span>b</span></div>'
        b'<div id="d3"><b><i data-x="2">c</i></b></div>'  # deep descendant
        b'<div id="d4"><p id="target">t</p></div>'
        b'<div id="d5"><a href="/x">L</a></div>'
        b'<div id="d6"><a>nohref</a></div>'
        b'<section id="s1"><span>x</span></section>'  # has a non-<p> descendant
        b'<section id="s2"><p>only p</p></section>'  # only <p> descendants
        b"</body></html>"
    )
    sel = parsel.Selector(body=html, encoding="utf-8")

    # premise check: cssselect really can't parse these, so parsel is not a usable direct oracle
    import cssselect
    with pytest.raises(cssselect.SelectorSyntaxError):
        sel.css("div:has([data-x])")

    def has_ids(e_css, f_css, child=False):
        f_roots = [f.root for f in sel.css(f_css)]
        out = []
        for e in sel.css(e_css):
            er = e.root
            hit = False
            for fr in f_roots:
                p = fr.getparent()
                while p is not None:
                    if p is er:
                        hit = True
                        break
                    if child:
                        break  # only the immediate parent counts for `:has(> F)`
                    p = p.getparent()
                if hit:
                    break
            if hit and er.get("id"):
                out.append(er.get("id"))
        return out

    cases = [
        ("div:has([data-x])::attr(id)", "div", "[data-x]", False),
        ("div:has(#target)::attr(id)", "div", "#target", False),
        ("div:has(a[href])::attr(id)", "div", "a[href]", False),
        ("div:has(> a)::attr(id)", "div", "a", True),
        ("section:has(:not(p))::attr(id)", "section", ":not(p)", False),
    ]
    for frost_sel, e_css, f_css, child in cases:
        got = frostwork.extract(html, [frost_sel])[0]
        assert got == has_ids(e_css, f_css, child), frost_sel


def test_is_where_correct_and_semantics_diverges_from_cssselect_bug():
    # `:is()` combined with a class/attr/id or another `:is` is implemented with CORRECT AND semantics
    # (a DOCUMENTED divergence: cssselect 1.4.0 mis-translates it, ORing the base condition with the
    # alternatives). We can't use parsel directly as the oracle here (it IS the bug); instead compare
    # Frostwork against parsel on the equivalent correct comma-EXPANSION, which cssselect handles fine.
    parsel = _oracle()
    html = (
        b'<html><body><div class="a x">1</div><div class="a c">2</div><div class="a">3</div>'
        b'<div class="b x">4</div><div class="c">5</div>'
        b'<a href="/1" class="p">L1</a><a class="q">L2</a><a href="/2" class="p q">L3</a></body></html>'
    )
    sel = parsel.Selector(body=html, encoding="utf-8")
    # (`:is` form, its correct comma-expansion)
    pairs = [
        ("div.a:is(.x, .c)::text", "div.a.x::text, div.a.c::text"),
        ("div:is(.a, .b):is(.x, .c)::text",
         "div.a.x::text, div.a.c::text, div.b.x::text, div.b.c::text"),
        ("a[href]:is(.p, .q)::text", "a[href].p::text, a[href].q::text"),
        ("div:not(.x):is(.a, .b)::text", "div.a:not(.x)::text, div.b:not(.x)::text"),
    ]
    for form, expansion in pairs:
        got = frostwork.extract(html, [form])[0]
        assert got == sel.css(expansion).getall(), form
        # and it genuinely diverges from cssselect's buggy direct evaluation (sanity-check the premise)
        assert got != sel.css(form).getall(), f"{form} unexpectedly agrees with the cssselect bug"


# --------------------------------------------------------------------------- audit CLI


def _write(tmp_path, body):
    p = tmp_path / "pages.py"
    p.write_text(body)
    return str(p)


def test_audit_cli_flags_problems_and_exits_nonzero(tmp_path, capsys):
    from frostwork.audit import main

    target = _write(
        tmp_path,
        "from frostwork import Page\n"
        "good = Page().field('t', 'h1::text')\n"
        "bad = Page().field('t', 'h1::text').field('x', 'li:has(.a .b)::text')\n",
    )
    code = main([target])
    out = capsys.readouterr().out
    assert code == 1
    assert "PROBLEMS  bad" in out
    assert "OK        good" in out
    assert ":has" in out
    assert "1/2 schema(s) OK" in out


def test_audit_cli_explicit_registry(tmp_path, capsys):
    from frostwork.audit import main

    target = _write(
        tmp_path,
        "from frostwork import Page\n"
        "good = Page().field('t', 'h1::text')\n"
        "ignored = Page().field('x', 'div:contains(\"x\")::text')\n"
        "SCHEMAS = {'chosen': good}\n",
    )
    code = main([target + ":SCHEMAS"])
    out = capsys.readouterr().out
    assert code == 0
    assert "chosen" in out
    assert "ignored" not in out
    assert "1/1 schema(s) OK" in out


def test_audit_cli_all_ok_exits_zero(tmp_path, capsys):
    from frostwork.audit import main

    target = _write(
        tmp_path,
        "from frostwork import Page\n"
        "p = Page().field('t', 'h1::text').many('o', '.card', {'h': './/h3/text()'})\n",
    )
    assert main([target]) == 0
    assert "2/2" not in capsys.readouterr().out  # one schema, all OK


def test_audit_cli_json_is_machine_readable(tmp_path, capsys):
    import json as _json

    from frostwork.audit import main

    target = _write(
        tmp_path,
        "from frostwork import Page\n"
        "good = Page().field('t', 'h1::text')\n"
        "bad = Page().field('t', 'h1::text').field('x', 'li:has(.a .b)::text')\n",
    )
    code = main([target, "--json"])
    payload = _json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["summary"] == {"total": 2, "ok": 1, "problems": 1}
    by_name = {s["name"]: s for s in payload["schemas"]}
    assert by_name["good"]["ok"] is True
    assert by_name["bad"]["ok"] is False
    bad_x = [f for f in by_name["bad"]["fields"] if f["name"] == "x"][0]
    assert bad_x["supported"] is False and ":has" in bad_x["reason"]


def test_audit_cli_bad_target_exits_2(capsys):
    from frostwork.audit import main

    assert main(["/no/such/file.py"]) == 2
    assert "could not import" in capsys.readouterr().err


def test_audit_cli_no_schemas_exits_2(tmp_path, capsys):
    from frostwork.audit import main

    target = _write(tmp_path, "x = 1\n")
    assert main([target]) == 2
    assert "no frostwork" in capsys.readouterr().err


def test_audit_cli_missing_registry_attr_blames_the_attribute(tmp_path, capsys):
    # the module imports fine; only the :REGISTRY attribute is missing — say so, don't blame import.
    from frostwork.audit import main

    target = _write(tmp_path, "from frostwork import Page\ngood = Page().field('t', 'h1::text')\n")
    assert main([target + ":NOPE"]) == 2
    err = capsys.readouterr().err
    assert "imported OK but has no attribute 'NOPE'" in err


def test_audit_cli_version(capsys):
    from frostwork.audit import main

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "frostwork-audit" in capsys.readouterr().out


# --------------------------------------------------------------------------- source scan (--scan)

SPIDER_SOURCE = """
import scrapy
from scrapy.linkextractors import LinkExtractor
from scrapy.loader import ItemLoader

FIELD = "price"


class Shop(scrapy.Spider):
    rules = (LinkExtractor(restrict_css=[".pagination a", "li:contains('next') a"]),)

    def parse(self, response):
        for row in response.css(".product"):
            yield {
                "title": row.css("h3::text").get(),
                "price": row.xpath("td/text()").get(),
                "dyn": row.css(f"{FIELD}::text").get(),
            }
        loader = ItemLoader(response=response)
        loader.add_css("name", "h1.title::text")
        loader.add_xpath("desc", "//div[@class='desc']//text()")
"""


def _write_spider(tmp_path, name="spider.py", body=SPIDER_SOURCE):
    p = tmp_path / name
    p.write_text(body)
    return str(p)


def test_scan_finds_inline_selectors_without_importing(tmp_path, capsys):
    # The gap Jan reported: a spider with inline .css()/.xpath(), an ItemLoader and a LinkExtractor has
    # no schema object to audit, yet these are exactly the selectors a migration must classify. --scan
    # parses the source (never imports it, so import-time setup can't fire) and reports file:line.
    from frostwork.audit import main

    target = _write_spider(tmp_path)
    code = main(["--scan", target, "-v"])
    out = capsys.readouterr().out
    assert code == 1  # unsupported selectors present
    assert "spider.py:10" in out and ":contains()" in out  # LinkExtractor restrict_css
    assert "'.product'" in out and "'h3::text'" in out  # inline .css()
    assert "'h1.title::text'" in out  # add_css takes (field_name, css) — the SECOND argument
    assert "Many/One" in out  # relative `td/text()` names the rewrite
    assert "not a literal" in out  # the f-string is skipped, not silently dropped
    assert "5/7 literal selector(s) supported" in out


def test_scan_json_and_directory_walk(tmp_path, capsys):
    import json as _json

    from frostwork.audit import main

    _write_spider(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "more.py").write_text("x = sel.xpath('//h1/text()')\n")
    code = main(["--scan", str(tmp_path), "--json"])
    payload = _json.loads(capsys.readouterr().out)
    assert code == 1  # the spider's unsupported selectors are still there
    assert payload["mode"] == "scan"
    assert payload["summary"]["sites"] == 9  # 8 in spider.py (7 literal + 1 f-string) + 1 in pkg/
    assert any(s["file"].endswith("more.py") and s["supported"] for s in payload["sites"])
    dynamic = [s for s in payload["sites"] if s["supported"] is None]
    assert len(dynamic) == 1 and dynamic[0]["selector"] is None


def test_scan_all_supported_exits_zero(tmp_path, capsys):
    from frostwork.audit import main

    target = _write_spider(tmp_path, "clean.py", "v = response.css('h1::text')\nw = sel.xpath('//a/@href')\n")
    assert main(["--scan", target]) == 0
    assert "2/2 literal selector(s) supported (100%)" in capsys.readouterr().out


def test_scan_reads_both_page_object_spellings(tmp_path, capsys):
    # web-poet `field(selector)` / `Many(container, ...)` take the selector FIRST; the `Page` builders
    # take a name first (`field(name, selector)`, `many(name, container, {...})`). Both are scanned.
    from frostwork.audit import main

    target = _write_spider(
        tmp_path,
        "objects.py",
        "class P(FrostPage):\n"
        "    title = field('h1::text')\n"
        "    cards = Many('.card', name=field('.//h3/text()'))\n"
        "    broken = field('li:contains(\"x\")::text')\n"
        "p = Page().field('t', 'h2::text').many('rows', '.row', {'c': './/td/text()'})\n",
    )
    assert main(["--scan", target, "-v"]) == 1
    out = capsys.readouterr().out
    for selector in ("'h1::text'", "'.card'", "'.//h3/text()'", "'h2::text'", "'.row'", "'.//td/text()'"):
        assert selector in out, selector
    assert ":contains()" in out
    assert "6/7 literal selector(s) supported" in out


def test_scan_missing_path_and_unparseable_file(tmp_path, capsys):
    from frostwork.audit import main

    assert main(["--scan", str(tmp_path / "nope.py")]) == 2
    assert "no such file or directory" in capsys.readouterr().err
    # a file this Python can't parse is reported, not fatal
    bad = _write_spider(tmp_path, "bad.py", "def broken(:\n")
    assert main(["--scan", bad]) == 1
    out = capsys.readouterr().out
    assert "could not be parsed" in out and "1 file(s) UNPARSEABLE" in out


def test_schema_audit_rejects_multiple_targets(tmp_path, capsys):
    from frostwork.audit import main

    a = _write(tmp_path, "from frostwork import Page\np = Page().field('t', 'h1::text')\n")
    assert main([a, a]) == 2
    assert "ONE module target" in capsys.readouterr().err


# ---------------------------------------------------------------- support-boundary parity vs the oracle
# The no-fallback contract has TWO halves and only one was tested. "An unsupported selector returns an
# empty column" was covered; "a selector the ORACLE rejects is REPORTED unsupported" was not — so the
# engine could accept `.1::text`, answer empty, and still call itself supported. That is a broken promise
# the scraper layer cannot distinguish from a legitimately empty field.
# NB `#1id` is NOT here: cssselect accepts it (a hash token's payload is a NAME, not an identifier), so
# rejecting it would make us narrower than the oracle. The precondition assert below catches exactly this
# kind of wrong assumption — it caught this one.
ORACLE_REJECTS_CSS = [".1::text", ".-2::text", "[1]::text", "div::attr(1)", ".2col::text", ".--x::text"]
ORACLE_REJECTS_XPATH = ["//div/@1", "//svg:rect/text()", "//div/@x:y", "//1div/text()"]


def test_selectors_the_oracle_rejects_are_reported_unsupported():
    parsel = _oracle()
    for sel in ORACLE_REJECTS_CSS + ORACLE_REJECTS_XPATH:
        s = parsel.Selector(text="<div class='1' data-1='v'><p>T</p></div>")
        try:
            (s.xpath(sel) if sel.startswith("/") else s.css(sel)).getall()
        except Exception:
            pass  # the oracle refuses it, which is the precondition for this assertion
        else:
            raise AssertionError(f"{sel!r} was expected to be oracle-invalid; fixture needs updating")
        assert not frostwork.check([sel]).fields[0].supported, (
            f"{sel!r} is rejected by the oracle, so check() must report it unsupported"
        )


def test_css_escapes_in_quoted_values_match_parsel():
    """`[data-x="\\61"]` selects `data-x="a"` in cssselect. Copying the raw bytes matched a DIFFERENT
    element — a wrong value, the one thing no-fallback rules out."""
    parsel = _oracle()
    html = b'<html><body><i data-x="a">ESCAPED</i><i data-x="\\61">RAW</i></body></html>'
    for sel in ['[data-x="\\61"]::text', '[data-x="a"]::text', '[data-x="\\0041"]::text']:
        want = parsel.Selector(body=html, encoding="utf-8").css(sel).getall()
        assert frostwork.check([sel]).fields[0].supported, sel  # not a conditional: it MUST stay supported
        assert frostwork.extract(html, [sel])[0] == want, sel


def test_the_whole_css_escape_surface_obeys_the_contract():
    """Escapes appear in five places, and the contract is the same in all of them: **supported means
    parity, unsupported means empty**. Never supported-and-empty, never non-empty-and-wrong.

    Worth stating as one sweep rather than per-form vectors, because the bug that motivated it was
    invisible per-form: `::attr(data-\\6b)` was reported SUPPORTED (the identifier validator only read the
    first character) and then matched the literal name, so it returned an empty column for a selector
    parsel answers. A per-form test that asserted "empty" would have passed. Only checking the pair
    (support verdict, values) against parsel catches it.

    Escapes in a class / id / attribute name / type name are an honest UNSUPPORTED gap today — this test
    pins the gap as empty-not-wrong, and will start demanding parity the moment one is claimed supported.
    """
    parsel = _oracle()
    html = (b'<html><body><p class="shared">A</p><p class="shared1">B</p>'
            b'<p id="i1">D</p><p data-k="v1">C</p></body></html>')
    surface = [
        r"[data-k]::attr(data-\6b)",     # ::attr() argument      -> data-k
        r'[data-k="\76 1"]::text',       # quoted value           -> v1
        r'[data-k^="\76"]::text',        # quoted value, prefix op -> v
        r".shared\31::text",             # class name             -> shared1
        r"#i\31::text",                  # id                     -> i1
        r".\73 hared::text",             # class, leading escape  -> shared
        r"\70::text",                    # type name              -> p
        r"[data-\6b]::text",             # attribute name         -> data-k
        r".shared\ ::text",              # escaped space in a class name
    ]
    for sel in surface:
        try:
            want = parsel.Selector(body=html, encoding="utf-8").css(sel).getall()
        except Exception:
            want = None  # the oracle rejects it: we may not answer at all
        got = frostwork.extract(html, [sel], strict=False)[0]
        supported = frostwork.check([sel]).fields[0].supported
        if want is None:
            assert not got, f"{sel!r}: parsel rejects this, so answering it is a no-fallback violation"
        elif supported:
            assert got == want, f"{sel!r}: claimed supported, so it must equal parsel ({want!r})"
        else:
            assert not got, f"{sel!r}: unsupported must be EMPTY, got {got!r}"


def test_quoted_delimiters_in_functional_pseudos_are_supported():
    """A `)` or `,` inside a QUOTED attribute value is data, not the end of `:is()`/`:not()`/`:where()`.

    This is the half of the contract that goes red: the no-fallback rule *permits* an unsupported
    selector to return empty, so a parser that ends the pseudo at the quoted `)` fails no value gate —
    `tools/sel_fuzz.py`'s quoted family simply moves ~250 pairs per seed into the UNSUPPORTED bucket and
    passes. What must be asserted is the SUPPORT verdict: these are valid CSS that parsel answers, so
    claiming them unsupported is the regression. (Same lesson as docs/TESTING.md's `if supported: assert
    parity` note — assert the verdict too, or the test passes the moment support disappears.)
    """
    parsel = _oracle()
    html = (b'<html><body><div id="outer" data-x=")" class="a,b"><span id="in">S</span></div>'
            b'<div id="plain" data-x="q"><span id="in2">T</span></div>'
            b'<p id="p1" title="a(b">P</p></body></html>')
    supported = [
        'div:is(#outer, [data-x=")"])::attr(id)',
        'div:is([data-x=")"], #other)::attr(id)',      # the quoted `)` BEFORE the comma
        'div:where([data-x=")"])::attr(id)',
        'div:not([data-x=")"])::attr(id)',
        'div:not([data-x="("])::attr(id)',             # an unbalanced `(` in a value
        "p:is([title='a(b'])::attr(id)",               # single-quoted, unbalanced
        r'div:is([data-x="\)"])::attr(id)',            # the same paren as a CSS escape
        'div:is([class="a,b"])::attr(id)',
        'div:is(#outer, [data-x=")"]) span::text',     # ...and the tail still splits correctly
        'div:is(#outer, [data-x=")"]) > span::text',
    ]
    for sel in supported:
        want = parsel.Selector(body=html, encoding="utf-8").css(sel).getall()
        assert frostwork.check([sel]).fields[0].supported, f"{sel!r} is valid CSS parsel answers"
        assert frostwork.extract(html, [sel])[0] == want, sel
    # FAIL CLOSED on syntax that is genuinely broken. parsel raises on all of these, so any non-empty
    # column would be the OVERMATCH the selector fuzzer gates.
    for sel in [
        'div:is(#outer, [data-x=")"]::attr(id)',
        'div:not([data-x=")"]::attr(id)',
        'div:is([data-x=")::attr(id)',
        'div:is()::attr(id)',
    ]:
        with pytest.raises(Exception):
            parsel.Selector(body=html, encoding="utf-8").css(sel).getall()
        assert not frostwork.extract(html, [sel], strict=False)[0], sel
        assert not frostwork.check([sel]).fields[0].supported, sel


def test_selector_grammar_surface_obeys_the_contract():
    """The same sweep over the REST of the grammar the fuzzer does not write.

    The escape hole was found by asking "what syntax does no generator emit?", so the rest of that list is
    worth pinning rather than re-deriving: single quotes (the fuzzer only writes double), a quote of the
    other kind inside a value, namespace prefixes, tabs/newlines between combinators, CSS comments, and
    tight combinators. All of these are correct today — two as parity, two as an honest unsupported gap —
    and the test states which, so a change that turns a gap into a WRONG answer fails here.
    """
    parsel = _oracle()
    html = (b'<html><body><p class="shared" data-k="v1" title="it\'s">A</p>'
            b'<p class="shared1">B</p><div><p id="i1">D</p></div></body></html>')
    for sel in [
        "[data-k='v1']::text", "[title='it\\'s']::text", '[title="it\'s"]::text',  # single quotes
        "*|p::text", "|p::text", "html|p::text",                                   # namespace prefixes
        "div  >  p::text", "div\t>\tp::text", "div\n p::text", "  p::text  ",      # internal whitespace
        "p/*c*/::text", "div/*x*/ p::text",                                        # CSS comments
        "div>p::text", "div+p::text", "div~p::text",                               # no space around combs
        '[data-k="V1" i]::text',                                                   # case-insensitive flag
    ]:
        try:
            want = parsel.Selector(body=html, encoding="utf-8").css(sel).getall()
        except Exception:
            want = None
        got = frostwork.extract(html, [sel], strict=False)[0]
        if want is None:
            assert not got, f"{sel!r}: parsel rejects this, so answering it is a no-fallback violation"
        elif frostwork.check([sel]).fields[0].supported:
            assert got == want, f"{sel!r}: claimed supported, so it must equal parsel ({want!r})"
        else:
            assert not got, f"{sel!r}: unsupported must be EMPTY, got {got!r}"


def test_xpath_grammar_surface_obeys_the_contract():
    """The XPath analogue of the CSS grammar sweep: 50 shapes, one contract.

    Supported means parity with lxml; unsupported means EMPTY. The interesting half is the shapes no
    generator writes — `position()`, `last()`, `not()`, `!=`, unions of terminals, axes, `string()`,
    `count()`, bare node tests, `//@attr`, `@*`, chained predicates, non-literal operands. That last
    family is what this branch set out to reject, so it is worth a standing check that rejecting them
    stayed *empty* rather than drifting into a wrong answer.
    """
    parsel = _oracle()
    html = (b'<html><body><div id="d1" class="a b"><p class="x" data-k="v1">one</p>'
            b'<p class="y">two</p><a href="/p1" title="t">link</a><span>s1</span>'
            b'<ul><li>l1</li><li>l2</li><li>l3</li></ul>'
            b'<table><tr><td>c1</td><td>c2</td></tr></table>'
            b'<em>e</em><b>bb</b></div><div id="d2"><p>three</p></div></body></html>')
    for q in [
        "//p/text()", "//p[@class]/text()", '//p[@class="x"]/text()', "//div/p/text()", "//a/@href",
        "//li[2]/text()", "//div[@id='d1']//text()", "//p[1]/text()", "//li[last()]/text()",
        "//li[position()=2]/text()", "//li[position()>1]/text()", "//p[contains(@class,'x')]/text()",
        "//p[contains(text(),'one')]/text()", "//p[starts-with(@class,'x')]/text()",
        "//p[not(@data-k)]/text()", "//p[@class and @data-k]/text()", "//p[@class or @data-k]/text()",
        "//p[@class!='x']/text()", "//*[@id]/@id", "//div/*/text()", "//p | //span",
        "//p/text() | //span/text()", "normalize-space(//p)", "string(//p)", "count(//p)",
        "//p/following-sibling::p/text()", "//p/parent::div/@id", "//p/ancestor::div/@id",
        "//div//p[@class='y']/text()", "//td/text()", "//p[@data-k=@class]/text()",
        "//p[text()=@class]/text()", "//p[contains(@class,@data-k)]/text()",
        "//p[@class=concat('x','')]/text()", "//p[.='one']/text()", "//p[./text()='one']/text()",
        "//p[@class='x'][@data-k='v1']/text()", "(//p)[1]/text()", "//node()", "//text()",
        "//comment()", "//@href", "//p/@*", "//P/text()", "//p[@CLASS='x']/text()",
        "/html/body/div/p/text()", "./div/p/text()", ".//p/text()",
        "//p[string-length(@class)>0]/text()", "//p[@class='x']/../@id",
    ]:
        try:
            want = parsel.Selector(body=html, encoding="utf-8").xpath(q).getall()
        except Exception:
            want = None
        got = frostwork.extract(html, [q], strict=False)[0]
        if want is None:
            assert not got, f"{q!r}: lxml rejects this, so answering it is a no-fallback violation"
        elif frostwork.check([q]).fields[0].supported:
            assert got == want, f"{q!r}: claimed supported, so it must equal lxml ({want!r})"
        else:
            assert not got, f"{q!r}: unsupported must be EMPTY, got {got!r}"


def test_scanner_finds_selector_literals_in_every_source_shape():
    """The scanner's failure mode is a SILENT MISS: it exists so a migration can see un-ported selectors,
    and a selector it does not report is one nobody knows to port.

    So this sweeps the source shapes a hand-written test set does not think of — chained calls, subscripts,
    multiline and triple-quoted arguments, comprehensions, `try`/`with`/`async def`/lambda/decorator
    contexts, processors after the selector, tuple and list kwargs. Dynamic selectors must be REPORTED as
    skipped rather than dropped, which is a different thing from being found.
    """
    from frostwork.scan import scan_source

    TRIPLE = "r.css(" + '"""' + ".a::text" + '"""' + ")\n"

    def found(src):
        return {s.selector for s in scan_source(src, "t.py") if s.selector}

    cases = [
        ("def p(r):\n    return r.css('.a::text').get() or r.css('.b::text').get()\n",
         {".a::text", ".b::text"}),
        ("r.css('.a').css('.b::text')\n", {".a", ".b::text"}),
        ("r.css('.a::text')[0]\n", {".a::text"}),
        ("r.css(\n    '.a::text'\n)\n", {".a::text"}),
        (TRIPLE, {".a::text"}),
        ("async def p(r):\n    return r.css('.a::text').get()\n", {".a::text"}),
        ("class S:\n    def parse(self, r):\n        return r.css('.a::text')\n", {".a::text"}),
        ("def p(r):\n    try:\n        return r.css('.a::text')\n    except E:\n        return r.css('.b')\n",
         {".a::text", ".b"}),
        ("def p(r):\n    for x in r.css('.row'):\n        yield x.css('.c::text').get()\n",
         {".row", ".c::text"}),
        ("l.add_xpath('n', '//p/text()', MapCompose(str.strip))\n", {"//p/text()"}),
        ("LinkExtractor(restrict_xpaths=['//a/@href', '//b/@href'])\n", {"//a/@href", "//b/@href"}),
        ("LinkExtractor(allow=(), restrict_css=('.a', '.b'))\n", {".a", ".b"}),
        ("r.css('.a::text').re(r'\\d+')\n", {".a::text"}),
        ("lambda r: r.css('.a::text')\n", {".a::text"}),
        ("@decorator(r.css('.a'))\ndef f():\n    pass\n", {".a"}),
        ("r.css('.a, .b::text')\n", {".a, .b::text"}),
        ("def p(r):\n    with open('f') as fh:\n        return r.css('.a::text')\n", {".a::text"}),
        ("d = {'f': r.css('.a::text')}\n", {".a::text"}),
        ("sel.get_css('.a::text')\n", {".a::text"}),
    ]
    for src, want in cases:
        assert found(src) == want, f"scanner missed/invented a selector in {src!r}"

    # dynamic selectors: a SITE must still be reported, with no selector — silently dropping them would
    # under-report a migration and read as "nothing left to port"
    for src in ["x = [r.css(s) for s in ['.a', '.b']]\n", "r.css('.a::text' if x else '.b')\n",
                "q = '.a' + '::text'\nr.css(q)\n", "r.css(f'.a{n}::text')\n"]:
        sites = scan_source(src, "t.py")
        assert sites and not any(s.selector for s in sites), \
            f"a dynamic selector must be reported as skipped, not dropped: {src!r}"


def test_deferred_predicates_are_unsupported_in_grouped_extraction():
    """Deferred-close matching is unsupported both AS a grouped container and INSIDE a grouped sub-field.

    That is a real limit (docs/PYTHON.md), and the thing worth pinning is that it is REPORTED rather than
    silently empty: a probe that only compared values against Parsel here would report hundreds of
    "divergences" that are really the no-fallback contract working. Each verdict must name its reason.
    """
    groups = [
        ("div:has(a)", [("h", "a::attr(href)")]),
        ("li:last-child", [("t", "::text")]),
        ("div.c1", [("h", "a::attr(href)")]),
        ("div", [("last", "li:last-child::text")]),
        ("div", [("hasa", "p:has(a) a::attr(href)")]),
    ]
    rep = frostwork.check([], groups)
    verdicts = [(g.container.supported, [f.supported for f in g.subfields]) for g in rep.groups]
    assert verdicts == [(False, [True]), (False, [True]), (True, [True]),
                        (True, [False]), (True, [False])], verdicts
    for g in rep.groups:
        if not g.container.supported:
            assert "grouped container" in (g.container.reason or ""), g.container.reason
        for f in g.subfields:
            if not f.supported:
                assert "grouped sub-field" in (f.reason or ""), f.reason


def test_deferred_predicate_combinations_match_parsel():
    """`:has()`, reverse positionals and XPath text-predicates all resolve at a local close, and the
    interesting cases are the ones where candidate spans NEST and OVERLAP — what the maximal-span de-dup
    and the tail re-scan exist for, which a single-predicate vector never exercises.

    The generator is CONTENT-MODEL CONFORMANT on purpose (`<p>` holds inline only, `<li>` only inside
    `<ul>`). A first version emitted `<div>` inside `<p>` and li-in-li, and its 9 "failures" were all the
    DOCUMENTED deep-`p`/misnest divergences — the probe was measuring the wrong thing. On conformant input
    the corrected stack equals lxml's tree, so any divergence here is a real bug.
    """
    parsel = _oracle()
    import random
    rng = random.Random(11)
    INLINE = ['<a href="/h">A</a>', '<span>S</span>', '<b>B</b>', 'txt', '<i>I</i>']

    def inline_run():
        return "".join(rng.choice(INLINE) for _ in range(rng.randint(1, 3)))

    def flow(d):
        if d == 0:
            return inline_run()
        r, cls = rng.random(), rng.choice(["c1", "c2", "c1 c2", ""])
        if r < 0.30:
            return f'<p class="{cls}">{inline_run()}</p>'
        if r < 0.50:
            items = "".join(f'<li class="{rng.choice(["c1", "c2", ""])}">'
                            f'{inline_run() if rng.random() < 0.6 else flow(d - 1)}</li>'
                            for _ in range(rng.randint(1, 3)))
            return f"<ul>{items}</ul>"
        tag = "div" if r < 0.78 else "section"
        kids = "".join(flow(d - 1) for _ in range(rng.randint(1, 3)))
        return f'<{tag} class="{cls}">{kids}</{tag}>'

    basket = [
        "div:has(a)::text", "div:has(a) a::attr(href)", "div:has(> a) a::attr(href)",
        "li:has(span)::text", "li:has(.c1) a::attr(href)", "div:has(b) span::text",
        "div:has(a):has(span) a::attr(href)", "li:last-child::text", "li:only-child::text",
        "div:last-child a::attr(href)", "li:last-of-type::text", "div:nth-last-child(2)::text",
        "div:has(a) li:last-child::text", "li:last-child:has(a) a::attr(href)",
        "//div[a]/text()", "//li[span]//text()", "//div[contains(., 'A')]/@class",
        "div:has(a) b::text", "section:has(i) span::text", "p:has(a) a::attr(href)",
        "ul:has(li.c1) li::text",
    ]
    sup = [f.supported for f in frostwork.check(basket).fields]
    for _ in range(60):
        body = "".join(flow(rng.randint(2, 4)) for _ in range(rng.randint(2, 4)))
        html = ("<html><body>" + body + "</body></html>").encode()
        mine = frostwork.extract(html, basket, strict=False)
        sel = parsel.Selector(body=html, encoding="utf-8")
        for i, q in enumerate(basket):
            want = (sel.xpath if q.startswith("/") else sel.css)(q).getall()
            if sup[i]:
                assert [v for v in mine[i] if v.strip()] == [v for v in want if v.strip()], \
                    f"{q!r} diverges on {html!r}"
            else:
                assert not mine[i], f"{q!r} is unsupported but returned {mine[i]!r}"


def test_scanner_handles_keyword_builder_forms():
    """Migration reports are only useful if they see every selector literal. The arity checks lost the
    all-keyword form entirely (silently 'clean') and audited a FIELD NAME as a selector in the
    keyword-selector form (noise that fails the audit)."""
    from frostwork.scan import scan_source

    def sites(expr):
        return sorted(s.selector for s in scan_source(
            f"from frostwork import Page\nclass P(Page):\n    x = {expr}\n", "x.py"))

    assert sites('Page.many("cards", ".card", {"t": ".t::text"})') == [".card", ".t::text"]
    assert sites('Page.many(name="cards", container=".card", subfields={"t": ".t::text"})') == \
        [".card", ".t::text"]
    assert sites('Page.many("cards", container=".card", subfields={"t": ".t::text"})') == \
        [".card", ".t::text"]
    assert sites('Page.field("title", "h1::text")') == ["h1::text"]
    assert sites('Page.field("title", selector="h1::text")') == ["h1::text"]
    assert sites('Page.field(name="title", selector="h1::text")') == ["h1::text"]
