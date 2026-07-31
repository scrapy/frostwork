# Testing: how Frostwork's correctness is proven

Frostwork is a "close to lxml, no fallback" engine, so **the test harness is the spec**: there is no
fallback to hide behind, and every shipped divergence must be one we chose and can name. The suite
answers one question at scale — *on which inputs and selectors does the engine's value differ from
lxml, and is that difference a documented divergence or a bug?*

## Oracle and verdict

- **Oracle:** Parsel/lxml (`parsel.Selector(...).css(q).getall()`), the exact engine Scrapy ships,
  pinned to `parsel==1.11.0`.
- **The oracle is per SUBSYSTEM, and picking the wrong one hides bugs.** Values are Parsel/lxml. Selector
  *acceptance* is cssselect/lxml's own parser: a selector the oracle REJECTS must be reported unsupported,
  not merely answered empty. Encoding *sniffing* is **w3lib** — `parsel.Selector(body=…)` never looks at
  `<meta>`, it defaults to UTF-8, so oracling a prescan against it is vacuous: it agrees on every
  declaration you MISS, because both sides produce mojibake. That mistake hid five prescan bugs.
- **An oracle is not automatically the target.** For encoding the intended policy is browser/WHATWG
  correctness, and w3lib differs from browsers in nine named places, so `tools/enc_check.py` is split:
  a SHARED set compared against w3lib, and a difference table where Frostwork must produce the *browser*
  answer and w3lib must still produce the other one (so a difference fixed upstream fails as stale rather
  than becoming a hole). Treating the whole surface as w3lib parity would have made reproducing w3lib's
  bugs the only way to keep the gate green. Same shape as the cssselect `:is()`/`:has()` divergences and
  the Parsel-decoder byte lists.
- **Oracle version matters — "byte-identical to lxml" means libxml2 ≥ 2.14.** Tree construction is
  matched empirically to libxml2 2.14 (see [COMPATIBILITY.md](COMPATIBILITY.md)), and
  `requirements-test.txt` cannot pin that: a wheel *vendors* its own libxml2, and the same lxml release
  ships different ones per platform — lxml 6.1.1 carries libxml2 2.14.x in its manylinux/macOS wheels
  and **2.11.9** in the Windows wheel, where CR-in-attribute-values and a raw `<` in text parse
  differently. The same engine build therefore measures 0 DIVERGE on Linux/macOS and thousands on
  Windows purely from the oracle. `tools/oracle.py` asserts `lxml.etree.LIBXML_VERSION >= (2, 14)` and
  every harness entry point (`diff_lxml.py`, `enc_check.py`, both fuzzers) exits `2` with that
  explanation rather than reporting divergences the engine is not accountable for; pass
  `--allow-old-libxml2` (or set `FROSTWORK_ALLOW_OLD_LIBXML2=1`) to explore on such a platform anyway.
  The same caveat applies to diffing Frostwork against a *crawl* whose lxml is older.
- **Bar:** non-whitespace byte-identity per emitted value.
- **Verdict per (input, selector):** `AGREE` · `WS` (equal after `.strip()`) · `SKIP-EXPECTED`
  (diverges on a documented tree-construction construct — foster-parenting, adoption agency, deep-`<p>`)
  · `DIVERGE` (any other difference — a bug) · `CRASH` (engine panics — a bug).

The gate is **`DIVERGE + CRASH == 0`**, with `SKIP-EXPECTED` reported as the measured distance from lxml.

## Layers

**Unit vectors — `cargo test`** (milliseconds). One per rule and edge: every implied-close
family (`li`/`p`/`td`/`tr`/`dt`/`dd`/`option`) × {closed, omitted}; void/self-closing; rawtext with
`<`/`&` inside; comments/CDATA/DOCTYPE; entity edges; attribute quoting/case; selector compounds,
combinators, `:not()`, comma groups; encoding resolution; XPath compilation; the `Page` layer; budget
safety; and a regression vector for every bug the fuzzers have found.

