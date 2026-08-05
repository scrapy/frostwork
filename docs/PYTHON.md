# Frostwork for Python (Scrapy / web-poet)

Python bindings over the Rust core, built with [PyO3](https://pyo3.rs) + [maturin](https://maturin.rs).
The FFI surface is deliberately tiny — extraction, compiled plans, and schema audit cross into Rust —
so there is exactly one implementation of matching logic (the Rust engine, held to the differential gate). The ergonomic
`Page`/`Item` layer and the web-poet integration are thin pure-Python on top.

Three layers, smallest to largest:

1. `frostwork.extract` — the one-pass primitive.
2. `frostwork.Page` / `frostwork.Item` — a declarative `{field: selector}` schema (mirror of the Rust API).
3. `frostwork.webpoet.FrostPage` / `FrostBrowserPage` — a web-poet page object whose selector fields
   share a single scan.

## Install / build

```bash
python -m venv .venv
.venv/bin/pip install maturin
.venv/bin/maturin develop            # builds the extension + installs `frostwork` (editable) into the venv
# for the web-poet integration:
.venv/bin/pip install web-poet
```

Wheels are **abi3** (`abi3-py39`): one wheel runs on CPython ≥ 3.9 (tested on 3.14).

## 1. The primitive

```python
import frostwork

html = b"<div class=product><h1>Widget</h1><span class=price>$9</span><img src=/a.png></div>"
cols = frostwork.extract(html, ["h1::text", ".price::text", "img::attr(src)", "//img/@src"])
# -> [['Widget'], ['$9'], ['/a.png'], ['/a.png']]     one column per query, in query order
```

`extract(html, queries, encoding=None, *, strict=True)` — `html` is `bytes` (preferred; the engine
tokenizes raw bytes) or `str` (encoded UTF-8). `encoding` is an optional charset label as Scrapy
passes from `Content-Type`; `None` sniffs (BOM → `<meta>` → UTF-8). A label naming no encoding at all
(`"utf8x"`) raises; one naming a real encoding WHATWG excludes (`utf-7`, `utf-32` — w3lib resolves
those against Python's codec set, so `response.encoding` can be one) is **ignored** and the page is
sniffed instead, per WHATWG's "failure, continue". Unsupported queries raise
`UnsupportedSelector` before the HTML is scanned. Pass `strict=False` for permissive empty columns.
Supported selectors and value semantics are exactly the Rust engine's — see
[COMPATIBILITY.md](COMPATIBILITY.md).

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

Pass `map=fn` to transform a field's shaped value in Python (never in the scan) — e.g.
`.field("price", ".price::text", map=lambda s: s.lstrip("$"))` or
`.field_all("prices", ".price::text", map=lambda xs: [float(x) for x in xs])`. `Item.value` /
`to_dict` reflect the transform; `get` / `get_all` return the raw matches.

## 3. web-poet page objects — `FrostPage` / `FrostBrowserPage`

`FrostPage` is a `web_poet.WebPage`. Declare fields with `field(...)`; each becomes a **real
`web_poet.field`**, so attribute access, `async to_item()`, `@handle_urls` routing, `Returns[Item]`,
and mixing in hand-written `@web_poet.field` methods all work as usual — but **every Frostwork
selector on the page object shares one cached `extract` call** (instead of one lxml parse + a query
per field). That is the point: one streaming pass per response, not one per field.

```python
import attrs
from web_poet import handle_urls, Returns
from frostwork.webpoet import FrostPage, field

@attrs.define
class Product:
    name: str
    price: str
    images: list
    brand: str | None

@handle_urls("example.com")
class ProductPage(FrostPage, Returns[Product]):
    name   = field("h1::text")
    price  = field(".price::text")
    images = field("img::attr(src)", all=True)     # -> list
    specs  = field(".spec ::text", join=" ")        # -> joined str
    brand  = field("//meta[@itemprop='brand']/@content")   # XPath works too

# scrapy-poet injects `response`; or construct directly:
#   item = await ProductPage(response=http_response).to_item()   # -> Product(...)
```

`field(selector, *, all=False, join=None)`:

| declaration | field value |
|---|---|
| `field(sel)` | first match, or `None` |
| `field(sel, all=True)` | `list[str]` of every match, document order |
| `field(sel, join=sep)` | every match joined into one `str` with `sep` |
| `field(sel).map(fn)` | the shaped value with `fn` applied (chainable) |
| `field(sel).re_first(rx)` | first regex match over the matched string (group 1 if any, else whole) |

`.re_first` operates on a scalar string, so it **raises `ValueError` at declaration on an `all=True`
field** (a list) — that would otherwise silently yield `None` for every page. Use `join=` (then
`re_first` sees the joined string) or `.map()` for a list transform.

`.map` / `.re_first` are **pure post-processing** — they run on the already-extracted value, never in
the scan, so a transformed field stays a one-liner and still rides the single pass:

```python
class ProductPage(FrostPage, Returns[Product], skip_nonitem_fields=True):
    price   = field(".price::text").map(parse_amount)          # "£51.77" -> "51.77"
    symbol  = field(".price::text").re_first(r"^\D+")           # -> "£"
    rating  = field("p.star-rating::attr(class)").map(rating_to_int)
```

The page body is scanned as `response.body` bytes using the response's resolved `response.encoding`, so
values match what Parsel would decode. For a browser snapshot or any other input, see
[Response types](#response-types--frostpage-frostbrowserpage-frostfields) below.

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

To pull a *list of sub-objects* (each product card, each spec row), declare a `Many` (or `One`): for
every element matching the container, each keyword sub-field — an ordinary `field(...)`, so `.map` /
`.re_first` compose — is extracted **scoped to that container** (descendant-or-self), all in the
**same one pass**. `Many` yields a `list` of rows; `One` yields the first row (or `None`). Pass
`item=` (a class/callable) to build each row into a typed object from `item(**row)`.

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

Rows are byte-identical to Parsel's `[ {sub: c.css(sub_sel).getall()} for c in doc.css(container) ]`
(the differential gates this). The primitive `frostwork.Page` has the same shape:
`Page().many("images", ".thumb", {"url": "img::attr(src)"})` / `.one(...)`. A sub-spec is a bare
selector string (first match) **or** a tuple carrying cardinality — `("sel", "all")` → list,
`("sel", "join", sep)` → joined string — matching `webpoet.Many`'s per-subfield expressiveness:

```python
Page().many("offers", ".offer", {"price": ".p::text", "tags": (".tag::text", "all")})
```

On the resulting `Item`, `get(name)` / `get_all(name)` also work for a group name: `get` returns the
first row (a `dict`), `get_all` returns the row list (a `One` group yields a 0- or 1-element list).
Sibling `+`/`~`, comma groups, reverse positions, `:has()`, and text-content predicates inside a
sub-field are unsupported and fail validation by default, as do deferred selectors used as group
containers. A `Many` nested inside a sub-field is also unsupported. Pass ``strict=False`` only when
empty results are the desired compatibility behavior.

### Response types — `FrostPage`, `FrostBrowserPage`, `FrostFields`

Pick the base that matches the input the framework will inject:

| base | input | notes |
|---|---|---|
| `FrostPage` | `web_poet.HttpResponse` | scans `.body` bytes with the response's resolved `.encoding` |
| `FrostBrowserPage` | `web_poet.BrowserResponse` | scans `.html`, encoded UTF-8 |
| `FrostFields` | anything — override `frostwork_input()` | a `web_poet.Extractor`, so it brings `to_item()` / `Returns[...]` |

A `BrowserResponse` carries `.html` (a `str`) and no bytes, so **nothing is sniffed**: the browser already
resolved the page's encoding, and re-deriving it from a re-encoding of the decoded text could only
disagree with the browser that produced it. A `<meta charset>` left over in the snapshot is therefore
ignored, which is the correct reading of an already-decoded DOM.

For any other dependency, override the hook:

```python
@attrs.define
class MyPage(FrostFields):
    blob: bytes
    def frostwork_input(self):
        return self.blob, None          # None -> the engine sniffs (BOM, then a <meta> prescan)
    title = field("h1::text")
```

**Declaring a `field()` on a class that does not inherit one of these raises `TypeError` at class
definition.** Markers are converted by `FrostFields.__init_subclass__`, so on a plain `web_poet.WebPage`
or `BrowserPage` nothing converts them and `to_item()` returns an item with those fields simply *absent* —
no error and no empty column. Failing at import is the point.

`web_poet.SelectorExtractor` is **not** supported and cannot usefully be: its input is a
`parsel.Selector`, i.e. a tree lxml has already built. Scanning it would mean serializing that tree back
to markup and re-parsing it, which can disagree with both the original bytes and the `Selector` itself —
and if a tree already exists, Frostwork has nothing left to save. Query the `Selector` directly there.

### Field processors — `Processors` / `out=`

web-poet applies **field processors** to a field's value before it reaches the item, and it finds them
**by field name** from a nested `Processors` class (`out=` on the field wins if given). That lookup is
why the zyte-common-items base pages need no wiring: inheriting `ProductPage` attaches
`breadcrumbs_processor` to a field merely *called* `breadcrumbs`.

A processor's input contract is an lxml/parsel **node** — they are gated on
`isinstance(value, (Selector, SelectorList, HtmlElement))` and documented to return anything else *as
is*. So for a **bare-element** field (whose value is the element's outer HTML, i.e. a node reference),
Frostwork re-parses that one subtree and hands the processor the **element**:

```python
from zyte_common_items.pages import ProductPage       # its Processors are inherited, not declared here
from frostwork.webpoet import field

class MyProductPage(ProductPage):
    breadcrumbs     = field(".crumbs")     # bare element -> breadcrumbs_processor gets the <nav> node
    descriptionHtml = field(".desc")       # -> description_html_processor gets the <div>, clear-html runs
    aggregateRating = field(".rating")     # -> rating_processor gets the <span>
    images          = field("img.hero::attr(src)", all=True)   # a SCALAR terminal: stays a list of str
```

Two things worth knowing about that, because both are deliberate:

- **The rule is keyed on the field's TERMINAL, not on whether a processor exists.** `::text` and
  `::attr()` fields are genuinely strings and are handed over untouched — `images_processor` takes URL
  *strings* and has no `Selector` branch at all, so converting every processor-bearing field to a node
  would break it. Which terminal a selector has is answered by the engine's own compiler
  (`frostwork._frostwork.selector_terminals`), never by pattern-matching the query string.
- **The node handoff is the one place this integration touches lxml/parsel**, imported lazily and only
  when a processor is attached to a bare-element field. A page object with no processors never reaches
  it. The cost is a parse of that *one subtree* — far less than the whole-document parse `FrostPage`
  exists to avoid, but not free. Anyone using processors already has both libraries installed;
  `zyte_common_items.processors` itself imports `from lxml.html import HtmlElement`.

`join=` on a processor-bearing field stays a string: a joined value is not a node and could only be
made into one by inventing a wrapper element.

## 4. Auditing a schema — `check` / strict validation

Frostwork has **no fallback**. The underlying engine can represent an unsupported selector as an empty
column, but the public Python APIs fail fast by default because that result looks exactly like a field
that is legitimately empty (or a page whose layout changed). Pass `strict=False` explicitly to request
permissive empty columns. `frostwork.check` audits a schema **without parsing any HTML** — it reports
which selectors the engine supports, an advisory reason for those it does not, and the budget usage.
The supported/unsupported *decision* is authoritative (it is the real compiler); each `reason` is
best-effort.

```python
import frostwork

report = frostwork.check(
    ["h1::text", "div:has(.a .b)::text", "//a[position()<2]/@href"],
    [("offers", ".offer", {"price": ".//span/text()", "kid": "./h3/text()"})],
)
report.ok            # False — some selectors are unsupported
report.over_budget   # False — within the 128-member / 64-sibling-bit limits
for f in report.unsupported:
    print(f.name, "->", f.reason)
# [1]  -> a chained selector inside :has() is unsupported
# [2]  -> positional predicate (`[position()<n]`, ...) ... a sole `[N]`/`[last()]` is supported
# kid  -> relative child anchor (`./x`) ... use the descendant form `.//x` instead
report.raise_for_status()   # raises frostwork.UnsupportedSelector unless ok
```

### Schema shapes `check` accepts

`queries` is one of — a **`{name: selector}` mapping**, an iterable of bare selectors (labelled `[0]`,
`[1]`, … by position), or an iterable of `(name, selector)` pairs. `groups` is one of — a
**`{name: (container, subfields)}` mapping**, `(name, container, subfields)` triples, or the bare
`(container, subfields)` shape `extract_grouped` takes (auto-named `group[0]`, …); `subfields` is
`{subname: selector}` or `[(subname, selector), …]` in every case. Anything else raises `TypeError`
naming these shapes.

The two mapping shapes are exactly what `FrostPage.frost_schema()` returns, so an exported schema can
be audited as-is:

```python
schema = ProductPage.frost_schema()
frostwork.check(schema["fields"], schema["groups"]).raise_for_status()
```

A mapping is **destructured, never iterated** — iterating `{"title": "h1::text"}` would audit the
field *name* `title`, which is a valid type selector, so every field would report supported and
`report.ok` would be `True`: a green report on a schema that was never looked at. For the same reason
`extract`/`extract_grouped` **reject** a mapping for `queries` with a `TypeError` instead of
extracting the names (their columns are positional — pass `list(schema.values())`, or use `Page` for a
named schema).

The primitive and both page-object layers validate by default:

```python
# frostwork.Page  (a reverse position IS supported — attached, subtree, or a descendant value; a CHILD
# step into that value tail is not, so `bad` below is the unsupported one)
page = Page().field("title", "h1::text").field("bad", "li:last-child > b::text")
page.check()                      # -> SchemaReport
page.extract(html)                # raises UnsupportedSelector before scanning
page.extract(html, strict=False)  # permissive: `bad` is empty

# frostwork.webpoet.FrostPage — validate at class-definition (import) time:
class ProductPage(FrostPage):     # raises here if any selector is unsupported
    name  = field("h1::text")
    price = field(".price::text")

ProductPage.check_schema()        # -> SchemaReport (own + inherited fields and groups)
```

Use `Page(strict=False)`, `extract(..., strict=False)`, or
`class ProductPage(FrostPage, strict=False)` only when permissive empty results are intentional. A
`frostwork-audit` step can still provide a consolidated report in CI, including for permissive page
objects.

`over_budget` (too many selectors) is a distinct problem from an unsupported selector — it is a caller
bug (split the schema), whereas unsupported is a coverage gap. `extract` still raises `ValueError` for
an over-budget schema at run time; `check` surfaces it as a report flag up front.

For CI, the `frostwork-audit` CLI runs the same static audit over a whole module of page objects
(discovering `Page` instances and `FrostPage` subclasses) and exits non-zero if any schema has an
unsupported selector or is over budget — no HTML required:

```console
$ frostwork-audit myproject/pages.py          # or: python -m frostwork.audit myproject/pages.py
PROBLEMS  ProductPage  (members 2/128, sib-bits 0/64)
    ✗ blurb = "div:contains('x')::text"
        :contains() is unsupported (Frostwork does not match on text content)
OK        CleanPage  (members 2/128, sib-bits 0/64)

1/2 schema(s) OK, 1 with problems           # exit status 1
```

Add `-v` to also list the supported selectors, or `--json` to emit the full report (per-schema fields,
groups, budget, and a top-level `ok`/`summary`) as machine-readable JSON for CI annotations — the exit
status is unchanged (`0` OK, `1` problems, `2` usage error).

`frostwork-audit` discovers schemas by **importing** the target (a `.py` path or a dotted module
name), so the module's top-level code runs. Point it at import-safe page-object modules — ones whose
import does not open network/service clients, read required env, or otherwise reach out. If a project
mixes schemas with heavy import-time setup, keep the page objects in a module that only defines them
and audit that one. You can also expose an explicit mapping/iterable and audit only it with
`frostwork-audit myproject.pages:SCHEMAS` (or `pages.py:SCHEMAS`).

### Auditing code that has no schema yet — `--scan`

The schema audit needs a `Page`/`FrostPage` object, so it cannot see selectors that live in plain
spider code: inline `response.css(...)`/`.xpath(...)` in a callback, `ItemLoader.add_css`/`add_xpath`,
`LinkExtractor(restrict_css=...)`. Those would have to be rewritten *before* they could be audited,
which is backwards — the audit is what tells you whether a rewrite is needed. `--scan` answers the same
question a step earlier: it parses the source with `ast` (**never imports it**, so import-time setup
can't fire) and classifies every selector *literal*, with `file:line` for each site:

```console
$ frostwork-audit --scan myproject/spiders/            # a file, a directory, or several of each
    ✗ spiders/shop.py:9 [LinkExtractor] "li:contains('next') a"
        :contains() is unsupported (Frostwork does not match on text content)
    ✗ spiders/shop.py:15 [xpath] 'td/text()'
        relative XPath step — a per-container loop's selector. Port the loop to a Many/One group
        (container + `.//`-anchored sub-fields); see docs/MIGRATION.md
    ? spiders/shop.py:18 [css] not a literal (f-string/variable) — cannot audit statically

5/7 literal selector(s) supported (71%), 2 unsupported, 1 skipped (not literals)   # exit status 1
```

A selector built at run time (f-string, concatenation, variable) cannot be decided statically, so it is
reported as **skipped** rather than quietly dropped — as is any file this interpreter cannot parse
(which also makes the exit status non-zero, since the scan is then incomplete). `-v` lists the supported
sites too and `--json` emits the whole report (per-site `file`/`line`/`call`/`selector`/`supported`, plus
a `summary` with the coverage ratio). This is a *migration sizing* tool, not a replacement for the
schema audit: it sees call sites, not schemas, so it cannot check the member/sibling budget or the
container↔sub-field relationship. Use both — `--scan` on un-ported code, the schema audit on page
objects.

### Dead selector or coverage gap? — `Item.empty_fields`

Without a fallback, an empty column under `strict=False` has two possible causes: the engine does not
support that selector, or the selector no longer matches the page. They *are* distinguishable, because
support is **static** — audit once, then treat emptiness as a page signal:

```python
report = page.check()                    # startup / CI: any unsupported selector is a coverage gap
item = page.extract(html)                # per response
for name in item.empty_fields():         # supported + empty  =>  the page changed
    log.warning("selector matched nothing: %s", name)
```

`empty_fields()` returns every declared field that matched nothing (a `many`/`one` group with no rows
counts; a field that matched an empty string does not — it matched). Under the default `strict=True` the
unsupported case cannot reach extraction at all, so every name it returns is a dead selector.

## Design notes

- **One implementation, one gate.** Python never re-implements matching; it calls the Rust `extract`.
  Correctness parity with lxml is whatever the Rust differential proves (`tools/diff_lxml.py`).
- **Compile once, extract many.** A `Page`/`FrostPage` schema is compiled to a native `Plan` a single
  time — `FrostPage` at class-creation, `Page` lazily on first `extract` (rebuilt only if you add a
  field) — and reused for every response, so the per-page selector recompile is gone. This is
  transparent (no API change) and matches the usage model: a page object is defined once, run over many
  pages. It matters most on small pages, where the recompile otherwise dominates (~3× on a 244-byte
  page with 8 selectors). An over-budget schema now raises when the plan is built (fail fast) rather
  than per page.
- **Why not a drop-in `.css()`/`.xpath()` selector?** A per-call selector would re-scan the page for
  every field — N scans instead of 1 — discarding Frostwork's whole advantage. `FrostPage` batches
  instead, which is why fields are *declared* rather than pulled ad-hoc inside methods.
- **Tests:** `tests/test_python.py` (`.venv/bin/python -m pytest tests/test_python.py`) covers the
  primitive, `Page`/`Item`, the web-poet wiring (incl. a monkeypatched assertion that a multi-field
  page object triggers exactly **one** `extract` call), and a parsel cross-check.
