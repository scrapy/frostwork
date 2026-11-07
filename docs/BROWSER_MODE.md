# Browser mode and browser-compatible encoding

Browser mode is Frostwork's default parse mode. It answers selectors against the tree HTML5 tree
construction builds, as Chrome 156 implements it, inside the same streaming matcher lxml mode uses, and
it is graded against Chrome. Lxml mode (`parser="lxml"`) stays for libxml2 and Parsel parity. Both modes
make the browser's encoding decision. This document is the design: what browser mode changes, how it fits
the engine, and what it is checked against. [COMPATIBILITY.md](COMPATIBILITY.md) is the contract and
[TESTING.md](TESTING.md) describes the gates.

Scope: the core engine, its gates and the API integrations build against. Adrian owns the Scrapy and
w3lib integration.

In short:

- **Encoding** follows the browser through every entry point, and matches all 48 recorded Chrome
  answers for encoding cases measured as exact bytes and headers. One of them varies between loads in
  Chrome itself, and its record is the most common answer from the deployed site (E3). Valid UTF-8 that
  declares nothing is windows-1252 when it came over http(s), as Chrome and Firefox read it (E2).
  Anything else undeclared goes to Firefox's detector, because Chrome's judges whatever network chunk
  first gives it a verdict (E3).
- **The parse modes** share the tokenizer, the matcher, encoding and value decoding, and differ only in
  the tree builder behind one interface (section 4).
- **The oracle is the browsers themselves**: a page load served with `Content-Security-Policy:
  script-src 'none'` builds the real tree without running page scripts, and `DOMParser` builds the same
  tree, at over 20,000 documents a second, for every document with neither `<noscript>` nor a declarative
  shadow root (section 5).
- **Five HTML5 rules change a decision a forward pass has already made**: the adoption agency with a
  furthest block, foster parenting's document order, a second `<html>` or `<body>` tag merging its
  attributes, a `<frameset>` replacing a body, and an `<option>` copied into a `<selectedcontent>` before
  it. Where one fires and changes an answer, or a page nests past Chrome's depth cap of 512, browser mode
  answers the page from a compact tree of the finished document (section 4, "The replay"), on 1.6% of
  30,031 crawl and real-web pages. So browser mode
  is exact on all five, and builds no tree on every other page.

## 1. Encoding

The encoding decision is the same in both parse modes, and the browser is its standard.
`tools/browser_oracle/lab_compare.py` grades the engine against 48 cases of exact bytes and headers
whose rendering was recorded in Chrome, and none differ, given the header or through
`Page.extract_response`. The fallback-dependent cases are Chrome 156 top-level loads; the other 32 are a
Chrome 148 iframe capture that Chrome 156 reproduces. One of them, `body-meta-512k`, depends on how the
bytes arrive, and its record is the deployed site's most common answer (E3).

### E1. The engine takes the transport label, not a decision

- `content_type=` (the raw header value) and `url=` are on `extract`, `extract_grouped`,
  `detect_encoding`, `Plan.extract*` and `Page.extract`. Rust parses the charset parameter and resolves it
  with `Encoding::for_label`, so `x-user-defined` and the `replacement` labels survive.
- One precedence table serves every entry point (`frostwork::resolve_encoding`):

  | rank | source | an unusable value |
  |---|---|---|
  | 1 | BOM: UTF-8, UTF-16LE, UTF-16BE | n/a |
  | 2 | BOM-less UTF-16, from the `<?` prefix | n/a |
  | 3 | `encoding=`, the caller's own label | unknown to every codec: raise. A real encoding WHATWG lacks (`utf-7`): ignored, continue |
  | 4 | `content_type=`, its charset parameter | ignored, continue. It is server data, so it never raises |
  | 5 | document declarations: an XML declaration at offset 0, then `<meta>` in document order, in the head at any depth or within the first 1024 bytes of the body | unsupported labels are skipped, and the scan continues |
  | 6 | autodetection: valid UTF-8 is windows-1252 with an http(s) `url=`, else UTF-8 (E2); anything else is chardetng's guess with the TLD of `url=` (E3) | n/a |

  `encoding=` ranks below a BOM, because a BOM-marked document read as anything else is mojibake. Scrapy
  and web-poet rank a forced `_encoding` first, so a forced label on a BOM-marked body is the one shape
  where Frostwork and the framework can disagree.
- `str` input has already been decoded, so no rank applies: `encoding=` must be absent or a UTF-8 label,
  and `content_type=`'s charset is ignored.
- Detection and extraction make the same decision. The label check sits below every entry point, and
  `tests/test_encoding_inputs.py` runs every `enc_check` document through each of them.
  `detect_encoding(..., explain=True)` also returns the winning source, so a framework can set its own
  `.encoding` from the same decision.
