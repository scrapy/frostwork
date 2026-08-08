# Frostwork benchmarks

Frostwork answers a fixed set of CSS/XPath selectors in **one streaming pass, with no DOM**. These are
its measured numbers against the parsers scraping actually uses.

| on a real production corpus | Frostwork |
| --- | --- |
| vs **Parsel** (what Scrapy uses) | **10.4×** faster, median page |
| vs **lxml + cssselect** | **7.0×** faster |
| vs **selectolax / lexbor** | **6.9×** faster |
| peak memory, large real pages | **0.5 MB** vs Parsel's 20.7 MB |
| values identical to lxml | **99.9%** of production columns |

Every figure here is measured on one warm Apple arm64 machine and is indicative rather than
controlled: treat a ratio as meaningful and an absolute µs as comparable only inside its own run.
**Nothing is timed before its values are checked against lxml** — a benchmark of two engines computing
different answers is not a benchmark.

## Real production pages

587 real pages across 116 page objects (363 MB, median 468 KB, max 3.7 MB), each extracted with its
**own production selectors** — 10 729 (page, selector) columns, ~18 per page. Frostwork does one
streaming pass; Parsel parses once then queries per field, which is its real reuse pattern.

Scoped to the 10 115 columns Frostwork, Parsel and lxml all express *and* agree on. Speedups are the
**median of the per-page ratios**, with p10/p90, because page shape moves this number more than
anything else.

|  | median µs/page | vs Parsel (p10 – median – p90) | vs lxml |
| --- | --- | --- | --- |
| **Frostwork (`Plan`)** | **645** (699 MB/s) | 7.2× – **13.9×** – 26.9× | **7.6×** |
| **Frostwork** | **699** (640 MB/s) | 5.1× – **10.4×** – 16.4× | **7.0×** |
| lxml + cssselect | 4 885 | 1.0× – 1.3× – 3.2× | 1× |
| Parsel | 5 985 | 1× | — |

`Plan` is what `frostwork.Page` / `FrostPage` run: the schema is compiled once and reused per page.
The plain row re-parses the selector strings on every call, so it is the pessimistic case.

**Stripping Parsel's Python wrapper does not explain the gap.** Raw lxml with compiled CSS→XPath is
only ~1.3× faster than Parsel at the median — the wrapper is worth about 20%, and the remaining ~7× is
against libxml2 itself.

The corpus is not distributed, but the harness runs against any directory of page snapshots laid out
as `<dir>/<page-object>/selectors.json` + `pages/*.html`:

```bash
make bootstrap-bench
```

```bash
make bench-engines CORPUS=<dir>
```

## The competitive field

The same corpus and the same production selectors, against the parsers a scraper would consider
swapping in.

| engine | what it is |
| --- | --- |
| **Frostwork** | one streaming pass, no DOM |
| **Parsel** | what Scrapy uses; also this run's parity oracle |
| **lxml + cssselect** | Parsel's tree and translation without Parsel's Python wrapper |
| **selectolax (lexbor)** | the fastest CSS-selector HTML parser in Python scraping |
| **bs4 + lxml** | lxml's tree, soupsieve's CSS, a Python object per node |

Two rules make this a comparison rather than a set of unrelated timings: nothing is timed before its
values are checked, and an engine that cannot express a selector does not get a cheaper workload. So
the table is scoped to the 7 913 columns all six express *and* answer identically — 582 pages, 363 MB.

| engine | median µs/page | MB/s | vs Parsel (p10 – median – p90) |
| --- | --- | --- | --- |
| **Frostwork (`Plan`)** | **478** | 837 | 7.4× – **13.7×** – 26.8× |
| **Frostwork** | **507** | 764 | 5.5× – **10.3×** – 16.0× |
| selectolax (lexbor) | 3 509 | 133 | 1.0× – 1.6× – 2.7× |
| lxml + cssselect | 4 434 | 82 | 1.0× – 1.1× – 3.0× |
| Parsel | 5 210 | 70 | 1× |
| bs4 + lxml | 34 822 | 11 | 0.1× – 0.2× – 0.3× |

### Where selectolax's speed actually comes from

