# Changelog

All notable user-facing changes will be recorded here. Frostwork follows semantic versioning once
the first public package is released.

## 0.1.0 — unreleased

- Treeless one-pass CSS/XPath extraction core with Rust and Python APIs.
- Declarative `Page`, grouped `Many`/`One`, web-poet integration, and schema audit CLI.
- Python extraction fails fast on unsupported selectors by default; `strict=False` enables the
  engine's permissive empty-result mode explicitly.
- Differential, encoding, selector, malformed-input, memory, and throughput harnesses.
- **Fix:** an XPath comparison against a non-literal operand is now unsupported instead of silently
  byte-comparing the raw text. A variable reference (`//*[@id=$pid]`, as `parsel` binds at call time)
  was reported *supported* by the audit and then matched an element whose id literally was `$pid`; a
  numeric (`[@a=2]`, numeric semantics in XPath) or bare-name (`[@a=b]`, a node-set compare) operand
  could likewise return a wrong value. All now yield an empty column with an explaining audit reason.
- **Fix (same family, CSS side):** an unquoted CSS attribute value must be a CSS identifier. `[a=2]`,
  `[href^=/p]`, `[a=$v]` and `[a=--v]` are syntax errors in cssselect, but Frostwork answered them with
  values — a non-empty column for a selector Parsel refuses. They are now unsupported (empty); quoting
  the value (`[a="2"]`) is the supported form, and the selector fuzzer now generates both.
- **Fix (audit input shapes):** `frostwork.check` now reads a `dict` as `{name: selector}` (and
  `{name: (container, subfields)}` for groups — the shapes `FrostPage.frost_schema()` returns) instead
  of iterating it. Iterating audited the field *names* as selectors, and a bare name like `title` is a
  valid type selector, so `report.ok` came back `True` for a schema that was never looked at. Sub-fields
  may now be a `dict` in the bare `(container, subfields)` group shape too, and any other shape raises
  `TypeError` naming the accepted ones. `extract`/`extract_grouped` likewise reject a `dict` of queries
  (their columns are positional) rather than extracting the names as selectors.
- `frostwork-audit --scan FILE|DIR` audits selector *literals* mined from Python source with `ast`
  (never importing it), covering inline `.css()`/`.xpath()`, `ItemLoader.add_*` and
  `LinkExtractor(restrict_*)` — code with no Frostwork schema yet. Reports `file:line` per site and
  flags run-time-built selectors as skipped.
- `Item.empty_fields()` lists the declared fields that matched nothing, so an audited-supported schema
  can tell a dead selector (the page changed) from a coverage gap.
- The differential harness now asserts the oracle's **vendored libxml2 is ≥ 2.14** (a pinned `lxml`
  does not pin it: the lxml 6.1.1 Windows wheel carries 2.11.9 and manufactures thousands of
  divergences). `--allow-old-libxml2` / `FROSTWORK_ALLOW_OLD_LIBXML2=1` downgrades it to a warning.
- **Fix:** the remaining libxml2 **data modes**. `iframe`, `noembed` and the obsolete `xmp` are raw text
  and `plaintext` runs to end of document, so their content is no longer tokenized as markup:
  `<iframe><div>fake</div></iframe>` matched `div::text` and desynchronized every offset after it.
  `script` additionally gets the escaped/double-escaped states, so a nested `<script>…</script>` inside a
  legacy `<!-- … -->` wrapper cannot end the outer script early.
- **Fix:** libxml2's start-close NAME-pair relation is now **generated from the oracle** over the whole
  element universe instead of a hand-picked list, which closed the last missing rows and columns:
  `<body>` closes an open `<head>` (so `<html><head><title>T</title><body><p>X</p>` no longer nests the
  entire body inside the head), an open `<listing>` behaves like an open `<pre>`, and `<listing>`/`<xmp>`/
  `<title>` close an open `<p>`.
- **Fix:** `<html>`/`<head>`/`<body>` are accepted only as the document frame and ignored elsewhere, so a
  stray one no longer inserts a second element or splits the text node around it.
