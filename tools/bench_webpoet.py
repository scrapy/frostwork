"""`FrostPage.to_item()` vs the equivalent Parsel `web_poet.WebPage` — the PAGE-OBJECT level.

`bench_matrix.py` times the engine against Parsel selector-for-selector. This times what a scraper
actually calls: `await page.to_item()` on a whole page object. The difference matters because the win here
is almost entirely per-field TRAVERSAL, not the parse — so it scales with FIELD COUNT, and quoting a single
number without saying at what field count is how a benchmark becomes folklore. The sweep is the point.

Parity before timing, the same standard as `bench_matrix`: both page objects are run once and their items
compared field by field, and a mismatch ABORTS instead of being timed. A speed comparison between two
things computing different answers is not a benchmark.

The Parsel side is its real reuse pattern — one `parsel.Selector` per response, then one query per field —
which is exactly what a hand-written `web_poet.WebPage` does.

Run:  .venv/bin/python tools/bench_webpoet.py
      .venv/bin/python tools/bench_webpoet.py --markdown       # the table for docs/BENCHMARKS.md
      .venv/bin/python tools/bench_webpoet.py --boundaries     # where the curve does NOT hold

Build the extension RELEASE-first (`maturin develop --release`); a debug build measures nothing useful.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parsel
import webpoet_structure
from parsel.selector import SelectorList
from web_poet import HttpResponse, HttpResponseBody, HttpResponseHeaders, ResponseUrl, WebPage
from web_poet import field as wp_field

from frostwork.webpoet import FrostPage, field

URL = "http://example.com/p/1"

# A realistic product/listing selector mix. Field counts below are PREFIXES of this pool, so every row of
# the sweep is a superset of the row above it — the curve is one schema growing, not five unrelated ones.
POOL = [
    ("name", "h1::text"),
    ("sku", ".sku::text"),
    ("price", ".price::text"),
    ("brand", ".brand::text"),
    ("title", "title::text"),
    ("cardTitles", ".card h3 a::text"),
    ("cardHrefs", ".card h3 a::attr(href)"),
    ("cardPrices", ".card .price::text"),
    ("cardSkus", ".card .sku::text"),
    ("tags", ".card .tags li::text"),
    ("images", "img::attr(src)"),
    ("links", "a::attr(href)"),
    ("cardClasses", ".card::attr(class)"),
    ("listItems", "li::text"),
    ("paras", "p::text"),
    ("spans", "span::text"),
    ("descText", ".desc ::text"),
    ("descLinks", ".desc a::attr(href)"),
    ("navLinks", "nav a::attr(href)"),
    ("navText", "nav a::text"),
]


def gen_page(cards: int) -> bytes:
    row = (
        '<div class="card"><h3><a href="/p{i}">Item {i}</a></h3>'
        '<p class="price">${i}.99</p><span class="sku">SKU-{i}</span>'
        '<ul class="tags"><li>a</li><li>b</li></ul>'
        '<img src="/i/{i}.jpg"></div>'
    )
    body = (
        "<h1>Roomy Bag</h1>"
        '<nav class="crumbs"><a href="/">Home</a><a href="/c">Cat</a></nav>'
        '<span class="sku">SKU-100</span><span class="price">$19.50</span>'
        '<span class="brand">Acme</span>'
        '<div class="desc"><p>A   roomy   bag.</p><p>See <a href="/more">more</a>.</p></div>'
        + "".join(row.format(i=i) for i in range(cards))
    )
    return f"<html><head><title>Product</title></head><body>{body}</body></html>".encode()


def _resp(html: bytes):
    return HttpResponse(
        url=ResponseUrl(URL),
        body=HttpResponseBody(html),
        headers=HttpResponseHeaders({"Content-Type": "text/html; charset=utf-8"}),
    )


def build_pair(n: int):
    """A `FrostPage` and an equivalent Parsel `WebPage` over the first `n` selectors of the pool."""
    sels = POOL[:n]
    frost_ns = {name: field(sel, all=True) for name, sel in sels}

    parsel_ns = {}
    for name, sel in sels:
        def getter(self, sel=sel):
            return self.css(sel).getall()

        getter.__name__ = getter.__qualname__ = name
        parsel_ns[name] = wp_field(getter)

    return (
        type("FrostBench", (FrostPage,), frost_ns),
        type("ParselBench", (WebPage,), parsel_ns),
    )


def assert_parity(frost_cls, parsel_cls, html: bytes, n: int) -> None:
    """Both items must be identical before either is timed."""
    resp = _resp(html)
    mine = asyncio.run(frost_cls(response=resp).to_item())
    theirs = asyncio.run(parsel_cls(response=resp).to_item())
    if set(mine) != set(theirs):
        raise SystemExit(f"bench-webpoet: field sets differ at n={n}: {set(mine) ^ set(theirs)}")
    for key in mine:
        if mine[key] != theirs[key]:
            raise SystemExit(
                f"bench-webpoet: values differ at n={n}, field {key!r} — refusing to time two page "
                f"objects that compute different answers.\n  frostwork={mine[key]!r:.200}\n"
                f"  parsel   ={theirs[key]!r:.200}"
            )


def timeit(fn, reps: int) -> float:
    """Median ms over `reps` runs, after a warmup."""
    fn()
    out = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t) * 1000.0)
    return statistics.median(out)


def timeit_loop(coro_fn, reps: int) -> float:
    """Median ms with ONE event loop reused across reps, instead of `asyncio.run` per sample.

    The sweep above pays a fresh loop per call, which is what a script does but not what a crawler does —
    Scrapy has a running loop already. Reporting only the `asyncio.run` number attributes loop setup to the
    page object, and at one field that setup is a visible share of the total."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro_fn())
        out = []
        for _ in range(reps):
            t = time.perf_counter()
            loop.run_until_complete(coro_fn())
            out.append((time.perf_counter() - t) * 1000.0)
        return statistics.median(out)
    finally:
        loop.close()


