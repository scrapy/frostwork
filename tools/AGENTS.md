# tools/ — the differential/benchmark harness

Python harness (needs `parsel`). `oracle.py` guards the oracle toolchain (libxml2 ≥ 2.14 — a pinned
lxml does *not* pin its vendored libxml2); `diff_lxml.py` is the gate; `conformant.py`/`families.py`/
`foreign.py` generate test pages; `enc_check.py` (encoding parity), `sel_fuzz.py`/`diff_fuzz.py`
(selector + malformed-HTML fuzz), `soak.py` (multi-seed soak), `support_snapshot.py`
(regenerates/checks `docs/SUPPORT_SNAPSHOT.md`), `abi3_smoke.py` (stdlib-only floor check — the
pinned toolchain can't be installed on py3.9), `bench_matrix.py`/`bench_corpus.py`/`bench_mem.py`
(benchmarks).

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

Three tools own the tree-construction rules and share ONE element universe: `gen_tree_rules.py`
derives the rules from libxml2 and GENERATES the Rust table (`--check` gates on drift, `--report`
prints the derivation); `audit_tree_rules.py` asks lxml whether every rule cell is RIGHT, by value,
through the real engine; `mutate_rules.py` asks whether a WRONG one would be noticed (flips a cell
via the `mutate` cargo feature and reruns the gates). `corpus_fetch.py` fetches real pages into a
gitignored corpus.
