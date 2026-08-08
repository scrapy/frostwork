# Testing Frostwork

Frostwork has no fallback parser. Correctness therefore has three parts:

1. the compiler must classify selectors accurately;
2. supported selectors must return the chosen oracle's values; and
3. the gates must prove that the relevant rules and cases were actually exercised.

`make ci` is the minimum local release check. The sections below explain what it covers, which oracle owns
each subsystem, and which broader checks run separately.

## Oracles and pass criteria

There is no single oracle for every layer:

- **Extracted values:** Parsel/lxml, using libxml2 2.14 or newer.
- **CSS selector acceptance:** cssselect/lxml's parser. A selector rejected by the oracle must be reported
  unsupported, not answered with an empty value that looks legitimate.
- **HTML tree construction:** empirical parity with libxml2, not a separate HTML5 implementation.
- **Encoding:** browser/WHATWG behavior is the target. `w3lib` is used where it agrees with browsers; known
  browser/w3lib and WHATWG/Python-decoder differences are asserted explicitly in
  [COMPATIBILITY.md](COMPATIBILITY.md).
- **web-poet page objects:** an equivalent Parsel-backed `web_poet.WebPage`, compared on the whole item.

The lxml wheel vendors libxml2, so pinning an lxml version does not guarantee the same oracle on every
platform. `tools/oracle.py` requires libxml2 2.14 or newer. `--allow-old-libxml2` (or
`FROSTWORK_ALLOW_OLD_LIBXML2=1`) is available for exploration, not release validation.

The core differential assigns each input/selector pair one verdict:

- `AGREE` — values are identical;
- `WS` — values differ only by permitted surrounding whitespace;
- `SKIP-EXPECTED` — the difference is covered by a documented compatibility exception;
- `DIVERGE` — an unexplained value difference;
- `CRASH` — the engine panicked or aborted.

The release gate requires zero `DIVERGE` and zero `CRASH`. Expected differences are reported rather than
hidden; [COMPATIBILITY.md](COMPATIBILITY.md) defines their scope.

## Running the checks

```bash
make bootstrap          # create .venv and install the pinned test/oracle toolchain
make ci                 # minimum local release gate

make test               # Rust unit tests and clippy
make gate               # lxml value differential and encoding checks
make gate-corpus        # self-authored corpus fixtures
make gate-seq           # exhaustive short tag sequences, compared as whole trees
make fuzz-smoke         # selector and malformed-HTML fuzz smoke tests
make py                 # extension, Python tests, typing, generated rules and rule audit
make gate-webpoet       # web-poet differential and upstream-surface snapshot
make gate-webpoet-mutate
```

`make help` lists every target. The Rust test binaries must be built without the `python` feature; maturin
owns the extension-module build used by the Python checks.

## Core engine gates

### Rust unit tests and clippy

`cargo test` contains focused vectors for tokenizer states, tree-construction rules, encodings, selector
compilation, matching, grouping, budgets and the Rust `Page` layer. Every fixed regression should have a
small vector here when the behavior can be isolated.

`cargo clippy --all-targets -- -D warnings` keeps the supported build surfaces warning-free.

### Differential against lxml

`tools/diff_lxml.py` drives the release `differ` binary and compares each emitted value with Parsel/lxml.
Its generated inputs cover:

- content-model-valid trees;
- optional-end-tag and implied-close families;
- foreign-content shapes (`svg`, `math`, `template`);
- flat extraction and grouped `Many`/`One` extraction;
- **legacy-encoding pages asked with non-ASCII selector literals** (`tools/encpages.py`), one family per
  charset label.

That last one closed an axis the harness did not have. Both selector-construction sites hardcoded
`encoding="utf-8"`, so no generated pair had ever crossed the byte/UTF-8 boundary a selector literal sits
on — and that is precisely where a silent value loss shipped: an attribute NAME arrives as raw page bytes
while a selector's is UTF-8, so `[data-año]` matched a UTF-8 page and returned nothing for the same
document in windows-1252. The label is now sent on both sides (the protocol's `ENC` field to the engine,
`PS(body=…, encoding=…)` to the oracle) across eight labels, with the literals placed in every position
that reaches the boundary: class, id, attribute name, attribute value and element text, in both the CSS
and the XPath spelling.

