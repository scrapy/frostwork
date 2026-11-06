# Working in Frostwork

Frostwork is a **streaming HTML parser**: a Rust core that answers a scraper's CSS/XPath selectors in a
single streaming scan, value-identical to Chrome in browser mode (the default) and to lxml/libxml2 in lxml
mode. It builds a minimal tree only when needed: browser mode answers a page where an HTML5 rule rewrites
elements already parsed from a tree of that page (the replay), and lxml mode never builds one. **No
fallback.** Tagline: *"Frost never takes root."*

Start with [README.md](README.md) for the pitch and API, [docs/DESIGN.md](docs/DESIGN.md) for how and why
it is built, [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) for the exact contract,
[docs/BROWSER_MODE.md](docs/BROWSER_MODE.md) for browser mode and [docs/TESTING.md](docs/TESTING.md) for
what each gate proves.

## Build and test

```bash
make bootstrap                 # .venv with the pinned test and oracle toolchain
cargo test                     # Rust unit vectors (python feature OFF)
make ci                        # every gate below: the minimum pre-release check
make help                      # every target, one line each
.venv/bin/maturin develop      # build the extension into .venv (see docs/PYTHON.md)
```

The `python` feature builds an `extension-module` cdylib. `cargo test`, `cargo build` and the bins must
NOT enable it (they would fail to link libpython); only maturin builds `--features python`.

`make ci` runs:

- `test`: cargo test and clippy.
- `gate`: the lxml differential (`tools/diff_lxml.py`, 0 non-whitespace DIVERGE and 0 CRASH) and
  encoding parity (`tools/enc_check.py`).
- `gate-corpus`: value parity over `tests/corpus`; `gate-seq`: every tag sequence up to length 4,
  compared on the whole tree; `fuzz-smoke`: selector and malformed-HTML fuzz.
- `py`: the Python suite, the tree-rule audit, the generated-table checks and mypy over the package.
- `gate-webpoet` and `gate-webpoet-mutate`: the web-poet integration against parsel, and whether a
  broken line in it is noticed.
- `gate-browser` and `gate-webpoet-browser`: browser mode against the Chrome build
  `tools/browser_oracle/html5_derivation.json` names. Needs Chrome and Node 22.
- `release-check`: the sdist and the metadata PyPI renders.

Hosted CI runs the same targets by name (`tests/test_release.py` checks it does).

## Testing rules

Every gate here has read green while the engine was wrong. These rules are what keeps that from
happening again; docs/TESTING.md has the detail.

**The real web.**

- Generated pages hold only the malformations their author thought of. Before trusting a parity number,
  run `make gate-corpus`, `make corpus-real` (about 30 real pages, gitignored) and a Common Crawl sample
  (`make gate-corpus CORPUS=<dir>`). A check that samples says so where its number is quoted.
- When a divergence looks like libxml2 being odd, check html5lib before calling it a divergence; for
  browser mode, check Chrome. A documented divergence is a claim and can be wrong.
- Parsel parses `text.strip().replace("\x00", "")`, not the response bytes. Lxml mode reproduces both.
- **Page-scoped attribution over-credits.** Attribute a divergence by its reduced repro, never by the page
  it came from. When one documented bucket never empties, suspect the bucket.
