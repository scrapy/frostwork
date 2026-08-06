# web-poet integration surface

Generated and verified by `tools/webpoet_surface.py`. The page bases, field keywords and processor
names come from the installed libraries; an unclassified upstream addition fails the gate.

Read from: web-poet 0.24.1, zyte-common-items 0.29.0 (both pinned in `requirements-test.txt`, so this snapshot moves only when a pin does).

## Page / extractor base classes

| web-poet class | Frostwork counterpart | if declined, why |
|---|---|---|
| `BrowserPage` | `FrostBrowserPage` |  |
| `Extractor` | — | non-injectable field bundle; use `FrostFields` for an injectable `ItemPage` |
| `Injectable` | — | marker base for dependency injection, not an extraction surface |
| `ItemPage` | `FrostFields` |  |
| `Returns` | — | generic item-class mixin, composed with any base |
| `SelectorExtractor` | — | input is an existing parsel.Selector; serializing and rescanning it adds work and can change source |
| `WebPage` | `FrostPage` |  |

## `web_poet.field` keyword surface

Forwarded verbatim by `frostwork.webpoet.field`, so a declaration built by Frostwork is not a
second-class `web_poet.field`.

| keyword | forwarded | if declined, why |
|---|---|---|
| `cached` | yes |  |
| `meta` | yes |  |
| `out` | yes |  |

## zyte-common-items field processors

Each supported processor has a generated case in `make gate-webpoet`, compared with Parsel.

| processor | covered | if declined, why |
|---|---|---|
| `brand_processor` | yes |  |
| `breadcrumbs_processor` | yes |  |
| `description_html_processor` | yes |  |
| `description_processor` | yes |  |
| `gtin_processor` | yes |  |
| `images_processor` | yes |  |
| `metadata_processor` | no | operates on an item's metadata object, not on a selector's value |
| `only_handle_nodes` | no | a DECORATOR used to build processors, not a processor |
| `price_processor` | yes |  |
| `probability_request_list_processor` | no | takes a Request list (one argument), not a field value |
| `rating_processor` | yes |  |
| `simple_price_processor` | yes |  |

## Value types a processor can be handed

A bare-element field with a processor must choose `.as_node()` or `.as_value()`; Frostwork does
not infer the representation from the processor.

| type | Frostwork can produce | from |
|---|---|---|
| `str` | yes | a `::text` / `::attr()` terminal, or a bare element declared with `.as_value()` |
| `list[str]` | yes | `all=True` on a scalar terminal — what images_processor consumes |
| `parsel.Selector` | yes | a bare-element field declared with `.as_node()` |
| `parsel.SelectorList` | yes | the same with `all=True` |
| `lxml.html.HtmlElement` | via Selector | processors accept `.root`; handed over as a Selector |
| `dict` | no | rating_processor's dict form composes sub-values; write it as a @web_poet.field |
