# tools/: the differential and benchmark harness

Python, needing the pinned toolchain in `requirements-test.txt`. The root AGENTS.md has the testing rules
these tools enforce; docs/TESTING.md has what each gate proves. A tool whose verdict decides a gate or a
published number keeps that decision in a function `tests/test_gates.py` can seed with a regression.

## Lxml-mode gates

- `oracle.py` guards the oracle toolchain: libxml2 2.15 or later, which a pinned lxml does not pin.
- `diff_lxml.py` is the gate (`make gate`). `conformant.py`, `families.py`, `foreign.py` and
  `encpages.py` generate its pages, the last one family per encoding label with selector literals in
  every position that crosses the raw-bytes boundary. `oracle_empty_basket` records the selectors the
  oracle answers nowhere.
- `seq_sweep.py` is `make gate-seq`, every tag sequence compared on the whole tree.
  `seq_sweep.parser_kwargs` is the one probe for the `parser=` keyword, so lxml-mode gates pin
  `parser="lxml"` whatever the default is.
- `sel_fuzz.py` and `diff_fuzz.py` are the selector and malformed-HTML fuzzers, `soak.py` the multi-seed
  soak.
- `gen_tree_rules.py` derives libxml2's tree-construction rules over the one element universe
  (`ELEMENTS`) and generates `src/implied_close/generated.rs` (`--check` gates on drift, `--report`
  prints the derivation). `audit_tree_rules.py` asks lxml whether every rule cell is right, by value,
  through the real engine. `mutate_rules.py` asks whether a wrong one would be noticed, flipping a cell
  through the `mutate` cargo feature and rerunning the gates.
- `corpus_fetch.py` fetches real pages into a gitignored corpus.

## Encoding

`enc_check.py` is encoding parity. Its importable halves are `enc_cases.py` (every document it grades,
which `tests/test_encoding_inputs.py` also runs through each Python entry point) and `decoder_sweep.py`
(the two-byte enumeration and its four disagreement classes). They are separate modules because
`enc_check` runs its gate at import, where a test cannot reach it. `tests/test_encoding.py` runs
`enc_cases.documents()` through `frostwork.encoding` as well.

`browser_decoders.py` is the same space against the browser, in `make gate-browser`: every legacy
decoder's lines compared with Chrome's (and Firefox's with `--browsers`). `BROWSER_BUGS` lists Chrome's
decoder bugs with Chrome's answer, where the engine keeps WHATWG's; `gate_failures` is its decision
function. It names what it cannot reach: ISO-2022-JP, gb18030's four-byte sequences, UTF-16.

## Benchmarks

`bench_matrix.py`, `bench_corpus.py`, `bench_mem.py` and `bench_webpoet.py` are the benchmarks. `bench_encoding.py`
times `frostwork.encoding` against Python's codecs, w3lib and charset-normalizer, and `bench_navigation.py`
`frostwork.navigation` against w3lib's base URL and meta refresh functions.
`bench_webpoet.py` times `to_item()` at the page-object level after checking both items are identical,
and `--boundaries` measures the shapes where the usual curve does not hold, one of which Parsel wins.

**Every benchmark compiles the schema once, outside the timed loop**, `src/bin/bench.rs` included,
because a page object compiles its selectors at class definition. There is one Frostwork row, compiled; a
"selectors re-parsed per call" row would time a program nobody writes.

`ab_bench.py` compares two `bench` builds by interleaving them inside each cell (min of reps) and reports
each cell's own jitter. It aborts if the builds disagree on the value count. `--corpus <dir>` runs the
comparison over real pages with their own production selectors, which is the mode that decides: generated
tables and real pages have disagreed on the sign of a change.

`bench_engines.py` is the competitive benchmark (`make bench-engines CORPUS=<dir>`, competitors pinned in
`requirements-bench.txt`) against selectolax/lexbor, raw lxml with cssselect, and bs4 with soupsieve.

- Nothing is timed before its values are checked: every column goes through `diff_lxml.verdict`.
- An engine that cannot express a selector does not get a cheaper workload. `workload_columns` decides
  both timed scopes (`W-all`, and `W-common`, the columns every engine expresses and gets right), and
  `assert_same_work` rechecks it per page.
- Coverage and its reasons are reported separately, and `coverage_gap` excludes refusals the oracle
  shares.
- The CSS-only engines are driven as their users drive them (match, then read text off each match). Where
  that differs from lxml's node-set semantics the column leaves the shared workload.

`bench_mem.py --engines` measures peak RSS over the same registry.

Other tools: `support_snapshot.py` regenerates and checks `docs/SUPPORT_SNAPSHOT.md`; `abi3_smoke.py`
checks the extension works with no dependencies installed; `retro_census.py` counts, per retroactive rule,
the share of a corpus's pages where browser mode replays (through the `differ`'s `R` op).

## The web-poet integration

- `webpoet_cases.py` is the shared registry: which processors are covered, how a page object reaches
  each, and which gate proves it.
