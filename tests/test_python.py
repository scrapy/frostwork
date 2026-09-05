"""Tests for the Frostwork Python bindings: the `extract` primitive, the declarative `Page`/`Item`
layer, and the web-poet integration. Run with `.venv/bin/python -m pytest tests/test_python.py`.

Correctness of the *engine* is proven by the Rust differential (`tools/diff_lxml.py`); these tests
cover the Python surface (marshalling, cardinality, item assembly, web-poet wiring) plus a small
parsel cross-check that values survive the FFI boundary unchanged.
"""

import asyncio
import contextlib
import re
import sys

import pytest

import frostwork
from frostwork import Page
from frostwork._frostwork import Plan as _Plan

# ONE specimen of an unsupported selector, and one fragment of its advisory reason. Tests that need "a
# selector the engine declines" reach for these rather than inlining an example: `:contains('x')` was
# inlined a dozen times as that example, and supporting it broke six tests whose subject was strict mode
# and `--scan` reporting, not `:contains()` at all.
#
# A reverse position with a CHILD step into the value tail needs "depth exactly 1 within the span", which
# the depth-agnostic matcher cannot express (docs/COMPATIBILITY.md) — a structural limit, not a feature
# waiting to land, so it is a stable specimen.
UNSUPPORTED_SEL = "li:last-child > b::text"
UNSUPPORTED_REASON = "reverse position"


def test_the_unsupported_specimen_is_still_unsupported():
    """If a later release supports it, every test using it becomes vacuous — so say so here, once."""
    bad = frostwork.check([UNSUPPORTED_SEL]).unsupported
    assert [f.selector for f in bad] == [UNSUPPORTED_SEL]
    assert UNSUPPORTED_REASON in bad[0].reason


@contextlib.contextmanager
def raises_at_class_definition(match):
    """Assert a `field()`/`Many()` misdeclaration is refused when the class is created, and that the
    diagnosis reaches the user — on every supported interpreter.

    The library raises `TypeError` from `_FrostField.__set_name__`, but CPython **wraps whatever
    `__set_name__` raises in `RuntimeError` before 3.12** (gh-77757 stopped doing so in 3.12). So on the
    abi3 floor (3.10) and on 3.11 a user catches `RuntimeError` whose `__cause__` carries Frostwork's
    message, and from 3.12 they catch the `TypeError` directly. Asserting only `TypeError` made this
    suite pass on the dev interpreter and fail on the floor — which is exactly what nobody saw while the
    floor job could not run pytest at all."""
    with pytest.raises((TypeError, RuntimeError)) as excinfo:
        yield
    err = excinfo.value
    chain = [err]
    while chain[-1].__cause__ is not None:
        chain.append(chain[-1].__cause__)
    assert any(re.search(match, str(e)) for e in chain), (
        f"none of {[type(e).__name__ for e in chain]} carried {match!r}: "
        f"{[str(e)[:80] for e in chain]}"
    )
    if sys.version_info >= (3, 12):
        assert isinstance(err, TypeError), f"3.12+ should surface TypeError unwrapped, got {err!r}"

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


@pytest.mark.parametrize(
    "label,name,doc_name",
    [
        ("windows-1252", "data-año", "data-a\xf1o"),
        ("shift_jis", "属性", "属性"),
        ("gb18030", "属性", "属性"),
        ("euc-jp", "属性", "属性"),
        ("big5", "屬性", "屬性"),
        ("euc-kr", "속성", "속성"),
    ],
)
def test_a_non_ascii_attribute_name_matches_under_a_legacy_encoding(label, name, doc_name):
    """Attribute VALUES are decoded before any comparison, which is what makes `.café` and
    `:contains("社")` encoding-agnostic. Attribute NAMES were the one place a selector's UTF-8 still met
    the page's raw bytes: `[data-año]` matched a UTF-8 page and silently returned nothing for the same
    document in windows-1252. lxml decodes the whole document before tokenizing, so it matches either
    way — it is the oracle here, over every predicate and the `::attr()` terminal.

    Both front-ends, because they answer the same question and must not diverge: the XPath spelling is
    refused in EVERY encoding while the CSS one worked."""
    parsel = _oracle()
    text = f'<p {doc_name}="v">t</p>'
    body = text.encode(label)
    css = [
        f"[{name}]::text",
        f'[{name}="v"]::text',
        f"[{name}]::attr({name})",
        f'[{name}^="v"]::text',
    ]
    xpath = [
        f"//p[@{name}]/text()",
        f'//p[@{name}="v"]/text()',
        f"//p/@{name}",
        f"//p//@{name}",
        f'//p[contains(@{name},"v")]/text()',
        f'//p[starts-with(@{name},"v")]/text()',
    ]
    theirs = parsel.Selector(text=text)
    for q in css:
        assert frostwork.extract(body, [q], label) == [theirs.css(q).getall()] != [[]], q
    for q in xpath:
        assert frostwork.extract(body, [q], label) == [theirs.xpath(q).getall()] != [[]], q
    # and a name the page does NOT carry stays empty in the same encoding, both spellings
    assert frostwork.extract(body, [f"[{name}x]::text"], label) == [[]]
    assert frostwork.extract(body, [f"//p[@{name}x]/text()"], label) == [[]]


def test_extract_str_with_non_utf8_label_raises():
    # already-decoded str is tokenized as UTF-8; a conflicting label would double-transcode silently.
    with pytest.raises(ValueError, match="already-decoded str"):
        frostwork.extract("<p>café</p>", ["p::text"], "windows-1252")
    # a UTF-8 label on str is consistent and allowed
    assert frostwork.extract("<p>café</p>", ["p::text"], "utf-8") == [["café"]]


@pytest.mark.parametrize("label", [None, "utf-8", "utf8", "UTF-8", "utf_8", "utf-8-sig", "U8"])
def test_a_str_accepts_every_utf8_label_spelling_through_both_entry_points(label):
    """`extract` normalizes Python codec spellings through `codecs`, and the native layer resolves
    WHATWG labels — two different labelling universes over the same argument. `utf_8`, `utf-8-sig` and
    `U8` are UTF-8 to Python and unknown to WHATWG, so a native check that demanded a WHATWG UTF-8 name
    made the same label mean different things through `extract` (accepted) and through `Plan` (refused),
    which is the path `frostwork.webpoet` uses. Both must answer the same way."""
    html = "<p>café</p>"
    assert frostwork.extract(html, ["p::text"], label) == [["café"]]
    assert _Plan(["p::text"], []).extract(html, label) == [["café"]]


@pytest.mark.parametrize("label", ["windows-1252", "latin-1", "iso-8859-1", "shift_jis", "gb18030"])
def test_a_str_refuses_a_label_that_resolves_to_another_encoding(label):
    """The mojibake this exists to prevent: UTF-8 bytes decoded as something else. Refused on BOTH
    entry points, since a direct `Plan` call bypasses the pure-Python check entirely."""
    html = "<p>café</p>"
    with pytest.raises(ValueError):
        frostwork.extract(html, ["p::text"], label)
    with pytest.raises(ValueError):
        _Plan(["p::text"], []).extract(html, label)


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


@pytest.mark.parametrize("body, selector, raw", [
    (b"<p>a</p><p>b</p>", "p::text", ["a", "b"]),
    (b'<a href=""></a><a href="/product"></a>', "a::attr(href)", ["", "/product"]),
    (b"<p> </p><p>b</p>", "p::text", [" ", "b"]),
    (b"<div><div>inner</div>outer</div>", "div",
     ["<div><div>inner</div>outer</div>", "<div>inner</div>"]),
    (b"<div><p>a</p></div><div><p>b</p></div>", "p:last-child::text", ["a", "b"]),
])
@pytest.mark.parametrize("card", ["first", "all", "join"])
@pytest.mark.parametrize("companion", ["none", "late-first", "missing-first", "all", "join", "group"])
def test_item_raw_values_follow_cardinality_across_schemas(body, selector, raw, card, companion):
    """Unrelated fields must not change get_all(), including when they prevent early exit.

    Empty values are real matches; nested captures are ordered by element start, not close. Raw
    access stays untransformed, and all/join declarations retain every match.
    """
    body += b"<aside>later</aside><aside>last</aside>"
    page = Page()
    shaped = raw[0] if card == "first" else raw if card == "all" else "|".join(raw)
    if card == "first":
        page.field("value", selector, map=lambda value: ("mapped", value))
    elif card == "all":
        page.field_all("value", selector, map=lambda value: ("mapped", value))
    else:
        page.field_join("value", selector, "|", map=lambda value: ("mapped", value))
    if companion == "late-first":
        page.field("other", "aside::text")
    elif companion == "missing-first":
        page.field("other", "nosuchtag::text")
    elif companion == "all":
        page.field_all("other", "aside::text")
    elif companion == "join":
        page.field_join("other", "aside::text", "|")
    elif companion == "group":
        page.many("other", "aside", {"text": "::text"})
    for _ in range(2):  # the cached plan must have the same contract
        item = page.extract(body)
        expected = raw[:1] if card == "first" else raw
        assert item.get_all("value") == expected
        assert item.get("value") == raw[0]
        assert item.value("value") == ("mapped", shaped)
        assert item.to_dict()["value"] == ("mapped", shaped)
        assert "value" not in item.empty_fields()
        # A caller owns the returned list; changing it must not change subsequent reads.
        item.get_all("value").clear()
        assert item.get_all("value") == expected


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
    # get()/get_all() surface group values rather than silently answering None/[].
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


def _rebuild_class(cls):
    """Rebuild a class from its own `__dict__`, the way `@attrs.define` does — but through a bare `type()`
    call, so a test probes the MECHANISM rather than one library's use of it. Anything that reconstructs a
    class (another decorator, a metaclass) has to survive the same way."""
    return type(cls)(cls.__name__, cls.__bases__, dict(cls.__dict__))


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


