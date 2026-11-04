# Browser compatibility: encoding, then a browser parse mode

Status: proposal, with six open decisions in section 8. Drafted by Claude for Shane, then reviewed by
Gemini (`gemini-4-argon`), Astra and Claude Fable, with every accepted finding re-measured before it
went in. Measured against `main` at `68927e9` on 2026-11-03; sources and versions are at the end.

Scope: browser mode is the default and is graded against real browsers; lxml mode stays available as
`parser="lxml"` for libxml2 parity. Adrian owns the Scrapy and w3lib integration. This proposal covers the core engine and the
API he builds against.

## Answers first

**Encoding.** The engine's encoding decision already matches the browser on every case
`~/src/encoding-hacks` measures, apart from two deliberate exceptions. Frostwork is still not
browser-compatible on encoding in practice, for three reasons:

1. The Python integrations hand the engine the response's *resolved* `.encoding`, which is w3lib's
   decision, and a caller label outranks Frostwork's own prescan. Through web-poet, 21 of the lab's 48
   cases differ from Chrome against 9 for the engine alone, and routing the header through w3lib drops
   `x-user-defined`.
2. On an unlabelled page that is valid UTF-8, Frostwork reads UTF-8. Chrome refuses to, on purpose,
   and shows mojibake (measured), and chardetng documents the same policy for Firefox (not yet
   measured). Decided: Frostwork keeps its answer, as one named exception.
3. Autodetection uses chardetng, which is Firefox's detector. Chrome uses a different one and they
   disagree. Decided: autodetection is pinned to Firefox.

The lab's two remaining "encoding" gaps (`utf32-bom`, `nul-split`) are not encoding at all. They come
from Parsel's input normalisation (NUL deletion), and they belong to the parse mode.

**A parse-mode option.** Yes, and the two modes stay separate, but browser mode is more than a
second set of answers to today's six rule functions. HTML5's tree builder is stateful. It chooses the
tokenizer's next state (an SVG `<title>` holds markup, an HTML one does not), tracks namespaces, and
keeps DOM ancestry apart from the parser stack. The two features that reparse source spans, deferred
selectors and `.as_node()`, lose their context in browser mode, so the first version refuses them
(section 4). Most of the cost is in the gates. Every gate in this repo is oracled against lxml, so
a second mode needs a second oracle, a second rule derivation and a second set of runs.

**The oracle can be the browsers themselves.** In both Chrome 156 and Firefox 159, a page load served
with `Content-Security-Policy: script-src 'none'` builds the real tree without running page scripts.
`DOMParser` built the same tree as that page load for all 4478 of 5002 random documents that contain
neither `<noscript>` nor a declarative shadow root, at over 20,000 documents a second per browser
process. That is fast enough for every gate, and it cannot drift from the browser the way parse5 or
html5lib can (section 5).

Some HTML5 rules change what a single forward pass has already decided: the adoption agency
algorithm, the document-order half of foster parenting, a second `<html>` or `<body>` tag merging its
attributes into an element already matched, and a `<frameset>` that replaces a body already parsed.
The first two are documented divergences in lxml mode today. All four should be documented
divergences in browser mode unless Phase 2 chooses to buffer for one of them.

## 1. What the lab shows today

The lab's browser column holds two captures. The 16 cases whose answer depends on the fallback
encoding (`no-declaration` and the 15 declaration-depth cases) were captured by top-level page loads
in headless Chrome 156 (en-GB). The other 32 are the Chrome 148 iframe capture; Chrome 156's iframe
answers equal the stored ones on all 32, so mixing the builds moves no grade. The depth cases declare
ISO-8859-15 over the byte 0xA4, so honouring the declaration (`€`) and ignoring it (`¤` under
windows-1252) give different text.

I ran the 48 cases through `frostwork.extract` (`main` at `68927e9`) with the raw `Content-Type`
charset. 9 differ from the browser.