- **Fix:** the void set gains the HTML4-only `basefont`, `frame` and `isindex`, which libxml2 treats as
  empty — they used to hold children, nesting everything after them a level too deep.
- **Fix:** raw NUL is deleted from the whole document before tokenizing (as Parsel/w3lib do) rather than
  only from emitted values, so a NUL inside a tag or attribute *name* no longer changes the tree:
  `<di\0v>X</di\0v>` now matches `div::text`.
- **Fix:** functional-pseudo arguments are parsed quote- and escape-aware, so a `)` or `,` inside a
  quoted attribute value is data: `div:is(#outer, [data-x=")"])` is valid CSS that Parsel answers and was
  reported unsupported. Genuinely unterminated syntax still fails closed.
- **Encoding:** XML-declaration compatibility (`<?xml … encoding=…?>` at offset 0), BOM-less UTF-16
  detection from the XML prefix, and a meta-declared `x-user-defined` resolved to windows-1252. The
  intended policy — browser/WHATWG correctness rather than unconditional w3lib parity — is now documented
  as a table of named differences in `docs/COMPATIBILITY.md`, and `tools/enc_check.py` asserts each one
  in both directions instead of reporting it as a mismatch.

### Found by a 1000-page Common Crawl sample

The first differential run over a real crawl sample rather than generated pages. Three of these are
things no generator here would have emitted, and one was a false positive the engine had been reporting
since the tokenizer was written.

- **Fix:** a **tag name ends only at whitespace, `>` or `/`** — every other byte belongs to it, which is
  what libxml2's `htmlParseHTMLName` does. The name used to end at the first byte outside
  `[A-Za-z0-9_:-]`, so `<p<mip-img …>` (twelve times on one crawled page) became a `<p>` with an odd
  attribute instead of an element named `p<mip-img`, and `p::text` returned a value that is **not in the
  document**. A false positive is the one outcome the no-fallback rule exists to prevent. The tokenizer
  already had the rule in `find_raw_end`, so its two name scanners had disagreed with each other.
- **Fix:** the `<meta charset>` **prescan window is 4096 bytes**, matching w3lib. WHATWG's 1024 is a
  *streaming* budget — "encouraged to … prescan the first 1024 bytes, but not to stall beyond that" —
  and nothing stalls here, because the whole document is already in memory. Three sampled pages put
  their `Content-Type` at byte 1080/1532/1611 behind a producer comment or a block of `og:` metas and
  decoded as UTF-8 with a U+FFFD in every value; both oracles read them correctly. This removes a row
  from the deliberate-difference table rather than adding one.
- **Gate:** the legacy CJK decoder comparison is **exhaustive** instead of sampled. It used to check 800
  assigned characters per label and conclude shift_jis / euc-jp / euc-kr / gb18030 were at full parity;
  they are not. An EUC-JP wiki containing the byte pair `A1 C1` — the JIS wave dash, U+FF5E to WHATWG
  and every browser, U+301C to Python's `euc_jp` — disproved it. Every label is now enumerated over
  every assigned two-byte sequence and gated in both directions: euc-jp differs on 6, gb18030 on 20 (all
  of them Parsel returning a pre-2005 private-use code point where the engine returns the real
  character), euc-kr and shift_jis on none.
- **Fix:** the **document frame is synthesized** when the page omits it. `<html>`, `<head>` and `<body>`
  all have optional start *and* end tags, so `<!DOCTYPE html><title>T</title><h1>a</h1><p>b</p>` is a
  conformant document with no frame in the byte stream — and libxml2 builds one anyway. The engine built
  nothing, so `body h1::text` was empty, `h1 + p::text` was empty (top-level elements had no shared
  parent, so they were not siblings), root-level text was dropped, and a `</body>` on a fragment matched
  nothing and coalesced the text around it. This was the largest entry on the divergence list; it is now
  in the supported set. Which part a bare element opens is derived from the oracle over the whole element
  universe (`implied_close::frame_content`, audited as `document-frame synthesis` — 572 cells, and 287
  disagreements against the pre-fix build), because it is not the relation it resembles: only six names
  open a `<head>`, while `input`/`noscript`/`template`/`basefont`/`bgsound`/`object` survive inside one
  already open and open none. `<frameset>`/`<frame>`/`<noframes>` open neither and have no body at all.