def transient_peak_kb(fn) -> float:
    """Peak TRANSIENT Python allocation during one call, in KB (tracemalloc's high-water mark).

    Not RSS and not retention: it says how much the call allocates at once, which is the question for the
    cardinality boundary (a column materialised and then discarded). What a cold response RETAINS is measured
    directly instead, by looking at the string web-poet cached."""
    import tracemalloc

    fn()  # warm the caches that are not the subject (compiled plans, cssselect translation)
    tracemalloc.start()
    fn()
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak / 1024.0


# ------------------------------------------------------------------- the performance boundaries
# Three shapes where the healthy-path curve above does NOT hold. Only ONE of them is slower than Parsel
# (the node handoff at scale); the other two are wasted work and a parity cost, which is why they are
# measured here rather than described — "there is a cliff" and "it costs 1.6x at ten matches" are
# different claims, and only the second can be checked. See docs/BENCHMARKS.md ("Performance boundaries").
def _structure_of(value):
    """The re-parsed structure of one HTML fragment (anything else passes through), so a parity check can
    allow the raw-source-vs-reflow divergence without allowing a different element.

    `webpoet_structure.structure_of` is the differential's own comparison. A second, weaker copy lived here
    and collapsed whitespace with `.split()`, under which `<pre>a  b</pre>` and `<pre>a b</pre>` are the same
    page — a benchmark can then time two page objects computing different answers."""
    if not isinstance(value, str):
        return value
    return webpoet_structure.structure_of(value)


def _same_answer(label: str, a, b) -> None:
    """Both sides of a boundary row must compute the same thing before either is timed — the same standard as
    the sweep above. Promising parity and not checking it is how a benchmark starts comparing two different
    computations."""
    if a != b:
        raise SystemExit(
            f"bench-webpoet: {label} — the two sides disagree, so timing them is meaningless.\n"
            f"  frostwork={str(a)[:160]}\n  parsel   ={str(b)[:160]}"
        )


