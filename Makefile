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
#   make soak        multi-million differential/fuzz soak across independent seeds
#   make py          rebuild the extension (maturin --release) and run the Python test suite
#   make bench       full throughput matrix vs Parsel (minutes; for release notes)
#   make bench-smoke quick article/deep-nesting performance check
#   make ci          test + gate + fuzz-smoke + py  — the minimum pre-release check
#
# The `python` cargo feature builds an extension-module cdylib that can't link into the test/bin
# targets; only maturin (the `py` target) builds it. So `cargo test`/`build` here never pass it.

PY      ?= .venv/bin/python
MATURIN ?= .venv/bin/maturin
FUZZ_ITERS ?= 6000

.DEFAULT_GOAL := help
.PHONY: help bootstrap test build gate fuzz-smoke soak py bench bench-smoke ci

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

soak: build
	$(PY) tools/soak.py

py:
	$(MATURIN) develop --release
	$(PY) -m pytest tests/test_python.py -q
	$(PY) tools/support_snapshot.py --check

bench: build
	$(PY) tools/bench_matrix.py

bench-smoke: build
	$(PY) tools/bench_matrix.py --smoke

ci: test gate fuzz-smoke py
	@echo "frostwork: all local gates passed"
