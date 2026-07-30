# Self-authored fixture corpus

`make gate-corpus` runs the differential gate's own verdict over these pages (this is the default
`CORPUS`). They live under `tests/` and not `fixtures/` because `/fixtures/` is gitignored — that is the
deliberately-untracked local area for real page snapshots, whose licensing and size are the reason no
crawl corpus is vendored.

These are **written for this repo**, not scraped, so there is no third-party licensing question — the
point is to give the corpus gate something to run in CI. They are deliberately shaped like the markup
that broke the engine while the *generated* gate read 100% parity:

- `docgen` — documentation-generator output: stray `</p>` (Sphinx emits `</p>\n</p>`), `<dt>`/`<dt>`
  runs in a definition list, rawtext blocks.
- `tabular` — a table with `<caption>`, `<colgroup><col>` and omitted section/row/cell end tags, which
  is where the missing `colgroup` rule lost cells from child-anchored selectors.
- `catalog` — a listing with a grouped `<select>` (`<optgroup>` runs), ruby annotations, and an
  unbalanced `</div>` inside a table cell (table scope).

A real crawl corpus is still worth adding on top: a generator only reproduces the malformations its
author thought of, and these fixtures are no exception — they encode the bugs we already know about.