@pytest.mark.webpoet_contract
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


@pytest.mark.webpoet_contract
def test_processor_on_a_bare_element_field_receives_a_node_not_raw_html():
    """A field processor's input contract is an lxml/parsel NODE. Frostwork's outer-HTML column is a
    string, and every zyte processor is gated on `isinstance(value, (Selector, HtmlElement))` and
    documented to return anything else "as is" — so the string sailed through UNCHANGED and a raw-HTML
    blob landed in a field typed `List[Breadcrumb]`, with nothing raised anywhere.

    Note there is no `out=` here and none is needed: web-poet resolves processors BY FIELD NAME from a
    nested `Processors` class, which every zyte-common-items base page declares. Inheriting `ProductPage`
    is enough to arm this."""
    from frostwork.webpoet import FrostPage, field

    def fake_breadcrumbs(value, page):
        """Stands in for `breadcrumbs_processor`'s isinstance gate without needing zyte installed."""
        from parsel import Selector
        from parsel.selector import SelectorList

        if isinstance(value, SelectorList):
            value = value[0] if len(value) else None
        if not isinstance(value, Selector):
            return f"<PASSTHROUGH {type(value).__name__}>"  # the bug's signature
        return [a.attrib["href"] for a in value.css("a")]

    html = (b'<html><body><nav class="crumbs"><a href="/a">A</a><a href="/b">B</a></nav>'
            b'<h1>Title</h1><p class="p">text</p></body></html>')

    class P(FrostPage):
        class Processors:
            crumbs = [fake_breadcrumbs]

        crumbs = field(".crumbs").as_node()   # the declaration that asks for the element
        name = field("h1::text")           # scalar terminal -> untouched
        raw = field(".crumbs")             # bare element, NO processor -> still raw source

    item = asyncio.run(P(response=_resp(body=html)).to_item())
    assert item["crumbs"] == ["/a", "/b"], item["crumbs"]
    assert item["name"] == "Title"
    # the no-processor bare-element field keeps its documented raw-source string
    assert item["raw"] == '<nav class="crumbs"><a href="/a">A</a><a href="/b">B</a></nav>'


@pytest.mark.webpoet_contract
def test_a_real_zyte_product_page_composes_and_every_processor_fires():
    """The composition this integration exists for, with nothing synthesized: a real
    `zyte_common_items.pages.ProductPage`, its real nested `Processors` (nine field NAMES), and a real
    `Product` item out the other end.

    Every other processor test here stands a fake in for the isinstance gate, which is faithful to the
    gate but not to the WIRING — the by-name lookup arriving through zyte's own MRO, with `Returns[Product]`
    and the typed item construction that made the original defect invisible (zyte's items do not validate
    types, so a raw-HTML string in `List[Breadcrumb]` raised nothing).

    A Frostwork base has to be in the bases; either order works. `class MyPage(ProductPage)` alone does
    not, and says so at class definition — a `field()` marker on a class that does not inherit a Frostwork
    base converts to nothing."""
    pytest.importorskip("zyte_common_items")
    from zyte_common_items import AggregateRating, Brand, Breadcrumb, Image, Product
    from zyte_common_items.pages import ProductPage

    from frostwork.webpoet import FrostPage, field

    html = (b'<html><head><title>T</title></head><body><h1>Roomy Bag</h1>'
            b'<nav class="crumbs"><a href="/c1">Cat 1</a><a href="/c2">Cat 2</a></nav>'
            b'<div class="desc"><p>A roomy bag.</p><script>track()</script>'
            b'<p>See <a href="/more">more</a>.</p></div>'
            b'<span class="rating">3.8 out of 5 stars</span>'
            b'<span class="price">$24.50</span><span class="brand">Acme</span>'
            b'<img class="hero" src="/i/1.jpg"></body></html>')

    class MyProductPage(FrostPage, ProductPage):
        name = field("h1::text")
        breadcrumbs = field(".crumbs").as_node()          # -> breadcrumbs_processor, by name only
        descriptionHtml = field(".desc").as_node()        # -> description_html_processor (clear-html)
        aggregateRating = field(".rating").as_node()      # -> rating_processor
        price = field(".price").as_node()                 # -> price_processor
        brand = field(".brand").as_node()                 # -> brand_processor
        images = field("img.hero::attr(src)", all=True)   # scalar terminal: URL strings, not nodes

    # the declaration that does NOT work, pinned so the docs cannot drift back to it
    with raises_at_class_definition("does not inherit a Frostwork page base"):
        type("Wrong", (ProductPage,), {"name": field("h1::text")})

    item = asyncio.run(MyProductPage(response=_resp(body=html)).to_item())
    assert isinstance(item, Product)
    assert item.name == "Roomy Bag"
    assert item.breadcrumbs == [
        Breadcrumb(name="Cat 1", url="http://example.com/c1"),
        Breadcrumb(name="Cat 2", url="http://example.com/c2"),
    ]
    assert item.aggregateRating == AggregateRating(bestRating=5.0, ratingValue=3.8, reviewCount=None)
    assert item.brand == Brand(name="Acme")
    assert item.price == "24.50"
    assert item.images == [Image(url="/i/1.jpg")]
    # clear-html ran: the <script> is gone and the relative href resolved against the RESPONSE url, which
    # a (value, page) processor reads off `page` — not off the re-parsed subtree
    assert "<script>" not in item.descriptionHtml
    assert 'href="http://example.com/more"' in item.descriptionHtml


@pytest.mark.webpoet_contract
def test_all_true_node_handoff_produces_a_selectorlist_not_a_plain_list():
    """`all=True` with a node-taking processor must hand over a `SelectorList`, not a list of `Selector`.

    zyte's `_handle_selectorlist` gates on `SelectorList` exactly and returns anything else UNCHANGED, so a
    plain list is the raw-string defect in a new shape. Asserted here as well as in the differential because
    the type is the contract: `[Selector, Selector]` compares equal to a `SelectorList` in plenty of
    assertions and is still wrong."""
    from parsel.selector import SelectorList

    from frostwork.webpoet import FrostPage, field

    seen = {}

    def needs_selectorlist(value, page):
        seen["type"] = type(value).__name__
        if not isinstance(value, SelectorList):
            return "<PASSTHROUGH>"  # what every zyte node processor does with the wrong type
        return [n.attrib.get("class") for n in value]

    html = b'<html><body><nav class="a"><a href="/1">1</a></nav><nav class="b"></nav></body></html>'

    class P(FrostPage):
        class Processors:
            navs = [needs_selectorlist]

        navs = field("nav", all=True).as_node()

    assert asyncio.run(P(response=_resp(body=html)).to_item()) == {"navs": ["a", "b"]}
    assert seen["type"] == "SelectorList"


@pytest.mark.webpoet_contract
def test_a_processor_receives_the_field_value_unless_the_field_says_otherwise():
    """web-poet hands a processor whatever the field returns. Any processor at all — so a plain
    `lambda v: v.upper()` is a legal processor over a bare-element field, and it must get the HTML source.

    Inferring "a processor is attached, therefore it wants a node" broke exactly that: the string transform
    got a `Selector` and raised. The input contract is the FIELD's to declare, not something to guess from
    processor presence, so this compares against the parsel page object that does the same thing."""
    from web_poet import WebPage
    from web_poet import field as wp_field

    from frostwork.webpoet import FrostPage, field

    def shout(value):
        return value.upper()

    html = b'<html><body><div id="x">hi <b>there</b></div></body></html>'

    class Frost(FrostPage):
        x = field("div", out=[shout]).as_value()

    class Parsel(WebPage):
        @wp_field(out=[shout])
        def x(self):
            return self.css("div").get()

    mine = asyncio.run(Frost(response=_resp(body=html)).to_item())
    theirs = asyncio.run(Parsel(response=_resp(body=html)).to_item())
    assert mine == theirs == {"x": '<DIV ID="X">HI <B>THERE</B></DIV>'}


def test_a_zyte_processor_on_an_element_field_must_state_its_input():
    """The other side of that: a zyte-common-items processor on a bare-element field needs the node, and
    every one of them but `images_processor` returns a string UNCHANGED — raw HTML in a typed field, silently.

    Frostwork cannot tell the two kinds apart from the outside (only `description_html_processor` carries the
    `only_handle_nodes` wrapper), so it refuses to choose: the declaration says which."""
    pytest.importorskip("zyte_common_items")
    from zyte_common_items.processors import breadcrumbs_processor, images_processor

    from frostwork.webpoet import FrostPage, field

    html = b'<html><body><nav class="crumbs"><a href="/c1">Cat 1</a></nav></body></html>'

    with pytest.raises(TypeError, match="must say what that processor receives"):
        class Unstated(FrostPage):
            class Processors:
                breadcrumbs = [breadcrumbs_processor]

            breadcrumbs = field(".crumbs")

    # ...and the string-taking one is refused the same way, which is the conversation to have: it wants
    # `::attr(src)`, not an element
    with pytest.raises(TypeError, match="must say what that processor receives"):
        class AlsoUnstated(FrostPage):
            class Processors:
                images = [images_processor]

            images = field("img")

    class AsNode(FrostPage):
        class Processors:
            breadcrumbs = [breadcrumbs_processor]

        breadcrumbs = field(".crumbs").as_node()

    class AsText(FrostPage):
        class Processors:
            breadcrumbs = [breadcrumbs_processor]

        breadcrumbs = field(".crumbs").as_value()

    node_item = asyncio.run(AsNode(response=_resp(body=html)).to_item())
    assert [b.name for b in node_item["breadcrumbs"]] == ["Cat 1"]
    # `.as_value()` is web-poet's own behaviour: the processor declines the string and hands it back
    assert asyncio.run(AsText(response=_resp(body=html)).to_item()) == {
        "breadcrumbs": '<nav class="crumbs"><a href="/c1">Cat 1</a></nav>'
    }