**Tokenizer conformance** (`src/tokenizer.rs`, `cargo test tokenizer`). The states that would cause a
*global* offset desync must be exact. A recording `TokenSink` pins the event stream for each: every DATA
MODE (raw text keeping `</b>`/`<div>` as text in `script`/`iframe`/`noembed`/`xmp`, RCDATA in `<title>`,
PLAINTEXT running to EOF with no end tag, and `listing` as the control that looks like raw text and is
not), script's escaped/double-escaped states, comment close boundaries (incl. `<!-->` / `--!>`), CDATA
emitting nothing, character references left raw for the matcher, attribute forms, and `<`-not-a-tag
staying literal.

**Differential vs lxml — `tools/diff_lxml.py`** (the core gate). Generators emit structurally diverse
HTML; every page runs a selector basket through the engine (via the `differ` binary) and Parsel, and
each pair gets a verdict:
- **conformant** (`tools/conformant.py`) — random *content-model-valid* trees. On such input the
  corrected stack equals lxml's tree, so this must be byte-identical (the safety invariant).
- **families** (`tools/families.py`) — optional-end-tag constructs tagged SHOULD/SKIP/CONTROL, so a
  divergence auto-classifies as bug vs documented SKIP.
- **foreign** (`tools/foreign.py`) — `<svg>`/`<math>`/`<template>` subtrees (self-closing leaves,
  camelCase names, rawtext in foreign content).
- **grouped** — single-pass `Many`/`One` vs Parsel's per-container loop.

**Encoding parity — `tools/enc_check.py`.** (encoding × selector) cases across windows-1252, shift_jis,
euc-jp, gbk, big5, koi8-r, utf-8, plus UTF-16 sniffing, vs Parsel given the same label. Charset
RESOLUTION is oracled against **w3lib**, not Parsel (see the per-subsystem rule above), in two parts: the
SHARED set that browsers and w3lib agree on, and an enumerated table of the deliberate browser-correct
differences, each row asserting both sides so it cannot rot into a hole.

Those label-parity vectors are all ordinary text, and that turned out to matter: fuzzing declaration forms against
w3lib surfaced that the two sides do not even use the same DECODER. Frostwork decodes with `encoding_rs`
(the WHATWG standard, what browsers use); Parsel uses Python's stdlib codecs via w3lib, which also
translates labels (`big5`→`big5hkscs`, `shift_jis`→`cp932`). A WHATWG index is **total**, Python's codecs
are not, so they disagree on bytes no ordinary vector contains. The difference is now measured and
enumerated rather than assumed away — 5 bytes for windows-1252/iso-8859-1, 11 assigned two-byte sequences
for big5, full parity on validly encoded text elsewhere — and gated in both directions, so a divergence
outside the list fails and a listed one that starts agreeing fails too. See
[COMPATIBILITY.md](COMPATIBILITY.md) for the contract. The lesson is the same one the CSS escapes taught:
**vectors made of realistic content only test the middle of the input space.**

**Differential fuzzing** (the malformed-input surface):
- `tools/diff_fuzz.py` — mutates conformant/foreign pages and the fuzz corpus into *malformed* HTML,
  then diffs against lxml. Catches tokenizer desync the well-formed gate can't reach. `CRASH` is gated
  absolutely. Raw `DIVERGE` is expected here, so each one is **attributed** to the documented construct
  that explains it (foster, misnest, deep-`p`, head-in-body, fragment, outer-HTML, truncated-tag);
  whatever no construct explains is reported as **NOVEL** and gated on a *rate*
  (`--novel-budget`, default 0.05% of pairs).

  A construct leaves that list the moment it is IMPLEMENTED, or it becomes an excuse: `nested-form` was
  documented until `<form>` closing an open `<form>` came in with libxml2's start-close table, and leaving
  it there would have made a regression in that rule invisible. `tests/test_gates.py` pins both it and
  `unmatched-end` out of the set.

  This split exists because a bulk "DIVERGE is expected" bucket is where a real bug hides. The
  dropped-end-tag text split sat in this tool's output clustered under a `foster` mutation signature,
  indistinguishable from the accepted adoption-agency cases. Attribution separates the two, and the rate
  is the ratchet: 0.250% before that fix, 0.049% after it, and **0.010–0.014% now** (seeds 0-3 at 6000
  iters) once raw NUL started being deleted before tokenization and the missing data modes stopped
  fabricating elements. Both of those were IN this tail, which is why the budget came down with them —
  **tighten the budget to work the tail down; do not raise it to make a run pass.** What is left is
  malformed-input framing: a corrupt `<html>` root, no implied `</head>` before `<body>`, a truncated
  attribute swallowing the rest of the document.
