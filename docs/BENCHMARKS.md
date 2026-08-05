# Frostwork benchmark matrix

Engine-only throughput (Rust `bench` binary, no IPC in the timed loop) vs Parsel (its real model:
parse once, then one `.css()` per field). Both run the **same** selectors on the **same** page, and
every page/selector pair is **byte-identical between the two** (verified: 32/32 AGREE per page), so
each cell is a fair "extract N fields from this page" comparison. Single machine (Apple arm64),
warm; indicative, not a controlled benchmark. Selector pool is a realistic product/article mix;
counts are prefixes of it. Reproduce: `.venv/bin/python tools/bench_matrix.py`.

## Page type × selector count (µs/page)

| page (≈196 KB) | sels | engine µs | engine MB/s | Parsel µs | speedup |
| --- | --- | --- | --- | --- | --- |
| **article** (text-heavy) | 1 | 58 | 3451 | 560 | 9.6× |
|  | 4 | 136 | 1475 | 2706 | 19.9× |
|  | 8 | 270 | 745 | 5324 | 19.8× |
|  | 16 | 358 | 560 | 6427 | 17.9× |
|  | 26 | 479 | 419 | 9686 | 20.2× |
|  | 32 | 556 | 361 | 11212 | 20.2× |
| **product&#95;listing** | 1 | 256 | 782 | 2619 | 10.2× |
|  | 4 | 747 | 268 | 15313 | 20.5× |
|  | 8 | 1068 | 188 | 22123 | 20.7× |
|  | 16 | 1838 | 109 | 80254 | 43.7× |
|  | 26 | 2671 | 75 | 144615 | 54.1× |
|  | 32 | 2924 | 68 | 144178 | 49.3× |
| **table-heavy** | 1 | 360 | 556 | 3192 | 8.9× |
|  | 8 | 1029 | 195 | 13432 | 13.1× |
|  | 16 | 1814 | 110 | 44571 | 24.6× |
|  | 32 | 3550 | 56 | 98915 | 27.9× |
| **deep-nested** (20-deep) | 1 | 619 | 324 | 3211 | 5.2× |
|  | 8 | 1296 | 154 | 12560 | 9.7× |
|  | 16 | 2252 | 89 | 41175 | 18.3× |
|  | 32 | 3861 | 52 | 57569 | 14.9× |
| **corpus** (real-shaped, 567 KB) | 1 | 175 | 3322 | 2070 | 11.8× |
|  | 8 | 712 | 815 | 8661 | 12.2× |
|  | 16 | 1346 | 431 | 67613 | 50.2× |
|  | 26 | 1880 | 309 | 83608 | 44.5× |
|  | 32 | 2094 | 277 | 85213 | 40.7× |

## Size sweep (product_listing, 8 selectors)

| size | engine µs | engine MB/s | Parsel µs | speedup |
| --- | --- | --- | --- | --- |
| 19 KB | 111 | 181 | 2176 | 19.6× |
| 195 KB | 1068 | 188 | 21545 | 20.2× |
| 976 KB | 5285 | 189 | 110631 | 20.9× |

## Grouped (Many/One) — `.product` container × N sub-fields

One `Many` group over the product listing (195 KB, ~50 `.product` cards), engine (`bench`'s
`G <container>` mode) vs **Parsel's real per-container loop** (`for c in sel.css(".product"): [c.css(sub)…]`)
— the exact model the single-pass grouping replaces. Reproduce: `.venv/bin/python tools/bench_matrix.py`.

| subs | engine µs | engine MB/s | vals/page | Parsel µs | speedup |
| --- | --- | --- | --- | --- | --- |
| 1 | 735 | 273 | 1006 | 14160 | 19.3× |
| 3 | 1064 | 188 | 3018 | 34401 | 32.3× |
| 5 | 1363 | 147 | 5030 | 55125 | 40.4× |

