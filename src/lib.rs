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
/// Rule-table mutation hook: an identity function unless built with `--features mutate`.
mod mutate;
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
///
/// `encoding` is an optional caller/HTTP charset label (as Scrapy passes); when `None` the encoding is
/// sniffed (BOM -> `<meta>` -> UTF-8). Structural tokenization runs on raw bytes for every
/// ASCII-compatible encoding; only the small emitted values are decoded with the resolved encoding.
/// UTF-16LE/BE are transcoded to UTF-8 up front (rare).
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
/// CSS? The one routing rule, shared by [`compile_query`] / [`compile_one`] and by [`diagnostics`] —
/// an explainer that classified a query differently from the compiler would name the wrong cause.
pub(crate) fn is_xpath(qt: &str) -> bool {
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

/// Lower a whole schema's query strings to the matcher's inputs. Every entry point that reasons about
/// a schema — [`budget_usage`], [`audit_schema`], [`Plan::compile`] — goes through here, so all three
/// necessarily agree on which selectors compiled: an audit that routed a query differently from the
/// `Plan` that runs it would promise support for a column that comes back empty.
fn compile_schema(
    queries: &[String],
    groups: &[GroupQuery],
) -> (Vec<Vec<Selector>>, Vec<matcher::GroupInput>) {
    let flat = queries.iter().map(|q| compile_query(q)).collect();
    let grouped = groups
        .iter()
        .map(|g| {
            (compile_one(&g.container), g.subfields.iter().map(|(_, sel)| compile_one(sel)).collect())
        })
        .collect();
    (flat, grouped)
}

/// The `(member-selector, sibling-bit)` demand of a schema. A caller
/// that would rather fail loud than get silently-empty columns compares this against
/// [`MAX_MEMBERS`] / [`MAX_SIB_BITS`]; the Python binding raises `ValueError`.
pub fn budget_usage(queries: &[String], groups: &[GroupQuery]) -> (usize, usize) {
    let (flat, grouped) = compile_schema(queries, groups);
    matcher::budget_usage(&flat, &grouped)
}

// ---------------------------------------------------------------- schema audit (no-fallback safety)
//
// The no-fallback contract makes an unsupported selector look identical to a legitimately-empty field
// at the scraper layer. These functions let a caller AUDIT a schema up front — before it silently
// yields empty columns in production — turning "unsupported selector" into an explicit, explainable
// signal. The supported/unsupported DECISION is authoritative (it is the real compiler); the reason is
// advisory (see [`diagnostics`]).

/// The VALUE TERMINAL each query produces: `"text"`, `"attr"`, `"outer"` (bare element — the value is
/// the matched element's raw source, i.e. a NODE reference rather than a scalar), `"normalize-space"`,
/// or `None` for a query that does not compile.
///
/// Exposed because a caller sometimes has to treat a node-valued column differently from a scalar one,
/// and the only authority on which a query is is the compiler that routes it. The web-poet layer needs
/// exactly this: a field processor's input contract is an lxml/parsel NODE, so when a processor is
/// attached to an `"outer"` field the raw source has to be re-parsed into one, while `"text"`/`"attr"`
/// fields are genuinely strings and must be handed over untouched. Deriving that from
/// [`compile_query`] — the same front-end `extract` and `Plan` use — is the difference between one
/// definition and a hand-written heuristic that has to keep agreeing with the parser. The heuristic
/// version of this question already shipped one bug (XPath `/text()` and `/@name` misread as node
/// queries), which is why it is answered here instead.
///
/// A routed query is uniform on the node-vs-scalar axis: [`compile_query`]'s comma/union rule refuses a
/// mix of outer-HTML and value terminals (deferred captures cannot interleave with streamed values in
/// document order), so the first member's terminal settles it for the whole query.
pub fn selector_terminals(queries: &[String]) -> Vec<Option<&'static str>> {
    queries
        .iter()
        .map(|q| compile_query(q).first().map(|s| terminal_name(&s.terminal)))
        .collect()
}

fn terminal_name(t: &selector::Terminal) -> &'static str {
    match t {
        selector::Terminal::Text { .. } => "text",
        selector::Terminal::Attr { .. } => "attr",
        selector::Terminal::OuterHtml => "outer",
        selector::Terminal::NormalizeSpace(_) => "normalize-space",
    }
}

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
    let (flat_sel, grouped_sel) = compile_schema(queries, groups);
    let (members, sib_bits) = matcher::budget_usage(&flat_sel, &grouped_sel);
    let schema = matcher::CompiledSchema::compile(&flat_sel, &grouped_sel);
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

/// Resolve the page encoding and yield `(bytes_to_tokenize, value_encoding)`. A non-ASCII-compatible
/// encoding is transcoded to a fresh UTF-8 `Vec` once (owned `Cow`); everything else borrows the input,
/// with a document-leading UTF-8 BOM stripped — libxml2 strips ONLY the leading BOM (a U+FEFF elsewhere
/// is real content), and per-value decoding must not re-strip it (see `matcher::finalize`), so it is
/// handled here, once per page.
///
/// **The transcode set is asked of `encoding_rs`, not listed here**, and that is the whole point: it was
/// listed here, as "UTF-16, the only non-ASCII-compatible family", and it was wrong. ISO-2022-JP is not
/// ASCII-compatible either — inside `ESC $ B` mode a JIS pair is two bytes below 0x80, and `社` is
/// literally `<R`. A crawled Japanese page tokenized as raw bytes therefore grew a `<r>` start tag out
/// of the middle of a word and dropped the character; every value downstream of it was wrong. The
/// predicate covers `replacement` too (the label for HZ-GB-2312 and friends), whose whole purpose is
/// that browsers refuse to decode such a document at all — one U+FFFD and nothing else.
///
/// Raw NUL is deleted here too, for the whole document (see [`strip_nul`]).
fn prepare_bytes<'h>(
    html: &'h [u8],
    encoding: Option<&str>,
) -> (Cow<'h, [u8]>, &'static encoding_rs::Encoding) {
    let enc = encoding::resolve(html, encoding);
    if !enc.is_ascii_compatible() {
        // Transcode FIRST: in UTF-16 every ASCII character carries a 0x00 byte, so deleting NUL bytes
        // from the raw input would shred the document. What must go is the U+0000 CHARACTER, which only
        // exists once the code units have been decoded. (Parsel deletes NUL from the raw bytes instead,
        // which is why it cannot read a UTF-16 page at all — a divergence in our favour, already
        // documented under Encoding in docs/COMPATIBILITY.md.)
        let mut utf8 = enc.decode(html).0.into_owned().into_bytes();
        utf8.truncate(document_end(&utf8));
        let start = document_start(&utf8);
        if start > 0 {
            utf8.drain(..start);
        }
        return (Cow::Owned(strip_nul(Cow::Owned(utf8)).into_owned()), encoding_rs::UTF_8);
    }
    let bytes = if enc == encoding_rs::UTF_8 {
        &html[document_start(html)..]
    } else {
        html
    };
    let bytes = &bytes[..document_end(bytes)];
    (strip_nul(Cow::Borrowed(bytes)), enc)
}

/// Where the document's text starts: past a leading UTF-8 BOM, and past any ASCII whitespace written
/// BEFORE it. Returns 0 when there is no BOM, so an ordinary page keeps every offset it arrived with.
///
/// The whitespace half is Parsel's rule, not libxml2's, and it is the same call as deleting raw NUL
/// (see [`strip_nul`]): `Selector(text=...)` parses `text.strip()`, so on a page that INDENTS its
/// doctype — `"    \u{FEFF}<!DOCTYPE HTML>"` — the U+FEFF is promoted to offset 0, where libxml2 then
/// eats it as a BOM. Read the bytes as they arrive and that U+FEFF is instead a character, and a
/// character before the frame opens the `<body>`: the page's `<head>`, `<title>` and even the
/// attributes of its own `<html>` tag (redundant once a body is open) are all lost, so `head
/// title::text` and `html::attr(xmlns)` come back silently EMPTY. libxml2 on the raw bytes and
/// html5lib both agree with the raw reading — this is Parsel normalizing its input, and matching what
/// a scraper actually sees is the point. Four pages in one 10000-page crawl sample were shaped this
/// way, between them 71 divergent columns.
///
/// Gated on UTF-8 by the caller: in windows-1252 those three bytes are `ï»¿`, three real characters,
/// and Parsel's own decode agrees.
/// Where the document's text ends: before any trailing ASCII whitespace, which is the other half of
/// Parsel's `text.strip()` (see [`document_start`]).
///
/// This one can only ever move whitespace-only text, so it never changes a value a scraper reads — but
/// it does change how many values come back. A page ending `…<option class="c3">\n` gives the last
/// option a text node here and none under Parsel, so `option::text` returned one extra row. Not gated
/// on UTF-8: no ASCII-compatible encoding can carry these bytes inside a multi-byte character, and a
/// non-ASCII-compatible one has already been transcoded.
fn document_end(bytes: &[u8]) -> usize {
    bytes
        .iter()
        .rposition(|b| !b.is_ascii_whitespace())
        .map_or(0, |i| i + 1)
}

