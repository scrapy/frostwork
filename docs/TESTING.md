# Testing: how Frostwork's correctness is proven

Frostwork is a "close to lxml, no fallback" engine, so **the test harness is the spec**: there is no
fallback to hide behind, and every shipped divergence must be one we chose and can name. The suite
answers one question at scale — *on which inputs and selectors does the engine's value differ from
lxml, and is that difference a documented divergence or a bug?*

## Oracle and verdict

- **Oracle:** Parsel/lxml (`parsel.Selector(...).css(q).getall()`), the exact engine Scrapy ships,
  pinned to `parsel==1.11.0`.
- **Oracle version matters — "byte-identical to lxml" means libxml2 ≥ 2.14.** Tree construction is
  matched empirically to libxml2 2.14 (see [COMPATIBILITY.md](COMPATIBILITY.md)), and
  `requirements-test.txt` cannot pin that: a wheel *vendors* its own libxml2, and the same lxml release
  ships different ones per platform — lxml 6.1.1 carries libxml2 2.14.x in its manylinux/macOS wheels
  and **2.11.9** in the Windows wheel, where CR-in-attribute-values and a raw `<` in text parse
  differently. The same engine build therefore measures 0 DIVERGE on Linux/macOS and thousands on
  Windows purely from the oracle. `tools/oracle.py` asserts `lxml.etree.LIBXML_VERSION >= (2, 14)` and
  every harness entry point (`diff_lxml.py`, `enc_check.py`, both fuzzers) exits `2` with that
  explanation rather than reporting divergences the engine is not accountable for; pass
  `--allow-old-libxml2` (or set `FROSTWORK_ALLOW_OLD_LIBXML2=1`) to explore on such a platform anyway.
  The same caveat applies to diffing Frostwork against a *crawl* whose lxml is older.
- **Bar:** non-whitespace byte-identity per emitted value.
- **Verdict per (input, selector):** `AGREE` · `WS` (equal after `.strip()`) · `SKIP-EXPECTED`
  (diverges on a documented tree-construction construct — foster-parenting, adoption agency, deep-`<p>`)
  · `DIVERGE` (any other difference — a bug) · `CRASH` (engine panics — a bug).

The gate is **`DIVERGE + CRASH == 0`**, with `SKIP-EXPECTED` reported as the measured distance from lxml.

## Layers

**Unit vectors — `cargo test`** (109 vectors, milliseconds). One per rule and edge: every implied-close
family (`li`/`p`/`td`/`tr`/`dt`/`dd`/`option`) × {closed, omitted}; void/self-closing; rawtext with
`<`/`&` inside; comments/CDATA/DOCTYPE; entity edges; attribute quoting/case; selector compounds,
combinators, `:not()`, comma groups; encoding resolution; XPath compilation; the `Page` layer; budget
safety; and a regression vector for every bug the fuzzers have found.

**Tokenizer conformance** (`src/tokenizer.rs`, `cargo test tokenizer`). The states that would cause a
*global* offset desync must be exact. A recording `TokenSink` pins the event stream for each: RAWTEXT
(`<script>` keeping `</b>` as text), RCDATA (`<title>`), comment close boundaries (incl. `<!-->` /
`--!>`), CDATA emitting nothing, character references left raw for the matcher, attribute forms, and
`<`-not-a-tag staying literal.

**Differential vs lxml — `tools/diff_lxml.py`** (the core gate). Generators emit structurally diverse
HTML; every page runs a selector basket through the engine (via the `differ` binary) and Parsel, and
each pair gets a verdict:
- **conformant** (`tools/conformant.py`) — random *content-model-valid* trees. On such input the
  corrected stack equals lxml's tree, so this must be byte-identical (the safety invariant).
- **families** (`tools/families.py`) — optional-end-tag constructs tagged SHOULD/SKIP/CONTROL, so a
  divergence auto-classifies as bug vs documented SKIP.
- **foreign** (`tools/foreign.py`) — `<svg>`/`<math>`/`<template>` subtrees (self-closing leaves,
  camelCase names, rawtext in foreign content).
- **grouped** — single-pass `Many`/`One` vs Parsel's per-container loop.

**Encoding parity — `tools/enc_check.py`.** 35 (encoding × selector) cases across windows-1252,
shift_jis, euc-jp, gbk, big5, koi8-r, utf-8, plus UTF-16 sniffing, vs Parsel given the same label.

**Differential fuzzing** (the malformed-input surface):
- `tools/diff_fuzz.py` — mutates conformant/foreign pages and the fuzz corpus into *malformed* HTML,
  then diffs against lxml. `DIVERGE` is expected here (documented SKIP set) and reported/clustered;
  `CRASH` is gated. Catches tokenizer desync the well-formed gate can't reach.
- `tools/sel_fuzz.py` — fuzzes the *query* (valid / exotic / malformed / budget-bomb) against real
  pages and asks the real compiler whether each selector is supported. A promised-supported selector
  may not lose oracle values, and an unsupported selector may not emit anything. Gates `WRONG` +
  `OVERMATCH` + `CRASH`.

**Multi-million soak — `make soak`.** Runs the clean differential and support-aware selector fuzzer
over five independent seeds, followed by a larger malformed-input crash run. The default workload is
over four million page/query pairs and reports the exact aggregate before returning success.

**Coverage-guided fuzzing — `fuzz/`.** A `cargo-fuzz`/libfuzzer target feeds arbitrary bytes to
`extract` (flat, grouped, sniffed + explicit encoding) and asserts no panic/hang/OOB. Run:
`cargo +nightly fuzz run extract`. The corpus (`fuzz/corpus/`) and artifacts are git-ignored and
regenerated locally.

## Wiring

The engine builds a `differ` binary that reads hex-framed cases and emits NDJSON, so the Python
generators/oracle (which own lxml) drive it without a PyO3 build. CI runs the unit tests, clippy, the
differential gate, encoding parity, both differential fuzzers (crash-gated), and the Python suite on
every change; the coverage-guided `cargo-fuzz` target is run locally/nightly.

**The abi3 floor is a separate job.** The wheel is `abi3-py39`, but the pinned toolchain cannot be
installed on Python 3.9 at all — `parsel`, `web-poet` *and* `pytest` each require ≥ 3.10 — so the floor
runs `tools/abi3_smoke.py` instead: build the extension on a real 3.9 and exercise the dependency-free
surface (primitive, strict/permissive, `Page`/`Item` + groups, `check`, `--scan`) with nothing but the
standard library. Parity and the web-poet integration are covered by the jobs that own the oracle.
`tools/abi3_smoke.py` is also the quickest local answer to "does this build work on this interpreter".

## Python

`tests/test_python.py` (`.venv/bin/python -m pytest tests/test_python.py`) covers the primitive,
`Page`/`Item`, the web-poet wiring (including that a multi-field page object triggers exactly **one**
`extract` call), `Many`/`One`, and a Parsel cross-check. The cross-checks go through `_oracle()`, which
skips them unless the installed lxml carries libxml2 ≥ 2.14 — same rule as the harness, so an
environment with an older vendored libxml2 reports a skip rather than a spurious failure.
