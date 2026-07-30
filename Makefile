# Frostwork — one-command local gates.
#
# The underlying checks live in `cargo`, `tools/`, and `pytest`; this file just names the
# combinations so a human or agent doesn't have to remember them (and can't quietly skip one).
# See AGENTS.md / CLAUDE.md for what each check proves.
#
#   make bootstrap   create .venv and install the pinned test/oracle toolchain
#   make test        Rust unit vectors + clippy (python feature OFF — it must be, see below)
#   make gate        the correctness gate: build the bins, then differential + encoding parity vs lxml
#   make fuzz-smoke  quick selector + malformed-HTML fuzz (crash/WRONG/OVERMATCH gate)
#   make gate-corpus [CORPUS=<dir>]  value-parity gate over a page corpus (defaults to tests/corpus)
#   make corpus-real fetch REAL pages into fixtures/realweb (gitignored), then gate over them
#   make gate-mutate flip rule-table cells one at a time and check a gate notices (sampled)
#   make gate-mutate-full  every cell (~2,200 mutants, ~15 min with the fast gates) — nightly
#   make soak        multi-million differential/fuzz soak across independent seeds
#   make py          rebuild the extension (maturin --release), Python suite + tree-rule audit
#   make bench       full throughput matrix vs Parsel (minutes; for release notes)
#   make bench-smoke quick article/deep-nesting performance check
#   make ci          test + gate + gate-corpus + fuzz-smoke + py — minimum pre-release check
#
# The `python` cargo feature builds an extension-module cdylib that can't link into the test/bin
# targets; only maturin (the `py` target) builds it. So `cargo test`/`build` here never pass it.

PY      ?= .venv/bin/python
MATURIN ?= .venv/bin/maturin
FUZZ_ITERS ?= 6000

.DEFAULT_GOAL := help
.PHONY: help bootstrap test build gate gate-corpus corpus-real gate-mutate gate-mutate-full \
	fuzz-smoke soak py bench bench-smoke ci

help:
	@grep -E '^#   make ' Makefile | sed 's/^#   /  /'

bootstrap:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-test.txt

test:
	cargo test
	cargo clippy --all-targets -- -D warnings

build:
	cargo build --release --bin differ --bin bench

gate: build
	$(PY) tools/diff_lxml.py
	$(PY) tools/enc_check.py

fuzz-smoke: build
	$(PY) tools/sel_fuzz.py --iters $(FUZZ_ITERS) --gate
	$(PY) tools/diff_fuzz.py --iters $(FUZZ_ITERS) --gate

# Value-parity over REAL pages: `gate` only ever sees GENERATED pages, which is not evidence about the
# real web (docs/TESTING.md explains what that missed). No corpus is vendored — third-party page
# snapshots are a licensing/size call, not a code one — so point this at one, laid out as
# `<dir>/<page-object>/{selectors.json,pages/*.html}`. Defaults to the self-authored fixtures in
# tests/corpus (which DO discriminate: 6 divergences against the pre-fix engine), so the target runs
# with no arguments; point CORPUS at a real crawl corpus for the coverage fixtures cannot give.
CORPUS ?= tests/corpus
gate-corpus: build
	$(PY) tools/bench_corpus.py $(CORPUS) --gate

# Fetch real third-party pages into a GITIGNORED dir and gate over them. Nothing is vendored; this is the
# one check that sees markup nobody in this repo wrote or imagined. Still not a substitute for a crawl
# corpus: it is one page per site, so it samples site variety rather than one site's template long tail.
REALWEB ?= fixtures/realweb
corpus-real: build
	$(PY) tools/corpus_fetch.py --out $(REALWEB)
	$(PY) tools/bench_corpus.py $(REALWEB) --gate

# "Is every rule cell RIGHT?" is what tools/audit_tree_rules.py answers. This answers "if a cell were
# WRONG, would any gate notice?" — the only check here that finds blind spots without a human guessing
# where they are. Needs the `mutate` feature (one build serves every mutant); puts the normal build back
# afterwards, because a mutate build must never be shipped or benchmarked.
MUTANTS ?= 40
# The `unit` detector costs ~2.7s per mutant, so the full sweep runs the fast gates only; the sampled
# form runs everything. A survivor is worth re-testing with every detector before believing it.
DETECTORS ?=
gate-mutate:
	cargo build --release --features mutate
	$(MATURIN) develop --release --features python,mutate
	-$(PY) tools/mutate_rules.py --sample $(MUTANTS) $(if $(DETECTORS),--detectors $(DETECTORS),) --gate
	cargo build --release
	$(MATURIN) develop --release

gate-mutate-full:
	$(MAKE) gate-mutate MUTANTS=0 DETECTORS=audit,corpus-fixtures

soak: build
	$(PY) tools/soak.py

py:
	$(MATURIN) develop --release
	$(PY) -m pytest tests/ -q
	$(PY) tools/support_snapshot.py --check
	$(PY) tools/audit_tree_rules.py --gate

bench: build
	$(PY) tools/bench_matrix.py

bench-smoke: build
	$(PY) tools/bench_matrix.py --smoke

ci: test gate gate-corpus fuzz-smoke py
	@echo "frostwork: all local gates passed"