@pytest.mark.webpoet_contract
def test_every_processor_bearing_element_field_must_declare_its_input():
    """The rule applies to EVERY processor, not to a recognised list of them. Provenance was the wrong test:
    it needed a hard-coded module name, it could not tell zyte's own node-takers from its string-taker, and a
    user's processor with the same contract got no help at all."""
    from frostwork.webpoet import FrostPage, field

    def whatever(value, page):
        return value

    # a processor from nowhere in particular, over a bare element: the declaration is still required
    with pytest.raises(TypeError, match="must say what that processor receives"):
        type("P", (FrostPage,), {"x": field("div", out=[whatever])})

    # ...and either declaration satisfies it
    for spelling in (lambda f: f.as_node(), lambda f: f.as_value()):
        cls = type("P", (FrostPage,), {"x": spelling(field("div", out=[whatever]))})
        assert set(cls.frost_schema()["fields"]) == {"x"}

    # a field with NO processor needs no declaration — nothing is being handed anywhere
    assert set(type("P", (FrostPage,), {"x": field("div")}).frost_schema()["fields"]) == {"x"}


@pytest.mark.parametrize(
    "build, match",
    [
        (lambda f: f("nav", join="").as_node(), "join="),
        (lambda f: f("nav", join=" ").as_node(), "join="),
        (lambda f: f("nav").as_node().map(str.upper), r"\.map\(\)"),
        (lambda f: f("nav").as_node().re_first(r"\w+"), r"\.map\(\)"),
    ],
)
@pytest.mark.webpoet_contract
def test_as_node_refuses_what_it_cannot_promise(build, match):
    """`.as_node()` promises ONE parsed element, so the combinations that cannot deliver one are refused where
    they are written.

    `join=` was the sharpest: it returned the joined string unchanged, so a page object could pass every check
    and still hand `breadcrumbs_processor` raw HTML — the exact defect the declaration exists to prevent. A
    `.map()` is refused because the handoff would have to re-parse whatever the transform returned, and no
    check on the parsed tree can prove nothing was lost: lxml silently drops content appended to an `<html>`
    and silently wraps two siblings in a synthetic element with the same tag the lookup is searching for."""
    from frostwork.webpoet import FrostPage, field

    with pytest.raises(TypeError, match=match):
        type("P", (FrostPage,), {"x": build(field)})


@pytest.mark.webpoet_contract
def test_as_node_on_a_group_subfield_is_refused():
    """A subfield is one column of a row; the group is the single `web_poet.field`, so there is no processor
    for a subfield to hand anything to. `.as_node()` there was accepted and silently ignored."""
    from frostwork.webpoet import Many, One, field

    for maker in (Many, One):
        with pytest.raises(TypeError, match="cannot apply to a subfield"):
            maker(".card", title=field("h3").as_node())


@pytest.mark.webpoet_contract
def test_as_node_on_a_field_with_no_element_is_refused():
    """`.as_node()` re-parses the element a bare-element selector matched. On a `::text`/`::attr()` field
    there is no element to re-parse, and on an unsupported selector there is no answer at all."""
    from frostwork.webpoet import FrostPage, field

    for selector in ("h1::text", "a::attr(href)"):
        with pytest.raises(TypeError, match="is not an element"):
            type("P", (FrostPage,), {"x": field(selector).as_node()})

    with pytest.raises(TypeError, match="is not an element"):
        type("P", (FrostPage,), {"x": field(UNSUPPORTED_SEL).as_node()}, strict=False)


@pytest.mark.webpoet_contract
def test_out_is_resolved_from_the_declaring_class_not_the_merged_view():
    """web-poet merges its `FieldInfo` in `cls.__bases__` order, so a LATER base overwrites an earlier one —
    the opposite of the MRO, which is what selects the descriptor that actually runs. Reading the merged view
    therefore answers about the wrong declaration under multiple inheritance, in both directions.

    Frostwork has to agree with the processor that RUNS, because the answer decides which type it is handed.
    Checked against web-poet's own behaviour over the whole two-base matrix."""
    from web_poet import WebPage
    from web_poet import field as wp_field

    from frostwork.webpoet import _processors_for

    def marker(value, page):
        return "RAN"

    def make_base(out):
        getter = lambda self: "raw"  # noqa: E731
        getter.__name__ = getter.__qualname__ = "v"
        ns = {"v": wp_field(out=out)(getter) if out is not None else wp_field(getter)}
        return type("Base", (WebPage,), ns)

    outs = (None, [], [marker])
    for first in outs:
        for second in outs:
            bases = (make_base(first), make_base(second))
            cls = type("P", bases, {})
            upstream_ran = asyncio.run(cls(response=_resp()).to_item())["v"] == "RAN"
            ours = bool(_processors_for(cls, "v"))
            assert ours == upstream_ran, (
                f"bases=({first!r}, {second!r}): web-poet "
                f"{'runs' if upstream_ran else 'runs no'} processor, frostwork says "
                f"{'a processor runs' if ours else 'none runs'}"
            )


@pytest.mark.webpoet_contract
def test_processor_on_a_scalar_terminal_field_still_gets_strings():
    """The other half of the rule, and the reason it is keyed on the TERMINAL rather than on processor
    presence: `images_processor` takes URL STRINGS and has no `Selector` branch at all, so converting
    every processor-bearing field to a node would return the node unchanged and break a field that
    works today."""
    from frostwork.webpoet import FrostPage, field

    def urls_only(value, page):
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            return [f"IMG:{v}" for v in value]
        return f"<PASSTHROUGH {type(value).__name__}>"

    html = b'<html><body><img class="hero" src="/1.jpg"><img class="hero" src="/2.jpg"></body></html>'

    class P(FrostPage):
        class Processors:
            images = [urls_only]

        images = field("img.hero::attr(src)", all=True)

    item = asyncio.run(P(response=_resp(body=html)).to_item())
    assert item["images"] == ["IMG:/1.jpg", "IMG:/2.jpg"], item["images"]


@pytest.mark.webpoet_contract
def test_node_handoff_reparses_the_subtree_not_the_document():
    """`parsel.Selector(text=...)` wraps a fragment in a synthetic `<html><body>` and its `.root` is the
    `<html>`, so a processor would receive the DOCUMENT rather than the element it selected. The handoff
    must yield the element itself, tag intact — including for a `<td>`, whose fragment reparse is the
    case most likely to acquire a wrapper."""
    from frostwork.webpoet import FrostPage, field

    seen = {}

    def capture(value, page):
        seen["tag"] = value.root.tag
        seen["text"] = value.css("::text").get()
        return "ok"

    html = b'<html><body><table><tr><td class="cell"><b>deep</b></td></tr></table></body></html>'

    class P(FrostPage):
        class Processors:
            cell = [capture]

        cell = field("td.cell").as_node()

    asyncio.run(P(response=_resp(body=html)).to_item())
    assert seen == {"tag": "td", "text": "deep"}, seen


@pytest.mark.webpoet_contract
@pytest.mark.parametrize(
    "shape",
    ["<p>x</p><p>y</p>", "", "text only", "<p>lone</p>", "<!--just a comment-->", "  ", "a<b>c</b>d"],
    ids=["multi-child", "empty", "text-only", "one-child", "comment-only", "whitespace", "mixed"],
)
def test_the_node_handoff_hands_over_parsels_own_node_for_every_element_and_shape(shape):
    """The handoff must hand over the node parsel would have — the whole subtree, compared exactly.

    Three universes, each of which caught something when it was narrower. NAMES: the shared element universe,
    because four hand-checked tags missed the document frame. SHAPES: lxml answers by shape as well as by
    name, so a `<body>` with one child passed while the same body with two came back as a `<div>`. And the
    two ways a field learns WHICH element it matched — `.k` reads the name off the raw source, `<tag>` has it
    pinned by the engine, and only the second can answer for a frame the page never wrote."""
    import os
    import sys

    # the shared element universe, imported rather than restated (see AGENTS.md: never add a name by hand)
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
    from gen_tree_rules import ELEMENTS
    from webpoet_structure import subtree

    from frostwork.webpoet import _as_node, _node_identity

    parsel = _oracle()

    # The three frame elements have to BE the frame rather than a nested copy of it (a second `<body>` does
    # not open an element), so they carry the class on the real tag; everything else nests inside a `<div>`.
    frame_docs = {
        "html": f"<html class=k><head><title>t</title></head><body>{shape}</body></html>",
        "head": f"<html><head class=k><title>t</title>{shape}</head><body>b</body></html>",
        "body": f"<html><head><title>t</title></head><body class=k>{shape}</body></html>",
    }
    wrong = []
    for tag in ELEMENTS:
        doc = frame_docs.get(
            tag,
            f"<html><head><title>t</title></head><body><div id=w>"
            f"<{tag} class=k>{shape}</{tag}></div><p>after</p></body></html>",
        )
        sel = parsel.Selector(text=doc)
        for query in (".k", tag):
            col = frostwork.extract(doc.encode(), [query])[0]
            theirs = sel.css(query)
            assert col and theirs, (
                f"<{tag}> is not matched by {query!r} in the probe document — fix the document, not this list"
            )
            pinned, _synth = _node_identity(query)
            try:
                mine = _as_node(col[0], pinned)
            except Exception as exc:  # noqa: BLE001 - a raise is a verdict here, not a test error
                wrong.append((tag, query, f"{type(exc).__name__}: {exc}"))
                continue
            if subtree(mine.root) != subtree(theirs[0].root):
                wrong.append((tag, query, subtree(mine.root), subtree(theirs[0].root)))
            # ...and the node is its OWN root: a document parse leaves it under a synthesized frame, where
            # `ancestor::*` answers with elements no selector matched and a processor can walk out of the
            # subtree the contract promises
            if mine.root.getparent() is not None or mine.xpath("ancestor::*"):
                wrong.append((tag, query, "attached to invented ancestors"))
    assert not wrong, f"the node handoff did not hand over parsel's node: {wrong[:4]}"


