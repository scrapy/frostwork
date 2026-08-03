# Frostwork

Fast HTML extraction for Python and Rust, without a DOM.

Frostwork runs a set of CSS or XPath selectors in one streaming pass over an HTML response. It does not
build a document tree, and one pass answers every ordinary field. (A deferred predicate whose value lives
in a subtree — `div:has(a) a::attr(href)` — re-reads just that element's span afterwards; see
[docs/DESIGN.md](docs/DESIGN.md).) Every speedup figure depends
on the workload, so each one below names its own; [docs/BENCHMARKS.md](docs/BENCHMARKS.md) is the
canonical source and the only place they are maintained:

- **12–20×** faster than Parsel on realistic synthetic pages at typical field counts, up to **40–54×**
  on selector-rich schemas (the Rust benchmark matrix).
- **10.5×** median (12.4× aggregate) on Zyte's production-selector corpus — 788 real pages, each with
  its own production selectors, through the Python binding.

Working memory stays small because Frostwork retains parser state and pending matches, not a tree
containing the whole page.

Python is a first-class API. The bindings provide a low-level `extract()` function, declarative
`Page` objects, and a `FrostPage` integration for web-poet and scrapy-poet. Frostwork is designed to
fit into Parsel/Scrapy projects: it accepts familiar selectors and tests its supported results
directly against Parsel/lxml. The engine itself is written in Rust and can also be used as a Rust
library.

Frostwork deliberately supports a focused subset of CSS and XPath. Python fails fast on unsupported
selectors by default, before scanning any HTML. Pass `strict=False` to opt into empty results; neither
mode falls back to another parser or returns a plausible wrong result. See the
[selector contract](docs/COMPATIBILITY.md) and
[benchmark results](docs/BENCHMARKS.md) for the precise boundaries and measurements.



## Why

DOM parsers are the right tool when callers need to navigate, modify, or query a document in ways
that are not known up front. A scraper usually has a fixed schema and keeps only a few strings or
attributes. Building a tree for that job creates work and allocations that the scraper never uses.

Frostwork instead compiles the schema, scans the response bytes once, and emits only the requested
values. The scan is shared by every field. Its corrected open-element stack reproduces the relevant
libxml2 2.14 behaviour—such as implied closing tags—without retaining the DOM.

This approach is a particularly good fit for:

- Scrapy page objects with several fields;
- repeated extraction with the same schema;
- large responses where DOM allocation is expensive; and
- services where throughput and predictable working memory matter.

If an application needs arbitrary DOM access, use lxml. If it knows the extraction schema in
advance, Frostwork can avoid building data it will immediately discard.

## Python quick start