- `Page.extract_response` and `FrostPage` pass the header and the URL, and a forced `_encoding` as
  `encoding=`. They never pass web-poet's resolved `.encoding`: it is always a string, it outranks the
  prescan, and passing it loses 13 of the 48 cases. PYTHON.md ("Encoding inputs") is the integrator
  contract.
- `diff_webpoet` grades against parsel built from `response.text`, which is w3lib's decision, so its
  generator stays out of the shapes where the two decisions differ.

### E2. Valid UTF-8 from an http(s) URL is windows-1252 (decided 2026-11-07)

Both browsers refuse to detect UTF-8 for a page from the network and keep the locale's default encoding,
windows-1252 in English locales. Blink's `DetectTextEncoding` refuses its detector's UTF-8 verdict for any
URL that is not a local scheme, and chardetng documents the same policy for Firefox. Measured in Chrome
156 and Firefox 159: five unlabelled valid UTF-8 pages (Latin, Japanese, Russian, Chinese and emoji
text) and the lab's `no-declaration` case are windows-1252 over http, and the same bytes loaded as
`file:` URLs are UTF-8. Frostwork used to read every valid UTF-8 page as UTF-8, and that was the lab's
`no-declaration` mismatch.

So in rank 6 a valid UTF-8 document (a sequence cut off at the very end still counts) is windows-1252,
reported as source `"default"`, when `url=` is `http` or `https`, and UTF-8 otherwise. A page with no URL
is most often one stored on disk, so Frostwork reads it as both browsers read a file. That, and UTF-8 for
schemes other than `http`, `https` and `file`, which the browsers would refuse, are Frostwork's choices.
Frostwork fixes the English default rather than reading a locale. There is no switch: a framework that
wants UTF-8 for these pages, as scraping stacks have traditionally read them, overrides the answer in its
own code. w3lib passes the response URL, so Scrapy's `response.text` follows the browsers unless w3lib
does that. `tools/enc_check.py` asserts the http and `file:` answers against both browsers'
measurements.

### E3. Autodetection follows Firefox's detector (decided 2026-11-03, measured again 2026-11-07)

Autodetection of a document that is not valid UTF-8 uses chardetng, Firefox's detector, with the
response URL's TLD as the hint (Gecko calls it from `nsHtml5StreamParser::GuessEncoding`). Chrome uses
Compact Encoding Detection (CED), and Blink's `TextResourceDecoder` calls it on each network chunk as it
arrives until one gives a verdict, so it judges whatever arrived first. Measured in Chrome 156 with 1.1 KB
of windows-1251 text placed in a 600 KB ASCII page: from the lab's server, which writes the body in one
call, Chrome detects windows-1251 when the text starts at byte 262,000 and reads windows-1252 when it
starts at 263,000. With the server writing 8 KB every 20 ms, it detects the text at byte 7,000 and not at
9,000.

The lab's `body-meta-512k` case shows the same boundary. From the lab's server Chrome reads it as
windows-874 once the page holds at least 262,944 bytes. From the deployed mojibake.wtf it read
windows-1252, Frostwork's answer, on 42 of 46 loads and windows-874 on 4, so Chrome itself has no single
answer for these bytes. Firefox 159 reads it as windows-1252 from the lab's server too. Binding CED would
not reproduce Chrome, because the answer depends on delivery, which the bytes do not record. chardetng
over the whole document is deterministic, is what Firefox shows, and costs nothing: encoding_rs and
chardetng are Firefox's own libraries. The URL is
the only other input to this step, so integrations pass it wherever one exists.

### E4. NUL and the strip are parse-mode behaviour

Lxml mode keeps Parsel's `text.strip().replace("\x00", "")`. Browser mode does neither (section 3).

## 2. The parse mode

- Rust: `ParseMode::Browser` (default) and `ParseMode::Lxml`, fixed when a `Plan` is compiled, because the
  rules are monomorphised into the matcher. One schema runs in one mode.
- Python: `extract(..., parser="browser" | "lxml")`, `Page(parser=...)`, and a `parser=` class keyword on
  `FrostPage`, carried on the class so that `@attrs.define`, which recreates the class, keeps it.
- `frostwork.check` reports support per mode, because it can differ (a `:has()` over template content).

Both modes share the tokenizer's states. On the shapes probed (the attribute-value entity exception,
semicolon-less legacy names, numeric C1 remapping, duplicate attributes, `--!>`, the script escape
states, `<` in an unquoted value) Frostwork, lxml and html5lib agree. What differs is who picks the state
after a start tag: the tag name in lxml mode, the tree builder in browser mode, because it depends on
context. An SVG `<title>` holds markup rather than RCDATA, and `<![CDATA[...]]>` is text inside
`<svg>`/`<math>` where libxml2 reads a bogus comment.

Encoding resolution, value decoding, what `::text` and `::attr()` return for a matched node, XPath
semantics and raw-source outer HTML are shared. CSS matching is not: browser mode takes the browser's CSS
semantics, which fold the case of some attribute values (section 3).

## 3. What browser mode changes

