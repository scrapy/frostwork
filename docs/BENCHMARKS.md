# Frostwork benchmark matrix

Engine-only throughput (Rust `bench` binary, no IPC in the timed loop) vs Parsel (its real model:
parse once, then one `.css()` per field). Both run the **same** selectors on the **same** page, and
every page/selector pair is **byte-identical between the two** (verified: 32/32 AGREE per page), so
each cell is a fair "extract N fields from this page" comparison. Single machine (Apple arm64),
warm; indicative, not a controlled benchmark. Selector pool is a realistic product/article mix;
counts are prefixes of it. Reproduce: `.venv/bin/python tools/bench_matrix.py` (~1 h; Parsel's cells
dominate it).

Every table here except the two at the end (real corpus, memory — they need a corpus this repo does not
ship) comes from **one** run of that command, so the cells are comparable to each other. They are **not**
comparable to a previous publication of this file: absolute µs move 5–10% with machine state, which is
more than many changes are worth. Measuring a change therefore means running both builds *interleaved
within each cell* (build each to its own `bench` binary, alternate them, take min-of-N) — see the
signature-filter deltas in [DESIGN.md](DESIGN.md#performance-decisions-all-validated-against-the-gate).

## Page type × selector count (µs/page)

| page (≈196 KB) | sels | engine µs | engine MB/s | Parsel µs | speedup |
| --- | --- | --- | --- | --- | --- |
| **article** (text-heavy) | 1 | 91 | 2217 | 565 | 6.2× |
|  | 4 | 198 | 1012 | 2805 | 14.2× |
|  | 8 | 299 | 670 | 5508 | 18.4× |
|  | 16 | 359 | 560 | 6537 | 18.2× |
|  | 26 | 482 | 416 | 9995 | 20.7× |
|  | 32 | 575 | 349 | 11300 | 19.6× |
| **product&#95;listing** | 1 | 526 | 380 | 2916 | 5.5× |
|  | 4 | 1108 | 181 | 15335 | 13.8× |
|  | 8 | 1366 | 147 | 21957 | 16.1× |
|  | 16 | 2035 | 98 | 81241 | 39.9× |
|  | 26 | 2784 | 72 | 146968 | 52.8× |
|  | 32 | 3066 | 65 | 147831 | 48.2× |
| **table-heavy** | 1 | 619 | 323 | 3182 | 5.1× |
|  | 4 | 1146 | 175 | 13205 | 11.5× |
|  | 8 | 1256 | 159 | 13535 | 10.8× |
|  | 16 | 1960 | 102 | 44934 | 22.9× |
|  | 26 | 2805 | 71 | 81940 | 29.2× |
|  | 32 | 3411 | 59 | 98707 | 28.9× |
| **deep-nested** (20-deep) | 1 | 1010 | 198 | 3257 | 3.2× |
|  | 4 | 1361 | 147 | 10518 | 7.7× |
|  | 8 | 1687 | 119 | 12684 | 7.5× |
|  | 16 | 2531 | 79 | 39293 | 15.5× |
|  | 26 | 3405 | 59 | 54797 | 16.1× |
|  | 32 | 4169 | 48 | 55607 | 13.3× |

The matrix also picks up a page from `fixtures/` when one is present. That directory is gitignored
(real snapshots are a licensing/size call), so the row is absent here rather than carried over stale
from a page this checkout cannot re-measure.

## Class-led schema × field count (the matcher's own regime)

Utility-CSS markup — every element carries 4–8 `class=` tokens, the shape Tailwind-style sites emit —
queried by N **class-led** fields, most of which deliberately miss (a real schema is written for one
site's template, and its misses still cost a test per element). This is where per-`(element, selector)`
matching work dominates, so it is the table a matcher change shows up in; the tag-led pool over the
same page is repeated underneath as the contrast (a tag test is one `memcmp`, so it exercises far less
of it). Both from the same run as above; `tools/bench_matrix.py --class-led` runs just these two.

| schema | fields | engine µs | engine MB/s | vals/page | Parsel µs | speedup |
| --- | --- | --- | --- | --- | --- | --- |
| **class-led** | 4 | 706 | 284 | 1760 | 13231 | 18.7× |
|  | 8 | 1009 | 199 | 3520 | 35744 | 35.4× |
|  | 16 | 1126 | 178 | 3520 | 55049 | 48.9× |
|  | 32 | 1521 | 132 | 3520 | 93471 | 61.4× |
| **tag-led** (same page) | 4 | 651 | 308 | 1320 | — | — |
|  | 8 | 809 | 248 | 2640 | — | — |
|  | 16 | 1111 | 180 | 5720 | — | — |
|  | 32 | 1523 | 132 | 7920 | — | — |

Engine cost rises **sub-linearly** in field count here — 4→32 class-led fields is 8× the schema for
2.2× the time — because the one-sided signature filter rejects most `(element, compound)` pairs in one
AND before any string comparison, and the pass itself is shared. Parsel's rises **linearly-plus**
(7.1× for the same 8× schema), which is what widens the ratio from 19× to 61×.

## Size sweep (product_listing, 8 selectors)

| size | engine µs | engine MB/s | Parsel µs | speedup |
| --- | --- | --- | --- | --- |
| 19 KB | 144 | 141 | 2234 | 15.6× |
| 195 KB | 1360 | 147 | 22020 | 16.2× |
| 976 KB | 6791 | 147 | 111313 | 16.4× |

## Grouped (Many/One) — `.product` container × N sub-fields

One `Many` group over the product listing (195 KB, ~50 `.product` cards), engine (`bench`'s
`G <container>` mode) vs **Parsel's real per-container loop** (`for c in sel.css(".product"): [c.css(sub)…]`)
— the exact model the single-pass grouping replaces. Reproduce: `.venv/bin/python tools/bench_matrix.py`.

| subs | engine µs | engine MB/s | vals/page | Parsel µs | speedup |
| --- | --- | --- | --- | --- | --- |
| 1 | 843 | 238 | 1006 | 13790 | 16.4× |
| 3 | 1161 | 172 | 3018 | 33378 | 28.7× |
| 5 | 1424 | 141 | 5030 | 52708 | 37.0× |

The engine's grouped cost grows **sub-linearly** in sub count (843 → 1161 → 1424 µs for 1 → 3 → 5
subs — the pass and per-element bookkeeping are shared; only the per-instance × per-sub match adds),
while Parsel's per-container loop grows **linearly** (it re-runs each sub-selector against every
container node). So the per-instance on-demand evaluation is healthy — no super-linear blow-up even
at 5 000 emitted cells/page — and needs no optimization at these schema sizes.

