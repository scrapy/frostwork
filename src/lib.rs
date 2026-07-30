//! Frostwork: a treeless, one-pass HTML extraction engine — a bespoke tokenizer + corrected-stack
//! matcher, "close to lxml, always streams, no fallback". See docs/COMPATIBILITY.md and docs/TESTING.md.
//!
//! Pipeline:  bytes ─▶ tokenizer (TokenSink events) ─▶ corrected-stack matcher ─▶ per-selector cols.
//! The tokenizer is deliberately swappable behind `TokenSink`; the matcher is where implied-close
//! tree-construction lives.

use std::borrow::Cow;

mod diagnostics;
mod encoding;
mod entities;
mod implied_close;
mod matcher;
mod page;
#[cfg(feature = "python")]
mod python;
pub mod selector;
mod tokenizer;
mod xpath;

pub use matcher::{MAX_MEMBERS, MAX_SIB_BITS};
pub use page::{CompiledPage, Field, Item, Page};
pub use selector::Selector;

/// One value-column per flat query — the output of [`extract`].
pub type FlatColumns = Vec<Vec<String>>;
/// A grouped (`Many`/`One`) query's rows: indexed `[row][subfield][value]`.
pub type GroupRows = Vec<Vec<Vec<String>>>;

/// Extract each query's values in one streaming pass. Unsupported queries yield an empty column
/// (there is no fallback — that is the whole point of Frostwork). Values match Parsel's per-text-node,
/// whitespace-kept, entity-decoded semantics on the supported subset.
/// Extract each query's values. `encoding` is an optional caller/HTTP charset label (as Scrapy
/// passes); when `None` the encoding is sniffed (BOM -> `<meta>` -> UTF-8). Structural tokenization
/// runs on raw bytes for every ASCII-compatible encoding; only the small emitted values are decoded
/// with the resolved encoding. UTF-16LE/BE are transcoded to UTF-8 up front (rare).
pub fn extract(html: &[u8], queries: &[String], encoding: Option<&str>) -> Vec<Vec<String>> {
    extract_grouped(html, queries, &[], encoding).0
}

/// A `Many`/`One` grouped query: for every element matching `container`, extract each `subfields`
/// selector **scoped to that element** (descendant-or-self), all in the same streaming pass. The
/// subfield names are carried for the caller's convenience; the engine keys sub-columns positionally.
#[derive(Clone, Debug)]
pub struct GroupQuery {
    pub container: String,
    pub subfields: Vec<(String, String)>, // (name, selector)
}

/// Is `qt` an XPath query (absolute/`.`-rooted path, or a `normalize-space(...)` wrapper) rather than
/// CSS? The shared routing rule for [`compile_query`] / [`compile_one`].
fn is_xpath(qt: &str) -> bool {
    qt.starts_with('/') || qt.starts_with("./") || qt.starts_with("normalize-space(")
}

/// Compile a single query string to one `Selector` (CSS first-member, or downward XPath); `None` if
/// unsupported. Used for group containers and sub-fields (MVP: no comma groups there). A
/// `normalize-space(...)` value is rejected here — it is a flat-query terminal, not a container/
/// sub-field selector (the grouped paths don't route its scalar output).
fn compile_one(q: &str) -> Option<Selector> {
    let qt = q.trim();
    if is_xpath(qt) {
        match xpath::compile(qt) {
            Some(s) if matches!(s.terminal, selector::Terminal::NormalizeSpace(_)) => None,
            other => other,
        }
    } else {
        let mut members = selector::parse_list(q);
        // Group containers/sub-fields accept exactly ONE selector. Taking `.next()` here used to
        // execute the first member of `div, span` and silently discard the rest: a partial, wrong
        // answer that violated the no-fallback contract. Multi-member CSS is therefore unsupported
        // in this shape, exactly like multi-member XPath unions/or-expansions.
        (members.len() == 1).then(|| members.pop()).flatten()
    }
}

/// Compile a flat query string to its member selectors (all sharing one output column); empty if
/// unsupported. CSS comma groups and XPath unions / `or`-expansions both yield multiple members. The
/// shared front-end for [`extract_grouped`] and [`budget_usage`].
fn compile_query(q: &str) -> Vec<Selector> {
    let qt = q.trim();
    // XPath (absolute / `.`-rooted path, or a `normalize-space(...)` wrapper) compiles to the same
    // Selector model; else CSS.
    if is_xpath(qt) {
        xpath::compile_members(qt)
    } else {
        selector::parse_list(q)
    }
}

/// The `(member-selector, sibling-bit)` demand of a schema. A caller
/// that would rather fail loud than get silently-empty columns compares this against
/// [`MAX_MEMBERS`] / [`MAX_SIB_BITS`]; the Python binding raises `ValueError`.
pub fn budget_usage(queries: &[String], groups: &[GroupQuery]) -> (usize, usize) {
    let queries_sel: Vec<Vec<Selector>> = queries.iter().map(|q| compile_query(q)).collect();
    let compiled_groups: Vec<matcher::GroupInput> = groups
        .iter()
        .map(|g| (compile_one(&g.container), g.subfields.iter().map(|(_, sel)| compile_one(sel)).collect()))
        .collect();
    matcher::budget_usage(&queries_sel, &compiled_groups)
}

// ---------------------------------------------------------------- schema audit (no-fallback safety)
//
// The no-fallback contract makes an unsupported selector look identical to a legitimately-empty field
// at the scraper layer. These functions let a caller AUDIT a schema up front — before it silently
// yields empty columns in production — turning "unsupported selector" into an explicit, explainable
// signal. The supported/unsupported DECISION is authoritative (it is the real compiler); the reason is
// advisory (see [`diagnostics`]).

/// Whether a selector is supported, and if not, a best-effort reason. See [`audit_schema`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Support {
    Supported,
    /// Unsupported — the query yields an empty column. The string is an advisory explanation.
    Unsupported(String),
}

impl Support {
    pub fn is_supported(&self) -> bool {
        matches!(self, Support::Supported)
    }
    pub fn reason(&self) -> Option<&str> {
        match self {
            Support::Supported => None,
            Support::Unsupported(r) => Some(r),
        }
    }
}

/// Support of one flat query (comma groups allowed, as [`extract`] accepts). The verdict is the REAL
/// compiler's: a reverse selector parses but is routed/dropped inside `CompiledSchema::compile` (a
/// subtree/comma-grouped/ancestor reverse yields an empty column), so we ask the compiled schema whether
/// column 0 ended up live rather than trusting parse success.
pub fn flat_query_support(q: &str) -> Support {
    let members = compile_query(q);
    let supported = !members.is_empty()
        && matcher::CompiledSchema::compile(std::slice::from_ref(&members), &[]).flat_col_supported(0);
    if supported {
        Support::Supported
    } else {
        Support::Unsupported(diagnostics::reason(q))
    }
}

/// Support of a group CONTAINER selector (single CSS/XPath selector, no comma group). A reverse position
/// on a container is out of scope (the group would never open), so reject it.
pub fn container_support(q: &str) -> Support {
    let input: Vec<matcher::GroupInput> = vec![(compile_one(q), Vec::new())];
    let schema = matcher::CompiledSchema::compile(&[], &input);
    if schema.group_container_routed(0) {
        Support::Supported
    } else {
        Support::Unsupported(group_container_reason(q))
    }
}

fn group_container_reason(q: &str) -> String {
    if compile_one(q).is_some() {
        "selector requires deferred-close matching, which is unsupported for a grouped container \
         (empty group)"
            .to_string()
    } else {
        diagnostics::reason(q)
    }
}

/// Support of a grouped SUB-FIELD selector. Stricter than a container: a sub-field must be a single
/// segment (no sibling `+`/`~`), matching the `Many`/`One` MVP (see `matcher::SubSel`); a reverse
/// position inside a group is also out of scope (empty column).
pub fn subfield_support(q: &str) -> Support {
    let container = compile_one("*");
    let input: Vec<matcher::GroupInput> = vec![(container, vec![compile_one(q)])];
    let schema = matcher::CompiledSchema::compile(&[], &input);
    if schema.group_sub_routed(0, 0) {
        Support::Supported
    } else {
        Support::Unsupported(group_sub_reason(q))
    }
}

fn group_sub_reason(q: &str) -> String {
    if q.contains('+') || q.contains('~') {
        "sibling combinator (`+`/`~`) inside a grouped sub-field is unsupported (empty column)"
            .to_string()
    } else if compile_one(q).is_some() {
        "selector requires deferred-close matching, which is unsupported inside a grouped sub-field \
         (empty column)"
            .to_string()
    } else {
        diagnostics::reason(q)
    }
}

/// A grouped query's support: its container plus each sub-field (aligned to `subfields` order).
#[derive(Clone, Debug)]
pub struct GroupAudit {
    pub container: Support,
    pub subfields: Vec<Support>,
}

/// The result of auditing a whole schema: per-selector support plus budget usage. `over_budget` (too
/// many member selectors / sibling bits) is a distinct failure from an unsupported selector — it is a
/// caller bug (the schema is too large), whereas unsupported is a coverage gap.
#[derive(Clone, Debug)]
pub struct SchemaAudit {
    pub flat: Vec<Support>,
    pub groups: Vec<GroupAudit>,
    pub members: usize,
    pub max_members: usize,
    pub sib_bits: usize,
    pub max_sib_bits: usize,
}