def boundary_cardinality(reps: int, sizes=(220, 2000, 6000)):
    """A FIRST-match field over a selector that matches everything.

    Cardinality reaches the plan (`Plan::compile_first_only`): a page object whose fields are all
    single-valued stops scanning once each has a value. `all=True` is the same field asked for
    everything — the work a multi-valued field genuinely needs — so the pair brackets what early exit
    skips, and the two should differ by roughly the ratio of the page to its first match. The `peak`
    column is the other half: one retained element source instead of N.

    The parsel column is the honest control, and the comparison that matters: a `first` field is the
    shape where lxml is structurally cheapest (`.get()` serializes only the element it returns).
    `all=True` is this engine against itself.

    A red-gate note: the two rows are compared for VALUE equality by `_same_answer` above, not for time,
    so a silently disarmed early exit would show up here only as a number nobody checks. The behavioural
    assertions are `src/page.rs::all_first_fields_stop_the_scan_and_keep_the_same_values` and
    `::an_outer_html_first_field_stops_only_once_no_capture_is_open`."""
    rows = []
    for cards in sizes:
        html = gen_page(cards)
        resp = _resp(html)
        First = type("FirstCard", (FrostPage,), {"card": field("div.card")})
        All = type("AllCards", (FrostPage,), {"card": field("div.card", all=True)})

        def parsel_first(html=html):
            return parsel.Selector(body=html, encoding="utf-8").css("div.card").get()

        # the `first` field and parsel's `.get()` must agree, allowing the documented raw-source divergence
        # (Frostwork returns source, lxml a reflow) — compared by re-parsed structure, like the differential
        mine = asyncio.run(First(response=resp).to_item())["card"]
        _same_answer(f"cardinality-first/{cards}", _structure_of(mine), _structure_of(parsel_first()))
        # ...and the `all=True` row, against `.getall()`: it is a different column with a different
        # cardinality, so timing it on the strength of the scalar row's parity proves nothing about it
        mine_all = asyncio.run(All(response=resp).to_item())["card"]
        theirs_all = parsel.Selector(body=html, encoding="utf-8").css("div.card").getall()
        _same_answer(
            f"cardinality-all/{cards}",
            [_structure_of(v) for v in mine_all],
            [_structure_of(v) for v in theirs_all],
        )
        f_ms = timeit(lambda c=First: asyncio.run(c(response=resp).to_item()), reps)
        a_ms = timeit(lambda c=All: asyncio.run(c(response=resp).to_item()), reps)
        p_ms = timeit(parsel_first, reps)
        f_kb = transient_peak_kb(lambda c=First: asyncio.run(c(response=resp).to_item()))
        rows.append(
            (f"{cards} matches, page {len(html) // 1024} KB", f_ms,
             f"all=True {a_ms:.2f}ms ({a_ms / f_ms:.0f}x), parsel .get() {p_ms:.2f}ms, peak {f_kb:.0f} KB")
        )
    return rows


def boundary_cold_response(reps: int, cards: int = 220):
    """A response with no charset anywhere: none in `Content-Type`, no BOM, no `<meta>`.

    `FrostPage.frostwork_input()` reads `resp.encoding` so the bytes are scanned with the label Parsel would
    have decoded with. On such a response web-poet cannot answer from metadata, so `HttpResponse.encoding`
    falls through to `_body_inferred_encoding()`, which runs `w3lib.html_to_unicode` over the WHOLE body and
    caches the decoded text on the response — a page-sized string the scan never needed. The metric is
    therefore what is RETAINED, not the peak of the call: the string outlives it, on the response.

    Also reported: the label itself. Inference answers `cp1252` where Frostwork's own sniffer would default
    to `utf-8`, so passing the label through is not a missed optimisation — it is the parity decision. Not
    reading it would decode some pages differently from Parsel, which is a correctness change wearing a
    performance costume."""
    body = gen_page(cards)
    rows = []
    for label, headers in (
        ("labelled (charset in Content-Type)", {"Content-Type": "text/html; charset=utf-8"}),
        ("cold (no charset anywhere)", {}),
    ):
        def make():
            return HttpResponse(
                url=ResponseUrl(URL),
                body=HttpResponseBody(body),
                headers=HttpResponseHeaders(headers),
            )

        def read():
            return make().encoding

        r = make()
        enc = r.encoding
        retained = len(r._cached_text or "") / 1024.0  # the decode web-poet cached on the response
        rows.append(
            (label, timeit(read, reps * 2),
             f"encoding={enc!r}, body {len(body) // 1024} KB, decoded text retained {retained:.0f} KB")
        )
    return rows


