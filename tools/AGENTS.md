# tools/ — the differential/benchmark harness

Python harness (needs `parsel`). `oracle.py` guards the oracle toolchain (libxml2 ≥ 2.14 — a pinned
lxml does *not* pin its vendored libxml2); `diff_lxml.py` is the gate; `conformant.py`/`families.py`/
`foreign.py` generate test pages; `enc_check.py` (encoding parity), `sel_fuzz.py`/`diff_fuzz.py`
(selector + malformed-HTML fuzz), `soak.py` (multi-seed soak), `support_snapshot.py`
(regenerates/checks `docs/SUPPORT_SNAPSHOT.md`), `abi3_smoke.py` (stdlib-only floor check — the
pinned toolchain can't be installed on py3.9), `bench_matrix.py`/`bench_corpus.py`/`bench_mem.py`/
`bench_webpoet.py` (benchmarks — the last one times `to_item()` at the PAGE-OBJECT level and verifies both
items are identical before timing either; `--boundaries` measures the three shapes where the healthy-path
curve does not hold, one of which Parsel wins outright, because a benchmark that only reports its good cases
is a brochure).

Three tools own the **web-poet integration**, and they mirror the engine's derive/audit/mutate trio
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