Every row is graded against Chrome by `make gate-browser`. "Streams" means the browser's answer is known
when the token arrives, so the streaming matcher produces it; "replay" means the rule changes a decision
already made, and the page is answered from its finished tree (section 4). `claims.json` in
`tools/browser_oracle/` holds the fixtures behind the rows measured in both Chrome 156 and Firefox 159.

| construct | example | lxml mode | browser mode | how browser mode answers it |
|---|---|---|---|---|
| NUL | `<di\0v>`; NUL in text | deleted everywhere, so `<di\0v>` is a `div` | U+FFFD in names and most text, dropped in body text | input rules |
| leading strip | indented BOM, leading VT | stripped (Parsel) | kept; a non-whitespace character opens `<body>` | input rules |
| `<noscript>` | `<noscript><p id=t>x` | markup | raw text, scripting on | the builder picks the data mode |
| HTML `<template>` | `<template id=t><p id=y>x` | `#y` matched | `#t` matches, `#y` never does: the content is an inert fragment. An SVG `<template>` is an ordinary element whose children stay selectable | inert content, keyed on namespace and name |
| SVG `<title>` | `<svg><title><b id=x>t</b>` | RCDATA by name, so no `<b>` | an HTML integration point: `#x` is a real element | the builder sets the tokenizer state |
| implied `<tbody>` | `table > tbody > tr > td` | empty; `table > tr > td` matches | the reverse, so DevTools' "Copy selector" works | synthetic element |
| `/>` on a non-void tag | `<div class="a"/>text` | div closed, `div.a::text` empty | `/>` ignored, text inside the div | streams |
| `<p>` closers | `<p>a<section>b</section>c` | `p::text` is `a`, `c` | `a`: HTML5's closer set includes `section`, `article`, `nav` | streams |
| deep `<p>` | `<p><b><div>x` | `div` inside `p` | `p` closed (button-scope walk) | streams |
| stray `</p>` | `<div>A</p>B</div>` | one node `AB` | empty `<p>` inserted, so `A` and `B` | synthetic element |
| formatting reconstruction | `<p><b>1</p>2` | `b::text` is `1` | `1`, `2`: after `</p>`, a copy of `<b>` reopens for the `2` | a synthetic copy with the original's attributes |
| quirks-mode tree | `<p id=p><table id=t>` with no doctype | `<table>` closes the `<p>` | the table stays inside the `<p>`; with a doctype it closes it | the document mode, read from the doctype |
| leading newline | `<pre>\nx`, also `<textarea>`, `<listing>` | `\nx` | `x` | streams |
| form pointer | `<form id=a><form id=b><input>`; `<table><form id=f><tr><td><input id=i>` | the second form nests; the input is inside `#f` | the second form is ignored; in a table the form is an empty child of the table, so `#f #i` is empty | streams; a form that leaves the stack stays a DOM ancestor |
| declarative shadow root | `<div><template shadowrootmode=open><p id=s>` | the `<template>` and `#s` match | the content becomes a shadow root, so neither matches a document selector | inert, with the host conditions |
| frameset replacing the body | `<div id=x></div><frameset>` | `#x` stays | the body and `#x` are removed and the `<frameset>` replaces them; text first makes the `<frameset>` ignored | replay |
| `dt`/`dd` repeat | `<dl><dt>a<dt>b` | nested | siblings | streams |
| table parts outside a table | `<td>x`, `<tr><input>` | the element is built | the start tag is ignored and its content joins the current node | streams |
| `<frame>` outside a frameset | `<div><frame>` | the element is built | ignored | streams |
| foreign-content breakout | `<svg><pre>`, `<math><p>` | nested inside the `<svg>` | an HTML start tag such as `<p>` closes the foreign elements first | streams |
| closes HTML5 does not do | `<pre><dd>`, `<dt><dl>` | libxml2 closes the first element | both stay open, so the second nests inside the first | streams |
| head content after the head | `<style>s</style><button>` and similar | stays where written | head-only elements arriving in "after head" go back into the head | the head is parked, then reopened |
| `<select>` rules | a `<select>` inside a `<select>` | nested | the second closes the first | streams |
| nested `button`/`h1`/`nobr` | `<button>x<button>y` | nested | the outer one closes | streams |
| `<head>` | `<p>x` | no head | an empty head is always built | synthetic element |
| after `</html>` | `…</html><div id=a>late` | outside `body` | inside `body`: HTML5 never pops `body` or `html` before EOF | streams |
| renames | `</br>`, `<image>` | ignored, unknown element | `<br>`, `<img>` | streams |
| CDATA in foreign content | `<svg><text><![CDATA[b]]>` | bogus comment | text | tokenizer hook |
| foster parenting | `<table><p id=t>moved` | `table #t` matches | `<p>` moved before the table | ancestry streams (the table is parked); document order by replay |
| adoption agency | `<b>1<p>2</b>3`; `<a><div>t</a>u</div>` | `b::text` is `1`; `a::text` empty | `<p>` moves out of the `<b>`, so `b p` is empty and `b::text` is `1`, `2`; `t` inside a copied `<a>` | streams without a furthest block; replay with one |
| second `<html>`/`<body>` tag | `<body x=1>…<body y=2>` | ignored | attributes merged into the open element, the first tag to set one winning | replay, when the attributes change an answer |
| `<selectedcontent>` | `<select><button><selectedcontent></selectedcontent></button><option>A` | an empty element | the selected option's content is copied in when the option closes | replay |
| foreign attribute names | `<svg viewbox="0 0 1 1">`, `<math definitionurl=x>`, `xlink:href` | as written, lowercased | `viewBox`, `definitionURL`, and `xlink:`/`xml:`/`xmlns` namespaced on foreign elements | streams |
| depth cap | more than 512 nested elements | nested | a new element becomes a sibling of the current node | replay |

