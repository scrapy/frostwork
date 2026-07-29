# Representative selector support snapshot

Generated/verified by `tools/support_snapshot.py` against the real compiler. The exhaustive
contract remains in `COMPATIBILITY.md`; this table is a drift tripwire for headline features.

| feature | context | selector | supported |
|---|---|---|---|
| CSS compound | flat | `div.card[data-x]::text` | yes |
| CSS sibling | flat | `dt + dd::text` | yes |
| CSS reverse position | flat | `li:last-child::text` | yes |
| CSS :has | flat | `div:has(a)::text` | yes |
| CSS widened :has | flat | `div:has([data-x])::attr(id)` | yes |
| CSS :is | flat | `div:is(.a, .b)::text` | yes |
| XPath union | flat | `//dt/text() \| //dd/text()` | yes |
| XPath following sibling | flat | `//dt/following-sibling::dd/text()` | yes |
| XPath upward | flat | `//a/ancestor::div/@id` | yes |
| XPath text predicate | flat | `//p[contains(.,"x")]/text()` | yes |
| XPath normalize-space | flat | `normalize-space(//h1)` | yes |
| XPath variable reference | flat | `//*[@id=$pid]` | no |
| XPath unquoted operand | flat | `//span[@x=2]/text()` | no |
| Grouped basic container | container | `.card` | yes |
| Grouped comma container | container | `div, span` | no |
| Grouped deferred container | container | `div:has(a)` | no |
| Grouped descendant sub-field | sub-field | `.//a/@href` | yes |
| Grouped comma sub-field | sub-field | `p::text, a::text` | no |
| Grouped deferred sub-field | sub-field | `p:has(a)::text` | no |