Not the parse. Timing each engine's document build alone, with no queries, over a size spread of 59
real pages (`make bench-engines ENGINE_ARGS="--parse-only --limit 60"`):

| document build alone | median µs | its time ÷ libxml2's (median, p10–p90) |
| --- | --- | --- |
| lxml (libxml2) | 1 415 | 1.00× |
| selectolax (lexbor) | 1 609 | **1.12×** (0.93–1.76) |
| selectolax (modest) | 3 169 | 2.23× (1.52–4.70) |
| bs4 + lxml | 7 521 | 6.37× (3.88–11.25) |

lexbor is a shade *slower* than libxml2 at building the tree. Its 1.6× comes from the query side —
`tree.css()` walks its own tree per selector, where cssselect translates to XPath and libxml2 re-walks
it. Frostwork's margin comes from neither: it answers every field in one scan and never builds the
tree, so it has no row in this table.

### Coverage — how much of a real schema can each engine express?

The other half of "can I use this?". Of 10 729 production columns:

| engine | expressible | what it cannot reach |
| --- | --- | --- |
| Parsel / lxml + cssselect | 97.5% | 266 columns cssselect itself rejects |
| **Frostwork** | **94.3%** | positional predicates (125), XPath outside the downward subset (108), the relative child anchor `./x` (33), text-content-predicate shapes (26) |
| bs4 + lxml | 79.6% | **no XPath at all (1 551)**, universal element part (364), mixed-terminal comma lists (160) |
| selectolax (lexbor) | 78.6% | the same three, plus 128 its CSS engine rejects |

Read down that column and the gap it implies is not the gap: `lxml + cssselect` refuses 266 and its gap
against Parsel is **zero** (same translator), while selectolax's 2 298 refusals are a gap of 2 203.

**XPath is the CSS-only engines' cliff**: 14% of this corpus's selectors are XPath and neither
selectolax nor bs4 has any. Frostwork's declines are listed in the
[compatibility contract](COMPATIBILITY.md) and reported by `frostwork.check` before a scrape runs, so
an unsupported selector is a build-time answer rather than a wrong value.

**The number to port against is the GAP, not the column above.** An engine's own refusal total also counts
selectors the oracle rejects, and those are not a gap — no spider asks for a selector Parsel refuses. The
set difference is measured (`coverage_gap`, reported per run) rather than inferred from the two
percentages, which only bound it:

| | columns |
| --- | --- |
| Parsel expresses it, Frostwork does not — **the gap** | **342** (3.2%) |
| both refuse it (invalid CSS, cssselect syntax errors) | 266 |

Two things that difference changed. The largest single refusal bucket used to read "mixed-terminal comma
groups (179)" and was **95% selectors with an empty member** — a trailing comma, which cssselect rejects
too; the real comma-group mechanism cost is 13 columns of genuinely mixed terminals plus 16 with a
deferred member. And of the 137 `:contains()` columns, **121 now run**: 97 from `:contains()` itself and
the last 24 from the implicit-universal spelling this corpus writes them with
(`:contains('Kilometer')+::text`), which is a second rule and was refused on its own. The 16 that remain
are all inside a comma group, where a deferred capture cannot interleave with streamed members.

### Value parity — the number a speed table hides

Each engine's own expressible columns, compared against Parsel/lxml with the differential gate's
comparator:

| engine | identical | whitespace-only | differs |
| --- | --- | --- | --- |
| Parsel / lxml + cssselect | 100% | 0 | 0 |
| **Frostwork** | **99.9%** | 0 | 6 |
| bs4 + lxml | 97.7% | 138 | 54 |
| selectolax (lexbor) | 95.8% | 4 | 344 |

**Roughly one production column in twenty-four changes value if you swap in selectolax** — before any
question of speed. lexbor is an HTML5 parser and libxml2 is not, and on real pages that is not subtle:

- **`table tbody tr` returns 353 rows under lexbor and 0 under lxml** on a page whose markup omits
  `<tbody>`. HTML5 parsers synthesize it; libxml2 does not.
- **`<template>` content is a separate document fragment in lexbor**, so `… template::text` comes back
  empty where lxml reads straight through it.
- **Tree recovery differs on malformed markup** — one page's `li a` finds 212 links under lexbor, 192
  under lxml.

