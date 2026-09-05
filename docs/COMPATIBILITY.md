# Frostwork compatibility contract

Frostwork is a **no-fallback** engine: unlike engines that route anything outside their native subset
to a full parser (Parsel/lxml) so results always match, Frostwork answers **every** query itself. So a
query falls into one of four buckets:

| bucket | meaning |
|---|---|
| ✅ **supported** | runs on the streaming engine with the matching, ordering and value semantics stated in its row |
| ≈ **divergent** | runs, but may differ from lxml on specific documented and bounded constructs |
| ➕ **beyond** | runs, and answers where lxml/Parsel refuses or loses the value — collected in [Beyond lxml](#beyond-lxml) |
| ∅ **unsupported** | the engine produces an **empty result**; public Python APIs raise by default and expose this behavior with `strict=False` |

The promise is *close to lxml, always one streaming pass, never a DOM*. Unless a row marks an exception,
the correctness bar is non-whitespace value parity with lxml (libxml2 2.14), not the HTML5 spec. A ✅ row
that can return a bare element covers selector matching and ordering; that value still inherits the
documented raw-source outer-HTML exception.

**Parity is the bar, not the ceiling.** "Close to lxml" is a one-directional phrase and this contract is
not: a dozen constructs below run *here* and refuse, truncate or mis-decode *there*, from `:has([data-x])`
to a UTF-16 response body. They are one section — [Beyond lxml](#beyond-lxml) — so a coverage number
read off this document is not mistaken for the whole comparison.

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
| class / id (`.c`, `#i`), including CSS escapes (`.sm\:text-lg`, `.\73 hared`, `#i\31`) | ✅ supported — literal punctuation and hex escapes are decoded; whitespace terminating a hex escape is not a descendant combinator. NUL/surrogate/XML-forbidden characters, decoded backslashes and whitespace inside a class name remain unsupported |
| attribute `[a]`, `[a=v]` | ✅ supported |
| attribute operators `[a^=v]` `[a$=v]` `[a*=v]` `[a~=v]` `[a\|=v]` | ✅ supported (empty operand matches nothing; case-sensitive by default — matches lxml) |
| the Selectors 4 case-sensitivity flag — `[type=submit i]`, `[href$=".PDF" i]` (and the no-op `s`, which is the default) | ➕ **beyond** — valid CSS that cssselect rejects (`Expected ']', got <IDENT 'i'>`). ASCII folding of the VALUE, on every operator, exactly as CSS defines it: `[data-x="café" i]` does **not** match `CAFÉ`, because a Unicode fold is not what the flag means. A flag is only a flag after whitespace — `[a=vi]` is the two-character value `vi` — and any other letter (`[a=v x]`) stays ∅ unsupported |
| attribute value **unquoted**: a CSS identifier only (`[a=v]`, `[a=-v]`, `[a=café]`) | ✅ supported. A non-identifier unquoted value (`[a=2]`, `[href^=/p]`, `[a=$v]`, `[a=--v]`, `[a=a.b]`) is ∅ unsupported (empty) — cssselect rejects those as a syntax error, so answering them would return values for a selector Parsel refuses. Quote it (`[a="2"]`, `[href^="/p"]`). A QUOTED value may hold XPath-compatible characters. A lone trailing backslash or an escape producing a backslash is refused (cssselect can reinterpret it in a second escape pass), and **CSS escapes in it are decoded** the way cssselect decodes them — `[data-x="\61"]` selects `data-x="a"`, not the literal two characters. Same for a `::attr()` argument (`::attr(data-\6b)` = `::attr(data-k)`). Escapes in a **type** / attribute *name* (`\70`, `[data-\6b]`) or unquoted attribute value remain ∅ unsupported (empty); class and ID escapes are supported |
| `:not(<compound>)` incl. compound arg (`a:not(a.x)`) and chained (`:not(.x):not(.y)`) | ✅ supported |
| `:not()` with a compound LIST — `p:not(.a, .b)` | ➕ **beyond** — Selectors 4, and exactly the chained spelling (`:not(.a):not(.b)`) the element must match none of. cssselect rejects the list and accepts the chain, which is what makes the chain a direct oracle for it. An empty member (`:not(.a, )`) is a syntax error to cssselect and stays ∅ unsupported |
| descendant (`a b`), child (`a > b`) | ✅ supported |
| adjacent / general sibling (`a + b`, `a ~ b`), incl. chains (`a ~ b ~ c`), a descendant/child **base** (`.list li ~ li`), and a trailing descendant/child **step** (`a + b c`, `input + label p::text`) | ✅ supported |
| `::text` — self (`E::text`) and descendant-or-self (`E ::text`) | ✅ supported (per text node, whitespace kept, entities decoded) |
| `::attr(name)` — self and descendant-or-self (`E ::attr(x)`) | ✅ supported (entity-decoded) |
| a value terminal with **no subject compound** after an explicit combinator — `dt + ::text`, `dt ~ ::text`, `div > ::attr(id)` | ✅ supported, and identical to the `*` spelling (`dt + *::text`). Parsel strips the pseudo-element itself before cssselect sees the selector, so the terminal reads as attached to an implicit universal; scope is that element's **own** value, never the subtree collapse (which applies only to a descendant-combinator `*`). A trailing combinator with **no** terminal (`dt +`) stays ∅ unsupported, as cssselect rejects it |
| bare element → outer HTML (`div`, `.card`) | ≈ divergent — **raw source**, not lxml's reflow (see below) |
| comma list, same terminal (`h1::text, h2::text` · `.a::attr(href), .b::attr(href)` · `h1, h2`) | ✅ supported (document-order union, per-column de-dup; bare-element members retain the raw-source exception) |
| comma list, **mixed** value terminals (`a::text, a::attr(href)` · `img::attr(src), img::attr(data-src)`) | ✅ supported (document-order union: `::attr` at element-open in source order, `::text` at text nodes; de-dup by node) |
| comma list, bare-element (outer HTML) mixed with a value terminal (`b, b::text`) | ∅ unsupported (empty) — deferred captures can't interleave with streamed values in document order |
| forward position — `:first-child`, `:nth-child(An+B)`, `:first-of-type`, `:nth-of-type(An+B)` (incl. `odd`/`even`, `-n+3`); composes inside `:not()` | ✅ supported (`:nth-child` counts element siblings; `:nth-of-type` needs a concrete tag) |
| reverse position — `:last-child`, `:last-of-type`, `:only-child`, `:only-of-type`, `:nth-last-child(An+B)`, `:nth-last-of-type(An+B)` — on ONE compound of a lone selector. The value may be that element's **own** (`li:last-child::text`), its **subtree** (`li:last-child ::text`), or a **descendant's** (`li:last-child b::text`) | ✅ supported (resolved at the parent's close; `-of-type` needs a concrete tag). An own-value terminal streams; the other two are recovered afterwards by re-scanning the winner's raw span — see the note below |
| reverse position in any OTHER shape — a **child** step into the value tail (`li:last-child > b::text`), a comma group, a `Many`/`One` sub-field, or `*`-of-type (`*:nth-last-of-type`) | ∅ unsupported (empty). The deferred re-scan does not yet carry a child anchor; a descendant tail has different semantics |
| `:has(<compound>)` / `:has(> <compound>)` — on ONE compound of a lone selector; the value may be that element's **own** (`div:has(a)::attr(id)`), its **subtree** (`div:has(a) ::text`), or a **descendant's** (`div:has(a) a::attr(href)`) | ✅ supported (resolved at the subject's own close). The inner is a single compound: tag/`*`, id, classes, attribute predicates, `:not(...)` — `:has(a)`, `:has(.price)`, `:has([data-src])`, `:has(#main)`, `:has(a.buy)`, `:has(:not(.hidden))`, `:has(> img)`. **≈ divergent** for id/attribute/`:not` inners — see below |
| `:has()` with a relative selector LIST — `div:has(a, img)`, `div:has(> a, > img)`, `div:has([data-x], #main)` | ➕ **beyond** — the shape [scrapy/cssselect#138](https://github.com/scrapy/cssselect/issues/138) names as unsupported. An element matches if ANY member is found, so the column equals the union of the single-member spellings (`div:has(a), div:has(img)`), which is the oracle. Every member is one compound under the same relative combinator; a list MIXING them (`:has(> a, img)`) is ∅ unsupported, since a `:has()` carries one relation and answering it under whichever half arrived first would be a guess |
| `:has()` on a PRECEDING-SIBLING compound — `C:has(..) ~ S` / `C:has(..) + S` (value from the later sibling `S`) | ✅ supported — same mechanism as the sibling text-predicate: `C`'s `:has` fires the sibling boundary at `C`'s close, `S` emits normally (single sibling combinator) |
| `:has()` in any OTHER shape — an inner with a **chain/sibling** (`:has(.a .b)`, `:has(a + b)`) or a positional/reverse/`:has`/`:is` inside, multiple `:has`, a **child** step into the value tail (`div:has(a) > p::text`), or a `Many`/`One`/comma member | ∅ unsupported (empty) |
| a QUOTED delimiter inside a functional pseudo's argument — `div:is(#a, [data-x=")"])`, `:not([title='a(b'])`, `:has([data-x="a, b"])`, `:is([class="a,b"])`, and the escaped forms (`[data-x="\)"]`) | ✅ supported — argument boundaries are quote- and escape-aware, so a `)` or `,` inside a value is data. Genuinely unterminated syntax (`:is(#a, [data-x=")"]::attr(id)`) stays ∅ unsupported (empty), matching cssselect's rejection |
| `:is(...)` / `:where(...)` — a comma-list of compound alternatives (`:is(h1, h2, h3)`, `div:is(.a, .b)`, `a:is([href], [src])`), including combined with other conditions (`div.card:is(.a, .b)`) or chained (`x:is(a, b):is(c, d)`) | ✅ supported — element matches iff it matches ≥1 alternative in EVERY group (OR within a group, AND across groups). `:is`/`:where` are identical (specificity is irrelevant to matching). Agrees with cssselect ≥ 1.5.0; **≈ divergent** for combined/chained forms against cssselect ≤ 1.4.0 — see below |
| `:is(...)` with a combinator inside an alternative (`:is(.a .b)`, `:is(a + b)`), a positional/reverse/`:has` inside an alternative (`:is(:first-child)`), or a nested `:is` | ∅ unsupported (empty) — cssselect itself rejects the combinator forms |
| `:contains("v")` — cssselect's extension. One STRING or IDENT argument, on ONE compound of a lone selector. The value may be that element's **own** (`dt:contains("Price")::text`), its **subtree** (`div:contains("x") ::text`), a **descendant's** (`div:contains("x") a::attr(href)`), or a following **sibling's** (`dt:contains("Price") + dd::text` — the label→value pattern) | ✅ supported — cssselect translates it to `contains(., "v")`, so it is the same deferred text predicate as the XPath spelling below and inherits its semantics exactly: `.` is the whole string-value (subtree text concatenated), the match is a **substring** with no whitespace trimming, and `:contains("")` is always-true. Resolved at the element's own close |
| `:contains()` as a **comma-group member** (`h2::text, p:contains("x")::text`) | ✅ supported when the member's value is its OWN element's and every member names the same KIND of node — all `::text`, or all `::attr` with the same name. The column is merged by document offset and deduped by node, so member order does not affect it and a node both members select appears once, exactly as in lxml's union |
| `:contains()` in a comma group whose members name DIFFERENT nodes (`p::attr(id), p:contains("x")::attr(class)`, `p::text, p:contains("x")::attr(id)`) or whose value comes from a SUBTREE (`h2::text, div:contains("x") ::text`, `h2::text, div:contains("x") a::attr(href)`) | ∅ unsupported (empty) — two attributes of one element are two distinct nodes sharing that element's offset, which lxml returns both of, so an offset-keyed merge cannot tell them apart; and a subtree value comes from a nested re-scan whose values carry no document offset at all. Refused rather than ordered by arrival |
| `:contains()` in any OTHER shape — a **second** one on the same compound (`p:contains("a"):contains("b")`, which cssselect ANDs), one inside `:not()`/`:is()`, a `Many`/`One` member, alongside a `:has()`/reverse position on the same compound, or a **child** step into the value tail (`div:contains("x") > p::text`) | ∅ unsupported (empty) — use the descendant form for the last one. The `:is()`/`:not()` refusal is a SAFETY one rather than an omission: `compile::any_compound` walks `negations` but not `is_groups`, so accepting it would stream the selector as ordinary matching with the text constraint silently DROPPED — an over-match, which is the one outcome no-fallback exists to prevent |
| `:contains()` inside `:has()` (`div:has(p:contains("x"))`) | ∅ unsupported (empty) — and **not a coverage gap**: cssselect cannot parse a pseudo-class inside `:has()` at all (`SelectorSyntaxError: Expected an argument, got <DELIM ':'>`), so Parsel refuses it too. A refusal the oracle shares is parity |
| `:contains()` with a non-STRING/IDENT argument — `:contains()`, `:contains(2)`, `:contains(a b)` | ∅ unsupported (empty) — cssselect's `xpath_contains_function` raises `ExpressionError` on these, so answering them would return values for a selector Parsel refuses |
| `::first-line`, other pseudos | ∅ unsupported (empty) |
| `:not()` with a combinator argument (`:not(a b)`), namespaces (`ns\|tag`) | ∅ unsupported (empty) |
| a **non-ASCII TAG** name (`café::text`, `x-é::text`) | ∅ unsupported (reported, not silently empty) — lxml *does* match them, so this is a coverage gap, and a deliberate one: accepting them would return values lxml never had. cssselect lowercases a type selector with Python's **Unicode** `.lower()` while libxml2 lowercases the tree ASCII-only, so `cafÉ` matches `<café>` there while `<CAFÉ>` is unmatchable by any selector; and a tag is compared as raw page bytes, where ASCII-folding a legacy multi-byte sequence collides real characters (949 pairs in shift_jis — `П` 0x8450 folds onto `а` 0x8470). Measured over 30000 crawled pages, lxml built a non-ASCII element on 5, all mojibake or an NBSP separator. Non-ASCII **class/id/attribute** names (`.café`, `#año`, `[data-año]`, `::attr(año)`) are fully supported, **in every encoding** — see [Encoding](#encoding) |

## XPath (downward subset)

| feature | status |
|---|---|
| document-rooted paths (`//div`, `/html/body/p`); `.`-relative **descendant** (`.//a`) | ✅ supported |
| `.`-relative **child** anchor (`./div`, `./h3/text()`) | ✅ supported in grouped subfields, anchored to the container's immediate children; ∅ unsupported as a flat query. `.//` selects all descendants and is not an equivalent rewrite |
| `//`→descendant, `/`→child steps; `*` and tag node tests | ✅ supported |
| predicates `[@a]`, `[@a="v"]`, `[contains(@a,"v")]`, `[starts-with(@a,"v")]`, `[… and …]`, `[… or …]` | ✅ supported (a predicate `or` is distributed into union members). The compared value must be a **quoted string literal** |
| comparison against a NON-literal operand — a variable reference (`[@id=$pid]`), a number (`[@a=2]`), or a bare name (`[@a=b]`) | ∅ unsupported (empty) — Frostwork's API takes no variable bindings (unlike `sel.xpath(q, pid=…)`), and XPath gives `[@a=2]` numeric semantics (`a="02"` matches) and `[@a=b]` node-set semantics (compare against child `<b>` elements), none of which is a byte compare. Quote the value if a literal is meant. A `$` **inside** a literal (`[contains(@id,"$p")]`) is just data |
| terminals `text()` (self), `//text()` (descendant), `/@attr`, `//@attr` (descendant-or-self), bare element → node | ✅ supported (bare elements retain the raw-source exception) |
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
| an ASCII-UPPERCASE name (`//DIV`, `//rect/@ID`, `//svg/@viewBox`, `//p[@data-AÑO]`) | ∅ unsupported (reported, not silently empty) — XPath name-tests compare case-sensitively while libxml2 lowercases every HTML name in the tree, so these match nothing in lxml either. Refusing is parity, not a gap |
| a **non-ASCII ATTRIBUTE** name (`//p[@data-año]`, `//p/@año`, `//p[@属性]`, `//p[@Ñ]`) | ✅ supported, **in every encoding** — the same answer as the CSS `[data-año]` / `::attr(año)` spellings. A non-ASCII *uppercase* is kept (`//p/@Ñ` matches) precisely because libxml2 folds ASCII only, so neither side lowercased it. A non-ASCII **TAG** name is still ∅ unsupported (reported) — see the CSS table for why |
| other functions (`count()`, `string()`, `substring()`, …) | ∅ unsupported (empty) |
| context `@href`, `text()`, `.` | ✅ supported in grouped subfields only; bare child paths such as `td/text()` remain unsupported — spell `./td/text()` |
| `contains(@a,"")` / `starts-with(@a,"")` — **empty operand** | ≈ divergent — returns **empty** (CSS empty-operand semantics: an empty needle matches nothing), whereas XPath-proper `contains` with `""` is always-true. Non-empty operands match lxml. |

XPath compiles to the **same** selector model as CSS, so a supported XPath query has identical
semantics and performance to its CSS equivalent.

## Nested / grouped extraction (`Many` / `One`)

| feature | status |
|---|---|
| `Many`/`One`: per-container sub-fields, one streaming pass | ✅ supported — scoping, row order and cardinality match Parsel's per-container loop; bare-element values retain the raw-source exception |
| container = any immediate (open-time) supported CSS/XPath selector; nested & empty containers | ✅ supported (a nested same-class container yields its own row; a value falls into every enclosing container's row, matching Parsel) |
| sub-field terminals: `::text`, `::attr(x)`, bare element (outer HTML) | ✅ supported (same value semantics and raw-source exception as flat fields) |
| sub-field with descendant/child combinators (`h3 a::text`, `.p > span::text`) | ✅ supported |
| sub-field scope axis: CSS is descendant-or-**self** (may match the container); XPath `.//` is strict **descendant** (excludes it) | ✅ supported — matches Parsel's `c.css(sub)` vs `c.xpath('.//…')` |
| subfield context: `./child`, `@attr` / `./@attr`, `text()` / `./text()`, `.//text()`, `.//@attr`, `.` | ✅ supported — immediate child, own attribute, own text nodes, subtree text, subtree attributes, and own raw HTML respectively. Join `.//text()` with an empty separator for the container string value |
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
`src/implied_close/`):

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
  - **`<colgroup>`** is closed by `<colgroup>`/`<thead>`/`<tbody>`/`<tfoot>`/`<tr>` — but not by
    `<col>` (void, so never the open element), a cell, or a caption. It closes an open `<caption>` and
    an open `<p>`. An omitted `</colgroup>` is ordinary table markup: without this rule
    `<table><colgroup><col><col><thead><tr><th>H` nested the sections inside the colgroup, so
    `table > thead th::text` returned nothing where lxml returns the cell.
- **An open `<p>`** is closed by the block set plus list/table *items* (`li`, `dd`, `dt`, `tr`, `td`,
  `th`, `tbody`, `tfoot`, `caption`, `p`) — but **not** by `option`, `optgroup`, `thead`, `rt` or `rp`,
  which nest inside it. Void tags count: `<col>` and `<hr>` close an open `<p>` too.
- **libxml2's start-close NAME-pair relation** (`htmlStartClose`) is **generated from the oracle**
  (`implied_close::start_closes`, written by `tools/gen_tree_rules.py`). It is a *name*-pair rule,
  deliberately finer than the implied-close ids, and it is why two elements that look interchangeable
  are not:
  - an incoming `<p>` closes an open `<b>`, `<i>`, `<u>`, `<big>`, `<small>`, `<tt>`, `<s>`, `<strike>`
    and any `<h1>`–`<h6>` — but **not** an open `<em>` or `<strong>`. So `<h1>Title<p>Body` and
    `<b>x<p>y` produce siblings, which is ordinary legacy and generator markup.
  - an incoming `<td>`/`<th>` closes an open `<a>`, `<b>`, `<i>`, `<u>`, `<font>`, `<span>` and a cell;
    `<table>` closes an open `<a>`, `<pre>`, `<listing>` and any heading, but **not** an open `<div>`.
  - `<a>` closes an open `<a>`, and `<form>` closes an open `<form>` — the unclosed-link and
    form-in-form cases.
  - `<fieldset>` closes an open `<legend>`; the list and definition starts (`li`, `dd`, `dt`, `dl`,
    `ul`, `menu`, `dir`, `form`, `pre`, `address`, `listing`) close each other per the pair list.
  - the obsolete names are in it too, and were the last gap: an open `<listing>` behaves exactly like an
    open `<pre>` (closed by `dd`/`dl`/`dt`/`fieldset`/`form`/`li`/`table`/`ul`), while incoming
    `<listing>`, `<xmp>` and `<title>` close an open `<p>`.
  - **an open `<head>` is a column of the table**, closed by essentially every body-level start tag
    (`a`, `b`, `div`, `p`, `table`, `li`, `hr`, any heading, …) but not by `<meta>`, `<link>`,
    `<style>`, `<script>` or a second `<title>`. Without it `<html><head><title>T</title><body><p>X</p>`
    left the whole `<body>` nested inside the `<head>`, so `html > body p::text` was empty.

  The relation is **derived**, not transcribed, and derived over the whole element universe rather than a
  set of representatives — the previous hand-written port called itself complete while `head`, `listing`,
  `xmp` and `plaintext` were absent from both its source list and the audit's. `tools/gen_tree_rules.py`
  measures every (open × incoming) pair against libxml2, proves its name universe is a superset of both
  the engine's own names and an independent element index, and regenerates the Rust table; `--check`
  fails on drift. `tools/audit_tree_rules.py` then re-checks the ENGINE against every pair by value, and
  `tools/mutate_rules.py` verifies that flipping a cell is noticed by a gate.
- **`<html>`, `<head>` and `<body>` are accepted only where the frame admits them**, and each has its own
  rule — swept over the whole element universe crossed with "is a body open" (`frame-in-element` in
  `tools/audit_tree_rules.py`), because reading them as one rule got two of the three wrong:
  - a **`<head>`** belongs to the phase before any body content, so anything else already open ends that
    phase and the tag is ignored;
  - a **`<body>`** is ignored only while one is OPEN. After a `</body>`, libxml2 starts a second body
    wherever the next `<body>` is written — inside a `<td>`, a `<div>`, or a `<frameset>` (whose document
    never had one, which is how a frameset page writes its no-frames fallback);
  - a **`<html>`** is ignored while one is open, but a second one *after* the first has closed gets its
    own ROOT element, because that is what libxml2 builds. Browsers keep a single `<html>` instead, so
    parsel's own CSS and XPath disagree about such a document — `.css('html')` is scoped to the first
    root and reports one, `//html` reports both. The tree is the oracle here, so `//html/@x` is right and
    the CSS side of such a page is a parsel scoping artifact.

  An ignored start tag inserts nothing (`<div>d<html>y</div>` is one text node `dy` under the div) and its
  implied closes still run first (`<body>` closes an open `<p>` and an open `<head>`) — but libxml2 keeps
  a stack SLOT for the tag it merged away, so the next frame END tag pops that phantom instead of closing
  the document. `<div><body>x</body>tail</div>` keeps `xtail` inside the div, and it is a slot rather than
  a named token: **any** of the three end tags pops a phantom left by **any** of the three, so a stray
  `<html>` in the body makes the document's own `</body>` a no-op.
- **The document frame is SYNTHESIZED when the page omits it.** `<html>`, `<head>` and `<body>` all have
  optional start *and* end tags, so a conformant document may contain none of them — and libxml2 builds
  the frame regardless, so the engine does too. `<!DOCTYPE html><title>T</title><h1>a</h1><p>b</p>`
  answers `head > title::text`, `body h1::text` and `h1 + p::text` (top-level siblings need a shared
  parent to be siblings at all), and root-level text is body text rather than dropped. Which part a bare
  element opens is derived from the oracle over the whole element universe (`implied_close::
  frame_content`, audited as `document-frame synthesis`), because it is not the relation it resembles:
  only `base`/`link`/`meta`/`script`/`style`/`title` open a `<head>`, while `input`/`noscript`/
  `template`/`basefont`/`bgsound`/`object` survive inside a head that is already open and open none.
  Whitespace before the frame starts is not content and starts nothing. A frame the page *does* write is
  used as written; nothing is invented on top of it — but a page whose FIRST tag is `<head>` or `<body>`
  has still omitted its `<html>`, and gets one. (Without that the head sat at the root with no parent and
  a second `<html>` was built for whatever followed `</head>`, so `html > head`, `html > body` and
  `head + script` were empty while the values under them looked right.)
- **What ends an open `<head>` usually starts an implied `<body>`.** The first non-head element or
  non-whitespace character ends the head; normally it and everything after it become body content, and a
  later explicit `<body>` is redundant. Leading whitespace stays in the head. `<frameset>`, `<frame>` and
  `<noframes>` are the exception: they open no body, and a frameset document has none. If a body is already
  open — reachable by writing a new `<head>` after `</body>` — the head still closes but its content remains
  at `<html>` level. The behavior is derived from the oracle over the full element universe because the
  names that end an open head are not the same as those that open a body after an explicit `</head>`.
- **A `<!DOCTYPE …>` does not break a text node**, and is the only declaration form that does not:
  `<div>a<!doctype html>b</div>` is the single node `ab`, while `<!foo>`, `<![CDATA[…]]>`, `<?x?>` and
  `<!>` all split the run in both engines. libxml2 matches the seven-letter prefix case-insensitively
  and does not require the name to be terminated, so `<!doctypex>` is one and `<!doctyp>` is not.
- **An end tag cannot unwind anything that OUT-RANKS it** ("end-tag scope"). libxml2 gives each element
  name an *end priority* and discards a misplaced end tag while something higher-priority is still open
  above its match. The order is
  `body` > `table` > `thead`/`tbody`/`tfoot` > `tr` > `td`/`th` > `div` > everything else (`caption` and
  `colgroup` are **not** boundaries; nor is a peer block such as `<blockquote>`), so:
  - `<div><table><tr><td>A</div>B` drops the `</div>` and the cell keeps `AB` as one text node —
    unbalanced `<div>`s around tables are a common real-world malformation;
  - `<nav><div>A</nav>B` keeps `AB` inside the div;
  - `<tr><strong><tbody></tr>` keeps the ROW open, because `</tr>` cannot unwind the `<tbody>` above it;
  - equal priority never blocks, so `</tbody>` still unwinds an open `<tr>`/`<td>`, and a nested
    `<table>` does not shield an outer `</table>`;
  - `<body>` out-ranks the whole table machinery, which matters wherever one can be open ABOVE a match —
    a `<body>` written after `</body>`, which a crawled page put inside a `<td>`.

  `</body>`/`</html>` close the document whatever is open. This is one comparison, not a set of boundary
  elements: it is derived and verified as such by `tools/gen_tree_rules.py` over every (open × closing)
  pair in the element universe, and `tools/audit_tree_rules.py` re-checks every pair through the engine
  by value. Reading it as a set of boundary elements instead costs a crawled page its table cells: the two
  coarsest cells look right while the order inside the table machinery is missing.
- **An end tag with no open element to match is DROPPED, and does not split the text node.** libxml2
  discards it and keeps the character data either side as ONE text node, so `<div>A</p>B</div>` yields
  `AB`, not `A` + `B`. Only source-adjacency across the dropped tag is re-joined: a comment, CDATA, PI,
  void element or real child in the gap is a node in libxml2 too, and still splits the run. Doc
  generators emit this (Sphinx: `</p>\n</p>`), and a split would silently *truncate* a `One`-cardinality
  field rather than empty it — the one failure mode no-fallback is meant to exclude.
- **Only an ASCII letter after `</` starts an end tag** (HTML5's end-tag-open state, which libxml2
  follows exactly). The other two branches are not end tags and each affects the text node differently:
  `</%>`, `</1>`, `</-x>` are **bogus comments** — a node, so they SPLIT the run — while `</>` is
  "missing end tag name" and is ignored entirely, so it does **not** split it. A bogus comment runs to
  the next `>` regardless of quoting, and both engines resume at the same byte, so this is a local
  difference and never an offset desync. `</` at end of input is character data. Reading all three as
  "scan a name, skip to `>`" merged a real page's `Copyright 1991-2026</%> VECMAR Corporation` into one
  text node where libxml2 has two, and was also the largest single source of unattributed divergences in
  the malformed-HTML fuzzer.
- **An end tag has a start tag's ATTRIBUTE states, and discards what they collect.** So — unlike the
  bogus comment above — a quoted value in an end tag carries the `>`: `</p x=">">` ends at the *second*
  `>`, and an unterminated one runs to the next matching quote or to EOF, swallowing whatever markup is
  in between. A Blogger template that writes `</img\nsrc="http:>` had libxml2 and html5lib eat the
  following `</a>`, two `</div>`s and a whole `<div id='HTML3'>` start tag; ending the tag at the first
  `>` instead kept an element that is in no other parser's tree — a FALSE POSITIVE. The rule covers the
  rawtext/RCDATA and `<script>` closers too, which are separate scanners.
- **`<p>`-closing block set** is the HTML4 block list (`div`, `p`, `h1`–`h6`, `ul`, `ol`, `dl`, `menu`,
  `dir`, `center`, `address`, `blockquote`, `fieldset`, `form`, `pre`, `table`, `hr`) — **not** the
  HTML5 sectioning elements (`section`, `article`, `header`, `footer`, `nav`, `main`, `figure`,
  `details`, …), which libxml2 treats as ordinary inline-ish containers that do **not** close `<p>`.
- **Void set**: `area base basefont br col frame hr img input isindex link meta param` — libxml2 2.14's
  set, derived by `tools/gen_tree_rules.py`, not HTML5's. Both differences from HTML5 are deliberate and
  both are tested by name:
  - the HTML4-only `basefont`, `frame` and `isindex` **are** empty, so in
    `<div><basefont><span>x</span></div>` the span belongs directly to the div;
  - the HTML5-era `embed`/`source`/`track`/`wbr` are **non-void** — libxml2 keeps them open as ordinary
    containers.
- **Data modes** (how an element's *content* is tokenized). Anything other than "ordinary markup" means
  the content is character data, so a `<` inside it starts no tag. This is not a cosmetic distinction:
  a missing mode *invents* elements that are not in the document and then honours the wrong end tag,
  desynchronizing everything after it.
  - **raw text**, entities literal: `script`, `style`, `iframe`, `noembed`, `noframes`, `xmp`. So
    `<iframe><div>fake</div></iframe>` has no `div` at all — the iframe's text is `<div>fake</div>`.
  - **RCDATA**, entities decoded: `textarea`, `title`.
  - **PLAINTEXT**: `plaintext` consumes the rest of the document as text. `</plaintext>` does not end it
    — nothing does — so in `<plaintext><div>fake</div><p>after</p>` everything after the start tag is
    one text node.
  - **`script` additionally has the escaped/double-escaped states**, so taking the first `</script>` is
    wrong: in `<script><!--<script></script><div>fake</div></script>` the inner `</script>` only leaves
    the double-escaped state, and there is no real `div`.
  - `listing` and `noscript` **look** like they belong in the list and do not: libxml2 parses their
    content as markup (it has scripting disabled). `tools/audit_tree_rules.py` sweeps the whole element
    universe for this, so the set is derived rather than guessed at.
- **Character references**: libxml2/Parsel-compatible — legacy semicolon-less names, numeric edge
  cases (`&#00`→U+FFFD, win-1252 remap).
- **A stray `=` where an attribute NAME should start is part of that name**, not a separator —
  `<div = class='x'>` has the class, exactly as libxml2 and html5lib read it (HTML5 calls this
  `unexpected-equals-sign-before-attribute-name`; html5lib names the attribute `U0003D`). Reading it as
  a separator instead gives the attribute an empty name, drops it, and swallows the real `class` after it
  as its *value* — a crawled page's `div::attr(class)` comes back a row short.
- **A start tag the response ends inside is dropped, whole** — `…<a href="/p2" class="` yields no
  element and no text, and the text before it is untouched. libxml2 and html5lib agree for every shape
  (`<a`, `<a href`, `<a href=`, `<a href="x`). Keeping the attributes already scanned instead would
  report an `<a>` whose `href` holds the rest of the document — an element no other parser has, i.e. a
  false positive. Truncated responses are not rare in a crawl: one such page accounts for 6 divergent
  columns and the shape is a major group in `tools/diff_fuzz.py`.
- **Parsel's own input normalization is reproduced, in Parsel's order.** `Selector(text=…)` parses
  `text.strip().replace("\x00", "")` — trim the ends, then delete NUL — and the engine does the same to
  the whole document before tokenizing. The order matters: parsel's *other* entry point,
  `Selector(body=…)`, deletes NUL FIRST, and the two disagree wherever a NUL sits at a document edge.
  The text path is the one reproduced here, because it is the path a scraper is on — Scrapy's
  `response.selector` and web-poet's `HttpResponse` both build their selector from `response.text`.
  - **Raw NUL is deleted document-wide**, not at value-emit time: a NUL inside a tag or attribute *name*
    is invisible to lxml (`<di\0v>` is a `div`), so dropping it only from emitted values would leave the
    two sides disagreeing about the document's STRUCTURE and `div::text` empty. For UTF-16 input the
    deletion applies to the DECODED text, since in UTF-16 every ASCII character carries a 0x00 byte. A
    character *reference* to NUL (`&#0;`) is a different thing and still becomes U+FFFD.
  - **A leading BOM may be INDENTED.** On a page that indents its doctype — `"    ﻿<!DOCTYPE HTML>"` —
    the strip promotes the U+FEFF to offset 0, where libxml2 eats it as a BOM. Read the bytes as they
    arrive and that U+FEFF is a *character* instead, and a character before the frame opens the `<body>`:
    the page's `<head>`, its `<title>` and even the attributes of its own `<html>` tag (redundant once a
    body is open) are all lost, so `head title::text` and `html::attr(xmlns)` come back silently
    **empty**. libxml2 on the raw bytes and html5lib both agree with the raw reading — this is Parsel
    normalizing its input, and matching what a scraper sees is the point. Only whitespace may precede the
    BOM, and the BOM half applies **only in UTF-8**: in windows-1252 those three bytes are `ï»¿`, three
    real characters, and Parsel's decode agrees.
  - **Trailing whitespace is not a text node**, the other half of that same `strip()`. It can only ever
    move whitespace, so it changes no value — but it changes how many a column returns: a page ending
    `…<option class="c3">\n` gives that option a text node here and none under Parsel.
  - **The whitespace stripped is Python's ASCII set, which includes VERTICAL TAB** (`0x0B`) — not HTML
    whitespace, and not Rust's `is_ascii_whitespace` either, both of which exclude it. It is a real page
    shape and not a curiosity: `0x0B` is not whitespace to libxml2, so an un-stripped leading one is
    character data before the frame and costs the page its whole head, exactly as the un-promoted BOM
    above does. Unlike the BOM this half is **not** gated on UTF-8, because Parsel strips the decoded
    text whatever the label says.
  - ⚠️ **Leading or trailing UNICODE whitespace (U+00A0, U+3000, U+2028, …) is not stripped**, and there
    Parsel's two entry points disagree with each other: `str.strip()` removes them, `bytes.strip()` does
    not. The engine matches the `body=` answer here and keeps them, so a page that begins with a stray
    NBSP has that character before its frame — which opens the `<body>` and loses the head, the same
    silence as above. Chasing it would mean picking one Parsel path over the other on a shape neither
    browsers nor libxml2 treat as whitespace, so it is documented rather than matched.

## Value semantics

- `::text` / `text()` — one value per text node, whitespace preserved, entities decoded. Matches Parsel.
- `::attr(name)` / `@attr` — entity-decoded attribute value. HTML4 minimized boolean attributes use
  their lowercased name as the value (`<input DISABLED>` → `"disabled"`), while `disabled=""` remains
  explicitly empty, matching libxml2/Parsel.
- **Outer HTML** (bare element / node query) — the element's **raw source bytes**, a *deliberate*
  divergence from lxml's tree reflow (lxml normalizes attribute quotes/case/whitespace and synthesizes
  omitted end tags). Raw source is cheaper, faithful to the page, and **re-parse-equivalent** (parsing
  it yields the same node set + non-whitespace text as lxml). Boundaries come from the corrected stack,
  so an unclosed `<li>` captures `<li>…` up to its implied close.

  **Newlines are normalized, and that is not a compromise of "raw":** HTML turns `\r\n` and lone `\r`
  into `\n` in the *input stream*, before anything parses, so every node libxml2 or html5lib serializes
  carries `\n` whatever the bytes said. Text and attribute values were already normalized and only this
  path was not — an inconsistency, not a divergence, and a CRLF-authored crawled page put `\r` in all
  eight of its node columns. What stays divergent is RE-SERIALIZATION (attribute order and quoting,
  minimized booleans, entity escaping), because there is no tree to re-serialize from.

  **One exception to "raw":** NUL deletion happens before tokenizing (above), so the raw source is the
  source *after* that deletion — `<di\0v id=x>t</div>` yields `<div id=x>t</div>`. It has to be this way
  round: the offsets that bound the span come from the normalized buffer, and a NUL inside a tag name is
  not part of the tag name in any engine that agrees with lxml on the tree.

  **And one place where re-parse-equivalence holds for us but not for lxml.** lxml HTML-*escapes* the
  content of a raw-text element when it serializes, so `<i><noframes><address>x</address></noframes></i>`
  comes back as `<i><noframes>&lt;address&gt;x&lt;/address&gt;</noframes></i>` — re-parsing that yields
  the literal text `&lt;address&gt;…`, not what lxml itself parsed. The two engines *agree* on the value
  (`noframes::text` is `<address>x</address>` in both); they differ only in that lxml's outer HTML does
  not round-trip and the raw source does. It shows up whenever a comparison re-parses both
  serializations — on real pages, in `<noframes>`, `<noscript>` and `<iframe>` content.

---

## Beyond lxml

Every row here is a query lxml/Parsel **refuses to run**, a value it **loses**, or a character it
**cannot decode** — and Frostwork returns the browser-correct answer instead. They are collected because
a one-directional headline ("N% of lxml") reads as a subset claim, and the set difference runs both ways.

None of them is a licence to guess. Each is either valid CSS the oracle's *parser* rejects, or a place
the oracle is measurably wrong about the document, and each names the gate that holds it — the same
oracle-bug policy applied consistently: implement the correct behaviour, document the difference, prove
it, and let the gate go red if upstream converges.

### Selectors the oracle rejects

| construct | Parsel / cssselect 1.5.0 | Frostwork | gate |
|---|---|---|---|
| `:has()` with an **id, attribute or `:not()`** inner — `div:has([data-x])`, `div:has(#main)`, `div:has(a[href])`, `section:has(:not(p))` | `SelectorSyntaxError` (part of cssselect's broader `:has()` limits, [scrapy/cssselect#138](https://github.com/scrapy/cssselect/issues/138)) | matched, with correct CSS semantics | `tests/test_python.py::test_has_widened_inners_match_correct_semantics` — a parsel/lxml ancestor walk, since parsel cannot evaluate these directly |
| `:has()` with a relative selector **LIST** — `div:has(a, img)`, `div:has(> a, > img)` | `SelectorSyntaxError` — the same issue | matched; an element qualifies if ANY member is found | `tests/test_python.py::test_has_selector_list_matches_the_union_of_its_members`, oracled against `div:has(a), div:has(img)` — the union it means, spelled the way cssselect *can* run |
| `:not()` with a compound **LIST** — `p:not(.a, .b)` | `SelectorSyntaxError` | matched; the element must match none of them | `tests/test_python.py::test_not_selector_list_matches_the_chained_spelling`, oracled against `:not(.a):not(.b)` — exactly equivalent, and accepted |
| the **case-sensitivity flag** — `[type=submit i]`, `[href$=".PDF" i]` | `SelectorSyntaxError: Expected ']', got <IDENT 'i'>` | ASCII-folded value comparison on every operator, as Selectors 4 defines it | `tests/test_python.py::test_attribute_case_insensitive_flag_matches_an_lxml_fold`, oracled against an lxml `translate()` of the same test |
| `:is()`/`:where()` **combined or chained** with another condition — `div.card:is(.a, .b)`, `x:is(a, b):is(c, d)` | correct at **≥ 1.5.0**; cssselect **≤ 1.4.0** ORs each alternative onto the base compound, so `div.a:is(.x, .c)` becomes `div[a or x or c]` — an over-match | correct AND-across-groups / OR-within-a-group at every cssselect version, since Frostwork does not depend on one | `tests/test_python.py::test_is_where_matches_correct_and_semantics`, oracled against the comma expansion both versions translate correctly |

Bare type/`*`+class `:has()` inners agree with parsel exactly, so these are coverage the oracle refuses
rather than semantic differences — and each is oracled against a spelling parsel *can* evaluate, so
"cssselect cannot parse it" never becomes "nothing checks it". The `:is()` row is listed even though
cssselect ≥ 1.5.0 agrees, because **the version is the user's, not ours**: a scraper comparing against a
parsel that carries ≤ 1.4.0 sees Frostwork return the standards-compliant node set, a strict subset of
that parsel's over-match. Upstream references:
[scrapy/cssselect#135](https://github.com/scrapy/cssselect/issues/135),
[#108](https://github.com/scrapy/cssselect/issues/108).

Each has a REFUSAL beside it, because a parser that accepts more is not the same thing as an engine that
answers more: a `:has()` list mixing relative combinators (`:has(> a, img)`), an empty `:not()` member
(`:not(.a, )`) and a bogus attribute flag (`[a=v x]`) are all ∅ unsupported and reported. `tools/sel_fuzz.py`
deliberately does **not** generate any of these forms — its oracle is parsel, so the correct answer would
grade OVERMATCH; they are covered by
`tests/test_python.py::test_selector_grammar_surface_obeys_the_contract`, which declares each one with its
expected values. A new beyond-lxml capability goes in that sweep, not in the fuzzer's generators.

### Values libxml2 loses

| construct | lxml / Parsel | Frostwork | gate |
|---|---|---|---|
| an element or attribute **name longer than 100 characters** | libxml2 parses names into a fixed 100-byte buffer and silently keeps the first 100, so the page's real name matches nothing | the whole name, as html5lib and every browser keep it | `tests/test_python.py::test_names_longer_than_libxml2s_buffer_are_kept_whole` |
| **content after `</html>`** | libxml2 stops building the tree there and discards the rest | kept, under the second root `<html>` libxml2 itself builds for a second `<html>` start tag | `src/lib.rs::content_after_close_html_is_kept`, which pins the browser comparison in both directions |
| a **FORM FEED between class names** (`class="a&#12;b"`) | one class — cssselect's `.x` goes through `normalize-space(@class)`, which is XML whitespace and has no FF | two classes — HTML's ASCII whitespace set, which has it | `src/lib.rs::class_lists_split_on_ascii_whitespace_only`, which sweeps both separator sets; `[attr~=v]` tokenizes identically and is checked there too |
| **outer HTML that re-parses to what was parsed** | lxml HTML-*escapes* raw-text content when it serializes, so `<i><noframes><address>x</address></noframes></i>` comes back with `&lt;address&gt;` and does not round-trip | raw source, which does | `tools/diff_lxml.py::verdict`, which re-parses **both** serializations rather than comparing bytes — the comparison that sees it, on `<noframes>`, `<noscript>` and `<iframe>` content |

The first three are **extra values**, which is the one direction that can surprise a port — see the
migration caveats under **Documented divergences** below, where the `</html>` entry keeps its full
three-way browser comparison and the 100-character entry explains why a selector copied out of an lxml
tree can come back empty here.

### Encoding beyond w3lib and Parsel

| construct | Parsel / w3lib | Frostwork | gate |
|---|---|---|---|
| **sniffing a `<meta charset>` at all** | `parsel.Selector(body=…)` never looks: it defaults to UTF-8 and every value carries U+FFFD. Scrapy users get sniffing from w3lib, one layer up | BOM → BOM-less UTF-16 → caller label → a `<meta>` prescan bounded the way a BROWSER bounds it → UTF-8, with no caller label needed | `tools/enc_check.py` |
| a declaration **after `<body>`**, or **after an unsupported label** | w3lib's regex has a `\|body` alternative and gives up there, and it stops at its first hit rather than continuing past a label it cannot resolve | honoured — browsers do not stop at `<body>`, and WHATWG treats an unsupported label as "failure, continue" | the difference table under [Encoding](#deliberate-differences-from-w3lib), each row asserted in **both** directions so an upstream fix fails as stale |
| a declaration **deep in the `<head>`** | ignored past 4096 bytes | honoured at any depth — measured in Chrome at 1KB/4KB/16KB/64KB/256KB **and 1MB**. A browser meeting the `<meta>` after its prescan budget runs "change the encoding" and re-decodes, so the budget is not a correctness cap. Legacy pages put `Content-Type` at byte ~1100–1600 behind a producer comment or a block of `og:` metas | `src/encoding.rs`; reasoning under [Encoding](#encoding) |
| a **UTF-16 response body** | lxml's HTML parser cannot parse UTF-16 bytes at all — `Selector(body=…, encoding="utf-16")` returns `[]`/errors | decoded, matching the decode-first result Scrapy uses | `src/lib.rs::encoding_utf16_bom_transcode`, `src/encoding.rs::bomless_utf16_is_detected_from_the_xml_prefix` |
| legacy bytes the **Python codec leaves undefined** | U+FFFD — `AD A1` is the `①` of ordinary Japanese prose, which strict `euc_jp` has no mapping for | the WHATWG character a browser shows: **457** such sequences in euc-jp, **192** in big5 | `tools/decoder_sweep.py`, swept over every two-byte sequence in each legacy lead/trail space |
| bytes where the **two indexes disagree** | Python's legacy codecs | WHATWG's: 5 windows-1252 bytes, 11 big5 and 6 euc-jp sequences, and 20 in gb18030 where GB18030-2005 moved characters out of the private use area (`A3A0` is U+3000 here, U+E5E5 there) | same sweep, gated by exact count in both directions |

`iso-8859-1` **means windows-1252** here — the WHATWG label table, what browsers do, and what Scrapy does
via `w3lib.encoding.resolve_encoding`. A *raw* `parsel.Selector(body=…, encoding="iso-8859-1")` bypasses
w3lib and applies Python's literal latin-1 codec, so a real page's en dash (`0x96`) is `–` here and in
Scrapy and `U+0096` there. A harness that grades against raw Parsel is measuring its own oracle
construction.

### Capabilities with no lxml equivalent

- **`frostwork.detect_encoding(body, label=None)`** returns the encoding a scan would use, as a WHATWG
  name — the whole resolution above, reachable without extracting anything. Parsel cannot answer it at
  all (`Selector(body=…)` never sniffs), and w3lib answers differently in the places tabulated below. It
  is the same function `extract` runs, not a second opinion.
- **`frostwork.check` / `frostwork-audit`** answer "will this schema run?" *before* a scrape, per selector,
  with a reason. lxml has no equivalent question to ask — every selector "works", and a wrong one is an
  empty column at runtime. See [PYTHON.md](PYTHON.md) §4 and [MIGRATION.md](MIGRATION.md).
- **Early exit on a single-valued schema.** A `Page`/`FrostPage` whose fields are all single-valued stops
  scanning once every field has a value, so a page whose fields live in the head is answered without
  tokenizing the body — something no engine that must build a tree first can do. It is not a mode and has
  no accuracy switch: the values dropped are the ones a single-valued consumer discards anyway, so the
  item is identical to a full scan's. One `all=True`/`join=` field, one group, or one deferred selector
  (`:has()`, a reverse position, a text predicate) disarms it, because those answers can still change.
- **One pass for the whole schema, and no tree**: throughput and a working set that tracks parser state and
  returned values rather than page size. [BENCHMARKS.md](BENCHMARKS.md) has the numbers and the boundaries.

---

## Documented divergences (≈) — where "close" gives way

These run without error but can differ from lxml. They are **0% on conformant/foreign input**; on
deliberately malformed input the residual differences cluster under the constructs below. Differences
that run the *other* way — Frostwork answering where lxml refuses or loses the value — are their own
section, [Beyond lxml](#beyond-lxml); the three of them that can still cost a port keep their caveat
here.

Do not read that as "real pages never hit this". **Malformed input is not the same thing as rare
input**: ordinary doc-generator output hits the `dd`/`dt` same-tag close and the dropped-end-tag text
split, and a generated-page gate can read 100% parity while both are wrong. `tools/diff_fuzz.py`
attributes each divergence to one of these constructs and reports anything left over as **NOVEL** — that
bucket, not this list, is where the next bug will be:

- **Foster-parenting** — table-scoped elements outside a `<table>` (`<p>…<td>x</td>…`). libxml2
  relocates/ignores them; the engine nests them.
- **Adoption agency** — misnested formatting (`<b><i></b></i>`) and block content inside `<a>`.
- **Deep-`<p>`** — a block/item start closing a `<p>` that is an *ancestor* rather than the immediate
  open element (`<p><b><div>`); a known gap that leaves the un-reshaped nesting (never worse).
- **Head-only elements in `<body>`** (`<title>` in body, etc.).
- **Encoding sniffing differs from w3lib in a few named places, all of them deliberate.** The policy is
  browser/WHATWG correctness rather than w3lib parity — `<body>`, comments, an invalid label, UTF-32,
  `utf-16`/`x-user-defined` declarations, BOM-less UTF-16, and where an XML declaration counts. They are
  tabulated with their reasons under [Encoding](#encoding) below rather than repeated here, and each is
  asserted (both ways) by `tools/enc_check.py`.
- **Nested `<form>`** — libxml2 *ignores* a `<form>` start tag while another `<form>` is open; the
  engine nests it, so the inner content sits one level deeper (`<div><form><form>x` → `div > *::text`
  is `x` in lxml, empty here).
- **Names longer than 100 characters** ([➕](#values-libxml2-loses)) — **the port hazard.** libxml2 keeps
  the first 100 characters of an element or attribute name; Frostwork, html5lib and every browser keep
  the whole name. A spider written against lxml copies the **truncated** name out of the tree it can see,
  and that selector then matches nothing here — an empty column. Selectors are matched as written; the fix
  is to take the real name from the page source. Emulating a parser's buffer size would mean reporting a
  name that is not in the document, which is the trade this project refuses everywhere else.
- **Outer-HTML serialization** (raw source vs reflow — above; the re-parse direction is
  [➕](#values-libxml2-loses)).
- **A FORM FEED between class names** ([➕](#values-libxml2-loses)) — one cell wide, and the only cell:
  everything else about the split agrees. The set is HTML's ASCII whitespace and nothing wider, which is
  what a page like `class="ctsListWrap fadein　clearfix"` depends on — the separator there is an
  IDEOGRAPHIC SPACE (U+3000), so those are two class names and not three. `[attr~=v]` tokenizes
  identically.
- **Frameset documents.** `<frameset>`/`<frame>`/`<noframes>` sit directly under `<html>` and such a
  document has no `<body>` at all — matched, but a rare enough shape that it is called out rather than
  assumed. The rest of the frame is synthesized (see the document-frame entry in the supported list).
- **`:has()` with an id/attribute/`:not` inner** and **`:is()`/`:where()` combined with other conditions**
  ([➕](#selectors-the-oracle-rejects)) — valid CSS whose *correct* semantics Frostwork implements
  rather than capping itself at the oracle. `:is()` is the clearest case for that policy: cssselect
  **≤ 1.4.0** over-matches it (`xpath_matching` folds each alternative into the base condition with
  `add_condition(..., "or")`) and **≥ 1.5.0** agrees with Frostwork exactly. The test oracles against the
  equivalent comma expansion, which both versions translate correctly, so it is valid on either side of
  that line and also asserts the direct evaluation agrees — an upstream regression reopens the divergence
  loudly rather than passing silently.
- **Content after `</html>` is KEPT** ([➕](#values-libxml2-loses)) — **only partly browser-equivalent,
  and the second port hazard.** libxml2 stops building the tree at `</html>` and silently discards
  everything after it, so `<html><body>…</body></html><div>late</div>` gives lxml/Parsel an empty column
  for `div::text` while Frostwork returns `late`. Keeping it is deliberate: trailing markup after
  `</html>` is common in the wild (injected analytics, chat widgets, CDN/proxy-appended snippets), and
  silently dropping real content is the worse failure for a scraper — same reasoning as `:is()`/`:has()`
  above and the browser-aligned encoding choices below.

  **This is not a marginal divergence**, as a 1000-page Common Crawl sample shows. The tag is not always
  a trailing stray: it is also written in the *wrong place*. One sampled page ends its
  head `</head></html><body><header>…` and puts 14 of its 17 KB after the `</html>`; another (a large
  retail page) carries a second `</html>` at the halfway mark. libxml2 keeps 2 elements of 100+ on the
  first and 40 `<a>` of 119 on the second, so a Parsel spider sees a near-empty page where a browser
  sees the site. 28 of that sample's divergences are this one rule, and every one of them is Frostwork
  recovering a page rather than losing one — re-running the engine on each page *truncated at* its
  first `</html>` makes all 28 agree, which is how they were attributed.

  Be precise about the three-way split, because "matches browsers" is only half true. Per the HTML
  Standard's *after after body* insertion mode a non-whitespace token there is a parse error that gets
  reprocessed "in body", so a browser both keeps the content **and re-parents it into `<body>`**
  (confirmed against Chrome `--dump-dom`: the trailing elements come back as `<body>` children).
  Frostwork keeps the content but does **not** re-parent it: the frame it synthesizes is built from the
  byte stream going forwards, and re-parenting means moving elements that are already closed. So they
  stay where the byte stream put them, after `<body>` closed. Therefore:

  | selector shape | lxml/Parsel | Frostwork | browser |
  |---|---|---|---|
  | unscoped (`div::text`, `#after::text`) | ∅ empty | ✅ finds it | ✅ finds it |
  | ancestor-scoped (`body div::text`, `body ::text`) | ∅ empty | ∅ empty | ✅ finds it |
  | `html`-scoped (`//html/script`, `html > div`) | ✅ finds it | ✅ finds it | ✅ finds it |

  So an unscoped selector is browser-equivalent, while a `body`-scoped one agrees with lxml instead.

  The last row is why the tail is not left bare: libxml2 does not simply discard it, it starts a
  **second root `<html>`** and puts the tail in that (the same shape it builds for a second `<html>`
  START tag — see the frame entry above). Frostwork synthesizes the same second root, so an
  `html`-scoped selector agrees. Leaving the tail parentless instead made `//html/script` miss a real
  page's trailing script that `//script` found — the values were all present, only the frame was not.

  **Migration caveat.** This is an *extra*-value divergence, the one direction that can surprise a
  port: a `Many` field whose selector also matches trailing injected markup gains rows it did not have
  under Parsel. If a spider depends on the oracle's truncation, constrain the selector to the real
  container (`main .card`, not `.card`) rather than relying on the parser to drop the tail.

## Encoding

This is the detail behind the encoding rows of [Beyond lxml](#encoding-beyond-w3lib-and-parsel):
the whole subsystem is a place where Frostwork answers and the oracles do not.

### The rule: never diverge from the browser

**On encoding, Frostwork targets what a browser actually renders — not w3lib, not lxml, not Parsel,
and not a reading of a spec.** This is the one subsystem where the usual oracle does not decide the
answer. The reason is what a scraper is *for*: a price, a title or an address is correct when it
matches what the site shows a person, so a value that differs from the browser is wrong even when
every Python library agrees with it. There is no divergence budget here and no "acceptable local
difference" — those exist for tree construction (foster parenting, the adoption agency), not for
this.

What that means in practice, for anyone porting a scraper *to* Frostwork: **where Frostwork's text
differs from Scrapy's, on a page whose encoding is at all unusual, Frostwork is very likely the one
matching the browser** — the differences are enumerated in the table below, each with the reason and
each asserted in `tools/enc_check.py` in *both* directions, so a row cannot rot into silent
agreement if an upstream library is fixed.

**What the rule covers.** It governs *which bytes become which characters*: BOM handling, the
`<meta>`/XML-declaration prescan, label resolution, and the decoder indexes. It does **not** reach
past that into what happens to the characters afterwards — Parsel's input normalization
(`text.strip()`, NUL deletion) and libxml2's tree construction (foster parenting, the adoption
agency) are separate contracts with their own oracle, documented above, and they keep it.

That boundary is load-bearing in exactly one place, so it is worth naming: a **UTF-32LE** document
(`FF FE 00 00`). The *encoding* decision matches the browser already — those first two bytes are the
UTF-16LE BOM, there is no UTF-32 in the Encoding Standard, and Frostwork reads UTF-16LE just as
Chrome does. What differs afterwards is NUL: Chrome's tokenizer turns each one into U+FFFD, so no
tag ever opens and the document renders as literal text, while Frostwork deletes NUL document-wide
because Parsel does and parses the tags. That is the normalization contract behaving as specified,
not an encoding divergence, and it is left as it is — a scraper's `response.text` really has been
NUL-stripped before it reaches any selector.

The rule has teeth: applying it moved the `<meta charset>` prescan off w3lib's 4096-byte window and
onto the browser's actual boundary, which turned out to be **two** rules and neither of them a flat
window — see the depth rows below.

Encoding *sniffing* is still oracled against `w3lib.encoding.html_to_unicode` (what Scrapy uses) for
everything the two agree on, because Parsel itself never looks at `<meta>` — `parsel.Selector(body=…)`
just defaults to UTF-8, so oracling a prescan against it is vacuous. But w3lib is **evidence, not the
target**. Claims about "what browsers do" are settled by measuring a browser, not by quoting the
standard at it: `~/src/encoding-hacks` runs exact bytes and exact HTTP headers past a real browser
and twelve scraper stacks side by side, and that is where a row in the table below comes from.

Resolution order (`src/encoding.rs`):

1. **BOM** — UTF-8, UTF-16LE, UTF-16BE.
2. **BOM-less UTF-16 detected from an XML declaration prefix** — `3C 00 3F 00` / `00 3C 00 3F`, i.e. `<?`
   encoded as UTF-16 (XML 1.0 Appendix F, and what libxml2's own `xmlDetectEncoding` does). Grouped with
   the BOM checks on the same reasoning WHATWG gives a BOM priority: the bytes are unambiguous, and no
   ASCII-compatible document can begin with a NUL.
3. **caller / HTTP charset label**.
4. **prescan, bounded the way a browser bounds it** — an XML declaration's `encoding=` (only at offset
   0) and `<meta charset>` / `<meta http-equiv=content-type>`, in document order. The bound is **the
   first 1024 bytes unconditionally, and past that only while still inside `<head>`**. It is two rules
   because a browser has two, and both were measured rather than read off the standard:
   - **In the `<head>`, at any depth** — honoured in Chrome at 1KB, 4KB, 16KB, 64KB, 256KB and 1MB.
     WHATWG's 1024 is a *streaming* budget ("not to stall beyond that", i.e. do not delay first paint
     waiting for bytes off the network), and a browser that meets the `<meta>` after it runs "change
     the encoding" and re-decodes. A flat window costs real pages: legacy sites that open with a
     producer comment or a block of `og:` metas put their `Content-Type` at byte ~1100–1600, and the
     page then decoded as UTF-8 with a U+FFFD in every value.
   - **In the `<body>`, only within the first 1024 bytes** — honoured in Chrome at byte 0, 100 and 512
     and ignored from 1024 on. Once real content is parsed the browser will not re-decode it.

   w3lib's flat 4096 is wrong in both directions, and so was Frostwork's copy of it: it dropped the
   head declarations real pages carry, and honoured body declarations no browser honours. Bounding at
   the head is also what makes this free — an unbounded scan measured a flat ~35µs per label-less page,
   one extra pass over the document, and the head bound gives all of it back.
5. **UTF-8** default.

Structural tokenization runs on raw bytes for every ASCII-compatible encoding — a byte below 0x80 *is*
that ASCII character there, so the delimiters are unambiguous — and only the small emitted values are
decoded with the resolved encoding (`encoding_rs`). Everything else is transcoded to UTF-8 up front, and
which encodings those are is asked of `Encoding::is_ascii_compatible` rather than listed: UTF-16LE/BE,
**ISO-2022-JP**, and `replacement` (the label WHATWG gives HZ-GB-2312 and friends, which browsers refuse
to decode at all — one U+FFFD for the whole document). ISO-2022-JP is the one that bites: inside its
`ESC $ B` mode a JIS pair is two bytes below 0x80, so `社` is the pair `<R`, and a crawled Japanese page
tokenized as raw bytes grew an `<r>` start tag out of the middle of a word.

### Extended characters in a SELECTOR

A selector's literals are UTF-8 and a byte-scanned page's are not, so **nothing is ever compared as raw
bytes across that boundary**: attribute values are decoded with the resolved encoding at element open and
text with it before the predicate sees it, which is what makes `.café`, `[title="café"]`, `#año` and
`:contains("社")` answer identically for the same document in UTF-8, windows-1252, shift_jis or UTF-16.
The same holds for CSS escapes in a quoted value — `[data-x="caf\e9"]` is the character, decoded the way
cssselect decodes it — and an escape in an *identifier* stays unsupported (see the CSS table).

Attribute **names** are decoded on the same rule, and are the one place where they were not: a name is
raw page bytes while a selector's is UTF-8, so `[data-año]` (and `::attr(año)`) matched a UTF-8 page and
silently returned nothing for the same document in windows-1252, where lxml — which decodes the whole
document before tokenizing — matches. A name a schema spells in ASCII still never needs the decode,
because `is_ascii_compatible` (the same predicate that decides what gets transcoded up front) is defined
as ASCII bytes mapping to ASCII characters *and vice versa*.

A non-ASCII TAG name is the remaining gap, and it is **reported rather than silently empty**. It is not
an encoding limit — see the CSS table for why accepting it would over-match rather than fix anything.

### Deliberate differences from w3lib

Every row is browser/WHATWG behaviour that w3lib does not implement, or the other way round. Each is
gated in `tools/enc_check.py`.

| behaviour | w3lib | Frostwork | why |
|---|---|---|---|
| a `<meta charset>` deep in the **head** | ignored past 4096 bytes | honoured at any depth (measured in Chrome to 1MB) | a browser meeting it after its prescan budget runs "change the encoding" and re-decodes; the budget is a *streaming* one |
| a `<meta charset>` deep in the **body** | ignored (its regex gives up at `body`) | ignored too, past the first 1024 bytes | measured: Chrome honours a body declaration at byte 0/100/512 and ignores it from 1024 on — once real content is parsed it will not re-decode. The two agree here, for different reasons |
| a `<meta charset>` after `<body>` | ignored (its regex has a `\|body` alternative and gives up there) | honoured | browsers do not stop at `<body>`, and real pages carry late declarations |
| `charset=` inside a `<!-- comment -->` | honoured (no comment handling) | ignored | WHATWG's prescan and every browser skip comments |
| an unsupported charset label | stops at the first regex hit, so a later valid declaration is lost | **continues** and takes the next valid one | WHATWG: an unsupported label is "failure, continue" |
| a stray quote in an unquoted charset value (`charset=big5"`) | stops at the quote and honours `big5` | treats the quote as part of the invalid label, then continues | an unquoted HTML attribute ends only at whitespace or `>`; html5lib and browsers agree |
| UTF-32 BOM | recognized | **not a BOM** | the WHATWG Encoding Standard has no UTF-32. A UTF-32LE document begins `FF FE 00 00`, whose first two bytes *are* the UTF-16LE BOM, so it is read as UTF-16LE — which, with NUL deletion, still yields the BMP text |
| `<meta charset=utf-16*>` | honoured; the whole document decodes as UTF-16 and Parsel then finds nothing | read as **UTF-8** | the prescan could only READ that declaration by treating the bytes as ASCII-compatible, so the declaration contradicts itself. A real UTF-16 document (BOM, prefix, or an HTTP label) is unaffected |
| `<meta charset=x-user-defined>` | label not resolved at all → falls back to the default | **windows-1252** | "get an encoding from a meta element", step 5. Taken literally the label maps every high byte into the private use area (`caf\xe9` → `caf\u{f7e9}`) |
| BOM-less UTF-16 | not detected | detected from the `<?` prefix (row 2 above) | libxml2 reads these files correctly, so this is parity with the *value* oracle as well as with browsers |
| `<?xml … encoding=…?>` **not** at offset 0 | honoured (regex search anywhere in the window) | ignored | a `<?` after the start of the document is a bogus comment to a browser and declares nothing |

An XML declaration **at** offset 0 *is* honoured, by both, including its precedence over a later
`<meta>` — that one is in the shared set, not this table.

- ✅ **Validated vs Parsel** given the same label — windows-1252, shift_jis, euc-jp, gbk, big5, koi8-r,
  utf-8 — in both text nodes and attribute values (`tools/enc_check.py`).
- ≈ **The DECODER is not Parsel's decoder, and on some bytes that shows.** Frostwork decodes with
  `encoding_rs` (the WHATWG Encoding Standard — what browsers use); Parsel decodes with Python's stdlib
  codecs, via w3lib, which also *translates* some labels (`big5`→`big5hkscs`, `gb2312`/`gbk`→`gb18030`,
  `shift_jis`→`cp932`). A WHATWG index is **total** — every byte maps to a character — while Python's
  codecs leave bytes undefined and yield U+FFFD. WHATWG is the lossless behaviour and the one a browser
  shows, so per the oracle-bug policy above Frostwork keeps it and the difference is enumerated:
  - **`windows-1252` / `iso-8859-1`: exactly 5 bytes** — `0x81 0x8D 0x8F 0x90 0x9D`, which WHATWG maps to
    the C1 controls U+0081…U+009D and Python's `cp1252` leaves undefined. Parsel returns U+FFFD there.
  - **The label translation is part of the contract, not a detail of the comparison.** `iso-8859-1`
    (and `latin1`, `ISO_8859-1:1987`, …) **means windows-1252** — the WHATWG label table, what browsers
    do, and what Scrapy does, since `w3lib.encoding.resolve_encoding("iso-8859-1")` is `cp1252`. A *raw*
    `parsel.Selector(body=…, encoding="iso-8859-1")` bypasses w3lib and applies Python's literal latin-1
    codec, so the whole C1 range stays as control characters: a real page in the crawl sample serves
    `charset=iso-8859-1` and writes its en dashes as the single byte `0x96`, which is `–` here and in
    Scrapy and `U+0096` under raw Parsel. A harness that grades against raw Parsel is measuring its own
    oracle construction, not Frostwork.
  - **`big5`: exactly 11 two-byte sequences** where WHATWG's index resolves a
    duplicate pointer differently from `big5hkscs` — `A145` → U+2027 not U+2022, `A244`/`A246`/`A247` →
    the fullwidth ￥/￠/￡ not the halfwidth ¥/¢/£, and seven more.
  - **`euc-jp`: exactly 6** — the JIS-vs-CP932 round-trip family. `A1C1` is the wave dash:
    U+FF5E here (what CP932, WHATWG and every browser say) and U+301C in Python's `euc_jp`. The others
    are `A1C2` ∥, `A1DD` －, `A1F1` ￠, `A1F2` ￡, `A2CC` ￢ — fullwidth here, the JIS forms there.
  - **`gb18030`: exactly 20**, and here it is *Parsel* that loses the character: GB18030-2005
    moved these out of the private use area, WHATWG's index is the newer revision and Python's is the
    older one, so `A3A0` is U+3000 (ideographic space) here and U+E5E5 there.
  - **`euc-kr`: full parity** on every real character. **`shift_jis`: full parity** too; its only
    differences (762 sequences) are cp932's *user-defined area*, which the WHATWG index leaves
    unassigned — private-use code points there, U+FFFD here, and not text with a meaning.
  - **And a class where the engine has a character Parsel does not: 457 sequences in `euc-jp`, 192 in
    `big5`.** WHATWG's index assigns them and the Python codec does not, so Parsel returns U+FFFD —
    `AD A1` is the `①` of ordinary Japanese prose, which `euc_jp` (strict JIS X 0208, no NEC row 13) has
    no mapping for. Browsers use the WHATWG index, so this is an oracle limitation rather than a
    divergence to fix; it is counted rather than listed because it runs to hundreds of sequences.
  - Every label is swept over **every two-byte sequence in the legacy lead/trail space**, not over the
    sequences the Python codec calls assigned. That filter is shaped like the oracle's own limitations, so
    it hides exactly the class above; a sample is worse still, since one `A1C1` on one crawled EUC-JP wiki
    is enough to disprove a "full parity" drawn from 800 characters per label. `tools/decoder_sweep.py`
    holds the enumeration, the four disagreement classes and the expected counts; `tools/enc_check.py`
    gates every one of them in both directions, so neither a new divergence nor one that quietly
    disappears can pass.
  - Sequences **neither** index assigns differ only in how many U+FFFD come back (WHATWG replaces per
    maximal subpart, Python per byte). Counted, not listed: it is not text any real page contains.

  The ordinary parity vectors never touch these bytes, which is why nothing caught any of it until the
  decoders were compared sequence by sequence.
- ✅ **UTF-16** (LE/BE; BOM, label, or the BOM-less `<?` prefix) decodes correctly, matching the
  decode-first result Scrapy uses.
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

- **Differential gate** (`tools/diff_lxml.py`): **0 DIVERGE / 0 CRASH** for every (page ×
  selector/group) pair per seed — conformant CSS/XPath, comma groups, sibling combinators, universal
  `*` terminals, `<svg>`/`<math>`/`<template>` foreign content, and single-pass `Many`/`One`.
- **Differential fuzzing**: mutated malformed HTML (`tools/diff_fuzz.py`) and random selectors
  (`tools/sel_fuzz.py`) vs lxml. Known divergences are attributed to documented constructs; anything left
  is **NOVEL** and gated on a rate that ratchets down as causes are fixed (see [TESTING.md](TESTING.md)).
- **Coverage-guided fuzz** (`cargo-fuzz`, `fuzz/`): arbitrary bytes → no panic / hang / OOB.
- **Unit**: hand-written vectors incl. tokenizer conformance (`cargo test` prints the count).
- **Rule audit** (`tools/audit_tree_rules.py --gate`): every tree-construction rule cell against lxml,
  over the whole element universe — the start-close relation, the void set, the data modes, `<p>`-closing,
  end-tag scope (every open × closing pair) and the document frame. **0 disagreements** is the gate.
- **Generated-table check** (`tools/gen_tree_rules.py --check`): the Rust start-close/void/end-priority
  tables still equal what libxml2 says, over that same universe.

To audit a specific query, use `frostwork.check` or run it through `tools/diff_lxml.py`'s verdict
logic. In permissive Python mode, an unsupported query yields `[]`.