impl SchemaAudit {
    pub fn over_budget(&self) -> bool {
        self.members > self.max_members || self.sib_bits > self.max_sib_bits
    }
    /// True iff every selector is supported AND the schema fits the budget.
    pub fn ok(&self) -> bool {
        !self.over_budget()
            && self.flat.iter().all(Support::is_supported)
            && self.groups.iter().all(|g| {
                g.container.is_supported() && g.subfields.iter().all(Support::is_supported)
            })
    }
}

/// Audit a schema (same shape [`extract_grouped`] accepts) without touching any HTML: report which
/// selectors are supported (with advisory reasons for those that are not) and the budget usage.
pub fn audit_schema(queries: &[String], groups: &[GroupQuery]) -> SchemaAudit {
    // Compile ONCE and derive every support verdict from the exact routes extraction will use. This
    // prevents the audit/strict-mode logic from drifting from matcher eligibility rules (notably the
    // deferred `:has` / text-predicate exclusions in grouped containers and sub-fields).
    let queries_sel: Vec<Vec<Selector>> = queries.iter().map(|q| compile_query(q)).collect();
    let compiled_groups: Vec<matcher::GroupInput> = groups
        .iter()
        .map(|g| (compile_one(&g.container), g.subfields.iter().map(|(_, sel)| compile_one(sel)).collect()))
        .collect();
    let (members, sib_bits) = matcher::budget_usage(&queries_sel, &compiled_groups);
    let schema = matcher::CompiledSchema::compile(&queries_sel, &compiled_groups);
    SchemaAudit {
        flat: queries
            .iter()
            .enumerate()
            .map(|(i, q)| {
                if schema.flat_col_routed(i) {
                    Support::Supported
                } else {
                    Support::Unsupported(diagnostics::reason(q))
                }
            })
            .collect(),
        groups: groups
            .iter()
            .enumerate()
            .map(|(gi, g)| GroupAudit {
                container: if schema.group_container_routed(gi) {
                    Support::Supported
                } else {
                    Support::Unsupported(group_container_reason(&g.container))
                },
                subfields: g
                    .subfields
                    .iter()
                    .enumerate()
                    .map(|(si, (_, sel))| {
                        if schema.group_sub_routed(gi, si) {
                            Support::Supported
                        } else {
                            Support::Unsupported(group_sub_reason(sel))
                        }
                    })
                    .collect(),
            })
            .collect(),
        members,
        max_members: MAX_MEMBERS,
        sib_bits,
        max_sib_bits: MAX_SIB_BITS,
    }
}

/// Resolve the page encoding and yield `(bytes_to_tokenize, value_encoding)`. UTF-16 (the only
/// non-ASCII-compatible family) is transcoded to a fresh UTF-8 `Vec` once (owned `Cow`); everything
/// else borrows the input, with a document-leading UTF-8 BOM stripped — libxml2 strips ONLY the
/// leading BOM (a U+FEFF elsewhere is real content), and per-value decoding must not re-strip it (see
/// `matcher::finalize`), so it is handled here, once per page.
fn prepare_bytes<'h>(
    html: &'h [u8],
    encoding: Option<&str>,
) -> (Cow<'h, [u8]>, &'static encoding_rs::Encoding) {
    let enc = encoding::resolve(html, encoding);
    if enc == encoding_rs::UTF_16LE || enc == encoding_rs::UTF_16BE {
        return (Cow::Owned(enc.decode(html).0.into_owned().into_bytes()), encoding_rs::UTF_8);
    }
    let bytes = if enc == encoding_rs::UTF_8 && html.starts_with(&[0xEF, 0xBB, 0xBF]) {
        &html[3..]
    } else {
        html
    };
    (Cow::Borrowed(bytes), enc)
}

/// A schema compiled ONCE, reusable across any number of pages — the compile-once/extract-many form of
/// [`extract_grouped`]. Building a `Plan` parses every selector, lowers it to the matcher's internal
/// form, and computes the interesting-attribute set a single time; each [`Plan::extract`] then only
/// resolves the page's encoding and runs the streaming pass. For a `Page`/`FrostPage` run over many
/// responses this removes the per-page recompile — the natural shape, since a schema is defined once
/// and applied to every page. Unsupported selectors compile to empty columns exactly as one-shot
/// [`extract`] does (no fallback); the flat/grouped output is identical.
pub struct Plan {
    schema: matcher::CompiledSchema,
    budget: (usize, usize), // (members, sibling-bits) raw demand, computed once at compile
}

impl Plan {
    /// Compile `queries` + `groups` (the same shapes [`extract_grouped`] accepts) once for reuse.
    pub fn compile(queries: &[String], groups: &[GroupQuery]) -> Plan {
        let queries_sel: Vec<Vec<Selector>> = queries.iter().map(|q| compile_query(q)).collect();
        let compiled_groups: Vec<matcher::GroupInput> = groups
            .iter()
            .map(|g| {
                (compile_one(&g.container), g.subfields.iter().map(|(_, sel)| compile_one(sel)).collect())
            })
            .collect();
        let budget = matcher::budget_usage(&queries_sel, &compiled_groups);
        Plan { schema: matcher::CompiledSchema::compile(&queries_sel, &compiled_groups), budget }
    }

    /// The `(member, sibling-bit)` budget demand of this plan — see [`budget_usage`]. Computed at
    /// compile time; a caller can reject an over-budget plan once, up front.
    pub fn budget_usage(&self) -> (usize, usize) {
        self.budget
    }

    /// Run the compiled schema over one page. `encoding` is an optional charset label (as Scrapy
    /// passes from `Content-Type`); `None` sniffs (BOM → `<meta>` → UTF-8). Returns `(flat_columns,
    /// grouped)` — byte-identical to [`extract_grouped`] with the same schema.
    pub fn extract(&self, html: &[u8], encoding: Option<&str>) -> (FlatColumns, Vec<GroupRows>) {
        let (bytes, value_enc) = prepare_bytes(html, encoding);
        self.schema.run(&bytes, value_enc)
    }
}

