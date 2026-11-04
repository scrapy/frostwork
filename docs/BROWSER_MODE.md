# Browser mode and browser-compatible encoding

Status: implemented on the `browser-mode` branch. Browser mode is the default parse mode, and lxml mode
stays available as `parser="lxml"` for libxml2 parity. The design was drafted by Claude for Shane and
reviewed by Gemini (`gemini-4-argon`), Astra and Claude Fable before any of it was built; the
implementation was then reviewed by Claude Fable. Sources and versions are at the end.

Scope: the core engine, its gates, and the API that integrations build against. Adrian owns the Scrapy
and w3lib integration.

## Answers first

**Encoding.** Frostwork now makes the browser's encoding decision through every entry point. Over the
48 cases of `~/src/encoding-hacks`, the engine differs from Chrome on 2, both deliberate: unlabelled
valid UTF-8 stays UTF-8 (E2), and autodetection follows Firefox's detector rather than Chrome's (E3).
Before this work it differed on 9, and on 21 through web-poet, because the integrations handed the
engine w3lib's resolved `.encoding` and that label outranked Frostwork's own prescan. The engine now
takes the transport label (`content_type=`) and the URL, and `Page.extract_response` and `FrostPage`
pass them (E1). The two remaining "encoding" gaps the lab used to show (`utf32-bom`, `nul-split`) were
Parsel's input normalisation, and browser mode does not apply it.

**A parse-mode option.** Each compiled schema runs in one of two modes. Browser mode builds the tree
HTML5 tree construction builds, as Chrome 156 implements it, inside the same one-pass matcher; lxml mode
builds libxml2's. The two share the tokenizer, the matcher, encoding and value decoding, and differ only
in the tree builder behind one interface (section 4). HTML5's builder is stateful: it chooses the
tokenizer's next state, tracks namespaces, and keeps DOM ancestry apart from the parser stack, so the
interface is a stateful builder rather than the six rule functions lxml mode used to expose.

**The oracle is the browsers themselves.** In Chrome 156 and Firefox 159, a page load served with
`Content-Security-Policy: script-src 'none'` builds the real tree without running page scripts, and
`DOMParser` builds the same tree for every document with neither `<noscript>` nor a declarative shadow
root, at over 20,000 documents a second per browser process (section 5). Every browser-mode gate grades
against Chrome, and against Firefox on request.

**Five HTML5 rules change a decision a single forward pass has already made**: the adoption agency with
a furthest block, foster parenting's document order, a second `<html>` or `<body>` tag merging its
attributes into an element already matched, a `<frameset>` replacing a body already parsed, and an
`<option>`'s content copied into a `<selectedcontent>` that precedes it. None of them fired on 97% of the
30,031 crawl and real-web pages measured (`tools/retro_census.py`). When one fires, browser mode stops streaming and answers the page from a compact tree of the
finished document, built by the same builder (section 4, "The replay"). So browser mode is exact on
all five, and treeless on every page where none of them fires.

## 1. What the lab shows today

The lab's browser column holds two captures. The 16 cases whose answer depends on the fallback
encoding (`no-declaration` and the 15 declaration-depth cases) were captured by top-level page loads
in headless Chrome 156 (en-GB). The other 32 are the Chrome 148 iframe capture; Chrome 156's iframe
answers equal the stored ones on all 32, so mixing the builds moves no grade. The depth cases declare
ISO-8859-15 over the byte 0xA4, so honouring the declaration (`€`) and ignoring it (`¤` under
windows-1252) give different text.

Before this work, I ran the 48 cases through `frostwork.extract` (`main` at `68927e9`) with the raw
`Content-Type` charset, and 9 differed from the browser.

| cases | cause | verdict |
|---|---|---|
| `utf32-bom`, `nul-split` | NUL deletion (Parsel parity) | parse mode |
| `noscript`, `template`, `foster` | libxml2 tree construction | parse mode |
| `demo-price`, `demo-stock` | the `<template>` decoy, plus `<noscript>` | parse mode |
| `no-declaration` | unlabelled valid UTF-8: Chrome shows windows-1252 mojibake, Frostwork returns the text | deliberate (E2) |
| `body-meta-512k` | autodetection: Chrome guesses windows-874 (`1.299,00 ค`), chardetng windows-1252 (`1.299,00 ¤`) | deliberate (E3) |

On every other depth case Frostwork matches Chrome: a head declaration is honoured at any depth up to
1 MB, a body declaration up to byte 512, and none from body byte 1024 on.

So at the engine level there was no unexplained encoding divergence in the lab, only the two
deliberate ones and seven parse-mode cases. Browser mode fixes all seven. `tools/browser_oracle/lab_compare.py`
now reports 2 of 48 for the engine given the header and 2 of 48 through `Page.extract_response`, the
two being `no-declaration` and `body-meta-512k`. Passing web-poet's resolved `.encoding` as the label
still loses 14, which is why the integrations no longer do (E1).

## 2. Encoding changes

### E1. Give the engine the transport label, not the decision

Before this change, `Page.extract_response` and `FrostPage.frostwork_input` passed `response.encoding`.
In web-poet that property is `_encoding or BOM or Content-Type or w3lib's <meta> regex or an inferred
fallback`, so it is always a string, and it outranked Frostwork's prescan and autodetection, which
therefore never ran behind a web-poet or Scrapy response. Measured over the same 48 cases, before
browser mode:

| label passed to the engine | cases that differ from Chrome |
|---|---|
| the raw `Content-Type` charset | 9 |
| `w3lib.encoding.http_content_type_encoding(ctype)` | 10 (`x-user-defined` dropped: Python has no codec for it) |
| web-poet `HttpResponse.encoding` | 21 |

The 11 extra losses through web-poet are the browser rules w3lib does not have. Five are head
declarations past its 4 KB window (`late-meta-4k` to `-1m`). Three are body declarations within the
first 1 KB (`body-meta-0`, `-100`, `-512`), which w3lib's regex gives up on at `<body>`. All eight
come back as `1.299,00 ¤`. The other three are `stray-quote` (`cp1252`, mojibake), `meta-utf16`
(`utf-16`, an empty column) and `xmldecl-late` (`big5hkscs`, mojibake). Frostwork answers all eleven
correctly when it gets only the header.

Adrian owns the Scrapy and w3lib integration; this work owns the core and the contract he builds against,
so the core makes the right call easy and the wrong one hard.

**The core:**

- `content_type=` is on `extract`, `extract_grouped`, `detect_encoding`, `Plan.extract*` and
  `Page.extract`, beside `url=`. Rust parses the charset parameter and resolves it with
  `Encoding::for_label`, without the Python-codec detour, so `x-user-defined` and the `replacement`
  labels survive.