- **Fix:** a **`<!DOCTYPE …>` no longer breaks a text node**, and it is the only declaration form that
  does not — `<!foo>`, `<![CDATA[…]]>`, `<?x?>` and `<!>` all split the run in libxml2 too. Measured
  rather than assumed, including that libxml2 matches the seven-letter prefix without requiring the name
  to be terminated (`<!doctypex>` is a doctype, `<!doctyp>` is not).
- **Fix:** whatever **ends the head starts the body**. The first thing in `<head>` that does not belong
  there — a `<div>`, an `<a>`, a non-whitespace character — implicitly ends the head, and libxml2 puts it
  and everything after it (including the remaining `<meta>`/`<link>`/`<title>`) in `<body>`. The engine
  closed the head correctly and then had nowhere to put the content, so it landed under `<html>` and
  `head + body::text`, `html > body::text` and `a:first-child` all disagreed — in both directions, since
  the relocation both adds elements to the body and removes them from the head. Character data splits at
  the first non-space byte, leaving the leading whitespace in the head. This one is a fix rather than a
  divergence because **html5lib, the HTML5 spec reference implementation, places the content exactly
  where libxml2 does** — on every shape and on the four crawled pages that surfaced it. Derived over the
  whole element universe (`implied <body>` in `tools/audit_tree_rules.py`, 695 cells), because the names
  that end the head are not the names that would open a body after an *explicit* `</head>` — and that
  explicit path is deliberately untouched, since libxml2 and html5lib disagree there.
- The **web-poet integration** ships with these contracts, each gated (`make gate-webpoet`,
  `make gate-webpoet-mutate`):
  - A field processor receives **the element the selector matched** — re-parsed from its own raw source, for
    every element name including the document frame. The handoff is subtree-local: own attributes and
    descendants, no ancestors/siblings/`base_url` (a `(value, page)` processor reads document context off
    `page` and is unaffected). Anything that is not one recoverable element raises, naming the field.
  - `out=[]` declines a processor a base page attaches by field NAME, exactly as web-poet defines it, and
    the field then yields its plain terminal value.
  - Composition order on a field is fixed: cardinality (`all`/`join`), then `.map()`/`.re_first()` on the
    HTML source, then web-poet's processors.
  - The schema is resolved against the class's **MRO**, so replacing an inherited field — with a
    hand-written `@web_poet.field`, a flat field over a group, a mixin — drops the inherited selector from
    the plan and from `frost_schema()`, at any depth.
  - `strict=False` survives a class rebuild (`@attrs.define`), and `Many`/`One` refuse web-poet keywords on
    a subfield rather than dropping them silently.
  - `FrostFields` is a `web_poet.ItemPage`, so a custom-input page object is injectable: scrapy-poet/andi
    silently omits a callback argument whose class `is_injectable()` rejects.
  - Field types are checked with mypy against `tests/typing_fixture.py`, including the explicit
    `all=False`/`join=None` forms and `.typed_as(T)` for a processor-bearing field, whose value type nothing
    static can infer.
- **Contract:** the `webpoet` extra requires `web-poet>=0.24.1` — the version it is tested against — and
  therefore **Python ≥ 3.10** for that extra only; the core stays ≥ 3.9.
- **Contract:** two sampled behaviours were already-known divergences whose documented scope was too
  narrow, and `docs/COMPATIBILITY.md` now says what the sample showed. Content after `</html>` is kept
  (28 of the sample's divergences, and the tag is sometimes *misplaced* rather than trailing — one page
  puts 14 of its 17 KB after it, which libxml2 discards); and lxml HTML-escapes raw-text content when
  serializing, so its outer HTML does not round-trip where the raw source does. The remaining entry —
  no `<html>`/`<body>` synthesis — is unchanged but now says what it costs: a page whose first bytes are
  character data (three stacked UTF-8 BOMs, where only the first is the BOM) makes libxml2 put the whole
  document in `<body>`, `<head>` and all.