def boundary_node_processors(reps: int, sizes=(1, 3, 10, 50, 220)):
    """`all=True` with a node-taking processor: every match is re-parsed on its own.

    The handoff exists because a processor's input contract is an lxml node, and for ONE element a subtree
    parse is far cheaper than the document parse this engine avoids. At N elements it is N subtree parses
    against Parsel's zero — it already has the tree — so the sign of the comparison flips somewhere, and
    the point of sweeping N is to say WHERE instead of "at hundreds"."""
    rows = []

    def count_links(value, page):
        return [len(node.css("a")) for node in value]

    procs = type("Processors", (), {"cards": [count_links]})
    Frost = type("FrostNodes", (FrostPage,),
                 {"Processors": procs, "cards": field("div.card", all=True).as_node()})

    def parsel_getter(self):
        return self.css("div.card")

    parsel_getter.__name__ = parsel_getter.__qualname__ = "cards"
    Parsel = type("ParselNodes", (WebPage,), {"Processors": procs, "cards": wp_field(parsel_getter)})

    for cards in sizes:
        resp = _resp(gen_page(cards))
        _same_answer(
            f"node-processor/{cards}",
            asyncio.run(Frost(response=resp).to_item()),
            asyncio.run(Parsel(response=resp).to_item()),
        )
        f_ms = timeit(lambda: asyncio.run(Frost(response=resp).to_item()), reps)
        p_ms = timeit(lambda: asyncio.run(Parsel(response=resp).to_item()), reps)
        faster = "frostwork" if f_ms < p_ms else "PARSEL"
        rows.append(
            (f"{cards} matches, one subtree parse each", f_ms,
             f"parsel {p_ms:.2f}ms -> {faster} faster ({max(f_ms, p_ms) / min(f_ms, p_ms):.1f}x)")
        )
    return rows


