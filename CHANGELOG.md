# Changelog

All notable user-facing changes will be recorded here. Frostwork will follow semantic versioning after its
first public release.

## 0.1.4 (unreleased)

### Correctness

- Sibling selectors retain document-level history across multiple HTML roots. Content after `</html>`
  can now match `html + html` and `html ~ html`, including preceding `:has()`/`:contains()` predicates.
  The minimized malformed-HTML regression is covered by the rule audit and both language suites.
- Python and Rust `Item.get_all()` now consistently respect the field declaration: a single-valued
  `field` returns zero or one raw match, while `field_all` and `field_join` return every raw match.
  Previously, adding an unrelated field could expose additional matches by disabling early exit.
  Declare `field_all` when every match is needed; single-valued schemas retain their early-exit behavior.
- The corpus gate now uses the main differential's verdict without discarding empty or whitespace-only
  values. Regressions that drop an empty attribute or change value count can no longer pass as
  whitespace-only differences. Command-level regression tests cover the verdict, report and exit status.
- CSS class and ID selectors now decode literal and hexadecimal escapes, including utility classes
  such as `.sm\:text-lg`. Escape terminators stay part of the identifier instead of becoming descendant
  combinators. Non-executable character escapes and ambiguous decoded-backslash escapes are refused throughout
  the compiler, including quoted attribute values.
- Grouped subfields support immediate-child XPath (`./h2/text()`), own attributes (`@id`), own text
  (`text()`), subtree text/attributes (`.//text()`, `.//@id`) and context HTML (`.`). Child paths are
  anchored to the actual container; nested descendants cannot satisfy the first child step.
- Grouped extraction and schema audits share input normalization: malformed subfield pairs are
  rejected instead of unpacking a string or mapping key into unrelated selectors. Extraction also
  accepts subfield mappings directly. `Page.many/one` rejects malformed cardinality tuples and
  non-string separators at declaration time instead of ignoring extra arguments.
- `Page` always applies a supplied callable transform, even when that callable has a false boolean
  value. First, all and joined fields use the same rule.
- Saved-response verification uses independent item shaping, so a cardinality bug cannot change both
  the engine result and its reference. It preserves escaped CSS whitespace, handles padded XPath
  consistently and checks types inside tuple-valued transforms.
- Rule-mutation checks propagate failures while restoring both normal builds, including after failed
  setup. Release wheels now exercise the expanded public API suite and run a core smoke check before
  optional dependencies are installed.

### Developer experience

- Documentation starts with reusable `Page` schemas for ordinary Scrapy callbacks and an API chooser.
  URL and grouped-item recipes are self-contained and tested with missing values; migration examples
  are executed from the docs. The compatibility guide distinguishes selector support from value parity.
  Release checks render Markdown and check repository links against heading IDs; Twine skips that step.
- Source audits use the grouped compiler for literal `Page.many/one` and web-poet `Many/One` fields,
  with context-specific diagnostics. Dynamic-only scans report unknown coverage; `--require-complete`
  can fail CI on unresolved sites or empty scans. Full schema audits still check shared budgets.
  Group detection follows field modifier chains; callbacks and unknown factories cannot make an
  unresolved group look complete.
- `Page.extract_response()` uses response bytes and encoding without a Scrapy dependency.
  `Page.frost_schema()` exposes the same named selector format as the web-poet integration.
- `Item.validate()` checks required processed values, retained match counts and required group
  subfields. Reports distinguish absent matches, empty matches, empty processed values and transform
  failures, and can record Scrapy stats. `FieldProcessingError` adds field/selector context while
  preserving the original exception. An empty row still counts as a matched group container.
- The migration workflow now includes complete-item verification on hashed saved responses, an
  inspectable fixture manifest, parity-gated timing reports and a manual Linux benchmark workflow.
  Historical benchmark ratios are no longer described as a guaranteed performance floor.
- Default pytest discovery is scoped to `tests/`, so Cargo package snapshots under `target/` cannot
  be collected as duplicate test modules.

### Performance