Tree rules that read an attribute value (`input type`, `template shadowrootmode`, `annotation-xml
encoding`, `select size`) read it with its character references decoded, and `size` is parsed as an HTML
non-negative integer into 32 bits, as Chrome does.

Four Chrome rules depart from the spec, and browser mode follows Chrome in each; Firefox follows the spec.

- A formatting element with an `is` attribute, whatever its value, is never put on the list of active
  formatting elements. It is not reconstructed (`<div><b is></div>x` leaves `x` in the body), and its end
  tag is any other end tag, so `<b is>1<p>2</b>3` ignores the `</b>`.

- Whitespace after `</body>` or `</html>` goes into the current node without reconstructing the active
  formatting elements (`<div><b></div></body>\n` leaves the LF in the body, not in a new `<b>`), and the
  rest of that text reconstructs them as usual.
- In body text, the one NUL right after a `<` read as text is U+FFFD, while every other NUL is dropped:
  `x<\0\0y` is `x<`, U+FFFD, `y`.
- A tag whose run of whitespace before the `>` has a CR as its second character swallows an LF right after
  the `>` (`<span \r>\nx` holds `x`). A CR elsewhere in the run does not (`<span\r>`, `<span  \r>`), and
  a CRLF counts as one character. The swallowed LF is not the one `<pre>` drops, so `<pre \r>\n\nx` is `x`.

`<select>` content is not a departure: Chrome 156 and Firefox 159 keep a `<div>` written inside a
`<select>` since the customisable-`<select>` parser change, and so does lxml, while html5lib and parse5
drop it.

Foster parenting needs both halves. A fostered element's parent is known when it opens (the table's own
parent), so its ancestry streams. Its place does not: in the browser's tree it comes before the table, so
`p + table`, the table's sibling positions and the order of a column are decided after the fact, by the
replay.

Parsing and selector semantics are separate in three places:

- **Quirks mode is two features.** The tree half is implemented: the builder computes the document mode
  from the doctype (WHATWG's quirks, limited-quirks and no-quirks tables). The selector half, where class
  and id selectors match case-insensitively in quirks mode, is deferred (decision 2), and the browser gate
  attributes it to its own value construct.
- **Attribute case folding is CSS-only.** HTML lists 46 attributes whose values CSS matches
  case-insensitively on HTML elements, so `input[type=submit]` matches `type="SUBMIT"` while XPath
  `//input[@type='submit']` does not. Chrome folds exactly that list, and not on SVG elements. Browser mode
  does the same from a compile-time table; lxml mode compares exactly.
- **Form owner is not ancestry.** Chrome's `DOMParser` builds the same tree as a page load for a form
  inside a table but does not set the input's form owner. Selectors see only the tree.

## 4. How it fits the engine

### One seam, two builders

`src/matcher/tree.rs` defines `TreeBuilder`. The tokenizer's events (start tag, end tag, text, comment,
doctype) go to the builder, which answers them by calling a few matcher primitives: `open_element` (also
for an element the page never wrote), `close_top`, `buffer_text`/`flush_text`, `absorb_into_text_gap`, and
`park`/`unpark`. The matcher evaluates selectors and the builder never does, so the modes share every line
of matching, capture, decoding and grouping. `CompiledSchema::run` picks the builder once per document and
`Scan<B>` is monomorphised over it, so nothing on the per-token path asks which mode it is in.

- `src/matcher/libxml2.rs` is lxml mode: libxml2's rules over the generated tables in
  `implied_close/generated.rs`.
- `src/matcher/browser/` is browser mode: `rules.rs` holds the insertion modes (WHATWG 13.2.6),
  `doctype.rs` the document-mode tables, `tags.rs` names and the SVG and MathML adjustments, `dom.rs` the
  replay's tree, and `mod.rs` the builder's state and how it drives the matcher. The HTML5 element
  categories are generated from Chrome by `tools/gen_html5_rules.py` into
  `implied_close/generated_html5.rs`; the algorithms that walk the stack are written from the spec and
  graded by sequence.