fn document_start(bytes: &[u8]) -> usize {
    let ws = bytes
        .iter()
        .position(|b| !b.is_ascii_whitespace())
        .unwrap_or(bytes.len());
    if bytes[ws..].starts_with(&[0xEF, 0xBB, 0xBF]) {
        ws + 3
    } else {
        0
    }
}

/// Delete every raw NUL byte from the document, as Parsel/w3lib do before handing bytes to lxml
/// (`body.replace(b"\x00", b"")`).
///
/// This has to happen before TOKENIZATION, not at value-emit time. The engine used to drop NUL only
/// from emitted values, so the two sides disagreed about the document's STRUCTURE: `<di\0v>X</di\0v>`
/// is a `div` to lxml and an element named `di\0v` here, and `div::text` returned nothing — a silently
/// empty column, not a slightly different string. Attribute names and values, text and tag names are
/// all covered by doing it once, up front.
///
/// The ordinary path (no NUL anywhere, which is every real page) costs one `memchr` over the buffer and
/// no allocation. Only a document that actually contains a NUL is copied.
fn strip_nul(bytes: Cow<'_, [u8]>) -> Cow<'_, [u8]> {
    if memchr::memchr(0, &bytes).is_none() {
        return bytes;
    }
    Cow::Owned(bytes.iter().copied().filter(|&b| b != 0).collect())
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
        let (flat, grouped) = compile_schema(queries, groups);
        let budget = matcher::budget_usage(&flat, &grouped);
        Plan { schema: matcher::CompiledSchema::compile(&flat, &grouped), budget }
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

    /// The node-vs-scalar answer `frostwork.webpoet` routes a processor on. The XPath rows are the ones
    /// that matter: the heuristic this replaces read `/text()` and `/@href` as node queries because they
    /// carry no `::`-pseudo, which is exactly the mistake a query string invites and the compiler cannot
    /// make.
    #[test]
    fn selector_terminals_names_the_value_terminal() {
        let q: Vec<String> = [
            "h1::text",                  // text
            "div ::text",                // subtree text, still text
            "a::attr(href)",             // attr
            "div.card",                  // bare element -> outer HTML, i.e. a NODE
            ".a, .b",                    // all-outer comma list stays outer
            "//a/text()",                // XPath text terminal, NOT a node
            "//a/@href",                 // XPath attribute terminal, NOT a node
            "//div[@id='x']",            // XPath bare element -> outer
            "normalize-space(//h1)",     // scalar string value
            "div:has(.a .b)::text",      // does not compile -> None
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        assert_eq!(
            selector_terminals(&q),
            vec![
                Some("text"),
                Some("text"),
                Some("attr"),
                Some("outer"),
                Some("outer"),
                Some("text"),
                Some("attr"),
                Some("outer"),
                Some("normalize-space"),
                None,
            ]
        );
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
    // above), and never auto-closes ruby annotations at all. The ported rule table that used to sit
    // alongside `start_closes` asserted the HTML5 behavior for both and over-closed.
    #[test]
    fn dt_dd_same_tag_repeat_nests() {
        assert_eq!(ex("<dl><dt>a<dt>b</dl>", "dl > dt::text"), v(&["a"]));
        assert_eq!(ex("<dl><dd>a<dd>b</dl>", "dl > dd::text"), v(&["a"]));
    }
    /// `<colgroup>` with an omitted end tag is ordinary table markup and had NO rule, so the sections
    /// nested inside it and every child-anchored selector past the colgroup lost its cells.
    #[test]
    fn colgroup_closed_by_sections_and_rows() {
        let t = "<table><colgroup><col><col><thead><tr><th>H<tbody><tr><td>D</table>";
        assert_eq!(ex(t, "table > thead th::text"), v(&["H"]));
        assert_eq!(ex(t, "table > tbody td::text"), v(&["D"]));
        assert_eq!(ex(t, "colgroup thead th::text"), v(&[])); // must NOT nest
        // a colgroup closes an open caption, and an open <p>
        assert_eq!(ex("<table><caption>C<colgroup><col><thead><tr><th>H</table>", "table > thead th::text"), v(&["H"]));
        assert_eq!(ex("<div><p>a<colgroup><col></div>", "div > p::text"), v(&["a"]));
        // ...but a bare <caption> is NOT a scope boundary: the `</div>` is honoured
        assert_eq!(ex("<div><caption>A</div>B", "caption::text"), v(&["A"]));
    }

    #[test]
    fn ruby_annotations_never_auto_close() {
        assert_eq!(ex("<ruby><rt>a<rt>b</ruby>", "ruby > rt::text"), v(&["a"]));
        assert_eq!(ex("<ruby><rt>a<rp>b</ruby>", "ruby > rp::text"), v(&[]));
        // ...and they do NOT close an open <p> either. `div > p::text` was asserted here before and
        // cannot tell: it is p's DIRECT text, which is `a` whether the <rt> nested or became a sibling.
        // `div > rt` discriminates — it matches only if the <rt> was lifted out of the <p>.
        assert_eq!(ex("<div><p>a<rt>b</div>", "div > rt::text"), v(&[]));
        assert_eq!(ex("<div><p>a<option>b</div>", "div > option::text"), v(&[]));
    }

    /// libxml2's `htmlStartClose` NAME-pair table (`implied_close::start_closes`).
    ///
    /// This is a different rule from the implied-close cross product, at a finer granularity than the
    /// engine's tag ids: it closes an open `<b>` for an incoming `<td>` but NOT an open `<em>`, and an
    /// open `<h1>` for an incoming `<table>` but NOT an open `<div>` — pairs a coarser id space lumps
    /// together.
    /// Every case here was read off libxml2 2.14 first; `tools/audit_tree_rules.py` re-checks all
    /// 11,543 (open x incoming) cells, and `tools/mutate_rules.py` checks that flipping one is noticed.
    #[test]
    fn start_close_pairs_match_libxml2() {
        // an incoming <p> closes an open heading or font-style element (very common in legacy markup:
        // `<h1>Title<p>Body` with the `</h1>` omitted)
        assert_eq!(ex("<div><h1>T<p>x</div>", "div > p::text"), v(&["x"]));
        assert_eq!(ex("<div><h1>T<p>x</div>", "div > h1::text"), v(&["T"]));
        assert_eq!(ex("<div><b>x<p>y</div>", "div > p::text"), v(&["y"]));
        assert_eq!(ex("<div><u>x<p>y</div>", "div > p::text"), v(&["y"]));
        assert_eq!(ex("<div><small>x<p>y</div>", "div > p::text"), v(&["y"]));
        // ...but NOT an open <em>/<strong>, which a coarser id space would lump in with <b>
        assert_eq!(ex("<div><em>x<p>y</div>", "div > p::text"), v(&[]));
        // a cell closes an open inline element, an anchor closes an anchor, a form closes a form
        assert_eq!(ex("<div><span>x<td>y</div>", "div > td::text"), v(&["y"]));
        assert_eq!(ex("<div><font>x<td>y</div>", "div > td::text"), v(&["y"]));
        assert_eq!(ex("<div><a>x<a>y</div>", "div > a::text"), v(&["x", "y"]));
        assert_eq!(ex("<div><form>a<form>b</div>", "div > form::text"), v(&["a", "b"]));
        // <table> closes an open heading, but not an open <div> (one coarse "block" id would lump them)
        assert_eq!(ex("<div><h1>T<table><tr><td>c</table></div>", "div > table td::text"), v(&["c"]));
        // list/definition starts close an open <pre> or <address>
        assert_eq!(ex("<div><pre>a<li>b</div>", "div > li::text"), v(&["b"]));
        assert_eq!(ex("<dl><pre>a<dt>b</dl>", "dl > dt::text"), v(&["b"]));
        assert_eq!(ex("<div><address>a<dd>b</div>", "div > dd::text"), v(&["b"]));
        assert_eq!(ex("<div><menu>a<ul>b</ul></div>", "div > ul::text"), v(&["b"]));
        // <fieldset> closes an open <legend>
        assert_eq!(ex("<div><legend>L<fieldset>x</div>", "div > fieldset::text"), v(&["x"]));
        // a VOID incoming tag still closes: <col> closes an open <p>. The p-closing audit skips void
        // elements ("no text of its own"), so this pair had no coverage at all before.
        assert_eq!(ex("<div><p>x<col>y</div>", "div > p::text"), v(&["x"]));
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
    // A 1000-page Common Crawl sample turned this from a marginal call into an obvious one. The tag is
    // not always a trailing stray: one page's head ends `</head></html><body><header>…`, i.e. the
    // `</html>` is MISPLACED and 14 of the file's 17 KB follow it. libxml2 keeps 2 elements out of 100+
    // and a Parsel spider sees an empty page; the engine (and a browser) see the site. Another page in
    // the same sample hides its JSON-LD `<script>` the same way. So the divergence is not small — and it
    // runs in the direction that recovers a page rather than losing one.
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
        // the crawled shape: a MISPLACED `</html>` in the head, with the whole real page after it
        let real = "<html><head></head></html><body><header><a href=/>L</a></header><p>A</p>";
        assert_eq!(ex(real, "p::text"), v(&["A"])); // lxml/Parsel: empty
        assert_eq!(ex(real, "a::attr(href)"), v(&["/"])); // lxml/Parsel: empty
    }

    // END-TAG SCOPE: libxml2 will not unwind a table for an ordinary end tag, so a stray `</div>` inside
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
        // `</body>` closes the document whether the page wrote the `<body>` or the engine synthesized
        // it. On a bare fragment that used to be an unmatched end tag that coalesced the runs instead —
        // a documented divergence that document-frame synthesis closed.
        assert_eq!(ex("<body><div><table><tr><td>A</body>B", "td::text"), v(&["A"]));
        assert_eq!(ex("<div><table><tr><td>A</body>B", "td::text"), v(&["A"]));
        // a non-table container does NOT block, and a </div> matched inside a cell is honoured
        assert_eq!(ex("<div><ul><li>A</div>B", "li::text"), v(&["A"]));
        assert_eq!(ex("<table><tr><td><div>A</div>B</td></tr></table>", "td::text"), v(&["B"]));
    }

    /// A table-family end tag is scoped too, because the rule is a PRIORITY comparison rather than a
    /// "the table blocks everything, its row machinery blocks nothing" split: a `<tbody>` open above a
    /// `<tr>` out-ranks it, so the `</tr>` is discarded and the row keeps what follows.
    ///
    /// From a crawled page whose table generator emits `<tr><strong><tbody>` rows: the engine closed each
    /// row and LOST the cells. The `<strong>` matters — with nothing between them the `<tbody>` start tag
    /// closes the row itself, and then the end tag has no match either way.
    #[test]
    fn end_tag_does_not_unwind_a_higher_priority_element() {
        let row = "<table><tr><strong><tbody></tr><td>A</td></table>";
        assert_eq!(ex(row, "tr td::text"), v(&["A"]));
        // `<thead>` does not close a row either, so it too can sit above one and swallow the next row
        let two = "<table><tr><td>A</td><thead></tr><tr><td>B</td></tr></table>";
        assert_eq!(ex(two, "tr tr td::text"), v(&["B"]));
        // `<body>` out-ranks the whole table machinery, which only matters where one can be open ABOVE
        // a match: a `<body>` written after `</body>`. A crawled page put one inside a `<td>` and the
        // `</td>` under it has to be discarded, or the rest of the page leaves the cell.
        assert_eq!(ex("<body></body><td><body>X</td>Y", "td > body::text"), v(&["XY"]));
        assert_eq!(ex("<body></body><td><body>X</td>Y", "td::text"), Vec::<String>::new());
        // ...and the comparison runs the other way too: a LOWER-priority row and cell above the match do
        // not block, so `</tbody>` unwinds both and the text after it is the table's own.
        assert_eq!(ex("<table><tbody><tr><td>A</tbody>B</table>", "td::text"), v(&["A"]));
        assert_eq!(ex("<table><tbody><tr><td>A</tbody>B</table>", "table::text"), v(&["B"]));
    }

    #[test]
    fn end_tag_does_not_unwind_a_div_scope_boundary() {
        // libxml2 ignores the ancestor closer while a <div> is open, then closes both at EOF.
        assert_eq!(ex("<nav><ul><div>A</nav>B", "div::text"), v(&["AB"]));
        assert_eq!(ex("<form><div>A</form>B", "div::text"), v(&["AB"]));
        // Other block elements are not the same boundary.
        assert_eq!(ex("<nav><blockquote>A</nav>B", "blockquote::text"), v(&["A"]));
    }

    #[test]
    fn nested_table_blocks_an_outer_table_family_end_tag() {
        // A table-family closer may unwind row/cell machinery in its own table, but it cannot cross
        // a nested <table> to reach an outer row.
        assert_eq!(ex("<tr><table>A</tr>B", "table::text"), v(&["AB"]));
        // Same-table section closes still unwind normally.
        assert_eq!(ex("<table><tbody><tr><td>A</tbody>B</table>", "td::text"), v(&["A"]));
    }

    /// The names the hand-written start-close port left out. All three were found by widening the
    /// audit's universe to every element name rather than the ones someone remembered, and each is
    /// ordinary legacy markup: `<listing>`/`<xmp>` in man-page and README-to-HTML output, and a
    /// `<title>` after an unclosed `<p>`.
    #[test]
    fn start_close_names_missing_from_the_hand_written_port() {
        // a definition item closes an open <listing> (it behaves exactly like <pre> as an open element)
        assert_eq!(ex("<div><listing>A<dd>B</dd></div>", "div > dd::text"), v(&["B"]));
        assert_eq!(ex("<div><listing>A<dd>B</dd></div>", "listing dd::text"), Vec::<String>::new());
        for t in ["dd", "dl", "dt", "fieldset", "form", "li", "table", "ul"] {
            let h = format!("<div><listing>A<{t} id=Z>B</div>");
            assert_eq!(ex(&h, &format!("div > {t}::attr(id)")), v(&["Z"]),
                       "<{t}> must close an open <listing>");
        }
        // ...and <listing>/<xmp>/<title> close an open <p>
        for t in ["listing", "xmp", "title"] {
            let h = format!("<div><p>A<{t}>T</{t}>B</div>");
            assert_eq!(ex(&h, "div > p::text"), v(&["A"]), "<{t}> must close an open <p>");
            assert_eq!(ex(&h, &format!("div > {t}::text")), v(&["T"]));
        }
    }

    /// libxml2 treats the HTML4 `basefont`/`frame`/`isindex` as EMPTY elements. The engine let all three
    /// hold children, so everything after one of them was nested a level too deep.
    #[test]
    fn html4_void_elements_hold_no_children() {
        for t in ["basefont", "frame", "isindex"] {
            let h = format!("<div><{t}><span>x</span></div>");
            assert_eq!(ex(&h, "div > span::text"), v(&["x"]), "<{t}> must be void");
            assert_eq!(ex(&h, &format!("{t} span::text")), Vec::<String>::new());
        }
        // the deliberate other half of the contract: libxml2 keeps these HTML5-era names OPEN
        for t in ["embed", "source", "track", "wbr"] {
            let h = format!("<div><{t}><span>x</span></div>");
            assert_eq!(ex(&h, &format!("{t} span::text")), v(&["x"]), "<{t}> must be NON-void");
        }
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
    /// A class list splits on ASCII whitespace, not on Unicode whitespace — see `OpenElem::has_class`.
    /// `[rel~=x]` tokenizes identically, so both are checked here.
    #[test]
    fn class_lists_split_on_ascii_whitespace_only() {
        // separators HTML recognizes: the two classes are distinct
        for sep in [" ", "\t", "\n", "\r", "\u{0C}"] {
            let doc = format!("<div class=\"a fadein{sep}clearfix\">x</div>");
            assert_eq!(ex(&doc, ".fadein::text"), v(&["x"]), "{sep:?}");
            assert_eq!(ex(&doc, "[class~=clearfix]::text"), v(&["x"]), "{sep:?}");
        }
        // ...and the ones it does not: ONE token, so neither name matches. U+3000 is the one that
        // fired on a real Japanese page; U+000B is ASCII but not ASCII WHITESPACE.
        for sep in ["\u{3000}", "\u{A0}", "\u{2003}", "\u{0B}"] {
            let doc = format!("<div class=\"a fadein{sep}clearfix\">x</div>");
            assert_eq!(ex(&doc, ".fadein::text"), Vec::<String>::new(), "{sep:?}");
            assert_eq!(ex(&doc, ".clearfix::text"), Vec::<String>::new(), "{sep:?}");
            assert_eq!(ex(&doc, "[class~=clearfix]::text"), Vec::<String>::new(), "{sep:?}");
            // the whole run IS a token, so asking for it by its real name still works — except for
            // U+000B, which cssselect rejects INSIDE a selector (`SelectorSyntaxError`), so an empty
            // column is the right no-fallback answer there
            let by_name = ex(&doc, &format!(".fadein{sep}clearfix::text"));
            let expected = if sep == "\u{0B}" { Vec::new() } else { v(&["x"]) };
            assert_eq!(by_name, expected, "{sep:?}");
        }
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

    /// The documented budget is a SHARED 128 across every tier, and the bitsets that address members
    /// are `u128` — so the number of LIVE members must never exceed it, whichever tiers they come from.
    ///
    /// Each deferred tier used its own counter, so 128 normal members plus 128 reverse ones left 256
    /// live: `budget_usage` reported the schema over budget and Python rejected it while Rust ran every
    /// one, contradicting COMPATIBILITY.md's "over-budget entries compile dead".
    #[test]
    fn live_members_never_exceed_the_shared_budget() {
        let html = b"<html><body><ul><li class=\"c\">A</li></ul></body></html>";
        // every selector here WOULD match, so a live column is always non-empty and the count of
        // non-empty columns is exactly the count of live members
        for (normal, reverse) in [(100usize, 100usize), (0, 200), (200, 0), (127, 2)] {
            let mut qs: Vec<String> = vec![".c::text".to_string(); normal];
            qs.extend(vec!["li:last-child::text".to_string(); reverse]);
            let (members, _) = budget_usage(&qs, &[]);
            let cols = extract(html, &qs, None);
            let live = cols.iter().filter(|c| !c.is_empty()).count();
            assert!(
                live <= matcher::MAX_MEMBERS,
                "{normal} normal + {reverse} reverse: {members} members reported, {live} live \
                 (limit {})",
                matcher::MAX_MEMBERS
            );
        }
        // and an IN-budget schema must still answer every column (the clamp must not over-fire)
        let qs: Vec<String> = vec![".c::text".to_string(); 60]
            .into_iter()
            .chain(vec!["li:last-child::text".to_string(); 60])
            .collect();
        let cols = extract(html, &qs, None);
        assert!(cols.iter().all(|c| !c.is_empty()), "in-budget schema lost a column");
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
        // Document order across nesting (outer before inner), not close order. `*` also matches the
        // SYNTHESIZED `<html>`/`<body>` — lxml returns the same four elements — and since outer HTML is
        // raw source, a synthesized element's is the source it spans, without tags it never had.
        assert_eq!(
            ex("<div><span>s</span></div>", "*"),
            v(&[
                "<div><span>s</span></div>", // the synthesized <html>, spanning the document
                "<div><span>s</span></div>", // ...and the <body> inside it
                "<div><span>s</span></div>",
                "<span>s</span>",
            ])
        );
        // scoped to what the page actually wrote, the frame is invisible
        assert_eq!(
            ex("<div><span>s</span></div>", "body > *"),
            v(&["<div><span>s</span></div>"])
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

    /// Raw source is raw about MARKUP, not about newlines: `\r\n` and lone `\r` become `\n` in HTML's
    /// input stream before anything parses, so libxml2 and html5lib both serialize `\n` whatever the
    /// bytes said. Text and attribute values already normalized and only the outer-HTML path did not —
    /// a CRLF-authored crawled page put `\r` in all eight of its node columns.
    #[test]
    fn outer_html_normalizes_newlines_like_the_input_stream() {
        assert_eq!(ex("<div>a\r\nb</div>", "div"), v(&["<div>a\nb</div>"]));
        assert_eq!(ex("<div>a\rb</div>", "div"), v(&["<div>a\nb</div>"]));
        assert_eq!(
            ex("<div>\r\n<p>x</p>\r\n</div>", "div"),
            v(&["<div>\n<p>x</p>\n</div>"])
        );
        // inside an attribute value of the captured span, too
        assert_eq!(
            ex("<div title=\"a\r\nb\">y</div>", "div"),
            v(&["<div title=\"a\nb\">y</div>"])
        );
        // and it does NOT entity-decode: that is what keeps the span raw
        assert_eq!(ex("<div>a&amp;b\r\nc</div>", "div"), v(&["<div>a&amp;b\nc</div>"]));
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
    /// ISO-2022-JP is NOT ASCII-compatible, so it has to be transcoded before tokenizing — see
    /// [`prepare_bytes`]. In `ESC $ B` mode a JIS pair is two bytes below 0x80: `社` is `<R`, which a
    /// byte tokenizer reads as a start tag, swallowing the character AND opening an `<r>` element that
    /// is not in the document. That is a FALSE POSITIVE — an element a scraper can match — which is the
    /// outcome no-fallback exists to prevent.
    #[test]
    fn encoding_iso_2022_jp_is_transcoded_not_byte_scanned() {
        // 株式会社 = ESC $ B 3t <0 2q <R ESC ( B  — note the two `<` bytes inside the word
        let body = b"<p>\x1b$B3t<02q<R\x1b(B</p><div>after</div>";
        assert!(body.windows(2).any(|w| w == b"<R"), "vector must contain the ambiguous pair");
        assert_eq!(
            extract(body, &["p::text".into()], Some("iso-2022-jp"))[0],
            v(&["株式会社"])
        );
        // and nothing downstream was reshaped by the phantom tag
        assert_eq!(
            extract(body, &["div::text".into(), "r::text".into()], Some("iso-2022-jp")),
            vec![v(&["after"]), v(&[])]
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

    /// Raw NUL is deleted from the WHOLE document before tokenizing, as Parsel/w3lib do. Dropping it
    /// only from emitted values (the previous behaviour) meant the two sides disagreed about the
    /// document's structure, so a NUL inside a tag or attribute NAME emptied the column outright.
    #[test]
    fn raw_nul_is_deleted_before_tokenizing_not_just_from_values() {
        assert_eq!(ex("<di\0v>X</di\0v>", "div::text"), v(&["X"]));
        assert_eq!(ex("<div cl\0ass=c>X</div>", "div.c::text"), v(&["X"]));
        assert_eq!(ex("<div id=a\0b>X</div>", "#ab::text"), v(&["X"]));
        assert_eq!(ex("<a hre\0f='/x'>t</a>", "a::attr(href)"), v(&["/x"]));
        assert_eq!(ex("<p>a\0b</p>", "p::text"), v(&["ab"]));
        assert_eq!(ex("<ul><li>a<li\0>b</ul>", "li::text"), v(&["a", "b"]));
        // a NUL-only text node is nothing at all, in both engines
        assert_eq!(ex("<div>\0</div>", "div::text"), Vec::<String>::new());
        // a CHARACTER REFERENCE to NUL is a different thing and still becomes U+FFFD
        assert_eq!(ex("<div>&#0;x</div>", "div::text"), v(&["\u{FFFD}x"]));
        // outer HTML is raw SOURCE, so the deletion shows there too — the one documented exception
        assert_eq!(ex("<di\0v id=x>t</div>", "div"), v(&["<div id=x>t</div>"]));
    }

    #[test]
    fn raw_nul_in_utf16_is_deleted_after_transcoding() {
        // Every ASCII character in UTF-16 carries a 0x00 byte, so the deletion has to happen on the
        // DECODED text or it would shred the document. What goes is the U+0000 character.
        //
        // The NUL goes in a TAG NAME on purpose: a NUL in a value is handled by the value decoder too,
        // so a test that only put one there passed with the document-level deletion disabled and proved
        // nothing. Structure is the half that needs it.
        let mut body = vec![0xFF, 0xFE]; // UTF-16LE BOM
        for c in "<di\0v id=a\0b>h\0i</di\0v>".chars() {
            body.push(c as u8);
            body.push(0);
        }
        assert_eq!(extract(&body, &["div::text".into()], None)[0], v(&["hi"]));
        assert_eq!(extract(&body, &["div#ab::text".into()], None)[0], v(&["hi"]));
    }

    #[test]
    fn void_attr() {
        assert_eq!(ex("<img src=\"a.png\"><img src=\"b.png\">", "img::attr(src)"), v(&["a.png", "b.png"]));
    }
    #[test]
    fn minimized_html4_boolean_attr_uses_its_name_as_value() {
        assert_eq!(ex("<frame noresize>", "frame::attr(noresize)"), v(&["noresize"]));
        assert_eq!(ex("<input DISABLED>", "input::attr(disabled)"), v(&["disabled"]));
        // Explicitly empty and arbitrary valueless attributes remain empty.
        assert_eq!(ex("<input disabled=\"\">", "input::attr(disabled)"), v(&[""]));
        assert_eq!(ex("<div hidden>", "div::attr(hidden)"), v(&[""]));
    }

    // ---- global-desync guards: rawtext / comments / entities ----
    #[test]
    fn rawtext_lt_inside_script() {
        // '<' inside <script> must NOT desync the parser
        assert_eq!(ex("<script>if (a<b) {x=\"</p>\"}</script><p>t</p>", "p::text"), v(&["t"]));
    }
    #[test]
    fn script_double_escaped_content_does_not_desync() {
        let h = "<script><!--[if IE]><script src=x></script><![endif]--><h4>not markup";
        assert_eq!(ex(h, "h4::text"), Vec::<String>::new());
        assert_eq!(
            ex(h, "script::text"),
            v(&["<!--[if IE]><script src=x></script><![endif]--><h4>not markup"])
        );
    }
    #[test]
    fn noframes_content_is_raw_text() {
        let h = "<html><frameset><frame noresize></frameset><noframes><body>go <a href=x>here</a></body></noframes></html>";
        assert_eq!(
            ex(h, "noframes::text"),
            v(&["<body>go <a href=x>here</a></body>"])
        );
        assert_eq!(ex(h, "noframes a::text"), Vec::<String>::new());
    }
    /// The rest of libxml2's data modes. Each of these used to tokenize its CONTENT as markup, which is
    /// worse than losing a value: it fabricates elements that are not in the document (and, because the
    /// end tag it then honours is the wrong one, desynchronizes every offset after it).
    #[test]
    fn iframe_noembed_xmp_content_is_raw_text() {
        for t in ["iframe", "noembed", "xmp"] {
            let h = format!("<{t}><div>fake</div></{t}><p>real</p>");
            assert_eq!(ex(&h, "div::text"), Vec::<String>::new(), "<{t}> must not hold a real div");
            assert_eq!(ex(&h, &format!("{t}::text")), v(&["<div>fake</div>"]));
            assert_eq!(ex(&h, "p::text"), v(&["real"]), "<{t}> must not desync what follows");
            // entities stay literal in all three (raw text, not RCDATA)
            let e = format!("<{t}>a&amp;b</{t}>");
            assert_eq!(ex(&e, &format!("{t}::text")), v(&["a&amp;b"]));
        }
    }

    #[test]
    fn plaintext_swallows_the_rest_of_the_document() {
        let h = "<div><plaintext>a<div>fake</div><p>after</p>";
        assert_eq!(ex(h, "plaintext::text"), v(&["a<div>fake</div><p>after</p>"]));
        assert_eq!(ex(h, "p::text"), Vec::<String>::new());
        // its own end tag is text too — nothing closes PLAINTEXT except end of document
        assert_eq!(ex("<plaintext>a</plaintext>b", "plaintext::text"), v(&["a</plaintext>b"]));
    }

    /// libxml2 accepts `<html>`/`<head>`/`<body>` only as the document frame. Both halves of that had
    /// observable consequences and neither was implemented.
    #[test]
    fn document_frame_tags_close_head_and_are_otherwise_ignored() {
        // `<body>` closes an open `<head>` — a full document may legally omit `</head>`, and without
        // this the whole body sat INSIDE the head.
        let doc = "<html><head><title>T</title><body><p>X</p>";
        assert_eq!(ex(doc, "html > body p::text"), v(&["X"]));
        assert_eq!(ex(doc, "head > body p::text"), Vec::<String>::new());
        assert_eq!(ex(doc, "html > head > title::text"), v(&["T"]));
        // a REDUNDANT frame tag inserts nothing at all, and does not split the text node around it
        assert_eq!(ex("<html><body><div>d<html>y</div>", "div::text"), v(&["dy"]));
        assert_eq!(ex("<html><body><div>d<body>y</div>", "div::text"), v(&["dy"]));
        assert_eq!(ex("<html><body><div>d<html id=Z>y</div>", "div > html::attr(id)"),
                   Vec::<String>::new());
        // ...but its implied closes still run: `<body>` closes an open `<p>`, so `y` is p's SIBLING
        assert_eq!(ex("<html><body><p>x<body>y", "p::text"), v(&["x"]));
        // and an end tag matching an ignored start tag pops that phantom, not the document
        assert_eq!(ex("<html><body><div><body>x</body>tail</div>", "div::text"), v(&["xtail"]));
        assert_eq!(ex("<html><body><div><body>x</div></body>tail", "div::text"), v(&["x"]));
        // ...whichever of the three names each of them is. libxml2 keeps a stack SLOT for the tag it
        // merged away, not a named token, so any frame end tag pops any frame phantom — a stray `<html>`
        // in the body makes the document's own `</body>` a no-op. Counting phantoms per name matched
        // only 2 of the 6 combinations.
        for stray in ["html", "head", "body"] {
            for closer in ["body", "html"] {
                let doc = format!("<html><body>x<{stray}>y</{closer}>tail");
                assert_eq!(ex(&doc, "body::text"), v(&["xytail"]),
                           "a stray <{stray}> must absorb the following </{closer}>");
            }
        }
    }

    /// ...but written SELF-CLOSING, that same redundant tag closes the element it sits in — see
    /// `Matcher::self_close_with_no_element_of_its_own`. Reading `<html/>` as "ignored, like `<html>`"
    /// left a crawled page's stray `<strong>` open around its whole document.
    #[test]
    fn a_self_closed_redundant_frame_tag_closes_its_enclosing_element() {
        for f in ["html", "head", "body"] {
            // one level, and only one: the `<div>` outside survives
            let doc = format!("<div><b>x<{f}/>y<i>S</i>");
            assert_eq!(ex(&doc, "b::text"), v(&["x"]), "<{f}/> must end the <b>");
            assert_eq!(ex(&doc, "div::text"), v(&["y"]), "<{f}/> must not end the <div>");
            assert_eq!(ex(&doc, "div > i::text"), v(&["S"]));
            // it pops what is CURRENT, whatever that is — including a table cell
            assert_eq!(ex(&format!("<table><tr><td>x<{f}/>y"), "td::text"), v(&["x"]));
            // after the implied closes this tag runs, not instead of them — which is visible in the
            // split between the three names: `<head>`/`<body>` close an open `<p>` and `<html>` does
            // not, so the extra pop lands one level further out for those two.
            let implied = format!("<div>d<p>x<{f}/>y");
            let outer = if f == "html" { v(&["d", "y"]) } else { v(&["d"]) };
            assert_eq!(ex(&implied, "div::text"), outer);
            // and the phantom it leaves still absorbs a later frame end tag — which is exactly what
            // separates it from `<html></html>`, where the written end tag pops the phantom instead
            assert_eq!(ex(&format!("<div><b>x<{f}/>y</body>z"), "div::text"), v(&["yz"]));
            assert_eq!(ex(&format!("<div><b>x<{f}/>y</body></body>z"), "div::text"), v(&["y"]));
        }
        // a non-frame self-closing tag is unaffected: it has an element of its own to close
        assert_eq!(ex("<div><b>x<zz/>y", "b::text"), v(&["x", "y"]));
    }

    /// A document that writes no `<html>`/`<head>`/`<body>` still HAS them: libxml2 synthesizes the
    /// frame, and so does the engine now.
    ///
    /// This was the largest documented gap. `<html>`, `<head>` and `<body>` all have optional start
    /// *and* end tags, so `<!DOCTYPE html><title>T</title><h1>a</h1><p>b</p>` is a conformant document
    /// with no frame in the byte stream at all — and every frame-anchored selector (`body h1`), every
    /// top-level sibling combinator (`h1 + p`, which needs a shared parent), and all root-level text
    /// were empty here while lxml answered.
    ///
    /// Which part a bare element opens is DERIVED from the oracle over the whole element universe
    /// (`implied_close::frame_content`), because it is not the relation it looks like: only
    /// `base`/`link`/`meta`/`script`/`style`/`title` open a `<head>`, while `input`/`noscript`/
    /// `template`/`basefont`/`bgsound`/`object` stay inside a head that is already open but open none.
    #[test]
    fn the_document_frame_is_synthesized_when_the_page_omits_it() {
        // the conformant-but-frameless document from docs/COMPATIBILITY.md
        let doc = "<!DOCTYPE html><title>T</title><h1>a</h1><p>b</p>";
        assert_eq!(ex(doc, "head > title::text"), v(&["T"]));
        assert_eq!(ex(doc, "body h1::text"), v(&["a"]));
        assert_eq!(ex(doc, "h1 + p::text"), v(&["b"])); // needs a shared parent to be siblings
        assert_eq!(ex(doc, "html > body > p::text"), v(&["b"]));
        assert_eq!(ex(doc, "body > :first-child::text"), v(&["a"]));

        // a bare fragment gets a body, and root-level text is no longer dropped
        assert_eq!(ex("<div>a</div>", "body div::text"), v(&["a"]));
        assert_eq!(ex("abc", "body::text"), v(&["abc"]));
        assert_eq!(ex("abc<div>d</div>", "body::text"), v(&["abc"]));
        // ...but whitespace before the frame starts is not content and starts nothing
        assert_eq!(ex("   \n <div>a</div>", "body > :first-child::text"), v(&["a"]));
        assert_eq!(ex("   ", "body::text"), Vec::<String>::new());

        // the head is opened only by the six names that open one, and only before the body starts
        assert_eq!(ex("<meta id=M><div>d</div>", "head > meta::attr(id)"), v(&["M"]));
        assert_eq!(ex("<div>d</div><meta id=M>", "body > meta::attr(id)"), v(&["M"]));
        assert_eq!(ex("<div>d</div><meta id=M>", "head > meta::attr(id)"), Vec::<String>::new());
        assert_eq!(ex("abc<meta id=M>", "body > meta::attr(id)"), v(&["M"]));
        // an element that merely SURVIVES in an open head does not open one
        for body_starter in ["input", "noscript", "template", "basefont", "bgsound", "object"] {
            let d = format!("<{body_starter} id=Z>");
            assert_eq!(ex(&d, &format!("body > {body_starter}::attr(id)")), v(&["Z"]),
                       "<{body_starter}> must open a body, not a head");
        }
        // head content NESTED in body content stays where it is
        assert_eq!(ex("<div><meta id=M></div>", "body div > meta::attr(id)"), v(&["M"]));

        // a frameset document has no body at all — including when the `<frameset>` is written INSIDE the
        // head, where it ends the head like any other non-head content but must NOT start a body. A real
        // frameset page does exactly that, and wrapping it in an invented body put the whole frameset
        // (and the `<body>` written after it) somewhere libxml2 never puts them.
        assert_eq!(ex("<html><head><frameset id=F><frame src=a></frameset></head><body>y</body>",
                      "html > frameset::attr(id)"), v(&["F"]));
        assert_eq!(ex("<html><head><frameset><frame src=a></frameset></head><body>y</body>",
                      "frameset + body::text"), v(&["y"]));
        assert_eq!(ex("<html><head><title>t</title><frameset id=F><frame src=a></frameset>",
                      "html > frameset::attr(id)"), v(&["F"]));
        // ...while an ordinary tag that ends the head still starts one
        assert_eq!(ex("<html><head><title>t</title><div>d</div>", "body div::text"), v(&["d"]));
        assert_eq!(ex("<frameset><frame src=a></frameset>", "html > frameset > frame::attr(src)"),
                   v(&["a"]));
        assert_eq!(ex("<frameset><frame src=a></frameset>", "body frame::attr(src)"),
                   Vec::<String>::new());
        // ...unless it WRITES one for its no-frames fallback, which libxml2 inserts: a written `<body>`
        // is ignored only while one is already OPEN, not merely because something else is. Two crawled
        // pages needed this — a frameset fallback, and a `<body>` after a `</body>` inside a table cell,
        // where ignoring it left a whole trailing table nested in the earlier cell.
        assert_eq!(ex("<frameset><body><p>gb</p></body></frameset>", "body > p::text"), v(&["gb"]));
        assert_eq!(ex("<frameset><frameset><body><p>gb</p></frameset></frameset>", "body p::text"),
                   v(&["gb"]));
        assert_eq!(ex("<body id=1></body><td><body id=2>x", "//body/@id"), v(&["1", "2"]));
        assert_eq!(ex("<body id=1></body><div><body id=2>x", "//body/@id"), v(&["1", "2"]));
        // ...and while one IS open it is ignored, however deep
        assert_eq!(ex("<body id=1><div><body id=2>x</div>", "//body/@id"), v(&["1"]));
        assert_eq!(ex("<td><body id=Z>y", "body::attr(id)"), Vec::<String>::new());
        // `<head>` has the other rule — it belongs to the phase before any body content, so anything
        // else being open ends it whether or not a head is currently open.
        assert_eq!(ex("<frameset><head id=Z><title>t</title></frameset>", "head::attr(id)"),
                   Vec::<String>::new());
        assert_eq!(ex("<head id=1></head><div><head id=2>x", "//head/@id"), v(&["1"]));
        // Character data ends an open head even when there is no body to move it into: a `<head>`
        // written after `</body>` keeps nothing but its elements.
        assert_eq!(ex("<body id=0></body><head>y</head>", "head::text"), Vec::<String>::new());
        assert_eq!(ex("<body id=0></body><head><title>t</title>y</head>", "head > title::text"),
                   v(&["t"]));

        // A page whose FIRST tag is `<head>` or `<body>` writes no `<html>` — and still gets one. The
        // frame tags used to build only their own part, so the head sat at the root with no parent and a
        // second `<html>` was built for whatever followed `</head>`: `html > head`, `html > body` and
        // `head + script` were all empty while the values under them looked right.
        assert_eq!(ex("<head id=H><title>t</title></head><p>y</p>", "html > head::attr(id)"),
                   v(&["H"]));
        assert_eq!(ex("<body id=B><p>y</p></body>", "html > body::attr(id)"), v(&["B"]));
        assert_eq!(ex("<head id=H></head><body id=B><p>y</p>", "html > head + body::attr(id)"),
                   v(&["B"]));
        // libxml2 leaves what follows an explicit `</head>` at `<html>` level (html5lib puts it back in
        // the head; the oracle here is libxml2), so the script is the head's SIBLING.
        assert_eq!(ex("<head><meta id=M></head><script>s</script><p>y</p>", "head + script::text"),
                   v(&["s"]));

        // A second `<html>` after the first has CLOSED gets its own element, because libxml2 builds a
        // second ROOT for it — verified on a crawled page that self-closes `<html/>` inside a
        // downlevel-revealed conditional comment, whose lxml tree has two roots both carrying the
        // attributes. Browsers keep one element instead, which is why parsel's own CSS (scoped to the
        // first root) and XPath disagree on such a document; the TREE is the oracle here.
        assert_eq!(ex("<html a=1 /><html a=2><p>x</p>", "//html/@a"), v(&["1", "2"]));
        assert_eq!(ex("<html a=1></html><html a=2><p>x</p>", "//html/@a"), v(&["1", "2"]));
        // ...and the tail still gets a parent, so the values under it stay reachable
        assert_eq!(ex("<html a=1></html><p>x</p>", "html > body > p::text"), v(&["x"]));
        // Head content inside an open `<frameset>` has no head to go in, so libxml2 opens a BODY for it —
        // the same one it opens for ordinary content there. Exactly the six `FrameContent::Head` names
        // change answer inside a frameset; found by `tools/seq_sweep.py`, then derived over the universe.
        for head_content in ["title", "meta", "style", "link", "base", "script"] {
            let doc = format!("<frameset><{head_content} id=Z>y");
            assert_eq!(ex(&doc, "//body//*/@id"), v(&["Z"]),
                       "<{head_content}> in a frameset belongs to a body");
            assert_eq!(ex(&doc, "//head//*/@id"), Vec::<String>::new());
        }
        // `</head>` is an unconditional closer like `</body>`/`</html>`: libxml2 gives it the same end
        // priority, so no open element out-ranks it. With `head` left at priority 0 an open `<tr>` blocked
        // it and everything after the `</head>` stayed inside the head.
        assert_eq!(ex("<head id=0><tr id=1></head><div id=3>", "//*[@id=\"0\"]//*/@id"), v(&["1"]));
        assert_eq!(ex("<head id=0><td id=1></head><div id=3>", "//*[@id=\"0\"]//*/@id"), v(&["1"]));
        // Text after `</html>` needs the second root too, or it is dropped outright: with the stack empty
        // there is nothing to attach it to.
        assert_eq!(ex("<div id=0></html>x", "//html/text()"), v(&["x"]));
        assert_eq!(ex("<html><body>y</body></html>tail", "//html/text()"), v(&["tail"]));

        // Content after `</html>` gets a SECOND ROOT `<html>` — the same shape libxml2 builds — and no
        // body, because one was already established. A crawled page's trailing `<script>` was reachable
        // as `//script` but not as `//html/script` until the tail had that frame.
        assert_eq!(ex("<html><body>x</body></html><script>s</script>", "//html/script/text()"),
                   v(&["s"]));
        assert_eq!(ex("<html><body>x</body></html><p>y</p>", "//html/p/text()"), v(&["y"]));
        assert_eq!(ex("<html><body>x</body></html><p>y</p>", "//html/body/p/text()"),
                   Vec::<String>::new());

        // an explicit frame is still used as written — nothing is invented on top of it
        assert_eq!(ex("<html><head><title>T</title></head><body><p>b</p></body></html>",
                      "html > head > title::text"), v(&["T"]));
        assert_eq!(ex("<html><div>d</div></html>", "html > body > div::text"), v(&["d"]));
        assert_eq!(ex("<html>  <meta id=M>", "html > head > meta::attr(id)"), v(&["M"]));
    }

    /// The first thing that does not belong in `<head>` ends the head AND OPENS THE BODY, and
    /// everything after it — including the remaining `<meta>`/`<link>`/`<title>`/`<script>` — is a child
    /// of that body.
    ///
    /// The engine already closed the head (the start-close relation has it) but had no body to open, so
    /// the content landed under `<html>` instead and every frame-anchored or positional selector
    /// disagreed: `head + body::text` and `html > body::text` came back empty, and `a:first-child`
    /// picked a different element — in both directions, because the relocation both adds elements to
    /// the body and removes them from the head.
    ///
    /// Checked against BOTH oracles, which is why this is a fix and not a divergence: libxml2 and
    /// html5lib (the HTML5 spec reference implementation) place the content identically here, on every
    /// shape below and on the four crawled pages that surfaced it. They part company only *after an
    /// explicit* `</head>` — `<html><head></head><meta>` leaves the meta at `<html>` level in libxml2
    /// and back in the head in html5lib — so that path is deliberately untouched, and the body is
    /// synthesized only where the head ends IMPLICITLY.
    #[test]
    fn non_head_content_ends_the_head_and_opens_the_body() {
        // a start tag that closes the head opens the body, and the head's own elements stay behind
        let doc = "<html><head><style>s</style><div>D</div><link rel=x></head><body><p>P</p></body>";
        assert_eq!(ex(doc, "head > style::text"), v(&["s"]));
        assert_eq!(ex(doc, "body > div::text"), v(&["D"]));
        assert_eq!(ex(doc, "head > div::text"), Vec::<String>::new());
        // the <link> AFTER the head ended is a body child, not a head child
        assert_eq!(ex(doc, "body > link::attr(rel)"), v(&["x"]));
        assert_eq!(ex(doc, "head > link::attr(rel)"), Vec::<String>::new());
        // ...and the explicit <body> that follows is redundant, so there is exactly one body
        assert_eq!(ex(doc, "body > p::text"), v(&["P"]));
        assert_eq!(ex(doc, "body body p::text"), Vec::<String>::new());

        // NON-WHITESPACE TEXT does it too, and the run SPLITS at the first non-space character:
        // the leading whitespace is still the head's, the rest starts the body.
        let t = "<html><head>\n\t  TXT<meta charset=x></head><body><p>P</p>";
        assert_eq!(ex(t, "body::text"), v(&["TXT"]));
        assert_eq!(ex(t, "head::text"), v(&["\n\t  "]));
        assert_eq!(ex(t, "body > meta::attr(charset)"), v(&["x"]));
        // whitespace ALONE is not "content" and leaves the head open
        assert_eq!(ex("<html><head>\n <title>T</title></head><body><p>P</p>",
                      "head > title::text"), v(&["T"]));
        // nor is text INSIDE a head element — only text whose parent is the head itself
        assert_eq!(ex("<html><head><title>T</title></head><body><p>P</p>",
                      "head > title::text"), v(&["T"]));

        // frame-anchored and positional selectors are the ones that were wrong, in both directions
        let a = "<html><head><style>s</style><a href=x>A</a></head><body><p>P</p>";
        assert_eq!(ex(a, "a:first-child::text"), v(&["A"])); // a IS body's first element child
        assert_eq!(ex(a, "head + body a::text"), v(&["A"]));
        assert_eq!(ex(a, "html > body a::text"), v(&["A"]));

        // head-only elements do NOT end the head, so they must not open a body
        for keep in ["<meta charset=x>", "<link rel=r>", "<base href=b>", "<script>s</script>"] {
            let d = format!("<html><head>{keep}<title>T</title></head><body><p>P</p>");
            assert_eq!(ex(&d, "head > title::text"), v(&["T"]), "{keep} must not end the head");
        }
        // a document with no `<head>` at all reaches the body through frame synthesis instead
        assert_eq!(ex("<div>a</div>", "body div::text"), v(&["a"]));
        assert_eq!(ex("<div>a</div>", "head div::text"), Vec::<String>::new());
    }

    /// A TAG NAME runs to whitespace, `>` or `/` — every other byte is part of the name, including `<`.
    ///
    /// libxml2's `htmlParseHTMLName` stops only at those three, so `<p<img src=s>` is one element named
    /// `p<img`, not a `<p>` carrying an odd attribute. The engine ended the name at the first byte
    /// outside `[A-Za-z0-9_:-]` and so reported a `p` that is not in the document — a FALSE POSITIVE,
    /// which is the failure mode the no-fallback rule exists to prevent (an unsupported query returns
    /// nothing; a supported one must not return something that is not there).
    ///
    /// Found on a crawled page that writes `<p<mip-img …>` twelve times. The tokenizer already had the
    /// rule in one place — `find_raw_end` ends a rawtext close tag on exactly whitespace/`>`/`/` — so
    /// the two name scanners disagreed with each other as well as with the oracle.
    #[test]
    fn a_tag_name_ends_only_at_whitespace_slash_or_gt() {
        // the crawled shape: `p` must NOT match, because the element is not a `p`
        let doc = "<div><p<img src=s>T</div>";
        assert_eq!(ex(doc, "p::text"), Vec::<String>::new());
        assert_eq!(ex(doc, "div > *::text"), v(&["T"])); // it IS an element, just not that one
        // ...and neither do the other bytes libxml2 keeps in a name
        for bad in ["<p.x>", "<p#x>", "<p\"x>", "<p=x>", "<p&x>", "<p,x>", "<p(x)>", "<p@x>"] {
            let d = format!("<div>{bad}T</div>");
            assert_eq!(ex(&d, "p::text"), Vec::<String>::new(), "{bad} must not be a <p>");
            assert_eq!(ex(&d, "div > *::text"), v(&["T"]), "{bad} must still be an element");
        }
        // an END tag reads the name the same way, so it closes that element and not the short one
        assert_eq!(ex("<div><p<img>T</p<img>U</div>", "div::text"), v(&["U"]));
        assert_eq!(ex("<div><p>T</p.x>U</div>", "p::text"), v(&["TU"])); // `</p.x>` closes no `<p>`

        // `/` and whitespace still terminate, so an ordinary tag is untouched
        assert_eq!(ex("<div><p/x>T</div>", "p::text"), v(&["T"]));
        assert_eq!(ex("<div><p class=a>T</div>", "p.a::text"), v(&["T"]));
        assert_eq!(ex("<div><p\tid=i>T</div>", "p#i::text"), v(&["T"]));
        assert_eq!(ex("<img src=s><p>T", "p::text"), v(&["T"]));
        // a `<` that does not begin a tag is still text, so ordinary prose is unaffected
        assert_eq!(ex("<p>a < b and 3<4</p>", "p::text"), v(&["a < b and 3<4"]));
    }

    /// HTML5's END-TAG-OPEN state: only an ASCII alpha starts an end tag.
    ///
    /// The engine collapsed all three branches into "scan a name, skip to `>`", and `is_name_char`
    /// accepts `%`/`1`/`-`, so `</%>` became an end tag named `%` — dropped as unmatched, JOINING the
    /// text either side, where libxml2 keeps a bogus-comment node that SPLITS it. A real page's
    /// `Copyright 1991-2026</%> VECMAR Corporation` came back as one text node instead of two. `</>` was
    /// the same bug the other way round: no event at all, so the runs were left un-joined where libxml2
    /// ignores the whole thing and keeps ONE node.
    #[test]
    fn end_tag_open_only_starts_a_tag_on_a_letter() {
        // a BOGUS COMMENT is a node, so it splits the run
        for bogus in ["</%>", "</1>", "</-x>", "</ >", "</%%%>"] {
            let doc = format!("<span>a{bogus}b</span>");
            assert_eq!(ex(&doc, "span::text"), v(&["a", "b"]), "{bogus} must split the text node");
        }
        // ...and it runs to the first `>`, quotes and all, so this is a LOCAL difference and not an
        // offset desync: both engines resume at the same byte
        assert_eq!(ex("<span>a</% x=\">\">b</span>", "span::text"), v(&["a", "\">b"]));
        // `</>` is ignored entirely — no node, so the runs are ONE
        assert_eq!(ex("<span>a</>b</span>", "span::text"), v(&["ab"]));
        // a real end tag still behaves like one, matched or not
        assert_eq!(ex("<span>a</p>b</span>", "span::text"), v(&["ab"]));
        assert_eq!(ex("<span>a</span>b", "span::text"), v(&["a"]));
        // EOF right after `</`: character data, joined to the run before it
        assert_eq!(ex("<span>a</", "span::text"), v(&["a</"]));
    }

    /// A DOCTYPE is invisible to the text node around it; every other declaration form breaks it.
    ///
    /// Measured against the oracle rather than assumed: `<!foo>`, `<![CDATA[…]]>`, `<?x?>` and `<!>`
    /// all end the run in libxml2, and only `<!DOCTYPE …>` does not. Found on a crawled page that opens
    /// with stray U+FEFF characters and then its doctype, which split what libxml2 keeps as one node.
    #[test]
    fn a_doctype_does_not_break_a_text_node() {
        assert_eq!(ex("<div>a<!doctype html>b</div>", "div::text"), v(&["ab"]));
        assert_eq!(ex("<div>a<!DOCTYPE HTML>b</div>", "div::text"), v(&["ab"]));
        assert_eq!(ex("<div>a<!doctype html><!doctype html>b</div>", "div::text"), v(&["ab"]));
        // ...but everything else in the `<!`/`<?` family DOES break it, in both engines
        assert_eq!(ex("<div>a<!--c-->b</div>", "div::text"), v(&["a", "b"]));
        assert_eq!(ex("<div>a<!foo>b</div>", "div::text"), v(&["a", "b"]));
        assert_eq!(ex("<div>a<![CDATA[x]]>b</div>", "div::text"), v(&["a", "b"]));
        assert_eq!(ex("<div>a<?x?>b</div>", "div::text"), v(&["a", "b"]));
        assert_eq!(ex("<div>a<!>b</div>", "div::text"), v(&["a", "b"]));
        // a doctype followed by a comment still breaks — on the comment
        assert_eq!(ex("<div>a<!doctype html><!--c-->b</div>", "div::text"), v(&["a", "b"]));
        // libxml2 matches on the seven-letter PREFIX, case-insensitively, and does not require the name
        // to be terminated — so these are doctypes and `<!doctyp>` is not
        assert_eq!(ex("<div>a<!doctypex>b</div>", "div::text"), v(&["ab"]));
        assert_eq!(ex("<div>a<!DoCtYpE x>b</div>", "div::text"), v(&["ab"]));
        assert_eq!(ex("<div>a<!doctype>b</div>", "div::text"), v(&["ab"]));
        assert_eq!(ex("<div>a<!doctyp>b</div>", "div::text"), v(&["a", "b"]));
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
    /// An INDENTED BOM still counts as the document BOM, because Parsel parses `text.strip()` — see
    /// [`document_start`]. What makes this worth a test rather than a footnote is the size of the
    /// silence when it is missed: the U+FEFF is a character, a character before the frame opens the
    /// `<body>`, and from there the page's whole head is outside `head` and its `<html>` tag is a
    /// redundant duplicate whose attributes are dropped.
    #[test]
    fn indented_bom_is_still_the_document_bom() {
        let q: Vec<String> = ["head title::text", "html::attr(xmlns)", "body::text"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let doc = b"  \t\n\xEF\xBB\xBF<!DOCTYPE HTML><html xmlns=\"x\"><head><title>T</title></head><body>b";
        assert_eq!(extract(doc, &q, None), vec![v(&["T"]), v(&["x"]), v(&["b"])]);
        // ...but only whitespace may precede it: after anything else the frame is already open, so the
        // U+FEFF is content — libxml2 keeps it and so does Parsel, whose strip cannot reach it.
        let after = b"z\xEF\xBB\xBF<!DOCTYPE HTML><html xmlns=\"x\"><head><title>T</title></head><body>b";
        assert_eq!(
            extract(after, &q, None),
            vec![v(&[]), v(&[]), v(&["z\u{FEFF}", "b"])]
        );
        // and a non-UTF-8 page decodes those three bytes as three characters, exactly as Parsel does
        let cp1252 = b"  \xEF\xBB\xBF<html><head><title>T</title></head><body>b";
        assert_eq!(
            extract(cp1252, &q, Some("windows-1252"))[0],
            v(&[]),
            "EF BB BF is not a BOM outside UTF-8"
        );
    }
    /// A stray `=` where an attribute name should start is that name's first character, not a
    /// separator — so the attribute AFTER it is still parsed. Reading it as a separator dropped the
    /// real attribute (empty name) and swallowed the next one as its value.
    #[test]
    fn a_stray_equals_before_an_attribute_name_does_not_eat_the_next_attribute() {
        assert_eq!(ex("<div = class='x'>y</div>", "div::attr(class)"), v(&["x"]));
        assert_eq!(ex("<div = class='x'>y</div>", "div[class]::text"), v(&["y"]));
        // ...including after a real attribute, and with the id still readable
        let both = "<div id=q = class='x'>y</div>";
        assert_eq!(ex(both, "div::attr(class)"), v(&["x"]));
        assert_eq!(ex(both, "div::attr(id)"), v(&["q"]));
        // the `=` itself is part of the name that follows it, so it consumes no real attribute
        assert_eq!(ex("<div =foo class=c>y</div>", "div::attr(class)"), v(&["c"]));
        // and `==x` is one attribute named `=` with value `x` — the next attribute still parses
        assert_eq!(ex("<div ==x class=c>y</div>", "div::attr(class)"), v(&["c"]));
    }

    /// A start tag the response ends inside is DROPPED, whole — libxml2 and html5lib agree, and the
    /// engine used to keep whatever it had scanned. That is the false-positive direction: an element,
    /// with an attribute value holding the rest of the document, that no other parser reports.
    #[test]
    fn a_start_tag_cut_off_by_eof_is_dropped() {
        for tail in ["<a", "<a ", "<a href", "<a href=", "<a href=\"x", "<a href='x", "<a href=x"] {
            let doc = format!("<p>t</p>{tail}");
            assert_eq!(ex(&doc, "a::attr(href)"), Vec::<String>::new(), "{tail}");
            assert_eq!(ex(&doc, "a"), Vec::<String>::new(), "{tail} must not exist at all");
            // the text before it is untouched, and does not gain the dropped bytes
            assert_eq!(ex(&doc, "p::text"), v(&["t"]), "{tail}");
        }
        // the real shape: an unterminated quoted value swallowing the document tail
        let real = "<p>t<a href=\"login.?>login</a>]</td></table></body>\n</html>";
        assert_eq!(ex(real, "a::attr(href)"), Vec::<String>::new());
        assert_eq!(ex(real, "p::text"), v(&["t"]));
        // a tag that IS terminated keeps working, closing `>` or `/>`
        assert_eq!(ex("<p>t</p><a href=\"x\">y", "a::attr(href)"), v(&["x"]));
        assert_eq!(ex("<p>t</p><img src=\"x\"/>", "img::attr(src)"), v(&["x"]));
    }

    /// The other half of that same `text.strip()`: trailing whitespace is not a text node, because
    /// Parsel removed it before libxml2 ever saw it — see [`document_end`]. Whitespace-only, so it
    /// changes no value, but it does change the ROW COUNT of a `::text` column.
    #[test]
    fn trailing_whitespace_is_not_a_text_node() {
        let doc = b"<select><option>a<option class=c>\n\t ";
        assert_eq!(extract(doc, &["option::text".into()], None)[0], v(&["a"]));
        // ...and a document that is nothing but whitespace has no content at all
        assert_eq!(extract(b"  \n ", &["p::text".into()], None)[0], v(&[]));
        // non-whitespace at the end is untouched, trailing whitespace INSIDE it too
        let kept = b"<p>a </p>\n";
        assert_eq!(extract(kept, &["p::text".into()], None)[0], v(&["a "]));
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