- One fixed call site is not a fixed rule. Grep every scanner for the same rule before believing the gate
  (the tokenizer's end-tag rules live in three).

**Rule coverage.**

- A rule no generated page exercises is asserted, not tested. `tools/audit_tree_rules.py` enumerates every
  rule cell against lxml; add a row when you add a rule.
- Rule tables are generated from the oracle over one element universe, `tools/gen_tree_rules.ELEMENTS`,
  proven a superset of the engine's names and an independent index. **Never add a name to a rule table by
  hand: add it to `ELEMENTS` and regenerate.** Browser mode's tables come from Chrome the same way
  (`tools/gen_html5_rules.py`).
- Bugs live in sequences. When a bug is about order, reach for `make gate-seq`, which compares the whole
  tree, before adding another cell to a table.
- `make gate-mutate` flips one effective rule answer at a time and asks whether a gate notices. Re-run it
  after touching a rule table. It can only flip what the engine models as a cell, so when a rule is
  coarser than reality, widen the derivation first.

**The gates themselves.**

- A gate that cannot go red is not a gate. `tests/test_gates.py` seeds a regression into each gate's
  decision function; add a case with every new gate, and run a new differential family against the
  pre-fix build to prove it discriminates.
- A generator finds bugs only in the syntax and input it emits. Ask what it never emits (CSS escapes, an
  end tag with attributes, a page that is not UTF-8: `tools/encpages.py`) before trusting a pair count. A
  generator alphabet must round-trip through its codec, and report orders are derived, not listed.
- A selector the oracle answers nowhere can only catch an over-match. `diff_lxml.py` and
  `diff_browser.py` record that set and name any selector that joins it.
- A column's answer must not depend on the schema around it. `diff_browser.py` asks every value column
  alone as well as in its batch.
- A deterministic pass that varies a column replaces it: keep a plain and a variant fixed schema.
- One comparator. A second copy of a structural check is a second standard (`tools/webpoet_structure.py`
  is the one).

**Published numbers.**

- A number the docs publish needs the same gate as a value the engine returns. Benchmarks check values
  before timing them, a timed cell must extract values (`bench_matrix.assert_cell_extracts`), and every
  shape has a zero-selector pure-scan row. A benchmark pool measures only what it contains.
- A refusal the oracle shares is not a coverage gap (`bench_engines.coverage_gap`). When one refusal
  reason dominates, disaggregate it before believing it.

**The web-poet layer.** Its universes are importable, so derive them (`tools/webpoet_surface.py`) rather
than list them; it has the engine's derive, audit and mutate trio.

- A mutation the differential misses is a hole in the differential. A sweep names what it cannot reach.
- Install what you integrate with (`zyte-common-items` is in `requirements-test.txt`), and apply a shape
  axis to both sides of a differential.
- Copy upstream's predicate, not its prose: `out=[]` declines a processor because web-poet tests
  `out is not None`, and `out` is read from the class declaring the resolved descriptor.
- Declare a contract rather than infer it (`.as_node()`), and refuse at class definition what cannot be
  verified. When a value cannot carry a fact, get it from the compiler or refuse.
- A class decorator recreates the class: what `__init_subclass__` or a class keyword sets must survive
  `@attrs.define`.
- `py.typed` makes annotations a promise. `make py` runs mypy, and `tests/test_typing.py` proves the check
  can go red.
- A usable page object is injectable (`web_poet.pages.is_injectable`, `andi.plan`).
- A doc example is untested code. The marked fenced blocks in the docs are executed as written, and each
  example has one copy.
- A dependency floor is a claim; the range is closed around the release line the gates run.

## Repo map

- `src/`: the engine.
  - `lib.rs`: the `extract`/`extract_grouped` entry points and routing, the `Plan` compile-once wrapper,
    `ParseMode`, `audit_schema`, per-mode input preparation.
  - `tokenizer.rs`: bytes to `TokenSink` start, text and end events.
  - `matcher/`, the real logic. `mod.rs` is the corrected-stack matcher (`CompiledSchema` compile,
    `Matcher` streaming execute, and the primitives a tree builder drives it with). `tree.rs` is the seam,
    the `TreeBuilder` trait each parse mode implements. `libxml2.rs` is lxml mode's builder and `frame.rs`
    its document-frame state (read its header before touching a frame rule). `browser/` is browser mode's
    HTML5 tree construction as Chrome runs it: `mod.rs` the builder's state (its header says how the
    parser stack differs from the DOM path), `rules.rs` the insertion modes, `lists.rs` the stack of open
    elements and the active formatting list, `dom.rs` the replay's tree, `tags.rs` and `doctype.rs` names
    and the document mode. `compile.rs` decides which selector shapes run and what browser mode refuses;
    `matching.rs` holds the read-only match predicates, `deferred.rs` the deferred-close state machines,
    `decode.rs` value decoding, and `sig.rs` the one-sided Bloom signatures `compound_matches` opens with
    (read its header before touching the hash).
  - `selector.rs` (CSS), `xpath.rs` (downward XPath), `diagnostics.rs` (the audit's reason classifier),
    `implied_close/` (the rule tables: `generated.rs` and `generated_html5.rs` are generated, `mod.rs` is
    hand-written), `encoding.rs`, `entities.rs`, and `mutate.rs`, an identity function unless built with
    `--features mutate` for `tools/mutate_rules.py`.
  - `page.rs`: the declarative `Page`/`field` to `Item` layer and `CompiledPage`. `python.rs`: the PyO3
    binding, feature-gated. `src/bin/{differ,bench}.rs` back the Python harness.
- `python/frostwork/`: the Python package. See `python/AGENTS.md`.
- `tools/`: the differential and benchmark harness. See `tools/AGENTS.md`.

## Principles to preserve

- **Correctness is parity, proven by a differential, in each mode.** Browser mode is graded against
  Chrome (`make gate-browser`, no divergence), lxml mode against lxml (`tools/diff_lxml.py`, 0
  non-whitespace DIVERGE and 0 CRASH). Run the gate before claiming a selector or feature works.
- **Encoding follows the browser, with no divergence budget.** For anything that decides which bytes
  become which characters (BOM handling, the `<meta>` prescan, label resolution, the decoder indexes) the
  target is what a browser renders, and w3lib, lxml, Parsel and the spec are evidence. Where an oracle
  disagrees, implement the browser's answer and assert the oracle's in `tools/enc_check.py`'s difference
  table, so an upstream fix fails as stale. Measure the browser, with exact bytes and headers, rather than
  quoting the spec at it.
- **Each mode matches its oracle empirically.** Lxml mode follows libxml2 2.15, not the HTML5 spec.
  Browser mode follows Chrome, not the spec, and where Chrome and Firefox differ it follows Chrome.
- **A parity gate names its mode.** Every test, tool and gate that asserts libxml2 or Parsel parity pins
  `parser="lxml"` (the `differ` bridge defaults to it); otherwise it grades the default and reads green on
  the wrong contract.
- **No fallback.** An unsupported query returns an empty column, never an error or a wrong value.
  Widening coverage means adding it natively and proving parity.
- **Local divergence, never global desync.** Accepted divergences (outer HTML as raw source, the ones
  COMPATIBILITY.md lists) are local. The tokenizer states that prevent offset desync (rawtext, comments,
  CDATA, encoding sniffing) are non-negotiable.
- **Byte and offset core.** The tokenizer works on `&[u8]`, and values are decoded lazily when emitted,
  with no whole-document UTF-8 validation.

## Releasing

Write the changelog entry under a `## X.Y.Z (unreleased)` heading, then:

```bash
bump-my-version bump minor    # rewrites Cargo.toml, Cargo.lock, pyproject.toml, dates the heading, tags
git push --atomic origin main X.Y.Z
```

The annotated tag runs `.github/workflows/publish.yml`: the whole CI workflow, abi3 wheels and an sdist,
artifact checks, a smoke install, PyPI Trusted Publishing and a GitHub Release. Once any file of a version
is on PyPI its tag is immutable: never delete, recreate or move it, and fix a failed release with a new
version. A release that failed before uploading anything is re-tagged instead. [docs/RELEASING.md](docs/RELEASING.md)
has the full process.

## Conventions

- **Do not add a `Co-Authored-By` trailer to commits.** Commit or push only when asked; the default branch
  is `main`.
- A new DIVERGE or NOVEL is a release blocker.
- `docs/BENCHMARKS.md` is canonical for benchmark numbers: cite it rather than re-quoting figures that
  drift. Never put an exact test count or pair count in prose; say what the gate proves.
- Pick the oracle per subsystem: values are Parsel/lxml (lxml mode) or Chrome (browser mode), selector
  acceptance is cssselect, encoding sniffing is the browser. w3lib differs from browsers in named places;
  those are asserted as differences in `tools/enc_check.py`, not chased, so check which side is
  browser-correct before adding a w3lib parity case. Decoder sweeps enumerate the whole byte space, never
  only the sequences one side calls assigned.
- Do not hand-edit `src/implied_close/generated.rs` or `generated_html5.rs`: regenerate them
  (`tools/gen_tree_rules.py`, `tools/gen_html5_rules.py`; `--check` gates on drift).
- Rust: exhaustive `match`, clippy clean. Measure before optimising; SIMD structural indexing and a
  tag-dispatch index were both measured and rejected, so do not retry them without a workload that shows a
  win.
- **Performance work: measure with `tools/ab_bench.py` and read the jitter column.** It interleaves two
  `bench` builds inside each cell and marks with `~` any delta inside the cell's own spread; two identical
  binaries differ by up to ±2.3% a cell, so a sub-2% cell is not evidence. Run nothing else meanwhile.
  Adding the benchmark pool that can see a change is part of the change. On a loaded machine, compare
  instructions retired instead of time.
- A pre-filter must be cheaper than what it guards for the schemas that cannot use it; both matcher filters
  are gated on a measured floor (`sig::BITS_MIN`, `AttrGate::MIN_NAMES`).
- `OpenElem`'s size is a performance lever: pushing an element copies the whole struct, so a new field is
  paid per element on every page. `stays_small_enough_to_push_cheaply` asserts the ceiling and names the
  benchmark to run before raising it.
