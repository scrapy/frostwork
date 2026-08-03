# tools/ — the differential/benchmark harness

Python harness (needs `parsel`). `oracle.py` guards the oracle toolchain (libxml2 ≥ 2.14 — a pinned
lxml does *not* pin its vendored libxml2); `diff_lxml.py` is the gate; `conformant.py`/`families.py`/
`foreign.py` generate test pages; `enc_check.py` (encoding parity), `sel_fuzz.py`/`diff_fuzz.py`
(selector + malformed-HTML fuzz), `soak.py` (multi-seed soak), `support_snapshot.py`
(regenerates/checks `docs/SUPPORT_SNAPSHOT.md`), `abi3_smoke.py` (stdlib-only floor check — the
pinned toolchain can't be installed on py3.9), `bench_matrix.py`/`bench_corpus.py`/`bench_mem.py`
(benchmarks).

Three tools own the tree-construction rules and share ONE element universe: `gen_tree_rules.py`
derives the rules from libxml2 and GENERATES the Rust table (`--check` gates on drift, `--report`
prints the derivation); `audit_tree_rules.py` asks lxml whether every rule cell is RIGHT, by value,
through the real engine; `mutate_rules.py` asks whether a WRONG one would be noticed (flips a cell
via the `mutate` cargo feature and reruns the gates). `corpus_fetch.py` fetches real pages into a
gitignored corpus.