- Immediate first-value text and attribute fields stop retaining later matches even when an all-value
  field or group requires a complete scan. Deferred predicates, normalization and raw-HTML captures keep
  their existing ordering and retention rules. The A/B harness now includes a mixed-cardinality workload;
  measurement details and limitations are in `docs/BENCHMARKS.md`.
- `Page` and `Item` share stable field definitions instead of copying parallel metadata lists on each
  response. Named item access uses direct lookups, and required-field validation uses set membership.
  Extending a page preserves earlier items' schemas. `Page.one()` shapes only the first matched row;
  the engine still scans and collects grouped matches as before.

## 0.1.3 (2026-09-02)

### Packaging and documentation

- Includes all 0.1.2 and 0.1.1 changes below. Neither version was uploaded to PyPI: the 0.1.2
  pre-upload smoke test exposed locale-dependent file decoding in the release checker on Windows. The
  checker now reads repository text explicitly as UTF-8 and can report Unicode diagnostics safely on a
  cp1252 console.

## 0.1.2 (2026-09-01)

### Packaging and documentation

- Includes all 0.1.1 changes below. Version 0.1.1 was never uploaded to PyPI: its immutable tag exposed
  a tag-context bug in the release-check test before artifact building began. The test now applies the
  dated-changelog rule on release tags and the unreleased rule on branches, matching the checker CLI.

## 0.1.1 (2026-09-01)

### Packaging and documentation

- The PyPI description now leads with the published install commands, states the Python 3.10 floor, and
  uses absolute repository links so its documentation, examples, changelog and license do not resolve
  under `pypi.org/project/frostwork/` and return 404.
- Releases now validate the built metadata and rendered README, rerun the complete correctness workflow,
  verify the package from the public index, and create a GitHub Release only after PyPI verification.

### Performance

- Reverse-position selectors (`:last-child`, `:only-*`, `:nth-last-*`, XPath `[last()]`) no longer copy a
  candidate's values on the way to the output. Each sibling's captured values are moved to the parent and
  then into the column, rather than copied at both steps, which is work proportional to sibling count and
  not to matches. Measured 13-17% faster on a reverse-position workload; no other selector shape is
  affected. `docs/BENCHMARKS.md` now publishes that workload as its own row, so a regression on it shows
  up in the canonical table.

## 0.1.0 (2026-08-19)

First public preview.

### Extraction engine

- Treeless, one-pass extraction for a focused CSS and XPath subset, available from Rust and Python.
- Compile-once `Plan`/`Page` APIs, named fields and grouped `Many`/`One` extraction.
- Strict selector validation by default. Unsupported selectors fail before scanning; `strict=False` opts
  into empty columns without invoking a fallback parser.
- Byte-oriented tokenization, lazy value decoding and browser-oriented charset resolution. **On
  encoding the standard is the browser** — not w3lib, lxml, Parsel or a reading of a spec — because a
  scraped value is correct when it matches what the site shows a person. Every difference from those
  libraries is tabulated with its reason in the compatibility contract and asserted in both directions,
  so an upstream fix fails as stale rather than passing silently.
- The `<meta charset>` prescan is bounded the way a **browser** bounds it, not the way w3lib does.
  A declaration in the `<head>` is honoured at any depth (Chrome measured at 1KB→1MB: a browser that
  meets the tag after its prescan budget re-decodes, so the budget is not a correctness cap); a
  declaration in the `<body>` counts only within the first 1024 bytes (Chrome honours one at byte
  0/100/512 and ignores it from 1024 on). The previous flat 4096-byte cap was w3lib's number and was
  wrong in both directions — it dropped head declarations real pages carry behind a producer comment
  or a block of `og:` metas, and honoured body declarations no browser does.
- Extended characters in a selector answer the same under any encoding. A selector is UTF-8 and a
  byte-scanned page is not, so both attribute values and attribute NAMES are decoded before comparison:
  `[data-año]` and `::attr(año)` answer identically for the same document in UTF-8, windows-1252 or
  shift_jis. A non-ASCII tag name is unsupported and reported.
- Deferred fields whose value comes from a subtree (`:has()`, `:last-child`, XPath text predicates) are
  resolved by re-scanning each winner's span. Fields deferring on the same compound share one sub-schema,
  so that span is re-scanned once for all of them rather than once per field.
