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
`fuzz-smoke` + `py` (which now also type-checks the shipped package) + `gate-webpoet` (the web-poet
integration vs parsel, compared on the whole item, plus the derived upstream-surface snapshot) +
`gate-webpoet-mutate` — the minimum pre-release check. Individual targets run their own piece.

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
  dash, and documented divergences whose stated scope was too narrow. A 2000-page sample then found the
  next one down: end-tag scope is a PRIORITY comparison, not a set of boundary elements, so `</tr>` cannot
  unwind an open `<tbody>` and a table generator's rows lost their cells. A 10000-page sample then found
  three more, all in the document frame and all hidden by an EXCLUSION rather than a short list: the frame
  probes skipped the three frame names themselves ("asking where `<head>` nests is meaningless"), so a
  page whose first tag is `<head>` got no `<html>` at all, a `<body>` written after `</body>` was ignored
  where libxml2 starts a second one, and `<body>` — which out-ranks every end tag — was missing from the
  priority order because the derivation had ruled it "never open". Two more 10000-page samples then found
  three more, and the most valuable was in the TOKENIZER rather than the tree: only an ASCII letter after
  `</` starts an end tag, so `</%>` is a bogus comment that SPLITS a text node while `</>` is ignored and
  does not — reading both as "scan a name, skip to `>`" merged a page's copyright line and was also the
  single largest source of unattributed divergences in the malformed-HTML fuzzer (NOVEL 93 -> 5). The
  other two were the frame again: `<frameset>` ends the head but starts no body, and content after
  `</html>` gets a second ROOT `<html>` rather than no parent at all. A third 10000-page sample found four
  more, and two of them were not parsing rules at all: **Parsel does not parse the response bytes**, it
  parses `text.strip().replace("\x00","")` — the NUL half was already matched, and the missing STRIP half
  promotes a whitespace-preceded U+FEFF to offset 0, where libxml2 eats it as a BOM. On the bytes it is a
  character, a character before the frame opens the `<body>`, and a page that merely INDENTS its doctype
  loses its `<head>`, its `<title>` and the attributes of its own `<html>` tag. **And ISO-2022-JP is not
  ASCII-compatible** — `社` is the two bytes `<R`, so the byte tokenizer grew a start tag out of the middle
  of a Japanese word; the transcode set is now `Encoding::is_ascii_compatible`'s answer rather than the
  hand-written "UTF-16 is the only one". The other two: a self-closed redundant frame tag (`<html/>`)
  closes the element it sits in, because libxml2's `endElement` pops the CURRENT node and the ignored
  start tag pushed nothing; a class list splits on ASCII whitespace and NOT on Unicode whitespace, so a
  Japanese page separating two class names with an IDEOGRAPHIC SPACE matched both of them here and
  neither in lxml; and a start tag the response ends inside must be DROPPED, whole.
  That last one carries the sharpest methodological lesson in this file. It was a *documented divergence*
  — "libxml2 discards the incomplete tag; the engine keeps the attributes it already scanned" — and it was
  wrong on both counts: html5lib discards it too, so the engine was alone, and what it kept was an element
  with an attribute holding the rest of the document, i.e. a FALSE POSITIVE. Fixing it took the
  malformed-HTML fuzzer from 426 divergences to **zero, on 915000 pairs**. Every one of those 426 had been
  attributed to foster-parenting, misnesting or deep-`<p>` — because attribution asks "does this PAGE
  contain the construct", and a truncated page usually also contains a table. **Page-scoped attribution
  over-credits**: when one documented bucket never empties, suspect the bucket, not the page.
  Three more 10000-page samples then found ONE bug between them, and its interest is entirely in how it
  hid: **an end tag has a start tag's attribute states**, so a quoted value in one carries the `>`. A
  Blogger template writes `</img\nsrc="http:>`, whose unterminated value runs to the next quote 300 bytes
  later; libxml2 and html5lib swallow the markup in between and the engine — "scan a name, skip to `>`" —
  kept a `<div id='HTML3'>` that is in no other parser's tree. It was 63 of the 63 UNEXPLAINED columns
  across the three samples and **the same one page shape each time**, which is what a long-tail template
  bug looks like: three independent 10k samples, three hits, one construct. The malformed-HTML fuzzer
  could not have found it — nothing in `diff_fuzz.py` emitted an end tag with an attribute at all, so the
  whole surface rode on hand vectors, exactly as the CSS-escape gap did. Its new `end_tag_attr` mutation
  then found the SAME rule broken in two more scanners (rawtext/RCDATA and `<script>`) on its first run.
  **One fixed call site is not a fixed rule**: grep for the others before believing the gate.
  None of these are exotic;
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
- **A rule sweep is 2-D; bugs live in SEQUENCES.** Six crawl-found bugs were shapes where the state at
  token N depends on tokens 1..N-1 (`<frameset>` in `<head>`, `<body>` after `</body>`, `</%>` between two
  text runs), which no (open x incoming) table sweep can reach. `make gate-seq` enumerates every sequence
  of ~23 tokens up to length 4 and compares the WHOLE TREE, not a few `::text` columns — a document can be
  reshaped completely and still answer `p::text` identically. It found three more bugs on its first run.
  When a bug is about ORDER, reach for that before adding another cell to a table.
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
  **And the sweep can only flip what the engine models as a cell.** End-tag scope was two `matches!` arms
  and a `scope:<tag_id>` mutation over the 19 ids the engine already had, so the sweep reported it fully
  protected while the ORDER inside the table machinery was missing from the engine, the audit's probe list
  and the sweep alike. It is now derived like the others and mutated per NAME (`prio:<name>`) over the same
  universe. When a rule turns out to be coarser than reality, widen the DERIVATION first — a mutation
  sweep over the wrong shape is a green light for the wrong thing.