The engine's grouped cost grows **sub-linearly** in sub count (735 → 1064 → 1363 µs for 1 → 3 → 5
subs — the pass and per-element bookkeeping are shared; only the per-instance × per-sub match adds),
while Parsel's per-container loop grows **linearly** (it re-runs each sub-selector against every
container node). So the per-instance on-demand evaluation is healthy — no super-linear blow-up even
at 5 000 emitted cells/page — and needs no optimization at these schema sizes.

## Reading the numbers

- **Typical speedup \~10–20×, rising to \~40–54× on selector-rich pages.** The engine's cost grows
  roughly linearly with selector count and page size; Parsel's grows **super-linearly** once selectors
  get descendant-heavy (`.product .price`, `.product ::text`) — cssselect translates each to XPath and
  libxml2 re-walks per query, so at 16–32 fields Parsel balloons to 80–145 ms while the engine's single
  pass stays in single-digit ms. The one-pass, no-DOM design is what widens the gap with field count.
- **Constant speedup across size** (~20× from 19 KB to ~1 MB): both scale linearly, engine at a steady
  ~189 MB/s on this page type.
- **Throughput is page-shape-dependent.** Text-dominated pages (article, corpus) run 0.8–3.5 GB/s
  because memchr bulk-skips text; tag-dense pages (table, listing, deep-nest) run 50–800 MB/s because
  cost is per-element (classify + stack + per-selector match), not per-byte.
- **Weak spot — deep nesting.** 20-level-deep pages are the engine's worst case (5–18×, and 619 µs for
  a single selector): structural matching walks the ancestor stack per element (`seg_match`), so cost
  rises with depth. Real pages are rarely this deep; still, it's the honest floor. (Parsel is *also*
  slow here, so the ratio holds up, but the engine's absolute µs is high.)

Net: on realistic pages (article, product listings, the real-shaped corpus) the engine is **~12–20× at
typical field counts and up to ~40–54× on rich schemas**.

## Real Zyte corpus (throughput)

Where the synthetic matrix above uses the Rust `bench` binary, this runs the **Python binding**
(`frostwork.extract`) against Parsel over a **real Zyte corpus**: 788 distinct real pages across 119
page objects (356 MB, median 330 KB, max 3.7 MB), each extracted with its **own production selectors**
(median 11/page). Frostwork does one streaming pass; Parsel parses once then queries per field (its
real reuse pattern). The corpus itself is not distributed, but the harness runs against any directory
of page snapshots laid out as `<dir>/<page-object>/selectors.json` + `pages/*.html` (see
`tools/bench_corpus.py`): `.venv/bin/python tools/bench_corpus.py <corpus_dir>` (build the extension
release-first — see below).

| metric | Frostwork | Parsel | speedup |
| --- | --- | --- | --- |
| median µs/page | 300 (494 MB/s) | 3 382 | **10.5×** (median), **12.4×** (aggregate) |

Value parity with lxml is proven separately by the differential gate ([TESTING.md](TESTING.md)), not
this timing run.

## Page objects — `FrostPage.to_item()` vs a Parsel `web_poet.WebPage`

Everything above times the selector layer. This times what a scraper actually calls: `await
page.to_item()` on a whole page object, against the equivalent hand-written `web_poet.WebPage` doing
Parsel's real thing (one `Selector` per response, one `.css()` per field). Both items are compared field
by field before either is timed, and a mismatch **aborts** — timing two page objects that compute
different answers is not a benchmark. Reproduce: `.venv/bin/python tools/bench_webpoet.py`.

| fields | `FrostPage` ms | Parsel `WebPage` ms | speedup | same, on a running loop | speedup |
| --- | --- | --- | --- | --- | --- |
| 1 | 0.32 | 0.90 | **3×** | 0.21 / 0.76 | **4×** |
| 4 | 0.45 | 4.52 | **10×** | 0.59 / 4.48 | **8×** |
| 8 | 0.59 | 13.17 | **22×** | 0.50 / 12.99 | **26×** |
| 12 | 0.79 | 21.39 | **27×** | 0.67 / 21.22 | **32×** |
| 16 | 0.91 | 25.01 | **27×** | 0.81 / 24.71 | **31×** |
| 20 | 1.01 | 27.02 | **27×** | 0.90 / 26.53 | **29×** |

