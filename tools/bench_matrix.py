"""
Benchmark matrix for the Frostwork engine: page types x selector counts, engine vs Parsel.

Engine: measured by the Rust `bench` binary (engine-only, no IPC in the timed loop). Parsel: timed
in-process with its real model (parse once, then one .css() per field). Both run the SAME selectors
on the SAME page, so each cell is a fair "extract N fields from this page" comparison.

Usage:  .venv/bin/python tools/bench_matrix.py
"""
from __future__ import annotations

import os
import subprocess
import time
import argparse

from parsel import Selector as PS

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "target", "release", "bench")
import tempfile
TMP = tempfile.mkdtemp(prefix="frostwork-bench-")

# ---- realistic selector pool (product/article shaped); prefixes give the selector-count sweep ----
POOL = [
    "h1::text", "a::attr(href)", ".price::text", ".title::text", "h2::text", "p::text",
    "img::attr(src)", "a::text", ".product .price::text", ".desc::text", "span::text",
    "h3::text", ".product a::attr(href)", "li::text", "td::text", "div ::text",
    "[class]::text", ".product ::text", "meta::attr(content)", ".product .title::text",
    "a[href^='/']::attr(href)", "h4::text", "strong::text", "em::text", ".product img::attr(src)",
    ".title a::text", "table td::text", "ul li::text", "p a::attr(href)", "nav a::attr(href)",
    "footer ::text", "header h1::text",
]
COUNTS = [1, 4, 8, 16, 26, 32]
SMOKE = False

# Deferred-TAIL selectors: their values come from a re-scan of each winner's span rather than the
# streaming pass (see docs/DESIGN.md), so a tail costs an extra pass over the matched subtrees that the
# pool selectors' shared scan does not. Tails with the same deferred PREFIX share one re-scan; the rest
# still cost per field, so the count matters (figures: docs/BENCHMARKS.md). Kept as a separate pool so
# that stays visible instead of being invisible in the headline numbers, and MIXED on purpose — three
# selectors here share `div:has(a)` and the others do not, which is the shape a real schema has.
TAIL_POOL = [
    "div:has(a) a::attr(href)", "div:has(a) p::text", ".product:has(img) .price::text",
    ".product:has(a) .title::text", "div:has(p) a::text", ".product:has(.price) img::attr(src)",
    "li:last-child a::attr(href)", "div:has(a) ::text",
]

# CLASS-LED, high field count — where per-(element, selector) work dominates: every class-led compound
# asks the element for its `class=` and looks for its own token. The tag-led POOL above barely exercises
# that, so a change to class/id comparison is invisible there. Most fields deliberately DON'T match,
# since a real schema's misses still cost a test per element.
CLASS_POOL = [
    ".price::text", ".title::text", ".desc::text", ".thumb::attr(src)", ".link::attr(href)",
    ".card .price::text", ".card .title::text", ".product-card .desc::text",
    ".badge::text", ".rating::text", ".sku::text", ".stock-label::text",
    ".card .badge .value::text", ".breadcrumb a::attr(href)", ".pagination .next::attr(href)",
    ".facet .label::text", ".swatch::attr(data-color)", ".promo .text-xl::text",
    ".review .author::text", ".review .body::text", ".seller-name::text", ".shipping-note::text",
    ".card .price-was::text", ".card .price-now::text", ".gallery img::attr(src)",
    ".spec-table .name::text", ".spec-table .value::text", ".qty-input::attr(value)",
    ".wishlist::attr(href)", ".compare::attr(href)", ".variant .title::text", ".tag-list a::text",
]
CLASS_COUNTS = [1, 2, 4, 8, 16, 32]

# DESCENDANT-LED: every selector is 2-3 compounds, what a real page object mostly contains
# (`.product-info .price::text`). CLASS_POOL is subject-led at its head, so it cannot see a change to the
# ANCESTOR walk; this pool can. Most selectors deliberately do not match, which is the case that costs
# the walk the most.
DESC_POOL = [
    ".card .price::text", ".card .title::text", ".product-card .desc::text",
    ".grid .card .link::attr(href)", ".listing .thumb::attr(src)", ".panel .badge::text",
    ".sidebar .facet .label::text", ".review .author::text", ".review .body::text",
    ".spec-table .name::text", ".spec-table .value::text", ".gallery img::attr(src)",
    ".breadcrumb a::attr(href)", ".pagination .next::attr(href)", ".promo .text-xl::text",
    ".header .nav a::text", ".footer .links a::attr(href)", ".modal .close::attr(title)",
    ".tabs .tab .label::text", ".accordion .item .head::text", ".rail .widget .title::text",
    ".hero .cta::attr(href)", ".filters .chip::text", ".compare .row .cell::text",
    ".variant .swatch::attr(data-color)", ".stock .label::text", ".shipping .note::text",
    ".seller .name::text", ".rating .stars::attr(title)", ".qty .input::attr(value)",
    ".wishlist .btn::attr(href)", ".related .card .title::text",
]