@pytest.mark.webpoet_contract
def test_a_document_with_no_frame_gives_each_field_its_own_node():
    """The case raw source cannot answer, end to end through real fields.

    `extract(b"<p>x</p>", ["html", "body", "p"])` returns the SAME string three times — the page wrote no
    frame, so both frame elements' outer HTML begins with their content — and reading the tag off it would
    hand the `<p>` to all three processors."""
    import os
    import sys

    from frostwork.webpoet import FrostPage, field

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
    from webpoet_structure import subtree

    parsel = _oracle()
    html = b"<p>x</p>"
    assert frostwork.extract(html, ["html", "body", "p"]) == [["<p>x</p>"]] * 3  # the whole problem

    seen = {}

    def capture(value, page):
        seen[value.root.tag] = (subtree(value.root), value.root.getparent(), value.xpath("ancestor::*"))
        return value.root.tag

    class P(FrostPage):
        class Processors:
            frame = [capture]
            page = [capture]
            para = [capture]

        frame = field("html").as_node()
        page = field("body").as_node()
        para = field("p").as_node()

    assert asyncio.run(P(response=_resp(body=html)).to_item()) == {
        "frame": "html", "page": "body", "para": "p",
    }
    theirs = parsel.Selector(text=html.decode())
    for tag, query in (("html", "html"), ("body", "body"), ("p", "p")):
        got, parent, ancestors = seen[tag]
        assert got == subtree(theirs.css(query)[0].root), tag
        assert parent is None and not ancestors, f"<{tag}> came back attached to invented ancestors"


@pytest.mark.webpoet_contract
def test_a_selector_that_cannot_name_its_own_node_is_refused_at_class_definition():
    """`*` matches the synthesized `<html>`, the synthesized `<body>` and the `<p>` in `<p>x</p>`, and the
    engine returns one identical string for all three: no identity to carry, so the declaration is refused
    where every other unanswerable `.as_node()` combination is."""
    from frostwork.webpoet import FrostPage, field

    for selector in ("*", "p, *", "[id]:not(div) *"):
        with pytest.raises(TypeError, match="never wrote"):
            type("Star", (FrostPage,), {"any": field(selector).as_node()})

    # ...while a selector that PINS the name is fine, frame or not: every match is a `<body>`, written or
    # synthesized, and a class/attribute constraint rules a synthesized frame out entirely (it has none).
    for selector in ("body", "div", ".crumbs", "[itemprop=brand]", "//html/body"):
        type("Named", (FrostPage,), {"any": field(selector).as_node()})


@pytest.mark.webpoet_contract
def test_the_node_handoff_does_not_reshape_a_frameset():
    """A document parse applies the document RULES, which put a `<body>` inside a `<frameset>`. This one is
    nested, where parsel's own subtree is `frameset > b`, so re-parsing it as a document would change what a
    direct-child query answers with nothing raised."""
    from frostwork.webpoet import _as_node

    parsel = _oracle()
    doc = "<html><body><div><frameset><b>x</b></frameset></div></body></html>"
    col = frostwork.extract(doc.encode(), ["frameset"])[0]
    node = _as_node(col[0], "frameset")
    assert [c.tag for c in node.root] == ["b"], parsel.Selector(text=doc).css("frameset").get()
    assert node.css("frameset > b::text").get() == "x"


@pytest.mark.webpoet_contract
def test_a_body_field_reaches_its_processor_through_a_real_page_object():
    """The same thing end to end, because that is how it was found: a `FrostPage` whose `<body>` field had
    two children raised `ValueError` from the handoff on an ordinary page."""
    from frostwork.webpoet import FrostPage, field

    seen = {}

    def capture(value, page):
        seen["tag"] = value.root.tag
        seen["children"] = [c.tag for c in value.root]
        return value.root.get("class")

    html = b'<html><body class="page"><p>one</p><p>two</p></body></html>'

    class P(FrostPage):
        class Processors:
            frame = [capture]

        frame = field("body").as_node()

    assert asyncio.run(P(response=_resp(body=html)).to_item()) == {"frame": "page"}
    assert seen == {"tag": "body", "children": ["p", "p"]}


@pytest.mark.webpoet_contract
def test_frame_element_node_handoff_end_to_end():
    """The same rule through a real page object, with a processor that reads what actually broke — the
    node's tag and attributes. `<body>` with a lone child is the collapsing case; `<meta>` is the hoisted
    one, and its whole value lives in an attribute."""
    from frostwork.webpoet import FrostPage, field

    seen = {}

    def capture(value, page):
        seen[value.root.tag] = dict(value.root.attrib)
        return value.root.tag

    html = (b'<html><head><meta itemprop="brand" content="Acme"></head>'
            b'<body class="page"><p>only child</p></body></html>')

    class P(FrostPage):
        class Processors:
            frame = [capture]
            brand = [capture]

        frame = field("body").as_node()
        brand = field("meta[itemprop=brand]").as_node()

    item = asyncio.run(P(response=_resp(body=html)).to_item())
    assert item == {"frame": "body", "brand": "meta"}
    assert seen == {"body": {"class": "page"}, "meta": {"itemprop": "brand", "content": "Acme"}}


def test_selector_terminals_is_the_engines_answer_not_a_heuristic():
    """The node-vs-scalar decision comes from the compiler. Guard the XPath rows in particular: a
    query-string heuristic reads `/text()` and `/@href` as node queries because neither carries a
    `::`-pseudo, which is the bug that shape of code invites."""
    from frostwork._frostwork import selector_terminals

    assert selector_terminals(["h1::text", "a::attr(href)", "div.card"]) == ["text", "attr", "outer"]
    assert selector_terminals(["//a/text()", "//a/@href", "//div[@id='x']"]) == ["text", "attr", "outer"]
    assert selector_terminals(["div:has(.a .b)::text"]) == [None]  # does not compile


def test_attrs_define_on_a_subclass_keeps_its_own_fields():
    """`@attrs.define` (slots=True, the default) does not mutate the class — it builds a NEW one from the
    old `__dict__`. So `__init_subclass__` ran a second time with the markers already converted, and the
    ORIGINAL class was not in the new MRO for the merge to find: own fields vanished from the plan and
    every one of them raised `KeyError` at `to_item()`.

    This is the shape of a page object that needs an injected dependency, which is the documented web-poet
    idiom — and web-poet hit the same attrs/`__init_subclass__` interaction itself (see the workaround
    comment in `web_poet.pages.Extractor`)."""
    import attrs

    from frostwork.webpoet import FrostPage, field

    @attrs.define
    class P(FrostPage):
        name = field("h1::text")
        price = field(".price::text")

    assert set(P._frostwork_specs) == {"name", "price"}
    assert asyncio.run(P(response=_resp()).to_item()) == {"name": "Widget", "price": "$9"}


def test_attrs_define_keeps_inherited_and_own_fields_together():
    """The half that made the bug hard to see: a base class's fields SURVIVED (the base is still in the new
    MRO), so a page object inheriting most of its schema looked fine and only lost what it declared
    itself — one missing key rather than an obviously empty item."""
    import attrs

    from frostwork.webpoet import FrostPage, field

    class Base(FrostPage):
        name = field("h1::text")

    @attrs.define
    class Sub(Base):
        price = field(".price::text")

    assert set(Sub._frostwork_specs) == {"name", "price"}
    assert asyncio.run(Sub(response=_resp()).to_item()) == {"name": "Widget", "price": "$9"}


@pytest.mark.webpoet_contract
def test_attrs_variants_and_groups_survive_class_recreation():
    """`slots=False` mutates in place and always worked, which is exactly why one hand vector could have
    picked the passing side and reported the feature green. Sweep the variants, and include a `Many` —
    groups are recovered through a separate attribute from flat fields, so a fix for one is not a fix for
    the other."""
    import attrs

    from frostwork.webpoet import FrostPage, Many, One, field

    for decorate in (attrs.define, attrs.define(slots=False), lambda c: c):
        @decorate
        class P(FrostPage):
            name = field("h1::text")
            cards = Many(".card", title=field("h3 a::text"))
            first = One(".card", title=field("h3 a::text"))

        assert set(P._frostwork_specs) == {"name"}, decorate
        assert set(P._frostwork_groups) == {"cards", "first"}, decorate
        item = asyncio.run(P(response=_resp(body=GRID)).to_item())
        assert item["cards"] == [{"title": "A"}, {"title": "B"}], decorate
        assert item["first"] == {"title": "A"}, decorate


@pytest.mark.webpoet_contract
def test_strict_false_survives_class_recreation():
    """`strict=False` is a CLASS KEYWORD, so it exists only in the `class ...` statement — and
    `@attrs.define` (slots=True) throws that statement away: it rebuilds the class from its `__dict__`, so
    `__init_subclass__` ran again with no keywords and strictness reverted to the default. A page object
    that had explicitly opted in to unsupported selectors then raised at import.

    The same ORDER bug as the spec recovery, in the one piece of state nothing was carrying. `slots=False`
    mutates in place and always worked, which is exactly why one hand vector could have reported this
    green — so sweep the variants."""
    import attrs

    from frostwork.webpoet import FrostPage, field

    for decorate in (attrs.define, attrs.define(slots=False), _rebuild_class, lambda c: c):
        @decorate
        class P(FrostPage, strict=False):
            name = field("h1::text")
            unsupported = field(UNSUPPORTED_SEL)

        assert P._frostwork_strict is False, decorate
        assert not P.check_schema().ok, decorate
        # the supported field still answers, and the unsupported one is the documented empty column
        item = asyncio.run(P(response=_resp()).to_item())
        assert item == {"name": "Widget", "unsupported": None}, (decorate, item)