Until packages are published, install the bindings from the repository. Building the extension needs
a [Rust toolchain](https://rustup.rs) (stable) alongside Python:

```bash
python -m venv .venv
.venv/bin/pip install maturin
.venv/bin/maturin develop --release   # compiles the Rust core; needs cargo/rustc on PATH
```

The primitive API accepts the response body and all selectors together:

```python
from frostwork import extract

html = b"<h1>Widget</h1><span class=price>$9</span><a href=/p/1>buy</a>"
title, price, link = extract(html, [
    "h1::text",
    ".price::text",
    "a::attr(href)",
])

assert title == ["Widget"]
assert price == ["$9"]
assert link == ["/p/1"]
```

All three selectors are evaluated during the same scan. `frostwork.Page` adds names and field
cardinality for applications that do not use web-poet.

### Scrapy and web-poet

With web-poet, a page object declares its selectors and Frostwork fills every field in one pass:

```python
from web_poet import handle_urls, Returns
from frostwork.webpoet import FrostPage, field

@handle_urls("example.com")
class ProductPage(FrostPage, Returns[Product]):
    name = field("h1::text")
    price = field(".price::text")
    images = field("img::attr(src)", all=True)
    brand = field("//meta[@itemprop='brand']/@content")
```

In a Scrapy spider, scrapy-poet injects the page object selected by `@handle_urls`. Calling
`to_item()` fills the whole `Product` in one scan:

```python
import scrapy

class ProductSpider(scrapy.Spider):
    name = "products"
    start_urls = ["https://example.com/p/1"]

    async def parse(self, response, page: ProductPage):
        yield await page.to_item()
```

Enable scrapy-poet in `settings.py` with `ADDONS = {"scrapy_poet.Addon": 300}` and it supplies the
page object according to `@handle_urls`. Outside Scrapy, construct it directly:
`item = await ProductPage(response=http_response).to_item()`.

`extract(..., encoding="windows-1252")` accepts the charset label supplied by Scrapy's response;
without one, Frostwork checks the BOM and `<meta>` declarations before defaulting to UTF-8. The
[Python guide](docs/PYTHON.md) covers `Page`, nested groups, schema auditing, and installation.

## Rust quick start

The Rust API exposes the same compiled, one-pass engine:

```rust
let html: &[u8] = b"<h1>Widget</h1><span class=price>$9</span><a href=/p/1>buy</a>";
let queries = vec![
    "h1::text".to_string(),
    ".price::text".to_string(),
    "a::attr(href)".to_string(),
];

let columns = frostwork::extract(html, &queries, None);
assert_eq!(columns[0], vec!["Widget"]);
assert_eq!(columns[1], vec!["$9"]);
assert_eq!(columns[2], vec!["/p/1"]);
```

For named fields, cardinality, nested groups, and compile-once reuse, use `Page` or `Plan`. See the
[runnable Rust example](examples/basic.rs) and [design notes](docs/DESIGN.md).

## Supported selectors

The common scraping selectors are covered:

- **CSS:** tags, IDs, classes, attribute operators, descendant/child/sibling combinators,
  `:not()`, `:is()`, `:where()`, subject `:has()`, and forward or reverse structural positions such
  as `:nth-child()` and `:last-of-type`.
- **XPath:** downward paths, attribute and text predicates, unions, positional predicates,
  `following-sibling::`, `ancestor::`, `parent::`, and top-level `normalize-space()`.
- **Values:** text, attributes, descendant attributes, joined text, and raw outer HTML.
- **Encoding:** explicit charset labels, BOMs, and HTML `<meta>` declarations.

Some valid CSS and XPath expressions cannot be answered without retaining more tree state and remain
unsupported. Reverse positions and `:has()` also have placement restrictions. Python validates these
by default; `check()` provides the same audit as a report for tooling and CI, and
`frostwork-audit --scan myproject/spiders/` classifies the selector literals in code that has no
Frostwork schema yet (inline `.css()`/`.xpath()`, ItemLoaders, LinkExtractors) without importing it. The
[compatibility contract](docs/COMPATIBILITY.md) lists every supported, divergent, and unsupported form
with examples.

## Build, test, benchmark

```bash
make bootstrap     # create .venv and install the pinned Python test toolchain
make ci            # Rust, Python, lxml differential, encoding, and fuzz gates
make bench         # benchmark matrix against Parsel
make soak          # multi-seed differential and fuzz soak
```

Run `make help` for the individual targets. The differential gate requires zero supported-result
divergences and zero crashes against the pinned Parsel/lxml oracle.

- Architecture & design decisions: [docs/DESIGN.md](docs/DESIGN.md).
- Correctness methodology: [docs/TESTING.md](docs/TESTING.md).
- Exact selector/divergence contract: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).
- Benchmarks: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
- Parsel migration guide: [docs/MIGRATION.md](docs/MIGRATION.md).
- Runnable examples: `cargo run --example basic` and `.venv/bin/python examples/basic.py`.
- Conventions for contributors and coding agents: [AGENTS.md](AGENTS.md).

## Status

Frostwork is usable from source but is not yet published to PyPI. Build the Python extension with
`maturin develop --release`.

Correctness is checked against lxml across hundreds of thousands of page/selector pairs per differential
seed (the gate prints the exact figure; see [docs/TESTING.md](docs/TESTING.md)),
with additional encoding, random-selector, and malformed-HTML fuzzing. The Rust and Python page-object
layers, nested `Many`/`One` extraction, schema audit, and web-poet integration are included in those
release gates.

## License

BSD-3-Clause. See [LICENSE](LICENSE).
