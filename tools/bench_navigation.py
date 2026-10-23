# `frostwork.navigation` against what Scrapy runs today for the base URL and meta refresh (w3lib's
# `get_base_url()` + `get_meta_refresh()` on the body, uncapped and with Scrapy's 16 KiB cap), on
# page-sized documents, plus what reading both costs on top of a `Page` pass that extracts fields anyway.
# Every row compares both answers before timing them and marks the rows where they differ.
#
# Run:  .venv/bin/python tools/bench_navigation.py [--size BYTES] [--reps N]
#
# Build the extension RELEASE-first (`maturin develop --release`).

from __future__ import annotations

import argparse
import timeit
from typing import Callable

from w3lib.html import get_base_url, get_meta_refresh

from frostwork import Page, navigation

URL = "https://example.com/p"
HEAD = '<base href="/shop/"><meta http-equiv="refresh" content="5;url=sale">'
ROW = '<div class="item"><a href="/p/1">Mug</a><span class="price">$9</span><img src="/i.png"></div>\n'
FIELDS = Page(strict=False)
for name, selector in [
    ("title", "title::text"), ("h1", "h1::text"), ("links", "a::attr(href)"), ("price", ".price::text"),
    ("images", "img::attr(src)"), ("description", "meta[name=description]::attr(content)"),
    ("canonical", "link[rel=canonical]::attr(href)"), ("ld", "script[type='application/ld+json']::text"),
]:
    FIELDS.field_all(name, selector)


def page(size: int, head: str = "", tail: str = "") -> bytes:
    rows = ROW * (size // len(ROW))
    start = f"<!DOCTYPE html><html><head><title>t</title>{head}</head>"
    return f"{start}<body><h1>H</h1>\n{rows}{tail}</body></html>".encode()


def w3lib(body: bytes, max_scan: int | None) -> tuple[str, tuple]:
    base = get_base_url(body, URL, "utf-8", max_scan=max_scan)
    return base, get_meta_refresh(body, base, "utf-8", max_scan=max_scan)


def best(stmt: Callable[[], object], reps: int) -> float:
    return min(timeit.repeat(stmt, number=1, repeat=reps))


def row(name: str, ours: Callable[[], object], theirs: Callable[[], object], reps: int, same: bool) -> None:
    a, b = best(ours, reps), best(theirs, reps)
    note = "" if same else "  (answers differ)"
    print(f"{name:<44} {a * 1e3:9.3f} ms {b * 1e3:9.3f} ms {a / b:7.2f}{note}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=300_000)
    parser.add_argument("--reps", type=int, default=20)
    args = parser.parse_args()
    bodies = {
        "both in the head": page(args.size, head=HEAD),
        "neither": page(args.size),
        "refresh at the end": page(args.size, tail='<meta http-equiv="refresh" content="5;url=/sale">'),
    }
    print(f"{'':<44} {'frostwork':>12} {'w3lib':>12} {'ratio':>7}")
    for label, body in bodies.items():
        nav = navigation(body, URL)
        # WHATWG serializes an empty path as "/", where w3lib keeps the URL as written.
        ours = (nav.base_url.rstrip("/"), nav.meta_refresh or (None, None))
        for max_scan in (None, 16384):
            theirs = w3lib(body, max_scan)
            same = (theirs[0].rstrip("/"), theirs[1]) == ours
            cap = "no cap" if max_scan is None else f"w3lib cap {max_scan}"
            ours_fn, theirs_fn = (lambda: navigation(body, URL)), (lambda: w3lib(body, max_scan))
            row(f"{label}, {cap}", ours_fn, theirs_fn, args.reps, same)
    body = bodies["both in the head"]
    same = FIELDS.extract_with_navigation(body, URL)[0].to_dict() == FIELDS.extract(body).to_dict()
    row(
        "8 fields + navigation vs 8 fields",
        lambda: FIELDS.extract_with_navigation(body, URL),
        lambda: FIELDS.extract(body),
        args.reps,
        same,
    )


if __name__ == "__main__":
    main()
