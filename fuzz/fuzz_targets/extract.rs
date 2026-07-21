#![no_main]
//! Robustness fuzz target (docs/TESTING.md L4): arbitrary bytes in → `extract` / `extract_grouped`
//! must NEVER panic, hang, or read out of bounds. Values may be anything; the only invariant is
//! "no crash". The basket spans the desync-critical + value-extraction surface (rawtext via
//! `script`, comments, entities, encoding sniff, combinators, XPath, comma groups, grouped One/Many).

use libfuzzer_sys::fuzz_target;

const BASKET: &[&str] = &[
    "div ::text",
    "a::attr(href)",
    "li + li::text",
    ".x > .y::text",
    "//a/@href",
    "h1::text, h2::text",
    "[data-k*=\"v\"]::text",
    "p:not(.z)::text",
    "img",
    "//div[@class=\"c\"]//text()",
];

fuzz_target!(|data: &[u8]| {
    let queries: Vec<String> = BASKET.iter().map(|s| (*s).to_string()).collect();
    // flat, sniffed encoding (exercises the BOM / <meta> prescan path on arbitrary bytes)
    let _ = frostwork::extract(data, &queries, None);
    // flat, an explicit legacy label (exercises the transcode-value path)
    let _ = frostwork::extract(data, &queries, Some("windows-1252"));
    // grouped One/Many: containers + multi-part / void subs over the same bytes
    let g = frostwork::GroupQuery {
        container: ".card".to_string(),
        subfields: vec![
            ("t".to_string(), "h3 a::text".to_string()),
            ("u".to_string(), "img::attr(src)".to_string()),
            ("h".to_string(), ".//a/@href".to_string()),
        ],
    };
    let _ = frostwork::extract_grouped(data, &queries, std::slice::from_ref(&g), None);
});