- `webpoet_structure.py` is the one structural signature for comparing parsed HTML, read by the
  differential, the benchmark and the node-handoff sweep.
- `diff_webpoet.py` is the gate (`make gate-webpoet`): a `FrostPage` and an equivalent parsel `WebPage`
  per generated schema, `to_item()` compared on the whole item, across the class-shape by response-type
  matrix. It fails on coverage as well as divergence (`coverage_failures`): a class shape that graded
  nothing, a processor row whose expected value was empty everywhere, a `REQUIRED_COLUMNS` combination
  the generator stopped emitting.
- `webpoet_surface.py` derives the upstream surfaces by introspecting the installed libraries (page bases,
  `web_poet.field` keywords, zyte processors, processor input types) and regenerates and checks
  `docs/WEBPOET_SURFACE.md`. It fails on any upstream name that is neither covered nor declined.
- `mutate_webpoet.py` breaks one load-bearing line at a time and runs three detectors (`diff`, `unit`,
  `surface`), printing which mutations the differential missed. One contract is visible only to
  `tests/test_python.py`'s element-universe sweep, because the real zyte processors render the same text
  from a wrong node. The sweep names what it cannot reach: everything inline in `__init_subclass__`.

`diff_webpoet.py --parser browser` is `make gate-webpoet-browser`. The expected item comes from Chrome:
the parsel page objects keep their getters, `Processors` and `out=`, and take their values from native
CSS or the XPath bridge. Browser mode must refuse `.as_node()` at class definition, so a schema with a
node-taking processor is checked for that refusal and graded without those fields; a refusal no run
provoked is a coverage failure (`required_refusals`). A field whose engine column already differs from
Chrome's is ENGINE, which is `gate-browser`'s to hold.

## The browser oracle (`browser_oracle/`)

The one Node tool here (Node 22 or later, no packages).

- `probe.cjs` drives stock Chrome over CDP and Firefox over WebDriver BiDi, headless, from throwaway
  profiles. It serves exact bytes with `Content-Security-Policy: script-src 'none'`, the one way of
  blocking page scripts that leaves the parser's scripting flag on. A tab fetches only the document it
  was sent: a later navigation (a meta refresh, a frame) gets a 204 and any other request is blocked
  (`browser_docs.PAGE_LOADS` proves it), and the nonces that CSP hides are restored before the page is
  read. Page-side code reads every element property through its prototype's accessor, since a `<form>`'s
  named controls replace its own properties (`browser_docs.CLOBBER`). `equiv` mode compares a page load with
  `DOMParser`, `parseHTMLUnsafe` and `srcdoc` node for node, and `claims` mode evaluates `claims.json`.
  `evidence/<date>/` holds the outputs the design quotes, each stamped with its browser build.
- `probe.cjs serve` is what the gates call: one browser, one JSON request per line on stdin, one line back
  with a lossless tree per document. A document with neither `noscript` nor `shadowrootmode` goes through
  `DOMParser` (`FAST_PATH_ELIGIBLE`, the only routing rule); everything else is page-loaded across a pool
  of tabs. With `selfcheck`, eligible documents are page-loaded too and any difference is reported. A
  request may carry CSS (`plans` and per-document `queries`) and exact bytes with a Content-Type
  (`bodies`, `ctypes`); every reply carries `charsets`.
- `client.py` wraps it as `BrowserOracle("chrome" | "firefox")`, a context manager that batches
  documents, records the build and kills the browser on exit. Binaries come from `FROSTWORK_CHROME` and
  `FROSTWORK_FIREFOX`, else the macOS install paths. A null tree is an oracle error, kept in `errors`.
  `query(docs, selectors)` returns `(trees, answers)`, where an answer is a list of values or a `Refused`
  naming who could not answer. A `Refused` is never graded and never reads as an empty column.
- `css.py` plans parsel's CSS for `querySelectorAll`. The browser decides what matches; what it has no
  syntax for (`::text`, `::attr()`, the subtree form, comma groups, `:contains()`) is mirrored from
  parsel's translator, and every plan must translate back to parsel's exact XPath or it is `Unplannable`.
- `bridge.py` is the lossless XPath bridge: the browser's tree as an lxml tree, so browser-mode XPath is
  lxml's XPath over Chrome's tree. Characters lxml refuses map one for one into an unused private-use
  plane, queries pass through the same map, and answers are mapped back; elements are answered with the
  browser's own `outerHTML`. What no map can hold (two adjacent text nodes) raises `Unrepresentable`.
  `round_trip_failures` is the round-trip gate. `open_bridge` and `xpath_answer` return a `Refused` for a
  failed load, a tree lxml cannot hold or a query lxml rejects.
- `tree.py` evaluates the fingerprint's XPath columns over the browser's JSON tree with lxml's semantics,
  and only the shapes `seq_sweep.fingerprint_selectors` emits. `from_lxml` lets `tests/test_gates.py`
  prove it returns parsel's answers on lxml's trees; widen the evaluator and that test together. It also
  reads a tree's document mode (`is_quirks`) and lists its elements.