Frostwork's six are whole-document `::text` collectors, a documented text-segmentation difference.

### Memory — the 12 largest real pages

Peak RSS attributable to parse+extract (2.3–3.7 MB pages, work − baseline, one process per engine,
every engine on the same selectors). Reproduce with `make bench-engines-mem CORPUS=<dir>`.

| engine | median RSS | median ms |
| --- | --- | --- |
| **Frostwork (`Plan`)** | **0.4 MB** | 0.9 |
| **Frostwork** | **0.5 MB** | 0.9 |
| selectolax (lexbor) | 12.2 MB | 8.9 |
| lxml + cssselect | 13.8 MB | 9.8 |
| Parsel | 20.7 MB | 10.3 |
| bs4 + lxml | 24.9 MB | 46.1 |

lexbor's tree is the leanest of the four — ~12% under raw libxml2's, ~40% under what Parsel retains.
It is still a tree: **\~25× Frostwork's**, scaling with the page rather than with what you asked for.

### Not measured, and why

- **lol&#95;html** (Cloudflare) is the closest architectural peer — streaming, CSS-driven, no DOM — but a
  Rust crate, not something a Python scraper can swap in.
- **resiliparse** is a second binding over the same lexbor engine, so it would report binding overhead
  rather than another parser.
- **selectolax's `modest` backend** is a second backend of an engine already here; the parse-only
  sweep carries it, at ~2× lexbor.
- **html5lib** is the HTML5 spec reference, used in this repo as a correctness oracle. At two orders
  of magnitude off lxml it is not a competitor.

Two fairness notes. The CSS-only engines are driven the way their users drive them — match elements,
then read text or an attribute off each match — which is not lxml's node-set semantics; where that
differs, the column drops out of the shared workload rather than being quietly reproduced. And
selectolax's row pays for value parity: its C-level `node.text()` returns one concatenated string per
element where Parsel's `::text` is one value per text node, and producing the latter costs a
Python-level walk.

## Memory profile — what "no DOM" buys

Frostwork's defining property is invisible to a throughput chart. It builds no tree, so peak memory
tracks open-element state and extracted output, not a materialized page. Measured as
subprocess-isolated peak RSS, reported as work − baseline so interpreter, imports and document bytes
cancel out (`.venv/bin/python tools/bench_mem.py`).

A tiny fixed selection (3 fields) from a page padded to N MB — Parsel must build the whole tree,
Frostwork streams past the filler:

| page | Parsel RSS | Frostwork RSS | leaner | Parsel | Frostwork | faster |
| --- | --- | --- | --- | --- | --- | --- |
| 1 MB | 7.8 MB | 0.3 MB | 24× | 8.4 ms | 2.0 ms | 4.3× |
| 4 MB | 29.9 MB | 0.4 MB | 74× | 34.7 ms | 8.2 ms | 4.2× |
| 16 MB | 118.2 MB | 0.3 MB | 420× | 150.1 ms | 32.5 ms | 4.6× |
| 64 MB | 472.3 MB | 0.3 MB | **1439×** | 575.8 ms | 133.7 ms | 4.3× |

