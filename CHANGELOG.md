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
- Deferred fields whose value comes from a subtree (`:has()`, `:last-child`, XPath text predicates) are
  resolved by re-scanning each winner's span. Fields deferring on the same compound now share one
  sub-schema, so that span is re-scanned once for all of them rather than once per field.
- Empirical libxml2-compatible tree construction for the supported surface, including optional document
  frames, implied closes, raw-text modes, void elements and malformed markup covered by the compatibility
  contract.

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