# ATTRIBUTE-PREDICATE-LED. The signature filter summarizes only tag/id/class, so a schema built on
# `[data-x=y]` and `a[href^=/p]` gets nothing from it and pays a materialized-attribute lookup per
# (element, predicate). Neither other pool contains an attribute predicate.
ATTR_POOL = [
    "[data-sku]::attr(data-sku)", "a[href^='/p/']::attr(href)", "[data-qty]::text",
    "img[src$='.jpg']::attr(src)", "[data-variant]::text", "[data-track='view']::text",
    "a[href*='promo']::attr(href)", "[data-price]::attr(data-price)", "[data-stock]::text",
    "[data-color]::attr(data-color)", "[data-size]::text", "[data-brand]::attr(data-brand)",
    "[data-rating]::text", "[data-reviews]::text", "[data-seller]::text", "[data-ship]::text",
    "input[value]::attr(value)", "[data-cat]::text", "[data-sub]::text", "[data-id]::attr(data-id)",
    "a[href$='.html']::attr(href)", "[data-img]::attr(data-img)", "[data-alt]::text",
    "[data-tag]::text", "[data-promo]::text", "[data-badge]::text", "[data-new]::text",
    "[data-sale]::text", "[data-eta]::text", "[data-store]::text", "[data-lang]::text",
    "[data-cur]::text",
]

# ---- page generators (deterministic, ~size bytes) ----
def article(size):
    para = ("<p>" + "the quick brown fox jumps over the lazy dog " * 8 +
            '<a href="/link">more</a></p>')
    block = f'<h2 class="title">Section</h2>{para}{para}'
    body, n = [], 0
    while n < size:
        body.append(block); n += len(block)
    return f'<!DOCTYPE html><html><head><title>Article</title></head><body><article>{"".join(body)}</article></body></html>'.encode()


def product_listing(size):
    card = ('<div class="product"><h3 class="title">Widget Pro</h3>'
            '<span class="price">$19.99</span><a href="/p/123">view</a>'
            '<img src="/img/w.jpg" alt="w"><p class="desc">A useful widget for many tasks.</p></div>')
    body, n = [], 0
    while n < size:
        body.append(card); n += len(card)
    return f'<!DOCTYPE html><html><head><title>Shop</title></head><body><div class="grid">{"".join(body)}</div></body></html>'.encode()


def table(size):
    row = "<tr>" + "".join(f'<td class="c">cell {i}</td>' for i in range(5)) + "</tr>"
    body, n = [], 0
    while n < size:
        body.append(row); n += len(row)
    return f'<!DOCTYPE html><html><head><title>Data</title></head><body><table><tbody>{"".join(body)}</tbody></table></body></html>'.encode()


def class_heavy(size):
    """Utility-CSS markup: every element carries a handful of class tokens (the shape Tailwind-style
    sites produce), so a class-led selector's token search is real work rather than a 1-token hit."""
    card = ('<div class="card product-card flex flex-col rounded-lg shadow-sm p-4 mb-2">'
            '<h3 class="title text-lg font-semibold leading-tight truncate">Widget Pro</h3>'
            '<span class="price text-xl font-bold text-green-700">$19.99</span>'
            '<a class="link btn btn-primary inline-block mt-2" href="/p/123">view</a>'
            '<img class="thumb rounded object-cover w-full" src="/img/w.jpg" alt="w">'
            '<p class="desc text-sm text-gray-600 leading-snug">A useful widget for many tasks.</p>'
            '</div>')
    body, n = [], 0
    while n < size:
        body.append(card); n += len(card)
    return ('<!DOCTYPE html><html><head><title>Shop</title></head>'
            f'<body><div class="grid grid-cols-3 gap-4 px-6">{"".join(body)}</div>'
            '</body></html>').encode()


def attr_heavy(size):
    """Elements carrying several attributes each, most of which no selector references -- the shape a
    component framework emits. Every one of them still meets `is_interesting` on the way in, so this is
    where attribute MATERIALIZATION cost shows, separately from attribute matching."""
    card = ('<div class="card" data-testid="card" data-index="7" role="listitem" aria-label="w">'
            '<a class="link" href="/p/123" title="Widget" rel="nofollow" data-ga="click">Widget</a>'
            '<img class="thumb" src="/img/w.jpg" alt="w" loading="lazy" width="120" height="90">'
            '<span class="price" data-currency="USD" itemprop="price" content="19.99">$19.99</span>'
            '<button class="add" type="button" data-action="add" aria-pressed="false">Add</button>'
            '</div>')
    body, n = [], 0
    while n < size:
        body.append(card); n += len(card)
    return ('<!DOCTYPE html><html><head><title>Shop</title></head>'
            f'<body><div class="grid">{"".join(body)}</div></body></html>').encode()