- `tools/sel_fuzz.py` — fuzzes the *query* (valid / exotic / escaped / malformed / budget-bomb) against
  real pages and asks the real compiler whether each selector is supported. A promised-supported selector
  may not lose oracle values, and an unsupported selector may not emit anything. Gates `WRONG` +
  `OVERMATCH` + `CRASH`.

  **A generator can only find bugs in syntax it emits.** CSS escapes — which cssselect *decodes*, so
  `.\63 1` is `.c1` — appeared in no generated selector, so the entire escape surface was covered by
  hand vectors alone, and a review found it treated as literal text. Adding an escape family caught a
  further real bug the hand vectors had missed: `::attr(data-\6b)` was reported SUPPORTED and then
  matched literally, returning an empty column for a selector parsel answers (the identifier validator
  only inspected the first character). When you add a parser rule, ask what SYNTAX the fuzzer never
  writes — that is the blind spot, and it is a different question from which selectors are supported.

  Its escape probes deliberately use substring/prefix operators over `class`/`data-k`/`href`, which every
  generated page carries. The first version used exact-match values (`[data-k="\76 1"]` for
  `data-k="v1"`) and only discriminated on pages that happened to hold that exact value — measured
  against the pre-fix build it failed on 3 seeds out of 4 and **passed a broken engine on the fourth**.
  Retargeted it fails every seed by 26–29 pairs. A new family's discrimination is a number to measure
  over several seeds, not a property to assume from one red run.

**Generated rule tables — `tools/gen_tree_rules.py --check` (in `make py`).** This tool asks libxml2 for
the whole (open × incoming) start-close relation, the void set and the per-element data mode over a fixed
element universe. The first two are then RENDERED as Rust: it partitions the names into behaviour classes
and writes `sc`/`sc_id`/`start_closes`/`is_void` into the generated block of `src/implied_close.rs`, so
those tables are derived rather than written. `--check` fails if source and oracle have drifted;
`--write` regenerates; `--report` prints the derivation (universe size, observable vs unobservable cells,
the classes and what each closes). The data-mode table in `src/tokenizer.rs` stays hand-written — it is
nine names — but the derivation is what the audit checks it against, name by name over the whole
universe, and `mode:<name>` mutants cover every name in the mutation sweep.

Three things it does that a hand-written table cannot. It reports a cell as **unobservable** rather than
guessing — a void or raw-text element can never be the open element when a start tag arrives, so those
cells are unreachable by construction, and saying so is different from asserting `false`. It probes the
`head` column at all, which needs its own scaffold (`<head>` cannot appear inside the body-context
wrapper the other cells use) and was therefore missing entirely. And it proves its universe is a superset
of both the engine's names and an outside element index, which is the containment the audit's earlier
hand-written lists kept failing.

**Tree-rule audit — `tools/audit_tree_rules.py` (in `make py`).** Coverage of *pages* is not coverage of
*rules*. The differential proves parity on the pages it generates, so a rule no generated page exercises
is asserted, not tested — and that is precisely where bugs were found: the `dd`/`dt` and `rt`/`rp`
same-tag closes shipped wrong, then an audit of the remaining cells found **19 more wrong cells and a
missing table-scope rule**, all in regions no generated page reached (`optgroup`, `thead`/`tfoot`/
`caption`, and `<p>` followed by a non-closer) — and widening the audit's universe afterwards found 12
more (`colgroup` had no rule at all, and `caption` was wrongly a scope boundary). So this walks the rule
tables *directly* — the start-close relation, the void set, the per-element data mode, the `<p>`-closing
set, table scope, the document frame — and asks lxml about every cell, printing the cell count it covered.
It is fast and deterministic (tens of thousands of cells in about two seconds), so it gates.