40 KB page (220 product cards), field counts are prefixes of one growing schema. Same machine and caveats
as the matrix above: single machine (Apple arm64), warm, median of 25 reps, indicative rather than
controlled. Repeated runs land in **25×–29×** at the top of the sweep, so treat the last three rows as one
number (~27×) rather than as a curve that peaks at 12 fields.

The **running-loop** columns exist because the main ones charge `asyncio.run` — a fresh event loop per
call — to the page object, while a crawler already has a loop running. Both sides are re-timed that way, not
just ours: charging the setup to one and not the other would be a comparison of one page object's best case
with the other's worst. It matters most at one field, where the loop is a visible share of a sub-millisecond
total, and is noise by twenty.

**Read the curve, not a number.** The lxml parse is only **0.76 ms of Parsel's 28 ms** at 20 fields — 3%.
So virtually none of the win is the parse Frostwork skips; it is per-field tree traversal that Frostwork
does not repeat. That is why the ratio is **3× at one field and ~28× at twenty**: a one-field page object
is mostly fixed cost on both sides and Frostwork's advantage has nothing to amortise. Quote this with the
field count attached, or it is folklore.

**This page shape flatters the ratio, so do not read it as the corpus figure.** These are product cards —
tag-dense, with descendant selectors (`.card .price`) that libxml2 re-walks per query, which the matrix
above already flags as Parsel's worst case. Growing the same page while holding fields at 12 gives 27× at
40 KB, **65× at 162 KB and 133× at 347 KB**: Parsel's per-field cost climbs super-linearly with page size
(22 ms → 174 ms → 734 ms) while Frostwork's stays linear. So the honest summary is that page *size* moves
this number more than field count does, upward, on this shape.

That makes the real-corpus median below (**10.5×**, median 330 KB, median 11 selectors) the number to quote
for realistic work, and the two are not reconcilable by arithmetic: real pages are more text-heavy than
card-dense, their production selectors are mostly cheaper than the descendant-heavy pool here, and
`bench_corpus.py` times `frostwork.extract` rather than `to_item()`. What this section establishes is
narrower and worth having on its own: the page-object layer adds no meaningful overhead of its own, and the
win is per-field traversal rather than the skipped parse.

### Performance boundaries

Three shapes where the curve above does not hold. Exactly one of them is a **loss** to Parsel; the other two
are wasted work and a parity cost, and calling all three "cliffs" would be three claims where the numbers
support one. Measured rather than described: reproduce with
`.venv/bin/python tools/bench_webpoet.py --boundaries`.

**1. A node-taking processor on `all=True` is slower than Parsel, at every size — this is the real loss.**
The processor contract is an lxml node, so each match is re-parsed on its own; Parsel hands over elements
from a tree it has already built.

| matches | `FrostPage` | Parsel | |
| --- | --- | --- | --- |
| 10 | 0.45 ms | 0.28 ms | Parsel faster |
| 50 | 1.08 ms | 0.71 ms | Parsel faster |
| 220 | 4.35 ms | 2.61 ms | Parsel faster |

There is no crossover to find above ten matches: for *that field*, the subtree parses cost more than the
scan saves, by a roughly constant factor. It is a compatibility cost, not a defect — the alternative is handing the processor a string it
silently ignores, which is the defect this handoff was built to fix — and it is bounded to
processor-bearing **bare-element** fields. A page object of `::text`/`::attr()` fields never reaches it, and
one field like this inside a page of ordinary fields still shares the single scan, so the page total can
win while this column loses. If a page object's hot field is a list of nodes, a hand-written
`@web_poet.field` over one Parsel `Selector` is the faster shape and always has been.

**2. Cardinality is applied after the scan, so a first-match field does the work of `all=True`.** Headroom
rather than a regression — not slower than Parsel, just slower than it needs to be:

