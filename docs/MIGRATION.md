# Migrating a Scrapy extraction schema

Start with [frostwork-demo](https://github.com/shaneaevans/frostwork-demo) for a runnable crawl. Use the
[Python guide](PYTHON.md) for the APIs and [compatibility contract](COMPATIBILITY.md) for selector limits.
For an existing spider, work through one complete callback or page schema at a time.

1. Run `frostwork-audit --scan myproject/spiders/` to find literal selector gaps without importing the
   spider. Dynamic selectors are unknown; aliases and custom wrappers can be invisible to the scanner.
   Add `--require-complete` in CI if a partial scan must fail.
2. Declare a reusable `Page`, or use `FrostPage` with scrapy-poet. Use `field_all` whenever the callback
   consumes all matches. Audit the complete schema with `frostwork-audit myproject/pages.py` to check
   group context and shared selector budgets.
3. Save representative response **bytes**, the resolved encoding and URL. Include missing optional
   content, multiple repeated rows, nested markup and the encodings your spider actually receives.
4. Compare every field and the whole item on those saved responses. Only then measure extraction time,
   and finally run the callback through a real Scrapy crawl to measure downloader and pipeline costs.

## Declare the schema

Keep selectors and pure transforms in a module that can be imported without starting a crawl. For
example, save this as `myproject/pages.py`:

<!-- doc-test: migration-schema -->
```python
from frostwork import Page

PRODUCT = (Page()
           .field("name", "h1::text")
           .field("price", ".price::text")
           .field_all("images", "img::attr(src)"))
REGISTRY = {"product": PRODUCT}

assert PRODUCT.check().ok
```

`frostwork-audit myproject/pages.py` checks selector support. The saved-response comparison below also
checks values. Its reference uses the same declared selectors and transforms in Parsel; when porting
an existing callback, separately compare its original output to catch mistakes in the translation itself.

## Verify saved responses

The repository includes a comparison tool and self-authored fixtures under `tests/migration/`, covered
by the repository license. The fixtures are small, inspectable examples, not a sample of the real web.
From the Frostwork checkout, install the test dependencies once and run either check:

```bash
make bootstrap
make verify-migration
make bench-migration
```

These commands write `target/migration-report.json`. The benchmark compiles Frostwork's plans before
timing, alternates the order of Frostwork and Parsel runs, and reports samples, medians and spread for
complete extraction plus cardinality shaping and transforms. It excludes file I/O, downloading,
ItemLoaders and pipelines. It does not measure memory; use the separate memory harness described in
[BENCHMARKS.md](BENCHMARKS.md). A manual **migration benchmark** GitHub Actions workflow runs the same
fixtures on Linux and uploads the report; its shared runner timings are indicative.

To check your own `Page` schemas:

```bash
.venv/bin/python tools/verify_migration.py myproject/pages.py:REGISTRY responses/manifest.json \
  --json migration-report.json
```

`REGISTRY` is a dictionary of schema names to `Page` instances, or point at a single `Page` instead.
The command imports that module and executes its transforms. Use an import-safe schema module, and
transforms that are deterministic and have no side effects. Values must be JSON-compatible.
The tool is repository tooling; it is not installed as a separate package command.

The response manifest names the schema for each page. Paths are relative to the manifest. `encoding`
is the response's resolved encoding; omit it only to test Frostwork sniffing against Parsel's default
UTF-8. A hash mismatch is an input error, so changed fixtures cannot silently reuse old provenance:

```json
{
  "version": 1,
  "responses": [
    {
      "schema": "product",
      "file": "product.html",
      "encoding": "windows-1252",
      "url": "https://example.invalid/product",
      "sha256": "SHA256_OF_THE_ORIGINAL_BODY_BYTES"
    }
  ]
}
```

Use `hashlib.sha256(response.body).hexdigest()` when saving the body. Keep capture provenance and
licensing information alongside it. Do not decode and re-encode a saved response to make a check pass.

A schema counts as verified only when it is supported, has at least one saved response and agrees on
**every whole item** and every retained raw flat column tested for it. A transform cannot hide a raw
value loss. Group subfields are compared after their declared cardinality shaping.

| Report result | Meaning |
| --- | --- |
| Whole-item or raw-column difference | Fails, including missing keys, extra values, changed order, empty strings or whitespace |
| Raw HTML serialization difference | Fails here, even if the engine's documented raw-source allowance accepts it; the engine verdict is included for context |
| Oracle error, unsupported schema or schema without a response | Unverified; cannot count as agreement |
| Empty field on both sides | Agreement for this response, reported in `empty_fields`; add a fixture where a required selector matches |

Add `--benchmark --rounds 7 --iterations 100` after parity succeeds. If any complete schema fails,
nothing is timed. A response with no matches is excluded from extraction timing. The report records
body and manifest hashes, the schema module hash, native extension hash, Git revision and dirty state,
Python/package/libxml2 versions, and machine details. Imported helper modules are not individually
hashed: retain the complete project revision and environment when sharing a measurement.

This tool compares a `Page` to Parsel with the same selectors and transforms. It does not port callback
logic or verify ItemLoaders, web-poet output processors, URL joining, requests or pipelines. The
[demo](https://github.com/shaneaevans/frostwork-demo) covers the crawl path; Frostwork's web-poet differential
checks the integration against its own oracle.

## Relative selectors inside repeated rows

Use `./` when the relation really is an immediate child. Replacing it with `.//` changes the meaning
and can pick up a nested recommendation or an inner card:

<!-- doc-test: group-context -->
```python
from frostwork import Page

products = Page().many("products", "article", {
    "id": "@id",                         # the article's attribute
    "name": "./h2/text()",                # its immediate heading
    "tags": ("./ul/li/text()", "all"),
    "text": (".//text()", "join", ""),    # its string value, including nested text
})

item = products.extract(b'<article id="mug"><h2>Mug</h2><aside><h2>Related</h2></aside></article>')
assert item.to_dict() == {"products": [{
    "id": "mug", "name": "Mug", "tags": [], "text": "MugRelated",
}]}
```

`text()` reads the container's own text nodes; `.//text()` reads its full subtree. `.` returns its raw
source HTML. These context forms work in grouped subfields; a flat `./child` remains unsupported.
Use scalar text and attribute fields when a processor needs values. `.as_node()` intentionally reparses
matched HTML for node-oriented web-poet processors and has a different performance boundary.

Other common translations:

| Parsel use | Frostwork declaration |
| --- | --- |
| `.get()` | `Page.field` |
| `.getall()` | `Page.field_all` |
| `normalize-space(path)` | supported as a flat scalar field |
| `.xpath(query, variable=value)` | variable bindings remain unsupported; validate a concrete selector before using it |
| arbitrary parent or reverse traversal | check the exact shape against the compatibility contract |

Keep tree-dependent queries in the existing implementation until the entire schema can be expressed
faithfully. There is no automatic parser fallback. Re-run the schema audit in CI when selectors change.

## Monitoring a running spider

`Page.extract_response(response)` passes `response.body` and `response.encoding` through directly.
After extraction, `item.validate(required=..., counts=..., group_required=...)` checks your data contract.
Use `report.record_stats(self.crawler.stats)` for Scrapy counters, then inspect `report.ok` or call
`report.raise_for_status()`. Yield `report.item` after validation to reuse the already processed values.

A missing optional field is not necessarily a broken selector. Keep requirements specific to the item
contract, and use saved responses to investigate changes. Group requirements check existing rows;
include the group itself in `required` when at least one row must exist.
