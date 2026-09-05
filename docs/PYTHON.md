# Frostwork for Python (Scrapy / web-poet)

Python bindings over the Rust core, built with [PyO3](https://pyo3.rs) and
[maturin](https://maturin.rs). Matching stays in Rust; the `Page`/`Item` layer and web-poet integration
provide the Python-facing API.

For an existing Scrapy spider, start with the [migration workflow](MIGRATION.md); for a runnable
project, use [frostwork-demo](https://github.com/shaneaevans/frostwork-demo).

Three layers, smallest to largest:

1. `frostwork.extract` — the one-pass primitive.
2. `frostwork.Page` / `frostwork.Item` — a declarative `{field: selector}` schema (mirror of the Rust API).
3. `frostwork.webpoet.FrostPage` / `FrostBrowserPage` — a web-poet page object whose selector fields
   share a single scan.

## Install

Frostwork requires Python ≥ 3.10. Install the core package, or include the web-poet integration:

```bash
pip install frostwork
pip install "frostwork[webpoet]"
```

Published wheels contain the Rust extension and do not require a Rust toolchain.

### Development build

```bash
python -m venv .venv
.venv/bin/pip install maturin
.venv/bin/maturin develop --release --extras=webpoet   # extension + `frostwork[webpoet]`, editable
```

`--extras=webpoet` installs the supported web-poet release. Drop it if you only need
`extract`/`Page`/`frostwork-audit`. Wheels are **abi3** (`abi3-py310`): one wheel runs on CPython ≥ 3.10
(tested up to the 3.15 prerelease).

**3.10 is the floor for everything** — core and extra alike. The wheel is `abi3-py310` so the engine can
borrow a `str`'s UTF-8 view instead of copying the document (`PyUnicode_AsUTF8AndSize` is limited-API
only from 3.10), and web-poet requires 3.10 as well. Python 3.9 reached end of life in October 2025. The
extra supports `web-poet >= 0.24.1, < 0.25`, the release line exercised by the integration gates.

## 1. The primitive

```python
import frostwork

html = b"<div class=product><h1>Widget</h1><span class=price>$9</span><img src=/a.png></div>"
cols = frostwork.extract(html, ["h1::text", ".price::text", "img::attr(src)", "//img/@src"])
# -> [['Widget'], ['$9'], ['/a.png'], ['/a.png']]     one column per query, in query order
```

`extract(html, queries, encoding=None, *, strict=True)` accepts `bytes` (preferred) or a `str`, which is
encoded as UTF-8. Pass the response's charset label as `encoding`; `None` checks the BOM and declarations
before defaulting to UTF-8. An unknown label raises, while a known label excluded by WHATWG is ignored and
sniffing continues.

Unsupported queries raise `UnsupportedSelector` before the HTML is scanned. Pass `strict=False` for
permissive empty columns. [COMPATIBILITY.md](COMPATIBILITY.md) defines selector, value and encoding behavior.

`detect_encoding(html, encoding=None)` answers the sniffing half on its own, as a WHATWG name:

```python
frostwork.detect_encoding(b"<html><body><meta charset=windows-1252>")   # 'windows-1252'
frostwork.detect_encoding(b"<p>x</p>", "latin-1")                       # 'windows-1252'
```

It is the same resolution `extract` runs, not a second opinion — useful when the rest of a pipeline needs
the label too. Parsel cannot answer it (`Selector(body=…)` never looks at `<meta charset>`), and w3lib
stops at `<body>` and at the first declaration it cannot resolve; see
[COMPATIBILITY.md](COMPATIBILITY.md#encoding) for the enumerated differences.

## 2. Declarative `Page` / `Item`

```python
from frostwork import Page

page = (Page()
        .field("title",  "h1::text")            # first match  -> str | None
        .field("price",  ".price::text")
        .field_all("images", "img::attr(src)")  # every match  -> list[str]
        .field_join("desc", ".desc ::text", " "))  # matches joined -> str

item = page.extract(html, encoding=None)         # ONE streaming pass fills every field
item.get("title")        # 'Widget'      (first value; cardinality-independent)
item.get_all("images")   # ['/a.png', ...]
item.value("desc")       # cardinality-aware value
item.to_dict()           # {'title': 'Widget', 'images': [...], ...}
item.to_json()           # same, JSON (UTF-8 preserved)
```

Build the schema once and reuse the `Page` across responses. The document scan is shared; matching
work grows with selector count instead of repeating a full parse/query workflow per field.

A schema of nothing but `field(...)` (single-valued) also **stops scanning** once every field has a
value, so a page whose fields live in the head never tokenizes the body. It is automatic and has no
accuracy trade-off — the values skipped are the ones a single-valued field discards anyway. One
`field_all`/`field_join`, one `many`/`one` group, or one deferred selector (`:has()`, `:last-child`, a
text predicate) turns it off, because those answers can still change further down the page.

Pass `map=fn` to transform a field's shaped value in Python (never in the scan) — e.g.
`.field("price", ".price::text", map=lambda s: s.lstrip("$"))` or
`.field_all("prices", ".price::text", map=lambda xs: [float(x) for x in xs])`. `Item.value` /
`to_dict` reflect the transform; `get` / `get_all` return the raw matches.

`get_all(name)` follows the field declaration: `field` returns zero or one raw match,
`field_all` returns every match, and `field_join` returns every match before joining. A matched empty
string is a value (`[""]`), distinct from no match (`[]`). To read every match, declare `field_all`;
adding another field or disabling early exit never changes what `get_all` returns for an existing field.

## 3. web-poet page objects — `FrostPage` / `FrostBrowserPage`

`FrostPage` is a `web_poet.WebPage`. Each `field(...)` becomes a real `web_poet.field`, so attribute
access, `async to_item()`, `@handle_urls`, `Returns[Item]` and hand-written fields work normally. All
Frostwork fields on the page share one compiled extraction pass.

<!-- doc-test: product-page -->
```python
import attrs
from typing import List, Optional
from web_poet import Returns
from frostwork.webpoet import FrostPage, field

@attrs.define
class Product:
    name: Optional[str]
    price: Optional[str]
    images: List[str]
    specs: str
    brand: Optional[str]

class ProductPage(FrostPage, Returns[Product]):
    name   = field("h1::text")
    price  = field(".price::text")
    images = field("img::attr(src)", all=True)     # -> list
    specs  = field(".spec ::text", join=" ")        # -> joined str
    brand  = field("//meta[@itemprop='brand']/@content")   # XPath works too

# scrapy-poet injects `response`; or construct directly:
#   item = await ProductPage(response=http_response).to_item()   # -> Product(...)
```

Every item field must fit the `Returns[...]` type or `to_item()` raises. Pass
`skip_nonitem_fields=True` when the page intentionally contains helper fields that are not item attributes.
Complete examples on this page are executed by the test suite as written.

`field(selector, *, all=False, join=None, cached=False, meta=None, out=None)`:

| declaration | field value | static type |
| --- | --- | --- |
| `field(sel)` | first match, or `None` | `str \ | None` |
| `field(sel, all=True)` | `list[str]` of every match, document order | `list[str]` |
| `field(sel, join=sep)` | every match joined into one `str` with `sep` | `str` |
| `field(sel).map(fn)` | the shaped value with `fn` applied (chainable) | `fn`'s return type |
| `field(sel).re_first(rx)` | first regex match over the matched string (group 1 if any, else whole) | `str \ | None` |
| `field(sel).typed_as(T)` | unchanged — a no-op that re-annotates the field as `T` | `T` |

The package ships `py.typed`. These annotations describe the value before a processor runs; use
`.typed_as(List[Breadcrumb])` (or any other type expression, including a union) when a processor changes
the result type. `.typed_as()` has no runtime effect.

`cached`, `meta` and `out` are `web_poet.field`'s own keywords, forwarded verbatim. They compose in a
fixed order with the Frostwork-side transforms:

1. the column is shaped by `all` / `join`,
2. `.map()` / `.re_first()` run — plain callables, value in, value out,
3. web-poet's processors run — `out=` if given, otherwise a nested `Processors` entry for the field name.
   A processor receives `value`; if it declares a `page` parameter, web-poet supplies the page too.

Reach for `.map()` for a local value tweak and `out=` for an ecosystem processor.

`.map()` and `.re_first()` run after extraction and do not add another scan. `.re_first()` requires a
scalar string, so it raises at declaration on an `all=True` field; use `join=` or a list-aware `.map()`
instead.

```python
class ProductPage(FrostPage, Returns[Product], skip_nonitem_fields=True):
    price   = field(".price::text").map(parse_amount)          # "£51.77" -> "51.77"
    symbol  = field(".price::text").re_first(r"^\D+")           # -> "£"
    rating  = field("p.star-rating::attr(class)").map(rating_to_int)
```

The page body is scanned as `response.body` bytes using the response's resolved `response.encoding`, which
aligns charset selection with Parsel. The legacy decoder differences listed in
[COMPATIBILITY.md](COMPATIBILITY.md) still apply. For a browser snapshot or another input, see
[Response types](#response-types--frostpage-frostbrowserpage-frostfields).

**Recipe — relative → absolute URLs.** Frostwork returns the raw attribute value; to resolve it
against the page URL, extract into a helper field and compose a computed `@web_poet.field` that calls
`self.response.urljoin`. Mark the helper non-item with `skip_nonitem_fields=True` so it's dropped:

```python
import web_poet
from frostwork.webpoet import FrostPage, field

class ProductPage(FrostPage, Returns[Product], skip_nonitem_fields=True):
    _href = field("a.next::attr(href)")          # helper column (relative), not an item attr

    @web_poet.field
    def url(self):                                # computed: composes with the batched selectors
        return self.response.urljoin(self._href)  # -> absolute
```

Any field that reads *other* fields or the response is just a normal `@web_poet.field` method — it
rides the same one pass.

**Introspection — `frost_schema()`.** A classmethod returning the page object's full schema (own +
inherited), for benchmarking / parity / tooling — the public replacement for reaching into the
private `_frostwork_own_specs`:

```python
ProductPage.frost_schema()
# {"fields": {name: selector, ...},
#  "groups": {name: (container, {subname: selector, ...}), ...}}
```

### Nested collections — `Many` / `One`

Use `Many` for a list of scoped rows and `One` for the first row (or `None`). Each keyword is a normal
`field(...)` evaluated relative to the container in the same extraction pass. Pass a class or callable as
`item=` to build typed rows with `item(**row)`.

```python
from frostwork.webpoet import FrostPage, field, Many, One

class ProductPage(FrostPage, Returns[Product], skip_nonitem_fields=True):
    name        = field("h1::text")
    images      = Many(".thumb", item=Image, url=field("img::attr(src)"))          # -> list[Image]
    breadcrumbs = Many(".crumb", item=Breadcrumb, name=field("a::text"), url=field("a::attr(href)"))
    rating      = One(".reviews", item=AggregateRating,
                      ratingValue=field(".stars::text").map(float),
                      reviewCount=field(".count::text").re_first(r"\d+").map(int))
```

A subfield uses the first match by default; `all=True` and `join=` provide the same cardinality choices as
a flat field. Scoping and cardinality are differentially checked against Parsel. The primitive `Page` API
has the same shape, with a selector string for the first match or a tuple for `all`/`join`:

```python
Page().many("offers", ".offer", {"price": ".p::text", "tags": (".tag::text", "all")})
```

For a group name, `Item.get()` returns the first row and `get_all()` returns its row list. Nested groups
are unsupported, and group containers/subfields support a narrower selector surface than flat fields; the
schema audit reports the exact verdict. Use `strict=False` only when empty results are intentional.

### Response types — `FrostPage`, `FrostBrowserPage`, `FrostFields`

Pick the base that matches the input the framework will inject:

| base | input | notes |
| --- | --- | --- |
| `FrostPage` | `web_poet.HttpResponse` | scans `.body` bytes with the response's resolved `.encoding` |
| `FrostBrowserPage` | `web_poet.BrowserResponse` | scans `.html` as UTF-8, borrowed rather than re-encoded |
| `FrostFields` | anything — override `frostwork_input()` | a `web_poet.ItemPage`: brings `to_item()` / `Returns[...]`, and is injectable |

A `BrowserResponse` already contains decoded HTML, so Frostwork does not sniff a charset from it. Any
remaining `<meta charset>` is ignored.

The `str` is handed over **unencoded** — `frostwork_input()` may return `bytes` *or* `str`, and the engine
borrows CPython's UTF-8 view of a `str` rather than allocating a second copy of the document. Return the
original bytes where you have them, the `str` itself where you do not, and never
`str.encode("utf-8")`. Because a `str` is scanned as UTF-8, the only `encoding` it accepts is `None` or a
UTF-8 label; anything else is refused instead of decoding those bytes wrongly.

For any other dependency, override the hook:

```python
@attrs.define
class MyPage(FrostFields):
    blob: bytes
    def frostwork_input(self):
        return self.blob, None          # None -> the engine sniffs (BOM, then a <meta> prescan)
    title = field("h1::text")
```

Declaring a Frostwork `field()` on a class without a Frostwork page base raises `TypeError` at class
definition. This prevents an unconverted marker from becoming a silently absent item field.

On **Python 3.10 and 3.11 it arrives wrapped**: the check runs in `__set_name__`, and CPython before 3.12
re-raises anything from `__set_name__` as `RuntimeError`
([gh-77757](https://github.com/python/cpython/issues/77757) stopped that in 3.12). Frostwork's message is
intact on `__cause__`, so the diagnosis is not lost, but code catching this needs both types:

```python
except (TypeError, RuntimeError) as exc:      # RuntimeError only on < 3.12; exc.__cause__ has the TypeError
```

`web_poet.SelectorExtractor` is not supported: its input is already a parsed tree, so serializing and
re-scanning it would add work and could change the source. Query that `Selector` directly.

### Field processors — `Processors` / `out=` / `.as_node()`

web-poet finds processors by field name in a nested `Processors` class; an explicit `out=` takes priority.
A processor normally receives the field value: a string, a list, or the raw HTML source of a bare element.

```python
name = field("h1::text", out=[str.title])                   # processor receives a string
raw = field(".desc", out=[lambda v: v.strip()]).as_value()  # processor receives HTML source
```

On a processor-bearing bare-element field, state the input explicitly. `.as_value()` passes source;
`.as_node()` reparses the matched element as a Parsel node. The latter is required by most
zyte-common-items field processors:

<!-- doc-test: zyte-product-page -->
```python
from zyte_common_items.pages import ProductPage       # its Processors are inherited, not declared here
from frostwork.webpoet import FrostPage, field

class MyProductPage(FrostPage, ProductPage):
    name = field("h1::text")
    breadcrumbs = field(".crumbs").as_node()
    descriptionHtml = field(".desc").as_node()
    aggregateRating = field(".rating").as_node()
    images = field("img.hero::attr(src)", all=True)  # images_processor expects URL strings
```

Frostwork refuses ambiguous or impossible declarations at class definition. It also refuses a match whose
resolved tag is context-dependent:

| refused | why it cannot be guessed |
| --- | --- |
| a processor on a bare-element field with neither `.as_node()` nor `.as_value()` | processor signatures do not reveal which representation they expect |
| `.as_node()` on a `::text` / `::attr()` field | there is no element to re-parse |
| `.as_node()` with `join=` | a joined string is not one element |
| `.as_node()` with `.map()` / `.re_first()` | the transform could change the source before it is reparsed; transform the processor output instead |
| `.as_node()` on an unconstrained selector such as `field("*")` | a synthesized document frame has no start tag in the source, so an unpinned match can be ambiguous |
| an `.as_node()` match whose tag is `frameset` | the same source builds a different subtree at document root and inside a body; the captured source has lost that context |

Name or constrain an ambiguous selector, for example `field("body")` or `field("[id=main]")`. To decline an
inherited processor, pass `out=[]`; the field then returns its ordinary value.

`.as_node()` returns a standalone subtree: the matched tag, attributes and descendants, without ancestors,
siblings or `base_url`. The compiler supplies a pinned tag when possible and otherwise proves that reading
the tag from each match's source is safe. Selectors whose original context cannot be reconstructed must fail
closed. Processors needing document context should accept `page` and read it there.

Reparsing happens once per match. It is inexpensive for the common scalar case but can lose to Parsel on a
many-match field; see [BENCHMARKS.md](BENCHMARKS.md#performance-boundaries).

`Many`/`One` subfields support `.map()`, not web-poet field keywords: web-poet sees the group as the field,
not each row column. Use a hand-written `@web_poet.field` to process a whole group.

## 4. Auditing a schema — `check` / strict validation

Frostwork has no fallback, so the Python APIs reject unsupported selectors by default.
`frostwork.check` validates a schema without parsing HTML and reports selector support and budget use.
The verdict comes from the compiler; explanations are advisory.

```python
import frostwork

report = frostwork.check(
    ["h1::text", "div:has(.a .b)::text", "//a[position()<2]/@href"],
    [("offers", ".offer", {"price": ".//span/text()", "kid": "./h3/text()"})],
)
report.ok            # False
report.over_budget   # False
for f in report.unsupported:
    print(f.name, "->", f.reason)
report.raise_for_status()   # raises UnsupportedSelector unless report.ok
```

### Accepted schema shapes

`queries` accepts a `{name: selector}` mapping, bare selectors, or `(name, selector)` pairs. `groups`
accepts a `{name: (container, subfields)}` mapping, `(name, container, subfields)` triples, or the bare
`(container, subfields)` form used by `extract_grouped`. Subfields may be a mapping or name-selector
pairs. Invalid shapes raise `TypeError`.

`Page.frost_schema()` and `FrostPage.frost_schema()` return the mapping form, ready to audit:

```python
schema = ProductPage.frost_schema()
frostwork.check(schema["fields"], schema["groups"]).raise_for_status()
```

The primitive and both page-object layers validate by default:

```python
page = Page().field("title", "h1::text").field("bad", "li:last-child > b::text")
page.check()                      # -> SchemaReport
page.extract(html)                # raises UnsupportedSelector before scanning
page.extract(html, strict=False)  # permissive: `bad` is empty

# FrostPage validates at class-definition time:
class ProductPage(FrostPage):
    name  = field("h1::text")
    price = field(".price::text")

ProductPage.check_schema()        # -> SchemaReport (own + inherited fields and groups)
```

Use `strict=False` only when an empty column is intentional. `over_budget` means the schema exceeds the
engine limits and should be split; extraction raises `ValueError` for it.

### CLI audit

`frostwork-audit` discovers `Page` instances and concrete `FrostFields` subclasses, including
`FrostPage` and `FrostBrowserPage` subclasses, and exits non-zero for unsupported or over-budget schemas:

```bash
frostwork-audit myproject/pages.py
python -m frostwork.audit myproject.pages
```

Use `-v` for supported selectors and `--json` for CI output. The command imports its target, so audit an
import-safe module. To limit discovery, expose a collection and pass `myproject.pages:SCHEMAS`.

### Auditing code that has no schema yet — `--scan`

`--scan` inspects selector literals in existing Scrapy code without importing it. It recognizes common
`response.css` / `response.xpath`, `ItemLoader`, and `LinkExtractor` call sites:

```bash
frostwork-audit --scan myproject/spiders/
```

Dynamic selectors are reported as skipped. Parse failures are reported as errors and make the scan fail.
Literal `Page.many/one` and web-poet `Many/One` selectors are checked in their group context, so a
flat-supported selector that cannot run inside a group is reported correctly. The scan cannot prove
whole-schema budgets or resolve dynamic construction; run the import-based schema audit after conversion.
The scanner recognizes documented call shapes; aliases and custom wrappers may be invisible.
A scan with no literal selectors reports **unknown** coverage. Add `--require-complete` to fail CI on
dynamic selectors, parse errors or a scan with no auditable literals.

### Dead selector or coverage gap? — `Item.empty_fields`

Audit support once, then use `empty_fields()` to detect selectors that matched nothing:

```python
report = page.check()                    # startup / CI: any unsupported selector is a coverage gap
item = page.extract(html)                # per response
for name in item.empty_fields():
    log.warning("selector matched nothing: %s", name)
```

An empty string counts as a match. An empty `many`/`one` group counts as empty. With the default
`strict=True`, every returned name is a supported selector that matched nothing.

## 5. Response adapters and item validation

`Page.extract_response(response)` reads `response.body` with `response.encoding`. It works with Scrapy
and web-poet byte responses without importing either framework. It never accesses `response.text`.

Validate the data contract separately from selector support:

<!-- doc-test: runtime-validation -->
```python
from frostwork import Page

page = (Page().field("title", "h1::text", map=str.strip)
        .field_all("images", "img::attr(src)")
        .many("offers", "article", {"price": "./b/text()"}))
item = page.extract(b'<h1> Mug </h1><img src="/mug.jpg"><article><b>9</b></article>')
report = item.validate(required=["title", "offers"], counts={"images": (1, 8)},
                       group_required={"offers": ["price"]})
report.raise_for_status()
assert report.item == {"title": "Mug", "images": ["/mug.jpg"], "offers": [{"price": "9"}]}
```

`report.issues` carries the field path, code and explanation. `report.states` distinguishes `no_match`,
`matched_empty`, `processed_empty`, `processing_failed` and `filled`. Required checks use the final value;
zero and `False` are values, and a transform may supply a missing default. Whitespace stays significant
unless your transform strips it. Count bounds apply to raw matches and require `field_all`, `field_join`
or `many`; first-value declarations cannot prove a maximum count. Group requirements check each existing
row, so require the group itself when at least one row must exist.

In a spider, use `report.record_stats(self.crawler.stats)` for bounded counters and yield `report.item`
after checking `report.ok`. This reuses processed values instead of running transforms again through
`item.to_dict()`. A `map=` exception accessed through `Item.value()` or `to_dict()` raises
`FieldProcessingError` with field and selector context, preserving the original exception as its cause.
Validation records it as an issue and omits that failed field from `report.item`.

An absent optional value does not establish a layout change or rule out a parsing difference. Use saved
responses to investigate, following the [migration workflow](MIGRATION.md).

## Further reading

- [Migration workflow](MIGRATION.md) — saved responses, whole-item parity and reproducible timings.
- [Compatibility](COMPATIBILITY.md) — supported selectors and known differences.
- [Testing](TESTING.md) — correctness gates and release checks.
- [Benchmarks](BENCHMARKS.md) — measured performance and boundaries.
