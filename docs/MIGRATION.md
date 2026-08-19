# Migrating a Parsel page object

Frostwork is best introduced as an audited hot path, not as an ad-hoc replacement for every
`.css()`/`.xpath()` call.

1. Collect the selectors used to build one item — `frostwork-audit --scan myproject/spiders/` mines them
   straight out of the source (inline `.css()`/`.xpath()`, `ItemLoader.add_*`,
   `LinkExtractor(restrict_*)`) without importing anything, and prints a supported/unsupported verdict
   per `file:line`. That is the coverage number for the migration, before any rewrite.
2. Run `frostwork.check(selectors)` or declare them on `Page` and call `page.check()`.
3. Keep supported selectors together so Frostwork answers them in one pass.
4. Rewrite simple unsupported forms using the audit suggestion where possible; keep genuinely
   tree-dependent queries on Parsel.
5. Cross-check representative pages before switching the production path.

```python
from parsel import Selector
from frostwork import Page

page = (
    Page()
    .field("title", "h1::text")
    .field_all("images", "img::attr(src)")
)
page.check().raise_for_status()

frost = page.extract(body).to_dict()
parsel = Selector(body=body, encoding="utf-8")
assert frost["title"] == parsel.css("h1::text").get()
assert frost["images"] == parsel.css("img::attr(src)").getall()
```

Common rewrites:

| Parsel shape | Frostwork approach |
|---|---|
| repeated `selector.css(field)` inside cards | `Page.many(container, subfields)` |
| `./child` or a bare `td/text()` in a grouped XPath | use `.//descendant` when descendant scope is acceptable (this is the single most common un-portable shape) |
| `normalize-space(path)` | supported as a flat scalar field |
| `sel.xpath('//*[@id=$pid]', pid=…)` (XPath variables) | inline the value as a quoted literal — Frostwork takes no variable bindings, so `$pid` is unsupported |
| arbitrary reverse/parent traversal | keep on Parsel unless listed in `COMPATIBILITY.md` |

Run `frostwork-audit myproject.pages --json` in CI so future selector edits cannot silently move a
field outside the supported subset, and `frostwork-audit --scan` over the spiders so un-ported selectors
stay visible.

Once a page object is live, `Item.empty_fields()` gives the other half of the picture: with the schema
already audited as supported, a field that comes back empty means the *page* changed, not that Frostwork
lacks the selector (see [PYTHON.md](PYTHON.md#dead-selector-or-coverage-gap--itemempty_fields)).