## Deferred tails — N tail fields vs N plain fields (product_listing)

A deferred predicate whose value lives in a subtree (`div:has(a) a::attr(href)`, `li:last-child a::attr(href)`)
re-scans each winner's span instead of streaming, and each one is its own sub-schema — so unlike ordinary
fields they do **not** share the pass, and the cost grows with field count. Kept as its own table so that
stays visible rather than averaged into the headline numbers.

| fields | tail µs | plain µs | tail/plain |
| --- | --- | --- | --- |
| 1 | 1097 | 430 | 2.6× |
| 2 | 1679 | 602 | 2.8× |
| 4 | 3715 | 1016 | 3.7× |
| 8 | 5812 | 1378 | 4.2× |

If that ratio ever matters for a real schema, the fix is merging tails that share a deferred prefix into
one sub-schema — not yet needed at these field counts.

## Reading the numbers

- **Typical speedup \~11–20×, rising to \~50–61× on selector-rich pages.** The engine's cost grows
  sub-linearly with selector count (the pass is shared, and the signature filter rejects most
  `(element, compound)` pairs before any string work); Parsel's grows **super-linearly** once selectors
  get descendant-heavy (`.product .price`, `.product ::text`) — cssselect translates each to XPath and
  libxml2 re-walks per query, so at 16–32 fields Parsel balloons to 80–148 ms while the engine's single
  pass stays in single-digit ms. That divergence, not a constant factor, is what the no-DOM design buys.
- **Constant speedup across size** (~16× from 19 KB to ~1 MB): both scale linearly, engine at a steady
  ~147 MB/s on this page type.
- **Throughput is page-shape-dependent.** Text-dominated pages (the article) run up to ~2.2 GB/s at one
  field because memchr bulk-skips text; tag-dense pages (table, listing, deep-nest) run 48–380 MB/s
  because cost is per-element (classify + stack + per-selector match), not per-byte.
- **Weak spot — deep nesting.** 20-level-deep pages are the engine's worst case (3–16×, and 1010 µs for
  a single selector): structural matching walks the ancestor stack per element (`seg_match`), so cost
  rises with depth. Real pages are rarely this deep; still, it's the honest floor. (Parsel is *also*
  slow here, so the ratio holds up, but the engine's absolute µs is high.) The signature filter helps
  least here — it rejects at the subject, while the cost is the ancestor walk; an ancestor-signature
  variant is the obvious next lever (see `matcher/sig.rs`).
- **A single-field schema is the weakest case for the engine** (3–6×): one pass costs what it costs, and
  there is almost nothing for the filter or the shared scan to amortize. The advantage is a *schema*
  property, not a per-selector one.

Net: on realistic pages the engine is **~11–20× at typical field counts and up to ~50–61× on rich
schemas**, with class-led schemas — what real page objects look like — at the top of that range.

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

> This row and the memory section below **predate the signature filter** and could not be re-measured
> here: that corpus is not in the repo. Their production selectors are class-led with a median of 11
> fields per page — the regime the class-led table shows the filter helping most — so treat these as a
> conservative floor rather than current numbers, and re-run them wherever the corpus lives.

Value parity with lxml is proven separately by the differential gate ([TESTING.md](TESTING.md)), not
this timing run.

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
