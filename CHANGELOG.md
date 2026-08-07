# Changelog

All notable user-facing changes will be recorded here. Frostwork will follow semantic versioning after its
first public release.

## 0.1.0 — unreleased

First public preview.

### Extraction engine

- Treeless, one-pass extraction for a focused CSS and XPath subset, available from Rust and Python.
- Compile-once `Plan`/`Page` APIs, named fields and grouped `Many`/`One` extraction.
- Strict selector validation by default. Unsupported selectors fail before scanning; `strict=False` opts
  into empty columns without invoking a fallback parser.
- Byte-oriented tokenization, lazy value decoding and browser/WHATWG-oriented charset resolution.
- Extended characters in a selector answer the same under any encoding. A selector is UTF-8 and a
  byte-scanned page is not, so both attribute values and attribute NAMES are decoded before comparison —
  `[data-año]` and `::attr(año)` used to match a UTF-8 page and return nothing for the same document in
  windows-1252 or shift_jis, where lxml matches. A non-ASCII tag name remains unsupported and reported.
- Empirical libxml2-compatible tree construction for the supported surface, including optional document
  frames, implied closes, raw-text modes, void elements and malformed markup covered by the compatibility
  contract.
- CSS `:contains("v")` is supported, with cssselect's semantics: one string/ident argument lowered to
  `contains(., "v")`, resolved at the element's own close. The value may be the element's own, its subtree,
  a descendant's or a following sibling's (`dt:contains("Price") + dd::text`). Shapes outside that tier —
  a second `:contains()` on one compound, one inside `:not()`/`:is()`, or a comma-group member — stay
  unsupported rather than silently dropping the constraint.
- A value terminal with no subject compound after an explicit combinator (`dt + ::text`, `div > ::attr(id)`)
  is supported and answers identically to the `*` spelling, which is how Parsel reads it.

### Python API and tooling

- `frostwork.extract`, declarative `Page`/`Item`, grouped extraction and schema introspection.
- `html` may be `str` as well as `bytes`, so code holding already-decoded text (a browser snapshot) does
  not pre-encode a second copy of the document per response; the engine borrows CPython's UTF-8 view of
  the string. A `str` is scanned as UTF-8, so an `encoding` label naming a different encoding is refused
  rather than silently decoding those bytes wrongly.
- `frostwork.check` and `frostwork-audit` for static schema validation; `frostwork-audit --scan` finds
  selector literals in existing Scrapy code without importing it.
- `frostwork.check` reads a `dict` as `{name: selector}` (and `{name: (container, subfields)}` for groups,
  the shapes `FrostPage.frost_schema()` returns) instead of iterating it. Iterating audited the field
  *names* as selectors, and a bare name like `title` is a valid type selector, so `report.ok` came back
  `True` for a schema that had never been looked at. Any other shape raises `TypeError` naming the
  accepted ones, and `extract`/`extract_grouped` reject a `dict` of queries outright, their columns
  being positional.
- `Item.empty_fields()` distinguishes supported selectors that matched nothing from unsupported coverage
  gaps already rejected by the audit.
- An abi3 wheel targeting CPython 3.10 and newer, which is also the floor for the optional web-poet
  integration. The two used to differ: the core ran on 3.9 while web-poet required 3.10. They converged
  when the wheel moved to `abi3-py310` so the engine could borrow a `str`'s UTF-8 view (below);
  `PyUnicode_AsUTF8AndSize` is limited-API only from 3.10, and Python 3.9 reached end of life in October
  2025.

### web-poet integration

- `FrostPage`, `FrostBrowserPage` and injectable `FrostFields` page bases whose declared selectors share one
  compiled scan per response.
- Real `web_poet.field` descriptors that forward `cached`, `meta` and `out`, plus Frostwork's chainable
  `.map()`, `.re_first()` and `.typed_as()` declaration helpers.
- Explicit processor input contracts for bare-element fields: `.as_value()` passes HTML source and
  `.as_node()` passes a Parsel node. Unconstrained or incompatible node declarations fail at class
  definition instead of guessing.
- Inherited fields, groups and processors resolve through the effective class MRO. `out=[]` explicitly
  declines a processor inherited by field name.
- `Many`/`One` groups, custom response inputs and composition with `Returns[...]`, attrs classes and
  hand-written web-poet fields.
- Supported dependency range: `web-poet >= 0.24.1, < 0.25`.

### Correctness and performance tooling

- Differential checks against Parsel/lxml for values, grouped rows and whole web-poet items.
- Encoding checks, real-page corpus support, malformed-HTML and selector fuzzers, whole-tree tag-sequence
  sweeps, generated tree-rule tables and mutation testing for gate effectiveness.
- Reproducible engine, page-object and memory benchmarks against Parsel. Current measurements and caveats
  live only in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
- The competitive benchmark reports the coverage GAP against the oracle — the columns parsel expresses and
  the engine does not — separately from the engine's own refusal total, which also counts selectors
  cssselect rejects.
- The page-shape matrix refuses to publish a cell that extracted no values, carries a pure-scan row per
  shape, and reports the class-led and attribute-predicate selector pools alongside the tag-led one.
