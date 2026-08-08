# Representative selector support snapshot

Generated/verified by `tools/support_snapshot.py` against the real compiler. The exhaustive
contract remains in `COMPATIBILITY.md`; this table is a drift tripwire for headline features.

| feature | context | selector | supported |
|---|---|---|---|
| CSS compound | flat | `div.card[data-x]::text` | yes |
| CSS sibling | flat | `dt + dd::text` | yes |
| CSS reverse position | flat | `li:last-child::text` | yes |
| CSS reverse subtree | flat | `li:last-child ::text` | yes |
| CSS reverse on ancestor | flat | `li:last-child b::text` | yes |
| CSS reverse child tail | flat | `li:last-child > b::text` | no |
| CSS :has subtree | flat | `div:has(a) ::text` | yes |
| CSS :has on ancestor | flat | `div:has(a) a::attr(href)` | yes |
| XPath text-pred descendant | flat | `//div[contains(.,"x")]//a/@href` | yes |
| CSS :contains | flat | `p:contains("x")::text` | yes |
| CSS :contains sibling | flat | `dt:contains("x") + dd::text` | yes |
| CSS :contains descendant | flat | `div:contains("x") a::attr(href)` | yes |
| CSS :contains doubled | flat | `p:contains("a"):contains("b")::text` | no |
| CSS :contains non-string arg | flat | `p:contains(2)::text` | no |
| CSS implicit subject after + | flat | `dt + ::text` | yes |
| CSS implicit subject after > | flat | `div > ::attr(id)` | yes |
| CSS dangling combinator | flat | `dt +` | no |
| XPath reverse subtree | flat | `//li[last()]//text()` | yes |
| CSS :has | flat | `div:has(a)::text` | yes |
| CSS widened :has | flat | `div:has([data-x])::attr(id)` | yes |
| CSS :has list | flat | `div:has(a, img)::attr(id)` | yes |
| CSS :has mixed-rel list | flat | `div:has(> a, img)::attr(id)` | no |
| CSS :not list | flat | `p:not(.a, .b)::text` | yes |
| CSS :not empty member | flat | `p:not(.a, )::text` | no |
| CSS attr case flag | flat | `[type=submit i]::attr(id)` | yes |
| CSS attr bogus flag | flat | `[type=submit x]::attr(id)` | no |
| CSS :is | flat | `div:is(.a, .b)::text` | yes |
| CSS quoted delimiter | flat | `div:is(#a, [data-x=")"])::attr(id)` | yes |
| CSS unterminated pseudo | flat | `div:is(#a, [data-x=")"]::attr(id)` | no |
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