- **And every one of those lessons was about the ENGINE, which is the part that had an oracle.** The
  layer above it did not, and shipped five defects a 100%-green engine gate could not see: `@attrs.define`
  on a page object dropped its own fields (the decorator recreates the class, so `__init_subclass__`
  re-runs after the markers are gone — an ORDER bug, like the frame ones); a `BrowserResponse` raised and
  web-poet's own `BrowserPage` returned `{}`; and a zyte processor gated on `isinstance(value, Selector)`
  received Frostwork's `str`, matched nothing, and returned it UNCHANGED, so a raw-HTML string landed in
  a field typed `List[Breadcrumb]` with no error anywhere. Each was a hand-written list that omitted
  something — class shapes, response inputs, `field()` options, processor value types — i.e. **the
  `colgroup` mistake again, four more times, in universes that are IMPORTABLE**: web-poet and
  zyte-common-items are Python objects you can introspect instead of guessing at. `make gate-webpoet`
  (`tools/diff_webpoet.py`) is the missing oracle: parsel with the same selectors and the same nested
  `Processors`, compared on the WHOLE ITEM, because a vanished KEY is the failure mode of two of the
  three silent ones. Two things it taught while being built. **`zyte-common-items` was not in
  `requirements-test.txt`** — the primary consumer of the integration was absent from the test
  environment, so no gate could ever have caught the processor bug; when a defect class looks unreachable,
  check whether the library it breaks is even installed. And the shape axis has to be applied to BOTH
  sides: `attrs.frozen` breaks the parsel oracle too (web-poet's `cached_method` writes to the instance),
  so decorating only our side files a web-poet/attrs incompatibility as our CRASH and leaves a bucket that
  never empties — the same over-attribution as the truncated-tag bug.
  The integration now has the engine's full derive/audit/mutate trio (`tools/webpoet_surface.py`,
  `tools/diff_webpoet.py`, `tools/mutate_webpoet.py`), and the mutation sweep immediately earned its keep:
  it found TWO holes in the brand-new differential — no generated field carried a `.map()`, and no
  bare-element field was `all=True` with a processor, so downgrading that branch from `SelectorList` to a
  plain `list` (which reintroduces defect 5, because zyte gates on `SelectorList` exactly) survived the
  entire gate. **A mutation the differential misses is a hole in the differential, not a spare cell.**
  Also: the sweep NAMES what it cannot reach (everything inline in `__init_subclass__`, which runs at class
  creation and cannot be patched after import) and points each entry at the gate that does cover it —
  because "7 mutations, 0 survivors" reads as "the module is covered" and the module is bigger than what a
  function patch can touch. That is the same lesson as end-tag scope being two `matches!` arms the engine's
  sweep could not see.
  One more thing the typing work settled: **`py.typed` makes annotations a PROMISE**, and `field()`
  annotated as the internal marker class, so correct user code (`x: str = page.name`) was an error in the
  user's CI while every test here passed. Nothing but a type checker catches that class of bug — `make py`
  and CI now run mypy over the shipped package, and `tests/test_typing.py` seeds a wrong `assert_type` to
  prove the check can go red.