def boundary_subtree_size(reps: int, sizes=(1, 30, 250)):
    """One `.as_node()` field over ONE subtree that grows in SIZE, with the match count held at one.

    The row above varies how many subtrees are parsed; this varies how big one is, because the handoff parses
    the element's whole subtree whether the processor reads it or not. A page object that hands over one large
    container pays for all of it, and that is the shape where a `.as_node()` field is most expensive."""
    rows = []

    def depth_of(value, page):
        # parsel hands a page object a `SelectorList`; `.as_node()` hands one `Selector` (the field is scalar),
        # so the processor takes the first of either — exactly what zyte's `_handle_selectorlist` does
        node = (value[0] if len(value) else None) if isinstance(value, SelectorList) else value
        if node is None:
            return 0
        node = node.root
        n = 0
        while len(node):
            node, n = node[0], n + 1
        return n

    procs = type("Processors", (), {"deep": [depth_of]})
    Frost = type("FrostDeep", (FrostPage,), {"Processors": procs, "deep": field("div.deep").as_node()})

    def parsel_getter(self):
        return self.css("div.deep")

    parsel_getter.__name__ = parsel_getter.__qualname__ = "deep"
    Parsel = type("ParselDeep", (WebPage,), {"Processors": procs, "deep": wp_field(parsel_getter)})

    for kb in sizes:
        row = '<div class="row"><span class="a">text</span><em>more</em><b>bold</b></div>'
        inner = row * max(1, (kb * 1024) // len(row))
        html = f'<html><body><div class="deep"><div class="l0">{inner}</div></div><p>after</p></body></html>'.encode()
        resp = _resp(html)
        _same_answer(
            f"subtree-size/{kb}KB",
            asyncio.run(Frost(response=resp).to_item()),
            asyncio.run(Parsel(response=resp).to_item()),
        )
        f_ms = timeit(lambda: asyncio.run(Frost(response=resp).to_item()), reps)
        p_ms = timeit(lambda: asyncio.run(Parsel(response=resp).to_item()), reps)
        rows.append((f"one match, subtree {len(html) // 1024 or 1} KB", f_ms,
                     f"parsel {p_ms:.2f}ms -> {'frostwork' if f_ms < p_ms else 'PARSEL'} faster "
                     f"({max(f_ms, p_ms) / min(f_ms, p_ms):.1f}x)"))
    return rows


def run_boundaries(reps: int) -> None:
    print(f"  performance boundaries ({reps} reps, median) — one loss, one win, one parity cost\n")
    for title, rows in (
        ("cardinality reaches the plan: a single-valued schema stops scanning", boundary_cardinality(reps)),
        ("cold response: reading resp.encoding decodes the whole body", boundary_cold_response(reps)),
        ("node handoff at scale: one subtree parse per match", boundary_node_processors(reps)),
        ("node handoff by subtree SIZE: the parse is the whole subtree", boundary_subtree_size(reps)),
    ):
        print(f"  {title}")
        for label, ms, note in rows:
            print(f"    {label:<40}{ms:>8.2f}ms   {note}")
        print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", type=int, default=220, help="product cards in the generated page")
    ap.add_argument("--reps", type=int, default=25)
    ap.add_argument("--fields", type=int, nargs="*", default=[1, 4, 8, 12, 16, 20])
    ap.add_argument("--markdown", action="store_true", help="emit the docs/BENCHMARKS.md table")
    ap.add_argument("--boundaries", action="store_true",
                    help="measure the boundary questions (four sweeps) where the main curve does not hold")
    args = ap.parse_args()

    if args.boundaries:
        run_boundaries(args.reps)
        return

    html = gen_page(args.cards)
    kb = len(html) / 1024
    resp = _resp(html)

    # the lxml parse alone: the fixed cost the per-field traversal is measured against
    parse_ms = timeit(lambda: parsel.Selector(body=html, encoding="utf-8"), args.reps)

    rows = []
    for n in args.fields:
        if n > len(POOL):
            raise SystemExit(f"bench-webpoet: only {len(POOL)} selectors in the pool, asked for {n}")
        frost_cls, parsel_cls = build_pair(n)
        assert_parity(frost_cls, parsel_cls, html, n)
        f_ms = timeit(lambda c=frost_cls: asyncio.run(c(response=resp).to_item()), args.reps)
        p_ms = timeit(lambda c=parsel_cls: asyncio.run(c(response=resp).to_item()), args.reps)
        # the same calls on an already-running loop, which is the state a crawler is in. BOTH sides, or the
        # column would compare one page object's best case with the other's worst.
        f_loop = timeit_loop(lambda c=frost_cls: c(response=resp).to_item(), args.reps)
        p_loop = timeit_loop(lambda c=parsel_cls: c(response=resp).to_item(), args.reps)
        rows.append((n, f_ms, p_ms, p_ms / f_ms, f_loop, p_loop))

    if args.markdown:
        print("| fields | `FrostPage` ms | Parsel `WebPage` ms | speedup | same, on a running loop | speedup |")
        print("| --- | --- | --- | --- | --- | --- |")
        for n, f_ms, p_ms, sp, f_loop, p_loop in rows:
            print(f"| {n} | {f_ms:.2f} | {p_ms:.2f} | **{sp:.0f}×** | {f_loop:.2f} / {p_loop:.2f} | "
                  f"**{p_loop / f_loop:.0f}×** |")
        print()
        print(f"Page {kb:.0f} KB; lxml parse alone {parse_ms:.2f} ms "
              f"({100 * parse_ms / rows[-1][2]:.0f}% of Parsel's {rows[-1][0]}-field total).")
        return

    print(f"  page: {kb:.1f} KB, {args.cards} cards | reps: {args.reps} (median)")
    print(f"  lxml parse alone: {parse_ms:.2f} ms  <- the fixed cost; everything above it is traversal\n")
    print(f"  {'fields':>7}  {'FrostPage':>10}  {'Parsel':>10}  {'speedup':>8}   on a running loop")
    for n, f_ms, p_ms, sp, f_loop, p_loop in rows:
        print(f"  {n:>7}  {f_ms:>9.2f}ms  {p_ms:>9.2f}ms  {sp:>7.1f}×   "
              f"{f_loop:>6.2f}ms vs {p_loop:>7.2f}ms  ({p_loop / f_loop:.1f}×)")
    print(
        f"\n  Parity verified on every row before timing. The parse is {parse_ms:.2f} ms of Parsel's "
        f"{rows[-1][2]:.2f} ms at {rows[-1][0]} fields ({100 * parse_ms / rows[-1][2]:.0f}%), so the gap "
        f"is per-field traversal and grows with field count."
    )


if __name__ == "__main__":
    main()
