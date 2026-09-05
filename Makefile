# Frostwork — one-command local gates.
#
# The underlying checks live in `cargo`, `tools/`, and `pytest`; this file just names the
# combinations so a human or agent doesn't have to remember them (and can't quietly skip one).
# See AGENTS.md / CLAUDE.md for what each check proves.
#
#   make bootstrap   create .venv and install the pinned test/oracle toolchain
#   make bootstrap-bench  add the pinned COMPETITOR parsers (selectolax, bs4) for bench-engines
#   make test        Rust unit vectors + clippy (python feature OFF — it must be, see below)
#   make gate        the correctness gate: build the bins, then differential + encoding parity vs lxml
#   make fuzz-smoke  quick selector + malformed-HTML fuzz (crash/WRONG/OVERMATCH gate)
#   make gate-corpus [CORPUS=<dir>]  value-parity gate over a page corpus (defaults to tests/corpus)
#   make gate-seq    every tag SEQUENCE up to depth 4, compared on the whole tree
#   make corpus-real fetch REAL pages into fixtures/realweb (gitignored), then gate over them
#   make gate-mutate flip rule-table cells one at a time and check a gate notices (sampled)
#   make gate-mutate-full  every rule cell, with the fast gates only (~an hour) — nightly
#   make soak        multi-million differential/fuzz soak across independent seeds
#   make py          rebuild the extension (maturin --release), Python suite + tree-rule audit +
#                    the generated start-close table vs the oracle (tools/gen_tree_rules.py --check)
#   make release-check  build the sdist and validate the exact metadata/README PyPI will render
#   make gate-webpoet  web-poet integration differential vs parsel, compared on the WHOLE item,
#                    plus the upstream surface snapshot (docs/WEBPOET_SURFACE.md) vs the real libraries
#   make gate-webpoet-mutate  break one webpoet.py line at a time; does any gate notice?
#   make bench       full throughput matrix vs Parsel (minutes; for release notes)
#   make bench-smoke quick article/deep-nesting performance check
#   make verify-migration  whole Page items vs Parsel on hashed, saved response fixtures
#   make bench-migration   the same check, then reproducible whole-item timings (JSON report)
#   make bench-webpoet  FrostPage.to_item() vs a Parsel WebPage, swept over field count
#                    BENCH_ARGS="--boundaries" for the boundary questions (four sweeps)
#   make bench-engines CORPUS=<dir>  vs the other fast scraping parsers (selectolax, lxml, bs4),
#                    values checked against parsel/lxml before anything is timed
#   make bench-engines-mem CORPUS=<dir>  peak RSS for the same engines on the largest real pages
#   make ci          test + gate + gate-corpus + gate-seq + fuzz-smoke + py + gate-webpoet(+mutate) +
#                    release-check
#                    — the minimum pre-release check, and the same target list hosted CI runs
#                    (.github/workflows/ci.yml names these targets rather than copying the commands)
#
# The `python` cargo feature builds an extension-module cdylib that can't link into the test/bin
# targets; only maturin (the `py` target) builds it. So `cargo test`/`build` here never pass it.

PY      ?= .venv/bin/python
MATURIN ?= .venv/bin/maturin
FUZZ_ITERS ?= 6000

.DEFAULT_GOAL := help
.PHONY: help bootstrap bootstrap-bench test build ext gate gate-corpus gate-seq corpus-real gate-mutate \
	gate-mutate-full fuzz-smoke soak py gate-webpoet gate-webpoet-mutate bench bench-smoke bench-webpoet \
	bench-engines bench-engines-mem verify-migration bench-migration release-check ci

VERSION := $(shell sed -nE 's/^version = "([^"]+)"/\1/p' pyproject.toml | head -1)
RELEASE_SDIST := target/release-check/frostwork-$(VERSION).tar.gz

help:
	@grep -E '^#   make ' Makefile | sed 's/^#   /  /'

bootstrap:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-test.txt -r requirements-release.txt

# The competitor parsers `bench-engines` measures against. Separate from `bootstrap` because they are
# not oracles — nothing gated depends on them, and `make ci` runs without them installed.
bootstrap-bench:
	.venv/bin/pip install -r requirements-bench.txt

test:
	cargo test
	cargo clippy --all-targets -- -D warnings

build:
	cargo build --release --bin differ --bin bench

# The compiled extension, RELEASE, installed into the venv. Everything that imports `frostwork` from
# Python depends on this, and only `py` used to build it — so the page-object gates and benchmarks below
# ran against whatever `.so` was already installed. Both failure modes are silent and both were hit: a
# STALE extension grades old behaviour as a passing gate, and a DEBUG one (what a bare `maturin develop`
# leaves behind) makes a benchmark a measurement of the wrong build.
ext:
	$(MATURIN) develop --release

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

# Tag SEQUENCES, exhaustively, compared on the whole TREE rather than a few selector values. The rule
# sweeps are two-dimensional and the crawl corpus is luck; this is the surface where the state at token N
# depends on tokens 1..N-1, which is where six of the last bugs lived. Depth 4 takes tens of seconds.
SEQ_DEPTH ?= 4
gate-seq:
	$(PY) tools/seq_sweep.py --depth $(SEQ_DEPTH) --random 20000 --length 8 --gate