Two properties the family has to keep, and `tests/test_gates.py` asserts both. Its alphabet is checked to
round-trip through the codec — a word the encoding cannot represent becomes `?` and would test ASCII while
wearing the label — and each case carries an over-match half, because a selector the ORACLE answers nowhere
can only catch a false positive, never a lost value.

The generator records documented exceptions separately from unexplained differences. A generated family is
useful only if it discriminates: when adding one, run it against the pre-fix build or an equivalent mutant
and confirm that it fails for the intended reason. The encoding family was checked that way against both
regressions it exists for — undecoded attribute names (400 of 720 pairs per label) and ASCII-only XPath
attribute names (160 of 720).

One report-shape lesson came with it: the by-family table's row order was a hand-written closed list, so a
new family counted toward the gate while being **invisible** in the output. The tail is derived from the
recorded families now.

### Encoding

`tools/enc_check.py` checks label handling, BOMs, the meta/XML prescan and emitted text/attribute values.
Resolution cases are split into:

- cases where browsers and w3lib agree; and
- named differences where Frostwork must keep the browser/WHATWG answer and w3lib must keep the other one.

Legacy decoder coverage enumerates the relevant byte space rather than sampling realistic strings. This is
necessary because `encoding_rs` follows WHATWG indexes while Parsel ultimately uses Python codecs.

### Corpus checks

`make gate-corpus` runs the same value verdict over `tests/corpus`, a small vendored set of self-authored
fixtures shaped like previously troublesome real markup.

For a real corpus, use:

```bash
make gate-corpus CORPUS=/path/to/corpus
```

The expected layout is `<corpus>/<page-object>/selectors.json` plus `pages/*.html`.

`make corpus-real` fetches a varied set of third-party pages into gitignored `fixtures/realweb` and gates
them. No third-party corpus is committed. The fetched set samples generator and site variety; it is not a
substitute for a larger crawl or a project's own production corpus.

### Fuzzing

- `tools/sel_fuzz.py` checks selector parsing, support classification, wrong matches, overmatches and crashes.
- `tools/diff_fuzz.py` mutates HTML and compares values with lxml. Known malformed-tree differences are
  attributed explicitly; unexplained differences and crashes fail the gate.
- `fuzz/` contains the coverage-guided `cargo-fuzz` target for arbitrary bytes. Run it separately with
  `cargo +nightly fuzz run extract`.

The smoke fuzzers run in `make ci`. `make soak` repeats the differential and fuzzers over multiple seeds at
a larger workload.

## Tree-rule coverage

Page coverage is not rule coverage, and pairwise rules do not cover order-dependent state. Frostwork uses
four complementary checks:

1. **Derive:** `tools/gen_tree_rules.py` measures libxml2 over the shared element universe and renders the
   generated start-close, end-scope, void and data-mode relations. `--check` fails on source drift.
2. **Audit:** `tools/audit_tree_rules.py` asks the oracle about every observable rule cell, including document
   framing. Add an audit row whenever a new tree rule is added.
3. **Sequence:** `tools/seq_sweep.py` enumerates short tag sequences and compares the whole tree, including
   document order, descendants, text and synthesized-frame placement.
4. **Mutate:** `tools/mutate_rules.py` flips the effective answer for one rule cell and records which detector
   notices. A survivor is a missing case unless the cell is provably unobservable.

All four consume the same element universe from `tools/gen_tree_rules.py`. Do not introduce another
hand-maintained tag list. `make gate-mutate` samples the mutation space; `make gate-mutate-full` is the slower
full sweep normally run before a release or nightly.

## Python layer

`make py` rebuilds the release extension and runs:

- the Python primitive and `Page`/`Item` behavior;
- groups, schema audit and migration scanning;
- web-poet page classes and response types;
- executable documentation examples;
- generated support/rule snapshots and the direct rule audit;
- mypy over the shipped `py.typed` package and its typing fixture.

