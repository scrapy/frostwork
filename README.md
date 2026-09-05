# Frostwork

Fast HTML extraction for Python and Rust, without a DOM.

Frostwork compiles a set of CSS or XPath selectors and extracts their values in one scan of an HTML
response. Reuse the schema across Scrapy callbacks, web-poet pages or standalone code. Working memory
tracks parser state, pending matches and returned values, without storing a document tree.

Frostwork supports a focused selector subset, continuously checked against Parsel/lxml. Python rejects
unsupported selectors before scanning; `strict=False` requests empty columns. There is no parser fallback.
Check the [compatibility contract](https://github.com/scrapy/frostwork/blob/main/docs/COMPATIBILITY.md)
before migrating code that needs arbitrary tree traversal.

The [benchmarks](https://github.com/scrapy/frostwork/blob/main/docs/BENCHMARKS.md) compare throughput,
memory and selector coverage with Parsel, lxml and other engines, including workloads where Frostwork
loses. Performance depends on page shape, selector count and output volume.

## Install

Frostwork requires Python ≥ 3.10. Install the core package, or include the web-poet integration:

```bash
pip install frostwork
pip install "frostwork[webpoet]"
```

Published wheels contain the Rust extension, so installing from PyPI does not need a Rust toolchain.
Building an editable checkout does; see the
[Python development guide](https://github.com/scrapy/frostwork/blob/main/docs/PYTHON.md#development-build).

## Extract a named item

Build a `Page` once, then reuse it for each response:

<!-- doc-test: page-quickstart -->
```python
from frostwork import Page

PRODUCT = (Page()
           .field("name", "h1::text")              # first match, or None
           .field("price", ".price::text")
           .field_all("images", "img::attr(src)")) # every match, or []

html = b"<h1>Widget</h1><span class=price>$9</span><img src=/a.png>"
assert PRODUCT.extract(html).to_dict() == {
    "name": "Widget", "price": "$9", "images": ["/a.png"],
}
```

In an ordinary Scrapy callback, pass the original bytes and the response's encoding:

```python
def parse_product(self, response):
    yield PRODUCT.extract(response.body, encoding=response.encoding).to_dict()
```

Use `field_all` whenever you need every match, and `field_join` to join text nodes. Without an explicit
encoding, Frostwork checks the BOM and declarations before defaulting to UTF-8. For positional columns
without field names, use the [primitive `extract` API](https://github.com/scrapy/frostwork/blob/main/docs/PYTHON.md#1-the-primitive).

The same engine is a Rust library — `frostwork::extract(html, &queries, None)`, with `Page`/`Plan` for named
fields and compile-once reuse. See the
[runnable example](https://github.com/scrapy/frostwork/blob/main/examples/basic.rs).

## Scrapy and web-poet

For a runnable Scrapy project, start with
[frostwork-demo](https://github.com/shaneaevans/frostwork-demo). Its local storefront examples cover
ordinary callbacks, ItemLoader, web-poet, nested products and browser rendering, with tests that run
real crawls and compare exported items against Parsel. For existing spiders, follow the
[migration workflow](https://github.com/scrapy/frostwork/blob/main/docs/MIGRATION.md) to audit complete
schemas, verify saved responses and monitor extracted items.

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
[Python guide](https://github.com/scrapy/frostwork/blob/main/docs/PYTHON.md#3-web-poet-page-objects--frostpage--frostbrowserpage).

For injection, install and configure `scrapy-poet` using its
[setup guide](https://scrapy-poet.readthedocs.io/en/stable/intro/setup.html), or copy the working settings
and explicitly assigned callbacks from the demo. Outside Scrapy, construct the page directly:
`item = await ProductPage(response=http_response).to_item()`.

## Before migrating a spider

Run `frostwork-audit --scan myproject/spiders/` to inspect selector literals without importing the spider.
Once you have a schema, `PRODUCT.check().raise_for_status()` checks its selectors and shared budgets.
Then [compare complete items on saved responses](https://github.com/scrapy/frostwork/blob/main/docs/MIGRATION.md)
before switching a callback.

The main compatibility boundaries are:

- **Selector context:** grouped fields support fewer shapes than flat fields. Audit the actual schema.
- **HTML values:** bare-element selectors return source HTML, which can differ from lxml serialization.
- **Encoding:** decoding follows browser behavior, including documented differences from Parsel/w3lib.

The [compatibility contract](https://github.com/scrapy/frostwork/blob/main/docs/COMPATIBILITY.md) contains
the exact limits and cases where Frostwork returns values Parsel cannot. The
[Python guide](https://github.com/scrapy/frostwork/blob/main/docs/PYTHON.md) covers groups, processors,
response adapters and runtime item validation.

## Build, test, benchmark

```bash
make bootstrap     # create .venv and install the pinned Python test toolchain
make ci            # Rust, Python, lxml differential, encoding, and fuzz gates
make bench         # benchmark matrix against Parsel
make soak          # multi-seed differential and fuzz soak
```

`make help` lists the individual targets.
[TESTING.md](https://github.com/scrapy/frostwork/blob/main/docs/TESTING.md) explains what each gate proves and
what remains outside it.

## More

- [Architecture and design decisions](https://github.com/scrapy/frostwork/blob/main/docs/DESIGN.md)
- [Python API and recipes](https://github.com/scrapy/frostwork/blob/main/docs/PYTHON.md)
- [Selector and divergence contract](https://github.com/scrapy/frostwork/blob/main/docs/COMPATIBILITY.md)
- [Correctness methodology](https://github.com/scrapy/frostwork/blob/main/docs/TESTING.md)
- [Benchmarks](https://github.com/scrapy/frostwork/blob/main/docs/BENCHMARKS.md)
- [Parsel migration](https://github.com/scrapy/frostwork/blob/main/docs/MIGRATION.md)
- [Release history](https://github.com/scrapy/frostwork/blob/main/CHANGELOG.md)

## License

Apache-2.0. See [LICENSE](https://github.com/scrapy/frostwork/blob/main/LICENSE).