The builder chooses the tokenizer's next state. `start_tag` returns the data mode for the element's
content, because in HTML5 it depends on where the tag lands, and `/>` is honoured in foreign content and
ignored on a non-void HTML element. `cdata_is_text` answers whether `<![CDATA[` is character data at that
point, which it is only in foreign content.

### The parser stack is not the DOM path

The matcher reads ancestry, positions and sibling state off its own stack, so in browser mode that stack
is the DOM path to the current insertion point, and the HTML5 stack of open elements is the builder's own.
Under libxml2 the two coincide. Under HTML5 they differ in four ways:

- **Ghosts.** An element can leave the HTML5 stack while it is still a DOM ancestor of what is open. In
  `<form id=f><div id=d>a</form><p id=after>x</p>` the `</form>` removes the form but leaves the div open
  inside it, so `#f #after` matches. The form's matcher element stays on the matcher stack with no
  builder entry, and closes when the insertion point next moves below it.
- **Parked elements.** Foster parenting inserts in the table's parent while the table and its rows stay
  open. The matcher parks the table's run of elements, with their positions, group instances and pending
  text, and unparks them when the next insertion targets them. The head is parked in "after head". The
  current node past Chrome's depth cap of 512 is parked too, until the replay takes over (below).
- **Inert elements.** HTML template content and declarative shadow roots are parsed with the full rules
  but never reach the matcher. An SVG or MathML `<template>` is an ordinary element, so this keys on
  namespace and name together.
- **Pending elements.** The adoption agency's copies of a formatting element enter the HTML5 stack
  without a matcher element and get one only if something is inserted into them.

`OpenElem` keeps its size, and lxml mode runs none of this.

### The replay

The streaming pass runs until the first of the five retroactive rules fires, or the depth cap is
reached, and stops. The cap is not a rule of its own kind: past it a new element opens beside the current
node, which stays open, so later content of the current node comes first in document order, and the
replay is the exact answer there too. The page is then
answered by the replay: the same builder, compiled with `REPLAY` set, runs again from the start, and every
insertion becomes a DOM operation (append, insert before the table, move, copy, detach) on an arena tree in
`browser/dom.rs`. The matcher then walks the finished tree in pre-order, each node's rank serving as its
document position, so values come out in document order even where fostering moved them. A merge of
`<html>` or `<body>` attributes counts only when it changes an answer: a compound of the schema matches the
element differently with the attributes merged so far, or a terminal reads one of them. That makes it the
one rule whose firing depends on the schema.

Over 30,031 pages, three Common Crawl samples plus `fixtures/realweb` (`tools/retro_census.py`, each page
with its own selectors: production ones on realweb, ones generated from the page on the crawl), the rules
fire on these shares of pages:

| rule | pages |
|---|---|
| a second `<html>`/`<body>` changes an answer | 0.03% (2.42% with any attribute) |
| the adoption agency with a furthest block | 1.14% |
| foster parenting | 0.46% |
| a `<frameset>` replacing the body | 1 page |
| `<selectedcontent>` | none |
| any of them, so the page replays | 1.59% (none of the 31 realweb pages) |

The last row also counts pages that nest past the depth cap.

The tree holds attributes and text runs in two arenas and lives only while its page is answered. Building
and walking it costs little more than streaming; most of what a replayed page costs is the streamed prefix
the replay starts again from. `docs/BENCHMARKS.md` ("Browser mode against lxml mode") has the figures.

Replaying the tokens with hints about what is to come (a body inert from the start, a table's subtree
deferred past its fostered content) would need every event known before it starts, so it would run the
builder over the whole document anyway, with a mechanism per rule where the tree has one for all five.

Early exit (`first_only` columns) stays sound: the streaming pass stops early only where no later token
can reach back, meaning no open table, no formatting element holding an open special element, no body a
frameset could still remove, and no `<html` or `<body` bytes left in the page.

### The rest of the engine

- **Synthetic elements.** The frame (`html`, `head`, `body`), an implied `<tbody>`, `<tr>` or
  `<colgroup>`, and the `<p>` a stray `</p>` inserts are opened through the same primitive with no source
  of their own. A formatting element's copy carries the original's attributes, and its outer HTML is the
  start tag it was copied from followed by its own content.
- **Input preparation.** Browser mode skips Parsel's strip and NUL deletion, and consumes only a UTF-8
  BOM at offset 0. NUL follows HTML5: a NUL in a tag or attribute name stands for U+FFFD, as in Chrome, so
  `<di\0v>` does not match `div`; body text drops it; raw text, RCDATA and attribute values turn it into U+FFFD.
- **Names.** Attribute names follow Chrome's DOM: lowercased, SVG camel case, MathML `definitionURL`, and
  `xlink:`/`xml:`/`xmlns` namespaced on foreign elements. `::attr()` reads a name as `getAttribute()`
  does: lowercased on an HTML element, compared exactly on an SVG or MathML one. An XPath `@name` must be
  lowercase, so it reaches no camel-cased foreign name (section 7). CSS `[attr]` compares it
  case-insensitively and never matches a namespaced attribute.

