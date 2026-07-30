# Frostwork compatibility contract

Frostwork is a **no-fallback** engine: unlike engines that route anything outside their native subset
to a full parser (Parsel/lxml) so results always match, Frostwork answers **every** query itself. So a
query falls into one of three buckets:

| bucket | meaning |
|---|---|
| ✅ **supported** | runs on the streaming engine and is **byte-identical to lxml** (non-whitespace), validated by the differential gate |
| ≈ **divergent** | runs, but may differ from lxml on specific constructs — **documented and bounded** (0% on conformant/foreign input; on deliberately malformed input, confined to the SKIP set below) |
| ∅ **unsupported** | the engine produces an **empty result**; public Python APIs raise by default and expose this behavior with `strict=False` |

The promise is *close to lxml, always one streaming pass, never a DOM*. The correctness bar is
**non-whitespace value parity with lxml (libxml2 2.14)** on the supported set — not the HTML5 spec.

For a quick, always-current headline of what runs, see the generated
[selector support snapshot](SUPPORT_SNAPSHOT.md) (regenerate with
`python tools/support_snapshot.py > docs/SUPPORT_SNAPSHOT.md`; `--check` fails CI on drift). The
exhaustive contract is the rest of this document.

Because an unsupported selector is indistinguishable from a legitimately-empty field at the engine
level, public Python APIs validate schemas and raise `UnsupportedSelector` by default. Pass
`strict=False` to request permissive empty results. The Rust `frostwork::audit_schema` and Python
`frostwork.check` report each selector's bucket (with an advisory reason) plus budget usage. See
[PYTHON.md](PYTHON.md) §4.

---

## CSS selectors