| matches (page) | `first` | `all=True` | Parsel `.get()` | peak |
| --- | --- | --- | --- | --- |
| 220 (39 KB) | 0.38 ms | 0.38 ms | 1.11 ms | 57 KB |
| 2 000 (365 KB) | 2.31 ms | 2.27 ms | 9.92 ms | 468 KB |
| 6 000 (1.1 MB) | 6.46 ms | 6.53 ms | 30.48 ms | 1 402 KB |

The first two columns are the same measurement at every size, which is the point: the column materialises
every match — on a bare-element field, one whole element's source each — and shaping then discards all but
one. Pushing the limit into the native plan (while continuing the scan for the other fields) is the
optimisation these numbers argue for; **it is not implemented.** The Parsel column says why it is not urgent:
this is the shape where lxml is structurally cheapest, since `.get()` serialises only the element it
returns, and Frostwork still wins it several-fold.

**3. A response with no charset anywhere costs a page-sized decode — and keeping it is a parity
decision.** `frostwork_input()` reads `resp.encoding` so the bytes are scanned with the label Parsel would
have decoded with. With no `Content-Type` charset, no BOM and no `<meta>`, web-poet falls through to
`w3lib.html_to_unicode` over the whole body and caches the text on the response:

| response | `resp.encoding` | label | decoded text retained |
| --- | --- | --- | --- |
| labelled (`charset=utf-8`) | 0.01 ms | `utf-8` | none |
| cold (no charset anywhere) | 0.03 ms | `cp1252` | the whole page, as `str` |

The time is negligible; the retained string is O(page) and the scan never needed it. But look at the label:
inference answers `cp1252` where Frostwork's own sniffer would default to `utf-8`, so *not* reading
`resp.encoding` would decode some pages differently from Parsel. That makes this a correctness trade wearing
a performance costume, and the current choice — pay the decode, match Parsel — is the deliberate one. A
Frostwork-specific scrapy-poet provider that supplies bytes plus Scrapy's own declared encoding is the way
out, and would need its own parity gate before it could be believed.

## Memory (no-DOM ⇒ bounded RSS)

Frostwork's defining property is invisible to a throughput chart: it **builds no tree**, so peak
memory tracks open-stack state and prospective/extracted output, not a materialized page tree.
Text-content predicates use bounded streaming comparison state rather than retaining their subject's
whole string-value. Measured as subprocess-isolated peak RSS
(`ru_maxrss`), reported as work − baseline so interpreter/import/doc-bytes cancel out. Reproduce:
`.venv/bin/python tools/bench_mem.py` (sweep) and `… --real <corpus_dir>` (any corpus in the layout
above; the measured corpus is not distributed).

**Synthetic size sweep** — a tiny fixed selection (3 fields) from a page padded to N MB. Parsel must
build the whole lxml tree (RSS scales with the page); Frostwork streams past the filler:

| page | Parsel RSS | Frostwork RSS | leaner | Parsel time | Frostwork time | faster |
| --- | --- | --- | --- | --- | --- | --- |
| 1 MB | 7.8 MB | 0.1 MB | 100× | 7.9 ms | 1.1 ms | 7.2× |
| 4 MB | 30.0 MB | 0.0 MB | 640× | 32.2 ms | 4.4 ms | 7.4× |
| 16 MB | 118.1 MB | 0.1 MB | 1 511× | 134.2 ms | 18.4 ms | 7.3× |
| 64 MB | 472.2 MB | 0.3 MB | 1 679× | 541.0 ms | 70.9 ms | 7.6× |

Parsel's memory is **linear in page size**; Frostwork's is **flat** (~0.1–0.3 MB regardless).

**Real pages** (largest 12 in the corpus, 2.3–3.7 MB): median **0.3 MB vs 20.6 MB → 72× leaner**
(range 2×–394×, and 7–19× faster). The gap narrows to ~2–3× only on the field-rich / value-heavy
pages (e.g. 42 selectors, or a big inline-JSON `script::text`), where Frostwork's RSS *is* the
returned data — it still never pays for a tree.

> **Build release first.** These tools call the Python extension; `maturin develop` defaults to a
> **debug** build that is ~10× slower and would make the time columns meaningless (RSS is
> unaffected). Run `.venv/bin/maturin develop --release` before benchmarking.
