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
the fixture corpus) + `fuzz-smoke` + `py` — the minimum pre-release check. Individual targets run
their own piece.

The limits of that gate are worth knowing before trusting a "100% parity" number — each bullet below is
a way it has read 100% while the engine was wrong:

- **It only sees generated pages.** A generator reproduces the malformations its author thought of, so it
  is not evidence about the real web — the `dd`/`dt` same-tag close and the dropped-end-tag text split
  were both found on real doc-generator output while the generated gate read 100%. `make gate-corpus`
  runs the gate's verdict over `tests/corpus` (self-authored, but shaped like what broke us, and
  proven to discriminate); point `CORPUS=<dir>` at a real corpus for what fixtures cannot give.
  `make corpus-real` fetches ~30 real pages (gitignored, never vendored) chosen for doc-GENERATOR variety
  and gates over them — the one check that sees markup nobody here wrote or imagined.
  **A 1000-page Common Crawl sample then found things none of the above did**, and the shape of what it
  found is the point: a tag name the engine truncated (`<p<mip-img …>` reported as a `<p>` that is not in
  the document — a FALSE POSITIVE, the outcome no-fallback exists to prevent), a `<meta charset>` past the
  prescan window, two whole tree-construction rules (what ends `<head>` also starts `<body>`; a `<!DOCTYPE>`
  does not break a text node) and the missing document-frame synthesis behind them, a decoder sweep that
  had been *sampled* rather than exhaustive and so read "full parity" while euc-jp differed on the wave
  dash, and documented divergences whose stated scope was too narrow. None of them are exotic;
  they are just markup nobody thought to generate. Sampling the real web is not optional — and when a
  check samples (800 characters, 30 pages), say so where the number is quoted, because "we measured N and
  it was clean" reads as "it is clean".
  **When a divergence looks like libxml2 being odd, check html5lib before calling it a divergence.** It
  is the HTML5 spec reference implementation and settles "is the oracle browser-correct here?" in one
  run: it agreed with libxml2 on the head→body rule (so that was a bug to fix) and disagreed on what
  follows an explicit `</head>` (so that stays libxml2's shape, and the fix was scoped around it).
- **Page coverage is not RULE coverage.** A tree-construction rule no generated page exercises is
  asserted, not tested. `tools/audit_tree_rules.py` (in `make py`) enumerates every rule cell against
  lxml — **add a row to it whenever you add a rule**. docs/TESTING.md has the count this turned up and
  why a new differential family must be checked against the pre-fix build to prove it discriminates.
- **And rule coverage is only as wide as the NAME UNIVERSE it is asked about.** This is the mistake that
  has now shipped four times: every hand-written list of tag names omitted something (`colgroup`, then
  `s`, then `head`/`listing`/`xmp`/`plaintext`), and a rule with no name to probe cannot fail a gate. So
  the universe is one list (`tools/gen_tree_rules.ELEMENTS`), it is proven to be a superset of both the
  engine's own names and an independent element index, and the audit/mutation/generator all read it.
  **Never add a name to a rule table by hand — add it to `ELEMENTS` and regenerate.**
- **And a gate that cannot go red is not a gate.** Three of them couldn't. `tests/test_gates.py` seeds a
  known regression into each gate's decision function and asserts it fails; add a case there when you add
  a gate. Same question for the generators: they can only find bugs in syntax they emit — CSS escapes
  appeared in no generated selector, so that whole surface rode on hand vectors until a review read the
  parser.
- **`make gate-mutate` asks the inverse question: flip one rule cell, does any gate notice?** The first
  sweep found 13% of the table protected by nothing, all from one root cause — the audit's universe was
  drawn from the tags we already thought were special (16 NAMES vs the engine's 19 IDS). Widening it turned
  up 87 real divergences, which were libxml2's `htmlStartClose` NAME-pair table (finer-grained than the tag
  ids). A second sweep then found 93 more unprobed cells, all of them the one tag name `s` missing from the
  audit's probe list; a third round of review found four more names missing from BOTH lists, and with them
  whole missing rows and columns (`head`, `listing`, `xmp`, `plaintext`). That is why the relation is now
  DERIVED from the oracle over a fixed universe rather than transcribed and spot-checked. The hook flips
  the EFFECTIVE close answer for a tag-name pair (and, since the same blind spot applied to raw text, the
  DATA MODE for a name) rather than a cell in one table — two tables feed the close answer and mutating
  either alone is masked wherever the other closes the same pair. Re-run the sweep after touching a rule
  table.

## Repo map

- `src/` — the engine. `lib.rs` (`extract`/`extract_grouped` entry points + routing, `Plan`
  compile-once wrapper, `audit_schema`), `tokenizer.rs` (bytes → `TokenSink` start/text/end events),
  `matcher/` — **the real logic**: `mod.rs` (corrected-stack matcher: `CompiledSchema` compile +
  `Matcher` streaming execute), `compile.rs` (routing eligibility: which selector shapes execute
  faithfully vs. stay unsupported), `matching.rs` (pure read-only match predicates), `deferred.rs`
  (bounded state machines for deferred-close predicates), `decode.rs` (value decoding).
  `selector.rs` (CSS parse), `xpath.rs` (downward XPath → `Selector`), `diagnostics.rs`
  (advisory unsupported-reason classifier for the audit API), `implied_close.rs` (libxml2
  tree-construction rules), `encoding.rs`, `entities.rs`, `mutate.rs` (an identity function unless built
  `--features mutate`, which lets `tools/mutate_rules.py` flip one rule cell per run). `page.rs` is the declarative `Page`/`field`
  → `Item` layer over `extract` (naming + cardinality only; no matching logic), plus `CompiledPage`.
  `python.rs` (feature-gated) is the PyO3 binding — `extract`/`extract_grouped`/`audit_schema`/`Plan`.
  `src/bin/{differ,bench}.rs` back the Python harness.
- `python/frostwork/` — the Python package over the `_frostwork` extension. See `python/AGENTS.md`.
- `tools/` — Python differential/benchmark harness (needs `parsel`). See `tools/AGENTS.md`.

## Principles to preserve

- **Correctness bar = value-parity with lxml, proven by the differential.** Before claiming a
  selector/feature works, run `tools/diff_lxml.py`; the gate is **0 non-whitespace DIVERGE (+ 0
  CRASH)**. An LLM converges on a *correct* parser only against the pass/fail loop, not plausibility.
- **Close to libxml2 2.14, NOT the HTML5 spec.** Tree construction (implied end tags, void set,
  `<p>`-closing) is matched *empirically* to libxml2 — it is the oracle. See `implied_close.rs`.
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
  a requirement.
- Tree-construction rule tables are **generated from the oracle**, not written: `tools/gen_tree_rules.py`
  derives the start-close relation, void set and data modes over a fixed element universe and rewrites
  `src/implied_close.rs`'s generated block (`--check` gates on drift). Do not hand-edit that block, and
  do not add a name to a rule table by hand — add it to `ELEMENTS` and regenerate.
- Rust: exhaustive `match`, keep `clippy` clean. Measure before optimizing (SIMD structural indexing
  and a tag-dispatch index were both prototyped and *rejected* by measurement — don't re-attempt
  without a workload that shows a win).