### The universe is the thing that keeps being wrong

Read the history of this audit as one repeated mistake, because it is: **every hand-written list of tag
names omitted something, and a rule with no name to probe cannot fail a gate.**

| round | what was missing | what it hid |
|---|---|---|
| 1 | the universe was the engine's own tag ids | `colgroup` had no rule at all |
| 2 | 16 tag NAMES vs the engine's 19 tag IDS | 87 pairs of libxml2's `htmlStartClose` |
| 3 | the one name `s`, "covered by a representative" | 93 unprobed cells |
| 4 | `head`, `listing`, `xmp`, `plaintext` | whole missing rows/columns: `<body>` nested inside `<head>`, `<dd>` inside `<listing>`, `<iframe>`/`<xmp>` content parsed as markup |

Round 4 is the one that ended the pattern, because the answer stopped being "add the names we forgot":

- there is now **one** universe (`tools/gen_tree_rules.ELEMENTS`), read by the generator, the audit and the
  mutation sweep, so there is no second list to forget a name in;
- it is checked to be a **superset** of both the engine's own names and an *independent* element index
  (lxml's tag definitions plus the stdlib `html.parser`'s CDATA list). The containment runs in that
  direction on purpose: deriving the universe FROM the engine is what made every earlier round
  self-referential — a rule the engine simply does not have has no name to enumerate;
- and the rule table itself is **generated** from the oracle over that universe
  (`tools/gen_tree_rules.py --check` gates on drift), so "complete" is a property the build checks rather
  than a claim in a comment.

Two probe-design lessons are also baked in. Each element's scope contribution is tested **bare**:
wrapping every candidate in `<table>` masked their own behaviour, because the table blocks regardless,
which is how a wrong `caption` scope entry survived. And the wrapper is an unknown element, since a
`<div>` wrapper confounds every cell whose open element is also a div.

**When you add a tree-construction rule, add a row to that audit.** A rule with no row is a rule on
trust, and a family that "looks covered" because a sibling tag is generated is the exact trap here.
Equally, when you add a differential family, check it actually *discriminates*: the first `optgroup`
family passed against the known-buggy engine because `optgroup` has no direct text, so every column was
empty either way. Run a new family against the pre-fix build and confirm it fails — and be precise about
what "fails" means. A parser gap makes selectors report UNSUPPORTED, and empty-when-unsupported is what
the no-fallback contract *permits*, so `tools/sel_fuzz.py`'s new quoted-delimiter family moves ~250 pairs
per seed out of AGREE without turning the gate red; the assertion that those shapes are SUPPORTED is a
contract sweep in `tests/test_python.py`. Measure which of your two halves actually goes red.

**Real-page parity, fetched — `make corpus-real`.** The gap above is real pages, and the reason it stayed
open was licensing and size, not difficulty: third-party HTML cannot be committed here. So
`tools/corpus_fetch.py` fetches it instead, into the gitignored `fixtures/realweb`, and derives a selector
basket from each page's own classes and ids (a generic `h1::text` basket produces mostly-empty columns,
which prove nothing). `make corpus-real` fetches and then gates.

The current list is ~30 pages across ~24 sites, chosen for GENERATOR variety rather than traffic: Sphinx,
MkDocs, Doxygen (two vintages), Javadoc (two vintages), rustdoc, godoc, Docusaurus, Hugo, Jekyll,
Asciidoctor, texinfo and troff→HTML, MediaWiki, plus two scraping sandboxes. The texinfo and troff pages
matter out of proportion to their traffic: they are `<dl>`-heavy and omit end tags freely, which is the
exact shape that produced the `dd`/`dt` and dropped-end-tag bugs. Measured over that corpus: 720 columns,
**429 of them carrying values, 177,704 values compared byte-for-byte, 0 divergences**.

