# Frostwork

Fast HTML extraction for Python and Rust, without a DOM.

Frostwork compiles a set of CSS or XPath selectors, scans an HTML response once, and emits only the
requested values. It does not build a document tree, so working memory tracks parser state and pending
matches rather than the whole page. Supported results are continuously checked against lxml; the exact
coverage and known differences are listed in the [compatibility contract](docs/COMPATIBILITY.md).

**~8× faster than lxml and ~11× faster than Parsel (what Scrapy uses) at the median on the measured
production-selector corpus, and ~6.7× faster than selectolax/lexbor on the workload both can express.
Often much faster on large, selector-rich product and listing pages, where each of them must traverse a
DOM per field.**

Because Frostwork never builds that DOM, working memory stays essentially constant as page size grows for
a fixed-output schema; it scales with parser state and returned values instead of the page tree. Results
depend on page shape, selector count and output volume; [BENCHMARKS.md](docs/BENCHMARKS.md) has the full
methodology and performance boundaries.

Frostwork deliberately supports a focused subset of CSS and XPath. Python fails before scanning when a
selector is unsupported; `strict=False` opts into an empty column instead. Unsupported selectors never
fall back to another parser or produce guessed results. If an application needs arbitrary DOM access, use
lxml — Frostwork is for schemas known in advance.

## Install

Until packages are published, install from the repository. Building the extension needs a
[Rust toolchain](https://rustup.rs) (stable) alongside Python ≥ 3.9:

```bash
python -m venv .venv
.venv/bin/pip install maturin
.venv/bin/maturin develop --release --extras=webpoet   # compiles the Rust core; needs cargo/rustc on PATH
```

`--extras=webpoet` installs the supported web-poet release; drop it if you only need `extract`/`Page`.
That extra requires Python ≥ 3.10, while the core supports Python ≥ 3.9.

## Extracting values

The primitive API takes the response body and all selectors together, and answers them in one scan:

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

`extract(..., encoding="windows-1252")` accepts the charset label a Scrapy response supplies; without one,
Frostwork checks the BOM and `<meta>` declarations before defaulting to UTF-8. `frostwork.Page` adds names
and per-field cardinality for applications that do not use web-poet.

The same engine is a Rust library — `frostwork::extract(html, &queries, None)`, with `Page`/`Plan` for named
fields and compile-once reuse. See the [runnable example](examples/basic.rs).

## Scrapy and web-poet

A page object declares its selectors; Frostwork fills every field from one scan of the response:

<!-- doc-test: frost-page -->
```python
from frostwork.webpoet import FrostPage, field

class ProductPage(FrostPage):
    name = field("h1::text")
    price = field(".price::text")
    images = field("img::attr(src)", all=True)
    specs = field(".spec ::text", join=" ")
    brand = field("//meta[@itemprop='brand']/@content")
```

`to_item()` returns a dict here. Add `Returns[YourItem]` for a typed item, as shown in the
[Python guide](docs/PYTHON.md#3-web-poet-page-objects--frostpage--frostbrowserpage).

Install `scrapy-poet` and enable it in `settings.py` with `ADDONS = {"scrapy_poet.Addon": 300}`
(Scrapy ≥ 2.10; see [scrapy-poet's setup guide](https://scrapy-poet.readthedocs.io/en/stable/intro/setup.html)
for older versions). It then builds the page object from the callback's **annotation**:

```python
import scrapy

class ProductSpider(scrapy.Spider):
    name = "products"
    start_urls = ["https://example.com/catalogue/"]

    def parse(self, response):
        for href in response.css("a.product::attr(href)").getall():
            yield response.follow(href, callback=self.parse_product)

    async def parse_product(self, response, page: ProductPage):
        yield await page.to_item()
```

Requests with `callback=None` — including those created from `start_urls` — do not reliably receive
dependencies in `parse`. Use an explicitly assigned callback for injected page objects. Outside Scrapy,
construct the page directly: `item = await ProductPage(response=http_response).to_item()`.

This repository does not pin or test a Scrapy/Twisted matrix; use
[scrapy-poet's documentation](https://scrapy-poet.readthedocs.io/) for setup details. Frostwork does gate
the injection boundary: every shipped page base is injectable and can be planned as a callback dependency.
Field processors, groups, response types and schema auditing are covered in the [Python guide](docs/PYTHON.md).

## Limitations

- **CSS:** tags, IDs, classes, attribute operators, descendant/child/sibling combinators, `:not()`, `:is()`,
  `:where()`, subject `:has()`, and structural positions such as `:nth-child()`/`:last-of-type`.
- **XPath:** downward paths, attribute and text predicates, unions, positional predicates,
  `following-sibling::`, `ancestor::`, `parent::`, and top-level `normalize-space()`.
- **Values:** text, attributes, descendant attributes, joined text, and raw outer HTML.

Expressions that cannot be answered without retaining more tree state stay unsupported, and reverse
positions and `:has()` have placement restrictions. `check()` reports the same verdict for tooling and CI,
and `frostwork-audit --scan myproject/spiders/` classifies selector literals in code with no Frostwork
schema yet, without importing it. The [compatibility contract](docs/COMPATIBILITY.md) lists supported,
divergent and unsupported forms with examples.

## Build, test, benchmark

```bash
make bootstrap     # create .venv and install the pinned Python test toolchain
make ci            # Rust, Python, lxml differential, encoding, and fuzz gates
make bench         # benchmark matrix against Parsel
make soak          # multi-seed differential and fuzz soak
```

`make help` lists the individual targets. [TESTING.md](docs/TESTING.md) explains what each gate proves and
what remains outside it.

## More

- [Architecture and design decisions](docs/DESIGN.md)
- [Python API and recipes](docs/PYTHON.md)
- [Selector and divergence contract](docs/COMPATIBILITY.md)
- [Correctness methodology](docs/TESTING.md)
- [Benchmarks](docs/BENCHMARKS.md)
- [Parsel migration](docs/MIGRATION.md)

Frostwork is usable from source but not yet published to PyPI.

## License

BSD-3-Clause. See [LICENSE](LICENSE).
