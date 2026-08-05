# web-poet integration surface

Generated/verified by `tools/webpoet_surface.py` **from the installed libraries** — not written by
hand. Every name below was read out of `web_poet` / `zyte_common_items.processors` by
introspection, and a name that appears upstream and in neither list fails the gate. Five defects
came from hand-written versions of these four tables; see `docs/PYTHON.md` for how to use the
supported entries and `AGENTS.md` for why the universe is derived rather than transcribed.

Read from: web-poet 0.24.1, zyte-common-items 0.29.0 (both pinned in `requirements-test.txt`, so this snapshot moves only when a pin does).

## Page / extractor base classes

| web-poet class | Frostwork counterpart | if declined, why |
|---|---|---|
| `BrowserPage` | `FrostBrowserPage` |  |
| `Extractor` | — | field support WITHOUT `Injectable`, for a bundle composed into a page (as `SelectorExtractor` is). `FrostFields` is the `ItemPage` form — the same support plus injectability, which scrapy-poet requires: andi silently drops a callback argument whose class `is_injectable()` rejects. |
| `Injectable` | — | marker base for dependency injection, not an extraction surface |
| `ItemPage` | `FrostFields` |  |
| `Returns` | — | generic item-class mixin, composed with any base |
| `SelectorExtractor` | — | input is a parsel.Selector — a tree lxml already built. Scanning it would mean serializing that tree back to markup and re-parsing it, which can disagree with both the original bytes and the Selector; and with a tree already built there is nothing left to save. |
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

Each supported processor is exercised against parsel by `make gate-webpoet`, on generated markup
the processor can actually parse (the run prints how many pairs carried a non-empty expected
value, because a processor returning `None` on both sides proves nothing).

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

The `isinstance` gates that decide whether a processor transforms its input or returns it
unchanged. `str` used to be the only type Frostwork produced, which is exactly why a node-taking
processor silently passed raw HTML through into a typed field.

| type | Frostwork can produce | from |
|---|---|---|
| `str` | yes | a `::text` / `::attr()` terminal, or a bare element with no processor (raw source) |
| `list[str]` | yes | `all=True` on a scalar terminal — what images_processor consumes |
| `parsel.Selector` | yes | a bare-element field with a processor attached: raw source re-parsed |
| `parsel.SelectorList` | yes | the same, `all=True` |
| `lxml.html.HtmlElement` | via Selector | processors accept `.root`; handed over as a Selector |
| `dict` | no | rating_processor's dict form composes sub-values; write it as a @web_poet.field |