@pytest.mark.webpoet_contract
def test_strict_is_not_inherited_by_a_fresh_subclass():
    """The other half of that fix, pinned because it is a deliberate asymmetry: a REBUILD of a class keeps
    its opt-out (it is the same class), while a new SUBCLASS does not inherit one — strictness is the
    default and each class says so for itself. `cls.__dict__` rather than `getattr` is what distinguishes
    the two."""
    from frostwork.webpoet import FrostPage, field

    class Permissive(FrostPage, strict=False):
        unsupported = field(UNSUPPORTED_SEL)

    with pytest.raises(frostwork.UnsupportedSelector):
        class Child(Permissive):
            name = field("h1::text")

    class ChildOptedOut(Permissive, strict=False):
        name = field("h1::text")

    assert set(ChildOptedOut._frostwork_specs) == {"unsupported", "name"}


def test_attrs_define_with_an_injected_dependency():
    """The reason to reach for `@attrs.define` on a page object at all: declaring an extra dependency for
    the framework to inject alongside `response`."""
    import attrs
    from web_poet import field as wp_field

    from frostwork.webpoet import FrostPage, field

    @attrs.define
    class Deps:
        currency: str

    @attrs.define
    class P(FrostPage):
        deps: Deps
        price = field(".price::text")

        @wp_field
        def priced(self):
            return f"{self.deps.currency}{self.price.lstrip('$')}"

    item = asyncio.run(P(response=_resp(), deps=Deps(currency="EUR")).to_item())
    assert item == {"price": "$9", "priced": "EUR9"}


def test_a_field_missing_from_the_plan_explains_itself():
    """Insurance for any OTHER class-rebuilding decorator. The symptom without it is a bare `KeyError`, which
    says nothing about the cause."""
    from frostwork.webpoet import FrostPage, field

    class P(FrostPage):
        name = field("h1::text")

    # simulate a rebuild we do not recognise: the descriptor is installed, the plan has no such column
    P._frostwork_specs = {}
    P._frostwork_flat_names = []
    with pytest.raises(RuntimeError, match="not in its compiled plan"):
        asyncio.run(P(response=_resp()).to_item())


def test_frost_browser_page_scans_a_browser_response():
    """`BrowserResponse` carries `.html` (a str) and no `.body`, so a `FrostPage` fed one raised
    `AttributeError`. It gets its own base rather than a duck-typed branch, because the encoding story
    differs: the browser already resolved the page's encoding, so there is nothing to sniff."""
    from web_poet import BrowserHtml, BrowserResponse, ResponseUrl

    from frostwork.webpoet import FrostBrowserPage, Many, field

    class P(FrostBrowserPage):
        name = field("h1::text")
        cards = Many(".card", title=field("h3 a::text"))

    resp = BrowserResponse(url=ResponseUrl("http://example.com/"), html=BrowserHtml(GRID.decode()))
    item = asyncio.run(P(response=resp).to_item())
    assert item["cards"] == [{"title": "A"}, {"title": "B"}]


def test_browser_page_handles_non_ascii_without_sniffing():
    """The `.html` str is encoded UTF-8 and scanned as UTF-8. A page whose ORIGINAL bytes declared some
    other charset must still come out right, because what the browser handed over is already decoded — so
    a `<meta charset=shift_jis>` left in the snapshot must not re-sniff the re-encoded text."""
    from web_poet import BrowserHtml, BrowserResponse, ResponseUrl

    from frostwork.webpoet import FrostBrowserPage, field

    html = '<html><head><meta charset="shift_jis"></head><body><h1>日本語 café</h1></body></html>'

    class P(FrostBrowserPage):
        name = field("h1::text")

    resp = BrowserResponse(url=ResponseUrl("http://example.com/"), html=BrowserHtml(html))
    assert asyncio.run(P(response=resp).to_item()) == {"name": "日本語 café"}


def test_a_marker_on_a_non_frost_class_fails_at_class_definition():
    """The silent failure this replaces: markers are converted by `FrostFields.__init_subclass__`, so on a
    plain web-poet class nothing converted them and `to_item()` returned an item with the fields simply
    ABSENT — no error, no empty column, nothing to notice. `__set_name__` runs before the parent's
    `__init_subclass__` and the class already exists, so this fires at import."""
    from web_poet import BrowserPage, WebPage

    from frostwork.webpoet import Many, field

    for base in (WebPage, BrowserPage):
        with raises_at_class_definition("does not inherit a Frostwork page base"):
            type("Bad", (base,), {"name": field("h1::text")})
        with raises_at_class_definition("does not inherit a Frostwork page base"):
            type("BadGroup", (base,), {"rows": Many(".card", title=field("h3 a::text"))})


def test_frostwork_input_is_the_extension_point_for_any_other_dependency():
    """web-poet's input universe is larger than the two bases shipped here (an `Extractor` can be given any
    dependency at all), so the bytes-and-encoding hook is public and overridable rather than a private
    branch over known response types."""
    import attrs

    from frostwork.webpoet import FrostFields, field

    @attrs.define
    class RawBytesPage(FrostFields):
        raw: bytes

        def frostwork_input(self):
            return self.raw, "utf-8"

    class P(RawBytesPage):
        name = field("h1::text")

    assert asyncio.run(P(raw=PRODUCT).to_item()) == {"name": "Widget"}


def test_frost_fields_without_an_input_hook_explains_itself():
    from frostwork.webpoet import FrostFields, field

    class P(FrostFields):
        name = field("h1::text")

    with pytest.raises(NotImplementedError, match="frostwork_input"):
        asyncio.run(P().to_item())


def test_field_forwards_web_poets_own_keywords():
    """`field()` builds a real `web_poet.field`, so there was no reason for `web_poet.field`'s options to
    be unreachable through it. `out=` was the one that mattered: without it, wanting a processor meant
    writing a hand-written `@web_poet.field` method (leaving the shared scan) or a nested `Processors`
    class."""
    from web_poet.fields import get_fields_dict

    from frostwork.webpoet import FrostPage, field

    class P(FrostPage):
        plain = field("h1::text")
        wrapped = field("h1::text", out=[lambda v, page: f"[{v}]"])
        tagged = field("h1::text", meta={"expensive": True})
        memo = field("h1::text", cached=True)

    item = asyncio.run(P(response=_resp()).to_item())
    assert item == {"plain": "Widget", "wrapped": "[Widget]", "tagged": "Widget", "memo": "Widget"}
    info = get_fields_dict(P)
    assert info["tagged"].meta == {"expensive": True}
    assert bool(info["wrapped"].out) and not info["plain"].out


@pytest.mark.webpoet_contract
def test_out_processors_run_after_map_transforms():
    """The composition order, pinned: shape by cardinality -> `.map()`/`.re_first()` -> web-poet
    processors. They are not duplicates — a processor takes `(value, page)` and can read the response,
    which a `.map()` cannot — so the order they compose in is part of the contract."""
    from frostwork.webpoet import FrostPage, field

    order = []

    def proc(value, page):
        order.append("processor")
        return f"proc({value})"

    def transform(value):
        order.append("transform")
        return f"map({value})"

    class P(FrostPage):
        v = field("h1::text", out=[proc]).map(transform)

    item = asyncio.run(P(response=_resp()).to_item())
    assert item == {"v": "proc(map(Widget))"}
    assert order == ["transform", "processor"]


@pytest.mark.webpoet_contract
def test_out_on_a_bare_element_field_also_gets_a_node():
    """`out=` is the other route by which a processor arrives, so the node handoff has to honour it too —
    not just the nested `Processors` class."""
    from frostwork.webpoet import FrostPage, field

    def needs_node(value, page):
        from parsel import Selector

        if not isinstance(value, Selector):
            return f"<PASSTHROUGH {type(value).__name__}>"
        return [a.attrib["href"] for a in value.css("a")]

    html = b'<html><body><nav class="crumbs"><a href="/a">A</a></nav></body></html>'

    class P(FrostPage):
        crumbs = field(".crumbs", out=[needs_node]).as_node()

    assert asyncio.run(P(response=_resp(body=html)).to_item()) == {"crumbs": ["/a"]}


@pytest.mark.webpoet_contract
def test_out_empty_list_cancels_an_inherited_processor():
    """`out=[]` is web-poet's way to say "no processors for this field" and cancel an inherited
    `Processors` entry — its resolution is `if out is not None`, so an EMPTY list is an answer, not the
    absence of one. Frostwork read it as a truthiness test, fell through to the nested class, decided a
    processor was coming and handed back a re-parsed `Selector` where web-poet's own answer is the string.

    The field-level opt-out is the whole reason this matters: a page object inheriting
    zyte-common-items' `ProductPage` gets processors on nine field NAMES whether it wants them or not, and
    `out=[]` is the documented way to decline one."""
    from frostwork.webpoet import FrostPage, field

    def never(value, page):  # pragma: no cover - the point is that it is not called
        return "PROCESSED"

    html = b'<html><body><nav class="crumbs"><a href="/a">A</a></nav></body></html>'

    class P(FrostPage):
        class Processors:
            declined = [never]
            kept = [never]

        declined = field(".crumbs", out=[])       # cancels the inherited entry -> raw source, as documented
        kept = field(".crumbs").as_value()       # the entry applies -> the processor runs, on the source

    item = asyncio.run(P(response=_resp(body=html)).to_item())
    assert item["declined"] == '<nav class="crumbs"><a href="/a">A</a></nav>'
    assert item["kept"] == "PROCESSED"