- **Then two review rounds found ten more defects behind that green gate, and the universes they had to
  widen were the GATE's own.** Every one was a combination the generator did not emit or a contract nobody
  restated. Four lessons worth carrying:
  **Copy upstream's predicate, not its prose.** `out=[]` is web-poet's documented way to decline one of the
  nine processors a zyte base attaches BY NAME, and its resolution is `out is not None`; reading that as
  `if out:` fell through to the nested class and silently re-enabled what a user switched off. One character.
  **A hand-checked handful is not a universe, again.** The node handoff promised "the element the selector
  matched" on the strength of four tags and was wrong for the whole document frame (`<body>` with a lone
  child comes back as that CHILD; `<head>`/`<title>`/`<meta>` as a synthesised `<html>`) — now derived
  against the same 142-name element universe as the tree rules.
  **A class keyword exists only in the `class` statement.** `@attrs.define` rebuilds the class, so
  `strict=False` was thrown away — the same ORDER bug as the spec recovery, with the same answer: carry it
  ON the class. And the sibling mistake: an inherited selector replaced by a hand-written `@web_poet.field`
  has to be resolved against the MRO, not popped off a merged dict, or the popped name is still in an
  ancestor's own declarations and comes BACK one generation later.
  **"A usable page object" has two halves.** `FrostFields` was built on web-poet's `Extractor`, which is
  deliberately not `Injectable`, so andi omitted the callback argument entirely — no exception, no log. That
  needs no Scrapy to gate: `web_poet.pages.is_injectable` and `andi.plan` answer the exact question
  scrapy-poet asks, and both are already installed. Pinning Scrapy to test it would assert a matrix this
  repo has no code for.
  The gate-shaped lessons are in [docs/TESTING.md](docs/TESTING.md) ("The web-poet layer: three gates"), and
  they reduce to three: a green differential means everything agreed OR nothing ran, so coverage is part of
  the exit condition, per (shape x input) CELL; the raw-source allowance has to compare STRUCTURE, since
  matching text excused a different tag entirely; and one detector answers one question, so each mutation
  declares which detectors must catch it — "something noticed" hides a gate losing a column. Two mutations
  are expected to be caught by unit vectors alone, and that is a finding, not a gap: the real zyte
  processors are too lenient to discriminate a wrong node end-to-end.
  Two smaller ones. **A doc example is untested code** — `docs/PYTHON.md` told everyone to write
  `class MyProductPage(ProductPage)`, which raises, and its `Returns[Product]` example declared a field the
  item had no room for; the examples now live in `tests/doc_examples.py` and the suite runs them. And a
  **dependency floor is a claim**: `web-poet>=0.8` was a guess no release near it satisfies, so the floor is
  the pinned version the gate runs, declared once in `pyproject.toml`.

## Repo map

- `src/` — the engine. `lib.rs` (`extract`/`extract_grouped` entry points + routing, `Plan`
  compile-once wrapper, `audit_schema`), `tokenizer.rs` (bytes → `TokenSink` start/text/end events),
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
- `python/frostwork/` — the Python package over the `_frostwork` extension. See `python/AGENTS.md`.
- `tools/` — Python differential/benchmark harness (needs `parsel`). See `tools/AGENTS.md`.

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
  policy is browser/WHATWG correctness, and w3lib differs from browsers in ten named places (prescan
  window, `<body>`, comments, an invalid label, a stray quote inside an unquoted charset value, UTF-32,
  `utf-16`/`x-user-defined` declarations, BOM-less UTF-16, XML-declaration position). Those are asserted as differences in `tools/enc_check.py`, not
  chased. Adding a w3lib parity case without checking which side is browser-correct is how a bug becomes
  a requirement. The same split applies to the DECODERS: Python's legacy codecs are not the WHATWG
  indexes, so `enc_check` sweeps every two-byte sequence per label and gates four disagreement classes by
  count. Enumerate over the whole byte space, never over "the sequences the other side calls assigned" —
  that filter hid 457 euc-jp characters browsers render and Python does not have.
- Tree-construction rule tables are **generated from the oracle**, not written: `tools/gen_tree_rules.py`
  derives the start-close relation, the end-tag scope priorities, the void set and the data modes over a
  fixed element universe and rewrites `src/implied_close/generated.rs` WHOLE (`--check` gates on drift).
  Do not hand-edit that block, and do not add a name to a rule table by hand — add it to `ELEMENTS` and
  regenerate.
- Rust: exhaustive `match`, keep `clippy` clean. Measure before optimizing (SIMD structural indexing
  and a tag-dispatch index were both prototyped and *rejected* by measurement — don't re-attempt
  without a workload that shows a win).