Read that number with its limits. It is one page per site, so it samples site variety, not the long tail
of any one site's templates — a real crawl is still the thing this cannot replace. The pages are also a
moving target: re-fetching next month diffs different bytes, which is a feature for finding bugs and
useless for bisecting one, so `MANIFEST.json` records the URL each page came from.

**Real-page parity, your own corpus — `make gate-corpus CORPUS=<dir>`.** `make gate` only ever sees *generated* pages,
and a generator reproduces the malformations its author thought of: the `dd`/`dt` same-tag close and the
dropped-end-tag split were both found on real pages while the generated gate read 100%. This runs the
gate's own verdict over a real corpus (`<dir>/<page-object>/{selectors.json,pages/*.html}`) and exits
nonzero on any value bug. It defaults to `tests/corpus` — a small SELF-AUTHORED set shaped like the
doc-generator and table markup that broke the engine, which is verified to discriminate (the tabular page
object alone reports 6 divergences against the pre-fix engine). That is not a substitute for a real crawl
corpus — no real/third-party pages are vendored (licensing and size), and fixtures only encode the bugs
already known, which is the same limitation the generators have.
`SEGMENT` (same text, extra node split) counts as a bug, not a cosmetic
difference — a `One` field takes `col[0]`, so an extra split truncates it. No corpus is vendored in this
repo. Doc-generator output is worth including, not just commerce pages.

**Would a wrong rule be NOTICED? — `tools/mutate_rules.py` (`make gate-mutate`).** The audit above asks
whether every rule cell is RIGHT. This asks the other half: **if a cell were wrong, would anything go
red?** It flips one cell at a time (via the `mutate` cargo feature, so one build serves every mutant
instead of one rebuild each) and records which gates notice. It is the only check here that finds blind
spots without a human first guessing where they are — and every gap closed before it was found by someone
reading code and thinking "hold on, nothing covers that", which had already missed things three review
rounds running.

