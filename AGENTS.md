# AGENTS.md — working in Frostwork

Frostwork is a **treeless, one-pass HTML extraction engine**: a Rust core that answers a scraper's
CSS/XPath selectors in a single streaming scan, **without building a DOM**, staying value-identical to
lxml/libxml2 on the common web. No parse tree, **no fallback**. Tagline: *"Frost never takes root."*

Start with [README.md](README.md) for the pitch + API, [docs/DESIGN.md](docs/DESIGN.md) for how/why
it's built, and [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) for the exact supported/divergent/
unsupported contract.

## Build & test

```bash
cargo test                     # Rust unit vectors (page-object layer included; python feature OFF)
cargo build --release          # builds the `differ` + `bench` binaries the harness below uses

# differential vs lxml — the correctness gate; needs Python + the pinned oracle toolchain:
python -m venv .venv && .venv/bin/pip install -r requirements-test.txt   # (= make bootstrap)
.venv/bin/python tools/diff_lxml.py      # GATE: DIVERGE + CRASH must be 0
.venv/bin/python tools/enc_check.py       # encoding parity vs Parsel
.venv/bin/python tools/bench_matrix.py    # throughput vs Parsel

# Python bindings (PyO3 + maturin) — the `python` cargo feature; see docs/PYTHON.md:
.venv/bin/maturin develop                 # build extension + install `frostwork` (editable) into .venv
.venv/bin/python -m pytest tests/test_python.py   # bindings + Page + web-poet + parsel cross-check
```