- One precedence table, the same for every entry point (`frostwork::resolve_encoding`):

  | rank | source | an unusable value |
  |---|---|---|
  | 1 | BOM: UTF-8, UTF-16LE, UTF-16BE | n/a |
  | 2 | BOM-less UTF-16, from the `<?` prefix | n/a |
  | 3 | `encoding=`, the caller's own label | unknown to every codec: raise. A real encoding WHATWG lacks (`utf-7`): ignored, continue |
  | 4 | `content_type=`, its charset parameter | ignored, continue. It is server data, so it never raises |
  | 5 | document declarations: an XML declaration at offset 0, then `<meta>` in document order, in the head at any depth or within the first 1024 bytes of the body | unsupported labels are skipped, and the scan continues |
  | 6 | autodetection: UTF-8 if valid (the E2 exception), else chardetng with the TLD of `url=` | n/a |

  `encoding=` ranks below the bytes that cannot lie: a BOM beats `encoding="windows-1252"`. That is a
  difference from Scrapy and web-poet, which rank `_encoding` ahead of the BOM. A label that outranks a
  BOM would be a new override contract, and a BOM-marked document read as anything else is mojibake, so
  there is none (decision 6).
- **`str` input has already been decoded**, so no rank applies. `encoding=` must be absent or a UTF-8
  label, and anything else raises. `content_type=` is accepted and its charset ignored, because transport
  metadata describes bytes that no longer exist.
- **Detection and extraction make the same decision.** The label check lives below every entry point, so
  `extract`, `detect_encoding`, `Plan.extract*` and `FrostPage` raise on the same unknown label and agree
  on every valid one. `tests/test_encoding_inputs.py` runs every `enc_check` document through each of
  them.
- `detect_encoding(body, content_type=..., url=..., encoding=...)` returns the WHATWG name the scan will
  use, so a framework can set its own `.encoding`, and with it `.text`, from the same decision.
  `explain=True` returns the winning source as well (`"bom"`, `"utf16-prefix"`, `"encoding"`,
  `"content-type"`, `"xml-declaration"`, `"meta"`, `"utf8"`, `"autodetect"`).
- PYTHON.md ("Encoding inputs") is the integrator contract: pass the raw `Content-Type` header value, the
  response URL, and `encoding=` only when the user forced one; never pass a resolved encoding.

**The integrations in this repo:**

