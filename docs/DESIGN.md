# Design: how Frostwork is built, and why

Frostwork extracts data from HTML in **one streaming pass with no parse tree**. This is the *why* and
*how* behind that; [COMPATIBILITY.md](COMPATIBILITY.md) is the precise contract, and the differential
harness in `tools/` is the executable spec.

## The core idea: query-directed, treeless parsing

A scraper knows its selectors before it sees the page. A general parser doesn't, so it builds a full
DOM and then discards the 96–99% of nodes no selector touches — and parsing is most of the end-to-end
cost. Frostwork treats extraction as a **query**: scan the bytes once, keep just enough structure (a
corrected open-element stack) to answer the selectors, and materialise/decode only the values emitted.

There is **no DOM** and **no fallback**: the engine answers every query itself. It stays *close to
libxml2* (the parser Parsel/lxml use) rather than to the HTML5 spec, because libxml2 is what real
Scrapy/Parsel output is compared against.

## Pipeline

```
bytes ─▶ tokenizer (TokenSink: start/text/end) ─▶ corrected-stack matcher ─▶ per-selector columns
```

- **Tokenizer** (`tokenizer.rs`) — a minimal, correctness-first HTML tokenizer over **raw `&[u8]`**.
  It implements only the states needed to avoid a *global* offset desync: every one of libxml2's DATA
  MODES (raw text, RCDATA, PLAINTEXT, plus script's escaped/double-escaped states), comments,
  CDATA/DOCTYPE/PI skipping, attribute parsing, and "`<`-not-a-tag as text". A missing data mode is the
  worst class of bug available here — it *fabricates* elements out of an element's text content and then
  honours the wrong end tag — so WHICH names take which mode is not written here at all: it is derived
  from the oracle over the whole element universe and rendered into `implied_close::data_mode`, and this
  file owns only the states themselves. It emits borrowed byte slices through a source-agnostic `TokenSink` trait — so the
  matcher is decoupled from it and the tokenizer can be swapped/optimised independently.
- **Matcher** (`matcher/`) — maintains an open-element stack **reshaped by HTML implied-end-tag
  rules** so combinators match the tree lxml *would* build. Each open element carries a `matched`
  bitset (subject-match per selector) and per-parent `seen`/`prev` frames for sibling combinators.
  Descendant/child are resolved by a right-to-left ancestor walk; `+`/`~` (incl. chains) by the frames.
  That walk is `O(depth x compounds)`: within a segment the ancestor chain is a **path**, so matching is
  anchored glob matching — group maximal `>`-runs into contiguous blocks and place each at the deepest
  feasible position, right to left. Greedy is sound because a deeper placement leaves strictly more room
  above (exchange argument), and grouping `>`-runs is what makes it sound: greedy on individual compounds
  gets `a > b c` against `<a><b><b><c>` wrong. Searching combinations instead was exponential.
  Before any of that runs, each `(element, compound)` pair meets a **one-sided signature filter**
  (`matcher/sig.rs`, the shape of WebKit/Blink's `SelectorFilter`): at open, an element hashes its tag
  name, `id` and each `class` token to two bits each of a `u64`; each compiled compound carries the same
  bits for its own positive tag/id/classes; `compound_matches` opens with `el.sig & c.req != c.req →
  false`. One AND rejects most pairs before a string is touched. A set bit is **necessary, never
  sufficient** — collisions produce false positives, which cost only the exact comparisons that always
  ran, while a false negative would be a silently dropped value. So the hash must mirror each
  predicate's own equality: the tag is ASCII-folded on both sides (`eq_ignore_ascii_case`), `id` and
  class tokens are hashed verbatim (`==`), the class list is split by the **one** tokenizer the
  membership predicate uses (ASCII whitespace, so `class="a\u{3000}b"` is one token on both sides), and
  nothing is contributed by `:not()` (inverted), `:is()` (an OR), attribute predicates, or positions.
- **Selectors** — `selector.rs` parses the CSS subset; `xpath.rs` compiles the **downward** XPath
  subset to the *same* `Selector` model (so XPath and CSS share matching + performance).

## Semantics: match libxml2 2.14, not the HTML5 spec

Frostwork runs the HTML5 *tokenizer* faithfully but not the HTML5 *tree-construction* algorithm.
Instead it applies the small set of implied-close rules libxml2 actually uses (`implied_close/`).
**The rules themselves are enumerated once**, under "Tree-construction contract" in
[COMPATIBILITY.md](COMPATIBILITY.md). What follows is why they take the shape they do:

- **The tables are DERIVED, not written.** `tools/gen_tree_rules.py` measures the whole (open ×
  incoming) start-close relation, the end-tag scope priorities, the void set and the per-element data
  mode against libxml2 over a fixed element universe, and *generates* the Rust. Three successive
  hand-written ports each omitted names, and with them whole rules — a code-generation problem, not a
  diligence problem. So the source of truth is the oracle, `implied_close/generated.rs` is its output
  (rewritten whole, `--check` gates on drift), and a prose list of tag names anywhere else is a fourth
  copy waiting to go stale. `implied_close/mod.rs` is the hand-written half, kept in a separate file so
  the boundary is a file boundary.
- **No rule is uniform per family**, which is why generation matters more here than it looks. A same-tag
  repeat auto-closes for some list/table elements and *nests* for others; `<tbody>` closes an open row
  and `<thead>` does not; libxml2's start-close relation is over tag *names* and is finer than any
  grouping the engine might impose, so two elements that look interchangeable behave differently. Any
  grouping you would reach for by intuition is wrong somewhere. The engine used to carry a second,
  coarser close table over tag *ids* and OR the two; deriving the name relation made it redundant, and
  proving that (every id-pair answer already implied by the generated table) let it be deleted.
- **Mutation targets the ANSWER, not a cell.** `tools/mutate_rules.py` flips the *effective* close
  decision for a name pair — and, separately, the data mode for a name — rather than one table entry,
  because a cell whose answer is also reachable another way is masked and the sweep then reports it
  protected.
- **The document frame is built when the page omits it, and ignored when the page misplaces it.** Both
  halves are needed and they are different mechanisms: `<html>`/`<head>`/`<body>` all have optional
  start *and* end tags, so a conformant document may contain none of them and libxml2 frames it anyway
  (`Matcher::ensure_frame` over the oracle-derived `frame_content`) — while a frame tag written in the
  *wrong place* is ignored, with a phantom-entry counter so its matching end tag pops the ignored tag
  instead of closing the document. The state and the questions the rules ask about it live in
  `matcher/frame.rs`, each named once: four crawl-found frame bugs were a rule asking a *proxy* question
  ("is this tag a frame tag") instead of the real one ("is anything open to hold what follows").
- **Scope is what keeps a malformed page local, and it is an ORDER rather than a set.** libxml2 discards
  a misplaced end tag while anything out-ranking it is still open above its match, so unbalanced markup
  around a table truncates one field instead of desynchronizing the rest of the document. Reading it as
  a set of boundary elements is what lost a crawled page its table cells: the two coarsest answers (a
  table blocks an ordinary end tag; so does an open `<div>`) were right, and the order *inside* the
  table machinery was simply absent — from the engine, the audit's probe list and the mutation sweep
  alike. When a rule turns out to be coarser than reality, widen the derivation first; a sweep over the
  wrong shape is a green light for the wrong thing.
- None of these arms had differential coverage until `tools/audit_tree_rules.py` enumerated them cell by
  cell against lxml. Add a row there when you add a rule; docs/TESTING.md has what that turned up.

**Guiding principle — local divergence, never global desync.** Accepted divergences are always *local*
(one field differs). The states that would cause a *global* offset desync (rawtext, comment/CDATA
boundaries, encoding) get all the correctness effort; the rare tree-construction tail (foster-parenting,
adoption agency, deep-`<p>`) is accepted and documented, because libxml2 barely does it either.

## Values

`::text` and `::attr` are Parsel-identical (one value per text node, whitespace kept, entities decoded);
COMPATIBILITY.md states that contract. The three below are *choices*, so they are argued here:

- **Outer HTML** (bare element / node query) — the element's **raw source bytes**, a deliberate
  divergence from lxml's tree *reflow* (which normalizes quotes/case/whitespace and synthesizes omitted
  end tags). Raw source is cheaper, faithful, and **re-parse-equivalent**.
- **Encoding** — resolved BOM (incl. the BOM-less UTF-16 `<?` prefix) → caller label → prescan of the
  document head (`<meta charset>` and an XML declaration) → UTF-8. Tokenization stays on raw bytes for
  every ASCII-compatible encoding; only emitted values are decoded (`encoding_rs`). UTF-16 is transcoded
  up front. The target is browser/WHATWG behaviour, not w3lib parity — see COMPATIBILITY.md for the
  prescan window (and why it is not WHATWG's 1024) and the table of deliberate differences.
- **Raw NUL** is deleted from the whole document before tokenizing, as Parsel/w3lib do. It has to be
  before, not at emit time: a NUL inside a tag or attribute *name* is invisible to lxml, so dropping it
  only from values made the two sides disagree about the tree. One `memchr` on the ordinary path.

## Deferred-to-close constraints: positions, `:has()`, upward axes

Structural positions split cleanly along the one-pass grain. A per-open-element sibling-counter stack
assigns each element its 1-based `child_index` / `of_type_index` at open (after implied-close reshaping),
so **forward** positions (`:first-child`, `:nth-child(An+B)`, `:nth-of-type`, XPath `[N]`) are decided
in the match kernel with no buffering.

**Reverse** positions (`:last-*`, `:only-*`, `:nth-last-*`, XPath `[last()]`/`[last()-k]`) can't be:
"position from the end" needs the parent's *total* sibling count, known only at the parent's close. So
the matcher **defers** them — the same discipline as grouped rows and outer-HTML captures, all of which
resolve at a local close, never at document end (no global desync). A provisional (structural) match at
open captures the subject's `::text`/`::attr` value onto its own frame; at the subject's close it is
promoted to the parent (for `:last-*`/`:only-*` a single slot, overwritten — only the last-positioned
candidate can win, so resolution is O(1) per parent; `:nth-last-*` buffers each matching child, bounded
by that parent's subtree); at the parent's close the total is read from the counter frame and qualifying
values are committed. Because a nested last-child resolves before an outer one, committed values carry a
byte offset and are re-sorted into document order at finish. Reverse is scoped to a single segment; a
`Many`/`One` sub-field or a comma member is out of this tier and yields an empty column, never a wrong
value.

**Where the value lives is a separate question from which predicate defers**, and all three deferred
tiers (reverse, `:has()`, text-predicate) share one answer. The value may be the deferred element's own
(`li:last-child::text`, `div:has(a)::attr(id)`) — that streams as described above. But it may also be its
whole **subtree** (`li:last-child ::text`, `div:has(a) ::text`) or a **descendant's**
(`li:last-child b::text`, `div:has(a) a::attr(href)`), and neither can stream: the engine would have to
buffer a whole subtree per candidate until resolution, which is exactly the retention the no-tree design
exists to avoid. So those **backtrack** instead — the candidate carries only its raw span `(start, end)`,
and a winner's values are recovered afterwards by re-scanning that span (`split_deferred` picks the tail
schema; `resolve_tail_spans` runs it). Three things make this work:

- *The span is self-contained.* An end tag inside it that matched an ancestor would have ENDED the span,
  and one discarded by end-tag scope behaves identically standalone — the same re-parse-equivalence the
  differential already proves for outer-HTML node queries.
- *The re-scan runs the real engine.* The tail is a compiled sub-schema — `* ::text` / `* ::attr(name)`
  for a subtree terminal, or the selector's own compounds after the deferred one for a descendant value,
  with `strict_desc` set so the span's root is excluded (`div:has(a) div::text` means a *proper*
  descendant). It therefore inherits dropped-end-tag coalescing, end-tag scope and implied close rather
  than re-deriving them; a hand-rolled collector here would silently re-introduce the split-text bug. An
  unsupported tail marks the entry dead, so the audit keeps reporting the selector unsupported instead of
  the column quietly coming back empty. Only a DESCENDANT step into the tail is expressible this way — a
  child anchor (`div:has(a) > p`) would need "depth exactly 1 in the fragment", the same limit that makes
  grouped sub-fields reject `./x`.
- *Winners nest*, so a contained span's values are a subset of its container's. Element spans only nest
  or are disjoint, so keeping the MAXIMAL spans de-duplicates exactly — and that also bounds the cost:
  nested winners collapse to one span, so the re-scan is a single bounded extra pass (~2× a plain
  subtree query, flat in nesting depth) rather than depth-multiplied.

Retention per candidate is therefore two integers, and the streaming path is untouched.

That extra pass is paid **per deferred prefix, not per field**. The prefix — the compounds up to and
including the one carrying the predicate — is what decides which elements win, so tails whose prefixes are
equal win on exactly the same elements and compile to ONE sub-schema (`TailPlan`): a single re-scan of a
winner's span answers every column of the group. That is the label→value shape,
`//div[contains(.,"Price")]//a/@href` beside `//div[contains(.,"Price")]//span/text()`, at one re-scan
rather than two. Two rules make the sharing sound. The prefix comparison is a DERIVED structural equality,
because a hand-written one that forgot a field would hand one column another's values. And an entry the
member budget has already killed neither joins a group nor is joined — a shared sub-schema fills every
column of its group from whichever entry is live, while a dead column must stay empty. What remains grows
with the number of DISTINCT prefixes, which [BENCHMARKS.md](BENCHMARKS.md) measures against the same count
of plain fields.

For contrast, the other suspected hot spot in this tier was **rejected by measurement**: `sub_hits` with a
subtree terminal loops `seg_match` over `floor..=top`, which looks like O(depth² × compounds) per text
node, but a grouped subtree sub-field measures ~1.0× an attached one from nesting depth 2 to 40. Per the
repo's rule, that stays unoptimised until a workload shows a win.

**`:has()`** rides the same discipline, but resolves at the element's *own* close (its descendants are
all known then) rather than the parent's. A provisional structural subject match at open starts
buffering the element's value (attr now, `::text` as it streams, outer-HTML at close); while the subtree
streams, any element matching the `:has` inner sets a `has_done` bit on the enclosing subject(s) — every
ancestor for `:has(x)`, only the direct parent for `:has(> x)`; at the subject's close the buffered value
commits (offset-sorted, since nested subjects close inner-first) iff the bit is set. The inner is a single
compound (tag/id/class/attribute/`:not`) matched by `compound_matches` at open — id/attribute/`:not`
inners are a divergence in our favor, since cssselect rejects them (see COMPATIBILITY). The upward XPath
axes reuse this wholesale: `//INNER/ancestor::E`
compiles to `E:has(INNER)` and `//INNER/parent::E` to `E:has(> INNER)` — lxml's upward node set is
exactly those `:has` matches. (The one *non*-deferred axis, `following-sibling::`, is instead the CSS
general-sibling relation, so it lowers to a `~` combinator and needs no new machinery at all.)