@pytest.mark.webpoet_contract
def test_processor_resolution_matches_web_poets_own():
    """Frostwork has to answer "will a processor run on this field?" the same way web-poet does, because a
    different answer means handing the wrong TYPE to something documented to accept anything and return it
    unchanged — a wrong value with nothing raised.

    So this does not restate the rule, it DERIVES it: for every combination of `out=` and nested
    `Processors`, ask web-poet (by observing whether a processor actually ran on an equivalent
    hand-written field) and compare with `_processors_for`'s answer. `out=[]` is the cell to watch — a
    restated rule gets it backwards in the direction no other test looks."""
    from web_poet import WebPage
    from web_poet import field as wp_field

    from frostwork.webpoet import _processors_for

    def marker(value, page):
        return "RAN"

    for has_nested in (True, False):
        for out in (None, [], [marker]):
            ns = {}
            if has_nested:
                ns["Processors"] = type("Processors", (), {"v": [marker]})
            getter = (lambda self: "raw")
            getter.__name__ = getter.__qualname__ = "v"
            ns["v"] = wp_field(out=out)(getter) if out is not None else wp_field(getter)
            cls = type("P", (WebPage,), ns)
            upstream_ran = asyncio.run(cls(response=_resp()).to_item())["v"] == "RAN"
            ours = bool(_processors_for(cls, "v"))
            assert ours == upstream_ran, (
                f"nested={has_nested} out={out!r}: web-poet {'runs' if upstream_ran else 'runs no'} "
                f"processor, frostwork thinks {'a processor runs' if ours else 'none runs'}"
            )


def test_field_covers_web_poet_fields_whole_keyword_surface():
    """A keyword web-poet adds to `field()` should be a visible gap, not a silent no-op. The enumeration
    lives in one place and is checked against the real signature."""
    import inspect

    from web_poet import field as wp_field

    from frostwork.webpoet import _WP_FIELD_KWARGS, field as frost_field

    upstream = {
        n for n, p in inspect.signature(wp_field).parameters.items()
        if p.kind == p.KEYWORD_ONLY
    }
    assert upstream == set(_WP_FIELD_KWARGS), (
        f"web_poet.field's keyword surface is {sorted(upstream)} but frostwork forwards "
        f"{sorted(_WP_FIELD_KWARGS)} — add the missing one to field() or decline it explicitly"
    )
    ours = set(inspect.signature(frost_field).parameters) - {"selector", "all", "join"}
    assert ours == set(_WP_FIELD_KWARGS), sorted(ours)


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


@pytest.mark.webpoet_contract
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


@pytest.mark.webpoet_contract
def test_a_manual_override_drops_the_inherited_selector():
    """A subclass may replace an inherited Frostwork field with a hand-written `@web_poet.field`. web-poet
    resolves the field to the nearest declaration, so `to_item()` is right regardless — the risk is the
    inherited SELECTOR staying in the merged schema, which makes the plan scan a column nothing reads and
    `frost_schema()` advertise a selector the page object does not answer with."""
    from web_poet import field as wp_field

    from frostwork.webpoet import FrostPage, Many, field

    class Base(FrostPage):
        name = field("h1::text")
        price = field(".price::text")
        cards = Many(".card", title=field("h3 a::text"))

    class Sub(Base):
        @wp_field
        def name(self):
            return "computed"

        @wp_field
        def cards(self):
            return ["computed"]

    assert Sub.frost_schema() == {"fields": {"price": ".price::text"}, "groups": {}}
    assert Sub._frostwork_flat_names == ["price"] and Sub._frostwork_group_names == []
    item = asyncio.run(Sub(response=_resp()).to_item())
    assert item == {"name": "computed", "price": "$9", "cards": ["computed"]}
    # the base is untouched — the override is the subclass's business
    assert set(Base.frost_schema()["fields"]) == {"name", "price"}


@pytest.mark.webpoet_contract
def test_an_override_stays_dropped_in_the_next_generation():
    """The schema is resolved against the MRO, not by popping names off a merged dict — because the popped
    name is still in an ANCESTOR's own declarations, so merging brought it back one generation later.

    Three generations for each replacement shape, since a grandchild is where the resurrection showed:
    a Frostwork field replaced by a hand-written one, a group replaced by a flat field, and a flat field
    replaced by a group."""
    from web_poet import field as wp_field

    from frostwork.webpoet import FrostPage, Many, field

    # 1. Frostwork field -> manual web-poet field
    class A1(FrostPage):
        name = field("h1::text")
        price = field(".price::text")

    class A2(A1):
        @wp_field
        def name(self):
            return "computed"

    class A3(A2):
        link = field("a::attr(href)")

    assert set(A3.frost_schema()["fields"]) == {"price", "link"}, A3.frost_schema()
    assert asyncio.run(A3(response=_resp()).to_item()) == {
        "name": "computed", "price": "$9", "link": "/p/1",
    }

    # 2. group -> flat field
    class B1(FrostPage):
        cards = Many(".card", title=field("h3 a::text"))

    class B2(B1):
        cards = field("h1::text")

    class B3(B2):
        extra = field(".price::text")

    assert B3.frost_schema() == {"fields": {"cards": "h1::text", "extra": ".price::text"}, "groups": {}}
    assert asyncio.run(B3(response=_resp()).to_item()) == {"cards": "Widget", "extra": "$9"}

    # 3. flat field -> group
    class C1(FrostPage):
        cards = field("h1::text")

    class C2(C1):
        cards = Many(".card", title=field("h3 a::text"))

    class C3(C2):
        extra = field(".price::text")

    assert C3.frost_schema()["fields"] == {"extra": ".price::text"}
    assert list(C3.frost_schema()["groups"]) == ["cards"]
    item = asyncio.run(C3(response=_resp(body=GRID)).to_item())
    assert item == {"cards": [{"title": "A"}, {"title": "B"}], "extra": "$1"}


@pytest.mark.webpoet_contract
def test_a_manual_field_mixin_before_the_frost_base_wins():
    """Resolution has to follow the whole MRO, not just "did this class declare it". A mixin listed before
    the Frostwork base supplies the descriptor web-poet will use, so the inherited selector is obsolete even
    though no class in the chain "overrode" it in the obvious sense."""
    from web_poet import field as wp_field

    from frostwork.webpoet import FrostPage, field

    class Mixin:
        @wp_field
        def name(self):
            return "from-mixin"

    class Base(FrostPage):
        name = field("h1::text")
        price = field(".price::text")

    class WithMixin(Mixin, Base):
        pass

    assert WithMixin.frost_schema()["fields"] == {"price": ".price::text"}
    assert asyncio.run(WithMixin(response=_resp()).to_item()) == {"name": "from-mixin", "price": "$9"}

    # ...and with the mixin AFTER the base, the Frostwork field is the one Python resolves, so it stays
    class BaseWins(Base, Mixin):
        pass

    assert set(BaseWins.frost_schema()["fields"]) == {"name", "price"}
    assert asyncio.run(BaseWins(response=_resp()).to_item()) == {"name": "Widget", "price": "$9"}


@pytest.mark.webpoet_contract
def test_an_override_also_clears_a_stale_strict_failure():
    """Why the stale selector was worse than a wasted column: strict validation ran over a schema that still
    contained the REPLACED field, so a class could be refused at import over a selector it does not use."""
    from web_poet import field as wp_field

    from frostwork.webpoet import FrostPage, field

    class Legacy(FrostPage, strict=False):
        broken = field(UNSUPPORTED_SEL)

    # replacing the unsupported field means this class's own schema is clean — so the strict default holds
    class Fixed(Legacy):
        @wp_field
        def broken(self):
            return "hand-written"

    assert Fixed.check_schema().ok
    assert asyncio.run(Fixed(response=_resp()).to_item()) == {"broken": "hand-written"}


@pytest.mark.webpoet_contract
def test_many_rejects_web_poet_keywords_on_a_subfield():
    """`Many`/`One` shape a row from their subfields; the GROUP is the single `web_poet.field` web-poet
    knows about, so `out=`/`cached=`/`meta=` on a subfield have nothing to attach to. Accepting and
    dropping them means a processor silently not running, which is the failure mode this integration is
    most exposed to — so they are refused at declaration, where the mistake is."""
    from frostwork.webpoet import FrostPage, Many, One, field

    for maker in (Many, One):
        with pytest.raises(TypeError, match="cannot apply to a subfield"):
            maker(".card", title=field("h3::text", out=[lambda v, page: v]))
        with pytest.raises(TypeError, match="cannot apply to a subfield"):
            maker(".card", title=field("h3::text", cached=True))
        with pytest.raises(TypeError, match="cannot apply to a subfield"):
            maker(".card", title=field("h3::text", meta={"k": 1}))

    # .map() is the supported way to transform a subfield's value, and it still works
    class P(FrostPage):
        cards = Many(".card", title=field("h3 a::text").map(str.upper))

    assert asyncio.run(P(response=_resp(body=GRID)).to_item()) == {"cards": [{"title": "A"}, {"title": "B"}]}


@pytest.mark.webpoet_contract
def test_every_shipped_base_is_injectable():
    """scrapy-poet builds a callback argument only if `web_poet.pages.is_injectable` accepts its class, and
    andi SILENTLY drops what it rejects — no exception, no log, just a missing argument. `FrostFields` was
    built on web-poet's `Extractor`, which is deliberately not injectable (it is the shape for a field
    bundle composed into a page), so the one base documented as "the shape to reach for when the input is
    neither of the two responses" could not be injected at all.

    Asked through andi with web-poet's own predicate: that is the exact question scrapy-poet asks, and it
    needs neither Scrapy nor scrapy-poet installed to answer."""
    import andi
    from web_poet import HttpResponse
    from web_poet.pages import is_injectable

    from frostwork.webpoet import FrostBrowserPage, FrostFields, FrostPage, field

    class Custom(FrostFields):
        name = field("h1::text")

        def frostwork_input(self):
            return b"<h1>x</h1>", "utf-8"

    for base in (Custom, FrostPage, FrostBrowserPage, FrostFields):
        assert is_injectable(base), f"{base.__name__} is not injectable"

    def callback(response: HttpResponse, page: Custom):  # what a spider writes
        ...

    plan = andi.plan(callback, is_injectable=is_injectable, externally_provided={HttpResponse})
    assert set(plan[-1][1]) == {"response", "page"}, (
        "andi left the page object out of the callback plan, which is how this fails in production: "
        "scrapy-poet omits the argument instead of raising"
    )


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


