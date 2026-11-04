# tools/ — the differential/benchmark harness

Python harness (needs `parsel`). `oracle.py` guards the oracle toolchain (libxml2 ≥ 2.15 — a pinned
lxml does *not* pin its vendored libxml2); `diff_lxml.py` is the gate; `conformant.py`/`families.py`/
`foreign.py` generate test pages; `enc_check.py` (encoding parity) with `enc_cases.py` (every document it
grades, which `tests/test_encoding_inputs.py` also runs through each Python entry point) and `decoder_sweep.py`
as its importable halves — the two-byte
enumeration and the four disagreement classes live there because `enc_check` runs its gate at module
level, so nothing in it could be exercised by a test, and a sweep that quietly narrows reads exactly
like a clean run; `sel_fuzz.py`/`diff_fuzz.py`
(selector + malformed-HTML fuzz), `soak.py` (multi-seed soak), `support_snapshot.py`
(regenerates/checks `docs/SUPPORT_SNAPSHOT.md`), `abi3_smoke.py` (stdlib-only check that the
extension works with no dependencies present), `ab_bench.py` (A/B two `bench` builds by
**interleaving** them inside each cell, min-of-reps, reporting each cell's own jitter; aborts if the two
builds disagree on the VALUE COUNT — and `--corpus <dir>` runs the same comparison over REAL pages with
their own production selectors, which is the mode that matters: the generated tables scored the
signature filter at −13% to −55% and the corpus scored it at zero, while the tokenizer fix the tables
could not see was worth −75% on a real page),
`bench_matrix.py`/`bench_corpus.py`/`bench_mem.py`/
`bench_webpoet.py` (benchmarks; the last one times `to_item()` at the PAGE-OBJECT level and verifies both
items are identical before timing either; `--boundaries` measures the three shapes where the healthy-path
curve does not hold, one of which Parsel wins outright, because a benchmark that only reports its good cases
is a brochure).

**Every benchmark compiles the schema ONCE, outside the timed loop** — `src/bin/bench.rs` included. A page
object is a class and its selectors are compiled at class-definition time, so timing a per-response re-parse
measures a program nobody writes, and its cost falls hardest on the schemas with the most fields. Do not
add a second "pessimistic" row beside it: publishing a configuration nobody runs reads as honesty and does
the opposite, inviting the real number to be discounted as the optimistic case. One Frostwork row, compiled.

Three decision functions across these tools exist because they decide a PUBLISHED number, so
`tests/test_gates.py` can seed each one: `bench_engines.coverage_gap` (a refusal the oracle shares is not a
coverage gap — two independent percentages only bound the set difference), `bench_matrix.assert_cell_extracts`
(a timed cell with selectors must extract values; six published cells extracted none) and
`diff_lxml.oracle_empty_basket` (a pair the oracle answers nowhere can only catch an over-match).

`bench_engines.py` is the COMPETITIVE benchmark (`make bench-engines CORPUS=<dir>`, competitors pinned in
`requirements-bench.txt`): the same corpus against selectolax/lexbor, raw lxml+cssselect and bs4+soupsieve,
because everything else here measures Frostwork against the incumbent rather than against the fast end of
the field. Three things it does that a speed table normally does not, each because the alternative reads as
a win that was not measured. **Nothing is timed before its values are checked** — every column goes through
`diff_lxml.verdict`, imported, so AGREE means what it means in the gate. **An engine that cannot express a
selector does not get a cheaper workload**: `workload_columns` is the one decision function behind both
timed scopes (`W-all`, and `W-common` = the columns every engine expresses AND gets right), and
`assert_same_work` re-checks the invariant per page. **Coverage and its reasons are reported separately**,
since "expresses 73% of production selectors" is the other half of "can I use this?". The CSS-only engines
are driven the way their users drive them — match, then read text off each match — which is NOT lxml's
node-set semantics; where that differs the column diverges out of the shared workload rather than being
quietly reproduced, because no scraper writes the document-order merge. `tests/test_gates.py` seeds a wrong
column, a mismatched workload and an absent competitor into those three functions. `bench_mem.py --engines`
runs the peak-RSS side over the same registry: lexbor's DOM is much leaner than libxml2's, which is exactly
the kind of thing a throughput-only comparison hides.

