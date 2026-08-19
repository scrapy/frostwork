# Changelog

All notable user-facing changes will be recorded here. Frostwork follows semantic versioning once
the first public package is released.

## 0.1.0 (unreleased)

- Treeless one-pass CSS/XPath extraction core with Rust and Python APIs.
- Declarative `Page`, grouped `Many`/`One`, web-poet integration, and schema audit CLI.
- Python extraction fails fast on unsupported selectors by default; `strict=False` enables the
  engine's permissive empty-result mode explicitly.
- Differential, encoding, selector, malformed-input, memory, and throughput harnesses.
