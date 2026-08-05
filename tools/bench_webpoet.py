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
      .venv/bin/python tools/bench_webpoet.py --markdown     # the table for docs/BENCHMARKS.md

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", type=int, default=220, help="product cards in the generated page")
    ap.add_argument("--reps", type=int, default=25)
    ap.add_argument("--fields", type=int, nargs="*", default=[1, 4, 8, 12, 16, 20])
    ap.add_argument("--markdown", action="store_true", help="emit the docs/BENCHMARKS.md table")
    args = ap.parse_args()

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
        rows.append((n, f_ms, p_ms, p_ms / f_ms))

    if args.markdown:
        print(f"| fields | `FrostPage` ms | Parsel `WebPage` ms | speedup |")
        print("| --- | --- | --- | --- |")
        for n, f_ms, p_ms, sp in rows:
            print(f"| {n} | {f_ms:.2f} | {p_ms:.2f} | **{sp:.0f}×** |")
        print()
        print(f"Page {kb:.0f} KB; lxml parse alone {parse_ms:.2f} ms "
              f"({100 * parse_ms / rows[-1][2]:.0f}% of Parsel's {rows[-1][0]}-field total).")
        return

    print(f"  page: {kb:.1f} KB, {args.cards} cards | reps: {args.reps} (median)")
    print(f"  lxml parse alone: {parse_ms:.2f} ms  <- the fixed cost; everything above it is traversal\n")
    print(f"  {'fields':>7}  {'FrostPage':>10}  {'Parsel':>10}  {'speedup':>8}")
    for n, f_ms, p_ms, sp in rows:
        print(f"  {n:>7}  {f_ms:>9.2f}ms  {p_ms:>9.2f}ms  {sp:>7.1f}×")
    print(
        f"\n  Parity verified on every row before timing. The parse is {parse_ms:.2f} ms of Parsel's "
        f"{rows[-1][2]:.2f} ms at {rows[-1][0]} fields ({100 * parse_ms / rows[-1][2]:.0f}%), so the gap "
        f"is per-field traversal and grows with field count."
    )


if __name__ == "__main__":
    main()