Five files own the **web-poet integration**: `webpoet_cases.py` is the shared registry (which processors
are covered, how a page object reaches each, which gate proves it — read by the two gates below so they
cannot drift), `webpoet_structure.py` is the one structural signature everything compares parsed HTML with
(the differential's raw-source allowance, the benchmark's parity check and the node-handoff sweep all read
it: a second comparator is a second standard, and the benchmark's collapsed whitespace), and the other three
mirror the engine's derive/audit/mutate trio
because the integration made the same mistake the engine's rule tables did — hand-written lists that
omitted something. `diff_webpoet.py` is the gate (build a `FrostPage` and an equivalent parsel `WebPage`
per generated schema, diff `to_item()` on the WHOLE item, across the class-shape × response-type matrix);
`webpoet_surface.py` DERIVES the four upstream surfaces from the installed libraries by introspection —
page bases, `web_poet.field` keywords, zyte processors, processor input types — and regenerates/checks
`docs/WEBPOET_SURFACE.md`, failing on any name that is upstream and in neither the covered nor the declined
list; `mutate_webpoet.py` breaks one load-bearing line at a time and asks whether the gate notices. That
last one earns its keep: its first run found two holes in `diff_webpoet` (no generated field carried a
`.map()`, and no bare-element field was `all=True` with a processor), because a mutation the differential
misses is a hole in the differential. It also NAMES the logic it cannot reach — everything inline in
`__init_subclass__` — so "0 survivors" is not read as "the module is covered".

Two things a later review round added to that trio, both about the gates rather than the code.
`diff_webpoet` now ends in COVERAGE FAILURES as well as divergences (`coverage_failures`): a green run means
either everything agreed or nothing ran, and a class shape that graded 0 pairs, a processor row whose
expected value was empty everywhere, and a `REQUIRED_COLUMNS` combination the generator stopped emitting
were all reported rather than gated. And `mutate_webpoet` runs THREE detectors (`diff`, `unit`, `surface`)
and prints which mutations the differential missed — because one of them genuinely cannot be seen there: the
real zyte processors are too lenient to notice a wrong node (clear-html renders the same text from a
`<title>` as from the `<html>` around it), so the element-universe sweep in `tests/test_python.py` is that
contract's gate, and a single-detector survivor list would have called it dead code.

`browser_oracle/` is the prototype oracle for the proposed browser mode (`docs/BROWSER_MODE.md`,
section 5), and the one Node tool here (Node 22 or later, no packages). `probe.cjs` drives stock Chrome
over CDP and Firefox over WebDriver BiDi, headless from throwaway profiles, and serves exact bytes with
`Content-Security-Policy: script-src 'none'`, the one way of blocking page scripts that leaves the
parser's scripting flag on. Its `equiv` mode compares a page load with `DOMParser`, `parseHTMLUnsafe` and
`srcdoc` node for node over a seeded generator that refuses to run unless every token is emitted (a float
LCG silently emitted 28 of its 60), and its `claims` mode evaluates `claims.json` on all three.
`lab_compare.py` grades the engine against the encoding-hacks browser column with three encoding labels,
and once through `Page.extract_response` on a web-poet `HttpResponse`.
`evidence/<date>/` holds the outputs the proposal quotes, each stamped with its browser build.

`probe.cjs serve` is the oracle the gates call. It keeps one browser running, reads one JSON request per
line on stdin (`{"id", "docs", "selfcheck"}`) and answers each with one line carrying a lossless tree per
document and the path it took. `FAST_PATH_ELIGIBLE` is the only routing rule: a document with neither
`noscript` nor `shadowrootmode` goes through `DOMParser`, and everything else is page-loaded from the
in-process CSP server across a pool of tabs. With `selfcheck`, each eligible document is page-loaded too
and any tree that differs is reported, so the fast path is trusted only as far as the run re-measures it.
`browser_oracle/client.py` wraps it as `BrowserOracle("chrome" | "firefox")`, a context manager that
batches documents, records the browser build and kills the process (and through it the browser) on exit;
the binaries come from `FROSTWORK_CHROME`/`FROSTWORK_FIREFOX`, else the macOS install paths. A null tree
is an oracle error and is kept in `errors`, never dropped.

A `serve` request may also carry CSS: `plans` (a table of `css.py` plans) and `queries` (per document,
indexes into it), answered as `css` on the same document the tree was serialised from. `bodies` (base64)
with `ctypes` serves exact bytes with that Content-Type and always page-loads, and every reply carries
`charsets`, the `document.characterSet` each document was decoded with. `BrowserOracle.query(docs,
selectors)` is the client side: it plans each selector once, sends the table, and returns `(trees,
answers)`, where an answer is a list of values or a `Refused` naming who could not answer (the planner or
the browser). A `Refused` is never graded and never reads as an empty column. With `selfcheck`, the CSS
answers of both paths are compared as well as the trees.

`browser_oracle/css.py` plans parsel's CSS for `querySelectorAll`. The browser decides what matches,
attribute case folding and quirks-mode class matching included; what the browser has no syntax for is
parsel's and is mirrored from parsel's own translator: `::text` is the matched element's text-node
children, `::attr(name)` the attribute named exactly `name`, a pseudo-element after a descendant
combinator and a bare universal (`p ::text`, `div *::text`) is the subtree form, a comma group is a union
in document order, and `:contains()` becomes a marker attribute for the duration of one query. Every plan
is checked against parsel before it is sent: the native CSS with the pseudo-element put back must
translate to parsel's exact XPath, or the selector is `Unplannable`. `grouped` plans answer parsel's
`node.css()` relative to each container match.

`browser_oracle/bridge.py` is the lossless XPath bridge: the browser's JSON tree as an lxml tree, so
browser-mode XPath is lxml's XPath over the browser's tree (`//svg` finds SVG, `//P` finds no `<p>`). What
lxml refuses (C0 controls, U+FFFE/U+FFFF, surrogates; `"&'/<>`, whitespace or a leading `{` in an element
name; `--` in a comment) maps one character for one character into a private-use plane the document never
uses, and XPath queries pass through the same map, so literals still match and `string-length()` stays
exact. Every answer is decoded back, and an element is answered with the browser's own `outerHTML`
serialisation of its JSON node, because the lxml tree is the document view and lxml would serialise a
`<template>` empty. Namespaces, doctypes, template content and shadow roots live beside the lxml tree, so
`to_json()` rebuilds the browser's tree exactly. What no map can hold (two adjacent text nodes, which lxml
would merge) raises `Unrepresentable` and is counted, never graded through lxml. `round_trip_failures` is
the round-trip gate's decision function, seeded in `tests/test_gates.py` with lossy bridges.