@pytest.mark.webpoet_contract
def test_frostpage_field_map_and_re_first():
    from frostwork.webpoet import FrostPage, field

    class P(FrostPage):
        price = field(".price::text").map(lambda s: s.lstrip("$")).map(float)
        symbol = field(".price::text").re_first(r"^\D+")
        n_images = field("img::attr(src)", all=True).map(len)

    item = asyncio.run(P(response=_resp()).to_item())
    assert item == {"price": 9.0, "symbol": "$", "n_images": 2}


@pytest.mark.webpoet_contract
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


@pytest.mark.webpoet_contract
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
        [("offers", ".offer", {"price": ".//span/text()", "sib": "a + b::text", "kid": "./h3[last()]/text()"})],
    )
    assert not r.ok
    assert not r.over_budget
    assert r.fields[0].supported
    unsup = {f.name: f.reason for f in r.unsupported}
    assert ":has()" in unsup["[1]"]
    assert "positional" in unsup["[2]"]
    assert "sibling combinator" in unsup["sib"]
    assert "deferred-close" in unsup["kid"]
    assert r.groups[0].container.supported  # .offer is fine


def test_check_reads_a_dict_as_name_to_selector():
    # Regression: `{name: selector}` — the shape Page/FrostPage schemas are written in, and what
    # `FrostPage.frost_schema()` returns — must be READ, not iterated: iterating audits the field NAMES.
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
    subs = {"price": ".//span/text()", "kid": "./h3[last()]/text()"}  # deferred predicates remain unsupported in groups
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
        blurb = field(UNSUPPORTED_SEL)
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
    # The extraction twin, and worse: iterating `{name: selector}` would extract
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
        bad = field(UNSUPPORTED_SEL)
        offers = Many(".offer", price=field(".//span/text()"), oops=field("a ~ b::text"))

    r = Mixed.check_schema()
    assert not r.ok
    names = {f.name for f in r.unsupported}
    assert "bad" in names and "oops" in names

    # Strict validation is the default and fails loudly at definition time.
    with pytest.raises(frostwork.UnsupportedSelector):
        class Broken(FrostPage):
            title = field("h1::text")
            broken = field(UNSUPPORTED_SEL)

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


def test_has_selector_list_matches_the_union_of_its_members():
    # `:has(a, img)` is a relative selector LIST — valid CSS that cssselect rejects outright
    # (scrapy/cssselect#138). The oracle is the union of the single-member spellings it DOES accept:
    # `E:has(a, b)` matches exactly the E-nodes matched by `E:has(a)` or `E:has(b)`.
    parsel = _oracle()
    import cssselect

    html = (
        b"<html><body>"
        b'<div id="d1"><a>x</a></div>'
        b'<div id="d2"><img src="s"></div>'
        b'<div id="d3"><b>y</b></div>'
        b'<div id="d4"><a>z</a><img src="t"></div>'
        b'<div id="d5"><span><a>deep</a></span></div>'
        b"</body></html>"
    )
    sel = parsel.Selector(body=html, encoding="utf-8")

    # premise: the list spelling really is a syntax error to cssselect, so parsel cannot answer it
    with pytest.raises(cssselect.SelectorSyntaxError):
        sel.css("div:has(a, img)")

    for frost_sel, oracle_sel in [
        ("div:has(a, img)::attr(id)", "div:has(a), div:has(img)"),
        ("div:has(img, a)::attr(id)", "div:has(a), div:has(img)"),  # member order is irrelevant
        ("div:has(a, img, b)::attr(id)", "div:has(a), div:has(img), div:has(b)"),
        ("div:has(a, nosuch)::attr(id)", "div:has(a)"),  # a member matching nothing adds nothing
        ("div:has(> a, > img)::attr(id)", "div:has(> a), div:has(> img)"),  # child-scoped list
    ]:
        got = frostwork.extract(html, [frost_sel])[0]
        want = sel.css(oracle_sel).xpath("@id").getall()
        assert got == want, (frost_sel, got, want)

    # a list MIXING relative combinators has no faithful representation (one `rel` per `:has`), so it is
    # REPORTED unsupported rather than answered under whichever half arrived first
    assert not frostwork.check(["div:has(> a, img)::attr(id)"]).ok
    assert frostwork.check(["div:has(a, img)::attr(id)"]).ok


def test_not_selector_list_matches_the_chained_spelling():
    # `:not(a, b)` is Selectors 4 and cssselect rejects it, but it is exactly `:not(a):not(b)` — which
    # cssselect accepts — so the chained spelling is a direct oracle.
    parsel = _oracle()
    import cssselect

    html = (
        b"<html><body>"
        b'<p class="a">A</p><p class="b">B</p><p class="c">C</p><p>D</p>'
        b'<p title="x, y">E</p>'
        b"</body></html>"
    )
    sel = parsel.Selector(body=html, encoding="utf-8")

    with pytest.raises(cssselect.SelectorSyntaxError):
        sel.css("p:not(.a, .b)")

    for frost_sel, oracle_sel in [
        ("p:not(.a, .b)::text", "p:not(.a):not(.b)::text"),
        ("p:not(.a, .b, .c)::text", "p:not(.a):not(.b):not(.c)::text"),
        # a comma inside an attribute VALUE is data, not a member separator
        ('p:not([title="x, y"])::text', 'p:not([title="x, y"])::text'),
    ]:
        got = frostwork.extract(html, [frost_sel])[0]
        assert got == sel.css(oracle_sel).getall(), frost_sel

    assert not frostwork.check(["p:not(.a, )::text"]).ok  # empty member — cssselect rejects it too


def test_attribute_case_insensitive_flag_matches_an_lxml_fold():
    # `[a=v i]` is Selectors 4; cssselect rejects the flag, so the oracle is the equivalent XPath fold
    # evaluated by lxml itself.
    parsel = _oracle()
    import cssselect

    html = (
        b"<html><body>"
        b'<a id="1" href="/Doc.PDF" type="SubMit" rel="NoFollow Me">x</a>'
        b'<a id="2" href="/other.txt" type="reset" rel="nofollow">y</a>'
        b"</body></html>"
    )
    sel = parsel.Selector(body=html, encoding="utf-8")
    up, lo = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
    fold = f"translate(@type,'{up}','{lo}')"

    with pytest.raises(cssselect.SelectorSyntaxError):
        sel.css("[type=submit i]")

    assert frostwork.extract(html, ["[type=submit i]::attr(id)"])[0] == sel.xpath(
        f"//a[{fold}='submit']/@id"
    ).getall()
    # ...and the flag really is what makes the difference: without it the same selector matches nothing
    assert frostwork.extract(html, ["[type=submit]::attr(id)"])[0] == []
    # every operator honours it, and `~=` still tokenizes on ASCII whitespace
    for q, want in [
        ('[href^="/doc" i]::attr(id)', ["1"]),
        ('[href$=".pdf" i]::attr(id)', ["1"]),
        ('[href*="OC.p" i]::attr(id)', ["1"]),
        ("[rel~=nofollow i]::attr(id)", ["1", "2"]),
    ]:
        assert frostwork.extract(html, [q])[0] == want, q
    # `s` is the default and parses as a no-op; the fold is ASCII-only, as CSS defines it
    assert frostwork.extract(html, ['[type="SubMit" s]::attr(id)'])[0] == ["1"]
    acc = "<p id=3 data-x='CAFÉ'>t</p>".encode()
    assert frostwork.extract(acc, ['[data-x="café" i]::attr(id)'])[0] == []
    assert frostwork.extract(acc, ['[data-x^="ca" i]::attr(id)'])[0] == ["3"]
    assert frostwork.check(["[type=submit i]::attr(id)"]).ok


def test_detect_encoding_reports_what_extract_will_use():
    # The prescan is reachable on its own. Nothing else in a scraper's stack answers this the way a
    # browser does: parsel never sniffs, and w3lib stops at `<body>` and at an unresolvable label.
    for body, label, want in [
        (b"\xef\xbb\xbf<p>x</p>", None, "UTF-8"),                       # BOM
        (b"<meta charset=windows-1252><p>x</p>", None, "windows-1252"),
        (b"<html><body><meta charset=shift_jis>", None, "Shift_JIS"),   # past w3lib's <body> stop
        (b"<!-- charset=big5 --><meta charset=euc-jp>", None, "EUC-JP"),  # comments are skipped
        (b"<meta charset=nosuch><meta charset=gbk>", None, "GBK"),      # failure, continue
        (b"<p>x</p>", "iso-8859-1", "windows-1252"),                    # the WHATWG label table
        (b"<p>x</p>", "latin-1", "windows-1252"),                       # a Python codec spelling
        (b"<p>x</p>", "bogus-label", "UTF-8"),                          # unresolvable -> sniff
        ('<?xml version="1.0"?><p>x</p>'.encode("utf-16-le"), None, "UTF-16LE"),
        (b"<p>x</p>", None, "UTF-8"),
        ("<p>café</p>", None, "UTF-8"),  # already-decoded str: those bytes ARE UTF-8
    ]:
        assert frostwork.detect_encoding(body, label) == want, (body[:40], label)

    # ...and it is the encoding `extract` actually decoded with, not a second opinion
    b = b"<meta charset=windows-1252><p>caf\xe9</p>"
    assert frostwork.detect_encoding(b) == "windows-1252"
    assert frostwork.extract(b, ["p::text"])[0] == ["café"]


