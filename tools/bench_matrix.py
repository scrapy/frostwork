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


def run_table(name, html_bytes, counts=COUNTS):
    kb = len(html_bytes) / 1024
    print(f"\n### {name}  ({kb:.0f} KB)")
    print(f"  {'sels':>4} | {'engine µs':>10} {'MB/s':>7} {'vals':>6} | {'parsel µs':>10} | {'speedup':>7}")
    print("  " + "-" * 60)
    for c in counts:
        sels = POOL[:c]
        eus, embs, vals = engine_bench(html_bytes, sels)
        pus = parsel_bench(html_bytes, sels)
        print(f"  {c:>4} | {eus:>10.1f} {embs:>7.0f} {vals:>6} | {pus:>10.1f} | {pus/eus:>6.1f}x")


def main():
    global SMOKE
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="quick article/deep check at 8 and 32 selectors")
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
