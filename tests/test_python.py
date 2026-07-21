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
    # an unrecognized label must fail loudly, not silently fall through to sniffing (a wrong decode).
    with pytest.raises(ValueError, match="unknown encoding label"):
        frostwork.extract(b"<p>x</p>", ["p::text"], "not-a-real-charset")


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
    import threading
    import time

    html = b"<html><body>" + b"<div class=x><span class=p>v</span></div>" * 20000 + b"</body></html>"
    plan = frostwork.Page().field_all("p", ".p::text")
    plan.extract(html)  # compile once

    def work():
        for _ in range(4):
            plan.extract(html)

    t0 = time.perf_counter()
    work()
    work()
    serial = time.perf_counter() - t0

    threads = [threading.Thread(target=work) for _ in range(2)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    parallel = time.perf_counter() - t0

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
    parsel = pytest.importorskip("parsel")
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
    parsel = pytest.importorskip("parsel")
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
    parsel = pytest.importorskip("parsel")
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
    parsel = pytest.importorskip("parsel")
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
    # Permissive mode exposes the engine's safe empty-column contract for unsupported forms.
    assert frostwork.extract(body, ["li:last-child ::text"], strict=False)[0] == []
    for q in xp:
        # last() is unsupported -> frostwork empty (allowed gap); the rest match exactly
        mine = frostwork.extract(body, [q])[0]
        theirs = sel.xpath(q).getall()
        assert mine == theirs or (mine == [] and "last()" in q), f"{q}: {mine} vs {theirs}"


def test_normalize_space_matches_parsel():
    parsel = pytest.importorskip("parsel")
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
    parsel = pytest.importorskip("parsel")
    html = b"<ul><li class=x>a</li><li>b</li></ul>"
    # a reverse position IS now supported in the attached ::text form; the detached subtree form isn't
    supported = "li:last-child::text"
    unsupported = "li:last-child ::text"
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
    parsel = pytest.importorskip("parsel")
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
    parsel = pytest.importorskip("parsel")
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
    parsel = pytest.importorskip("parsel")
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
    parsel = pytest.importorskip("parsel")
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
    parsel = pytest.importorskip("parsel")
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
    parsel = pytest.importorskip("parsel")
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