`browser_oracle/tree.py` evaluates the fingerprint's XPath columns over the browser's JSON tree itself,
with lxml's XPath semantics. `document_view` drops HTML template content and shadow roots, which no
selector reaches. The evaluator implements only the shapes `seq_sweep.fingerprint_selectors` emits and
raises on any other, and `from_lxml` converts lxml's own tree to the same format so
`tests/test_gates.py` can prove the evaluator returns parsel's answers on lxml's trees. Widen the
evaluator and that test together whenever the fingerprint gains a shape. Every other XPath column goes
through the bridge, and `diff_browser` checks on every run that the two agree on the fingerprint.

`diff_browser.py` is the browser differential (`make gate-browser`). It grades the engine on
`seq_sweep`'s fingerprint against Chrome's tree, over `seq_sweep`'s short sequences plus seeded random
documents from an HTML5-heavy alphabet in which every start tag carries an id, and it refuses to grade
unless every token is emitted. Each differing column is reduced to the smallest token subsequence that
still differs on it (ids survive the reduction, so the column names the same element) and the reduced
document is attributed to a row of `CONSTRUCTS`. Attribute by the repro, never by the page it came from.
The only rows left are the constructs browser mode cannot stream, because their rule changes a decision
the matcher has already made (the adoption agency's moves, fostered content's document order, a second
`<html>`/`<body>`, a `<frameset>` replacing a body, `<selectedcontent>`). Anything else is NOVEL. `--gate` fails on NOVEL and on coverage: nothing graded,
a token never emitted, an oracle error, or a self-check difference. `gate_failures` and `attribute` are
its decision functions, seeded in `tests/test_gates.py`. With `--browsers chrome,firefox` the columns
where the two browsers disagree go into their own reduced table and the engine is graded against
Chrome there. `--parser` (default `browser`) is passed to `frostwork.extract` only when the installed
extension accepts it, and the run prints which mode it graded. `seq_sweep.parser_kwargs` is the one probe
for that keyword: `seq_sweep` and the engine calls in `tests/test_gates.py` pass `parser="lxml"` through
it, so lxml mode's gates keep grading libxml2 behaviour whatever the default mode is. When one of those
constructs becomes streamable, delete its row in the same change, so the gate holds it at zero.