def test_single_valued_page_stops_scanning_without_changing_the_item():
    # EARLY EXIT: a Page whose fields are all single-valued may stop as soon as each has a value. The
    # observable contract is that the ITEM is unchanged — the values dropped are the ones a
    # single-valued consumer discards anyway — so every case here compares against the full scan.
    head = b"<html><head><title>T</title><link rel=canonical href=/a></head><body>"
    tail = b"<title>LATER</title><link rel=canonical href=/b><p>body</p></body></html>"
    doc = head + tail

    page = frostwork.Page().field("t", "title::text").field("c", "link::attr(href)")
    item = page.extract(doc)
    assert item.to_dict() == {"t": "T", "c": "/a"}

    # the same values a full scan's cardinality reduction gives; `extract` is never armed and still
    # sees the tail, which is what proves the two are the same answer rather than the same bug
    cols = frostwork.extract(doc, ["title::text", "link::attr(href)"])
    assert [c[0] for c in cols] == ["T", "/a"]
    assert len(cols[0]) == 2

    # one multi-valued field disarms the whole schema
    both = frostwork.Page().field("t", "title::text").field_all("all", "title::text")
    assert both.extract(doc).to_dict() == {"t": "T", "all": ["T", "LATER"]}
    joined = frostwork.Page().field_join("j", "title::text", "|")
    assert joined.extract(doc).to_dict() == {"j": "T|LATER"}

    # a field that never matches leaves the schema unsatisfied, so the scan runs to EOF and the fields
    # that DO match are still complete
    missing = frostwork.Page().field("t", "title::text").field("n", "nosuchtag::text")
    assert missing.extract(doc).to_dict() == {"t": "T", "n": None}

    # shapes that are not armed still answer correctly (deferred, outer-HTML, grouped)
    d = b"<div>x<p>a</p><p>b</p></div><div>y<p>c</p></div>"
    assert frostwork.Page().field("x", "p:last-child::text").extract(d).to_dict() == {"x": "b"}
    assert frostwork.Page().field("x", "p").extract(d).to_dict() == {"x": "<p>a</p>"}
    grouped = frostwork.Page().field("x", "div::text").many("rows", "div", {"p": "p::text"})
    assert grouped.extract(d).to_dict() == {"x": "x", "rows": [{"p": "a"}, {"p": "c"}]}


def test_names_longer_than_libxml2s_buffer_are_kept_whole():
    # libxml2 parses element and attribute names into a fixed 100-byte buffer and silently keeps the
    # first 100 characters. html5lib and every browser keep the whole name, and so does Frostwork —
    # a divergence in our favor (docs/COMPATIBILITY.md, "Beyond lxml"). Found on a crawled page whose
    # templating had run away and emitted `data-wp-` eleven times in front of `oncontextmenu`.
    #
    # Both halves are asserted. The oracle's truncation is a PREMISE: if libxml2 ever grows the buffer
    # the entry stops being a divergence and this test says so instead of passing silently.
    parsel = _oracle()
    attr = "data-" + "x" * 110          # 115 chars
    tag = "x" + "y" * 110
    html = f"<p {attr}='v'>t</p><{tag}>u</{tag}>".encode()
    sel = parsel.Selector(body=html, encoding="utf-8")

    # premise: the oracle truncates at 100, so the page's REAL names are absent from its tree
    assert sel.css(f"[{attr}]").getall() == []
    assert sel.css(tag).getall() == []
    assert sel.css(f"[{attr[:100]}]::text").getall() == ["t"]
    assert sel.css(f"{tag[:100]}::text").getall() == ["u"]

    # ...and Frostwork answers by the name the document actually carries
    assert frostwork.extract(html, [f"[{attr}]::text"])[0] == ["t"]
    assert frostwork.extract(html, [f"p::attr({attr})"])[0] == ["v"]
    assert frostwork.extract(html, [f"{tag}::text"])[0] == ["u"]
    assert frostwork.extract(html, [f"//p/@{attr}"])[0] == ["v"]

    # THE PORT HAZARD, stated as an assertion: a selector copied out of the lxml tree carries the
    # truncated name, which is in no document and matches nothing here. An empty column, never a value.
    assert frostwork.extract(html, [f"[{attr[:100]}]::text"])[0] == []
    assert frostwork.extract(html, [f"{tag[:100]}::text"])[0] == []
    # and it is a supported selector, so the emptiness is the page's answer rather than a refusal
    assert frostwork.check([f"[{attr[:100]}]::text", f"{tag[:100]}::text"]).ok


def test_is_where_matches_correct_and_semantics():
    # `:is()` combined with a class/attr/id or another `:is` is implemented with CORRECT AND semantics.
    # This WAS a documented divergence: cssselect <= 1.4.0 mis-translates it, ORing the base condition
    # with the alternatives. Frostwork implemented the correct semantics anyway (the oracle-bug policy),
    # cssselect >= 1.5.0 agrees; <= 1.4.0 does not.
    #
    # The primary oracle is deliberately NOT parsel's direct `.css(":is(...)")` — it is the equivalent
    # comma-EXPANSION, which BOTH cssselect versions translate correctly. That keeps the test valid
    # whichever side of 1.5.0 the installed cssselect is on, so this file does not need a version pin to
    # be meaningful. The direct evaluation is then asserted to AGREE, which is what makes an upstream
    # regression (or an accidental downgrade to <= 1.4.0) reopen the divergence loudly instead of
    # silently passing. If that assertion fails, check cssselect's version before suspecting the engine.
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
    import cssselect

    for form, expansion in pairs:
        got = frostwork.extract(html, [form])[0]
        assert got == sel.css(expansion).getall(), form
        # cssselect 1.5.0+ translates the direct form correctly too, so it must agree. A failure here is
        # the divergence reopening — either an upstream regression or a cssselect older than the pin.
        assert got == sel.css(form).getall(), (
            f"{form}: parsel disagrees with the correct AND semantics — cssselect "
            f"{cssselect.__version__} may have regressed to the <=1.4.0 OR mis-translation "
            f"(see COMPATIBILITY.md, ':is()/:where() combined with other conditions')"
        )


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
        f"ignored = Page().field('x', '{UNSUPPORTED_SEL}')\n"
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


def test_audit_cli_audits_every_page_base_not_just_frostpage(tmp_path, capsys):
    """Discovery keys off `FrostFields`, the shared machinery base, so a browser page object and a custom
    `frostwork_input()` one are audited too. Keying it off `FrostPage` would have left the newer bases
    silently un-audited — which is how the no-fallback contract turns an unsupported selector into an
    empty field nobody warned about."""
    from frostwork.audit import main

    target = _write(
        tmp_path,
        "import attrs\n"
        "from frostwork.webpoet import FrostBrowserPage, FrostFields, field\n"
        # strict=False so the unsupported selector survives class definition and reaches the audit,
        # which is exactly the situation the CLI exists for
        "class Browser(FrostBrowserPage, strict=False):\n"
        "    bad = field('li:has(.a .b)::text')\n"
        "@attrs.define\n"
        "class Custom(FrostFields, strict=False):\n"
        "    raw: bytes\n"
        "    def frostwork_input(self):\n"
        "        return self.raw, None\n"
        "class Sub(Custom, strict=False):\n"
        "    ok = field('h1::text')\n",
    )
    assert main([target]) == 1  # Browser's selector is unsupported -> nonzero
    out = capsys.readouterr().out
    assert "Browser" in out and "li:has(.a .b)::text" in out, out
    assert "Sub" in out, out  # the custom-input page object is discovered too


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
    rules = (LinkExtractor(restrict_css=[".pagination a", "li:last-child > a"]),)

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
    assert "spider.py:10" in out and UNSUPPORTED_REASON in out  # LinkExtractor restrict_css
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
        f"    broken = field('{UNSUPPORTED_SEL}')\n"
        "p = Page().field('t', 'h2::text').many('rows', '.row', {'c': './/td/text()'})\n",
    )
    assert main(["--scan", target, "-v"]) == 1
    out = capsys.readouterr().out
    for selector in ("'h1::text'", "'.card'", "'.//h3/text()'", "'h2::text'", "'.row'", "'.//td/text()'"):
        assert selector in out, selector
    assert UNSUPPORTED_REASON in out
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

    "The oracle rejects it, so we must be empty" is the rule for everything EXCEPT the handful of valid
    CSS constructs Frostwork answers deliberately (docs/COMPATIBILITY.md, "Beyond lxml"). Those are
    declared with their expected values rather than exempted, because the rule only stays sharp if a new
    capability has to be written down: loosening the assertion to "or we answer it" would retire the
    check that catches a genuine over-match.
    """
    parsel = _oracle()
    html = (b'<html><body><p class="shared" data-k="v1" title="it\'s">A</p>'
            b'<p class="shared1">B</p><div><p id="i1">D</p></div></body></html>')
    beyond = {
        '[data-k="V1" i]::text': ["A"],          # Selectors 4 case flag
        "p:not(.shared, .nope)::text": ["B", "D"],  # `:not()` selector list
        "div:has(p, span) p::text": ["D"],       # `:has()` relative selector list
    }
    for sel, want_beyond in beyond.items():
        with pytest.raises(Exception):  # premise: the oracle really cannot run these
            parsel.Selector(body=html, encoding="utf-8").css(sel)
        assert frostwork.check([sel]).fields[0].supported, f"{sel!r}: declared beyond, but refused"
        assert frostwork.extract(html, [sel])[0] == want_beyond, sel

    for sel in [
        "[data-k='v1']::text", "[title='it\\'s']::text", '[title="it\'s"]::text',  # single quotes
        "*|p::text", "|p::text", "html|p::text",                                   # namespace prefixes
        "div  >  p::text", "div\t>\tp::text", "div\n p::text", "  p::text  ",      # internal whitespace
        "p/*c*/::text", "div/*x*/ p::text",                                        # CSS comments
        "div>p::text", "div+p::text", "div~p::text",                               # no space around combs
        "[data-k=v1 x]::text",                     # a bogus attribute flag is still a syntax error
        "p:not(.a, )::text", "div:has(> p, span)::text",  # the list forms' own refusals
    ]:
        assert sel not in beyond
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
    family is the one this subset deliberately rejects, so it is worth a standing check that the refusal
    is *empty* rather than a wrong answer.
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