### Deferred tails and node handoffs

In lxml mode two features rest on a libxml2 property HTML5 does not have: an element's raw source,
parsed on its own, builds the same subtree.

- **Deferred tails.** A deferred winner (`:has()`, a reverse position, a text predicate) whose value is
  inside it (`li:last-child b::text`, `div:has(a) ::text`) has its values read after the pass. Lxml mode
  re-scans each winner's raw span on its own (`Matcher::resolve_tail_spans`). An HTML5 span is not
  self-contained: `<tr><td>c</td></tr>` parsed alone has neither a row nor a cell, the `<b>` that
  reconstruction copies into the last `<li>` of `<p><b>x</p><ul><li>one<li>two</ul>` was opened before
  that `<li>`, and the form pointer, the adoption agency and foster parenting read state from outside it.

  So browser mode parses the whole document a second time with the same builder
  (`Matcher::resolve_tail_pass`). The builder's decisions depend on the input alone, so the second pass
  opens the same elements in the same order. The first pass records each winner by its place in that
  order (`OpenElem::elem`), which names an implied `<tbody>` or a formatting copy as well as an element
  the page wrote. The second pass marks each winner with a mark no page or selector can spell, and every
  tail of the schema is compiled into one schema rooted at that mark (`marked_tail`), so a tail column is
  an ordinary flat column of the second pass. The cost is one more pass over a document where a winner
  arose, however many tails and winners there are. A re-scan seeded with the builder's state would need a
  snapshot at every provisional winner and could not stop at the winner's end tag, and buffering candidate
  values would hold every provisional winner's subtree until it resolves.
- **`.as_node()`** reparses raw source through `lxml.html.document_fromstring`, so in browser mode a
  processor would receive libxml2's subtree for a node the browser built differently.

Browser mode therefore refuses two things lxml mode supports, each as `UnsupportedSelector` with a reason
`frostwork.check` reports per mode: an `.as_node()` handoff, and the outer HTML of an element the page
never wrote (the frame, an implied `<tbody>`, `<tr>` or `<colgroup>`, the `<p>` a stray `</p>` inserts).

## 5. The oracle

The closest oracle to a browser is the browser. Chrome 156 is driven over the DevTools Protocol and
Firefox 159 over WebDriver BiDi, each headless from a temporary profile, with a server in the harness
supplying exact bytes and headers. These entry points were compared against a page load over 5002 seeded
documents covering table parts, formatting tags, the frame tags, `<select>` and `<selectedcontent>`,
`<template>` with both shadow root modes, foreign content and its integration points, `<frameset>`,
self-closed tags, CDATA and processing instructions:

| entry point | scripting flag | page scripts | same tree as a page load | Chrome 156, docs/s | Firefox 159, docs/s |
|---|---|---|---|---|---|
| page load | on | run | no: scripts can change the tree | | |
| page load, `Content-Security-Policy: script-src 'none'` | on | blocked | yes, by construction | 98 (181 across 8 tabs) | 112 (221) |
| page load, CSP `sandbox` | off | blocked | no: `<noscript>` parses as markup | | |
| page load, CDP "disable JavaScript" | off | blocked | no: the same | | |
| `srcdoc` iframe inside a CSP page | on | blocked | Chrome 5002 of 5002; Firefox 7 of 5002 differ (quirks) | 520 | 596 |
| `DOMParser` | off | none | 4478 of 4478 eligible documents in both browsers; differs on 112 of the 524 with `shadowrootmode`, and on `<noscript>` | 20,477 | 25,467 |
| `Document.parseHTMLUnsafe` | off | none | Firefox 5002 of 5002; Chrome differs on 333, dropping comment nodes | | |

Rates are for one browser process and documents of about 210 bytes, serialising the whole tree in the
page. `srcdoc` is out because Firefox never puts a `srcdoc` document in quirks mode, and `parseHTMLUnsafe`
because Chrome's drops comments, which merges text nodes. So the oracle has two paths in each browser:

- **Fast path: `DOMParser`,** for a document that contains neither `noscript` nor `shadowrootmode` in any
  letter case. A false hit (the word in a comment) only sends a document down the slow path.
- **Slow path: a CSP-served page load,** for everything else and wherever the bytes matter: encoding
  cases, the lab, corpus pages. A tab fetches only the document it was sent. A later navigation (a meta
  refresh, a frame) is answered 204, which a browser does not commit, and every subresource is blocked,
  so a page can neither replace the tree being graded nor reach the network. A header CSP also makes
  Chrome hide every `nonce` attribute, so the probe restores each from the element's nonce slot before
  reading the page, as `DOMParser`'s documents, which have no CSP, already show it.