- `Page.extract_response` and `FrostPage` pass the header and the URL, so chardetng's TLD hint reaches
  both, and pass a forced encoding from `_encoding` as `encoding=`, where it ranks below a BOM. web-poet's
  `HttpResponse(encoding=...)` and Scrapy's `TextResponse(encoding=...)` both store a forced label there
  and both rank it first, ahead of BOM, header and `<meta>`, so the one shape where Frostwork and the
  framework can disagree is a forced label on a BOM-marked body. Scrapy's order is
  `self._encoding or self._bom_encoding() or self._headers_encoding() or self._body_declared_encoding()`
  ([source](https://github.com/scrapy/scrapy/blob/master/scrapy/http/response/text.py)).
- `diff_webpoet` grades against parsel built from `response.text`, which is w3lib's decision, so its
  generator has to stay out of the shapes above.

None of this needs the shared-decision API (scrapy/w3lib#333, reverted).

### E2. Unlabelled valid UTF-8 (decided: keep the UTF-8 check)

Decided on 2026-11-03: Frostwork keeps reading unlabelled valid UTF-8 as UTF-8, as the encoding
subsystem's one named exception to the browser rule. The work left is documenting it and asserting it,
as in option (a) below.

`encoding::autodetect` returns UTF-8 whenever the whole document is valid UTF-8, and only then asks
chardetng. Browsers do not. chardetng's documentation says browsers must pass `Utf8Detection::Deny`
for `text/html` on non-`file:` URLs ("This way, Web developers cannot start depending on UTF-8
detection"), and with UTF-8 denied it returns the TLD-affiliated legacy encoding. Chrome 156 does the
same (section 1). Firefox's answer rests on that documented policy until the lab's Firefox column
measures it.

- **(a) Keep the UTF-8 check** and document it as the encoding subsystem's one named exception,
  asserted in `tools/enc_check.py` against the measured browser answer so it cannot drift silently.
- **(b) Follow the browser.** Drop the pre-check and every unlabelled UTF-8 page becomes mojibake.

Option (a) was the recommendation and is the decision. The browsers get there by policy, not by
reading the bytes differently, and the result is text nobody intended: a scraper that stores `cafÃ©`
has a wrong value by any standard. The exception cannot be turned against a scraper either. Every multi-byte UTF-8 sequence starts with a
byte from 0xC2 to 0xF4, which windows-1252 shows as `Â` to `ô` followed by junk, so a page that relied
on it would look broken to its own users.

The cost of (a) is one named exception in a subsystem whose rule says there are none. Making the
browser's answer available behind browser mode would be possible, but I would not: it adds a mode
difference to the one layer that is otherwise mode-free.

### E3. Pin autodetection to Firefox (decided)

Decided on 2026-11-03: autodetection follows Firefox, whose detector Frostwork already runs.

Autodetection only runs when there is no BOM, no label and no declaration, and the bytes are not valid
UTF-8. Frostwork uses chardetng, which is Firefox's detector. Chrome uses its own detector (Compact
Encoding Detection), and on `body-meta-512k` the two disagree: Chrome 156 guesses windows-874 and
Frostwork windows-1252, so the price comes back as `1.299,00 ค` in one and `1.299,00 ¤` in the other.
Chrome's guess is also unstable. On the same page with the earlier 0x80 payload, Chrome 156 guessed
windows-1252 and Chromium 152 guessed windows-1256, so it moves with the build and with a single byte.

Chrome's detector is not out of reach as code. [google/compact_enc_det](https://github.com/google/compact_enc_det)
is Apache-2.0 and builds with CMake. But it is archived (last commit 2024-02-12), and Chrome's
behaviour is CED plus Blink's own rules in `text_encoding_detector.cc` and `text_resource_decoder.cc`.
Those rules throw away a UTF-8 or ISO-2022-JP guess for non-local content, fall back to the locale or
TLD default, and, from Chromium 159 (not yet in stable), pass only the TLD as a hint. Matching
Chrome would mean binding an archived C++ library and re-implementing that wrapper. Matching Firefox
costs nothing, because encoding_rs and chardetng are the libraries Firefox itself uses.

What pinning to Firefox involves:

- Name Firefox as the reference for autodetection in COMPATIBILITY.md, and list Chrome's disagreement
  as measured rather than chasing it. Gecko calls chardetng from `nsHtml5StreamParser::GuessEncoding`
  with a TLD hint, which is the call `encoding::autodetect` should be checked against.
- Add a Firefox column to the lab. It is Chrome-only today, and the built-in browser is a second
  Chromium, not a second engine.
- Treat the response URL as a required input wherever one exists (E1). It is the only lever on this
  step, in the same way the UI locale is Chrome's.

### E4. Lab fixes (done in `~/src/encoding-hacks`, uncommitted)

The lab now measures the fallback correctly, which is what section 1's numbers rest on:

- The depth decoys declare ISO-8859-15 over 0xA4, an encoding nothing falls back to. `make
  discriminate` reads each depth case as served, with the `<meta>` deleted and as the declared label,
  and fails any case where the last two agree. It fails all 15 cases on the old windows-1252 design and
  passes all 15 on the new one, for Frostwork and for Chrome.
- Fallback-dependent cases are declared top-level, and `make capture-top` captures them in headless
  Chrome over CDP, one fresh tab and one top-level load per case. `make audit-nav` loads every case
  both ways and fails on an iframe-versus-top-level split that is not declared.
- A harness save merges into `results/browser.json` instead of replacing it, and fingerprints each
  cell with the bytes and `Content-Type` it measured. `make summary` refuses to grade a stale cell.

Still to do there: a Firefox column (E3), and `results/REPORT.md` and `results/CROSS-LANGUAGE.md`,
which still quote the old counts.

### E5. NUL and the strip are parse-mode behaviour

Lxml mode keeps Parsel's `text.strip().replace("\x00", "")`, as COMPATIBILITY.md already scopes it.
Browser mode drops it (see section 3). The encoding decision is the same in both modes.

## 3. The parse mode

### API

- Rust: `ParseMode::Browser` (default) and `ParseMode::Lxml`, fixed when a `Plan` is compiled,
  because the rules are monomorphised into the matcher. One schema runs in one mode.
- Python: `extract(..., parser="browser" | "lxml")`, `Page(parser=...)`, and a `parser=` class keyword
  on `FrostPage`. That keyword must be carried on the class, because `@attrs.define` recreates the
  class and drops class keywords, the same trap `strict=False` hit.
- `frostwork.check` reports per mode, because a selector's support can differ between modes (a
  `:has()` over template content, for example).

### What stays the same in both modes

The tokenizer's states are shared. On the shapes I probed (the attribute-value entity exception,
legacy semicolon-less names, numeric C1 remapping, duplicate attributes, `--!>`, the script escape
states, `<` in an unquoted value) Frostwork, lxml and html5lib agree. What changes is who picks the
state after a start tag. In lxml mode the tag name decides; in browser mode the tree builder does,
because it depends on parsing context (section 4). The cases that show it are an SVG `<title>`, which
holds markup rather than RCDATA, and `<![CDATA[...]]>`, which HTML5 reads as text inside
`<svg>`/`<math>` and libxml2 reads as a bogus comment everywhere.

Encoding resolution, value decoding, what `::text` and `::attr()` return for a matched node, XPath
semantics and raw-source outer HTML are also shared. CSS matching is not: browser mode takes the
browser's CSS semantics, which fold the case of some attribute values (below).

### What browser mode changes

Rows marked "browser" were measured in Chrome 156 and Firefox 159 with CSP-served page loads, and both
agreed. Rows marked "gate" were found by `tools/diff_browser.py` against both browsers, as divergences
no other row explained. Rows marked "lab" come from the lab's Chrome 148 column. The rest compare Frostwork with
html5lib (lxml tree builder, HTML elements un-namespaced, parsel over the result) on the minimal input
shown, and `make gate-browser` (section 5) now confirms every row against Chrome. "Streams" means the
browser's answer is known when the token arrives, so the one-pass matcher produces it. "Replay" means
the rule changes a decision already made, and the page is answered from its finished tree (section 4).

| construct | example | lxml mode | browser mode | how browser mode answers it |
|---|---|---|---|---|
| NUL | `<di\0v>`; NUL in text | deleted everywhere, so `<di\0v>` is a `div` | U+FFFD in names and most text, dropped in body text (lab) | input rules |
| leading strip | indented BOM, leading VT | stripped (Parsel) | kept; a non-whitespace character opens `<body>` | input rules |
| `<noscript>` | `<noscript><p id=t>x` | markup | raw text, scripting on (lab, browser) | the builder picks the data mode |
| HTML `<template>` | `<template id=t><p id=y>x` | `#y` matched | `#t` matches, `#y` never does: the content is an inert fragment (lab, browser). An SVG `<template>` is an ordinary element whose children stay selectable (browser) | inert content, keyed on namespace and name |
| SVG `<title>` | `<svg><title><b id=x>t</b>` | RCDATA by name, so no `<b>` | an HTML integration point: `#x` is a real element (browser) | the builder sets the tokenizer state |
| implied `<tbody>` | `table > tbody > tr > td` | empty; `table > tr > td` matches | the reverse: DevTools' "Copy selector" works (browser) | synthetic element |
| `/>` on a non-void tag | `<div class="a"/>text` | div closed, `div.a::text` empty | `/>` ignored, text inside the div (browser) | streams |
| `<p>` closers | `<p>a<section>b</section>c` | `p::text` is `a`, `c` | `a`: HTML5's closer set includes `section`, `article`, `nav`... | streams |
| deep `<p>` | `<p><b><div>x` | `div` inside `p` | `p` closed (button-scope walk) | streams |
| stray `</p>` | `<div>A</p>B</div>` | one node `AB` | empty `<p>` inserted, so `A` and `B` | synthetic element |
| formatting reconstruction | `<p><b>1</p>2` | `b::text` is `1` | `1`, `2`: after `</p>`, a clone of `<b>` reopens for the `2` (browser) | a synthetic copy with the original's attributes |
| quirks-mode tree | `<p id=p><table id=t>` with no doctype | `<table>` closes the `<p>` | in quirks mode the table stays inside the `<p>`; with a doctype it closes it (browser). The document mode comes from the doctype | the document mode, read from the doctype |
| leading newline | `<pre>\nx`, also `<textarea>`, `<listing>` | `\nx` | `x`: a newline right after the start tag is dropped (browser) | streams |
| form pointer | `<form id=a><form id=b><input>`; `<table><form id=f><tr><td><input id=i>` | the second form nests; the input is inside `#f` | the second form is ignored; in a table the form is an empty child of the table, so `#f #i` is empty (browser) | streams; a form that leaves the stack stays a DOM ancestor |
| declarative shadow root | `<div><template shadowrootmode=open><p id=s>` | the `<template>` and `#s` match | the `<template>` leaves the document tree and its content becomes a shadow root, so neither matches a document selector (browser) | inert, with the host conditions |
| frameset replacing the body | `<div id=x></div><frameset>` | `#x` stays | the body and `#x` are removed and the `<frameset>` replaces them (browser). Text first makes the `<frameset>` ignored instead | replay |
| `dt`/`dd` repeat | `<dl><dt>a<dt>b` | nested | siblings | streams |
| table parts outside a table | `<td>x`, `<tr><input>` | the element is built | the start tag is ignored and its content joins the current node (gate) | streams |
| `<frame>` outside a frameset | `<div><frame>` | the element is built | ignored (gate) | streams |
| foreign-content breakout | `<svg><pre>`, `<math><p>` | nested inside the `<svg>` | an HTML start tag such as `<p>` or `<pre>` closes the foreign elements first (gate) | streams |
| closes HTML5 does not do | `<pre><dd>`, `<dt><dl>` | libxml2 closes the first element | both stay open, so the second nests inside the first (gate) | streams |
| head content after the head | `<style>s</style><button>` and similar | stays where written | head-only elements arriving in "after head" go back into the head (gate) | the head is parked, then reopened |
| `<select>` rules | a `<select>` inside a `<select>` | nested | the second closes the first (gate) | streams |
| nested `button`/`h1`/`nobr` | `<button>x<button>y` | nested | the outer one closes | streams |
| `<head>` | `<p>x` | no head | an empty head is always built | synthetic element |
| after `</html>` | `…</html><div id=a>late` | outside `body` | inside `body`: HTML5 never pops `body` or `html` before EOF | streams |
| renames | `</br>`, `<image>` | ignored, unknown element | `<br>`, `<img>` | streams |
| CDATA in foreign content | `<svg><text><![CDATA[b]]>` | bogus comment | text | tokenizer hook |
| foster parenting | `<table><p id=t>moved` | `table #t` matches | `<p>` moved before the table (lab, browser) | ancestry streams (the table is parked); document order by replay |
| adoption agency | `<b>1<p>2</b>3`; `<a><div>t</a>u</div>` | `b::text` is `1`; `a::text` empty | `<p>` moves out of the `<b>`, so `b p` is empty and `b::text` is `1`, `2` (browser); `t` inside a cloned `<a>` | streams without a furthest block; replay with one |
| second `<html>`/`<body>` tag | `<body x=1>…<body y=2>` | ignored | attributes merged into the open element | replay, when it adds an attribute a selector reads |
| `<selectedcontent>` | `<select><button><selectedcontent></selectedcontent></button><option>A` | an empty element | the selected option's content is copied in when the option closes (browser) | replay |
| foreign attribute names | `<svg viewbox="0 0 1 1">`, `<math definitionurl=x>`, `xlink:href` | as written, lowercased | `viewBox`, `definitionURL`, and `xlink:`/`xml:`/`xmlns` namespaced on foreign elements (browser) | streams |
| depth cap | more than 512 nested elements | nested | a new element becomes a sibling of the current node (browser) | replay |

`<select>` content is not a departure. html5lib and parse5 drop a `<div>` written inside a `<select>`,
but Chrome 156 and Firefox 159 keep it since the customisable-`<select>` parser change, and so does
lxml. It is also an example of why the libraries cannot be the oracle.

Foster parenting is the clearest case of a rule that needs both halves. A fostered element's parent is
known when it opens (the table's own parent), so its ancestry streams. Its place does not: in the
browser's tree it comes before the table, while the source puts it after the table's start, so `p +
table`, the table's sibling positions and the order of a column are all decided after the fact. That half
goes through the replay.

Parsing semantics and selector semantics are separate, and browser mode has to keep them apart in
three places:

- **Quirks mode is two features.** The tree half is implemented: without a doctype, `<p><table>`
  nests where with one it does not, in both browsers, so the builder computes the document mode from
  the doctype (WHATWG's quirks, limited-quirks and no-quirks tables). The selector half, where class and
  id selectors match case-insensitively in quirks mode, is deferred, and the browser gate attributes it
  to its own value construct.
- **Attribute case folding is CSS-only.** HTML lists 46 attributes whose values CSS matches
  case-insensitively on HTML elements, so `input[type=submit]` matches `type="SUBMIT"` in both browsers,
  while XPath `//input[@type='submit']` does not. Chrome folds exactly that list and does not fold on
  SVG elements (measured). Browser mode does the same: the attribute test carries which syntax it came
  from, and the list is a compile-time table. Lxml mode compares exactly.
- **Form owner is not ancestry.** Chrome's `DOMParser` builds the same tree as a page load for a form
  inside a table but does not set the input's form owner. Selectors only see the tree, so this
  matters only to anyone tempted to compare whole documents rather than trees.

## 4. How it fits the engine

In lxml mode, tree rules used to reach the matcher through six functions of a tag name and the open
elements (`classify`, `start_closes`, `blocks_end_tag`/`end_tag_discardable`, `frame_content_of`,
`data_mode_of`, and `matcher/frame.rs`). That boundary fits libxml2. It does not fit HTML5, whose tree
builder is stateful and steers the tokenizer, so both modes now sit behind one stateful interface.

### One seam, two builders

`src/matcher/tree.rs` defines `TreeBuilder`. The tokenizer's events (start tag, end tag, text,
comment, doctype) go to the builder, and the builder answers them by calling a few matcher primitives:
`open_element` (also for an element the page never wrote), `close_top`, `buffer_text`/`flush_text`,
`absorb_into_text_gap`, and `park`/`unpark` (below). The matcher evaluates selectors and the builder
never does, so the two modes share every line of matching, capture, decoding and grouping code.
`CompiledSchema::run` picks the builder once per document, and `Scan<B>` is monomorphised over it, so
nothing on the per-token path asks which mode it is in.

- `src/matcher/libxml2.rs` is lxml mode: libxml2's rules, moved there unchanged, over the generated
  tables in `implied_close/generated.rs`.
- `src/matcher/browser/` is browser mode: `rules.rs` holds the insertion modes (WHATWG 13.2.6, initial
  through after after frameset), `doctype.rs` the document-mode tables, `tags.rs` names and the SVG and
  MathML adjustments, `dom.rs` the replay's tree, and `mod.rs` the builder's state and how it drives the
  matcher. The HTML5 element categories (special, formatting, the scope lists, implied end tags, void,
  data mode) are generated from Chrome by `tools/gen_html5_rules.py` into
  `implied_close/generated_html5.rs`. The algorithms that walk the stack are written from the spec and
  graded by sequence.

The builder chooses the tokenizer's next state. `start_tag` returns the data mode for the element's
content, because in HTML5 it depends on where the tag lands: an SVG `<title>` is an HTML integration
point whose content is markup, while an HTML `<title>` is RCDATA, and `/>` is honoured in foreign content
and ignored on a non-void HTML element. `cdata_is_text` answers whether `<![CDATA[` is character data at
that point, which it is only in foreign content.

### The parser stack is not the DOM path

The matcher reads ancestry, positions and sibling state off its own stack, so in browser mode that stack
is the DOM path to the current insertion point, and the HTML5 stack of open elements is the builder's
own. Under libxml2 the two coincide. Under HTML5 they differ in four ways:

- **Ghosts.** An element can leave the HTML5 stack while it is still a DOM ancestor of what is open. In
  `<form id=f><div id=d>a</form><p id=after>x</p>` the `</form>` removes the form but leaves the div open
  inside it, so `#f #after` matches. The form's matcher element stays on the matcher stack with no
  builder entry, and closes when the insertion point next moves below it.
- **Parked elements.** Foster parenting inserts beside the table, in the table's parent, while the table
  and its rows stay open. The matcher parks the table's run of elements, with their positions, group
  instances and pending text, so fostered content is a child of the table's parent and not a descendant
  of the table, and unparks them when the next insertion targets them. The head is parked in "after
  head", where whitespace goes to `<html>` but a later `<script>` still goes into the head, and so is the
  current node past Chrome's depth cap of 512.
- **Inert elements.** HTML template content and declarative shadow roots are parsed with the full rules
  but never reach the matcher. An SVG or MathML `<template>` is an ordinary element, so this keys on
  namespace and name together.
- **Pending elements.** The adoption agency's copies of a formatting element enter the HTML5 stack
  without a matcher element and get one only if something is inserted into them.

`OpenElem` keeps its size and lxml mode runs none of this.

### The replay

Five rules change a decision the matcher has already made (Answers first). The streaming pass runs
unchanged until the first of them fires, and then stops. The page is then answered by the replay: the
same builder, compiled with `REPLAY` set, runs again from the start, and every insertion becomes a DOM
operation (append, insert before the table, move, copy, detach) on an arena of the finished tree in
`browser/dom.rs`. The matcher then walks that tree in pre-order, each node's rank serving as its document
position, so values come out in document order even where fostering moved them. A merge of `<html>` or
`<body>` attributes counts only when it adds an attribute some selector of the schema reads, which makes
it the one rule whose firing depends on the schema.

Over 30,031 pages, three Common Crawl samples plus `fixtures/realweb` (`tools/retro_census.py`, each
page with its own production selectors), the rules fire on these shares of pages:

| rule | pages |
|---|---|
| a second `<html>`/`<body>` adds an attribute a selector reads | 1.48% (2.42% for any attribute) |
| the adoption agency with a furthest block | 1.14% |
| foster parenting | 0.46% |
| a `<frameset>` replacing the body | 1 page |
| `<selectedcontent>` | none |
| any of them, so the page replays | 2.99% (none of the 31 realweb pages) |

On a replayed page, `extract` took a median of 1.35 and a 90th percentile of 2.0 times one streaming
pass (200 such pages, minimum of 15 runs each, on a shared CPU), against 1.06 on pages that stream.

The alternative was to replay the tokens with hints about what is to come: a body inert from the start,
a table's subtree deferred past its fostered content, the adoption agency's final parents applied when
the block opens. It needs every event known before it starts, so it would run the builder over the whole
document first anyway, and it needs a mechanism per rule where the tree needs one for all five.

Early exit (`first_only` columns) stays sound. A replay walks a finished tree, and the streaming pass
stops early only where no later token can reach back: no open table, no formatting element holding an
open special element, no body a frameset could still remove, and no `<html` or `<body` bytes left in the
page.

### The rest of the engine

- **Synthetic elements.** The frame (`html`, `head`, `body`), an implied `<tbody>`, `<tr>` or
  `<colgroup>`, and the `<p>` a stray `</p>` inserts are opened through the same primitive with no source
  of their own. A formatting element's copy carries the original's attributes, and its outer HTML is the
  start tag it was copied from followed by its own content.
- **Input preparation.** Browser mode skips Parsel's strip and NUL deletion, and consumes only a UTF-8
  BOM at offset 0. NUL follows HTML5: tag and attribute names keep the byte, so `<di\0v>` does not match
  `div`; body text drops it; raw text, RCDATA and attribute values turn it into U+FFFD.
- **Names.** Attribute names follow Chrome's DOM: lowercased, SVG camel case, MathML `definitionURL`, and
  `xlink:`/`xml:`/`xmlns` namespaced on foreign elements. `::attr()` and XPath `@` compare the name
  exactly. CSS `[attr]` compares it case-insensitively and never matches a namespaced attribute.

### Rescans and node handoffs

Two features read raw source again after the pass. In lxml mode both rest on a libxml2 property that
HTML5 does not have: an element's raw source, parsed on its own, builds the same subtree.

- **Deferred tails.** A deferred winner (`:has()`, a reverse position, a text predicate) whose value is
  inside it (`li:last-child b::text`, `div:has(a) ::text`) has its values read after the pass. Lxml mode
  re-scans each winner's raw span as a standalone document (`Matcher::resolve_tail_spans`), which is
  sound because a libxml2 span is self-contained, and the differential proves it. An HTML5 span is not.
  `<tr><td>c</td></tr>` parsed on its own has neither a row nor a cell. The `<b>` that reconstruction
  copies into the last `<li>` of `<p><b>x</p><ul><li>one<li>two</ul>` was opened before that `<li>`. An
  SVG `<title>` holds a `<b>` element only inside its `<svg>`. The form pointer, the adoption agency and
  foster parenting read state from outside the span too.

  So browser mode parses the whole document a second time, with the same builder
  (`Matcher::resolve_tail_pass`). The builder's decisions depend on the input alone, never on the schema,
  so the second pass opens the same elements in the same order. The first pass records each winner by its
  place in that order (`OpenElem::elem`), which names an implied `<tbody>` or a formatting element's copy
  as well as an element the page wrote, where a start offset would not. The second pass puts a mark on
  each winner that no page or selector can spell, and every tail of the schema is compiled into one
  schema whose selectors are rooted at that mark (`marked_tail`). A tail column is then an ordinary flat
  column of the second pass: every element has its true context, each node comes once, in the order the
  pass streams it, and nested winners need no de-duplication. The cost is one more pass over a document
  on which a winner arose, however many tail columns and winners the schema has, and nothing for a
  schema with no tail or a document with no winner. `make gate-browser` grades the tails against Chrome
  over documents built for this (`TAIL_CONTEXTS` in `tools/diff_browser.py`), and fails a build that
  re-scans each span alone instead.

  Two alternatives were weighed. A re-scan seeded with the builder's state at the winner's start would
  have to snapshot that state at every provisional winner, since a winner is only known once its subtree
  or its parent has closed, and it could not stop at the winner's end tag: content after it can land
  inside the winner (`</form>` leaves a `<div>` open) and content inside the span can be fostered out.
  Buffering candidate values during the pass would hold every provisional winner's subtree until it
  resolves, which is the retention a one-pass engine exists to avoid, and it would cost on every
  document with a candidate, winner or not.
- **`.as_node()`** reparses the raw source through `lxml.html.document_fromstring`. In browser mode a
  processor would receive libxml2's subtree for a node the browser built differently.

Browser mode therefore refuses two things lxml mode supports, each as `UnsupportedSelector` with a
reason that `frostwork.check` reports per mode: an `.as_node()` handoff, and the outer HTML of an element
the page never wrote (the frame, an implied `<tbody>`, `<tr>` or `<colgroup>`, the `<p>` a stray `</p>`
inserts), which has no source of its own to return. Every shape browser mode does support is proven
against the browser oracle first. A browser-mode `.as_node()` would hand over a node built from the
browser-mode engine's own view, never an lxml reparse.

## 5. Oracle and gates

This is the expensive part. `diff_lxml`, `gate-corpus`, `gate-seq`, `audit_tree_rules`,
`gen_tree_rules --check`, `mutate_rules`, `diff_fuzz`, `sel_fuzz` and `gate-webpoet` all grade against
lxml, and they now pin `parser="lxml"`. Browser mode is only as trustworthy as its own oracle, and the
gates below are that oracle.

### The oracle is the browser

The closest oracle to a browser is the browser, and both stock browsers on this Mac can play the part.
I drove Chrome 156 over the DevTools Protocol and Firefox 159 over WebDriver BiDi (Firefox no longer
ships CDP), each headless from a temporary profile, with a server inside the harness supplying exact
bytes and headers. The question was which way of getting a document into the browser builds the tree
a user's page load builds. The probe page held a `<noscript>`, a `<template>`, an inline script, a
fostered `<p>`, a table that needs an implied `<tbody>`, a self-closed `<div/>` and a `<div>` inside a
`<select>`.

| entry point | scripting flag | page scripts | same tree as a page load | Chrome 156, docs/s | Firefox 159, docs/s |
|---|---|---|---|---|---|
| page load | on | run | no: scripts can change the tree | | |
| page load, `Content-Security-Policy: script-src 'none'` | on | blocked | yes, by construction | 98 (181 across 8 tabs) | 112 (221) |
| page load, CSP `sandbox` | off | blocked | no: `<noscript>` parses as markup | | |
| page load, CDP "disable JavaScript" | off | blocked | no: the same | | |
| `srcdoc` iframe inside a CSP page | on | blocked | Chrome 5002 of 5002; Firefox 7 of 5002 differ (quirks) | 520 | 596 |
| `DOMParser` | off | none | 4478 of 4478 eligible documents in both browsers; differs on 112 of the 524 with `shadowrootmode`, and on `<noscript>` | 20,477 | 25,467 |
| `Document.parseHTMLUnsafe` | off | none | Firefox 5002 of 5002; Chrome differs on 333, dropping comment nodes in the cases inspected | | |

The rates are for one browser process and generated documents of about 210 bytes. They include
serialising the whole tree in the page; only its length crossed the protocol in the timed runs, while
the equivalence runs below transferred every tree. A 41 KB page loads at 57 a second (110 across 8
tabs) in Chrome and 49 (100) in Firefox.

The 5002 documents are random runs of up to seven tokens from a 70-token alphabet: table parts,
formatting tags, the frame tags, `<select>` and `<selectedcontent>`, `<template>` including both
declarative shadow root modes, `<svg>`, `<math>`, the foreign-content integration points,
`<frameset>`, self-closed tags, CDATA and processing instructions. Half have no doctype, and none
contains `<noscript>`. The generator is mulberry32, with exact 32-bit arithmetic, and the run refuses
to start unless every token is emitted; each one appeared 245 to 309 times. A float LCG loses
precision past 2^53, and a generator like that silently emits a fraction of its alphabet. Each
document was parsed by a CSP page load and by each other entry point in the same browser, and the
trees were compared node for node, with HTML template content and open shadow roots included.

Three results decide the design. `srcdoc` is out: all 7 of Firefox's differences are a doctype-less
`<p>` followed by `<table>`, because the spec never puts a `srcdoc` document in quirks mode and Firefox
follows it. `parseHTMLUnsafe` is out, because Chrome's drops comments, and a dropped comment merges two
text nodes. Every document where `DOMParser` differs contains `shadowrootmode`: the same 112 in both
browsers, out of the 524 that contain it. Where a page load attaches a shadow root it removes the
`<template>`, while `DOMParser` keeps it as an ordinary element. The other 412 agree, probably because
the template landed where no element can host a shadow root, so it stays a template on both paths;
that explanation is not yet checked. None of the 4478 documents without `shadowrootmode` differs.

So the oracle has two paths in each browser, and eligibility for the fast one is derived from these
results rather than assumed:

- **Fast path: `DOMParser`,** for a document that contains neither `noscript` nor `shadowrootmode`, in
  any letter case. The search is ASCII case-insensitive because tag and attribute names are, and a
  false hit (the word in a comment or in text) only sends a document down the slow path. At about
  20,000 documents a second per process the browser stops being the bottleneck.
- **Slow path: a CSP-served page load,** for everything else and for anything where the bytes matter:
  encoding cases, the lab, crawl pages. That runs at 100 to 200 documents a second per process.
  Several processes in parallel should scale with cores, but I have not measured that.
- **A standing self-check that re-derives eligibility.** Every run re-parses a sample through both
  paths and fails if an eligible document's trees differ. When the spec or a browser adds a parser
  feature (declarative shadow roots, `<selectedcontent>`, processing instructions), its tokens join
  the generator's alphabet, and the eligibility rule grows only from what that measurement shows.
- **Pinned builds.** Chrome for Testing publishes versioned builds that do not auto-update, and Firefox
  release builds are on archive.mozilla.org. Not Playwright's Firefox or WebKit, which are patched
  builds. Safari can be driven through `safaridriver` but not headless, so it is a calibration column
  at most. Every piece of compatibility evidence records the browser builds and the harness commit,
  as it records libxml2's version today.

### Evaluating selectors without losing input

Rebuilding the browser's tree in lxml loses values browsers keep. lxml refuses U+000B, U+0001 and
U+FFFE in text and attribute values, tag names such as `p<img` (the HTML tokenizer ends a tag name only
at whitespace, `/` or `>`) and `a:b`, attribute names such as `1a`, and comments containing `--`, and
each raises `ValueError`. Both browsers keep the control characters and the `p<img` tag name. These
are real malformed-HTML cases, so the gate must not discard or sanitise them. The oracle therefore
evaluates each kind of query where it can do so losslessly:

- **Structure** is compared in the browser's own tree, serialised losslessly to JSON, never through
  lxml.
- **CSS** runs natively in the browser. `querySelectorAll` finds the matches on the browser's own
  document, and the harness extracts `::text` (direct child text nodes), `::attr()` and outer HTML
  from them explicitly. Browser-mode CSS semantics, attribute case folding included, then come from
  the browser itself. Frostwork's extensions (`:contains()`, the pseudo-elements) are applied in-page
  to the native matches.
- **XPath** cannot run natively, because the browser's XPath is the wrong semantics for a scraper. In
  both browsers `document.evaluate("//svg")` and `//circle` find nothing in an HTML document, because
  an unprefixed name only matches an HTML-namespace element, and `//P` matches a `<p>`. Frostwork
  finds both SVG elements and treats `//P` as case-sensitive, as lxml does. So browser mode changes
  the tree that XPath runs over, not the XPath language. XPath goes through the lxml/Parsel bridge,
  with a reversible map: forbidden code points and illegal names become reserved placeholders,
  recorded per document. Frostwork's values pass through the same map, and a round-trip gate decodes
  every rebuilt tree and requires it to equal the browser's JSON tree. A document the map cannot
  represent is counted and reported, never skipped.

The driver is `tools/browser_oracle/probe.cjs`, Node with no packages, covering both protocols. Its
`fidelity`, `throughput`, `equiv` and `claims` modes produced the measurements above, and `serve` is the
oracle the gates call: one browser kept running, one JSON request per line, a lossless tree (and native
CSS answers) per document. `client.py` wraps it as `BrowserOracle("chrome" | "firefox")`, `bridge.py` is
the lossless XPath bridge with its round-trip check, and `css.py` plans each selector as a native
`querySelectorAll` and checks the plan against parsel's own XPath before sending it. `claims.json` holds
the parser fixtures behind every "(browser)" cell in section 3, and `evidence/2026-11-03/` holds the
outputs, each stamped with its browser build.

### Which browser

The browsers do not agree with each other everywhere. On WPT's `html/syntax/parsing` (wpt.fyi stable
runs, 2026-11-03), Chrome 156 passes 8121 of 8166 tests, Firefox 159 passes 7859 of 8164, and Safari
27.2 passes 7832 of 8158. Most of the gap is one 2026 spec change, processing instructions
(whatwg/html#12118), which Chrome shipped in 150 and the others have not. Firefox also fails the four
`selectedcontent` cases of customisable `<select>`, and Chrome and Safari fail some `<search>` cases.

So browser mode has to name its reference, as autodetection does. Decided on 2026-11-03: **where the
two disagree, Frostwork follows Chrome**, because Chrome is what most users see. The default gate
grades against Chrome, and `BROWSERS=chrome,firefox` adds Firefox. Where the two agree, that is the
target. Where they disagree, the case goes in a Chrome-versus-Firefox table of its own, which is
reported but not yet gated in both directions the way `enc_check`'s w3lib table is. In the last
two-browser run that table held a handful of tree and value columns: `</br>` and frameset-ok,
`search` and `dialog` being special in Firefox, `</p>` inside a MathML integration point, and processing
instructions, which Firefox turns into comments.

Encoding and tree construction therefore pin to different browsers, each for its own reason.
Autodetection follows Firefox because Chrome's detector cannot be run outside Chrome. Tree
construction follows Chrome because both browsers can be run as oracles, so the larger audience wins.

### Firefox's parser without Firefox

Chrome's parser cannot be lifted out. Blink's `HTMLConstructionSite` builds Blink DOM nodes directly
and depends on Oilpan (Blink's garbage collector) and WTF, and I found no maintained standalone
extraction. WebKit's parser has the same shape. gumbo came from Google, but it was never Chrome's
parser, and it is archived.

Mozilla has already factored Firefox's parser out. Gecko's C++ parser is generated by a Java-to-C++
translator from the validator.nu htmlparser (`parser/html/java/`), and `parser/html/javasrc/` holds the
Java the current C++ was generated from. Behaviour changes land in the Java and the C++ in the same
commit, and upstream `validator/htmlparser` master differs from Gecko's copy only in C++-only hooks.
Built from git (the Maven artifact dates from 2020) with our own `TreeBuilder` subclass, it would be
Firefox's tree construction on the JVM, pinned to a Firefox release by commit.

Three behaviours live outside the Java, so the subclass would have to match them:

- scripting, which is off by default and has to be turned on;
- template content, which Gecko moves into a fragment and the Java DOM builder does not;
- customisable `<select>`, where the Java DOM builder clones `selectedcontent` and Gecko's hook is a
  stub.

I would not build it first. The browsers' own `DOMParser` is already fast enough, and the Java parser
would need real Firefox to check it anyway. It is the fallback if running browsers in CI turns out to
be a burden: an in-process, Firefox-exact oracle, checked against real Firefox on a sample every run.
The lab's JVM bridge is the obvious host for it.

### Why not parse5 or html5lib

The drift worry is real, and it is measurable today. parse5 8.1.0 (released 2026-11-01) has not
implemented the customisable-`<select>` parser change that Chrome 134, Firefox 153 and Safari 27 have
all shipped, so on `<select>` content it already disagrees with all three. Its html5lib-tests pin is
from 2023, and the tree-construction tests now live in WPT. html5lib's last release was in 2020, and
in the lab both html5lib and lexbor parse `<noscript>` as markup. A library oracle drifts the day a
browser ships a parser change, and no gate here would notice. A browser oracle cannot drift from the
browser.

### Gates

- **`make gate-browser`** (`tools/diff_browser.py`) is the main one. Its documents are every sequence of
  the frame and table alphabet up to `--seq-depth`, seeded random runs over an HTML5-heavy alphabet in
  which every start tag carries an id, decorated documents for the value rules, and documents built
  around deferred tails. It refuses to grade unless every token is emitted. Each document is graded on
  two kinds of column. The tree columns are `seq_sweep`'s whole-tree fingerprint, with a child-axis
  column per id and a column per name browser mode can synthesise, evaluated on the browser's own JSON
  tree. The value columns are a selector basket answered natively in the browser for CSS and through the
  bridge for XPath. Every differing column is reduced to the smallest token subsequence that still
  differs, and the reduced document is attributed to a named construct. A value column that differs
  where the fingerprint agrees has its own constructs. The gate fails on NOVEL (a divergence no construct
  explains), and on coverage: nothing graded, a token never emitted, an oracle error, a bridge round
  trip that changed a tree, or a `DOMParser` tree that differs from a page load. Every tree construct
  has been implemented and deleted, so the gate holds each of them at zero. It then re-measures the HTML5
  categories in Chrome (`gen_html5_rules.py --check --live`).
- **`gen_html5_rules.py --check`** runs in `make py` with no browser. It compares the generated Rust
  file with the stored per-cell answers.
- **`make gate-webpoet-browser`** grades browser-mode page objects against parsel page objects whose
  values come from Chrome, and checks that `.as_node()` is refused at class definition.
- **`make gate-corpus-browser`** is report-only. It serves each corpus page as exact bytes with its
  `Content-Type`, page-loads it, and grades every production selector, split into tree and value.
- **`tools/retro_census.py`** counts how often each retroactive rule fires over a corpus, through the
  `differ` bridge.
- `tests/test_gates.py` seeds a regression into each of these decision functions, the fast/slow
  self-check and the bridge round trip included, so each is shown to go red.

Not built yet: browser-mode verdicts in `diff_lxml`, `gate-seq` and `mutate_rules`, browser columns in
the crawl lab, and pinned browsers in CI. `gate-browser`, `gate-webpoet-browser` and
`gate-corpus-browser` need local browsers and are not part of `make ci`.

## 6. Cost

- **Engine.** Browser mode is one more tree builder behind the seam, plus the replay. Lxml mode keeps
  its own code path, and `OpenElem` its size. The replay roughly doubles the work on the 3% of pages it
  runs on (section 4), and a deferred tail costs one more pass on a page where it has a winner.
  `docs/BENCHMARKS.md` has the measured throughput of each mode.
- **Gates.** A second oracle for the tree, values and the page-object layer, which needs local browsers.
- **Contract.** COMPATIBILITY.md has a section per mode, each with its own divergence list.

## 7. What is left

- Quirks-mode class and id matching, where CSS compares case-insensitively without a doctype. The gate
  attributes it to its own value construct.
- Outer HTML is the element's raw source, as in lxml mode, and browser mode refuses it for an element
  the page never wrote. Attributes merged in from a later `<html>` or `<body>` tag do not appear in
  that source.
- `.as_node()` is refused in browser mode. A faithful one would hand over a node built from browser
  mode's own tree, never an lxml reparse.
- XPath names must be lowercase in both modes, and browser mode keeps SVG and MathML names in their
  camel case. So an XPath query cannot reach a camel-cased foreign name in browser mode at all:
  `@viewBox` and `//clipPath` are refused, and `@viewbox` and `//clippath` match nothing, where lxml mode
  answers the lowercase spelling. CSS reaches them (`svg::attr(viewBox)`, `clipPath`).
- The gaps listed under Gates.

## 8. Decisions

Decided on 2026-11-03: E2 (unlabelled valid UTF-8 stays UTF-8), E3 (autodetection follows Firefox),
browser mode is the default, and where Chrome and Firefox build different trees, browser mode follows
Chrome. The six questions this proposal left open were settled as recommended:

1. The oracle is the browsers themselves: the `DOMParser` fast path, CSP-served page loads, and Chrome
   and Firefox, with Firefox's Java parser held in reserve.
2. Browser-mode CSS folds HTML's case-insensitive attribute list. Quirks-mode class and id matching is
   deferred; quirks-mode tree construction is not.
3. The public names are `parser="browser" | "lxml"`, with browser builds recorded in the evidence rather
   than in the name.
4. Browser mode refuses synthetic outer HTML and `.as_node()`, and reads a deferred winner's subtree with
   a second whole-document pass.
5. Browser-mode XPath has lxml's XPath semantics over the browser's tree, so `//svg` still finds SVG.
6. `encoding=` stays below the BOM, with no BOM-beating override.

One decision was taken during the build and is Shane's to confirm: the replay builds a tree of the page
on the 3% of pages where a retroactive rule fires, instead of leaving those rules as documented
divergences.

## Sources and versions

Measured for the design on 2026-11-03, on macOS:

- Frostwork `main` at `68927e9`, with the extension rebuilt from it, and web-poet 2.5.0 with w3lib from
  the repo's `.venv`.
- Chrome 156.0.8078.80 and Firefox 159.0, stock builds, headless, each from a fresh temporary profile,
  driven over CDP and WebDriver BiDi. The first top-level checks used Chromium 152 inside the Claude
  desktop app, and are superseded by the lab's Chrome 156 capture.
- `~/src/encoding-hacks` at its uncommitted 2026-11-03 state (`STATE.md`), with 32 cells from the
  Chrome 148 iframe capture and 16 from Chrome 156 top-level loads.
- `tools/browser_oracle/`: `probe.cjs` and its fixtures for section 5 and the "(browser)" cells,
  `lab_compare.py` for section 1 and E1, and the raw outputs in `evidence/2026-11-03/`. The fidelity,
  equivalence and claims outputs were re-run from the saved harness and reproduce every count above;
  the throughput files are the prototype's run of the same loops.

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
  and [google/compact_enc_det](https://github.com/google/compact_enc_det).
- The move of the tree-construction tests to WPT:
  [html5lib-tests 224991ec10](https://github.com/html5lib/html5lib-tests/commit/224991ec10) and
  [web-platform-tests/wpt#59976](https://github.com/web-platform-tests/wpt/pull/59976).
- Shipping versions for customisable `<select>` (Chrome 134, Firefox 153, Safari 27) and processing
  instructions (Chrome 150), and the parse5 8.1.0 release, from the projects' release records.
- Pinned automation builds: [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/);
  [Playwright's patched Firefox and WebKit](https://playwright.dev/docs/browsers);
  [Firefox's CDP retirement](https://fxdx.dev/cdp-retirement-in-firefox/).
- chardetng 1.0.0's `Utf8Detection` documentation (`src/lib.rs`), and Scrapy's
  [`TextResponse`](https://github.com/scrapy/scrapy/blob/master/scrapy/http/response/text.py).
