# Frostwork benchmark matrix

Engine-only throughput (Rust `bench` binary, no IPC in the timed loop) vs Parsel (its real model:
parse once, then one `.css()` per field). Both run the same selectors on the same page, with values
checked before timing. Results are warm runs on one Apple arm64 machine: indicative, not controlled.
Selector counts are prefixes of one product/article pool. Reproduce with
`.venv/bin/python tools/bench_matrix.py`.

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

One `Many` group over the product listing (195 KB, ~50 `.product` cards), compared with Parsel's
per-container loop. Reproduce with `.venv/bin/python tools/bench_matrix.py`.

| subs | engine µs | engine MB/s | vals/page | Parsel µs | speedup |
| --- | --- | --- | --- | --- | --- |
| 1 | 735 | 273 | 1006 | 14160 | 19.3× |
| 3 | 1064 | 188 | 3018 | 34401 | 32.3× |
| 5 | 1363 | 147 | 5030 | 55125 | 40.4× |

The shared scan keeps grouped growth below Parsel's per-container loop in this sweep. No super-linear
growth appears at these schema sizes.

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
- **Weak spot — deep nesting.** The 20-level page is the slowest Frostwork shape in this matrix (5–18×,
  and 619 µs for one selector): structural matching walks the ancestor stack per element (`seg_match`),
  so cost rises with depth. Parsel is also slow here, but Frostwork's absolute time is higher than on the
  other synthetic shapes.

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
different answers is not a benchmark. Reproduce: `make bench-webpoet` (it rebuilds the release
extension first, so a stale or debug build cannot be timed by accident).

| fields | `FrostPage` ms | Parsel `WebPage` ms | speedup | running-loop ms (`FrostPage` / Parsel) | speedup |
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

That makes the real-corpus median above (**10.5×**, median 330 KB, median 11 selectors) the number to quote
for realistic work, and the two are not reconcilable by arithmetic: real pages are more text-heavy than
card-dense, their production selectors are mostly cheaper than the descendant-heavy pool here, and
`bench_corpus.py` times `frostwork.extract` rather than `to_item()`. What this section establishes is
narrower and worth having on its own: the selector-layer advantage remains visible through `to_item()`, and
the measured win is per-field traversal rather than the skipped parse.

### Performance boundaries

Three boundary questions where the curve above does not hold: node handoff (measured by match count and
subtree size), cardinality retention, and response decoding. Only node handoff becomes slower than Parsel;
the others expose wasted work or a correctness cost. Reproduce with
`make bench-webpoet BENCH_ARGS="--boundaries"`, which rebuilds the release extension first.

**1. A node-taking processor (`.as_node()`) on a many-match field loses to Parsel, and the gap grows with the
match count.** The processor contract is an lxml node, so each match is re-parsed on its own; Parsel hands over
elements from a tree it has already built.

| matches | `FrostPage` | Parsel | |
| --- | --- | --- | --- |
| 1 | 0.16 ms | 0.16 ms | tie |
| 3 | 0.19 ms | 0.18 ms | tie |
| 10 | 0.28 ms | 0.26 ms | Parsel 1.1× |
| 50 | 0.83 ms | 0.62 ms | Parsel 1.3× |
| 220 | 3.27 ms | 2.23 ms | Parsel 1.5× |

Scoped to what was measured: **near parity at one to three matches, and behind by ten.** One `.as_node()`
field per page — the common case, since most zyte item fields are scalars — costs nothing measurable.
Subtree size is not the problem: one match over a 1 KB, 30 KB and 250 KB subtree stays slightly ahead (0.16/0.88/6.41 ms
against 0.18/0.93/6.86), because both sides parse that subtree once and Frostwork skips the whole-document
parse. It is the per-match repetition that costs. Each row checks its processed item before timing; these
processor-specific checks are not a substitute for the node-structure differential.

**2. Cardinality is applied after the scan, so a first-match field does the work of `all=True`.** Headroom
rather than a regression — not slower than Parsel, just slower than it needs to be:

| matches (page) | `first` | `all=True` | Parsel `.get()` | transient peak |
| --- | --- | --- | --- | --- |
| 220 (39 KB) | 0.32 ms | 0.33 ms | 0.97 ms | 57 KB |
| 2 000 (365 KB) | 2.06 ms | 2.11 ms | 9.10 ms | 468 KB |
| 6 000 (1.1 MB) | 6.00 ms | 6.14 ms | 27.82 ms | 1 402 KB |

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

(The peak column above is a *transient* high-water mark — what the call allocates at once, not what it
retains. The retained figure is this table's last column, read off the string web-poet caches.)

The time is negligible; the retained string is O(page) and the scan never needed it. But look at the label:
inference answers `cp1252` where Frostwork's own sniffer would default to `utf-8`, so *not* reading
`resp.encoding` would decode some pages differently from Parsel. That makes this a correctness trade wearing
a performance costume, and the current choice — pay the decode, match Parsel — is the deliberate one. A
Frostwork-specific scrapy-poet provider that supplies bytes plus Scrapy's own declared encoding is the way
out, and would need its own parity gate before it could be believed.

## Memory profile (no DOM)

Frostwork's defining property is invisible to a throughput chart: it **builds no tree**, so peak
memory tracks open-stack state and prospective/extracted output, not a materialized page tree.
Text-content predicates use bounded streaming comparison state rather than retaining their subject's
whole string-value. Measured as subprocess-isolated peak RSS
(`ru_maxrss`), reported as work − baseline so interpreter/import/doc-bytes cancel out. Reproduce:
`.venv/bin/python tools/bench_mem.py` (sweep) and `… --real <corpus_dir>` (any corpus in the layout
above; the measured corpus is not distributed).

**Synthetic size sweep** — a tiny fixed selection (3 fields) from a page padded to N MB. Parsel must
build the whole lxml tree (RSS scales with the page); Frostwork streams past the filler:

| page | Parsel RSS | Frostwork RSS | Parsel time | Frostwork time | faster |
| --- | --- | --- | --- | --- | --- |
| 1 MB | 7.8 MB | 0.1 MB | 7.9 ms | 1.1 ms | 7.2× |
| 4 MB | 30.0 MB | <0.1 MB | 32.2 ms | 4.4 ms | 7.4× |
| 16 MB | 118.1 MB | 0.1 MB | 134.2 ms | 18.4 ms | 7.3× |
| 64 MB | 472.2 MB | 0.3 MB | 541.0 ms | 70.9 ms | 7.6× |

With this fixed-output selector set, Parsel's memory scales with page size while Frostwork stays below
0.3 MB. Frostwork memory can still scale with returned values, as the corpus result below shows.

**Real pages** (largest 12 in the corpus, 2.3–3.7 MB): median incremental RSS is **0.3 MB vs 20.6 MB**;
the median of the per-page ratios is **72×** (range 2×–394×, and 7–19× faster). The gap narrows to ~2–3× only on the field-rich / value-heavy
pages (e.g. 42 selectors, or a big inline-JSON `script::text`), where Frostwork's RSS *is* the
returned data — it still never pays for a tree.

> **Build release first.** These tools call the Python extension; `maturin develop` defaults to a
> **debug** build that is ~10× slower and would make the time columns meaningless (RSS is
> unaffected). Run `.venv/bin/maturin develop --release` before benchmarking.