/// One streaming pass returning both flat query columns and grouped (`Many`/`One`) results.
/// `grouped[g]` is group `g`'s rows in document order; each row is one value-column per sub-field
/// (`[group][row][subfield][value]`). The caller (the Python/Rust `Page` layer) applies per-field
/// cardinality (first/all/join) and, for `One`, takes the first row. This is the one-shot form; to run
/// the same selectors over many pages, compile a [`Plan`] once and reuse it.
pub fn extract_grouped(
    html: &[u8],
    queries: &[String],
    groups: &[GroupQuery],
    encoding: Option<&str>,
) -> (FlatColumns, Vec<GroupRows>) {
    Plan::compile(queries, groups).extract(html, encoding)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ex(html: &str, q: &str) -> Vec<String> {
        extract(html.as_bytes(), &[q.to_string()], None).pop().unwrap()
    }
    fn v(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }

    /// One grouped query -> its rows; each row is one value-column per sub-field.
    fn grp(html: &str, container: &str, subs: &[&str]) -> Vec<Vec<Vec<String>>> {
        let g = GroupQuery {
            container: container.to_string(),
            subfields: subs.iter().map(|s| (s.to_string(), s.to_string())).collect(),
        };
        extract_grouped(html.as_bytes(), &[], &[g], None).1.pop().unwrap()
    }

    #[test]
    fn nonsubject_sibling_predicate_case_b() {
        // Case B: a deferred predicate on a PRECEDING sibling, value from the later sibling.
        let html = "<dl><dt>Price</dt><dd>$10</dd><dt>Size</dt><dd>L</dd><dt>Price</dt><dd>$20</dd></dl>";
        // `dt[.="Price"] ~ dd` = every dd with a preceding-sibling Price dt (document order, deduped)
        assert_eq!(ex(html, "//dt[.=\"Price\"]/following-sibling::dd/text()"), v(&["$10", "L", "$20"]));
        // adjacent `+`: only the dd immediately after each Price dt
        assert_eq!(ex(html, "//dt[.=\"Price\"]/following-sibling::dd[1]/text()"), v(&[])); // positional on axis: gap
        // predicate that never holds -> no sibling fires (empty, never a stale value)
        assert_eq!(ex(html, "//dt[.=\"Nope\"]/following-sibling::dd/text()"), v(&[]));
        // CSS `:has` on a preceding sibling
        let cards = "<ul><li class=\"r\"><span class=\"new\">A</span></li><li>a1</li><li class=\"r\">B</li><li>a2</li></ul>";
        assert_eq!(ex(cards, "li:has(.new) ~ li::text"), v(&["a1", "B", "a2"])); // all following li (direct text)
        assert_eq!(ex(cards, "li:has(.new) + li::text"), v(&["a1"])); // adjacent: only the immediate next
        assert_eq!(ex(cards, "li:has(.absent) ~ li::text"), v(&[])); // :has fails -> nothing
    }

    #[test]
    fn audit_schema_reports_support_and_budget() {
        let queries = vec![
            "h1::text".to_string(),        // supported
            "div:has(.a .b)::text".to_string(), // unsupported :has form (chain inner; `:has(a)` IS supported)
            "//a[position()<2]/@href".to_string(), // unsupported (range position, not a sole `[N]`/`[last()]`)
        ];
        let groups = vec![GroupQuery {
            container: ".card".to_string(),
            subfields: vec![
                ("t".to_string(), ".//h3/text()".to_string()), // supported
                ("bad".to_string(), "a + b::text".to_string()), // unsupported sub (sibling)
                ("child".to_string(), "./h3/text()".to_string()), // unsupported (relative child)
            ],
        }];
        let a = audit_schema(&queries, &groups);
        assert!(a.flat[0].is_supported());
        assert!(a.flat[1].reason().unwrap().contains(":has()"));
        assert!(a.flat[2].reason().unwrap().contains("positional"));
        assert!(a.groups[0].container.is_supported());
        assert!(a.groups[0].subfields[0].is_supported());
        assert!(a.groups[0].subfields[1].reason().unwrap().contains("sibling combinator"));
        assert!(a.groups[0].subfields[2].reason().unwrap().contains("descendant"));
        assert!(!a.ok()); // has unsupported members
        assert!(!a.over_budget());
        assert_eq!(a.max_members, matcher::MAX_MEMBERS);
    }

    #[test]
    fn grouped_multi_member_css_is_rejected_whole() {
        let html = "<div><p>P</p><a>A</a></div><span><p>S</p></span>";
        // A grouped container/sub-field accepts one selector only. It must never execute just the
        // first member of a comma list: unsupported means zero rows / an empty cell.
        assert!(grp(html, "div, span", &["p::text"]).is_empty());
        assert_eq!(grp(html, "div", &["p::text, a::text"]), vec![vec![Vec::<String>::new()]]);

        assert!(!container_support("div, span").is_supported());
        assert!(!subfield_support("p::text, a::text").is_supported());
    }

    #[test]
    fn grouped_audit_uses_matcher_routes() {
        let groups = vec![
            GroupQuery {
                container: "div:has(a)".into(),
                subfields: vec![("x".into(), "div::text".into())],
            },
            GroupQuery {
                container: ".root".into(),
                subfields: vec![
                    ("has".into(), "p:has(a)::text".into()),
                    ("text".into(), ".//p[contains(.,\"x\")]/text()".into()),
                ],
            },
        ];
        let audit = audit_schema(&[], &groups);
        assert!(!audit.groups[0].container.is_supported());
        assert!(audit.groups[0].subfields[0].is_supported());
        assert!(audit.groups[1].container.is_supported());
        assert!(audit.groups[1].subfields.iter().all(|s| !s.is_supported()));
        assert!(!audit.ok());
    }

    #[test]
    fn deferred_entries_share_the_128_member_budget() {
        for (html, q, expected) in [
            ("<div id=x><a></a></div>", "div:has(a)::attr(id)", "x"),
            ("<ul><li>x</li></ul>", "li:last-child::text", "x"),
            ("<p>x</p>", "//p[.=\"x\"]/text()", "x"),
        ] {
            let queries = vec![q.to_string(); 65];
            let audit = audit_schema(&queries, &[]);
            assert!(audit.ok(), "{q}: {audit:?}");
            let cols = extract(html.as_bytes(), &queries, None);
            assert_eq!(cols.len(), 65);
            assert!(cols.iter().all(|c| c == &v(&[expected])), "{q}: last={:?}", cols.last());
        }
    }

    #[test]
    fn xpath_union_or_and_descendant_attr_values() {
        // union: document-ordered across both members (parsel `//a/text() | //b/text()`)
        let h = "<html><body><a href=/1>A</a><b>B</b><a href=/2>C</a></body></html>";
        assert_eq!(ex(h, "//a/text() | //b/text()"), v(&["A", "B", "C"]));
        // union node-dedup: an element matched by both members appears once
        assert_eq!(ex("<a>A</a><a>C</a>", "//a | //a").len(), 2);
        // `or` distributed to members, node-deduped (D matches both @x and @y -> once)
        let h2 = "<a x=1>A</a><a y=2>B</a><a z=3>C</a><a x=1 y=2>D</a>";
        assert_eq!(ex(h2, "//a[@x or @y]/text()"), v(&["A", "B", "D"]));
        assert_eq!(ex(h2, "//a[@x or @y or @z]/text()"), v(&["A", "B", "C", "D"]));
        assert_eq!(ex(h2, "//a[@x and @y]/text()"), v(&["D"]));
        // `//X//@a`: descendant-or-self attribute harvest, document order incl. the div's own
        let h3 = "<div id=d href=/self><p><a href=/1>x</a><img src=/i href=/2></p></div>";
        assert_eq!(ex(h3, "//div//@href"), v(&["/self", "/1", "/2"]));
        assert_eq!(ex(h3, "//div/@href"), v(&["/self"])); // child attr = div's own only
        assert_eq!(ex(h3, "//div//@src"), v(&["/i"]));
    }

    #[test]
    fn positional_values() {
        let h = "<ul><li>a</li>t<li>b</li><span>s</span><li>c</li></ul>";
        // :nth-child counts ELEMENT siblings (text ignored); :nth-of-type counts same-tag siblings
        assert_eq!(ex(h, "li:first-child::text"), v(&["a"]));
        assert_eq!(ex(h, "li:nth-child(3)::text"), Vec::<String>::new()); // 3rd child is <span>
        assert_eq!(ex(h, "*:nth-child(2)::text"), v(&["b"])); // 2nd ELEMENT child (text skipped)
        assert_eq!(ex(h, "li:nth-of-type(2)::text"), v(&["b"]));
        assert_eq!(ex(h, "li:nth-child(odd)::text"), v(&["a"])); // odd CHILDREN = 1,3; only li(a) is li
        assert_eq!(ex(h, "li:nth-of-type(odd)::text"), v(&["a", "c"])); // odd LI = 1st,3rd li
        assert_eq!(ex(h, "li:nth-child(-n+2)::text"), v(&["a", "b"])); // child_index 1,2 -> a,b
        // XPath [N]: tag[N] = of-type; *[N] = nth element child; per-parent
        assert_eq!(ex(h, "//li[2]/text()"), v(&["b"]));
        assert_eq!(ex(h, "//ul/*[3]/text()"), v(&["s"]));
        // positional INSIDE :not() must turn the counters on too (regression: it once didn't, so the
        // negation saw index 0 and excluded nothing)
        let li3 = "<ul><li>a</li><li>b</li><li>c</li></ul>";
        assert_eq!(ex(li3, "li:not(:first-child)::text"), v(&["b", "c"]));
        assert_eq!(ex(li3, "li:not(:nth-child(2))::text"), v(&["a", "c"]));
        let two = "<ul><li>a</li><li>b</li></ul><ol><li>x</li><li>y</li></ol>";
        assert_eq!(ex(two, "li:nth-child(2)::text"), v(&["b", "y"])); // per-parent
        assert_eq!(ex(two, "//li[1]/text()"), v(&["a", "x"]));
        assert_eq!(ex(h, "//p[@class=\"x\"][1]/text()"), Vec::<String>::new()); // filtered position: empty
    }

    #[test]
    fn reverse_positional_values() {
        // children of <ul>: li(a), text, li(b), span(s), li(c) — 4 element children, 3 of them <li>.
        let h = "<ul><li>a</li>t<li>b</li><span>s</span><li>c</li></ul>";
        assert_eq!(ex(h, "li:last-child::text"), v(&["c"])); // li(c) is the last child
        assert_eq!(ex(h, "li:last-of-type::text"), v(&["c"])); // and the last <li>
        assert_eq!(ex(h, "span:last-child::text"), Vec::<String>::new()); // span isn't the last child
        assert_eq!(ex(h, "li:only-child::text"), Vec::<String>::new()); // ul has >1 child
        // :only-child / :only-of-type
        let one = "<ul><li>solo</li></ul><ol><li>x</li><b>y</b></ol>";
        assert_eq!(ex(one, "li:only-child::text"), v(&["solo"])); // ul's single child
        assert_eq!(ex(one, "li:only-of-type::text"), v(&["solo", "x"])); // ol's only <li> (b doesn't count)
        // per-parent, and re-sorted to document order across parents that close inner-first
        let two = "<ul><li>a</li><li>b</li></ul><ul><li>c</li></ul>";
        assert_eq!(ex(two, "li:last-child::text"), v(&["b", "c"]));
        let nested = "<ul><li>outer<ul><li>inner</li></ul></li></ul>";
        assert_eq!(ex(nested, "li:last-child::text"), v(&["outer", "inner"])); // doc order, not close order
        // ::attr reverse — the reverse must be on the SUBJECT (rightmost) compound
        let links = "<div><a href='/1'>1</a><a href='/2'>2</a></div>";
        assert_eq!(ex(links, "a:last-child::attr(href)"), v(&["/2"]));
        // :nth-last-* — position counted from the end (among ALL children for -child, same-tag for -type)
        let five = "<ul><li>a</li><li>b</li><li>c</li><li>d</li><li>e</li></ul>";
        assert_eq!(ex(five, "li:nth-last-child(1)::text"), v(&["e"]));       // == :last-child
        assert_eq!(ex(five, "li:nth-last-child(2)::text"), v(&["d"]));       // 2nd from end
        assert_eq!(ex(five, "li:nth-last-of-type(2)::text"), v(&["d"]));
        assert_eq!(ex(five, "li:nth-last-child(odd)::text"), v(&["a", "c", "e"])); // odd from the end
        // XPath [last()] / [last()-k] — tag[last()] of-type, *[last()] nth-last-child; [last()-1] = 2nd-last
        assert_eq!(ex(five, "//li[last()]/text()"), v(&["e"]));
        assert_eq!(ex(five, "//li[last()-1]/text()"), v(&["d"]));
        assert_eq!(ex(five, "//ul/*[last()]/text()"), v(&["e"]));
        assert_eq!(ex(links, "//a[last()]/@href"), v(&["/2"]));
        // SUBTREE terminals are supported: values are recovered by re-scanning the winner's raw span
        // (`Matcher::resolve_tail_spans`), so nothing is buffered during the pass.
        assert_eq!(ex("<ul><li><b>p</b>q</li></ul>", "li:last-child ::text"), v(&["p", "q"]));
        assert_eq!(ex("<ul><li>c</li></ul>", "//li[last()]//text()"), v(&["c"]));
        assert_eq!(ex("<ul><li>a<li>L<b>t</b></ul>", "li:last-child ::text"), v(&["L", "t"]));
        // the re-scan runs the real engine, so tree rules inside the span still apply: a dropped end
        // tag coalesces, and table scope holds
        assert_eq!(ex("<ul><li>a<li>HELLO</p>WORLD</ul>", "li:last-child ::text"), v(&["HELLOWORLD"]));
        assert_eq!(
            ex("<ul><li>a<li><div><table><tr><td>X</div>Y</table></ul>", "li:last-child ::text"),
            v(&["XY"])
        );
        // NESTED winners de-duplicate (a contained span's values are a subset of its container's)
        assert_eq!(
            ex("<ul><li>a<li>L<ul><li>x<li>IN</ul>M</ul>", "li:last-child ::text"),
            v(&["L", "x", "IN", "M"])
        );
        assert_eq!(ex("<div>1<div>A<div>B</div></div></div>", "div:last-child ::text"), v(&["1", "A", "B"]));
        // subtree `::attr` and the of-type / nth-last variants
        assert_eq!(
            ex("<ul><li><a href=\"/1\">x</a><li><a href=\"/2\">y</a><a href=\"/3\">z</a></ul>",
               "li:last-child ::attr(href)"),
            v(&["/2", "/3"])
        );
        assert_eq!(ex("<ul><li>A<li>B<li>C</ul>", "li:nth-last-child(2) ::text"), v(&["B"]));
        // reverse on an ANCESTOR compound: the value comes from a DESCENDANT, recovered by re-scanning
        // the winner's span with the selector tail (`b::text`) — see `split_deferred`.
        assert_eq!(ex(links, "div:only-child a::attr(href)"), v(&["/1", "/2"]));
        assert_eq!(
            ex("<ul><li><b>b1</b><li><b>b2</b></ul>", "li:last-child b::text"),
            v(&["b2"])
        );
        assert_eq!(
            ex("<ul><li><span><a href=\"/1\">x</a></span><li><span><a href=\"/2\">y</a></span></ul>",
               "li:last-child span a::attr(href)"),
            v(&["/2"])
        );
        assert_eq!(ex("<ul><li><b>a</b><li><b>b</b></ul>", "//li[last()]//b/text()"), v(&["b"]));
        // nested winners still de-duplicate through the tail
        assert_eq!(
            ex("<ul><li>x<li>L<b>bl</b><ul><li>y<li>IN<b>bi</b></ul></ul>", "li:last-child b::text"),
            v(&["bl", "bi"])
        );
        // still unsupported -> empty (never wrong):
        // a CHILD step into the tail needs "depth exactly 1 in the span", which the depth-agnostic
        // matcher can't express (same reason grouped sub-fields reject `./x`)
        assert_eq!(ex("<ul><li><b>a</b><li><b>b</b></ul>", "li:last-child > b::text"), Vec::<String>::new());
        assert_eq!(ex("<ul><li><b>a</b><li><b>b</b></ul>", "//li[last()]/b/text()"), Vec::<String>::new());
        assert_eq!(ex(h, "h1::text, li:last-child::text"), Vec::<String>::new()); // reverse in a comma group
    }

    /// `:has()` and text-content predicates share the reverse tier's span re-scan, so their values may
    /// also come from the subject's SUBTREE (`div:has(a) ::text`) or from a value-bearing DESCENDANT
    /// (`div:has(a) a::attr(href)`) — the common "link inside a card that has an image" shape.
    #[test]
    fn deferred_predicate_subtree_and_descendant_values() {
        let d = "<div class=w><p class=x>px</p><a href='/w'>aw</a>wt</div><div class=n><p>pn</p>nt</div>";
        // :has with a SUBTREE terminal
        assert_eq!(ex(d, "div:has(a) ::text"), v(&["px", "aw", "wt"]));
        assert_eq!(ex(d, "div:has(a) ::attr(href)"), v(&["/w"]));
        // :has on an ANCESTOR — value from a descendant
        assert_eq!(ex(d, "div:has(a) p::text"), v(&["px"]));
        assert_eq!(ex(d, "div:has(a) a::attr(href)"), v(&["/w"]));
        assert_eq!(ex(d, "div:has(p) a::text"), v(&["aw"]));
        assert_eq!(ex(d, "div:has(a) p.x::text"), v(&["px"]));
        // XPath text-content predicate with a non-attached terminal
        assert_eq!(ex(d, "//div[contains(.,\"aw\")]//text()"), v(&["px", "aw", "wt"]));
        assert_eq!(ex(d, "//div[contains(.,\"aw\")]//a/@href"), v(&["/w"]));
        assert_eq!(ex(d, "//div[.=\"pxawwt\"]//a/@href"), v(&["/w"]));
        // NESTED qualifying subjects de-duplicate (outer span contains the inner one)
        let n = "<div class=o><a href=/o>o</a><div class=i><a href=/i>i</a></div></div>";
        assert_eq!(ex(n, "div:has(a) a::attr(href)"), v(&["/o", "/i"]));
        assert_eq!(ex(n, "div:has(a) ::text"), v(&["o", "i"]));
        // attached forms unchanged, and a CHILD step into the tail stays out of tier
        assert_eq!(ex(d, "div:has(a)::attr(class)"), v(&["w"]));
        assert_eq!(ex(d, "//div[contains(.,\"aw\")]/text()"), v(&["wt"]));
        assert_eq!(ex(d, "div:has(a) > p::text"), Vec::<String>::new());
        // two deferred predicates in one selector remain out of tier
        assert_eq!(ex(d, "div:has(a) p:has(b)::text"), Vec::<String>::new());
    }

    #[test]
    fn normalize_space_values() {
        let h = "<h1>  Hello   <b>big</b>  world </h1><h1> second </h1><a href='  /x '>k</a><p>a\tb\n c</p>";
        // element string-value: concat of the FIRST matched element's subtree text, ws-collapsed
        assert_eq!(ex(h, "normalize-space(//h1)"), v(&["Hello big world"]));
        assert_eq!(ex(h, "normalize-space(//p)"), v(&["a b c"]));
        // first text node / first attr value
        assert_eq!(ex(h, "normalize-space(//h1/text())"), v(&["Hello"]));
        assert_eq!(ex(h, "normalize-space(//a/@href)"), v(&["/x"]));
        // ALWAYS exactly one value — empty string when nothing matches (XPath `['']`), never `[]`
        assert_eq!(ex(h, "normalize-space(//nope)"), v(&[""]));
        assert_eq!(ex(h, "normalize-space(//zzz/@q)"), v(&[""]));
        // a whitespace-only element normalizes to the empty string
        assert_eq!(ex("<li>  \t </li>", "normalize-space(//li)"), v(&[""]));
    }

    #[test]
    fn dot_child_anchor_is_unsupported_not_wrong() {
        // Regression for the `./…` child-anchor over-match: the matcher can't enforce a child anchor
        // on a segment's first compound, so `./step` used to behave like `.//step` (wrong values).
        // It is now unsupported -> empty column, never a wrong value.
        //
        // flat: parsel `./h3/text()` on the doc root is [] (h3 is not a child of the document node);
        // rejecting it (empty column) matches that oracle exactly.
        let html = "<html><body><div><h3>A</h3></div><h3>B</h3></body></html>";
        assert_eq!(ex(html, "./h3/text()"), Vec::<String>::new());
        assert_eq!(ex(html, "./body/h3/text()"), Vec::<String>::new());
        // grouped direct-child `./h3/text()` used to leak the nested `NEST`; now empty (unsupported),
        // while the `.//h3/text()` descendant control still collects both, in document order.
        let ghtml = "<html><body><div class=card><h3>A</h3><div><h3>NEST</h3></div></div></body></html>";
        let rows = grp(ghtml, ".card", &["./h3/text()", ".//h3/text()"]);
        assert_eq!(rows, vec![vec![Vec::<String>::new(), v(&["A", "NEST"])]]);
    }

    #[test]
    fn relative_descendant_excludes_context_node() {
        // `.//x` is a STRICT descendant of the context node (the root/container), which the descendant
        // axis excludes; the absolute `//x` is descendant-or-self of the doc root and includes <html>.
        let html = "<html><head><title>t</title></head><body><div><p>x</p></div></body></html>";
        // parsel: `//*` includes html; `.//*` excludes it (context = the <html> element).
        assert!(ex(html, "//*").iter().any(|s| s.starts_with("<html")));
        assert!(!ex(html, ".//*").iter().any(|s| s.starts_with("<html")));
        // parsel: `.//html` is [] (html has no html descendant); `//html` is the whole element.
        assert_eq!(ex(html, ".//html"), Vec::<String>::new());
        assert_eq!(ex(html, "//html").len(), 1);
        // grouped: a `.//tag` sub excludes the container even when its tag equals the container tag.
        let g = "<div class=box>T<div class=inner>N</div></div>";
        // `.//div//text()` from the .box container collects only the inner div's text, not the box's.
        let rows = grp(g, ".box", &[".//div//text()"]);
        assert_eq!(rows, vec![vec![v(&["N"])]]);
    }

    // ---- single-pass One/Many (grouped) extraction ----
    #[test]
    fn many_basic_per_container_grouping() {
        let html = "<div class=p><h3><a href=/1>A</a></h3><span class=price>$1</span></div>\
                    <div class=p><h3><a href=/2>B</a></h3><span class=price>$2</span></div>";
        let rows = grp(html, ".p", &["h3 a::text", "h3 a::attr(href)", ".price::text"]);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0], vec![v(&["A"]), v(&["/1"]), v(&["$1"])]);
        assert_eq!(rows[1], vec![v(&["B"]), v(&["/2"]), v(&["$2"])]);
    }

    #[test]
    fn many_descendant_or_self_scope() {
        // a sub-selector may match the container element itself (Parsel's descendant-or-self)
        let rows = grp("<div class=card data-id=x><p>hi</p></div>", ".card", &[".card::attr(data-id)"]);
        assert_eq!(rows, vec![vec![v(&["x"])]]);
    }

    #[test]
    fn many_empty_container_still_a_row() {
        // a container with no sub-field match still yields a row (empty column), per Parsel
        let rows = grp("<div class=p></div><div class=p><b>y</b></div>", ".p", &["b::text"]);
        assert_eq!(rows, vec![vec![Vec::<String>::new()], vec![v(&["y"])]]);
    }

    #[test]
    fn many_nested_same_group_routes_to_all_open_instances() {
        // Parsel: for c in doc.css(".box") -> [outer, inner]; outer.css("span") sees BOTH spans.
        let html = "<div class=box><span>outer</span>\
                    <div class=box><span>inner</span></div></div>";
        let rows = grp(html, ".box", &["span::text"]);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0], vec![v(&["outer", "inner"])]); // outer box (document order) sees both
        assert_eq!(rows[1], vec![v(&["inner"])]); // inner box sees only its own
    }

    #[test]
    fn many_outer_html_subfield() {
        let rows = grp("<div class=p><b>x</b></div>", ".p", &["b"]);
        assert_eq!(rows, vec![vec![v(&["<b>x</b>"])]]);
    }

    #[test]
    fn many_flat_and_grouped_together() {
        // flat columns and grouped rows come from the same single pass
        let html = "<h1>Shop</h1><div class=p><a>A</a></div><div class=p><a>B</a></div>";
        let g = GroupQuery {
            container: ".p".to_string(),
            subfields: vec![("t".to_string(), "a::text".to_string())],
        };
        let (flat, grouped) = extract_grouped(html.as_bytes(), &["h1::text".to_string()], &[g], None);
        assert_eq!(flat, vec![v(&["Shop"])]);
        assert_eq!(grouped[0], vec![vec![v(&["A"])], vec![v(&["B"])]]);
    }

    // ---- the headline divergences: implied-close makes child/sibling match lxml ----
    #[test]
    fn li_child_omitted_end() {
        assert_eq!(ex("<ul><li>a<li>b<li>c</ul>", "ul > li::text"), v(&["a", "b", "c"]));
    }
    #[test]
    fn td_tr_child_omitted_end() {
        assert_eq!(
            ex("<table><tr><td>a<td>b</tr><tr><td>c</table>", "tr > td::text"),
            v(&["a", "b", "c"])
        );
    }
    #[test]
    fn dt_dd_child_omitted_end() {
        assert_eq!(ex("<dl><dt>t<dd>d<dt>t2<dd>d2</dl>", "dl > dd::text"), v(&["d", "d2"]));
    }
    #[test]
    fn option_child_omitted_end() {
        assert_eq!(
            ex("<select><option>a<option>b</select>", "select > option::text"),
            v(&["a", "b"])
        );
    }
    // libxml2 2.14 NESTS a same-tag dt/dd repeat instead of auto-closing it (unlike `li`/`td`/`option`
    // above), and never auto-closes ruby annotations at all. The ported rule table asserted the HTML5
    // behavior for both and over-closed. See `implied_close::implies_close_id`.
    #[test]
    fn dt_dd_same_tag_repeat_nests() {
        assert_eq!(ex("<dl><dt>a<dt>b</dl>", "dl > dt::text"), v(&["a"]));
        assert_eq!(ex("<dl><dd>a<dd>b</dl>", "dl > dd::text"), v(&["a"]));
    }
    #[test]
    fn ruby_annotations_never_auto_close() {
        assert_eq!(ex("<ruby><rt>a<rt>b</ruby>", "ruby > rt::text"), v(&["a"]));
        assert_eq!(ex("<ruby><rt>a<rp>b</ruby>", "ruby > rp::text"), v(&[]));
        // ...but they still close an open <p>, so `b` is not p's text.
        assert_eq!(ex("<div><p>a<rt>b</div>", "div > p::text"), v(&["a"]));
    }
    #[test]
    fn p_closed_by_block() {
        assert_eq!(ex("<div><p>x<h2>y</h2><p>z</div>", "div > p::text"), v(&["x", "z"]));
    }

    // Content after `</html>` is KEPT — a DELIBERATE divergence from libxml2, which stops there and
    // discards the rest. Trailing injected markup is common in the wild, so dropping real content
    // silently is the worse failure for a scraper. Locked here because an intentional divergence with no
    // test is indistinguishable from a bug.
    //
    // Only PARTLY browser-equivalent, and the test pins both halves: a browser ALSO re-parents the
    // content into `<body>` (HTML Standard "after after body" reprocesses the token in body — verified
    // against Chrome `--dump-dom`), whereas the engine leaves it where the byte stream put it. So an
    // unscoped selector finds it (like a browser, unlike lxml) but a `body`-scoped one does not (like
    // lxml, unlike a browser) — the same root cause as the no-`<body>`-synthesis divergence.
    #[test]
    fn content_after_close_html_is_kept() {
        let h = "<html><body><p>in</p></body></html><div>late</div>";
        assert_eq!(ex(h, "p::text"), v(&["in"]));
        assert_eq!(ex(h, "div::text"), v(&["late"])); // lxml/Parsel: empty
        assert_eq!(ex(h, "div ::text"), v(&["late"]));
        // ...but NOT re-parented into <body>, so ancestor-scoped selectors agree with lxml, not a browser
        assert_eq!(ex(h, "body div::text"), Vec::<String>::new());
        assert_eq!(ex(h, "body > div::text"), Vec::<String>::new());
        // whitespace-only tails are inert either way
        assert_eq!(ex("<html><body><p>in</p></body></html>\n\n", "p::text"), v(&["in"]));
    }

    // TABLE SCOPE: libxml2 will not unwind a table for an ordinary end tag, so a stray `</div>` inside
    // a cell is discarded and the cell's text stays whole. Unbalanced `<div>`s around tables are a
    // common real-world malformation. See `implied_close::blocks_end_tag`.
    #[test]
    fn end_tag_does_not_unwind_a_table() {
        assert_eq!(ex("<div><table><tr><td>A</div>B</td></tr></table>", "td::text"), v(&["AB"]));
        assert_eq!(ex("<div><td>A</div>B", "td::text"), v(&["AB"])); // no <table> needed
        assert_eq!(ex("<ul><table><tr><td>A</ul>B", "td::text"), v(&["AB"]));
        assert_eq!(ex("<span><table><tr><td>A</span>B", "td::text"), v(&["AB"]));
        // ...but a table-scoped end tag unwinds normally, and </body>/</html> still close the document
        assert_eq!(ex("<div><table><tr><td>A</table>B</div>", "div::text"), v(&["B"]));
        assert_eq!(ex("<table><tbody><tr><td>A</tbody>B</table>", "td::text"), v(&["A"]));
        // `</body>` needs a real open <body> to match: the engine synthesizes no document frame, so on
        // a bare FRAGMENT it is an unmatched end tag and coalesces instead (documented fragment
        // divergence — real HTTP responses have a <body>).
        assert_eq!(ex("<body><div><table><tr><td>A</body>B", "td::text"), v(&["A"]));
        assert_eq!(ex("<div><table><tr><td>A</body>B", "td::text"), v(&["AB"]));
        // a non-table container does NOT block, and a </div> matched inside a cell is honoured
        assert_eq!(ex("<div><ul><li>A</div>B", "li::text"), v(&["A"]));
        assert_eq!(ex("<table><tr><td><div>A</div>B</td></tr></table>", "td::text"), v(&["B"]));
    }

    // ---- an end tag with nothing to match is DROPPED, and does not split the text node ----
    // libxml2 discards it and keeps the character data either side as ONE text node. Emitting two
    // values instead silently TRUNCATES a `One`-cardinality field (`.get()` -> "HELLO", not
    // "HELLOWORLD") — the one failure mode no-fallback is supposed to rule out. Real pages hit this:
    // Sphinx emits stray `</p>`. See `Matcher::text`.
    #[test]
    fn dropped_end_tag_does_not_split_text() {
        for tag in ["p", "span", "b", "li", "td", "bogus", "br"] {
            let html = format!("<div>HELLO</{tag}>WORLD</div>");
            assert_eq!(ex(&html, "div::text"), v(&["HELLOWORLD"]), "</{tag}>");
        }
        // chains across consecutive dropped tags, and applies to the XPath terminals too
        assert_eq!(ex("<div>A</p>B</p>C</div>", "div::text"), v(&["ABC"]));
        // ADJACENT drops with no text between must chain too — tracking one dropped tag instead of the
        // node's whole gap split these, and the Sphinx `</p>\n</p>` case only passed via the `\n` run
        assert_eq!(ex("<div>A</p></p>B</div>", "div::text"), v(&["AB"]));
        assert_eq!(ex("<div>A</p></span></b>B</div>", "div::text"), v(&["AB"]));
        assert_eq!(ex("<div>A</p>B</div>", "//div/text()"), v(&["AB"]));
        assert_eq!(ex("<div>A</p>B</div>", "//div//text()"), v(&["AB"]));
        // entity decoding still spans the join
        assert_eq!(ex("<div>A&amp;</p>&lt;B</div>", "div::text"), v(&["A&<B"]));
        // ...but each run is decoded on its OWN: the oracle decodes while tokenizing, i.e. before tree
        // construction, so a construct split across a discarded tag must NOT reassemble. Joining the raw
        // bytes first manufactured an entity here and a character in the next case.
        assert_eq!(ex("<div>A&am</p>p;B</div>", "div::text"), v(&["A&amp;B"]));
        assert_eq!(ex("<div>x&lt</p>;y</div>", "div::text"), v(&["x<;y"]));
        assert_eq!(
            extract(b"<div>\xc3</p>\xa9</div>", &["div::text".to_string()], None).pop().unwrap(),
            v(&["\u{fffd}\u{fffd}"]) // two replacement chars, NOT `é`
        );
    }
    // The join is SOURCE-ADJACENCY only: everything that is a real node in libxml2 still splits the
    // run, including a comment sitting in the gap left by a dropped tag.
    #[test]
    fn real_nodes_still_split_text() {
        assert_eq!(ex("<div>A<!--c-->B</div>", "div::text"), v(&["A", "B"]));
        assert_eq!(ex("<div>A<span>s</span>B</div>", "div::text"), v(&["A", "B"]));
        assert_eq!(ex("<div>A<br>B</div>", "div::text"), v(&["A", "B"]));
        assert_eq!(ex("<div>A</p><!--c-->B</div>", "div::text"), v(&["A", "B"]));
    }
    // A dropped tag must not corrupt the deferred text-content predicates either — they resolve on the
    // element's own text nodes, so they need the same joined node the value columns get.
    #[test]
    fn dropped_end_tag_text_predicates() {
        assert_eq!(ex("<div>HELLO</p>WORLD</div>", "//div[text()='HELLOWORLD']/text()"), v(&["HELLOWORLD"]));
        assert_eq!(ex("<div>HELLO</p>WORLD</div>", "//div[contains(text(),'LOWOR')]/text()"), v(&["HELLOWORLD"]));
        assert_eq!(ex("<div>HELLO</p>WORLD</div>", "normalize-space(//div/text())"), v(&["HELLOWORLD"]));
    }

    // ---- the invariant: well-formed input is unchanged (== lxml, == today's engine) ----
    #[test]
    fn wellformed_child_unchanged() {
        assert_eq!(ex("<ul><li>a</li><li>b</li></ul>", "ul > li::text"), v(&["a", "b"]));
    }
    #[test]
    fn wellformed_nested_lists() {
        // legitimate nesting must NOT be treated as an implied close
        assert_eq!(
            ex("<ul><li>a<ul><li>b</li></ul></li></ul>", "ul > li::text"),
            v(&["a", "b"])
        );
    }

    // ---- descendant is robust to nesting (0% divergence, control) ----
    #[test]
    fn descendant_robust() {
        assert_eq!(ex("<ul><li>a<li>b</ul>", "ul li::text"), v(&["a", "b"]));
    }
    #[test]
    fn subtree_text() {
        assert_eq!(ex("<div><p>a<b>c</b></p></div>", "div ::text"), v(&["a", "c"]));
    }

    // ---- compounds, attrs, terminals ----
    #[test]
    fn class_and_id() {
        assert_eq!(ex("<div class=\"c\"><span id=\"x\">s</span></div>", ".c #x::text"), v(&["s"]));
    }
    #[test]
    fn attr_self() {
        assert_eq!(ex("<a href=\"/p?a=1&amp;b=2\">t</a>", "a::attr(href)"), v(&["/p?a=1&b=2"]));
    }
    #[test]
    fn attr_crlf_normalized_like_text() {
        // HTML normalizes \r\n and lone \r to \n in the input stream, BEFORE entity expansion — so
        // a literal CR in an attribute value comes out as \n (parity with lxml), but a `&#13;`
        // char-ref stays a real \r (normalization precedes entity decode). (T4)
        assert_eq!(ex("<a title=\"x\r\ny\">t</a>", "a::attr(title)"), v(&["x\ny"]));
        assert_eq!(ex("<a title=\"x\ry\">t</a>", "a::attr(title)"), v(&["x\ny"]));
        assert_eq!(ex("<a title=\"x&#13;y\">t</a>", "a::attr(title)"), v(&["x\ry"]));
    }
    #[test]
    fn attr_eq_predicate() {
        assert_eq!(
            ex("<i data-k=\"v\">m</i><i data-k=\"w\">n</i>", "i[data-k=v]::text"),
            v(&["m"])
        );
    }
    #[test]
    fn attr_operators() {
        let h = "<a class=\"btn primary\" href=\"/shop/x\" hreflang=\"en-US\">t</a>";
        assert_eq!(ex(h, "a[href^=\"/shop\"]::text"), v(&["t"]));
        assert_eq!(ex(h, "a[href$=\"/x\"]::text"), v(&["t"]));
        assert_eq!(ex(h, "a[href*=\"hop\"]::text"), v(&["t"]));
        assert_eq!(ex(h, "a[class~=\"primary\"]::text"), v(&["t"]));
        assert_eq!(ex(h, "a[hreflang|=\"en\"]::text"), v(&["t"]));
        // negatives + empty-value-matches-nothing (CSS spec, verified vs lxml)
        assert_eq!(ex(h, "a[href^=\"/nope\"]::text"), v(&[]));
        assert_eq!(ex(h, "a[class~=\"prim\"]::text"), v(&[]));
        assert_eq!(ex(h, "a[href^=\"\"]::text"), v(&[]));
        assert_eq!(ex(h, "a[href*=\"\"]::text"), v(&[]));
        assert_eq!(ex(h, "a[hreflang|=\"e\"]::text"), v(&[]));
    }

    #[test]
    fn unicode_identifiers() {
        // CSS idents admit non-ASCII (class/id/tag), and attribute values with non-ASCII must match
        // through combinators/brackets (regression: `split_structural` used to mangle multi-byte UTF-8
        // via `c as char`). Oracle: parsel/cssselect accept all of these. (T6)
        assert_eq!(ex("<p class=\"café\">x</p><p class=cafe>y</p>", ".café::text"), v(&["x"]));
        assert_eq!(ex("<p id=\"naïve\">x</p>", "#naïve::text"), v(&["x"]));
        assert_eq!(ex("<i data-k=\"日本\">m</i><i data-k=\"x\">n</i>", "[data-k=\"日本\"]::text"), v(&["m"]));
        assert_eq!(ex("<i data-k=\"日本\">m</i>", "[data-k*=\"本\"]::text"), v(&["m"]));
        assert_eq!(ex("<p class=\"café\"><span>hi</span></p>", "p.café > span::text"), v(&["hi"]));
        // XPath side: non-ASCII attribute values match too (tag/attr NAMES stay ASCII — tokenizer-bound)
        assert_eq!(ex("<i data-k=\"日本\">m</i>", "//i[@data-k=\"日本\"]/text()"), v(&["m"]));
    }

    #[test]
    fn not_pseudo() {
        let h = "<div><a class=\"x\" href=\"/1\">A</a><a class=\"y\">B</a><a>C</a><b class=\"x y\">D</b></div>";
        assert_eq!(ex(h, "a:not(.x)::text"), v(&["B", "C"]));
        assert_eq!(ex(h, "a:not([href])::text"), v(&["B", "C"]));
        assert_eq!(ex(h, "a:not(.x):not(.y)::text"), v(&["C"]));
        assert_eq!(ex(h, ":not(a)::text"), v(&["D"]));
        assert_eq!(ex(h, "a:not(a.x)::text"), v(&["B", "C"])); // compound :not arg
        assert_eq!(ex(h, "b:not(.x.y)::text"), v(&[])); // matches full compound -> excluded
        assert_eq!(ex(h, "a:not(.z)::text"), v(&["A", "B", "C"])); // no a has class z
        // invalid selectors cssselect rejects must be unsupported (empty), not over-matches (sel_fuzz):
        assert_eq!(ex(h, "a:not(:not(.x))::text"), v(&[])); // nested :not() rejected
        assert_eq!(ex(h, "div >> a::text"), v(&[])); // doubled combinator rejected
        assert_eq!(ex(h, "div > > a::text"), v(&[]));
    }

    #[test]
    fn two_different_ids_match_nothing_but_dont_poison_group() {
        // an element has one id, so `#a#b` (different ids) is unsatisfiable -> [] (cssselect parity),
        // yet it stays a VALID member: a comma group with it must still yield the other member's
        // matches (found by sel_fuzz — Compound's single id slot silently kept the last id).
        let h = "<span id=\"i4\">x</span><a id=\"i2\">z</a>";
        assert_eq!(ex(h, "#i2#i4::text"), v(&[]));
        assert_eq!(ex(h, "span#i2#i4::text"), v(&[]));
        assert_eq!(ex(h, "#i4#i4::text"), v(&["x"])); // repeated same id is a no-op
        assert_eq!(ex(h, "a::text, #i2#i4::text"), v(&["z"])); // impossible member doesn't poison group
    }

    #[test]
    fn empty_selector_is_unsupported_not_universal() {
        // An empty / whitespace-only NODE query is not a selector (parsel raises SelectorSyntaxError).
        // Per the no-fallback contract it must be an empty column — NOT an implicit `*` that dumps the
        // whole document into the field (the pre-fix behavior). Found by the selector fuzzer.
        let h = "<body><p>A</p><div>B</div></body>";
        assert_eq!(ex(h, ""), v(&[]));
        assert_eq!(ex(h, "   "), v(&[]));
        // but a bare value pseudo still means "every element's own value" (matches parsel `::text`)
        assert_eq!(ex(h, "::text"), v(&["A", "B"]));
        assert_eq!(ex(h, "*::text"), v(&["A", "B"]));
    }

    #[test]
    fn comma_group_text() {
        // members merge in document order; an element matched by two members emits once (dedup)
        assert_eq!(
            ex("<div><h1>a</h1><p>b</p><h2>c</h2></div>", "h1::text, h2::text"),
            v(&["a", "c"])
        );
        assert_eq!(
            ex("<ul><li class=\"a b\">x</li><li class=\"a\">y</li></ul>", ".a::text, .b::text"),
            v(&["x", "y"]) // 'x' element has both classes -> emitted once
        );
    }
    #[test]
    fn comma_group_attr_same_name() {
        assert_eq!(
            ex("<a href=\"/1\">a</a><link href=\"/2\">", "a::attr(href), link::attr(href)"),
            v(&["/1", "/2"])
        );
    }
    #[test]
    fn comma_group_mixed_terminal() {
        // mixed `::text` + `::attr` union, merged in DOCUMENT order (lxml: attribute node precedes the
        // element's text node) regardless of selector order.
        assert_eq!(ex("<a href=\"/1\">t</a>", "a::text, a::attr(href)"), v(&["/1", "t"]));
        assert_eq!(ex("<a href=\"/1\">t</a>", "a::attr(href), a::text"), v(&["/1", "t"]));
        // across elements: document order (title text before meta's attr, and vice-versa)
        assert_eq!(
            ex("<title>TT</title><meta content=\"MM\">", "title::text, meta::attr(content)"),
            v(&["TT", "MM"])
        );
        assert_eq!(
            ex("<meta content=\"MM\"><p>PP</p>", "p::text, meta::attr(content)"),
            v(&["MM", "PP"])
        );
    }
    #[test]
    fn comma_group_multi_name_attr() {
        // `::attr(<different names>)` on one element: both values, in the element's SOURCE order.
        assert_eq!(
            ex("<img src=\"S\" data-src=\"D\">", "img::attr(src), img::attr(data-src)"),
            v(&["S", "D"])
        );
        assert_eq!(
            ex("<img data-src=\"D\" src=\"S\">", "img::attr(src), img::attr(data-src)"),
            v(&["D", "S"])
        );
        // same attribute node selected twice -> once (de-dup by node)
        assert_eq!(ex("<a href=\"/1\">t</a>", "a::attr(href), a::attr(href)"), v(&["/1"]));
    }
    #[test]
    fn comma_group_outer_mixed_with_value_unsupported() {
        // outer-HTML captures are reordered at finish, so mixing them with streaming value terminals
        // would break document order -> that combination stays unsupported (empty), never wrong.
        assert_eq!(ex("<b class=\"x\">hi</b>", "b.x, b.x::text"), v(&[]));
    }

    #[test]
    fn universal_after_combinator_is_self_scoped() {
        // REGRESSION (T1): `E > *`/`+`/`~` + ::text|::attr must NOT collapse to E's subtree.
        // Oracle (parsel): the `descendant-or-self::*/*` collapse applies only to the descendant
        // (whitespace) combinator, never to `>`/`+`/`~`.
        let h = "<body><div>direct<span>inner</span>tail</div></body>";
        assert_eq!(ex(h, "div > *::text"), v(&["inner"])); // text children of div's element children
        assert_eq!(ex(h, "div>*::text"), v(&["inner"])); // combinator without surrounding spaces
        assert_eq!(ex(h, "div *::text"), v(&["direct", "inner", "tail"])); // descendant collapse (unchanged)
        let sib = "<body><div>a</div><p>b</p></body>";
        assert_eq!(ex(sib, "div + *::text"), v(&["b"])); // next-sibling element's text
        assert_eq!(ex(sib, "div ~ *::text"), v(&["b"])); // following-sibling element's text
        // ::attr variant: only direct element children, not the whole subtree
        let nested = "<body><div><p id=outer><span id=inner>x</span></p></div></body>";
        assert_eq!(ex(nested, "div > *::attr(id)"), v(&["outer"]));
        // `div*::text` is invalid CSS (`*` attached to a compound) -> empty, never subtree values
        assert_eq!(ex(h, "div*::text"), v(&[]));
    }

    #[test]
    fn universal_with_detached_terminal_is_subtree_scoped() {
        // REGRESSION (T1 follow-up): whitespace between the `*` and the terminal makes the `*` a
        // REAL subject compound with a subtree terminal — distinct from BOTH the attached forms.
        // Oracle (parsel): `div > * ::text` = descendant-or-self::div/*/descendant-or-self::text();
        // `div * ::text` EXCLUDES div's own direct text (unlike the attached `div *::text` collapse).
        let h = "<body><div><p>a<b>c</b></p>x</div></body>";
        assert_eq!(ex(h, "div > * ::text"), v(&["a", "c"])); // subtree of div's children (not "x")
        assert_eq!(ex(h, "div > *::text"), v(&["a"])); // attached: self-scoped (control)
        let d = "<body><div>direct<span>s<b>deep</b></span></div></body>";
        assert_eq!(ex(d, "div * ::text"), v(&["s", "deep"])); // strict-descendant subtrees only
        assert_eq!(ex(d, "div *::text"), v(&["direct", "s", "deep"])); // attached collapse (control)
        let sib = "<body><div>a</div><p>p1<b>pb</b></p></body>";
        assert_eq!(ex(sib, "div + * ::text"), v(&["p1", "pb"])); // next-sibling's whole subtree
        let nested = "<body><div><p id=o><span id=i>x</span></p></div></body>";
        assert_eq!(ex(nested, "div > * ::attr(id)"), v(&["o", "i"])); // subtree attrs
        // `div* ::text` is still invalid CSS (`*` attached to a compound) -> empty
        assert_eq!(ex(h, "div* ::text"), v(&[]));
    }

    // ---- budget safety (T2): over-budget entries are DEAD (deterministic empty), never a panic ----
    #[test]
    fn budget_sibling_bits_over_64_is_safe_empty() {
        // 66 two-segment sibling selectors need 66 sibling bits > 64. Pre-fix the 65th tripped
        // `1u64 << 64`: debug panic / release bit-aliasing (contaminating another column). Now the
        // over-budget tail is dead -> empty, and the in-budget selectors still match. MUST NOT PANIC.
        let queries: Vec<String> = (0..66).map(|i| format!(".a{i} + .b{i}::text")).collect();
        let html = "<div><i class=\"a0\">.</i><i class=\"b0\">zero</i>\
                    <i class=\"a65\">.</i><i class=\"b65\">sixtyfive</i></div>";
        let cols = extract(html.as_bytes(), &queries, None);
        assert_eq!(cols.len(), 66);
        assert_eq!(cols[0], v(&["zero"])); // in-budget selector matches
        assert!(cols[65].is_empty()); // over-budget selector: deterministic empty, no aliasing
    }

    #[test]
    fn budget_members_over_128_is_safe_empty() {
        // 130 flat members: columns 0..127 are live, 128/129 are over the 128-member budget (dead).
        let queries: Vec<String> = (0..130).map(|i| format!(".c{i}::text")).collect();
        let html = "<b class=\"c0\">first</b><b class=\"c129\">last</b>";
        let cols = extract(html.as_bytes(), &queries, None);
        assert_eq!(cols.len(), 130);
        assert_eq!(cols[0], v(&["first"])); // in-budget column matches
        assert!(cols[129].is_empty()); // over-128 column: deterministically empty
    }

    #[test]
    fn budget_usage_counts_members_and_sibling_bits() {
        // members = flat member selectors + supported containers; sibling bits = adjacent/general combs
        let q = vec![
            "a::text".to_string(),            // 1 member, 0 sib
            "h1::text, h2::text".to_string(), // 2 members, 0 sib
            "li + li::text".to_string(),      // 1 member, 1 sib
            "a ~ b + c::text".to_string(),    // 1 member, 2 sib
        ];
        let groups = vec![GroupQuery {
            container: ".card".to_string(), // +1 member, 0 sib
            subfields: vec![("t".to_string(), "h3 a::text".to_string())], // subs cost nothing
        }];
        assert_eq!(budget_usage(&q, &groups), (6, 3));
    }

    #[test]
    fn outer_html_bare_element() {
        // raw-source capture; on clean well-formed double-quoted markup this equals lxml's reflow
        assert_eq!(
            ex("<div class=\"x\">a<b>c</b></div>", "div"),
            v(&["<div class=\"x\">a<b>c</b></div>"])
        );
        // document order across nesting (outer before inner), not close order
        assert_eq!(
            ex("<div><span>s</span></div>", "*"),
            v(&["<div><span>s</span></div>", "<span>s</span>"])
        );
        // multiple matches in document order
        assert_eq!(ex("<ul><li>a</li><li>b</li></ul>", "li"), v(&["<li>a</li>", "<li>b</li>"]));
        // void element: raw source is just the start tag
        assert_eq!(ex("<p><img src=\"a.png\">x</p>", "img"), v(&["<img src=\"a.png\">"]));
        // scoped bare element
        assert_eq!(ex("<div class=\"c\"><a>1</a></div><a>2</a>", ".c a"), v(&["<a>1</a>"]));
    }

    #[test]
    fn outer_html_omitted_end_tag_is_raw() {
        // documented divergence: raw source, NOT lxml's reflow (lxml would synthesize </li>)
        assert_eq!(ex("<ul><li>a<li>b</ul>", "ul > li"), v(&["<li>a", "<li>b"]));
    }

    #[test]
    fn xpath_downward() {
        let h = "<html><body><div class=\"c\"><a href=\"/1\">A</a><a href=\"/2\">B</a></div><p>x</p></body></html>";
        assert_eq!(ex(h, "//a/@href"), v(&["/1", "/2"]));
        assert_eq!(ex(h, "//a/text()"), v(&["A", "B"]));
        assert_eq!(ex(h, "//div[@class=\"c\"]/a/@href"), v(&["/1", "/2"]));
        assert_eq!(ex(h, "/html/body/p/text()"), v(&["x"]));
        assert_eq!(ex(h, "//div//text()"), v(&["A", "B"])); // descendant text
        assert_eq!(ex(h, "//a[contains(@href,\"1\")]/text()"), v(&["A"]));
        assert_eq!(ex(h, "//a[starts-with(@href,\"/2\")]/text()"), v(&["B"]));
        assert_eq!(ex(h, ".//p/text()"), v(&["x"]));
    }
    #[test]
    fn xpath_node_and_implied_close() {
        // XPath node query -> raw source (like a bare CSS element)
        assert_eq!(ex("<ul><li>a<li>b</ul>", "//ul/li"), v(&["<li>a", "<li>b"]));
        assert_eq!(ex("<ul><li>a<li>b</ul>", "//li/text()"), v(&["a", "b"]));
    }

    #[test]
    fn encoding_legacy_and_sniff() {
        // windows-1252 (é = 0xE9), explicit label
        assert_eq!(
            extract(b"<p class=\"c\">caf\xe9</p>", &["p::text".into()], Some("windows-1252"))[0],
            v(&["café"])
        );
        // Shift_JIS (日本 = 0x93FA 0x967B), explicit label, in an attribute value too
        assert_eq!(
            extract(b"<a title=\"\x93\xfa\x96\x7b\">x</a>", &["a::attr(title)".into()], Some("shift_jis"))[0],
            v(&["日本"])
        );
        // <meta charset> sniff (no label)
        assert_eq!(
            extract(
                b"<html><head><meta charset=windows-1252></head><body><p>caf\xe9</p></body></html>",
                &["p::text".into()],
                None,
            )[0],
            v(&["café"])
        );
    }
    #[test]
    fn encoding_utf16_bom_transcode() {
        let mut body = vec![0xFF, 0xFE]; // UTF-16LE BOM
        for c in "<p>hi</p>".chars() {
            body.push(c as u8);
            body.push(0);
        }
        assert_eq!(extract(&body, &["p::text".into()], None)[0], v(&["hi"]));
    }

    #[test]
    fn void_attr() {
        assert_eq!(ex("<img src=\"a.png\"><img src=\"b.png\">", "img::attr(src)"), v(&["a.png", "b.png"]));
    }

    // ---- global-desync guards: rawtext / comments / entities ----
    #[test]
    fn rawtext_lt_inside_script() {
        // '<' inside <script> must NOT desync the parser
        assert_eq!(ex("<script>if (a<b) {x=\"</p>\"}</script><p>t</p>", "p::text"), v(&["t"]));
    }
    #[test]
    fn comment_breaks_text_and_is_skipped() {
        assert_eq!(ex("<p>a<!-- c -->b</p>", "p::text"), v(&["a", "b"]));
    }
    #[test]
    fn entity_decode_text() {
        assert_eq!(ex("<p>a&amp;b &lt;c&gt;</p>", "p::text"), v(&["a&b <c>"]));
    }
    #[test]
    fn bom_stripped_only_at_document_start() {
        // libxml2 removes ONLY the leading document BOM; a U+FEFF inside a text node is real content
        // (encoding_rs' `decode` would strip it per-value — the bug this guards against).
        let lead = b"\xEF\xBB\xBF<p>x</p>"; // BOM at doc start -> dropped
        assert_eq!(extract(lead, &["p::text".into()], None)[0], v(&["x"]));
        let mid = b"<p>\xEF\xBB\xBFx</p>"; // U+FEFF mid-text -> preserved
        assert_eq!(extract(mid, &["p::text".into()], None)[0], v(&["\u{FEFF}x"]));
        // and in an attribute value
        let attr = "<a title=\"\u{FEFF}hi\">t</a>".as_bytes();
        assert_eq!(extract(attr, &["a::attr(title)".into()], None)[0], v(&["\u{FEFF}hi"]));
    }
    #[test]
    fn lt_not_a_tag_is_text() {
        assert_eq!(ex("<p>1 < 2 and 3</p>", "p::text"), v(&["1 < 2 and 3"]));
    }

    // ---- sibling combinators (+ / ~), incl. chains, on the corrected stack ----
    #[test]
    fn adjacent_sibling() {
        assert_eq!(ex("<ul><li>a</li><li>b</li><li>c</li></ul>", "li + li::text"), v(&["b", "c"]));
    }
    #[test]
    fn general_sibling() {
        assert_eq!(ex("<div><h1>a</h1><p>b</p><p>c</p></div>", "h1 ~ p::text"), v(&["b", "c"]));
    }
    #[test]
    fn sibling_on_omitted_end_matches_lxml() {
        // the OLD lol-html engine returned [] here (nested, not siblings); corrected stack fixes it
        assert_eq!(ex("<ul><li>a<li>b<li>c</ul>", "li + li::text"), v(&["b", "c"]));
    }
    #[test]
    fn sibling_chain() {
        assert_eq!(
            ex("<div><i class=a>x</i><i class=b>y</i><i class=c>z</i></div>", ".a ~ .b ~ .c::text"),
            v(&["z"])
        );
    }
    #[test]
    fn adjacent_broken_by_intervening_element() {
        assert_eq!(ex("<div><h1>a</h1><span>s</span><p>b</p></div>", "h1 + p::text"), v(&[]));
        assert_eq!(ex("<div><h1>a</h1><span>s</span><p>b</p></div>", "h1 ~ p::text"), v(&["b"]));
    }
    #[test]
    fn sibling_base_with_descendant() {
        assert_eq!(ex("<ul class=l><li>a</li><li>b</li></ul>", ".l li ~ li::text"), v(&["b"]));
    }
    #[test]
    fn sibling_then_descendant_step() {
        // a descendant/child step AFTER the sibling combinator: the sibling anchor is the FIRST
        // compound of the right segment (`b`/second `p`), not the subject. Only the sibling's subtree
        // matches — the first `p`'s `<a>` must NOT (that `p` has no qualifying preceding sibling).
        assert_eq!(
            ex("<div><p><a>1</a></p><p><a>2</a></p></div>", "p + p a::text"),
            v(&["2"])
        );
        assert_eq!(ex("<div><a>1</a><b><c>x</c></b></div>", "a + b c::text"), v(&["x"]));
        assert_eq!(ex("<div><a>1</a><b><c>x</c></b></div>", "a + b > c"), v(&["<c>x</c>"]));
        // general-sibling variant + a nested subject across multiple sibling groups
        let html = "<div><s><a>1</a><b><c>x</c></b></s><s><a>2</a><b><c>y</c></b></s></div>";
        assert_eq!(ex(html, "a + b c::text"), v(&["x", "y"]));
    }

    // ---- local-not-global: a malformed list must not corrupt a later sentinel field ----
    #[test]
    fn no_global_desync_sentinel() {
        let html = "<ul><li>a<li>b</ul><div class=\"end\">SENTINEL</div>";
        assert_eq!(ex(html, ".end::text"), v(&["SENTINEL"]));
    }
}