def deep_nested(size):
    # nested divs with a leaf; stresses ancestor-chain matching
    depth = 20
    leaf = '<div class="leaf"><span>x</span><a href="/l">y</a></div>'
    unit = "<div>" * depth + leaf + "</div>" * depth
    body, n = [], 0
    while n < size:
        body.append(unit); n += len(unit)
    return f'<!DOCTYPE html><html><body>{"".join(body)}</body></html>'.encode()


def engine_bench(html_bytes, sels):
    """-> (us_per_page, mb_per_s, vals_per_page) via the Rust bench binary."""
    path = os.path.join(TMP, "_bench_page.html")
    open(path, "wb").write(html_bytes)
    env = os.environ.copy()
    if SMOKE:
        env["FROSTWORK_BENCH_ITERS"] = "200"
    p = subprocess.run([BIN, path], input="\n".join(sels).encode(), capture_output=True, env=env)
    parts = p.stderr.decode().strip().split("\t")
    return float(parts[2]), float(parts[4]), int(parts[5])


def engine_bench_grouped(html_bytes, container, subs):
    """-> (us_per_page, mb_per_s, vals_per_page) for one `Many` group (bench's `G <container>` mode)."""
    path = os.path.join(TMP, "_bench_page.html")
    open(path, "wb").write(html_bytes)
    stdin = ("G " + container + "\n" + "\n".join(subs)).encode()
    env = os.environ.copy()
    if SMOKE:
        env["FROSTWORK_BENCH_ITERS"] = "200"
    p = subprocess.run([BIN, path], input=stdin, capture_output=True, env=env)
    parts = p.stderr.decode().strip().split("\t")
    return float(parts[2]), float(parts[4]), int(parts[5])


def parsel_bench_grouped(html_bytes, container, subs):
    """-> us_per_page for Parsel's per-container loop: parse once, then for each container node run
    each sub-selector scoped to it (the exact model Frostwork's single-pass grouping replaces)."""
    for _ in range(50):
        s = PS(body=html_bytes, encoding="utf-8")
        for c in s.css(container):
            for q in subs:
                c.css(q).getall()
    iters = max(80, min(1500, int(4e8 / max(len(html_bytes), 1))))
    t = time.perf_counter()
    for _ in range(iters):
        s = PS(body=html_bytes, encoding="utf-8")
        for c in s.css(container):
            for q in subs:
                c.css(q).getall()
    return (time.perf_counter() - t) / iters * 1e6


def parsel_bench(html_bytes, sels):
    """-> us_per_page for Parsel's parse-once + query-per-field model."""
    for _ in range(5 if SMOKE else 50):
        s = PS(body=html_bytes, encoding="utf-8")
        for q in sels:
            s.css(q).getall()
    # scale iters to keep each cell quick but stable
    iters = 20 if SMOKE else max(80, min(1500, int(4e8 / max(len(html_bytes), 1))))
    t = time.perf_counter()
    for _ in range(iters):
        s = PS(body=html_bytes, encoding="utf-8")
        for q in sels:
            s.css(q).getall()
    return (time.perf_counter() - t) / iters * 1e6


# Rows accumulated for `--markdown`, so docs/BENCHMARKS.md is regenerated from a run rather than
# transcribed by hand.
MD_ROWS = []


def emit_markdown():
    """The `page type x selector count` table in docs/BENCHMARKS.md's exact shape."""
    print("\n\n<!-- BEGIN generated: tools/bench_matrix.py --markdown -->")
    print("| page (≈196 KB) | sels | engine µs | engine MB/s | Parsel µs | speedup |")
    print("| --- | --- | --- | --- | --- | --- |")
    last = None
    for name, c, eus, embs, pus in MD_ROWS:
        label = f"**{name.replace('_', '&#95;')}**" if name != last else ""
        last = name
        print(f"| {label} | {c} | {eus:.0f} | {embs:.0f} | {pus:.0f} | {pus / eus:.1f}× |")
    print("<!-- END generated -->")