Note: the `python` feature builds an `extension-module` cdylib. `cargo test`/`cargo build`/the bins
must NOT enable it (they'd fail to link libpython); maturin builds only the `--features python` cdylib.

The `Makefile` bundles these into one-command gates (`make help` lists them): `make ci` = `test`
(unit + clippy) + `gate` (differential + encoding parity vs lxml) + `gate-corpus` (value parity over
the fixture corpus) + `gate-seq` (every tag sequence up to length 4, compared on the whole tree) +
`fuzz-smoke` + `py` — the minimum pre-release check. Individual targets run
their own piece.

The limits of that gate are worth knowing before trusting a "100% parity" number. Each bullet below is a
way it has read 100% while the engine was wrong; **[docs/TESTING.md](docs/TESTING.md) has the evidence and
the history** (including everything four Common Crawl samples found that no generated page did), so what
is here is the operational rule.

- **It only sees generated pages.** A generator reproduces the malformations its author thought of, so it
  is not evidence about the real web. `make gate-corpus` runs the gate's verdict over `tests/corpus`
  (self-authored, but shaped like what broke us, and proven to discriminate); point `CORPUS=<dir>` at a
  real corpus for what fixtures cannot give. `make corpus-real` fetches real pages (gitignored, never
  vendored) chosen for doc-GENERATOR variety. **Sampling the real web is not optional** — it has found
  a bug the rest of the suite missed every time it has been run, and when a check samples, say so where
  the number is quoted.
- **When a divergence looks like libxml2 being odd, check html5lib before calling it a divergence.** It is
  the HTML5 spec reference implementation and settles "is the oracle browser-correct here?" in one run.
- **Page coverage is not RULE coverage.** A tree-construction rule no generated page exercises is
  asserted, not tested. `tools/audit_tree_rules.py` (in `make py`) enumerates every rule cell against
  lxml — **add a row to it whenever you add a rule**, and check a new differential family against the
  pre-fix build to prove it discriminates.
- **And rule coverage is only as wide as the NAME UNIVERSE it is asked about.** This mistake has shipped
  four times: every hand-written list of tag names omitted something (`colgroup`, then `s`, then
  `head`/`listing`/`xmp`/`plaintext`), and a rule with no name to probe cannot fail a gate. So the
  universe is one list (`tools/gen_tree_rules.ELEMENTS`), proven to be a superset of both the engine's own
  names and an independent element index, and the audit/mutation/generator all read it.
  **Never add a name to a rule table by hand — add it to `ELEMENTS` and regenerate.**
- **A rule sweep is 2-D; bugs live in SEQUENCES.** Six crawl-found bugs were shapes where the state at
  token N depends on tokens 1..N-1 (`<frameset>` in `<head>`, `<body>` after `</body>`, `</%>` between two
  text runs), which no (open x incoming) table sweep can reach. `make gate-seq` enumerates every short
  sequence and compares the WHOLE TREE, not a few `::text` columns — a document can be reshaped completely
  and still answer `p::text` identically. When a bug is about ORDER, reach for that before adding another
  cell to a table.
- **And a gate that cannot go red is not a gate.** Three of them couldn't. `tests/test_gates.py` seeds a
  known regression into each gate's decision function and asserts it fails; **add a case there when you
  add a gate**. Same question for the generators: they can only find bugs in syntax they emit.
- **`make gate-mutate` asks the inverse question: flip one rule cell, does any gate notice?** It is the
  only check here that finds blind spots without a human guessing where they are, and every sweep so far
  has found unprobed cells — which is why the rule tables are now DERIVED from the oracle over a fixed
  universe rather than transcribed and spot-checked. **Re-run it after touching a rule table.** Two
  cautions: the hook flips the EFFECTIVE answer rather than a cell in one table (two tables feeding one
  answer mask each other), and the sweep can only flip what the engine models as a cell — when a rule
  turns out to be coarser than reality, widen the DERIVATION first, because a mutation sweep over the
  wrong shape is a green light for the wrong thing.

## Repo map

- `src/` — the engine. `lib.rs` (`extract`/`extract_grouped` entry points + routing, `Plan`
  compile-once wrapper, `audit_schema`), `tokenizer.rs` (bytes → `TokenSink` start/text/end events;
  the data modes it switches on are looked up in `implied_close`, which generates them),
  `matcher/` — **the real logic**: `mod.rs` (corrected-stack matcher: `CompiledSchema` compile +
  `Matcher` streaming execute), `compile.rs` (routing eligibility: which selector shapes execute
  faithfully vs. stay unsupported), `matching.rs` (pure read-only match predicates), `deferred.rs`
  (bounded state machines for deferred-close predicates), `frame.rs` (document-frame state and the NAMED
  questions the frame rules ask about it — read its header before touching one), `decode.rs` (value
  decoding). `selector.rs` (CSS parse), `xpath.rs` (downward XPath → `Selector`), `diagnostics.rs`
  (advisory unsupported-reason classifier for the audit API), `implied_close/` (libxml2 tree-construction
  rules: `generated.rs` is derived from the oracle, `mod.rs` is the hand-written half),
  `encoding.rs`, `entities.rs`, `mutate.rs` (an identity function unless built
  `--features mutate`, which lets `tools/mutate_rules.py` flip one rule cell per run). `page.rs` is the declarative `Page`/`field`
  → `Item` layer over `extract` (naming + cardinality only; no matching logic), plus `CompiledPage`.
  `python.rs` (feature-gated) is the PyO3 binding — `extract`/`extract_grouped`/`audit_schema`/`Plan`.
  `src/bin/{differ,bench}.rs` back the Python harness.
- `python/frostwork/` — the Python package (pure Python over the `_frostwork` extension): `page.py`
  (`extract` wrapper + `Page`/`Item`), `webpoet.py` (`FrostPage`/`field` web-poet integration, soft
  dep), `audit.py` (`frostwork-audit` CLI: schema audit + `--scan`), `scan.py` (ast-based selector-literal
  scan for un-ported source — inline `.css()`, ItemLoaders, LinkExtractors). `pyproject.toml` = maturin
  build; `tests/test_python.py` = pytest suite.
- `tools/` — Python differential/benchmark harness (needs `parsel`). `oracle.py` guards the oracle
  toolchain (libxml2 ≥ 2.14 — a pinned lxml does *not* pin its vendored libxml2); `diff_lxml.py` is the gate;
  `conformant.py`/`families.py`/`foreign.py` generate test pages; `enc_check.py` (encoding parity),
  `decoder_sweep.py` (the importable half of it: the two-byte enumeration, the four disagreement classes
  and their expected counts), `sel_fuzz.py`/`diff_fuzz.py` (selector + malformed-HTML fuzz),
  `seq_sweep.py` (tag SEQUENCES, compared on the whole tree), `soak.py` (multi-seed soak),
  `support_snapshot.py` (regenerates/checks `docs/SUPPORT_SNAPSHOT.md`), `abi3_smoke.py` (stdlib-only
  floor check — the pinned toolchain can't be installed on py3.9), `bench_matrix.py`/
  `bench_corpus.py`/`bench_mem.py` (benchmarks). Three tools own the tree-construction rules and share
  ONE element universe: `gen_tree_rules.py` derives the rules from libxml2 and GENERATES the Rust table
  (`--check` gates on drift, `--report` prints the derivation); `audit_tree_rules.py` asks lxml whether
  every rule cell is RIGHT, by value, through the real engine; `mutate_rules.py` asks whether a WRONG one
  would be noticed (flips a cell via the `mutate` cargo feature and reruns the gates). `corpus_fetch.py`
  fetches real pages into a gitignored corpus.
- `docs/` — `COMPATIBILITY.md` (contract), `DESIGN.md` (architecture), `PYTHON.md` (bindings),
  `TESTING.md`, `BENCHMARKS.md`, `MIGRATION.md` (from Parsel), `SUPPORT_SNAPSHOT.md` (generated).

## Principles to preserve

- **Correctness bar = value-parity with lxml, proven by the differential.** Before claiming a
  selector/feature works, run `tools/diff_lxml.py`; the gate is **0 non-whitespace DIVERGE (+ 0
  CRASH)**. An LLM converges on a *correct* parser only against the pass/fail loop, not plausibility.
- **Close to libxml2 2.14, NOT the HTML5 spec.** Tree construction (implied end tags, void set,
  `<p>`-closing) is matched *empirically* to libxml2 — it is the oracle. See `implied_close/`.
- **No fallback.** An unsupported query returns an empty column — never an error, never a wrong value.
  Widening coverage means adding it natively *and proving parity*, not routing elsewhere.
- **Local divergence, never global desync.** Accepted divergences (foster-parenting, adoption agency,
  outer-HTML raw-source) are *local*. The tokenizer states that prevent offset desync (rawtext,
  comments, CDATA, encoding sniffing) are non-negotiable.
- **Byte/offset core, not `&str` or a DOM.** The tokenizer works on `&[u8]`; values are decoded lazily
  only when emitted (no whole-document UTF-8 validation). Keep it that way — it's a big perf lever.

## Conventions

- **Do not add a `Co-Authored-By` trailer to commits.**
- Commit/push only when asked; default branch is `main`.
- Keep the differential gate green — treat any new `DIVERGE` as a release blocker.
- Benchmark numbers: `docs/BENCHMARKS.md` is canonical — cite it, don't re-quote figures that drift.
- Same rule for TEST COUNTS and differential pair counts: don't put an exact one in prose. Every review
  round so far has caught a stale "N unit tests / ~Nk pairs" claim, because they change every commit. Say
  what the gate proves and let the run print the number.
- Pick the oracle per subsystem: values = Parsel/lxml, selector ACCEPTANCE = cssselect/lxml's parser,
  encoding SNIFFING = w3lib **for the cases browsers and w3lib agree on**. Parsel does not sniff
  `<meta>`, so it cannot oracle the prescan at all; but w3lib is not the *target* either — the intended
  policy is browser/WHATWG correctness, and w3lib differs from browsers in nine named places (prescan
  window, `<body>`, comments, an invalid label, UTF-32, `utf-16`/`x-user-defined` declarations, BOM-less
  UTF-16, XML-declaration position). Those are asserted as differences in `tools/enc_check.py`, not
  chased. Adding a w3lib parity case without checking which side is browser-correct is how a bug becomes
  a requirement. The same split applies to the DECODERS: Python's legacy codecs are not the WHATWG
  indexes, so `enc_check` sweeps every two-byte sequence per label and gates four disagreement classes by
  count. Enumerate over the whole byte space, never over "the sequences the other side calls assigned" —
  that filter hid 457 euc-jp characters browsers render and Python does not have.
- Tree-construction rule tables are **generated from the oracle**, not written: `tools/gen_tree_rules.py`
  derives the start-close relation, the end-tag scope priorities, the void set and the tokenizer data
  modes over a fixed element universe and rewrites `src/implied_close/generated.rs` WHOLE (`--check`
  gates on drift).
  Do not hand-edit that block, and do not add a name to a rule table by hand — add it to `ELEMENTS` and
  regenerate.
- Rust: exhaustive `match`, keep `clippy` clean. Measure before optimizing (SIMD structural indexing
  and a tag-dispatch index were both prototyped and *rejected* by measurement — don't re-attempt
  without a workload that shows a win).
