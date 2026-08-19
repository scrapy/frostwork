"""
tools/corpus_fetch.py — build a REAL-PAGE corpus for `bench_corpus.py --gate`.

Why this exists. `make gate` only ever sees pages a generator wrote, and a generator reproduces the
malformations its author thought of. Both bugs that reached a user — the `dd`/`dt` same-tag close and the
dropped-end-tag text split — were found on real doc-generator output while the generated gate read 100%.
`tests/corpus` closes part of that gap but is self-authored, so it encodes only bugs already known.

This fetches real pages into the layout `bench_corpus.py` expects and derives a selector basket from each
page's own markup, so the gate has real bytes to diff against Parsel:

    <outdir>/<page-object>/selectors.json     {field: css_or_xpath}
    <outdir>/<page-object>/pages/*.html       raw response bytes, undecoded

Nothing is committed: the default output is `fixtures/realweb`, and `/fixtures/` is gitignored. Pages are
third-party content — fetch them locally, diff them, do not vendor them (licensing and size).

    .venv/bin/python tools/corpus_fetch.py                       # default URL list -> fixtures/realweb
    .venv/bin/python tools/corpus_fetch.py --urls my-urls.txt --out fixtures/mine
    .venv/bin/python tools/bench_corpus.py fixtures/realweb --gate

The URL list is a starting point, not a corpus. A real crawl (Zyte has them) is still the thing this
cannot replace — it is one page per site, so it samples site *variety* rather than the long tail of any
one site's templates. Point `--urls` at a file to use your own.

RAW BYTES MATTER: the file is written exactly as served, no decoding and no re-encoding, because the
encoding prescan is part of what the gate measures. `Content-Encoding: gzip` is decompressed (that is
transport, not document encoding); nothing else is touched.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zlib
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Doc-generator output first: that is where both escaped bugs were found, and every generator (Sphinx,
# MkDocs, Doxygen, Javadoc) emits its own dialect of omitted end tags. Then a scraping sandbox, a
# reference wiki, and a couple of hand-written pages for template variety.
DEFAULT_URLS = [
    # Sphinx
    "https://docs.python.org/3/library/stdtypes.html",
    "https://docs.python.org/3/reference/datamodel.html",
    "https://numpy.org/doc/stable/reference/generated/numpy.ndarray.html",
    "https://docs.scrapy.org/en/latest/topics/selectors.html",
    # MkDocs / material
    "https://www.mkdocs.org/user-guide/configuration/",
    # Doxygen
    "https://docs.opencv.org/4.x/d3/d63/classcv_1_1Mat.html",
    # Javadoc
    "https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/List.html",
    # scraping sandboxes (explicitly published for this)
    "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
    "https://books.toscrape.com/catalogue/category/books/mystery_3/index.html",
    "https://quotes.toscrape.com/",
    "https://quotes.toscrape.com/tableful/",
    # reference / long tables
    "https://en.wikipedia.org/wiki/Comparison_of_HTML_parsers",
    "https://en.wikipedia.org/wiki/List_of_HTTP_status_codes",
    # hand-written and generated mixes
    "https://developer.mozilla.org/en-US/docs/Web/HTML/Element/table",
    "https://www.w3.org/TR/html401/struct/lists.html",
    # texinfo / troff -> HTML. These matter out of proportion to their traffic: they are
    # `<dl>`-heavy, they omit end tags freely, and they are the shape that produced the `dd`/`dt`
    # and dropped-end-tag bugs.
    "https://www.gnu.org/software/bash/manual/bash.html",
    "https://www.gnu.org/software/make/manual/make.html",
    "https://man7.org/linux/man-pages/man2/open.2.html",
    # more generators, each with its own dialect of omitted end tags
    "https://doc.rust-lang.org/std/vec/struct.Vec.html",            # rustdoc
    "https://pkg.go.dev/net/http",                                  # godoc
    "https://requests.readthedocs.io/en/latest/api/",               # Sphinx RTD theme
    "https://docusaurus.io/docs",                                   # Docusaurus
    "https://gohugo.io/documentation/",                             # Hugo
    "https://jekyllrb.com/docs/",                                   # Jekyll
    "https://docs.asciidoctor.org/asciidoc/latest/",                # Asciidoctor
    "https://doxygen.nl/manual/config.html",                        # Doxygen (different vintage)
    "https://docs.oracle.com/javase/8/docs/api/java/util/List.html",  # older Javadoc
    "https://www.php.net/manual/en/function.array-map.php",         # PHP docs
    "https://peps.python.org/pep-0008/",                            # PEP toolchain
    "https://webscraper.io/test-sites/e-commerce/allinone",         # scraping sandbox
    "https://httpbin.org/html",
]

UA = "frostwork-corpus-fetch/0.1 (+https://github.com/shaneaevans/frostwork; local parity testing)"


def fetch(url: str, timeout: float = 20.0) -> bytes:
    """Return the response body as RAW BYTES (transport decompression only)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip, deflate",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = (r.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def page_object_name(url: str) -> str:
    host = re.sub(r"^https?://", "", url).split("/")[0]
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")


