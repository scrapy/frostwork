# python/ — the Frostwork Python package

Pure Python over the `_frostwork` extension: `page.py` (`extract` wrapper + `Page`/`Item`),
`webpoet.py` (`FrostPage`/`field` web-poet integration, soft dep), `audit.py` (`frostwork-audit`
CLI: schema audit + `--scan`), `scan.py` (ast-based selector-literal scan for un-ported source —
inline `.css()`, ItemLoaders, LinkExtractors). `pyproject.toml` = maturin build;
`tests/test_python.py` = pytest suite; `tests/doc_examples.py` holds the page objects the docs show and
`tests/test_doc_examples.py` runs them, because a documented example is untested code otherwise.