- **A standing self-check.** Every run re-parses a sample through both paths and fails if an eligible
  document's trees differ. When a browser adds a parser feature, its tokens join the generator's alphabet,
  and the eligibility rule grows only from what that measurement shows.
- **Pinned builds.** Hosted CI installs the Chrome for Testing build the rule tables were derived from.
  Every piece of evidence records its browser builds and harness commit.

### Evaluating selectors without losing input

lxml refuses characters and names browsers keep: U+000B, U+0001 and U+FFFE in text and attribute values,
tag names such as `p<img` and `a:b`, attribute names such as `1a`, comments containing `--`. These are
real malformed-HTML cases, so the oracle evaluates each kind of query where it can do so losslessly:

- **Structure** is compared in the browser's own tree, serialised losslessly to JSON.
- **CSS** runs natively: `querySelectorAll` finds the matches and the harness extracts `::text`,
  `::attr()` and outer HTML from them, so browser-mode CSS semantics, case folding included, come from the
  browser. Frostwork's extensions (`:contains()`, the pseudo-elements) are applied in-page to the native
  matches.
- **XPath** goes through a lossless bridge into lxml, because the browser's own XPath is the wrong
  semantics for a scraper: `document.evaluate("//svg")` finds nothing in an HTML document, and `//P`
  matches a `<p>`. Browser mode changes the tree XPath runs over, not the XPath language. Characters and
  names lxml refuses map reversibly into placeholders, and a round-trip gate requires every rebuilt tree to
  equal the browser's.

`tools/AGENTS.md` describes the driver (`probe.cjs`), the client, the CSS planner and the bridge.
`evidence/2026-11-03/` holds the measurement outputs, each stamped with its browser build.

### Which browser

The browsers disagree in places. On WPT's `html/syntax/parsing` (wpt.fyi stable runs, 2026-11-03) Chrome
156 passes 8121 of 8166, Firefox 159 7859 of 8164 and Safari 27.2 7832 of 8158, most of the gap being
processing instructions, which only Chrome has shipped. Where the two disagree, Frostwork follows Chrome,
because most users see Chrome (decided 2026-11-03). `BROWSERS=chrome,firefox` adds Firefox to the gate and
reports the columns where they disagree in a table of their own; the last run held `</br>` and
frameset-ok, `search` and `dialog` being special in Firefox, `</p>` inside a MathML integration point, and
processing instructions, which Firefox parses as comments.

Encoding and tree construction pin to different browsers for different reasons: autodetection of
legacy bytes follows Firefox because Chrome's detector judges the first network chunk, which complete
bytes do not record, and tree construction follows Chrome because both browsers can be run as oracles,
so the larger audience wins.

Two alternatives to running browsers were rejected. Chrome's parser cannot be lifted out (Blink's
`HTMLConstructionSite` depends on Oilpan and WTF). Firefox's can: Gecko's C++ parser is generated from
the validator.nu Java parser, so a JVM build with our own `TreeBuilder` subclass would be Firefox's tree
construction pinned by commit, if running browsers in CI becomes a burden. It would have to match three
behaviours that live outside the Java (scripting on, template content as a fragment, customisable
`<select>`), and it would still need real Firefox to check it. Library oracles drift: parse5 8.1.0 has not
implemented the customisable-`<select>` change all three browsers ship, and html5lib's last release was in
2020. A browser oracle cannot drift from the browser.

### Gates