def derive_selectors(pages: list[bytes], want: int = 24) -> dict[str, str]:
    """Derive a scraper-shaped selector basket from the pages themselves.

    A real page object's selectors are unknowable from outside, and inventing generic ones
    (`h1::text`) produces mostly-empty columns that prove nothing. So: take the classes and ids the
    pages actually use, most frequent first, and build the shapes a scraper writes over them — text,
    attribute, descendant text, and a bare-element (outer-HTML) query. Every candidate is then filtered
    through BOTH engines' compilers, since `bench_corpus` runs strict and needs a supported selector on
    the Frostwork side and a compilable one on the Parsel side.
    """
    import frostwork
    import lxml.html

    classes: Counter[str] = Counter()
    ids: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    for raw in pages:
        try:
            doc = lxml.html.fromstring(raw)
        except Exception:
            continue
        for el in doc.iter():
            if not isinstance(el.tag, str):
                continue
            tags[el.tag] += 1
            for c in (el.get("class") or "").split():
                # a CSS identifier only: escapes and non-identifier characters are a separate surface
                # (covered by sel_fuzz), and here they would just drop out of both engines
                if re.fullmatch(r"-?[A-Za-z_][-\w]*", c):
                    classes[c] += 1
            i = el.get("id")
            if i and re.fullmatch(r"-?[A-Za-z_][-\w]*", i):
                ids[i] += 1

    cand: list[str] = [
        "title::text", "h1::text", "h2::text",
        'meta[name="description"]::attr(content)',
        "a::attr(href)", "img::attr(src)",
        "table td::text", "table th::text", "dl dt::text", "dl dd::text",
        "ul li::text", "ol li::text", "pre::text", "code::text",
        "div p::text", "//table//td/text()", "//dl/dd//text()",
    ]
    for c, _ in classes.most_common(18):
        cand += [f".{c}::text", f".{c} a::attr(href)", f"div.{c} p::text"]
    for i, _ in ids.most_common(6):
        cand += [f"#{i}::text", f"#{i} li::text"]
    for t, _ in tags.most_common(12):
        if t not in ("html", "head", "body"):
            cand.append(f"{t}::text")

    # keep only what BOTH engines will run, and de-duplicate while preserving the priority order
    import parsel
    ok: list[str] = []
    seen = set()
    for q in cand:
        if q in seen:
            continue
        seen.add(q)
        if not frostwork.check([q]).fields[0].supported:
            continue
        try:
            sel = parsel.Selector(text="<html></html>")
            (sel.xpath if q.startswith(("/", "./", "(")) else sel.css)(q)
        except Exception:
            continue
        ok.append(q)
        if len(ok) >= want:
            break
    return {f"f{n}": q for n, q in enumerate(ok)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--urls", help="file with one URL per line (default: the built-in list)")
    ap.add_argument("--out", default="fixtures/realweb", help="output corpus dir (gitignored)")
    ap.add_argument("--timeout", type=float, default=20.0)
    args = ap.parse_args()

    urls = DEFAULT_URLS
    if args.urls:
        with open(args.urls) as fh:
            urls = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]

    out = os.path.abspath(args.out)
    by_obj: dict[str, list[tuple[str, bytes]]] = {}
    failed: list[tuple[str, str]] = []
    for url in urls:
        try:
            raw = fetch(url, args.timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
            failed.append((url, f"{type(e).__name__}: {e}"))
            print(f"  SKIP {url}\n       {type(e).__name__}: {e}")
            continue
        by_obj.setdefault(page_object_name(url), []).append((url, raw))
        print(f"  {len(raw):>9,}B  {url}")

    if not by_obj:
        print("\nnothing fetched — no corpus written")
        return 1

    manifest = {}
    for obj, items in sorted(by_obj.items()):
        pdir = os.path.join(out, obj, "pages")
        os.makedirs(pdir, exist_ok=True)
        for n, (url, raw) in enumerate(items):
            with open(os.path.join(pdir, f"p{n}.html"), "wb") as fh:
                fh.write(raw)
        sels = derive_selectors([raw for _u, raw in items])
        with open(os.path.join(out, obj, "selectors.json"), "w") as fh:
            json.dump(sels, fh, indent=2)
        manifest[obj] = {"urls": [u for u, _r in items], "selectors": len(sels)}
        print(f"\n{obj}: {len(items)} page(s), {len(sels)} selectors")

    # provenance, so a divergence can be traced back to a URL (and re-fetched to check it still repros)
    with open(os.path.join(out, "MANIFEST.json"), "w") as fh:
        json.dump({"urls_failed": dict(failed), "page_objects": manifest}, fh, indent=2)

    print(f"\ncorpus written to {out} ({len(by_obj)} page objects)")
    print(f"next: .venv/bin/python tools/bench_corpus.py {args.out} --gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
