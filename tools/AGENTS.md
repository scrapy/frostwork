# tools/ — the differential/benchmark harness

Python harness (needs `parsel`). `oracle.py` guards the oracle toolchain (libxml2 ≥ 2.14 — a pinned
lxml does *not* pin its vendored libxml2); `diff_lxml.py` is the gate; `conformant.py`/`families.py`/
`foreign.py` generate test pages; `enc_check.py` (encoding parity) with `decoder_sweep.py` as its importable half — the two-byte
enumeration and the four disagreement classes live there because `enc_check` runs its gate at module
level, so nothing in it could be exercised by a test, and a sweep that quietly narrows reads exactly
like a clean run; `sel_fuzz.py`/`diff_fuzz.py`
(selector + malformed-HTML fuzz), `soak.py` (multi-seed soak), `support_snapshot.py`
(regenerates/checks `docs/SUPPORT_SNAPSHOT.md`), `abi3_smoke.py` (stdlib-only check that the
extension works with no dependencies present), `ab_bench.py` (A/B two `bench` builds by
**interleaving** them inside each cell, min-of-reps, reporting each cell's own jitter; aborts if the two
builds disagree on the VALUE COUNT — and `--corpus <dir>` runs the same comparison over REAL pages with
their own production selectors, which is the mode that matters: the generated tables scored the
signature filter at −13% to −55% and the corpus scored it at zero, while the tokenizer fix the tables
could not see was worth −75% on a real page),
`bench_matrix.py`/`bench_corpus.py`/`bench_mem.py`/
`bench_webpoet.py` (benchmarks — the last one times `to_item()` at the PAGE-OBJECT level and verifies both
items are identical before timing either; `--boundaries` measures the three shapes where the healthy-path
curve does not hold, one of which Parsel wins outright, because a benchmark that only reports its good cases
is a brochure).

`bench_engines.py` is the COMPETITIVE benchmark (`make bench-engines CORPUS=<dir>`, competitors pinned in
`requirements-bench.txt`): the same corpus against selectolax/lexbor, raw lxml+cssselect and bs4+soupsieve,
because everything else here measures Frostwork against the incumbent rather than against the fast end of
the field. Three things it does that a speed table normally does not, each because the alternative reads as
a win that was not measured. **Nothing is timed before its values are checked** — every column goes through
`diff_lxml.verdict`, imported, so AGREE means what it means in the gate. **An engine that cannot express a
selector does not get a cheaper workload**: `workload_columns` is the one decision function behind both
timed scopes (`W-all`, and `W-common` = the columns every engine expresses AND gets right), and
`assert_same_work` re-checks the invariant per page. **Coverage and its reasons are reported separately**,
since "expresses 73% of production selectors" is the other half of "can I use this?". The CSS-only engines
are driven the way their users drive them — match, then read text off each match — which is NOT lxml's
node-set semantics; where that differs the column diverges out of the shared workload rather than being
quietly reproduced, because no scraper writes the document-order merge. `tests/test_gates.py` seeds a wrong
column, a mismatched workload and an absent competitor into those three functions. `bench_mem.py --engines`
runs the peak-RSS side over the same registry: lexbor's DOM is much leaner than libxml2's, which is exactly
the kind of thing a throughput-only comparison hides.

Five files own the **web-poet integration**: `webpoet_cases.py` is the shared registry (which processors
are covered, how a page object reaches each, which gate proves it — read by the two gates below so they
cannot drift), `webpoet_structure.py` is the one structural signature everything compares parsed HTML with
(the differential's raw-source allowance, the benchmark's parity check and the node-handoff sweep all read
it: a second comparator is a second standard, and the benchmark's collapsed whitespace), and the other three
mirror the engine's derive/audit/mutate trio
because the integration made the same mistake the engine's rule tables did — hand-written lists that
omitted something. `diff_webpoet.py` is the gate (build a `FrostPage` and an equivalent parsel `WebPage`
per generated schema, diff `to_item()` on the WHOLE item, across the class-shape × response-type matrix);
`webpoet_surface.py` DERIVES the four upstream surfaces from the installed libraries by introspection —
page bases, `web_poet.field` keywords, zyte processors, processor input types — and regenerates/checks
`docs/WEBPOET_SURFACE.md`, failing on any name that is upstream and in neither the covered nor the declined
list; `mutate_webpoet.py` breaks one load-bearing line at a time and asks whether the gate notices. That
last one earns its keep: its first run found two holes in `diff_webpoet` (no generated field carried a
`.map()`, and no bare-element field was `all=True` with a processor), because a mutation the differential
misses is a hole in the differential. It also NAMES the logic it cannot reach — everything inline in
`__init_subclass__` — so "0 survivors" is not read as "the module is covered".

Two things a later review round added to that trio, both about the gates rather than the code.
`diff_webpoet` now ends in COVERAGE FAILURES as well as divergences (`coverage_failures`): a green run means
either everything agreed or nothing ran, and a class shape that graded 0 pairs, a processor row whose
expected value was empty everywhere, and a `REQUIRED_COLUMNS` combination the generator stopped emitting
were all reported rather than gated. And `mutate_webpoet` runs THREE detectors (`diff`, `unit`, `surface`)
and prints which mutations the differential missed — because one of them genuinely cannot be seen there: the
real zyte processors are too lenient to notice a wrong node (clear-html renders the same text from a
`<title>` as from the `<html>` around it), so the element-universe sweep in `tests/test_python.py` is that
contract's gate, and a single-detector survivor list would have called it dead code.

Three tools own the tree-construction rules and share ONE element universe: `gen_tree_rules.py`
derives the rules from libxml2 and GENERATES the Rust table (`--check` gates on drift, `--report`
prints the derivation); `audit_tree_rules.py` asks lxml whether every rule cell is RIGHT, by value,
through the real engine; `mutate_rules.py` asks whether a WRONG one would be noticed (flips a cell
via the `mutate` cargo feature and reruns the gates). `corpus_fetch.py` fetches real pages into a
gitignored corpus.
