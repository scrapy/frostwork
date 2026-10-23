# python/ — the Frostwork Python package

Pure Python over the `_frostwork` extension: `page.py` (`extract` wrapper + `Page`/`Item`), `webpoet.py`
(`FrostPage`/`field` web-poet integration, soft dep), `encoding.py` (`detect_encoding()` as a whole and
as separate steps, for w3lib; kept free of the page-object layer, which `__init__.py` loads lazily for
that reason), `audit.py` (`frostwork-audit` CLI: schema audit + `--scan`), `scan.py` (ast-based
selector-literal scan for un-ported source — inline `.css()`, ItemLoaders, LinkExtractors).
`pyproject.toml` = maturin build; `tests/test_python.py` = pytest suite; `tests/test_doc_examples.py`
EXECUTES the marked fenced blocks in README.md and docs/PYTHON.md, because a documented example is
untested code otherwise.
