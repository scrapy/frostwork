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
- `frostwork-audit --scan FILE|DIR` audits selector *literals* mined from Python source with `ast`
  (never importing it), covering inline `.css()`/`.xpath()`, `ItemLoader.add_*` and
  `LinkExtractor(restrict_*)` — code with no Frostwork schema yet. Reports `file:line` per site and
  flags run-time-built selectors as skipped.
- `Item.empty_fields()` lists the declared fields that matched nothing, so an audited-supported schema
  can tell a dead selector (the page changed) from a coverage gap.
- The differential harness now asserts the oracle's **vendored libxml2 is ≥ 2.14** (a pinned `lxml`
  does not pin it: the lxml 6.1.1 Windows wheel carries 2.11.9 and manufactures thousands of
  divergences). `--allow-old-libxml2` / `FROSTWORK_ALLOW_OLD_LIBXML2=1` downgrades it to a warning.
