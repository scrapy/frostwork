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
- **Selectors** — `selector.rs` parses the CSS subset; `xpath.rs` compiles the **downward** XPath
  subset to the *same* `Selector` model (so XPath and CSS share matching + performance).

## Semantics: match libxml2 2.14, not the HTML5 spec

Frostwork runs the HTML5 *tokenizer* faithfully but not the HTML5 *tree-construction* algorithm.
Instead it applies the small set of implied-close rules libxml2 actually uses (`implied_close.rs`,
derived empirically against lxml):

- **Implied end tags**: `li`; `dd`/`dt`; `option`/`optgroup`; `td`/`th`/`tr` + table sections; `rt`/`rp`.
  An open `<p>` is closed by *any* block-level or list/table-item start tag.
- **`<p>`-closing set** = the HTML4 block list (`div`, `p`, `h1`–`h6`, `ul`, `ol`, `dl`, `menu`, `dir`,
  `center`, `address`, `blockquote`, `fieldset`, `form`, `pre`, `table`, `hr`) — **not** the HTML5
  sectioning elements (`section`/`article`/`header`/…), which libxml2 leaves inside `<p>`.
- **Void set** = `area base br col hr img input link meta param` — libxml2 2.14 treats
  `embed`/`source`/`track`/`wbr` as **non-void**, so Frostwork does too.

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
byte offset and are re-sorted into document order at finish. Reverse is scoped to the selector's
**subject** with an **attached** `::text`/`::attr` terminal: a detached subtree terminal would inherit
lxml's merging of text nodes made adjacent by restructuring (a divergence that also affects plain
`div ::text`), and a `Many`/`One` sub-field or non-subject compound is out of this tier — all yield an
empty column, never a wrong value.

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
- **Rejected by measurement (do not re-attempt without a workload that shows a win):** a SIMD
  structural index (the base scan is per-*element* bound, not per-byte; memchr already bulk-skips text)
  and a subject-tag dispatch index (real selectors are class-led, so it can't bucket them).

Result: ~12–20× Parsel on realistic pages at typical field counts, up to ~40–54× on rich schemas — the
one-pass advantage grows with field count. Numbers + methodology: [BENCHMARKS.md](BENCHMARKS.md).

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