- Empirical libxml2-compatible tree construction for the supported surface, including optional document
  frames, implied closes, raw-text modes, void elements and malformed markup covered by the compatibility
  contract.
- CSS `:contains("v")` is supported, with cssselect's semantics: one string/ident argument lowered to
  `contains(., "v")`, resolved at the element's own close. The value may be the element's own, its subtree,
  a descendant's or a following sibling's (`dt:contains("Price") + dd::text`). A comma group may mix it
  with ordinary members (`h2::text, p:contains("x")::text`) when the value is the element's own and every
  member names one kind of node; the column is merged by document offset and deduped by node, so member
  order does not change it. Shapes outside that tier —
  a second `:contains()` on one compound, one inside `:not()`/`:is()`, or a comma group whose members
  name different nodes — stay
  unsupported rather than silently dropping the constraint.
- A value terminal with no subject compound after an explicit combinator (`dt + ::text`, `div > ::attr(id)`)
  is supported and answers identically to the `*` spelling, which is how Parsel reads it.
- Three constructs that are valid CSS and that cssselect rejects outright are supported, each oracled
  against a spelling parsel can evaluate: a `:has()` relative selector LIST (`div:has(a, img)`, the union
  of its members), a `:not()` compound LIST (`p:not(.a, .b)`, the chained `:not(.a):not(.b)`), and the
  Selectors 4 case-sensitivity flag (`[type=submit i]`, an ASCII fold on every operator). Each has its
  own refusal: a `:has()` list mixing relative combinators, an empty `:not()` member and a bogus flag
  letter stay unsupported and reported.
- A `Page` whose fields are all single-valued STOPS scanning once every field has a value, instead of
  running to the end of the document. The values skipped are the ones a single-valued consumer discards,
  so the item is identical to a full scan's; a multi-valued or joined field, a group, or a deferred
  selector turns it off. `extract`/`extract_grouped` never arm it — their contract is every value.

### Python API and tooling

- `frostwork.extract`, declarative `Page`/`Item`, grouped extraction and schema introspection.
- `html` may be `str` as well as `bytes`, so code holding already-decoded text (a browser snapshot) does
  not pre-encode a second copy of the document per response; the engine borrows CPython's UTF-8 view of
  the string. A `str` is scanned as UTF-8, so an `encoding` label naming a different encoding is refused
  rather than silently decoding those bytes wrongly.
- `frostwork.detect_encoding(html, encoding=None)` reports the encoding a scan would use, as a WHATWG
  name — the BOM/prescan resolution on its own, without extracting. Parsel cannot answer it (it never
  sniffs `<meta charset>`) and w3lib answers differently in the places the compatibility contract
  enumerates.
- `frostwork.check` and `frostwork-audit` for static schema validation; `frostwork-audit --scan` finds
  selector literals in existing Scrapy code without importing it.
- `frostwork.check` reads a `dict` as `{name: selector}` (and `{name: (container, subfields)}` for groups,
  the shapes `FrostPage.frost_schema()` returns) rather than iterating it, since iterating a dict would
  audit the field *names* as selectors and a bare name like `title` is a valid type selector. Any other
  shape raises `TypeError` naming the accepted ones, and `extract`/`extract_grouped` reject a `dict` of
  queries outright, their columns being positional.
- `Item.empty_fields()` distinguishes supported selectors that matched nothing from unsupported coverage
  gaps already rejected by the audit.
- An abi3 wheel targeting CPython 3.10 and newer, which is also the floor for the optional web-poet
  integration. `abi3-py310` is what lets the engine borrow a `str`'s UTF-8 view (below), since
  `PyUnicode_AsUTF8AndSize` is limited-API only from 3.10; Python 3.9 reached end of life in October
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
  cssselect rejects, and separately again from the REVERSE gap: the columns the engine expresses and
  cssselect rejects. Expressible% alone is scored over a column set the oracle defines, so it can only
  ever show a deficit.
- The page-shape matrix refuses to publish a cell that extracted no values, carries a pure-scan row per
  shape, and reports the class-led and attribute-predicate selector pools alongside the tag-led one.