| cases | cause | verdict |
|---|---|---|
| `utf32-bom`, `nul-split` | NUL deletion (Parsel parity) | parse mode |
| `noscript`, `template`, `foster` | libxml2 tree construction | parse mode |
| `demo-price`, `demo-stock` | the `<template>` decoy, plus `<noscript>` | parse mode |
| `no-declaration` | unlabelled valid UTF-8: Chrome shows windows-1252 mojibake, Frostwork returns the text | deliberate (E2) |
| `body-meta-512k` | autodetection: Chrome guesses windows-874 (`1.299,00 ค`), chardetng windows-1252 (`1.299,00 ¤`) | deliberate (E3) |

On every other depth case Frostwork matches Chrome: a head declaration is honoured at any depth up to
1 MB, a body declaration up to byte 512, and none from body byte 1024 on.

So at the engine level there is no unexplained encoding divergence left in the lab, and two
deliberate ones. COMPATIBILITY.md describes the UTF-8 check and the chardetng step as behaviour, but
says neither that browsers answer `no-declaration` differently nor that Chrome's guesses differ from
Firefox's.

## 2. Encoding changes

### E1. Give the engine the transport label, not the decision

`Page.extract_response` and `FrostPage.frostwork_input` pass `response.encoding`. In web-poet that
property is `_encoding or BOM or Content-Type or w3lib's <meta> regex or an inferred fallback`, so it
is always a string. Frostwork's BOM check still runs first, but its prescan and its autodetection
never run behind a web-poet or Scrapy response.

Measured over the same 48 cases:

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
correctly when it gets only the header, so through web-poet today, the prescan work this repo did to
match the browser does not reach the user.

The fix splits by owner. Adrian owns the Scrapy and w3lib integration. This work owns the core and
the contract he builds against, so the core has to make the right call easy and the wrong one hard.

**Core (this work):**

- Add `content_type=` to `extract`, `extract_grouped`, `detect_encoding`, `Plan.extract*` and
  `Page.extract`. `url=` already exists on all of them; the gap is that the two response entry points
  never pass it (below). Rust parses the charset parameter and resolves it with `Encoding::for_label`,
  without the Python-codec detour, so `x-user-defined` and the `replacement` labels survive.
- One precedence table, the same for every entry point:

  | rank | source | an unusable value |
  |---|---|---|
  | 1 | BOM: UTF-8, UTF-16LE, UTF-16BE | n/a |
  | 2 | BOM-less UTF-16, from the `<?` prefix | n/a |
  | 3 | `encoding=`, the caller's own label | unknown to every codec: raise. A real encoding WHATWG lacks (`utf-7`): ignored, continue |
  | 4 | `content_type=`, its charset parameter | ignored, continue. It is server data, so it never raises |
  | 5 | document declarations: an XML declaration at offset 0, then `<meta>` in document order, in the head at any depth or within the first 1024 bytes of the body | unsupported labels are skipped, and the scan continues |
  | 6 | autodetection: UTF-8 if valid (the E2 exception), else chardetng with the TLD of `url=` | n/a |

  This keeps `encoding=` exactly as it behaves today, in rank 3 after the bytes that cannot lie. A BOM
  beats `encoding="windows-1252"` now (measured), and it keeps beating it. That is a difference from
  Scrapy and web-poet, which rank `_encoding` ahead of the BOM. A label that outranks a BOM would be a
  new override contract, and a BOM-marked document read as anything else is mojibake, so I would not
  add one unless Adrian's integration needs it.
- **`str` input has already been decoded**, so no rank applies. `encoding=` must be absent or a UTF-8
  label, which is today's rule, and anything else raises. `content_type=` is accepted and its charset
  ignored, because transport metadata describes bytes that no longer exist and must not reinterpret
  decoded text.
- **Detection and extraction make the same decision.** Today they do not on an unknown label.
  `extract` and `Page.extract` raise, because the check lives in `page._check_encoding`.
  `detect_encoding` ignores the label and sniffs, and so does `Plan.extract*`, which is what
  `FrostPage` calls, so the integration path silently sniffs where the public function raises. Every
  valid label I tried agrees. Move the rank-3 check below every entry point, into the binding or the
  Rust resolver, and gate it: every `enc_check` input through `extract`, `detect_encoding`,
  `Plan.extract*` and `FrostPage` must give the same answer, error included.
