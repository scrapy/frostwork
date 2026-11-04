# Testing Frostwork

Frostwork has no fallback parser. Correctness therefore has three parts:

1. the compiler must classify selectors accurately;
2. supported selectors must return the chosen oracle's values; and
3. the gates must prove that the relevant rules and cases were actually exercised.

`make ci` is the minimum local release check, and hosted CI runs the same targets. The sections below
explain what it covers, which oracle owns each subsystem, and which broader checks run separately.

## Oracles and pass criteria

There is no single oracle for every layer, and two of them depend on the parse mode. Browser mode is the
default; lxml mode is `parser="lxml"`.

- **Extracted values:** in lxml mode, Parsel/lxml using libxml2 2.15 or newer. In browser mode, Chrome:
  CSS answered natively by `querySelectorAll` and XPath through a lossless bridge from the browser's tree.
- **CSS selector acceptance:** cssselect/lxml's parser. A selector rejected by the oracle must be refused
  as unsupported, never answered as if it had matched nothing.
- **HTML tree construction:** in lxml mode, empirical parity with libxml2. In browser mode, the tree
  Chrome builds, with Firefox run alongside on request. Every lxml-mode gate pins `parser="lxml"`.
- **Encoding:** browser/WHATWG behavior is the target. `w3lib` is used where it agrees with browsers; known
  browser/w3lib and WHATWG/Python-decoder differences are asserted explicitly in
  [COMPATIBILITY.md](COMPATIBILITY.md).
- **web-poet page objects:** an equivalent Parsel-backed `web_poet.WebPage`, compared on the whole item.

The lxml wheel vendors libxml2, so pinning an lxml version does not guarantee the same oracle on every
platform. `make oracle` (run by `make bootstrap` and CI) builds the pinned lxml on a static libxml2, and
`tools/oracle.py` requires libxml2 2.15 or newer. `--allow-old-libxml2` (or
`FROSTWORK_ALLOW_OLD_LIBXML2=1`) is available for exploration, not release validation.

The core differential assigns each input/selector pair one verdict:

- `AGREE`: values are identical;
- `WS`: values differ only by permitted surrounding whitespace;
- `SKIP-EXPECTED`: the difference is covered by a documented compatibility exception;
- `DIVERGE`: an unexplained value difference;
- `CRASH`: the engine panicked or aborted.

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
make gate-browser       # browser mode against Chrome: trees, values, debug invariants, HTML5 categories
make gate-webpoet-browser
```

`make ci` runs every target in that block, then `make release-check`. `make help` lists every target. The Rust test binaries must be built without the `python` feature; maturin
owns the extension-module build used by the Python checks. The browser gates need Chrome and Node 22;
`FROSTWORK_CHROME` names the binary, and hosted CI installs the Chrome for Testing build that
`tools/browser_oracle/chrome_build.py` prints.

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

That last one is the axis a UTF-8-only generator cannot reach. An attribute name arrives as raw page
bytes while a selector's literal is UTF-8, and that boundary is where a value is lost silently: `[data-año]` can match a UTF-8 page and return nothing for the same
document in windows-1252. So the label is sent on both sides (the protocol's `ENC` field to the engine,
`PS(body=…, encoding=…)` to the oracle) across eight labels, with the literals placed in every position
that reaches the boundary: class, id, attribute name, attribute value and element text, in both the CSS
and the XPath spelling.

The family has two properties, and `tests/test_gates.py` asserts both. Its alphabet round-trips through
each codec, since a word the encoding cannot represent becomes `?` and would test ASCII under the label.
Each case also carries an over-match half, because a selector the oracle answers nowhere can only catch a
false positive, never a lost value.

The generator records documented exceptions separately from unexplained differences, and the by-family
report derives its rows from the recorded families, so a new family is never counted but unseen. A
generated family is useful only if it discriminates: when adding one, run it against the pre-fix build or
an equivalent mutant and confirm that it fails for the intended reason.

### Encoding

`tools/enc_check.py` checks label handling, BOMs, the meta/XML prescan and emitted text/attribute values.
Resolution cases are split into:

- cases where browsers and w3lib agree;
- named differences where Frostwork must keep the browser/WHATWG answer and w3lib must keep the other one;
- named exceptions where Frostwork deliberately differs from a recorded browser answer, which fail as stale
  if Frostwork starts agreeing; and
- the precedence table, one row per pair of competing inputs (BOM, `encoding=`, `content_type=`, the
  document's declarations), checking the winning encoding and the rank that decided it.

The documents live in `tools/enc_cases.py`, so `tests/test_encoding_inputs.py` can run every one of them,
with valid, unknown and ignored labels, through each Python entry point and a real web-poet `FrostPage`,
and require one answer, a raised error included.

Legacy decoder coverage enumerates the relevant byte space rather than sampling realistic strings. This is
necessary because `encoding_rs` follows WHATWG indexes while Parsel ultimately uses Python codecs.

### Corpus checks

`make gate-corpus` runs the same value verdict over `tests/corpus`, a small vendored set of self-authored
fixtures shaped like the real markup that breaks parsers.

The corpus gate uses the differential's verdict without additional normalization. Empty attributes and
whitespace-only text nodes still count as values: removing one can change a first-valued field.
`tests/test_gates.py` injects such regressions through the complete corpus command and checks its exit
status and report, alongside ordering, duplicate-count and text-node-splitting regressions.

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

## Browser-mode gates

Browser mode is graded against Chrome, and against Firefox as well with `BROWSERS=chrome,firefox`.
[BROWSER_MODE.md](BROWSER_MODE.md) section 5 describes the oracle; `tools/AGENTS.md` describes the tools.

- **`make gate-browser`** (`tools/diff_browser.py`) grades the whole tree and the value columns. Its
  documents are every short sequence of the frame and table alphabet, seeded random runs over an
  HTML5-heavy alphabet in which every start tag carries an id, decorated documents for the value rules,
  deferred-tail contexts, and structured families a random run rarely assembles (documents ending inside a
  tag, comments in every position, formatting copies, Noah's Ark, the depth cap, successive frame merges,
  and every attribute value a tree rule reads spelled with and without character references). It refuses
  to grade unless every token is emitted.
  - The tree columns are `seq_sweep`'s whole-tree fingerprint, evaluated on Chrome's own JSON tree. The
    value columns are a selector basket, answered natively in the browser for CSS and through the lossless
    bridge for XPath.
  - Every differing column is reduced to the smallest token subsequence that still differs and
    attributed by that repro. No tree construct is a documented divergence, so every tree difference
    fails. A value column that differs where the tree agrees may be explained only by a named value
    construct (raw-source outer HTML, quirks-mode class matching).
  - Every value column is also asked of the engine alone and compared with its answer beside the other
    columns, because which columns share a schema decides what the engine tracks.
  - It also fails on coverage (nothing graded, a token never emitted, an oracle error, a bridge round trip
    that changed a tree, a `DOMParser` tree that differs from a page load) and on a panic in a debug
    `differ`, whose `debug_assert!`s are the builder's invariants.
  - It then re-measures the HTML5 element categories in Chrome (`gen_html5_rules.py --check --live`);
    `make py` checks the generated file against the stored answers without a browser.
  - Last, it sweeps every legacy decoder against the browser (`browser_decoders.py`): each single-byte
    encoding's high half and each multi-byte encoding's two-byte space, in sequence, so a decoder state one
    malformed sequence leaves reaches the next. Chrome's decoder bugs are listed with Chrome's answer, and
    the sweep fails on any other difference and on a listed one that stops occurring.
- **`make gate-webpoet-browser`** grades browser-mode page objects against parsel page objects whose
  values come from Chrome, and checks that `.as_node()` is refused at class definition.
- **`make gate-corpus-browser`** is report-only and outside `make ci`. It page-loads each corpus page as
  exact bytes with its `Content-Type` and grades every selector, split into tree and value differences.
- **`tools/retro_census.py`** counts how often each retroactive rule fires over a corpus, which is the
  share of pages browser mode replays.

## Gate integrity

`tests/test_gates.py` seeds known failures into gate decision functions and asserts that each one turns red.
This catches a particularly dangerous regression: a check that prints a mismatch but still exits zero, or a
coverage bucket that disappears while the run remains green.

When adding a gate or a coverage requirement, add a corresponding negative test. When adding a generated
family, prove that the family reaches the intended behavior. Passing output is not evidence unless the same
check can fail.

## The abi3 floor, and the dependency-free smoke test

The wheel is `abi3-py310`, so CPython 3.10 must keep working, and the floor job runs the same
`pytest tests/` as every other interpreter. The floor and the test toolchain have to agree for that to be
possible: `parsel`, `web-poet` *and* `pytest` each require ≥ 3.10, so a lower floor leaves its own job
unable to run the real suite. Some behaviour differs on the floor: a marker on a non-Frostwork base
raises `TypeError`, wrapped in a `RuntimeError` on every Python before 3.12 (see PYTHON.md), and only the
full suite on the floor interpreter sees it.

`tools/abi3_smoke.py` runs there too. It is the only check that the core works with **no Python
dependencies installed at all**: importing the extension, extraction, `Page`/`Item`, groups, audit and
source scanning, standard library only. A venv holding parsel and web-poet cannot show that.
Value parity remains the job of the oracle-backed jobs.

## Adding coverage

Use this checklist for a new selector, parser rule or integration behavior:

1. Choose the oracle for that subsystem and record any deliberate difference in COMPATIBILITY.md.
2. Assert both halves of the no-fallback contract: supported means oracle parity; unsupported means
   refused, so `frostwork.check` reports it, the default strict mode raises `UnsupportedSelector`, and
   `strict=False` gives an empty column.
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

`make ci` includes `make release-check`: it builds the sdist, validates the Core Metadata and public
project URLs, rejects README links that would resolve under `pypi.org/project/frostwork/`, and runs
Twine's strict metadata checks. `tools/check_docs.py` renders the public Markdown and validates repository
links against the actual rendered heading IDs; Twine itself skips Markdown rendering. The tag,
public-index installation and provenance checks are described in [RELEASING.md](RELEASING.md).