def run_table(name, html_bytes, counts=COUNTS, pool=None):
    pool = pool or POOL
    kb = len(html_bytes) / 1024
    print(f"\n### {name}  ({kb:.0f} KB)")
    print(f"  {'sels':>4} | {'engine µs':>10} {'MB/s':>7} {'vals':>6} | {'parsel µs':>10} | {'speedup':>7}")
    print("  " + "-" * 60)
    for c in counts:
        sels = pool[:c]
        eus, embs, vals = engine_bench(html_bytes, sels)
        pus = parsel_bench(html_bytes, sels)
        MD_ROWS.append((name, c, eus, embs, pus))
        print(f"  {c:>4} | {eus:>10.1f} {embs:>7.0f} {vals:>6} | {pus:>10.1f} | {pus/eus:>6.1f}x")


def main():
    global SMOKE
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="quick article/deep check at 8 and 32 selectors")
    ap.add_argument("--markdown", action="store_true",
                    help="also emit the docs/BENCHMARKS.md table, so it is regenerated not transcribed")
    args = ap.parse_args()
    SMOKE = args.smoke
    med = 200_000
    pages = (
        [("article (text-heavy)", article(med)), ("deep-nested", deep_nested(med))]
        if args.smoke
        else [
            ("article (text-heavy)", article(med)),
            ("product_listing", product_listing(med)),
            ("table-heavy", table(med)),
            ("deep-nested", deep_nested(med)),
        ]
    )
    # a real corpus page if present
    import glob
    corpus = sorted(glob.glob(os.path.join(HERE, "..", "fixtures", "*.html")))
    if corpus:
        pages.append(("fixtures", open(corpus[len(corpus) // 2], "rb").read()))

    print("=" * 64)
    print("PAGE TYPE x SELECTOR COUNT  (engine vs Parsel, µs/page)")
    print("=" * 64)
    for name, html in pages:
        run_table(name, html, [8, 32] if args.smoke else COUNTS)

    if args.markdown:
        emit_markdown()
    if args.smoke:
        return

    # grouped (Many/One): one `.product` container × {1,3,5} subs vs Parsel's per-container loop.
    # This measures the single-pass per-instance × per-sub on-demand evaluation the grouped path adds.
    print("\n\n### grouped — product_listing, .product container × N subs (vs Parsel per-container loop)")
    print(f"  {'subs':>4} | {'engine µs':>10} {'MB/s':>7} {'vals':>6} | {'parsel µs':>10} | {'speedup':>7}")
    print("  " + "-" * 60)
    g_html = product_listing(med)
    g_subs = ["h3::text", ".price::text", "a::attr(href)", "img::attr(src)", ".desc::text"]
    for n in (1, 3, 5):
        subs = g_subs[:n]
        eus, embs, vals = engine_bench_grouped(g_html, ".product", subs)
        pus = parsel_bench_grouped(g_html, ".product", subs)
        print(f"  {n:>4} | {eus:>10.1f} {embs:>7.0f} {vals:>6} | {pus:>10.1f} | {pus/eus:>6.1f}x")

    # Deferred-TAIL fields vs the same count of plain fields. Tails re-scan each winner's span instead of
    # streaming. Tails that share a deferred prefix share one sub-schema and so one re-scan; what is left
    # still grows with FIELD COUNT, which is what this table makes visible. Both halves are in the pool,
    # so a change to either shows here — merging every selector onto one prefix would report the merge as
    # a bigger win than a real schema gets.
    print("\n\n### deferred tails — product_listing, N tail fields vs N plain fields")
    print(f"  {'fields':>6} | {'tail µs':>9} | {'plain µs':>9} | {'tail/plain':>10}")
    print("  " + "-" * 46)
    t_html = product_listing(med)
    for n in (1, 2, 4, 8):
        tus, _, _ = engine_bench(t_html, TAIL_POOL[:n])
        pus_, _, _ = engine_bench(t_html, POOL[:n])
        print(f"  {n:>6} | {tus:>9.1f} | {pus_:>9.1f} | {tus / pus_:>9.1f}x")

    # size sweep on the product listing (fixed 8 selectors)
    print("\n\n### size sweep — product_listing, 8 selectors")
    print(f"  {'size':>7} | {'engine µs':>10} {'MB/s':>7} | {'parsel µs':>10} | {'speedup':>7}")
    print("  " + "-" * 52)
    for sz in (20_000, 200_000, 1_000_000):
        html = product_listing(sz)
        sels = POOL[:8]
        eus, embs, _ = engine_bench(html, sels)
        pus = parsel_bench(html, sels)
        print(f"  {len(html)//1024:>5}KB | {eus:>10.1f} {embs:>7.0f} | {pus:>10.1f} | {pus/eus:>6.1f}x")


if __name__ == "__main__":
    main()