**Text-content predicates** (`[.="v"]`, `[contains(., "v")]`, `[text()="v"]`, `[contains(text(),"v")]`)
are the same deferral: the predicate tests the element's text, known only at close. While the element is
open its text is buffered — the `.` string-value accumulates every descendant text node (no boundaries),
while `text()` keeps the *direct* child text nodes as separate pieces (the tokenizer already splits text
events at libxml2's text-node boundaries — across child elements, comments, and CDATA — so one piece is
one node). At close the predicate decides: `.` compares the concatenation; `text()` `=` is existential
over the direct nodes while `contains()` reads only the first (XPath coerces the node-set argument to its
first node). Scope is subject-only for the *value*, but the predicate may instead sit on a **preceding
sibling** (next paragraph).

**Deferred predicate on a preceding sibling** — `//dt[.="Price"]/following-sibling::dd/text()` (the
label→value pattern), `C:has(..) ~ S`, `C:has(..) + S`. Here the predicate-bearer `C` is *not* the value
element, but because `C` fully precedes `S` it closes *before* `S` opens — so `S` never needs buffering;
only `C`'s sibling *trigger* is deferred. `C`'s deferred predicate is compiled as an ordinary
has/text-pred entry with a `trigger: Some(bit)` action: instead of emitting a value, at `C`'s close (if
the predicate holds) it sets sibling-boundary `bit` on the parent's `seen`/`prev`, exactly as an
immediate trigger would at open. The value subject `S` is a normal multi-segment entry anchored to that
boundary and emits normally; its own structural trigger for `bit` is masked out of the open-time update
(`trig_immediate_mask`), so the boundary fires *only* pred-gated at `C`'s close. This reuses the entire
has/text accumulation machinery — only the resolution action differs — and touches the hot sibling path
by exactly one AND (a no-op mask when no such selector is compiled). Restricted to a single sibling
combinator (so `C`'s segment is the leftmost and needs no anchor of its own); the *ancestor* form
(`//div[.="x"]//a`, value from a descendant that emits before `C` closes) would require deferring a
descendant's emission and stays an empty column.

## Performance decisions (all validated against the gate)

- **Zero-copy + lazy decode** — borrowed slices; materialise only selector-referenced attributes;
  decode text only when it matches.
- **Bytes, not `&str`** — the single biggest scan win: `from_utf8_lossy` over the whole document was
  ~⅓ of extract time. Now the tokenizer runs on `&[u8]` and only emitted values are validated/decoded.
- **Fixed-width bitsets** — a `u128` match set (≤128 member-selectors; comma groups expand to one
  member each) and a `u64` sibling-trigger set (≤64 `+`/`~` bits). Over-budget entries compile dead.
- **Compile once, extract many** — a schema's selectors are parsed and lowered to the matcher's
  internal form (`matcher::CompiledSchema`) a single time, reusable across any number of pages via
  `Plan` (Rust) / the native `Plan` object that `Page`/`FrostPage` build once and reuse. This matches
  the real usage model — a page object is defined once and run over thousands of responses — and pays
  the parse cost per *schema*, not per *page*. The per-page recompile it removes is negligible against
  a large page's scan, but on small pages it dominates: ~3× fewer µs/page on a 244-byte page with 8
  selectors. `extract`/`extract_grouped` remain as one-shot convenience (a throwaway `Plan`).
- **One-sided signature filter** (`matcher/sig.rs`, described under *Pipeline*) — the matcher's cost is
  `selectors × elements`, and every pair used to pay string work: a `memcmp` per tag test, and per class
  test an `attrs` scan plus a fresh split of `class=`. The filter answers "could this compound match at
  all?" in one AND, which is where a class-led, high-field-count schema gets most of its time back:
  −13% at 4 class-led fields, −24% at 8, −41% at 16, −54% at 32 on the utility-CSS page; −10%/−22% at
  8/32 on the product listing; −2% to −19% on the tag-led pools, which use it far less. Each KIND of bit
  is included only above `sig::BITS_MIN` tests of that kind, dropped from both sides together.
- **Attribute materialization gate** (`matcher::AttrGate`) — the signature filter deliberately summarizes
  only tag/id/class, because a prefix/substring test is not the token equality a hash can model, so an
  attribute-predicate schema (`[data-testid=x]`, `a[href^="/p/"]`) got nothing from it. Its cost was
  `attributes × interesting names` case-insensitive comparisons per element, growing with schema size just
  where it hurts: a 32-field attribute-led schema names ~30 attributes while a real element carries half a
  dozen the schema never asked for. One 64-bit set of `(first byte, length)` pairs rejects those in one
  test, one-sided in the same direction as the signature (a set bit is necessary, never sufficient).
  Measured −4% at 4 attribute-led fields to −9.5% at 32, neutral elsewhere. Applied only above a
  name-count floor (`AttrGate::MIN_NAMES`): with one or two interesting names the scan it guards is a
  single length comparison. The count is a proxy for what actually decides the trade — the fraction of a
  page's attributes the schema ignores, highest on component-framework markup (`data-*`, `aria-*`).
- **Where the cost lives: scanning, not matching.** On a real production corpus, running with no
  selectors at all still accounts for ~43% of median page time — the tokenizer — with the rest spread
  across value decoding, the ancestor walk and output. Every scanner in `tokenizer.rs` therefore jumps
  with `memchr` rather than stepping, because a `<script>` holding inline JSON, a long quoted attribute
  value and a commented-out block are each one long run of bytes no scan predicate can act on.
  Generated pages carry none of those shapes, so a synthetic table cannot see this class of cost:
  **profile the real corpus before choosing what to optimize.**
- **Rejected by measurement (do not re-attempt without a workload that shows a win):**
  - a SIMD structural index — the base scan is per-*element* bound, not per-byte, and memchr already
    bulk-skips text;
  - a subject-tag dispatch index — real selectors are class-led, so it cannot bucket them;
  - **caching each element's split `class=` tokens** on the open-element stack — worth −27% to −51% on
    class-led schemas alone, but only ~3% on top of the signature filter while taxing every other
    workload ~8–11%, for the 40 bytes it added to each stack element;
  - an **eight-bytes-per-multiply hash** for the signature — no faster than byte-at-a-time FNV-1a,
    because short tokens pay a variable-length copy per word;
  - an **ancestor signature** (the OR of an element's and its open ancestors' bits, to reject `div.foo a`
    before the ancestor walk) — ~5% at 24–32 descendant-led fields on a deep page, but +3% to +11% worse
    at 4–16 even when gated, since the residue is a wider `Segment` and a branch in the hot walk, and the
    corpus median of 11 fields/page sits in the losing half;
  - **member dedup** — the corpus schemas contain zero exact duplicate selectors, and their shared
    prefixes would be factored by a weaker form of that ancestor signature;
  - a **UTF-8 fast path for text and raw-source decoding** (`std::str::from_utf8` instead of
    encoding_rs) — value decoding is ~25% of the work on a schema full of bare-element fields, so it
    looked like a lever; A/B over the real corpus gave median −0.5% with **0 of 15 cells above their own
    jitter**. encoding_rs's UTF-8 validation is already as fast as std's. The helper it introduced
    stayed, because it gives "decode" one definition instead of two that had drifted, but it is not an
    optimization and `matcher/decode.rs` says so.

The shape of the result is what the design predicts: the advantage over Parsel grows with field count,
because the scan is paid once per page rather than once per field, and shrinks with nesting depth (the
ancestor walk). Figures and methodology live only in [BENCHMARKS.md](BENCHMARKS.md).

## Page-object layer

`src/page.rs` is a thin, declarative layer over `extract` — it adds **no matching logic and no new
divergence**. A `Page` is an ordered `{name → (selector, cardinality)}` schema; `Page::extract`
forwards the fields' selectors as the query list to a single `extract` call and zips the returned
columns back onto the field names. Because the primitive already answers all fields in one scan, the
page object inherits the one-pass property for free: it shares the document scan, while matching cost
still grows with selector count and complexity.

Cardinality is the only presentation choice the layer makes over the raw string columns: `field`
(first match → `Option`), `field_all` (whole column → list), `field_join` (column joined by a
separator → one string). `Item` owns its value columns and a copy of the schema, so it outlives the
`Page` and needs no lifetime; `to_json` serializes it dependency-free (scalar/array by cardinality,
JSON-escaped). The engine's ≤128 member-selector budget is shared across a page's fields.

## Python bindings

`src/python.rs` (behind the `python` cargo feature, built by maturin) is a deliberately small FFI
surface exported as `frostwork._frostwork`: the `extract`/`extract_grouped` primitives, an
`audit_schema` diagnostic, and a `Plan` class (a compiled schema, reused across pages). **Only the hot
path crosses the boundary** — there is no second implementation of the matching logic to keep in sync,
so the Python results are exactly the Rust engine's (held to the same differential gate). The ergonomic
`Page`/`Item` layer, the schema-audit report, and the web-poet integration are thin pure-Python
(`python/frostwork/`).

The web-poet integration (`frostwork.webpoet.FrostPage`) compiles the page object's whole schema to one
native `Plan` **at class-creation time**, then each declared `field(selector)` becomes a real
`web_poet.field` whose getter reads from a per-instance `@cached_method` that runs that plan **once**
over `response.body`. So the idiomatic web-poet surface (`to_item()`, `Returns[Item]`, `@handle_urls`,
mixing hand-written `@field` methods) is preserved while every selector field shares a single scan of a
pre-compiled schema — a per-call `.css()` shim would re-scan per field and throw away the one-pass
advantage, so we don't offer one. Wheels are abi3 (`abi3-py310`). Details: [PYTHON.md](PYTHON.md).