Cross-checks that require the value oracle use the same libxml2-version guard as the standalone harness.

### The web-poet layer: three gates, and what each one can see

The engine differential cannot see failures above the selector columns, such as a dropped field, a wrong
processor input type or an incorrectly built item. The integration therefore has three gates:

| gate | question | source of truth |
| --- | --- | --- |
| `tools/webpoet_surface.py` | Is every upstream base, field keyword and processor classified, and are the shipped bases injectable? | installed `web_poet` and `zyte_common_items` APIs |
| `tools/diff_webpoet.py` | Does a Frostwork page return the same whole item as an equivalent Parsel page? | Parsel-backed `WebPage` with the same fields and processors |
| `tools/mutate_webpoet.py` | Would the differential, surface check or focused tests notice a broken integration function? | the two gates above plus `webpoet_contract` tests |

For a green differential to be meaningful, the gate must enforce these conditions:

- Fixed schemas exercise every plain processor column and every required variant before randomized class
  shapes run.
- Items are compared over the union of their keys, so a field missing from one side is a divergence.
- Processor coverage requires evidence against the same field value immediately before processor execution;
  a non-empty value alone proves nothing.
- Raw-source allowances compare parsed structure exactly and use the selector's node identity when document
  framing changes how a fragment is parsed.
- ProductPage processor wiring is read across the full upstream MRO and compared as ordered lists, including
  explicitly declined fields.

The mutation sweep declares which detectors must catch each mutation. The caught set must match exactly:
an unexpected detector often means the patch changed a function's calling convention rather than the
behavior under test.

## Gate integrity

`tests/test_gates.py` seeds known failures into gate decision functions and asserts that each one turns red.
This catches a particularly dangerous regression: a check that prints a mismatch but still exits zero, or a
coverage bucket that disappears while the run remains green.

When adding a gate or a coverage requirement, add a corresponding negative test. When adding a generated
family, prove that the family reaches the intended behavior. Passing output is not evidence unless the same
check can fail.

## The abi3 floor, and the dependency-free smoke test

The wheel is `abi3-py310`, so CPython 3.10 must keep working, and the floor job runs the same
`pytest tests/` as every other interpreter. That is new. At the old 3.9 floor it could not: `parsel`,
`web-poet` *and* `pytest` each require ≥ 3.10, so the job ran `tools/abi3_smoke.py` as a stand-in and the
floor was never covered by the real suite. Raising the floor turned the weakest job in CI into a full one
— and the first run there found a live bug (`RuntimeError` wrapping the `TypeError` a marker on a
non-Frostwork base raises, on every Python before 3.12; see PYTHON.md).

`tools/abi3_smoke.py` still runs there, now for the thing it is the only witness to: the core works with
**no Python dependencies installed at all** — importing the extension, extraction, `Page`/`Item`, groups,
audit and source scanning, standard library only. A venv holding parsel and web-poet cannot show that.
Value parity remains the job of the oracle-backed jobs.

## Adding coverage

Use this checklist for a new selector, parser rule or integration behavior:

1. Choose the oracle for that subsystem and record any deliberate difference in COMPATIBILITY.md.
2. Assert both halves of the no-fallback contract: supported means oracle parity; unsupported means empty.
3. Add the smallest focused regression vector.
4. Add or extend the generated family, rule audit, sequence sweep or integration contract that owns the
   broader surface.
5. Prove the new check fails against the pre-fix implementation or a targeted mutant.
6. Compare the whole relevant result: full values, whole grouped rows, whole items or whole trees.
7. Use real-page sampling when the behavior depends on markup a generator may not emit.

Keep volatile test counts and pair totals in command output rather than prose. Benchmark measurements belong
only in [BENCHMARKS.md](BENCHMARKS.md).

## Release validation

Before sharing a release candidate:

```bash
make ci
make gate-mutate-full
make soak
```

Also run `make corpus-real` or a larger project corpus when network and data access are available, and run the
coverage-guided fuzzer for parser changes with a meaningful fuzzing budget.
