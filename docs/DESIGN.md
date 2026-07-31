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
  It implements only the states needed to avoid a *global* offset desync: rawtext/RCDATA
  (`script`/`style`/`textarea`/`title`), comments, CDATA/DOCTYPE/PI skipping, attribute parsing, and
  "`<`-not-a-tag as text". It emits borrowed byte slices through a source-agnostic `TokenSink` trait —
  so the matcher is decoupled from it and the tokenizer can be swapped/optimised independently.
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
  class tokens are hashed verbatim (`==`), and nothing is contributed by `:not()` (inverted), `:is()`
  (an OR), attribute predicates, or positions.
- **Selectors** — `selector.rs` parses the CSS subset; `xpath.rs` compiles the **downward** XPath
  subset to the *same* `Selector` model (so XPath and CSS share matching + performance).

## Semantics: match libxml2 2.14, not the HTML5 spec

Frostwork runs the HTML5 *tokenizer* faithfully but not the HTML5 *tree-construction* algorithm.
Instead it applies the small set of implied-close rules libxml2 actually uses (`implied_close.rs`,
derived empirically against lxml):

- **Implied end tags** are NOT uniform per family — every cell is verified individually, because the
  families disagree with each other and with HTML5. A same-tag repeat auto-closes for
  `li`/`option`/`tr`/`td`/`th`/`tbody`/`p` but **nests** for `dt`/`dd`/`rt`/`rp`/`optgroup`/`thead`/
  `tfoot`/`caption`. `<tbody>`/`<tfoot>` close an open row/cell; `<thead>` does not. `<colgroup>` is
  closed by a section or a row, and closes an open `<caption>` or `<p>`.
- **`<p>`-closing set** = the HTML4 block list (`div`, `p`, `h1`–`h6`, `ul`, `ol`, `dl`, `menu`, `dir`,
  `center`, `address`, `blockquote`, `fieldset`, `form`, `pre`, `table`, `hr`) plus list/table *items*
  (`li`, `dd`, `dt`, `tr`, `td`, `th`, `tbody`, `tfoot`, `caption`) — **not** the HTML5 sectioning
  elements (`section`/`article`/`header`/…), and **not** `option`/`optgroup`/`thead`/`rt`/`rp`, all of
  which libxml2 leaves nested inside the `<p>`.
- **Table scope**: an ordinary end tag never unwinds a table. With a table-scoped element open above its
  match, libxml2 discards the tag (`<div><table><tr><td>A</div>B` keeps `AB` in the cell). The set is
  per element and excludes `caption`/`colgroup`, which is only visible BARE — wrapped in a `<table>`
  the table blocks regardless, which is how a wrong `caption` entry survived the first audit.
- **Void set** = `area base br col hr img input link meta param` — libxml2 2.14 treats
  `embed`/`source`/`track`/`wbr` as **non-void**, so Frostwork does too.
- None of these arms had differential coverage until `tools/audit_tree_rules.py` enumerated them cell by
  cell against lxml. Add a row there when you add a rule; docs/TESTING.md has what that turned up.

**Guiding principle — local divergence, never global desync.** Accepted divergences are always *local*
(one field differs). The states that would cause a *global* offset desync (rawtext, comment/CDATA
boundaries, encoding) get all the correctness effort; the rare tree-construction tail (foster-parenting,
adoption agency, deep-`<p>`) is accepted and documented, because libxml2 barely does it either.

## Values

- `::text` / `text()` — per text node, whitespace preserved, entities decoded (Parsel-identical).
- `::attr(x)` / `@attr` — entity-decoded attribute value.
- **Outer HTML** (bare element / node query) — the element's **raw source bytes**, a deliberate
  divergence from lxml's tree *reflow* (which normalizes quotes/case/whitespace and synthesizes omitted
  end tags). Raw source is cheaper, faithful, and **re-parse-equivalent**.
- **Encoding** — resolved BOM → caller label → `<meta charset>` → UTF-8. Tokenization stays on raw
  bytes for every ASCII-compatible encoding; only emitted values are decoded (`encoding_rs`). UTF-16
  is transcoded up front.

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
  and one discarded by table scope behaves identically standalone — the same re-parse-equivalence the
  differential already proves for outer-HTML node queries.
- *The re-scan runs the real engine.* The tail is a compiled sub-schema — `* ::text` / `* ::attr(name)`
  for a subtree terminal, or the selector's own compounds after the deferred one for a descendant value,
  with `strict_desc` set so the span's root is excluded (`div:has(a) div::text` means a *proper*
  descendant). It therefore inherits dropped-end-tag coalescing, table scope and implied close rather
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

That "single bounded extra pass" is **per selector**, though: each deferred tail is its own sub-schema, so
N tail-bearing fields over the same outer subtree re-scan it N times, losing the multi-selector scan
sharing the main pass has. Measured on a 200 KB product listing (`bench_matrix`'s `TAIL_POOL`): one tail
field costs ~2.6× a plain one and eight cost ~4.3×, i.e. the penalty grows with FIELD COUNT. Left as is
deliberately — the absolute cost is milliseconds and no real schema is tail-heavy yet — but it is now in
the benchmark so the decision is revisitable. The fix, if it ever matters, is merging tails that share a
deferred prefix into one sub-schema (their winner spans are identical by construction).

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
  `selectors × elements`, and before this every pair paid string work: a `memcmp` per tag test, and per
  class test an `attrs` scan plus a fresh `split_whitespace` of `class=`. The filter answers "could this
  compound match at all?" in one AND. It is where the *class-led, high-field-count* schema — the shape
  real page objects have — gets most of its time back; measured against the pre-filter build on the
  class-heavy page (`tools/bench_matrix.py --class-led`): −18% at 4 class-led fields, −31% at 8, −50% at
  16, −63% at 32; on the product listing −16% at 8 and −31% at 32; and −2% to −28% on the tag-led pools,
  which use the filter far less (a tag test is one `memcmp`). Signatures are built per start tag, so each
  KIND of bit is included only for a schema that performs at least two tests of that kind
  (`sig::BITS_MIN`) — below that the bits leave both sides, because hashing every element's class list to
  save one comparison is a loss (measured: −11% before this rule, ±1% after). Compile pays ~0.4 µs more
  per schema (11 selectors), which the `Plan` reuse model amortizes to nothing but a one-shot `extract`
  on a ~1 KB page can still see as a percent or two.
- **Rejected by measurement (do not re-attempt without a workload that shows a win):** a SIMD
  structural index (the base scan is per-*element* bound, not per-byte; memchr already bulk-skips text);
  a subject-tag dispatch index (real selectors are class-led, so it can't bucket them); **caching each
  element's split `class=` tokens** on the open-element stack (inline `(start, len)` ranges, no
  allocation) — on its own it was worth −27% to −51% on class-led schemas, but on top of the signature
  filter it added only ~3% at 8+ class fields while taxing every other workload ~8–11% for the 40 bytes
  it added to each stack element, so it was reverted; and an **eight-bytes-per-multiply hash** for the
  signature (word load + whole-word case fold), which measured no faster than byte-at-a-time FNV-1a
  because short tokens pay a variable-length copy per word.

Result: ~11–20× Parsel on realistic pages at typical field counts, up to ~50–61× on rich schemas — the
one-pass advantage grows with field count, and a one-field schema is the weak case (3–6×). Numbers +
methodology: [BENCHMARKS.md](BENCHMARKS.md).

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
advantage, so we don't offer one. Wheels are abi3 (`abi3-py39`). Details: [PYTHON.md](PYTHON.md).