- `lab_compare.py` grades the engine against a recorded browser column of encoding cases (`--lab <dir>`)
  with three encoding labels, and once through `Page.extract_response` on a web-poet `HttpResponse`.
- `chrome_build.py` prints the Chrome build `html5_derivation.json` was derived from, which hosted CI
  installs from Chrome for Testing.

## Browser-mode gates

`diff_browser.py` is `make gate-browser`. It grades the engine on `seq_sweep`'s fingerprint against
Chrome's tree, over short sequences, seeded random documents and structured documents, and refuses to
grade unless every token is emitted. Each differing column is reduced to the smallest token subsequence
that still differs (ids survive reduction) and attributed by that repro. `CONSTRUCTS` has no rows: every
tree divergence is NOVEL and fails the gate. A construct that cannot be made exact gets a row and a
COMPATIBILITY.md entry; delete the row when it becomes exact. `gate_failures` and `attribute` are the
decision functions. With `--browsers chrome,firefox` the columns where the browsers disagree get their
own table, and the engine is graded against Chrome there. `--debug-differ` runs every graded document
through a debug `differ` and fails on a panic, since the builder's `debug_assert!`s are invariants a
release build can break silently.

`browser_docs.py` generates the documents and holds the column lists: `ALPHABET` (with mixed-case names,
formatting and special end tags, references and NUL in text, the markup declarations the tokenizer
special-cases, and `ARK` tokens sharing the id `k` so Noah's Ark has equal attribute sets to compare),
`VALUE_ATTRS`, `VALUE_TEXTS`, `TAIL_CONTEXTS`, `RULE_SELECTORS`, `GROUPED_COLUMNS` and `value_selectors`.
`structured_docs` adds what random runs rarely assemble: documents ending before the body or inside a
raw-text end tag, a comment in every position of three skeletons, formatting copies that share an
offset, Noah's Ark families, runs of divs at Chrome's depth cap, successive `<html>` and `<body>` tags
(`FRAME_MERGES`), and every attribute value a tree rule reads spelled literally and with a character
reference (`RULE_VALUES`).

The same run grades value columns: the `conformant.BASKET`, `RULE_SELECTORS` and per-id columns, over the
documents plus `--values` decorated ones and every `TAIL_CONTEXTS` by `TAIL_BODIES` document (a deferred
winner whose content depends on what is open outside it). CSS is answered natively and XPath through the
bridge.

- A value column that differs where the fingerprint already differs is filed under the tree. Otherwise
  it is reduced with the fingerprint agreeing at every step and attributed to `VALUE_CONSTRUCTS` in
  `browser_values.py`, which read the oracle's tree. Never add a value construct to `CONSTRUCTS`.
- Raw-source outer HTML must have the browser's start tag and the browser's text, in order, inside the
  engine's value; a value that lost content is not raw source. The allowances for a merged `<html>`/`<body>`
  and for a `<selectedcontent>` copy each excuse exactly that difference.
- Every value column is also asked of the engine alone and compared with its batch answer (`check_alone`),
  because which columns share a schema decides what the engine tracks.
- Also fatal: a bridge round trip that changed a tree, bridge XPath that disagrees with the native
  evaluator, native CSS that disagrees with parsel over the bridged tree where the semantics coincide, a
  self-check difference, a rule selector the oracle answered nowhere or the engine refuses, and a column
  nothing can ask.
- Decorations go before the id slot and are undone by id for tree attribution (`undecorate`,
  `redecorate`).

`browser_answers.py` holds what the browser gates share: `Engine` (frostwork in one parse mode), `Answers`
(every source's answer to every column), `same_values` (what AGREE means) and `engine_refusals`, which
asks about each selector alone.

`corpus_browser.py` is `make gate-corpus-browser [CORPUS=<dir>]`, a report-only view: each page is
page-loaded as exact bytes with its Content-Type (a `<page>.content-type` sidecar, else `text/html`) and
every selector graded against the engine given the same header. A differing column is TREE, VALUE or
UNSPLIT by whether the page's own ids show a tree difference. `column_verdict` is its decision function.

`gen_html5_rules.py` derives browser mode's element categories (special, formatting, the five scopes, the
implied-end sets, void, data mode) from Chrome over the same `ELEMENTS` universe and generates
`src/implied_close/generated_html5.rs`. Each category is read off one tree difference, three carry a
second witness probe that must agree, and every category must read both ways on its controls. Cells no
tree can show come from the WHATWG list, named with their reason in the generated header. `--check`
compares the file with the stored answers in `html5_derivation.json` offline (`make py`); `--check --live`
re-measures in Chrome (`make gate-browser`); `--write` re-measures both browsers and refuses on a failed
control or a fast-path difference; `--report` prints every difference from the spec and from Firefox.