**Mutate the ANSWER, not a table.** The first version flipped cells in one rule table at a time, and 51
mutants survived every gate while the behaviour was in fact protected: two tables feed the close decision
(`implies_close_id` over tag ids, `start_closes` over libxml2's finer name pairs) and the matcher ORs
them, so mutating either alone is masked wherever the other closes the same pair. A survivor list padded
with false alarms is worse than no list — it trains you to skim the one output that matters. So the hook
now inverts the *effective* close decision for a tag-name pair. Nothing can mask it, and the only
exclusion left is provable: a VOID element is never the open element, so `close:<any>,<void>` is
unobservable by construction.

The first sweep, over the implied-close id table alone, ran 425 mutants against five gates:

| gate | mutants it noticed |
|---|---|
| rule audit (`audit_tree_rules.py`) | 83% |
| unit vectors (`cargo test`) | 27% |
| differential vs lxml (reduced) | 25% |
| corpus, self-authored fixtures | 10% |
| corpus, real fetched pages | 5% |
| **no gate at all** | **13%** |

Two things in that table are worth internalising. **Most cells are caught by the rule audit and nothing
else** — for those the audit is a single point of failure, which is why a new rule needs an audit ROW and
not merely a passing differential. And the page-based gates are weak detectors of rule errors by nature:
real pages exercised 5% of the table, because ordinary markup simply does not contain most of these
constructs. That is not an argument against the corpus, it is the argument FOR the audit.

**What it found, twice.** The 55 first-round survivors had one root cause, and it was the same one as
twice before: **the audit's universe was drawn from the things we already believed were special.** Its
cross product was over 16 tag NAMES while the engine's table is over 19 tag IDS, three of which are not
names (`OTHER`, `BLOCK`, `TABLE`) — so "does `<div>` close an open `<dd>`?" and "does `<dd>` close an open
`<span>`?" were never asked. Widening it (plus the scope universe, which was missing `dd`/`dt`/`rp`) turned
up **87 real divergences**: libxml2 decides the close question from a hardcoded NAME-pair list
(`htmlStartClose`) finer than the engine's ids — `<td>` closes an open `<b>` but not an `<em>`, `<table>`
closes an open `<h1>` but not a `<div>`, `<a>` and `<form>` close a same-tag repeat. All 87 were the same
direction, so that table was ported in full (`implied_close::start_closes`, generated from the oracle
rather than transcribed) instead of tolerated.

Then the sweep over the *composite* decision found 93 more survivors — every one of them the single tag
name `s`, missing from the audit's probe list in both directions, because it shares a behaviour class with
`big`/`small`/`tt` and "a representative is enough" had quietly become "a representative we remembered".
Adding it took them all to caught.

And then a review found four more names absent from BOTH the audit's list and this sweep's — `head`,
`listing`, `xmp`, `plaintext` — and with them whole missing rows and columns of the table. That is the
point at which "add the names we forgot" stopped being the fix: the universe is now one shared, proven
superset and the table is generated from it (see the round-by-round table above). The sweep's own name
list is no longer written either — the close dimension takes **one representative per oracle-derived
behaviour class** (a full name-pair sweep would be 142² mutants, about 13 hours), which is sound because the
classes come from the measurement and the AUDIT probes all 142 names individually. Two dimensions were
added at the same time: `void:` covers the derived void set plus the four names libxml2 deliberately keeps
open, and `mode:` covers the DATA MODE of every name in the universe — because "the raw-text set is the
four names we remembered" was the same bug in a different table.

Three probe-design lessons are baked into the tools, each of which cost a wrong answer first:

- **A `::text` probe cannot see inside a table.** Text in an open `<table>` is foster-parented, so the probe
  reads empty whether or not the table was closed — which is exactly why the `table`-as-open-element cells
  survived. An **attribute** is never foster-parented. Both probes now run; verified to agree cell for cell.
- **A known wrapper confounds the probe.** With `<div>` as the wrapper, every cell whose open element is
  also a `div` matches on the wrapper itself; a first pass read 72 spurious closes that way. The wrapper is
  now an UNKNOWN element, which libxml2 closes for nothing.
- **`if x == y: continue` silently deleted the diagonal.** One line of misplaced caution excluded every
  "does `<X>` close an open `<X>`?" cell, and with it the nested-`<a>` and nested-`<form>` closes.

### Proving a regression test discriminates

"Add a test" is not the same as "add a test that could fail", and the difference has bitten here more than
once. The mutation hook doubles as the cheap way to check: a `close:`/`void:`/`mode:` mutant reintroduces
exactly one shipped bug, so `FROSTWORK_MUTATE=<spec> cargo test --features mutate` must go red. Where no
hook exists, the equivalent is to disable the fix in place, run the one test, and restore. Both were done
for every fix in this round, and one test failed to discriminate and had to be rewritten: the UTF-16 NUL
vector put its NUL in an attribute *value*, which the value decoder strips anyway, so it passed with
document-level NUL deletion disabled. Moving the NUL into the tag name made it a real test.

| fix | how the test was shown to fail without it |
|---|---|
| data modes (`iframe`/`noembed`/`xmp`/`plaintext`) | `mode:iframe`, `mode:plaintext`, `mode:xmp` mutants |
| void set (`basefont`/`frame`/`isindex`) | `void:basefont`, `void:frame`, `void:isindex` mutants |
| start-close (`listing`, `title`, `body`→`head`) | `close:dd,listing`, `close:title,p`, `close:body,head` mutants |
| document-frame ignore + phantom end tag | `frame_tag_is_redundant` stubbed to `false` |
| raw NUL before tokenization | `strip_nul` stubbed to the identity |
| quoted delimiters in functional pseudos | the quote/escape branches removed from both scanners (the *support verdict* is what goes red — see below) |
| encoding: XML declaration, BOM-less UTF-16, `x-user-defined` | each of the three removed from `resolve`/`prescan_label` independently |

**Contract sweeps over a whole syntax surface** (`tests/test_python.py`). One test per surface, each
asserting the same two-sided contract — **supported means parity with the oracle, unsupported means
empty** — over every shape in a grammar rather than a hand-picked few: the CSS selector surface, CSS
escapes, the XPath surface (50 shapes, including `position()`/`last()`/`not()`/axes/unions/non-literal
operands), the migration scanner's source shapes, and `:has()`/reverse-positional/text-predicate
combinations on randomly generated CONFORMANT pages. These are cheap (well under a second all together)
and they catch the failure the per-feature vectors cannot: a selector that quietly stops being supported
still "passes" a test that only asserts its value is empty.

Two probe-design traps are worth knowing before adding one. A sweep over MALFORMED generated pages
measures the documented divergences (deep-`p`, misnest), not the feature — the first version of the
deferred sweep reported 9 "failures" that were all foster-parenting and misnest. And a sweep that compares
only VALUES will report the no-fallback contract as hundreds of divergences: deferred predicates are
unsupported as grouped containers, so a grouped probe has to consult the support verdict, not just diff
against Parsel.

**Do the gates actually fail? — `tests/test_gates.py` (in `make py`).** Every gate above is a claim of
the form "if the engine regresses, this goes red", and that claim is itself untested code. It has been
wrong three times: `enc_check` printed MISMATCH and exited 0; `diff_fuzz` filed real divergences into a
bulk "expected" bucket; `bench_corpus` treated a supported selector losing values as a coverage gap and
passed. Each time a gate was green while the engine was broken — the worst failure mode available to a
test suite, because it converts a bug into a documented feature.

So this suite seeds a KNOWN regression into each gate's *decision function* and asserts it goes red:
`divergence_kind`/`is_value_bug` (corpus), `constructs`/`DOCUMENTED` (fuzz attribution), `verdict`
(differential), the batch size vs the engine's advertised member budget, and the w3lib prescan
comparison. It runs no engine and generates no pages, so it is fast enough to sit in `make py`.

A related habit, cheaper than any tool: **when a test passes, check it can fail.** `assert "w3lib" in
src` passed against a *comment* that mentioned w3lib. `if supported: assert parity` passes silently the
moment support regresses — assert the support verdict too. Both shipped here.

**Multi-million soak — `make soak`.** Runs the clean differential and support-aware selector fuzzer
over five independent seeds, followed by a larger malformed-input crash run. The default workload is
over four million page/query pairs and reports the exact aggregate before returning success.

**Coverage-guided fuzzing — `fuzz/`.** A `cargo-fuzz`/libfuzzer target feeds arbitrary bytes to
`extract` (flat, grouped, sniffed + explicit encoding) and asserts no panic/hang/OOB. Run:
`cargo +nightly fuzz run extract`. The corpus (`fuzz/corpus/`) and artifacts are git-ignored and
regenerated locally.

## Wiring

The engine builds a `differ` binary that reads hex-framed cases and emits NDJSON, so the Python
generators/oracle (which own lxml) drive it without a PyO3 build. CI runs the unit tests, clippy, the
differential gate, encoding parity, both differential fuzzers (crash-gated), and the Python suite on
every change; the coverage-guided `cargo-fuzz` target is run locally/nightly.

**The abi3 floor is a separate job.** The wheel is `abi3-py39`, but the pinned toolchain cannot be
installed on Python 3.9 at all — `parsel`, `web-poet` *and* `pytest` each require ≥ 3.10 — so the floor
runs `tools/abi3_smoke.py` instead: build the extension on a real 3.9 and exercise the dependency-free
surface (primitive, strict/permissive, `Page`/`Item` + groups, `check`, `--scan`) with nothing but the
standard library. Parity and the web-poet integration are covered by the jobs that own the oracle.
`tools/abi3_smoke.py` is also the quickest local answer to "does this build work on this interpreter".

## Python

`tests/test_python.py` (`.venv/bin/python -m pytest tests/test_python.py`) covers the primitive,
`Page`/`Item`, the web-poet wiring (including that a multi-field page object triggers exactly **one**
`extract` call), `Many`/`One`, and a Parsel cross-check. The cross-checks go through `_oracle()`, which
skips them unless the installed lxml carries libxml2 ≥ 2.14 — same rule as the harness, so an
environment with an older vendored libxml2 reports a skip rather than a spurious failure.
