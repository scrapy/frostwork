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