For a fixed-output schema Frostwork's memory is flat while Parsel's tracks the page. On real pages it
scales with the values you asked for instead: across the 12 largest corpus pages the median ratio is
**31×**, narrowing to ~2× on the value-heavy ones (42 selectors, or a big inline-JSON `script::text`,
where Frostwork's RSS *is* the returned data). It never pays for a tree either way.

## Page shape — the synthetic matrix

Engine-only throughput (the Rust `bench` binary, no IPC in the timed loop) against Parsel on generated
pages, to isolate how page shape, **selector KIND** and selector count move the ratio. `sels = 0` is the
pure-scan floor and `vals` is what the cell actually extracted — both are load-bearing, see *Reading
these* below. Regenerate with `.venv/bin/python tools/bench_matrix.py --markdown`.

<!-- BEGIN generated: tools/bench_matrix.py --markdown -->
| page (≈196 KB) | sels | vals | engine µs | engine MB/s | Parsel µs | speedup |
| --- | --- | --- | --- | --- | --- | --- |
| **article (text-heavy)** | 0 | 0 | 82 | 2440 | 547 | — |
|  | 1 | 1 | 89 | 2251 | 567 | 6.4× |
|  | 4 | 757 | 204 | 982 | 2889 | 14.1× |
|  | 8 | 2017 | 342 | 587 | 5794 | 16.9× |
|  | 16 | 2017 | 368 | 546 | 6810 | 18.5× |
|  | 26 | 2773 | 496 | 404 | 10314 | 20.8× |
|  | 32 | 3277 | 567 | 354 | 11388 | 20.1× |
| **product&#95;listing** | 0 | 0 | 385 | 520 | 2636 | — |
|  | 1 | 1 | 420 | 476 | 2770 | 6.6× |
|  | 4 | 3019 | 1072 | 187 | 15788 | 14.7× |
|  | 8 | 6037 | 1492 | 134 | 22466 | 15.1× |
|  | 16 | 15091 | 2230 | 90 | 87091 | 39.1× |
|  | 26 | 25151 | 2931 | 68 | 159669 | 54.5× |
|  | 32 | 25151 | 3118 | 64 | 152243 | 48.8× |
| **table-heavy** | 0 | 0 | 551 | 364 | 3902 | — |
|  | 1 | 1 | 675 | 297 | 3740 | 5.5× |
|  | 4 | 4371 | 1494 | 134 | 21266 | 14.2× |
|  | 8 | 8741 | 2029 | 99 | 30949 | 15.3× |
|  | 16 | 8741 | 2672 | 75 | 45069 | 16.9× |
|  | 26 | 13111 | 3481 | 58 | 70383 | 20.2× |
|  | 32 | 13111 | 4066 | 49 | 71846 | 17.7× |
| **deep-nested** | 0 | 0 | 891 | 225 | 3295 | — |
|  | 1 | 1 | 1031 | 194 | 3566 | 3.5× |
|  | 4 | 726 | 1399 | 143 | 11028 | 7.9× |
|  | 8 | 1451 | 1782 | 112 | 13369 | 7.5× |
|  | 16 | 3626 | 2682 | 75 | 40585 | 15.1× |
|  | 26 | 4351 | 3696 | 54 | 59372 | 16.1× |
|  | 32 | 4351 | 4518 | 44 | 58509 | 13.0× |
| **class-led / utility-CSS** | 0 | 0 | 204 | 981 | 1804 | — |
|  | 1 | 440 | 433 | 463 | 5122 | 11.8× |
|  | 8 | 3520 | 1013 | 198 | 38721 | 38.2× |
|  | 32 | 3520 | 1451 | 138 | 100242 | 69.1× |
| **attr-led / attribute-heavy** | 0 | 0 | 260 | 769 | 2891 | — |
|  | 1 | 447 | 350 | 572 | 4457 | 12.7× |
|  | 8 | 1341 | 562 | 357 | 9327 | 16.6× |
|  | 32 | 1341 | 1198 | 167 | 18970 | 15.8× |
<!-- END generated -->

**Size sweep** (product listing, 8 selectors) — both scale linearly, so the ratio holds:

| size | engine µs | engine MB/s | Parsel µs | speedup |
| --- | --- | --- | --- | --- |
| 19 KB | 162 | 125 | 2 514 | 15.6× |
| 195 KB | 1 459 | 137 | 22 715 | 15.6× |
| 976 KB | 7 000 | 143 | 114 200 | 16.3× |

**Grouped** (`Many`/`One`) — one `.product` container × N sub-fields over the same listing, against
Parsel's per-container loop. The shared scan keeps growth below the loop:

| subs | engine µs | vals/page | Parsel µs | speedup |
| --- | --- | --- | --- | --- |
| 1 | 886 | 1 006 | 15 852 | 17.9× |
| 3 | 1 221 | 3 018 | 36 059 | 29.5× |
| 5 | 1 545 | 5 030 | 54 250 | 35.1× |

### Reading these

The `vals` column is the point of the table, not decoration: a cell that extracts nothing is timing a
scan, not an extraction, and `assert_cell_extracts` now refuses to publish one. The **0-selector row is
the pure-scan floor** — tokenizer, open-element stack and nothing else — so every other cell in a shape
decomposes into that floor plus matcher work. Parsel's 0-selector cell is its parse.

- **The matcher, not the scan, is where the time goes.** On the class-led page the floor is 204 µs and
  eight class-led fields cost 1 013 — **80% matching**, rising to 86% at 32 fields. Even one field is
  already the majority on that shape. The throughput figures below describe the floor; they do not
  describe a real schema's cost.
- **The gap widens with field count.** Frostwork's cost grows roughly linearly with selectors and page
  size; Parsel's grows super-linearly once selectors are descendant-heavy (`.product .price`), because
  cssselect translates each to XPath and libxml2 re-walks the tree per query. At 26–32 fields Parsel
  reaches 71–160 ms where the single pass stays in single-digit ms, and the class-led page at 32 fields
  is the widest cell in the table at **69×** (100 ms vs 1.5 ms).
- **A one-field schema is the weak case** (3.5–6.6× on the tag-led shapes): the scan is paid whether or
  not there is anything to amortise it over. The class-led and attribute-led rows sit higher (11.8×,
  12.7×) because their first field matches every card rather than once per page.
- **Throughput is page-shape-dependent, and that is a property of the SCAN.** Text-dominated pages reach
  ~2.4 GB/s at the floor because `memchr` bulk-skips text; tag-dense pages run 225–520 MB/s because the
  cost is per element, not per byte.
- **Deep nesting is the slowest shape (3.5–16.1×) because of element DENSITY, not depth.** Its floor is
  891 µs against the article's 82 µs on the same 195 KB — a 10.8× spread with **zero selectors and no
  structural matching at all** — and it carries ~12 bytes per element against the article's ~166. An
  earlier version of this section blamed the ancestor-stack walk; the pure-scan row is what disproved
  that. Separating depth from density would need a shape that varies one at constant other, which the
  harness does not have, so nothing here is evidence about depth itself.
- **Deferred tails cost extra, per distinct prefix.** `:has()`, `:contains()`, `:last-child` and XPath
  text predicates resolve from a re-scan of each winner's span rather than from the streaming pass, so
  they do not share the main scan. Tails deferring on the SAME compound share one re-scan, so the cost
  tracks the number of distinct prefixes rather than the number of fields — two fields on one compound
  (`div:has(a) a::attr(href)` and `div:has(a) p::text`) collapse into a single extra pass. **The ratios
  in the table above predate that sharing and are stale; re-run `tools/bench_engines.py` before quoting
  one.**
- **A pool only measures what it contains.** The four page shapes run the tag-led pool; the class-led and
  attribute-predicate rows exist because a real page object is mostly class-led and neither other pool
  holds an attribute predicate. `tools/ab_bench.py` carries the same pools crossed with more shapes.
- **Run-to-run spread is \~5–15% per cell** on the same binary. For anything comparative, A/B two builds
  with `tools/ab_bench.py` (interleaved, min-of-reps, each cell carrying its own jitter) rather than
  comparing absolute figures across runs.

## Page objects — `FrostPage.to_item()` vs a Parsel `web_poet.WebPage`

Everything above times the selector layer. This times what a scraper actually calls:
`await page.to_item()` on a whole page object, against the equivalent hand-written
`web_poet.WebPage` doing Parsel's real thing. Both items are compared field by field before either is
timed, and a mismatch aborts. Reproduce: `make bench-webpoet`.

| fields | `FrostPage` ms | Parsel `WebPage` ms | speedup | running-loop ms (`FrostPage` / Parsel) | speedup |
| --- | --- | --- | --- | --- | --- |
| 1 | 0.30 | 0.87 | **3×** | 0.19 / 0.72 | **4×** |
| 4 | 0.39 | 4.26 | **11×** | 0.32 / 4.24 | **13×** |
| 8 | 0.52 | 12.46 | **24×** | 0.42 / 12.41 | **30×** |
| 12 | 0.66 | 20.15 | **30×** | 0.55 / 20.09 | **36×** |
| 16 | 0.83 | 23.32 | **28×** | 0.69 / 23.35 | **34×** |
| 20 | 0.86 | 25.31 | **29×** | 0.75 / 24.82 | **33×** |

40 KB page (220 product cards), field counts are prefixes of one growing schema, median of 25 reps.
Repeated runs land in 25×–30× at the top, so read the last three rows as one number (~29×) rather than
as a curve that peaks at 12 fields.

The **running-loop** columns exist because the main ones charge `asyncio.run` — a fresh event loop per
call — to the page object, while a crawler already has one running. Both sides are re-timed that way.

**Read the curve, not a number.** The lxml parse is only 0.76 ms of Parsel's 25 ms at 20 fields, ~3%.
Virtually none of the win is the parse Frostwork skips; it is per-field tree traversal Frostwork does
not repeat. That is why the ratio is 3× at one field and ~29× at twenty.

**This page shape flatters the ratio.** These are product cards — tag-dense, with descendant selectors
that libxml2 re-walks per query, the matrix's worst case for Parsel. Growing the page while holding
fields at 12 gives 27× at 40 KB, **65× at 162 KB and 133× at 347 KB**. Page *size* moves this number
more than field count does. The real-corpus median (**10.4×**) is the figure to quote for realistic
work: real pages are more text-heavy than card-dense, and their production selectors are mostly cheaper
than the descendant-heavy pool here.

### Where the design trades off

Three shapes where the curve above does not hold. Reproduce with
`make bench-webpoet BENCH_ARGS="--boundaries"`.

**1. A node-taking processor (`.as_node()`) on a many-match field loses to Parsel.** The processor
contract is an lxml node, so each match is re-parsed on its own, where Parsel hands over elements from
a tree it has already built.

| matches | `FrostPage` | Parsel |  |
| --- | --- | --- | --- |
| 1 | 0.16 ms | 0.16 ms | tie |
| 3 | 0.19 ms | 0.18 ms | tie |
| 10 | 0.28 ms | 0.26 ms | Parsel 1.1× |
| 50 | 0.83 ms | 0.62 ms | Parsel 1.3× |
| 220 | 3.27 ms | 2.23 ms | Parsel 1.5× |

Near parity at one to three matches, behind by ten. One `.as_node()` field per page — the common case,
since most zyte item fields are scalars — costs nothing measurable, and subtree size is not the
problem: one match over a 1 KB, 30 KB and 250 KB subtree stays slightly ahead. It is the per-match
repetition that costs.

**2. Cardinality is applied after the scan**, so a first-match field does the work of `all=True`.
Headroom rather than a regression — still several-fold faster than Parsel's `.get()`, which is the
shape where lxml is structurally cheapest:

| matches (page) | `first` | `all=True` | Parsel `.get()` |
| --- | --- | --- | --- |
| 220 (39 KB) | 0.32 ms | 0.33 ms | 0.97 ms |
| 2 000 (365 KB) | 2.06 ms | 2.11 ms | 9.10 ms |
| 6 000 (1.1 MB) | 6.00 ms | 6.14 ms | 27.82 ms |

**3. A response with no charset anywhere costs a page-sized decode.** `frostwork_input()` reads
`resp.encoding` so the bytes are scanned with the label Parsel would have decoded with; with no
`Content-Type` charset, no BOM and no `<meta>`, web-poet falls through to `w3lib.html_to_unicode` over
the whole body. The time is negligible (0.01 → 0.03 ms) but the decoded text is retained. Keeping it
is deliberate: inference answers `cp1252` where Frostwork's own sniffer would default to `utf-8`, so
*not* reading `resp.encoding` would decode some pages differently from Parsel.

## Method

- One warm Apple arm64 machine, best-of-N per cell. Ratios are comparable; absolute µs figures are
  comparable only inside their own run.
- Values are checked before timing everywhere: the corpus runs compare every column against
  Parsel/lxml with the differential gate's own comparator, and the page-object bench aborts on a
  mismatched item.
- Value parity with lxml is proven separately and continuously by the differential gate — see
  [TESTING.md](TESTING.md).
- For "did this change help?", A/B two builds with `tools/ab_bench.py` (add `--corpus <dir>` to run it
  over real pages with their own selectors) rather than comparing figures between runs.

> **Build release first.** These tools call the Python extension, and `maturin develop` defaults to a
> **debug** build ~10× slower than release, which would make every time column meaningless. Run
> `.venv/bin/maturin develop --release`, or use the `make` targets above — they rebuild it for you.