# "Is every rule cell RIGHT?" is what tools/audit_tree_rules.py answers. This answers "if a cell were
# WRONG, would any gate notice?" — the only check here that finds blind spots without a human guessing
# where they are. Needs the `mutate` feature (one build serves every mutant); puts the normal build back
# afterwards, because a mutate build must never be shipped or benchmarked.
MUTANTS ?= 40
# The `unit` detector costs ~2.7s per mutant, so the full sweep runs the fast gates only; the sampled
# form runs everything. A survivor is worth re-testing with every detector before believing it.
# The rule audit now sweeps the whole element universe, which made it the dominant per-mutant cost
# (~2.3s). That is the trade that took the close-rule survivors to 0 — do not shrink the universe to
# make this faster; the close dimension is already compressed to one name per behaviour class.
DETECTORS ?=
gate-mutate:
	@set -eu; \
	trap 'status=$$?; trap - EXIT; cargo build --release || status=$$?; $(MATURIN) develop --release || status=$$?; exit $$status' EXIT; \
	cargo build --release --features mutate; \
	$(MATURIN) develop --release --features python,mutate; \
	$(PY) tools/mutate_rules.py --sample $(MUTANTS) $(if $(DETECTORS),--detectors $(DETECTORS),) --gate

gate-mutate-full:
	$(MAKE) gate-mutate MUTANTS=0 DETECTORS=audit,corpus-fixtures

soak: build
	$(PY) tools/soak.py

py: ext
	$(PY) -m pytest tests/ -q
	$(PY) tools/support_snapshot.py --check
	$(PY) tools/gen_tree_rules.py --check
	$(PY) tools/audit_tree_rules.py --gate
	$(PY) -m mypy python/frostwork tests/typing_fixture.py

verify-migration: ext
	$(PY) tools/verify_migration.py tests/migration/pages.py:REGISTRY tests/migration/manifest.json --json target/migration-report.json

bench-migration: ext
	$(PY) tools/verify_migration.py tests/migration/pages.py:REGISTRY tests/migration/manifest.json --json target/migration-report.json --benchmark $(BENCH_ARGS)

# The README is valid on GitHub even when every relative link is broken on PyPI. Check both the source
# policy and the Core Metadata from the artifact, and render the docs to check their repository links.
# Twine checks metadata but skips Markdown rendering. The artifact path names the current version
# exactly so a stale build cannot be mistaken for the candidate.
release-check:
	$(PY) tools/release_check.py
	$(PY) tools/check_docs.py
	$(MATURIN) sdist --out target/release-check
	$(PY) -m twine check --strict $(RELEASE_SDIST)
	$(PY) tools/release_check.py --distribution $(RELEASE_SDIST)

# The layer ABOVE the engine. `gate` proves a selector returns lxml's column; nothing proved that a page
# OBJECT returns parsel's item, and five defects lived in that gap — three of them silent (a processor
# handed a str, a field dropped from the plan, an item that came back `{}`). Compared on the whole item,
# because a vanished KEY is the failure mode of two of them. Needs the extension, so it follows `py`.
WEBPOET_SCHEMAS ?= 120
gate-webpoet: ext
	$(PY) tools/diff_webpoet.py --schemas $(WEBPOET_SCHEMAS)
	$(PY) tools/webpoet_surface.py --check

# "Would a gate notice if one of these lines were WRONG?" — the same question `gate-mutate` asks of the
# engine's rule tables, for the layer that until recently had no gate at all. Its first run found two
# holes in the differential above (no generated field had a `.map()`, and no bare-element field was
# `all=True` with a processor), which is the whole reason to run it: a mutation the differential misses is
# a hole in the differential. Sampled; a survivor is a missing case, not a shrug.
gate-webpoet-mutate: ext
	$(PY) tools/mutate_webpoet.py --gate

bench: build
	$(PY) tools/bench_matrix.py

# The page-object layer rather than the selector layer: `FrostPage.to_item()` vs an equivalent Parsel
# `web_poet.WebPage`, swept over field count because the ratio depends on it (3x at one field, ~28x at
# twenty). Verifies both items are identical before timing either.
# The canonical way to run this: it depends on `ext`, so the numbers cannot come from a stale or debug
# extension. `BENCH_ARGS="--boundaries"` measures the shapes where the healthy-path curve does not hold;
# `BENCH_ARGS="--markdown"` emits the docs/BENCHMARKS.md table.
BENCH_ARGS ?=
bench-webpoet: ext
	$(PY) tools/bench_webpoet.py $(BENCH_ARGS)

bench-smoke: build
	$(PY) tools/bench_matrix.py --smoke

# The competitive field: Frostwork vs the other fast scraping parsers over a real production-selector
# corpus. Depends on `ext` for the same reason `bench-webpoet` does. Nothing is timed before its values
# are checked against parsel/lxml, so a missing or mis-translated competitor column is reported rather
# than measured. Point CORPUS at a corpus in the `bench_corpus.py` layout; `ENGINE_ARGS="--limit 20"`
# for a quick look, and `make bench-engines-mem` for the peak-RSS table over the same engines.
ENGINE_ARGS ?=
bench-engines: ext
	$(PY) tools/bench_engines.py $(CORPUS) $(ENGINE_ARGS)

bench-engines-mem: ext
	$(PY) tools/bench_mem.py --engines $(CORPUS) $(MEM_DOCS)

ci: test gate gate-corpus gate-seq fuzz-smoke py gate-webpoet gate-webpoet-mutate release-check
	@echo "frostwork: all local gates passed"