`diff_browser.py` also grades VALUE columns, over the same documents plus `--values` decorated ones:
ALPHABET tokens carrying `VALUE_ATTRS` and `VALUE_TEXTS` (case variants of the attributes HTML folds,
class lists with form feed and Unicode whitespace, NUL, entities, control characters), under the same
token-coverage refusal, plus every `TAIL_CONTEXTS` x `TAIL_BODIES` document: a deferred winner
(`li:last-child b::text`, `tr:has(b) td::attr(id)`) whose content depends on what is open outside it (a
table, a formatting element reconstruction copies in, an SVG or MathML integration point), which is where
re-scanning a winner's source alone goes wrong. The basket is `conformant.BASKET`, `RULE_SELECTORS` and
per-id `::text`/`::attr`/descendant-text columns, minus what the engine refuses in the mode under test.
`engine_refusals` asks about each selector alone, because one `check` over a whole run is one schema past
the column ceiling and reports selectors refused that are not; `corpus_browser.py` reads the same helper.
CSS is answered natively
and XPath through the bridge, on the tree the fingerprint was read from. A value column that differs where
the fingerprint already differs is filed under the TREE, which that gate holds. Where the fingerprint
agrees, it is reduced with the fingerprint still agreeing at every step and attributed to
`VALUE_CONSTRUCTS` (case-insensitive attribute values, quirks-mode class matching, raw-source outer HTML),
which read the oracle's tree rather than the page; never add a value construct to `CONSTRUCTS`. A value
column about an id-less element no fingerprint column can see (the empty `<p>` a stray `</p>` inserts) is
attributed through `CONSTRUCTS` as a tree difference only the value column found. Anything else is value
NOVEL and fails the gate. So do: a bridge round trip that changed a tree, bridge XPath that answers a
fingerprint column differently from the native evaluator, native CSS that answers differently from
parsel's `.css()` over the bridged tree where the two semantics coincide (the planner's standing check:
no-quirks documents, no folded attribute, no form feed in a class), a CSS self-check difference, and a
value-rule selector the oracle answered nowhere or the engine refuses. Decorations go before the id slot
and are undone by id for tree attribution (`undecorate`/`redecorate`), so the construct rules see
ALPHABET tokens and a counterfactual edit keeps its decorations.

`diff_webpoet.py --parser browser` (`make gate-webpoet-browser`, outside `ci`) builds browser-mode page
objects and takes the expected item from Chrome: the parsel page objects keep their getters, `Processors`
and `out=`, and their values come from native CSS or the bridge. `.as_node()` must be refused at class
definition in browser mode, so a schema with a node-taking processor is checked for that refusal and then
graded without those fields; an accepted one fails the gate, and a refusal no run provoked is a coverage
failure (`required_refusals`). A field whose engine column already differs from the browser's is ENGINE,
not DIVERGE: that divergence is gate-browser's, and this gate grades the page-object layer.

`corpus_browser.py` (`make gate-corpus-browser [CORPUS=<dir>]`) is the report-only corpus view of browser
mode: each page of a `bench_corpus.py`-layout corpus is served as exact bytes with its Content-Type (a
`<page>.content-type` sidecar, else `text/html`) and page-loaded, and every selector is graded against the
engine given the same header. A page Chrome and the engine decoded differently is reported as such and
graded on Chrome's decoding, since encoding has its own gates. A differing column is TREE where the page's
fingerprint over its own ids differs, VALUE where it agrees over at least one id, and UNSPLIT on a page
with no ids. Nothing is reduced or attributed: a corpus page is not a token sequence. `column_verdict` is
its decision function, seeded in `tests/test_gates.py`.

`gen_html5_rules.py` is browser mode's counterpart of `gen_tree_rules.py`. It derives the HTML5 element
categories (special, formatting, the five scope lists, the two implied-end sets, void, data mode) from
Chrome over the same `ELEMENTS` universe and generates `src/implied_close/generated_html5.rs` whole. Each
category is read off one tree difference. A cell no tree can show is not guessed: a void element is never
on the stack, a table part exists only inside a `<table>` that is itself special and a scope boundary, and
a raw-text element has no end tag token. Those cells come from the WHATWG list, and the generated header
names each one with its reason. `</b>` and `<rb>` reach an element only through a scope check, so the scope
answers are read first and a default-scope boundary is reported unreadable for them. Three categories also
carry a witness, a second probe over a different algorithm (the adoption agency, `<button>` and `<p>`
start tags), and the two must agree wherever both read a name. Every category must read both ways on its
controls. `tests/test_gates.py` runs the same probes against libxml2, which has none of these rules, to
show the answers move with the parser. Firefox runs the same documents. Chrome is what gets generated, and
`--report` prints every difference from the spec and from Firefox. `--check` is offline: it compares the
Rust file with `browser_oracle/html5_derivation.json` (the stored per-cell answers) and checks a hash of
the probe documents, so `make py` needs no browser. `--check --live` re-measures in Chrome and runs in
`make gate-browser`. `--write` re-measures both browsers with the DOMParser fast path self-checked, and
refuses to write on a failed control or a fast-path difference.

Three tools own the tree-construction rules and share ONE element universe: `gen_tree_rules.py`
derives the rules from libxml2 and GENERATES the Rust table (`--check` gates on drift, `--report`
prints the derivation); `audit_tree_rules.py` asks lxml whether every rule cell is RIGHT, by value,
through the real engine; `mutate_rules.py` asks whether a WRONG one would be noticed (flips a cell
via the `mutate` cargo feature and reruns the gates). `corpus_fetch.py` fetches real pages into a
gitignored corpus.