| feature | status |
|---|---|
| type / universal (`div`, `*`) | ✅ supported |
| class / id (`.c`, `#i`) | ✅ supported |
| attribute `[a]`, `[a=v]` | ✅ supported |
| attribute operators `[a^=v]` `[a$=v]` `[a*=v]` `[a~=v]` `[a\|=v]` | ✅ supported (empty operand matches nothing; case-sensitive — matches lxml) |
| attribute value **unquoted**: a CSS identifier only (`[a=v]`, `[a=-v]`, `[a=café]`) | ✅ supported. A non-identifier unquoted value (`[a=2]`, `[href^=/p]`, `[a=$v]`, `[a=--v]`, `[a=a.b]`) is ∅ unsupported (empty) — cssselect rejects those as a syntax error, so answering them would return values for a selector Parsel refuses. Quote it (`[a="2"]`, `[href^="/p"]`) — quoted values are unrestricted |
| `:not(<compound>)` incl. compound arg (`a:not(a.x)`) and chained (`:not(.x):not(.y)`) | ✅ supported |
| descendant (`a b`), child (`a > b`) | ✅ supported |
| adjacent / general sibling (`a + b`, `a ~ b`), incl. chains (`a ~ b ~ c`), a descendant/child **base** (`.list li ~ li`), and a trailing descendant/child **step** (`a + b c`, `input + label p::text`) | ✅ supported |
| `::text` — self (`E::text`) and descendant-or-self (`E ::text`) | ✅ supported (per text node, whitespace kept, entities decoded) |
| `::attr(name)` — self and descendant-or-self (`E ::attr(x)`) | ✅ supported (entity-decoded) |
| bare element → outer HTML (`div`, `.card`) | ≈ divergent — **raw source**, not lxml's reflow (see below) |
| comma list, same terminal (`h1::text, h2::text` · `.a::attr(href), .b::attr(href)` · `h1, h2`) | ✅ supported (document-order union, per-column de-dup) |
| comma list, **mixed** value terminals (`a::text, a::attr(href)` · `img::attr(src), img::attr(data-src)`) | ✅ supported (document-order union: `::attr` at element-open in source order, `::text` at text nodes; de-dup by node) |
| comma list, bare-element (outer HTML) mixed with a value terminal (`b, b::text`) | ∅ unsupported (empty) — deferred captures can't interleave with streamed values in document order |
| forward position — `:first-child`, `:nth-child(An+B)`, `:first-of-type`, `:nth-of-type(An+B)` (incl. `odd`/`even`, `-n+3`); composes inside `:not()` | ✅ supported (`:nth-child` counts element siblings; `:nth-of-type` needs a concrete tag) |
| reverse position — `:last-child`, `:last-of-type`, `:only-child`, `:only-of-type`, `:nth-last-child(An+B)`, `:nth-last-of-type(An+B)` — on ONE compound of a lone selector. The value may be that element's **own** (`li:last-child::text`), its **subtree** (`li:last-child ::text`), or a **descendant's** (`li:last-child b::text`) | ✅ supported (resolved at the parent's close; `-of-type` needs a concrete tag). An own-value terminal streams; the other two are recovered afterwards by re-scanning the winner's raw span — see the note below |
| reverse position in any OTHER shape — a **child** step into the value tail (`li:last-child > b::text`), a comma group, a `Many`/`One` sub-field, or `*`-of-type (`*:nth-last-of-type`) | ∅ unsupported (empty). The child step would need "depth exactly 1 within the span", which the depth-agnostic matcher can't express — use the descendant form |
| `:has(<compound>)` / `:has(> <compound>)` — on ONE compound of a lone selector; the value may be that element's **own** (`div:has(a)::attr(id)`), its **subtree** (`div:has(a) ::text`), or a **descendant's** (`div:has(a) a::attr(href)`) | ✅ supported (resolved at the subject's own close). The inner is a single compound: tag/`*`, id, classes, attribute predicates, `:not(...)` — `:has(a)`, `:has(.price)`, `:has([data-src])`, `:has(#main)`, `:has(a.buy)`, `:has(:not(.hidden))`, `:has(> img)`. **≈ divergent** for id/attribute/`:not` inners — see below |
| `:has()` on a PRECEDING-SIBLING compound — `C:has(..) ~ S` / `C:has(..) + S` (value from the later sibling `S`) | ✅ supported — same mechanism as the sibling text-predicate: `C`'s `:has` fires the sibling boundary at `C`'s close, `S` emits normally (single sibling combinator) |
| `:has()` in any OTHER shape — an inner with a **chain/sibling** (`:has(.a .b)`, `:has(a + b)`) or a positional/reverse/`:has`/`:is` inside, multiple `:has`, a **child** step into the value tail (`div:has(a) > p::text`), or a `Many`/`One`/comma member | ∅ unsupported (empty) |
| `:is(...)` / `:where(...)` — a comma-list of compound alternatives (`:is(h1, h2, h3)`, `div:is(.a, .b)`, `a:is([href], [src])`), including combined with other conditions (`div.card:is(.a, .b)`) or chained (`x:is(a, b):is(c, d)`) | ✅ supported — element matches iff it matches ≥1 alternative in EVERY group (OR within a group, AND across groups). `:is`/`:where` are identical (specificity is irrelevant to matching). **≈ divergent** for combined/chained forms — see below |
| `:is(...)` with a combinator inside an alternative (`:is(.a .b)`, `:is(a + b)`), a positional/reverse/`:has` inside an alternative (`:is(:first-child)`), or a nested `:is` | ∅ unsupported (empty) — cssselect itself rejects the combinator forms |
| `:contains()`, `::first-line`, other pseudos | ∅ unsupported (empty) |
| `:not()` with a combinator argument (`:not(a b)`), namespaces (`ns\|tag`), `[a=b i]` case flag (cssselect rejects it) | ∅ unsupported (empty) |
| a **non-ASCII TAG** name (`café::text`, `x-é::text`) | ∅ unsupported (reported, not silently empty) — the tokenizer's tag-name state is ASCII-only, so these can never match. lxml *does* match them, so this is a coverage gap; non-ASCII **class/id/attribute** names (`.café`, `#año`) are fully supported |

## XPath (downward subset)

| feature | status |
|---|---|
| document-rooted paths (`//div`, `/html/body/p`); `.`-relative **descendant** (`.//a`) | ✅ supported |
| `.`-relative **child** anchor (`./div`, `./h3/text()`) | ∅ unsupported (empty) — the depth-agnostic matcher can't enforce a child anchor on the first step, so it is rejected rather than over-matched like `.//`. Use `.//` (descendant) instead. |
| `//`→descendant, `/`→child steps; `*` and tag node tests | ✅ supported |
| predicates `[@a]`, `[@a="v"]`, `[contains(@a,"v")]`, `[starts-with(@a,"v")]`, `[… and …]`, `[… or …]` | ✅ supported (a predicate `or` is distributed into union members). The compared value must be a **quoted string literal** |
| comparison against a NON-literal operand — a variable reference (`[@id=$pid]`), a number (`[@a=2]`), or a bare name (`[@a=b]`) | ∅ unsupported (empty) — Frostwork's API takes no variable bindings (unlike `sel.xpath(q, pid=…)`), and XPath gives `[@a=2]` numeric semantics (`a="02"` matches) and `[@a=b]` node-set semantics (compare against child `<b>` elements), none of which is a byte compare. Quote the value if a literal is meant. A `$` **inside** a literal (`[contains(@id,"$p")]`) is just data |
| terminals `text()` (self), `//text()` (descendant), `/@attr`, `//@attr` (descendant-or-self), bare element → node | ✅ supported |
| unions (`a \| b`) | ✅ supported — one document-ordered, node-deduped column (same as a CSS comma group) |
| `normalize-space(path)` as the whole query | ✅ supported — a **scalar**: the string-value of the FIRST matched node (element → concat of its subtree text; `/text()` → first text node; `/@a` → attr), whitespace-collapsed. Always exactly one value (`""` if nothing matched). `normalize-space()` / `normalize-space(.)` (context node) and `normalize-space` inside a predicate are ∅ unsupported. |
| positional predicate `[N]` (constant, sole predicate) — `//li[2]` (of-type), `//ul/*[3]` (nth child) | ✅ supported (per parent, matching lxml) |
| reverse positional predicate `[last()]` / `[last()-k]` / `[position()=last()[-k]]` (sole predicate) — `//li[last()]` (of-type), `//ul/*[last()]` (nth-last child) — with an attached `/text()`/`/@a` terminal | ✅ supported (resolved at the parent's close, like CSS `:last-*`) |
| `following-sibling::` axis after a single `/` (`//dt/following-sibling::dd/text()`) | ✅ supported — the same tree relation as CSS `~`, lowered to a general-sibling combinator. A positional predicate on it (`following-sibling::td[1]`) or a `//` before it (`//a//following-sibling::b`) stays ∅ unsupported (no faithful `~` lowering) |
| upward `ancestor::` / `parent::` as an ABSOLUTE two-step path (`//span/ancestor::div/@id`, `//a/parent::li`) | ✅ supported — reframed onto `:has`: `//INNER/ancestor::E` → `E:has(INNER)`, `//INNER/parent::E` → `E:has(> INNER)`. INNER and E each a lone compound (tag/`*` + attribute predicates). A relative context (`.//`), a chain INNER (`//div//span/ancestor::E`), or a positional predicate stays ∅ unsupported |
| other non-downward axes (`ancestor-or-self::`, `preceding[-sibling]::`, `following::`) and downward synonyms (`child::`, `descendant::`) | ∅ unsupported (empty) |
| reverse positional predicate with a **descendant** terminal — `//li[last()]//text()`, `//li[last()-1]//text()` | ✅ supported (the subtree form of the row above; same span re-scan) |
| `[position()<n]`, a reverse predicate with no value terminal, and `[N]`/`[last()]` combined with another predicate (`//p[@x][1]`, position among the filtered set) | ∅ unsupported (empty) |
| text-content predicate as a SOLE predicate on ONE step — `[.="v"]`, `[contains(.,"v")]`, `[text()="v"]`, `[contains(text(),"v")]` — with an attached `/text()`/`/@a` or bare-element terminal, a descendant `//text()`, or a descendant value step (`//div[contains(.,"x")]//a/@href`) | ✅ supported (resolved at the element's own close). `.` is the whole string-value (subtree text concatenated); `text()` is the direct child text nodes — `=` is existential (ANY node), `contains()` reads only the FIRST. No whitespace trimming (use `normalize-space` for that). `contains(…,"")` is always-true, matching XPath |
| text-content predicate on a PRECEDING-SIBLING step — `//dt[.="Price"]/following-sibling::dd/text()`, the label→value pattern (also `:has` — `C:has(..) ~ S` / `+ S` in CSS) | ✅ supported — the predicate-bearer `C` closes before the value sibling `S` opens, so `C`'s deferred predicate fires the sibling boundary at its close and `S` emits normally (single sibling combinator; `C` and `S` each a compound/descendant-chain) |
| text-content predicate in any OTHER shape — combined with another predicate (`//p[@x][text()="y"]`), `!=`/`<`/`>`, `normalize-space(…)`/other functions inside, an `or`-join, or on a NON-subject **ancestor** step (`//div[.="x"]//a` — value from a descendant, which would need deferred descendant emission) | ∅ unsupported (empty) |
| other functions (`count()`, `string()`, `substring()`, …) | ∅ unsupported (empty) |
| relative / context steps without a leading `/` or `./` (`td/text()`, `@href`) | ∅ unsupported (empty) |
| `contains(@a,"")` / `starts-with(@a,"")` — **empty operand** | ≈ divergent — returns **empty** (CSS empty-operand semantics: an empty needle matches nothing), whereas XPath-proper `contains` with `""` is always-true. Non-empty operands match lxml. |

XPath compiles to the **same** selector model as CSS, so a supported XPath query has identical
semantics and performance to its CSS equivalent.

## Nested / grouped extraction (`Many` / `One`)

| feature | status |
|---|---|
| `Many`/`One`: per-container sub-fields, one streaming pass | ✅ supported — byte-identical to Parsel's `for c in doc.css(container): {sub: c.css(sub)...}` (descendant-or-self scope), gated |
| container = any immediate (open-time) supported CSS/XPath selector; nested & empty containers | ✅ supported (a nested same-class container yields its own row; a value falls into every enclosing container's row, matching Parsel) |
| sub-field terminals: `::text`, `::attr(x)`, bare element (outer HTML) | ✅ supported (same value semantics as flat) |
| sub-field with descendant/child combinators (`h3 a::text`, `.p > span::text`) | ✅ supported |
| sub-field scope axis: CSS is descendant-or-**self** (may match the container); XPath `.//` is strict **descendant** (excludes it) | ✅ supported — matches Parsel's `c.css(sub)` vs `c.xpath('.//…')` |
| sibling `+`/`~` **inside** a sub-field | ∅ unsupported (empty column) |
| comma group, reverse position, `:has()`, or text-content predicate in a container/sub-field | ∅ unsupported (empty group/column) — grouped routing is open-time and single-member |
| `Many` nested **inside** a sub-field (grouped-within-grouped) | ∅ unsupported |
| comma-group / leading-combinator sub-field | ∅ unsupported (empty) |

Rows are emitted in container document order; sub-fields not present in a container yield an empty
column (the container still yields a row). See [PYTHON.md](PYTHON.md) for the `Many`/`One` API.

---

## Tree-construction contract — matches libxml2 2.14, NOT the HTML5 spec

The engine runs the HTML5 *tokenizer* plus a minimal, **libxml2-matching** implied-close reshape (no
DOM, no full tree construction). These rules were derived empirically against libxml2 2.14 (see
`src/implied_close.rs`):

- **Implied end tags** (auto-close to siblings) are NOT uniform per family — every cell below is
  verified individually against libxml2 2.14.6, because the families disagree with each other and with
  HTML5:
  - **auto-close a same-tag repeat**: `li`, `option`, `tr`, `td`, `th`, `tbody`, `p`.
  - **NEST a same-tag repeat**: `dt`, `dd`, `rt`, `rp`, `optgroup`, `thead`, `tfoot`, `caption`. So
    `<dl><dt>a<dt>b</dl>` is `<dt>a<dt>b</dt></dt>` — one child of the `<dl>` — and a grouped
    `<select>` with omitted `</optgroup>` nests each group inside the previous one.
  - **cross-tag**: `dt`↔`dd` close each other; `rt`/`rp` never close anything. `<tbody>`/`<tfoot>`
    close an open `tr`/`td`/`th`/`caption`, but **`<thead>` does not** — the three sections are not
    interchangeable. `<tfoot>` is closed by `<tbody>` but nests a second `<tfoot>`.
- **An open `<p>`** is closed by the block set plus list/table *items* (`li`, `dd`, `dt`, `tr`, `td`,
  `th`, `tbody`, `tfoot`, `caption`, `p`) — but **not** by `option`, `optgroup`, `thead`, `rt` or `rp`,
  which nest inside it.
- **An end tag never unwinds a table** ("table scope"). With a table-scoped element (`table`, `caption`,
  `thead`, `tbody`, `tfoot`, `tr`, `td`, `th`) open above its match, an ordinary end tag is *discarded*:
  in `<div><table><tr><td>A</div>B`, the `</div>` is dropped and the cell keeps `AB` as one text node.
  Table-scoped end tags (`</table>`, `</tr>`, …) unwind normally, and `</body>`/`</html>` still close the
  document. Unbalanced `<div>`s around tables are a common real-world malformation, so this path matters.
- **An end tag with no open element to match is DROPPED, and does not split the text node.** libxml2
  discards it and keeps the character data either side as ONE text node, so `<div>A</p>B</div>` yields
  `AB`, not `A` + `B`. Only source-adjacency across the dropped tag is re-joined: a comment, CDATA, PI,
  void element or real child in the gap is a node in libxml2 too, and still splits the run. Doc
  generators emit this (Sphinx: `</p>\n</p>`), and a split would silently *truncate* a `One`-cardinality
  field rather than empty it — the one failure mode no-fallback is meant to exclude.
- **`<p>`-closing block set** is the HTML4 block list (`div`, `p`, `h1`–`h6`, `ul`, `ol`, `dl`, `menu`,
  `dir`, `center`, `address`, `blockquote`, `fieldset`, `form`, `pre`, `table`, `hr`) — **not** the
  HTML5 sectioning elements (`section`, `article`, `header`, `footer`, `nav`, `main`, `figure`,
  `details`, …), which libxml2 treats as ordinary inline-ish containers that do **not** close `<p>`.
- **Void set**: `area base br col hr img input link meta param`. Note `embed`/`source`/`track`/`wbr`
  are **non-void** here — libxml2 2.14 keeps them open as containers.
- **Rawtext**: `script`/`style` (raw, entities literal), `textarea`/`title` (RCDATA, entities decoded).
- **Character references**: libxml2/Parsel-compatible — legacy semicolon-less names, numeric edge
  cases (`&#00`→U+FFFD, win-1252 remap), raw NUL dropped.

## Value semantics

- `::text` / `text()` — one value per text node, whitespace preserved, entities decoded. Matches Parsel.
- `::attr(name)` / `@attr` — entity-decoded attribute value. Matches Parsel.
- **Outer HTML** (bare element / node query) — the element's **raw source bytes**, a *deliberate*
  divergence from lxml's tree reflow (lxml normalizes attribute quotes/case/whitespace and synthesizes
  omitted end tags). Raw source is cheaper, faithful to the page, and **re-parse-equivalent** (parsing
  it yields the same node set + non-whitespace text as lxml). Boundaries come from the corrected stack,
  so an unclosed `<li>` captures `<li>…` up to its implied close.

---

## Documented divergences (≈) — where "close" gives way

These run without error but can differ from lxml. They are **0% on conformant/foreign input**; on
deliberately malformed input the residual differences cluster under the constructs below.

Do not read that as "real pages never hit this". An earlier version of this section claimed exactly
that, and it was wrong twice over: the `dd`/`dt` same-tag close and the dropped-end-tag text split both
fired on ordinary pages (doc-generator output) while the generated gate read 100% parity. Malformed
input is not the same thing as rare input. `tools/diff_fuzz.py` now attributes each divergence to one of
these constructs and reports anything left over as **NOVEL** — that bucket, not this list, is where the
next bug will be:

- **Foster-parenting** — table-scoped elements outside a `<table>` (`<p>…<td>x</td>…`). libxml2
  relocates/ignores them; the engine nests them.
- **Adoption agency** — misnested formatting (`<b><i></b></i>`) and block content inside `<a>`.
- **Deep-`<p>`** — a block/item start closing a `<p>` that is an *ancestor* rather than the immediate
  open element (`<p><b><div>`); a known gap that leaves the un-reshaped nesting (never worse).
- **Head-only elements in `<body>`** (`<title>` in body, etc.).
- **Nested `<form>`** — libxml2 *ignores* a `<form>` start tag while another `<form>` is open; the
  engine nests it, so the inner content sits one level deeper (`<div><form><form>x` → `div > *::text`
  is `x` in lxml, empty here).
- **A tag left unterminated by EOF** — on a response cut off mid-tag (`…<a href="/p2" class="`),
  libxml2 discards the incomplete tag; the engine has already read its attributes and keeps them.
  Only reachable on truncated input.
- **Outer-HTML serialization** (raw source vs reflow — above).
- **No `<html>`/`<body>` synthesis on fragments.** The engine matches off the byte stream with no
  synthesized document frame, so on a *fragment* (e.g. `<h1>a</h1><p>b</p>` with no `<body>`):
  top-level siblings don't match sibling selectors (`h1 + p::text` → empty; no parent frame to gate
  against), and text directly under the implicit root is dropped (text *inside* a top-level element
  is fine). lxml wraps fragments in `html`/`body`, so it matches. **Real HTTP responses are full
  documents**, where this never bites; it only affects hand-parsed fragments.
- **`:has()` with an id/attribute/`:not` inner — a divergence *in our favor*.** cssselect 1.4.0 only
  accepts a type/`*`+classes inner inside `:has()` and *raises* `SelectorSyntaxError` on `:has([data-x])`,
  `:has(#id)`, `:has(a[href])`, `:has(:not(.x))` (part of its broader `:has()` limitations —
  cf. [scrapy/cssselect#138](https://github.com/scrapy/cssselect/issues/138), which notes `:has(a, b)`
  selector lists are also unsupported). These are valid CSS, so Frostwork matches them correctly and is
  simply *more capable* than raw parsel here — no wrong values, just coverage parsel refuses. Bare
  type/`*`+class inners agree with parsel exactly. Oracled by
  `tests/test_python.py::test_has_widened_inners_match_correct_semantics` (a parsel/lxml ancestor walk,
  since parsel can't evaluate these directly).
- **`:is()`/`:where()` combined with other conditions — a divergence *in our favor*.** cssselect 1.4.0
  mis-translates a `:is()` whose compound carries any other condition: its `xpath_matching` ORs each
  alternative's condition onto the *base* compound's condition, so `div.a:is(.x, .c)` becomes
  `div[a or x or c]` (matches every `div.a`, `div.x`, or `div.c`) instead of `div[a and (x or c)]`, and
  chained `:is` ORs all groups together. Frostwork implements the **correct** CSS semantics (AND across
  groups, OR within), so it intentionally differs from parsel on these forms — returning the standards-
  compliant node set, a strict subset of parsel's over-match. Bare `[tag|*]:is(...)` (the only shape
  cssselect gets right) agrees exactly. Upstream: the `:is`/`:where` handling is tracked as
  not-spec-compliant in [scrapy/cssselect#135](https://github.com/scrapy/cssselect/issues/135) and
  [#108](https://github.com/scrapy/cssselect/issues/108); the over-match is in `xpath_matching`
  (`add_condition(..., "or")` folding alternatives into the base). Verified by
  `tests/test_python.py::test_is_where_correct_and_semantics_diverges_from_cssselect_bug`, which oracles
  against the equivalent comma-expansion (which cssselect translates correctly).
- **Content after `</html>` is KEPT — a divergence *in our favor*, but only partly browser-equivalent.**
  libxml2 stops building the tree at `</html>` and silently discards everything after it, so
  `<html><body>…</body></html><div>late</div>` gives lxml/Parsel an empty column for `div::text` while
  Frostwork returns `late`. Keeping it is deliberate: trailing markup after `</html>` is common in the
  wild (injected analytics, chat widgets, CDN/proxy-appended snippets), and silently dropping real
  content is the worse failure for a scraper — same reasoning as `:is()`/`:has()`/UTF-16 above.

  Be precise about the three-way split, because "matches browsers" is only half true. Per the HTML
  Standard's *after after body* insertion mode a non-whitespace token there is a parse error that gets
  reprocessed "in body", so a browser both keeps the content **and re-parents it into `<body>`**
  (confirmed against Chrome `--dump-dom`: the trailing elements come back as `<body>` children).
  Frostwork keeps the content but does **not** re-parent it — it has no synthesized document frame, so
  the elements stay where the byte stream put them, after `<body>` closed. Therefore:

  | selector shape | lxml/Parsel | Frostwork | browser |
  |---|---|---|---|
  | unscoped (`div::text`, `#after::text`) | ∅ empty | ✅ finds it | ✅ finds it |
  | ancestor-scoped (`body div::text`, `body ::text`) | ∅ empty | ∅ empty | ✅ finds it |

  So an unscoped selector is browser-equivalent, while a `body`-scoped one agrees with lxml instead.
  That second row is the same root cause as **No `<html>`/`<body>` synthesis** above.

  **Migration caveat.** This is an *extra*-value divergence, the one direction that can surprise a
  port: a `Many` field whose selector also matches trailing injected markup gains rows it did not have
  under Parsel. If a spider depends on the oracle's truncation, constrain the selector to the real
  container (`main .card`, not `.card`) rather than relying on the parser to drop the tail.

## Encoding

Full WHATWG-style resolution: **BOM → caller/HTTP charset label → `<meta charset>` prescan (first
1024 bytes) → UTF-8 default** (`src/encoding.rs`). Structural tokenization runs on raw
bytes for every ASCII-compatible encoding (all HTML delimiters are `< 0x40`); only the small emitted
values are decoded with the resolved encoding (`encoding_rs`). UTF-16LE/BE (BOM- or label-detected)
are transcoded to UTF-8 up front.

- ✅ **Validated 35/35 vs Parsel** given the same label — windows-1252, shift_jis, euc-jp, gbk, big5,
  koi8-r, utf-8 — in both text nodes and attribute values (`tools/enc_check.py`).
- ✅ **UTF-16** (LE/BE, BOM or label) decodes correctly, matching the decode-first result Scrapy uses.
  (Note: lxml's HTML parser can't parse UTF-16 *bytes* — `Selector(body=…, encoding="utf-16")` returns
  `[]`/errors — so here the engine is strictly *more* capable than raw Parsel; a divergence in our favor.)
- Robust to bogus/unknown labels and non-UTF-8 garbage (fuzzed, no panic).

## Limits
- **≤128 member-selectors** per `extract` call (comma-group members and group containers each count
  as one) and **≤64 sibling trigger bits** (one per `+`/`~` combinator across all of the above).
  These are the fixed-width bitsets (`u128` / `u64`) the matcher runs on — a deliberate perf lever.
- **Over budget is a *caller* bug, not a coverage gap**, so it is handled loudly rather than silently:
  in Rust each over-budget selector compiles *dead* (its column is deterministically empty — never a
  panic, never an aliased/wrong value); in Python `frostwork.extract` / `extract_grouped` raise
  `ValueError`. Split an over-budget schema into multiple calls. (In Rust, `frostwork::budget_usage`
  reports a schema's `(members, sibling_bits)` demand.)
- **No fallback**: an unsupported query compiles to an empty engine column, never a Parsel detour.
  Public Python APIs raise before scanning unless the caller passes `strict=False`.

---

## Validation basis

Every ✅ above is held to non-whitespace parity with lxml by the differential harness; every ≈ is
measured and bucketed, not assumed (full methodology: [TESTING.md](TESTING.md)):

- **Differential gate** (`tools/diff_lxml.py`): **0 DIVERGE / 0 CRASH** across ~778k (page ×
  selector/group) pairs per seed — conformant CSS/XPath, comma groups, sibling combinators, universal
  `*` terminals, `<svg>`/`<math>`/`<template>` foreign content, and single-pass `Many`/`One`.
- **Differential fuzzing**: mutated malformed HTML (`tools/diff_fuzz.py`) and random selectors
  (`tools/sel_fuzz.py`) vs lxml — no crash, no non-empty-wrong value; residual divergences are all
  documented SKIP constructs.
- **Coverage-guided fuzz** (`cargo-fuzz`, `fuzz/`): arbitrary bytes → no panic / hang / OOB.
- **Unit**: 109 hand-written vectors incl. tokenizer conformance (`cargo test`).

To audit a specific query, use `frostwork.check` or run it through `tools/diff_lxml.py`'s verdict
logic. In permissive Python mode, an unsupported query yields `[]`.