`make gate-browser` and `make gate-webpoet-browser` are part of `make ci` and run in hosted CI.
`make gate-corpus-browser` is a report-only view over a page corpus, and `tools/retro_census.py` counts how
often each retroactive rule fires. [TESTING.md](TESTING.md#browser-mode-gates) describes what each grades
and when it fails. The lxml-mode gates all pin `parser="lxml"`.

## 6. Cost

- **Engine.** Browser mode is one more tree builder behind the seam, plus the replay. Lxml mode keeps its
  own code path and `OpenElem` its size. Browser mode costs more than lxml mode in the scan, which shared
  matching dilutes as selectors are added; the replay costs more again on the pages it runs on, and a
  deferred tail costs one more pass where it has a winner. `docs/BENCHMARKS.md` ("Browser mode against
  lxml mode") has the figures.
- **Gates.** A second oracle for the tree, values and the page-object layer, which needs Chrome and Node.
- **Contract.** COMPATIBILITY.md has a section per mode, each with its own divergence list.

## 7. Known gaps

- Quirks-mode class and id matching, where CSS compares case-insensitively without a doctype (decision 2).
- Outer HTML is the element's raw source, as in lxml mode, and is refused for an element the page never
  wrote. A formatting element's copy has no source of its own either: its outer HTML is the start tag it
  was copied from followed by its own content. Attributes a later `<html>` or `<body>` tag merged in, and the content a `<select>` copies into a
  `<selectedcontent>`, are not in that source.
- `.as_node()` is refused. A faithful one would hand over a node built from browser mode's own tree,
  never an lxml reparse.
- XPath names must be lowercase in both modes, and browser mode keeps SVG and MathML names in camel case,
  so XPath cannot reach a camel-cased foreign name: `@viewBox` and `//clipPath` are refused, and
  `@viewbox` and `//clippath` match nothing. CSS reaches them (`svg::attr(viewBox)`, `clipPath`).
- Not built: browser-mode verdicts in `diff_lxml`, `gate-seq` and `mutate_rules`, browser columns in the
  Common Crawl corpus checks, a Firefox column in the encoding measurements, and gating the Chrome-versus-Firefox table in both
  directions.

## 8. Decisions

1. The oracle is the browsers themselves: the `DOMParser` fast path, CSP-served page loads, Chrome and
   Firefox, with Firefox's Java parser held in reserve.
2. Browser-mode CSS folds HTML's case-insensitive attribute list. Quirks-mode class and id matching is
   deferred; quirks-mode tree construction is not.
3. The public names are `parser="browser" | "lxml"`, with browser builds recorded in the evidence rather
   than in the name.
4. Browser mode refuses synthetic outer HTML and `.as_node()`, and reads a deferred winner's subtree with
   a second whole-document pass.
5. Browser-mode XPath has lxml's XPath semantics over the browser's tree, so `//svg` still finds SVG.
6. `encoding=` stays below the BOM, with no BOM-beating override.
7. Valid UTF-8 from an http(s) URL is windows-1252, as in both browsers (E2), and autodetection of
   anything else follows Firefox (E3).
8. Browser mode is the default, and where Chrome and Firefox build different trees it follows Chrome.
9. The replay builds a tree of the page where a retroactive rule changes an answer, rather than leaving
   those rules as documented divergences, because it is needed for correctness.

Decisions 1 to 8 were taken on 2026-11-03 and decision 9 on 2026-11-04. Decision 7 was revised on
2026-11-07: until then valid UTF-8 stayed UTF-8 for every URL.

## Sources and versions

Measured on 2026-11-03, on macOS:

- Chrome 156.0.8078.80 and Firefox 159.0, stock builds, headless, each from a fresh profile, over CDP and
  WebDriver BiDi.
- 48 encoding cases, 32 from a Chrome 148 iframe capture and 16 from Chrome 156 top-level loads, and
  web-poet 2.5.0 with w3lib from the repo's `.venv`.
- `tools/browser_oracle/`: `probe.cjs` and its fixtures for section 5, `claims.json` for section 3,
  `lab_compare.py` for section 1, and the raw outputs in `evidence/2026-11-03/`.

Taken from upstream sources and not re-measured here:

- WPT `html/syntax/parsing` pass counts: wpt.fyi stable runs, 2026-11-03.
- Firefox's parser source: Gecko's
  [`parser/html/javasrc/README.txt`](https://github.com/mozilla-firefox/firefox/blob/390cc2e14e/parser/html/javasrc/README.txt)
  and [`parser/html/java/Makefile`](https://github.com/mozilla-firefox/firefox/blob/390cc2e14e/parser/html/java/Makefile),
  and validator.nu's
  [`TreeBuilder.java`](https://github.com/validator/htmlparser/blob/f5c4147718/src/nu/validator/htmlparser/impl/TreeBuilder.java),
  whose scripting flag defaults to off.
- Blink's parser dependencies:
  [`html_construction_site.h`](https://github.com/chromium/chromium/blob/05d7bd634b/third_party/blink/renderer/core/html/parser/html_construction_site.h).
  Chrome's encoding detector:
  [`text_encoding_detector.cc`](https://github.com/chromium/chromium/blob/05d7bd634b/third_party/blink/renderer/platform/text/text_encoding_detector.cc)
  and [google/compact_enc_det](https://github.com/google/compact_enc_det), and its per-chunk caller,
  [`text_resource_decoder.cc`](https://chromium.googlesource.com/chromium/src/+/refs/tags/156.0.8078.80/third_party/blink/renderer/core/html/parser/text_resource_decoder.cc)
  at the Chrome 156.0.8078.80 tag.
- The move of the tree-construction tests to WPT:
  [html5lib-tests 224991ec10](https://github.com/html5lib/html5lib-tests/commit/224991ec10) and
  [web-platform-tests/wpt#59976](https://github.com/web-platform-tests/wpt/pull/59976).
- Shipping versions for customisable `<select>` (Chrome 134, Firefox 153, Safari 27) and processing
  instructions (Chrome 150), and the parse5 8.1.0 release, from the projects' release records.
- Pinned automation builds: [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/);
  [Firefox's CDP retirement](https://fxdx.dev/cdp-retirement-in-firefox/).
- chardetng 1.0.0's `Utf8Detection` documentation (`src/lib.rs`), and Scrapy's
  [`TextResponse`](https://github.com/scrapy/scrapy/blob/master/scrapy/http/response/text.py).