- `detect_encoding(body, content_type=..., url=..., encoding=...)` returns the WHATWG name the scan
  will use, so a framework can set its own `.encoding`, and with it `.text`, from the same decision.
  An `explain=True` form returns the winning source as well (`"bom"`, `"utf16-prefix"`, `"encoding"`,
  `"content-type"`, `"xml-declaration"`, `"meta"`, `"utf8"`, `"autodetect"`), which is what an
  integrator debugging a mismatch needs first.
- Write the contract down in PYTHON.md, where an integrator will look for it. Pass the raw
  `Content-Type` header value, the response URL, and `encoding=` only when the user forced one; never
  pass a resolved encoding. Each argument gets one sentence on what goes wrong if it is omitted or
  replaced by `response.encoding`, with the counts above as evidence.
- Gate the core: every `enc_check` case through the public API with header-only input must give the
  engine's answer.

**Integrations:**

- In this repo, `Page.extract_response` and `FrostPage` follow the contract. They pass the header and
  the URL (neither passes one today, so chardetng's TLD hint never reaches either), and pass a forced
  encoding from `_encoding` as `encoding=`, where it ranks below a BOM. web-poet's
  `HttpResponse(encoding=...)` and Scrapy's `TextResponse(encoding=...)` both store it there and both
  rank it first, ahead of BOM, header and `<meta>`, so the one shape where the two can disagree is a
  forced label on a BOM-marked body. Document that in the contract. Scrapy's order is
  `self._encoding or self._bom_encoding() or self._headers_encoding() or self._body_declared_encoding()`
  ([source](https://github.com/scrapy/scrapy/blob/master/scrapy/http/response/text.py)).
  Pin that private-attribute dependency with one test per framework, and run every `enc_check` case
  through a real web-poet `HttpResponse` and `FrostPage`.
- Scrapy and w3lib are Adrian's. The core API and the contract above are what he needs from this side.
- `diff_webpoet` grades against parsel built from `response.text`, which is w3lib's decision. If its
  generator can emit any of the shapes above, it will grade the fix as a divergence, so it needs
  `enc_check`'s browser-difference table or a generator that stays out of those shapes.

None of this needs the shared-decision API (scrapy/w3lib#333, reverted) to land.

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
shown, and the browser gate in section 5 is what would confirm them. "Streams" means the browser's
answer is known when the token arrives, so the one-pass matcher can produce it.

| construct | example | here (= lxml) | browser | streams | phase |
|---|---|---|---|---|---|
| NUL | `<di\0v>`; NUL in text | deleted everywhere, so `<di\0v>` is a `div` | U+FFFD in names and most text, dropped in body text (lab) | yes | 1 |
| leading strip | indented BOM, leading VT | stripped (Parsel) | kept; a non-whitespace character opens `<body>` | yes | 1 |
| `<noscript>` | `<noscript><p id=t>x` | markup | raw text, scripting on (lab, browser) | yes, data mode | 1 |
| HTML `<template>` | `<template id=t><p id=y>x` | `#y` matched | `#t` matches, `#y` never does: the content is an inert fragment (lab, browser). An SVG `<template>` is an ordinary element whose children stay selectable (browser) | yes, keyed on namespace and name | 1 |
| SVG `<title>` | `<svg><title><b id=x>t</b>` | RCDATA by name, so no `<b>` | an HTML integration point: `#x` is a real element (browser) | yes, the builder sets the tokenizer state | 2 |
| implied `<tbody>` | `table > tbody > tr > td` | empty; `table > tr > td` matches | the reverse: DevTools' "Copy selector" works (browser) | yes, synthetic element | 2 |
| `/>` on a non-void tag | `<div class="a"/>text` | div closed, `div.a::text` empty | `/>` ignored, text inside the div (browser) | yes | 2 |
| `<p>` closers | `<p>a<section>b</section>c` | `p::text` is `a`, `c` | `a`: HTML5's closer set includes `section`, `article`, `nav`... | yes | 2 |
| deep `<p>` | `<p><b><div>x` | `div` inside `p` | `p` closed (button-scope walk) | yes | 2 |
| stray `</p>` | `<div>A</p>B</div>` | one node `AB` | empty `<p>` inserted, so `A` and `B` | yes | 2 |
| formatting reconstruction | `<p><b>1</p>2` | `b::text` is `1` | `1`, `2`: after `</p>`, a clone of `<b>` reopens for the `2` (browser) | yes, synthetic element | 2 |
| quirks-mode tree | `<p id=p><table id=t>` with no doctype | `<table>` closes the `<p>` | in quirks mode the table stays inside the `<p>`; with a doctype it closes it (browser). The document mode comes from the doctype | yes, the mode is known before the first element | 2 |
| leading newline | `<pre>\nx`, also `<textarea>`, `<listing>` | `\nx` | `x`: a newline right after the start tag is dropped (browser) | yes | 2 |
| form pointer | `<form id=a><form id=b><input>`; `<table><form id=f><tr><td><input id=i>` | the second form nests; the input is inside `#f` | the second form is ignored; in a table the form is an empty child of the table, so `#f #i` is empty (browser) | yes, form-pointer state | 2 |
| declarative shadow root | `<div><template shadowrootmode=open><p id=s>` | the `<template>` and `#s` match | the `<template>` leaves the document tree and its content becomes a shadow root, so neither matches a document selector (browser) | yes, with the spec's host conditions | 2 |
| frameset replacing the body | `<div id=x></div><frameset>` | `#x` stays | the body and `#x` are removed and the `<frameset>` replaces them (browser). Text first makes the `<frameset>` ignored instead | only by buffering every match since the body opened until frameset-ok clears. Text clears it, and so do some start tags (`li`, `table`, `img`, `input`...), but `div`, `p`, `span`, `a` and `form` do not, so the buffer is unbounded in principle though short on real pages. Otherwise a divergence | 2 or divergence |
| `dt`/`dd` repeat | `<dl><dt>a<dt>b` | nested | siblings | yes | 2 |
| table parts outside a table | `<td>x`, `<tr><input>` | the element is built | the start tag is ignored and its content joins the current node (gate) | yes | 2 |
| `<frame>` outside a frameset | `<div><frame>` | the element is built | ignored (gate) | yes | 2 |
| foreign-content breakout | `<svg><pre>`, `<math><p>` | nested inside the `<svg>` | an HTML start tag such as `<p>` or `<pre>` closes the foreign elements first (gate) | yes | 2 |
| closes HTML5 does not do | `<pre><dd>`, `<dt><dl>` | libxml2 closes the first element | both stay open, so the second nests inside the first (gate) | yes | 2 |
| head content after the head | `<style>s</style><button>` and similar | stays where written | head-only elements arriving in "after head" go back into the head (gate) | yes | 2 |
| `<select>` rules | a `<select>` inside a `<select>` | nested | the second closes the first (gate) | yes | 2 |
| nested `button`/`h1`/`nobr` | `<button>x<button>y` | nested | the outer one closes | yes | 2 |
| `<head>` | `<p>x` | no head | an empty head is always built | yes | 2 |
| after `</html>` | `…</html><div id=a>late` | outside `body` | inside `body`: HTML5 never pops `body` or `html` before EOF | yes | 2 |
| renames | `</br>`, `<image>` | ignored, unknown element | `<br>`, `<img>` | yes | 2 |
| CDATA in foreign content | `<svg><text><![CDATA[b]]>` | bogus comment | text | yes, tokenizer flag | 2 |
| foster parenting | `<table><p id=t>moved` | `table #t` matches | `<p>` moved before the table (lab, browser) | ancestry yes; document order and siblings no | divergence |
| adoption agency | `<b>1<p>2</b>3`; `<a><div>t</a>u</div>` | `b::text` is `1`; `a::text` empty | `<p>` moves out of the `<b>`, so `b p` is empty and `b::text` is `1`, `2` (browser); `t` inside a cloned `<a>` | no: the later end tag re-parents content already matched | divergence |
| second `<html>`/`<body>` tag | `<body x=1>…<body y=2>` | ignored | attributes merged into the open element | no: retroactive for selectors already evaluated | divergence |

`<select>` content is not a departure. html5lib and parse5 drop a `<div>` written inside a `<select>`,
but Chrome 156 and Firefox 159 keep it since the customisable-`<select>` parser change, and so does
lxml. It is also an example of why the libraries cannot be the oracle.

Foster parenting fixes only part of the lab's `foster` case even with its ancestry right. The fostered
element's parent is known when it opens (the element below the table on the stack), but in the
browser's tree it comes before the table's content while the source puts it after. A single column can
therefore come back in the wrong order, and `p + table` is decided after the table already opened.
There is a way to fix the ordering later, if a crawl shows it matters: give each value a document-order
key, give a fostered subtree a key that sorts before its table, and sort a column only when a foster
actually happened.

Parsing semantics and selector semantics are separate, and browser mode has to keep them apart in
three places:

- **Quirks mode is two features.** The tree half is not optional. Without a doctype, `<p><table>`
  nests where with one it does not, in both browsers, so the document mode has to be computed from the
  doctype (WHATWG's quirks, limited-quirks and no-quirks tables) in Phase 2. The selector half, where
  class and id selectors match case-insensitively in quirks mode, can be deferred.
- **Attribute case folding is CSS-only.** HTML lists about forty attributes whose values CSS matches
  case-insensitively, so `input[type=submit]` matches `type="SUBMIT"` in both browsers, while XPath
  `//input[@type='submit']` does not. The compiled matcher shares one representation for CSS and
  XPath, so the attribute test has to carry which syntax it came from. The list itself is a
  compile-time table.
- **Form owner is not ancestry.** Chrome's `DOMParser` builds the same tree as a page load for a form
  inside a table but does not set the input's form owner. Selectors only see the tree, so this
  matters only to anyone tempted to compare whole documents rather than trees.

## 4. How it fits the engine

Tree rules reach the matcher through `classify` (void flag and start-close class), `start_closes`,
`blocks_end_tag`/`end_tag_discardable`, `frame_content_of`, `data_mode_of` (used by the tokenizer) and
`matcher/frame.rs`. Input normalisation is `lib.rs::normalize`. That boundary fits libxml2, whose
rules are a function of a tag name and the open elements. It does not fit HTML5, whose tree builder
is stateful and steers the tokenizer. So the interface below has to be specified before any Phase 2
rule is written.

### A stateful tree builder, not six functions

- **The tree builder chooses the next tokenizer state.** Today `tokenizer::handle_start` picks the data
  mode from the tag name, through `data_mode_of`, before the matcher sees the token. In HTML5 the
  answer depends on where the tag lands. An SVG `<title>` is an HTML integration point, so its content
  is markup and `<svg><title><b id=x>` holds a real `<b>`, while an HTML `<title>` is RCDATA. The same
  goes for `<style>`, `<script>`, `<textarea>` and `<plaintext>` inside foreign content, for CDATA
  (text only in foreign content), and for `/>` (honoured in foreign content, ignored on a non-void
  HTML element). In browser mode the tokenizer therefore asks the tree builder after every start tag.
  The builder answers with whether the tag was inserted, ignored or reprocessed, its namespace,
  whether it self-closed, and which tokenizer state comes next.
- **Namespaces and integration points are tree-builder state.** The builder tracks the namespace of
  the adjusted current node, the HTML integration points (`foreignObject`, `desc` and `title` in SVG,
  and `annotation-xml` with an HTML encoding in MathML), the MathML text integration points, and the
  start tags that break out of foreign content. The matcher needs the namespace too. An HTML
  `<template>` is itself selectable while its content is outside selector traversal, but an SVG
  `<template>` is an ordinary SVG element whose descendants stay selectable. Template suppression
  keys on namespace and name together, never on the name alone.
- **The parser stack and DOM ancestry are different things.** The matcher reads ancestry off the stack
  of open elements, and under libxml2 the two coincide. Under HTML5 they do not. In
  `<form id=f><div id=d>a</form><p id=after>x</p>`, the `</form>` takes the form off the stack but
  leaves the div open inside it, so `#f #after` matches in a browser. lxml agrees on that value for a
  different reason (libxml2 discards the `</form>` while the div is open), so this is a trap for the
  implementation rather than a divergence today: a browser-mode matcher that read ancestry off the
  stack would lose the match. Foster parenting inserts into an element that is not the current node,
  and the adoption agency moves elements. Browser mode keeps a DOM-parent link for each open element,
  separate from its stack position, and selector matching walks that link.
- **Ignored and reprocessed tokens are builder outcomes.** A `<td>` outside a table, a second
  `<form>`, or a `</p>` that inserts an empty `<p>` is reported to the matcher by the builder, not
  re-derived by it.
- **Monomorphised dispatch.** `Matcher` is generic over the builder, and `Plan` holds one of two
  compiled matchers, so dispatch happens once per document. Lxml mode must come out unchanged within
  jitter on `tools/ab_bench.py`. "No new `OpenElem` fields" stays a performance goal for lxml mode, not
  a constraint on browser mode's correctness model. If the DOM-parent link and the namespace cost
  bytes, browser mode keeps its own element record.

### The rest of the engine

- **A second generated file**, `implied_close/generated_html5.rs`, written by `tools/gen_tree_rules.py`
  from the HTML5 oracle over the same `ELEMENTS` universe. HTML5's rules are not an (open x incoming)
  table, though. They are element categories (special, formatting, the four scope sets, implied end
  tags, void, data modes) plus algorithms that walk the stack. Derive the categories from the oracle,
  write the algorithms from the spec, and audit the algorithms by sequence, because that is where this
  repo's own history says tree bugs live.
- **A separate HTML5 frame module** rather than branches in `frame.rs`. HTML5's frame is simpler (one
  `<html>`, a head that always exists, `<body>` never popped before EOF), and `frame.rs` exists to keep
  libxml2-specific distinctions apart, phantom slots among them, that HTML5 does not have.
- **Synthetic elements** already exist: the frame synthesises `html`/`head`/`body` with no source span.
  An implied `<tbody>` and a reconstructed formatting element reuse that path for matching. Their
  outer HTML has no source span to return, so browser mode refuses it (next subsection).
- **Normalisation:** browser mode skips the strip and NUL deletion. A NUL byte in a tag name then fails
  to match an ASCII selector by itself. Value decoding turns NUL into U+FFFD, or drops it in body text.

### Rescans and node handoffs

Two features reparse raw source outside the pass. Both rest on a libxml2 property that HTML5 does not
have.

- `Matcher::resolve_tail_spans` recovers the values of deferred winners (`:has()`, reverse positions,
  text predicates) by re-scanning each winner's raw source span as a standalone document. Its own
  comment gives the reason this is sound: under libxml2 such a span is self-contained, and the
  differential proves it. Under HTML5 a span is not self-contained. `<td><b>cell</b></td>` keeps its
  cell inside a table, but parsed on its own it loses the `<td>`, so `td b` matches there in Frostwork
  and lxml and in neither browser. Passing browser mode into the rescan does not restore the missing
  context. A synthetic `<tbody>` or a reconstructed formatting element has no source span of its own
  to rescan at all.
- `.as_node()` reparses the raw source through `lxml.html.document_fromstring`. In browser mode a
  processor would receive libxml2's subtree for a node the browser built differently.

Browser mode does not inherit lxml mode's support contract for these. Until context travels with
them, it refuses them explicitly. A selector that needs a tail rescan, the outer HTML of a synthetic
or cloned element, and an `.as_node()` handoff are each `UnsupportedSelector` in browser mode, with a
reason, and `frostwork.check` reports them per mode. Every shape browser mode does support is proven
against the browser oracle first.

There are two ways to lift the refusals later. The rescan can carry its context: what the spec's
fragment-parsing algorithm takes as input, meaning the context element and its namespace, the
insertion mode, the open elements and the form pointer at the span's start. Or the pass can buffer
deferred values instead of rescanning. A browser-mode `.as_node()` would hand over a node built from
the browser-mode engine's own view, never an lxml reparse.

## 5. Oracle and gates

This is the expensive part. `diff_lxml`, `gate-corpus`, `gate-seq`, `audit_tree_rules`,
`gen_tree_rules --check`, `mutate_rules`, `diff_fuzz`, `sel_fuzz` and `gate-webpoet` all grade against
lxml. A browser mode is only as trustworthy as a second oracle for each of them.

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

The driver is small. The harness behind these numbers is `tools/browser_oracle/probe.cjs`, about 360
lines of Node with no packages, covering both protocols and the four measurements above (`fidelity`,
`throughput`, `equiv`, `claims`). `claims.json` holds the parser fixtures behind every "(browser)" cell
in section 3, and `evidence/2026-11-03/` holds the outputs, each stamped with its browser build. It is
a prototype: no gate calls it yet.

### Which browser

The browsers do not agree with each other everywhere. On WPT's `html/syntax/parsing` (wpt.fyi stable
runs, 2026-11-03), Chrome 156 passes 8121 of 8166 tests, Firefox 159 passes 7859 of 8164, and Safari
27.2 passes 7832 of 8158. Most of the gap is one 2026 spec change, processing instructions
(whatwg/html#12118), which Chrome shipped in 150 and the others have not. Firefox also fails the four
`selectedcontent` cases of customisable `<select>`, and Chrome and Safari fail some `<search>` cases.

So browser mode has to name its reference, as autodetection does. Decided on 2026-11-03: **where the
two disagree, Frostwork follows Chrome**, because Chrome is what most users see. Every run still grades
against both Chrome and Firefox. Where they agree, that is the target. Where they disagree, the case
goes in a named Chrome-versus-Firefox table, gated in both directions like `enc_check`'s w3lib table,
so a browser that changes its answer fails as stale rather than passing silently.

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

Each existing gate then gains a browser mode:

- `diff_lxml` gets `--oracle chrome,firefox`: the same generators, with two more verdicts.
- `gate-seq` against the browsers matters most, because insertion-mode bugs are ordering bugs, but it
  has to be strengthened first. Its fingerprint (`seq_sweep.fingerprint_selectors`) reads document
  order, descendant sets and own text per id, and it cannot see an element that carries no id. These
  two tables give identical answers on all 13 of its selectors:
  `<table id="0"><tr id="1"><td id="2">x</td></tr></table>` and the same with a `<tbody>` around the
  row. Run as it is, the gate would pass a browser mode with no implied `<tbody>` at all, one of the
  mode's headline features. Add a child-axis column per id (`//*[@id="0"]/*/@id` gives `["1"]`
  without the `<tbody>` and `[]` with it; the engine supports it today), keep the document-order id
  column duplicate-aware (a reconstructed formatting element copies the original's attributes, id
  included, so a clone shows up as a repeated id), and add a column per name browser mode can
  synthesise. Then seed two regressions into `tests/test_gates.py`, an oracle tree with its `<tbody>`
  removed and one with a formatting clone removed, and require the gate to reject both. Its whole
  sweep is seconds of `DOMParser` time per browser. The same blind spot applies in lxml mode to any
  id-less intermediate element, so lxml mode should get the columns too.
- `gen_tree_rules`, `audit_tree_rules` and `mutate_rules` derive, audit and mutate the HTML5
  categories against the browsers, and the mutation sweep runs per mode.
- `gate-corpus` and the crawl lab get two more oracle columns, through the slow path.
- `tests/test_gates.py` gets a seeded regression for every new gate, the fast/slow self-check
  included, so each one is shown to go red.
- `gate-webpoet` grades browser-mode items through the same split: CSS natively in the browser,
  XPath through the lossless bridge.

Until Phase 2 lands, a browser gate cannot be green on its divergence count. The generators already
emit an implied `<tbody>`, `<p>` closers, `<div/>` and formatting reconstruction, which only Phase 2
implements. So from Phase 1 on, each browser gate attributes every divergence to a named construct
from section 3's table, the way `diff_fuzz` attributes lxml-mode divergences, and fails on NOVEL: any
divergence that no named construct explains. Each Phase 2 rule lands with its bucket deleted, so from
then on the gate holds that construct at zero. Attribute by the smallest input that reproduces a
divergence, not by what the page contains, because page-scoped attribution over-credits (AGENTS.md's
truncated-tag lesson).

CI downloads two pinned browsers (cacheable), and the tree gates roughly double in wall time. The
encoding gates (`enc_check`, the decoder sweep) do not change, because encoding is the same in both
modes.

## 6. Does it complicate the implementation much?

- **Engine: moderate to large.** Phase 1 is small. Phase 2 is a second rule set, closer to a spec than
  the libxml2 one, but it sits behind a stateful builder interface that steers the tokenizer and keeps
  DOM ancestry apart from the stack. The rules that change decisions already made stay out of it,
  and so do context-carrying rescans until they are needed.
- **Gates: large, though the oracle itself is cheap.** The driver is small and the browsers are fast
  enough. The cost is a second verdict in every gate, two pinned browsers in CI, and roughly double the
  tree-gate wall time. The oracle layer is where this repo's correctness comes from, so this is the
  part not to skimp on.
- **Contract: moderate.** COMPATIBILITY.md gains a second tree-construction section with its own
  divergence list. The README's "value-identical to lxml" becomes "lxml by default, browser on
  request".

The main risk is a browser mode that reads as "what Chrome shows" before it is. It ships with its
divergence list and its fuzzer attribution buckets from day one, as lxml mode does, and it ships
**experimental**. A gate that reports no NOVEL divergence is not evidence of readiness, because every
known bucket can still be large. Browser mode leaves experimental when, for the selector shapes it
claims to support, the gates show zero divergences against both pinned browsers on generated pages,
the lab and a crawl sample. Every shape that cannot meet that is refused, not bucketed.

## 7. Phasing

- **Phase 0: encoding, no mode needed.** E1, documenting E2 and E3, and the lab's Firefox column (the
  rest of E4 is done). Small, and E1 is a user-visible fix on its own: through web-poet it recovers 12
  of the lab's 48 cases.
- **Phase 1: the mode exists, and its gates can be trusted.**
  - The oracle harness, grown from `tools/browser_oracle/`: the browser driver, native CSS evaluation,
    the lossless XPath bridge and its round-trip gate, and the fast/slow self-check.
  - The strengthened sequence fingerprint, with seeded `<tbody>` and formatting-clone regressions that
    must go red.
  - Every gate parameterised by mode, starting with one attribution bucket per Phase 2 construct and
    failing only on NOVEL (section 5).
  - The stateful builder interface written down, with the tokenizer asking the builder for its next
    state, even though only a few rules use it yet.
  - The browser-mode refusals for tail rescans, synthetic outer HTML and `.as_node()`.
  - `parser="browser"` published as experimental, with the oracle builds recorded in the evidence.
  - Browser rules at this stage: the NUL rules, no strip, `<noscript>` as raw text and HTML template
    content inert. That fixes six of the lab's seven parse-mode differences (all but `foster`),
    including both bot-poisoning demos.
- **Phase 2: streaming HTML5 tree rules,** the "2" rows in the table above: foreign content and
  integration points, the DOM-parent link, then the insertion-mode rules. Each rule deletes its
  attribution bucket when it lands.
- **Phase 3, only if a crawl calls for it:** foster-parenting order.

## 8. Decisions needed

Decided on 2026-11-03: E2 (unlabelled valid UTF-8 stays UTF-8), E3 (autodetection follows Firefox),
and where Chrome and Firefox build different trees, browser mode follows Chrome. These are open:

1. Oracle: the browsers themselves (`DOMParser` fast path, CSP-served page loads, pinned Chrome for
   Testing and Firefox), with Firefox's Java parser held in reserve? I recommend yes.
2. Browser-mode CSS: include HTML's case-insensitive attribute list now, and defer quirks-mode class and
   id matching (quirks-mode tree construction is not deferrable)? The CSS oracle is native
   `querySelectorAll`, so it applies the list by itself. XPath keeps exact matching.
3. Names: keep `parser="lxml" | "browser"` as the stable public names, with browser builds recorded in
   the evidence rather than in the name? I recommend yes.
4. Browser mode's first release refuses tail rescans, synthetic outer HTML and `.as_node()` rather
   than carrying parse context into them. I recommend yes: refusing is honest and cheap, and
   context-carrying rescans can follow once a schema needs them.
5. Browser-mode XPath: the browser's tree with lxml's XPath semantics, so `//svg` still finds SVG?
   I recommend yes; native `document.evaluate` finds no SVG element at all.
6. Encoding: keep `encoding=` below the BOM, as it is today, and add no BOM-beating override unless
   Adrian's integration needs one? I recommend yes.

## Sources and versions

Measured for this proposal on 2026-11-03, on macOS:

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
