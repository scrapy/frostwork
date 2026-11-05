//! Frostwork: a streaming HTML parser for extraction — a bespoke tokenizer + corrected-stack matcher, no
//! fallback. Lxml mode always streams in one pass; browser mode answers a page where an HTML5 rule
//! rewrites elements already parsed from a compact tree of the finished page (the replay), and reads a
//! deferred predicate's inner value in a second pass. See docs/DESIGN.md and docs/COMPATIBILITY.md.
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

pub use encoding::{EncodingHints, EncodingSource};
pub use matcher::{MAX_MEMBERS, MAX_SIB_BITS};
pub use page::{CompiledPage, Field, Item, Page};
pub use selector::Selector;

/// Which parser's tree a schema is answered against. Fixed when a schema is compiled (see
/// [`CompileOptions`]), because the tree rules are part of the compiled matcher: one schema runs in one
/// mode.
///
/// `Browser`, the default, is graded against real browsers (Chrome where Chrome and Firefox disagree):
/// raw input bytes with no stripping, HTML5's NUL rules, and HTML5 tree construction as Chrome runs it,
/// with scripting on (`<noscript>` is raw text) and `<template>` content kept out of the document. It
/// refuses outer HTML for a query that can match an element the page never wrote, which has no source
/// of its own (see [`audit_schema_with`]). `Lxml` is value parity with lxml/libxml2 2.15 behind Parsel,
/// the contract docs/COMPATIBILITY.md describes and every lxml gate proves. docs/BROWSER_MODE.md has the
/// design and the measured browser answers.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash)]
pub enum ParseMode {
    #[default]
    Browser,
    Lxml,
}

impl ParseMode {
    /// The mode's public name, as the Python `parser=` keyword and the bins spell it.
    pub fn as_str(self) -> &'static str {
        match self {
            ParseMode::Browser => "browser",
            ParseMode::Lxml => "lxml",
        }
    }

    /// The mode a public name spells, or `None` for any other string (exact, lowercase).
    pub fn from_name(name: &str) -> Option<ParseMode> {
        match name {
            "browser" => Some(ParseMode::Browser),
            "lxml" => Some(ParseMode::Lxml),
            _ => None,
        }
    }
}

/// How [`Plan::compile_with`] compiles a schema: the parse mode its tree is built under, and the
/// per-column cardinality described at [`Plan::compile_first_only`] (`&[]` declares none).
#[derive(Clone, Copy, Debug, Default)]
pub struct CompileOptions<'a> {
    pub mode: ParseMode,
    pub first_only: &'a [bool],
}

/// One value-column per flat query — the output of [`extract`].
pub type FlatColumns = Vec<Vec<String>>;
/// A grouped (`Many`/`One`) query's rows: indexed `[row][subfield][value]`.
pub type GroupRows = Vec<Vec<Vec<String>>>;

/// Extract each query's values in one run, in the default [`ParseMode`]. Unsupported queries
/// yield an empty column (there is no fallback — that is the whole point of Frostwork). Values are
/// per-text-node, whitespace-kept and entity-decoded, as Parsel's are; for lxml parity compile a
/// [`Plan`] with [`ParseMode::Lxml`].
///
/// `encoding` is an optional caller label for an encoding the user forced; a BOM still outranks it. When
/// `None` the encoding is sniffed (BOM -> `<meta>` -> autodetection). `url` is the URL of the response,
/// if known: the top-level domain of its host informs autodetection. To pass a response's `Content-Type`
/// header as well, use [`Plan::extract_with`]. Structural tokenization runs on raw bytes for every
/// ASCII-compatible encoding; only the small emitted values are decoded with the resolved encoding.
/// UTF-16LE/BE are transcoded to UTF-8 up front (rare).
pub fn extract<Q: AsQuery>(
    html: &[u8],
    queries: &[Q],
    encoding: Option<&str>,
    url: Option<&str>,
) -> Vec<Vec<String>> {
    extract_grouped(html, queries, &[], encoding, url).0
}

/// A `Many`/`One` grouped query: for every element matching `container`, extract each `subfields`
/// selector **scoped to that element** (descendant-or-self), all in the same streaming pass. The
/// subfield names are carried for the caller's convenience; the engine keys sub-columns positionally.
#[derive(Clone, Debug)]
pub struct GroupQuery<Q = String> {
    pub container: Q,
    pub subfields: Vec<(String, Q)>, // (name, selector)
}

/// The grammar a query string is written in. `Auto` applies the routing rule in [`is_xpath`]; the
/// other two bypass it, so a relative XPath step such as `h1` reaches the XPath compiler (which
/// refuses it) instead of being read as a CSS type selector and matching every `<h1>`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Syntax {
    #[default]
    Auto,
    Css,
    XPath,
}

/// A query string with its grammar declared. A plain `String`/`&str` in the same position is the
/// same query under [`Syntax::Auto`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Query {
    pub text: String,
    pub syntax: Syntax,
}

impl Query {
    pub fn new(text: impl Into<String>, syntax: Syntax) -> Query {
        Query { text: text.into(), syntax }
    }
}

/// What the schema entry points accept as a query: a `str`/`String`, routed by [`Syntax::Auto`], or a
/// [`Query`] carrying its own syntax.
pub trait AsQuery {
    fn text(&self) -> &str;
    fn syntax(&self) -> Syntax {
        Syntax::Auto
    }
}

impl AsQuery for str {
    fn text(&self) -> &str {
        self
    }
}

impl AsQuery for String {
    fn text(&self) -> &str {
        self
    }
}

impl AsQuery for Query {
    fn text(&self) -> &str {
        &self.text
    }
    fn syntax(&self) -> Syntax {
        self.syntax
    }
}

impl<T: AsQuery + ?Sized> AsQuery for &T {
    fn text(&self) -> &str {
        (**self).text()
    }
    fn syntax(&self) -> Syntax {
        (**self).syntax()
    }
}

/// Is `qt` an XPath query rather than CSS? Under [`Syntax::Auto`] that means an absolute/`.`-rooted
/// path or a `normalize-space(...)` wrapper. The one routing rule, shared by [`compile_query`] /
/// [`compile_one`] and by [`diagnostics`] — an explainer that classified a query differently from the
/// compiler would name the wrong cause.
pub(crate) fn is_xpath(qt: &str, syntax: Syntax) -> bool {
    match syntax {
        Syntax::Auto => {
            qt.starts_with('/') || qt.starts_with("./") || qt.starts_with("normalize-space(")
        }
        Syntax::Css => false,
        Syntax::XPath => true,
    }
}

/// Compile a single query string to one `Selector` (CSS first-member, or downward XPath); `None` if
/// unsupported. Used for group containers and sub-fields (MVP: no comma groups there). A
/// `normalize-space(...)` value is rejected here — it is a flat-query terminal, not a container/
/// sub-field selector (the grouped paths don't route its scalar output).
fn compile_one(q: &str, syntax: Syntax) -> Option<Selector> {
    let qt = q.trim();
    if is_xpath(qt, syntax) {
        match xpath::compile(qt) {
            Some(s) if matches!(s.terminal, selector::Terminal::NormalizeSpace(_)) => None,
            other => other,
        }
    } else {
        let mut members = selector::parse_list(q);
        // Group containers/sub-fields accept exactly ONE selector. Taking `.next()` instead would run
        // the first member of `div, span` and silently discard the rest — a partial, wrong answer, which
        // the no-fallback contract forbids. Multi-member CSS is therefore unsupported in this shape,
        // exactly like multi-member XPath unions/or-expansions.
        (members.len() == 1).then(|| members.pop()).flatten()
    }
}

/// Group-relative XPath uses an explicit context depth on the first compound.
fn compile_sub(q: &str, syntax: Syntax) -> Option<Selector> {
    let qt = q.trim();
    if syntax == Syntax::Css {
        return compile_one(q, syntax);
    }
    let (mut selector, depth) = if qt == "." {
        (compile_one("*", Syntax::Css)?, 0)
    } else if qt == "text()" || qt == "./text()" {
        (xpath::compile("//*/text()")?, 0)
    } else if qt == ".//text()" {
        (xpath::compile("//*//text()")?, 0)
    } else if let Some(name) = qt.strip_prefix(".//@") {
        (xpath::compile(&format!("//*//@{name}"))?, 0)
    } else if let Some(name) = qt.strip_prefix("./@").or_else(|| qt.strip_prefix('@')) {
        (xpath::compile(&format!("//*/@{name}"))?, 0)
    } else if qt.starts_with("./") && !qt.starts_with(".//") {
        (compile_one(&format!(".//{}", &qt[2..]), Syntax::XPath)?, 1)
    } else {
        return compile_one(q, syntax);
    };
    selector.strict_desc = false;
    selector.context_depth = Some(depth);
    Some(selector)
}

/// Compile a flat query string to its member selectors (all sharing one output column); empty if
/// unsupported. CSS comma groups and XPath unions / `or`-expansions both yield multiple members. The
/// shared front-end for [`extract_grouped`] and [`budget_usage`].
fn compile_query(q: &str, syntax: Syntax) -> Vec<Selector> {
    let qt = q.trim();
    // XPath compiles to the same Selector model; else CSS.
    if is_xpath(qt, syntax) {
        xpath::compile_members(qt)
    } else {
        selector::parse_list(q)
    }
}

/// Lower a whole schema's query strings to the matcher's inputs. Every entry point that reasons about
/// a schema — [`budget_usage`], [`audit_schema`], [`Plan::compile`] — goes through here, so all three
/// necessarily agree on which selectors compiled: an audit that routed a query differently from the
/// `Plan` that runs it would promise support for a column that comes back empty.
fn compile_schema<Q: AsQuery>(
    queries: &[Q],
    groups: &[GroupQuery<Q>],
) -> (Vec<Vec<Selector>>, Vec<matcher::GroupInput>) {
    let flat = queries.iter().map(|q| compile_query(q.text(), q.syntax())).collect();
    let grouped = groups
        .iter()
        .map(|g| {
            (
                compile_one(g.container.text(), g.container.syntax()),
                g.subfields.iter().map(|(_, sel)| compile_sub(sel.text(), sel.syntax())).collect(),
            )
        })
        .collect();
    (flat, grouped)
}

/// The encoding [`extract`] would scan `html` with, as a WHATWG canonical name (`"UTF-8"`,
/// `"windows-1252"`, `"Shift_JIS"`, …). `label` is the caller's forced label, or `None` to sniff, and
/// `url` the response URL, as in [`extract`].
///
/// The same resolution `extract` runs — BOM → BOM-less UTF-16 prefix → label → browser-bounded
/// `<meta>`/XML-declaration prescan → autodetection — exposed on its own because it is useful without an
/// extraction, and because nothing else in a scraper's stack answers this question the way a browser
/// does. Parsel does not sniff at all (`Selector(body=…)` defaults to UTF-8), and w3lib — what Scrapy
/// uses — differs from browsers in the places tabulated under Encoding in docs/COMPATIBILITY.md. The
/// answer is always a real encoding: an unresolvable label is ignored rather than propagated, per
/// WHATWG's "failure, continue".
pub fn detect_encoding(html: &[u8], label: Option<&str>, url: Option<&str>) -> &'static str {
    resolve_encoding(html, EncodingHints { encoding: label, content_type: None, url }).0
}

/// [`detect_encoding`] over every input a response carries (see [`EncodingHints`]), with the rank of the
/// precedence table that decided it. This is what [`Plan::extract_with`] scans with.
pub fn resolve_encoding(html: &[u8], hints: EncodingHints<'_>) -> (&'static str, EncodingSource) {
    let (enc, source) = encoding::resolve_explained(html, hints);
    (enc.name(), source)
}

/// The `(member-selector, sibling-bit)` demand of a schema. A caller
/// that would rather fail loud than get silently-empty columns compares this against
/// [`MAX_MEMBERS`] / [`MAX_SIB_BITS`]; the Python binding raises `ValueError`.
pub fn budget_usage<Q: AsQuery>(queries: &[Q], groups: &[GroupQuery<Q>]) -> (usize, usize) {
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
/// version of this question gets it wrong (XPath `/text()` and `/@name` read as node
/// queries), which is why it is answered here instead.
///
/// A routed query is uniform on the node-vs-scalar axis: [`compile_query`]'s comma/union rule refuses a
/// mix of outer-HTML and value terminals (deferred captures cannot interleave with streamed values in
/// document order), so the first member's terminal settles it for the whole query.
pub fn selector_terminals<Q: AsQuery>(queries: &[Q]) -> Vec<Option<&'static str>> {
    queries
        .iter()
        .map(|q| compile_query(q.text(), q.syntax()).first().map(|s| terminal_name(&s.terminal)))
        .collect()
}

/// Per query, `(pinned, synthesized_frame)` — the matched-node IDENTITY a caller needs before it can
/// re-parse an outer-HTML value into the node it came from.
///
/// `pinned` is the tag name every match must carry (the subject compound's name test, when every
/// comma/union member agrees on one concrete name); `None` when the query pins none (`*`, `.crumbs`,
/// `p, div`). `synthesized_frame` says a match could be a document-frame element the page never wrote.
///
/// An outer-HTML value is the matched element's own source, so it normally NAMES the element — but a
/// synthesized frame has no source of its own and its value begins with its CONTENT:
/// `extract(b"<p>x</p>", ["html", "body", "p"])` returns the same string in all three columns. So
/// `frostwork.webpoet`'s `.as_node()` handoff has to be told the identity, or refuse the field, and
/// browser mode refuses such a selector's outer HTML outright. The frame rule is the matcher's
/// `admits_synthesized_frame`, which both read.
pub fn selector_node_identity<Q: AsQuery>(queries: &[Q]) -> Vec<(Option<String>, bool)> {
    queries
        .iter()
        .map(|q| {
            let members = compile_query(q.text(), q.syntax());
            if members.is_empty() {
                return (None, true); // does not compile: fail closed on both axes
            }
            // `None` until the first member; then the name every member so far agrees on (inner `None` =
            // they do not, or one of them has no name test at all).
            let mut pinned: Option<Option<String>> = None;
            let mut synth = false;
            for sel in &members {
                let subject = sel.parts.last().expect("a compiled selector has a subject compound");
                let name = subject
                    .tag
                    .as_deref()
                    .filter(|t| *t != "*")
                    .map(|t| t.to_ascii_lowercase());
                synth |= matcher::admits_synthesized_frame(sel);
                pinned = Some(match pinned {
                    None => name,                          // first member sets the candidate
                    Some(prev) if prev == name => name,    // ...every later one has to repeat it
                    Some(_) => None,
                });
            }
            (pinned.flatten(), synth)
        })
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
///
/// In the default [`ParseMode`]; [`audit_schema_with`] audits a whole schema in either mode.
pub fn flat_query_support(q: &(impl AsQuery + ?Sized)) -> Support {
    flat_query_support_in(q, ParseMode::default())
}

pub(crate) fn flat_query_support_in(q: &(impl AsQuery + ?Sized), mode: ParseMode) -> Support {
    let members = compile_query(q.text(), q.syntax());
    if members.is_empty() {
        return Support::Unsupported(diagnostics::reason(q.text(), q.syntax()));
    }
    let schema = matcher::CompiledSchema::compile(std::slice::from_ref(&members), &[], mode);
    if schema.flat_col_supported(0) {
        Support::Supported
    } else {
        Support::Unsupported(flat_reason(&schema, 0, q))
    }
}

/// Why flat column `col` of `schema` is unsupported: the mode's refusal when there is one, else the
/// lxml contract's advisory reason.
fn flat_reason(schema: &matcher::CompiledSchema, col: usize, q: &(impl AsQuery + ?Sized)) -> String {
    match schema.flat_refusal(col) {
        Some(refusal) => refusal.reason().to_string(),
        None => diagnostics::reason(q.text(), q.syntax()),
    }
}

/// Support of a group CONTAINER selector (single CSS/XPath selector, no comma group). A reverse position
/// on a container is out of scope (the group would never open), so reject it.
pub fn container_support(q: &(impl AsQuery + ?Sized)) -> Support {
    let input: Vec<matcher::GroupInput> = vec![(compile_one(q.text(), q.syntax()), Vec::new())];
    let schema = matcher::CompiledSchema::compile(&[], &input, ParseMode::default());
    if schema.group_container_routed(0) {
        Support::Supported
    } else {
        Support::Unsupported(group_container_reason(q))
    }
}

fn group_container_reason(q: &(impl AsQuery + ?Sized)) -> String {
    if let Some(reason) = group_shape_reason(q) {
        reason
    } else if compile_one(q.text(), q.syntax()).is_some() {
        "selector requires deferred-close matching, which is unsupported for a grouped container \
         (empty group)"
            .to_string()
    } else {
        diagnostics::reason(q.text(), q.syntax())
    }
}

/// Support of a grouped SUB-FIELD selector. Stricter than a container: a sub-field must be a single
/// segment (no sibling `+`/`~`), matching the `Many`/`One` MVP (see `matcher::SubSel`); a reverse
/// position inside a group is also out of scope (empty column).
pub fn subfield_support(q: &(impl AsQuery + ?Sized)) -> Support {
    let container = compile_one("*", Syntax::Css);
    let input: Vec<matcher::GroupInput> = vec![(container, vec![compile_sub(q.text(), q.syntax())])];
    let schema = matcher::CompiledSchema::compile(&[], &input, ParseMode::default());
    if schema.group_sub_routed(0, 0) {
        Support::Supported
    } else {
        Support::Unsupported(sub_reason(&schema, 0, 0, q))
    }
}

/// [`flat_reason`] for a grouped sub-field.
fn sub_reason(
    schema: &matcher::CompiledSchema,
    group: usize,
    sub: usize,
    q: &(impl AsQuery + ?Sized),
) -> String {
    match schema.sub_refusal(group, sub) {
        Some(refusal) => refusal.reason().to_string(),
        None => group_sub_reason(q),
    }
}

fn group_sub_reason(q: &(impl AsQuery + ?Sized)) -> String {
    if let Some(reason) = group_shape_reason(q) {
        reason
    } else if compile_sub(q.text(), q.syntax()).is_some_and(|s| {
        s.combs
            .iter()
            .any(|c| matches!(c, selector::Comb::Adjacent | selector::Comb::General))
    }) {
        "sibling combinator (`+`/`~`) inside a grouped sub-field is unsupported (empty column)"
            .to_string()
    } else if compile_sub(q.text(), q.syntax()).is_some() {
        "selector requires deferred-close matching, which is unsupported inside a grouped sub-field \
         (empty column)"
            .to_string()
    } else {
        diagnostics::reason(q.text(), q.syntax())
    }
}

fn group_shape_reason(q: &(impl AsQuery + ?Sized)) -> Option<String> {
    let members = compile_query(q.text(), q.syntax());
    if members
        .iter()
        .any(|s| matches!(s.terminal, selector::Terminal::NormalizeSpace(_)))
    {
        Some("normalize-space() is supported for flat fields, but its scalar output is unsupported in a group".into())
    } else if members.len() > 1 {
        Some("a grouped container or sub-field requires a single selector; CSS comma groups and XPath unions/or-expansions are unsupported in this context".into())
    } else {
        None
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
/// selectors are supported (with advisory reasons for those that are not) and the budget usage, in the
/// default [`ParseMode`].
pub fn audit_schema<Q: AsQuery>(queries: &[Q], groups: &[GroupQuery<Q>]) -> SchemaAudit {
    audit_schema_with(queries, groups, ParseMode::default())
}

/// [`audit_schema`] in `mode`. Support is per mode: browser mode refuses, with a reason, one shape the
/// lxml contract supports, the outer HTML of a selector that can match an element the page never wrote
/// (a synthesized `<html>`/`<head>`/`<body>`, an implied `<tbody>`/`<tr>`/`<colgroup>`, the `<p>` a stray
/// `</p>` inserts), which has no source of its own.
pub fn audit_schema_with<Q: AsQuery>(
    queries: &[Q],
    groups: &[GroupQuery<Q>],
    mode: ParseMode,
) -> SchemaAudit {
    // Compile ONCE and derive every support verdict from the exact routes extraction will use. This
    // prevents the audit/strict-mode logic from drifting from matcher eligibility rules (notably the
    // deferred `:has` / text-predicate exclusions in grouped containers and sub-fields).
    let (flat_sel, grouped_sel) = compile_schema(queries, groups);
    let (members, sib_bits) = matcher::budget_usage(&flat_sel, &grouped_sel);
    let schema = matcher::CompiledSchema::compile(&flat_sel, &grouped_sel, mode);
    SchemaAudit {
        flat: queries
            .iter()
            .enumerate()
            .map(|(i, q)| {
                if schema.flat_col_routed(i) {
                    Support::Supported
                } else {
                    Support::Unsupported(flat_reason(&schema, i, q))
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
                            Support::Unsupported(sub_reason(&schema, gi, si, sel))
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

/// Resolve the page encoding and prepare the buffer the scan tokenizes: `(bytes, value_encoding,
/// keeps_nul)`. A non-ASCII-compatible encoding is transcoded to a fresh UTF-8 `Vec` once (owned `Cow`);
/// everything else borrows the input, with a document-leading UTF-8 BOM stripped — libxml2 and browsers
/// strip ONLY the leading BOM (a U+FEFF elsewhere is real content), and per-value decoding must not
/// re-strip it (see `matcher::finalize`), so it is handled here, once per page.
///
/// **The transcode set is asked of `encoding_rs`, not listed here**, and that is the whole point: no
/// hand-written list gets it right. "UTF-16, the only non-ASCII-compatible family" is the obvious one
/// and it is wrong — ISO-2022-JP is not ASCII-compatible either, since inside `ESC $ B` mode a JIS pair
/// is two bytes below 0x80 and `社` is literally `<R`. Tokenized as raw bytes, a Japanese page grows a
/// `<r>` start tag out of the middle of a word and every value downstream of it is wrong. The
/// predicate covers `replacement` too (the label for HZ-GB-2312 and friends), whose whole purpose is
/// that browsers refuse to decode such a document at all — one U+FFFD and nothing else.
///
/// The rest is per mode. lxml mode applies Parsel's normalization to the whole document (see
/// [`normalize`]). Browser mode tokenizes what a browser does: nothing is stripped but the BOM, and NUL
/// stays in for the HTML5 value rules (`keeps_nul`, decided by one `memchr`).
fn prepare_bytes<'h>(
    html: &'h [u8],
    hints: EncodingHints<'_>,
    mode: ParseMode,
) -> (Cow<'h, [u8]>, &'static encoding_rs::Encoding, bool) {
    let (enc, _) = encoding::resolve_explained(html, hints);
    if !enc.is_ascii_compatible() {
        // Transcode FIRST: in UTF-16 every ASCII character carries a 0x00 byte, so deleting NUL bytes
        // from the raw input would shred the document. What must go is the U+0000 CHARACTER, which only
        // exists once the code units have been decoded. (Parsel deletes NUL from the raw bytes instead,
        // which is why it cannot read a UTF-16 page at all — a divergence in our favour, already
        // documented under Encoding in docs/COMPATIBILITY.md.) `decode` consumes the BOM.
        let utf8 = Cow::Owned(enc.decode(html).0.into_owned().into_bytes());
        return match mode {
            ParseMode::Lxml => (normalize(utf8, true), encoding_rs::UTF_8, false),
            ParseMode::Browser => {
                let keeps_nul = memchr::memchr(0, &utf8).is_some();
                (utf8, encoding_rs::UTF_8, keeps_nul)
            }
        };
    }
    let utf8 = enc == encoding_rs::UTF_8;
    match mode {
        ParseMode::Lxml => (normalize(Cow::Borrowed(html), utf8), enc, false),
        ParseMode::Browser => {
            let body = if utf8 { html.strip_prefix(UTF8_BOM).unwrap_or(html) } else { html };
            (Cow::Borrowed(body), enc, memchr::memchr(0, body).is_some())
        }
    }
}

/// The UTF-8 byte order mark.
const UTF8_BOM: &[u8] = &[0xEF, 0xBB, 0xBF];

/// Parsel's own input normalization, which lxml mode applies ONCE to the whole document before it is
/// tokenized: `text.strip().replace("\x00", "")` — trim the ends, then delete NUL, **in that order**.
///
/// The order is not incidental and must not be "tidied" into recomputing the ends afterwards. Parsel's
/// two entry points disagree about it — `Selector(text=…)` strips first, `Selector(body=…)` deletes
/// first — and this engine matches the TEXT one, because that is the path a scraper is on: Scrapy's
/// `response.selector` and web-poet's `HttpResponse` both build their selector from `response.text`.
/// The two answers differ only when a NUL sits at a document edge, and there they differ visibly:
/// `b"<option>x \x00"` keeps its trailing space (so `option::text` is `"x "`, not `"x"`), and in
/// `b"\x00 \xEF\xBB\xBF<html …>"` the NUL blocks the strip, so the U+FEFF is never promoted to offset 0
/// and stays a character rather than becoming a BOM. Deleting first would match `body=` and diverge
/// from every Scrapy scraper.
fn normalize(bytes: Cow<'_, [u8]>, utf8: bool) -> Cow<'_, [u8]> {
    let (start, end) = document_bounds(&bytes, utf8);
    let trimmed = match bytes {
        Cow::Borrowed(b) => Cow::Borrowed(&b[start..end]),
        Cow::Owned(mut v) => {
            v.truncate(end);
            v.drain(..start);
            Cow::Owned(v)
        }
    };
    strip_nul(trimmed)
}

/// The byte range Parsel's `text.strip()` leaves for libxml2: past a leading UTF-8 BOM and any
/// whitespace written before it, and short of any trailing whitespace. Parsel parses the STRIPPED text,
/// so stripping the same bytes is what makes the two agree.
///
/// **Leading.** The strip can promote a U+FEFF to offset 0, where libxml2 eats it as a BOM, so
/// `"    \u{FEFF}<!DOCTYPE HTML>"` starts with a doctype. Left unstripped, that U+FEFF would be character
/// data before the frame and would open the `<body>`, losing the page's `<head>`, its `<title>` and the
/// attributes of its own `<html>` tag (redundant once a body is open). `head title::text` and
/// `html::attr(xmlns)` would then come back empty.
///
/// The strip is Parsel's, not libxml2's: given the raw bytes, libxml2 and html5lib both read that
/// U+FEFF as a character. Frostwork follows Parsel because `response.text` is the input a Scrapy
/// scraper actually selects over.
///
/// **Trailing.** This half moves only whitespace-only text, so it changes no value a scraper reads. It
/// does change how MANY values come back: unstripped, a page ending `…<option class="c3">\n` would give
/// the last option a text node that Parsel has no node for.
///
/// Only the BOM is gated on UTF-8 (`utf8`). In windows-1252 those three bytes are `ï»¿`, three real
/// characters, and Parsel's own decode agrees. The whitespace trim applies under every label because
/// none of the ASCII strip bytes can occur inside a multi-byte character in an ASCII-compatible
/// encoding; a non-ASCII-compatible encoding is transcoded before it reaches here.
fn document_bounds(bytes: &[u8], utf8: bool) -> (usize, usize) {
    let end = bytes.iter().rposition(|&b| !is_strip_ws(b)).map_or(0, |i| i + 1);
    let mut start = bytes[..end].iter().position(|&b| !is_strip_ws(b)).unwrap_or(end);
    if utf8 && bytes[start..end].starts_with(UTF8_BOM) {
        start += UTF8_BOM.len();
    }
    (start, end)
}

/// The bytes Python's `strip()` removes from a document's ends: ASCII whitespace **including vertical
/// tab**, which is why the set is written out instead of deferred to `u8::is_ascii_whitespace` (that
/// one omits `0x0b`).
///
/// It is deliberately not `tokenizer::is_ws` either. That set is HTML whitespace, which correctly
/// excludes `0x0b`, because inside a document a vertical tab is ordinary character data. The two only
/// look alike. Dropping `0x0b` from this set would leave it as the leading character in
/// `b"\x0b<html a=1><head><title>T</title>…"`, opening the `<body>` before a head can start and emptying
/// `head title::text` and `html::attr(a)`.
const fn is_strip_ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// Delete every raw NUL byte from the document, as Parsel/w3lib do before handing bytes to lxml
/// (`body.replace(b"\x00", b"")`).
///
/// This has to happen before TOKENIZATION, not at value-emit time. Dropping NUL only from emitted values
/// leaves the two sides disagreeing about the document's STRUCTURE: `<di\0v>X</di\0v>` is a `div` to
/// lxml and an element named `di\0v` here, so `div::text` returns nothing — a silently empty column, not
/// a slightly different string. Attribute names and values, text and tag names are all covered by doing
/// it once, up front.
///
/// The ordinary path (no NUL anywhere, which is every real page) costs one `memchr` over the buffer and
/// no allocation. Only a document that actually contains a NUL is copied.
fn strip_nul(bytes: Cow<'_, [u8]>) -> Cow<'_, [u8]> {
    if memchr::memchr(0, &bytes).is_none() {
        return bytes;
    }
    Cow::Owned(bytes.iter().copied().filter(|&b| b != 0).collect())
}

/// A schema compiled ONCE and run over any number of pages — **how the engine is actually used, and
/// what every benchmark measures.** Building a `Plan` parses every selector, lowers it to the matcher's
/// internal form, and computes the interesting-attribute set a single time; each [`Plan::extract`] then
/// only resolves the page's encoding and runs the streaming pass.
///
/// This is not an optimization a caller opts into. A schema is declared once and applied to every
/// response — a Scrapy/web-poet page object is a *class*, and `frostwork.Page` / `FrostPage` compile
/// their selectors here at definition time. [`extract`] and [`extract_grouped`] are the one-shot
/// convenience form for a caller holding selector strings and one document; they compile a `Plan`
/// internally and throw it away, which no scraper does per response.
///
/// Unsupported selectors compile to empty columns exactly as [`extract`] does (no fallback); the
/// flat/grouped output is identical either way.
pub struct Plan {
    schema: matcher::CompiledSchema,
    budget: (usize, usize), // (members, sibling-bits) raw demand, computed once at compile
}

impl Plan {
    /// Compile `queries` + `groups` (the same shapes [`extract_grouped`] accepts) once for reuse, in the
    /// default [`ParseMode`].
    pub fn compile<Q: AsQuery>(queries: &[Q], groups: &[GroupQuery<Q>]) -> Plan {
        Self::compile_with(queries, groups, CompileOptions::default())
    }

    /// [`compile`](Plan::compile), plus the caller's per-column cardinality: `first_only[c]` declares
    /// that column `c`'s consumer keeps only the FIRST value and discards the rest.
    ///
    /// That lets the scan STOP once every column has one — a page whose fields all live in the head is
    /// answered without tokenizing the body. The columns are unchanged apart from the values that would
    /// have been discarded anyway, so the `Item` a cardinality layer builds is identical.
    ///
    /// Immediate first-value text/attribute columns also stop retaining subsequent values when groups
    /// or all-value fields still require EOF. Deferred predicates, reverse positions, mixed deferred
    /// columns and `normalize-space` disarm retention. Outer-HTML captures retain start-order semantics.
    /// See `matcher::CompiledSchema::arm_early_exit` for the narrower whole-scan early-exit conditions.
    /// `&[]` — what [`compile`](Plan::compile) passes — arms nothing, which is why plain
    /// [`extract`]/[`extract_grouped`], whose contract is EVERY value, are unaffected.
    pub fn compile_first_only<Q: AsQuery>(
        queries: &[Q],
        groups: &[GroupQuery<Q>],
        first_only: &[bool],
    ) -> Plan {
        Self::compile_with(queries, groups, CompileOptions { first_only, ..CompileOptions::default() })
    }

    /// [`compile`](Plan::compile) with every compile-time choice spelled out: the parse mode and the
    /// per-column cardinality ([`CompileOptions`]).
    pub fn compile_with<Q: AsQuery>(
        queries: &[Q],
        groups: &[GroupQuery<Q>],
        options: CompileOptions<'_>,
    ) -> Plan {
        let (flat, grouped) = compile_schema(queries, groups);
        let budget = matcher::budget_usage(&flat, &grouped);
        let mut schema = matcher::CompiledSchema::compile(&flat, &grouped, options.mode);
        if options.first_only.iter().any(|&f| f) {
            schema.arm_early_exit(options.first_only);
        }
        Plan { schema, budget }
    }

    /// The parse mode this plan was compiled for.
    pub fn mode(&self) -> ParseMode {
        self.schema.mode()
    }

    /// The `(member, sibling-bit)` budget demand of this plan — see [`budget_usage`]. Computed at
    /// compile time; a caller can reject an over-budget plan once, up front.
    pub fn budget_usage(&self) -> (usize, usize) {
        self.budget
    }

    /// Run the compiled schema over one page. `encoding` is an optional caller label; `None` sniffs
    /// (BOM → `<meta>` → autodetection), informed by `url` as in [`extract`]. Returns `(flat_columns,
    /// grouped)` — byte-identical to [`extract_grouped`] with the same schema.
    pub fn extract(
        &self,
        html: &[u8],
        encoding: Option<&str>,
        url: Option<&str>,
    ) -> (FlatColumns, Vec<GroupRows>) {
        self.extract_with(html, EncodingHints { encoding, content_type: None, url })
    }

    /// [`extract`](Plan::extract) with every input a response carries, the raw `Content-Type` header
    /// included (see [`EncodingHints`] and [`resolve_encoding`]).
    pub fn extract_with(&self, html: &[u8], hints: EncodingHints<'_>) -> (FlatColumns, Vec<GroupRows>) {
        // Keep the prepared `Cow` alive here: the matcher borrows its bytes for the whole scan.
        let (bytes, value_enc, keeps_nul) = prepare_bytes(html, hints, self.mode());
        self.schema.run(matcher::EncodedInput::new(&bytes, value_enc, keeps_nul))
    }
}

impl Plan {
    /// Which of browser mode's retroactive rules fire on `html`, one bit per rule (see the header of
    /// `matcher::browser`), whatever the plan's mode. A second `<html>`/`<body>` tag counts only when it
    /// adds an attribute this plan's selectors ask about. For `tools/retro_census.py`.
    #[doc(hidden)]
    pub fn retro_census(&self, html: &[u8], hints: EncodingHints<'_>) -> u8 {
        let (bytes, value_enc, keeps_nul) = prepare_bytes(html, hints, ParseMode::Browser);
        self.schema.retro_census(matcher::EncodedInput::new(&bytes, value_enc, keeps_nul))
    }
}

/// One run returning both flat query columns and grouped (`Many`/`One`) results.
/// `grouped[g]` is group `g`'s rows in document order; each row is one value-column per sub-field
/// (`[group][row][subfield][value]`). The caller (the Python/Rust `Page` layer) applies per-field
/// cardinality (first/all/join) and, for `One`, takes the first row. This is the one-shot form; to run
/// the same selectors over many pages, compile a [`Plan`] once and reuse it.
pub fn extract_grouped<Q: AsQuery>(
    html: &[u8],
    queries: &[Q],
    groups: &[GroupQuery<Q>],
    encoding: Option<&str>,
    url: Option<&str>,
) -> (FlatColumns, Vec<GroupRows>) {
    Plan::compile(queries, groups).extract(html, encoding, url)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Every vector in this module asserts libxml2/lxml parity, so the one-shot entry points are pinned
    // to lxml mode here: these local items shadow the crate's own through `use super::*`. Browser mode's
    // vectors are in `browser_mode_tests`, each against a measured browser answer.
    fn extract<Q: AsQuery>(html: &[u8], queries: &[Q], encoding: Option<&str>, url: Option<&str>) -> FlatColumns {
        extract_grouped(html, queries, &[], encoding, url).0
    }
    fn extract_grouped<Q: AsQuery>(
        html: &[u8],
        queries: &[Q],
        groups: &[GroupQuery<Q>],
        encoding: Option<&str>,
        url: Option<&str>,
    ) -> (FlatColumns, Vec<GroupRows>) {
        let lxml = CompileOptions { mode: ParseMode::Lxml, first_only: &[] };
        Plan::compile_with(queries, groups, lxml).extract(html, encoding, url)
    }
    fn audit_schema<Q: AsQuery>(queries: &[Q], groups: &[GroupQuery<Q>]) -> SchemaAudit {
        audit_schema_with(queries, groups, ParseMode::Lxml)
    }

    fn ex(html: &str, q: &str) -> Vec<String> {
        extract(html.as_bytes(), &[q.to_string()], None, None).pop().unwrap()
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
        extract_grouped(html.as_bytes(), &[], &[g], None, None).1.pop().unwrap()
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

    /// The identity `.as_node()` cannot read off a value. `*` is the row that makes the function
    /// necessary: on `<p>x</p>` it matches three nodes and the engine returns one string for all three.
    #[test]
    fn selector_node_identity_pins_a_name_and_reports_the_frame_risk() {
        let q: Vec<String> = [
            "div.card",         // a name AND an attribute constraint: never a synthesized frame
            "div",              // pinned, and `div` is not a frame name
            ".crumbs",          // no name test, but a class -> a synthesized frame has no attributes
            "[itemprop=brand]", // ditto, via an attribute
            "*",                // matches every frame, names nothing -> the ambiguous case
            "body",             // pinned: every match is a `<body>`, written or synthesized
            "//html/body",      // ...through XPath as well
            "p, div",           // members disagree on the name; neither is a frame
            "p, *",             // one member is the ambiguous case, so the query is
            "body.page",        // a frame name, but attribute-bound: the source names it
            "div:has(.a .b)",   // does not compile -> no identity, fail closed
        ]
        .iter()
        .map(|s| s.to_string())
        .collect();
        let want: Vec<(Option<String>, bool)> = [
            (Some("div"), false),
            (Some("div"), false),
            (None, false),
            (None, false),
            (None, true),
            (Some("body"), true),
            (Some("body"), true),
            (None, false),
            (None, true),
            (Some("body"), false),
            (None, true),
        ]
        .iter()
        .map(|(n, s)| (n.map(str::to_string), *s))
        .collect();
        assert_eq!(selector_node_identity(&q), want);
    }

    /// A declared syntax bypasses the prefix rule: XPath `h1` is a relative step the XPath compiler
    /// refuses, where the CSS reading would have answered with every `<h1>`; CSS `/html` is not a
    /// selector, where the XPath reading would have answered with the root element.
    #[test]
    fn a_declared_syntax_overrides_prefix_routing() {
        let html = b"<html><body><h1>T</h1></body></html>";
        let auto = ["h1".to_string(), "/html".to_string()];
        assert_eq!(extract(html, &auto, None, None), vec![v(&["<h1>T</h1>"]), v(&["<html><body><h1>T</h1></body></html>"])]);
        let declared = [Query::new("h1", Syntax::XPath), Query::new("/html", Syntax::Css)];
        assert_eq!(extract(html, &declared, None, None), vec![v(&[]), v(&[])]);
        let audit = audit_schema(&declared, &[]);
        assert_eq!(audit.flat.len(), 2);
        assert!(audit.flat[0].reason().unwrap().contains("relative"));
        assert!(audit.flat[1].reason().unwrap().contains("CSS"));
        assert_eq!(selector_terminals(&declared), vec![None, None]);
        assert_eq!(
            audit_schema(&[Query::new("//h1/text()", Syntax::XPath), Query::new("h1::text", Syntax::Css)], &[]).flat,
            vec![Support::Supported, Support::Supported]
        );
        // Grouped: the container and a sub-field each carry their own syntax.
        let group = GroupQuery {
            container: Query::new("//body", Syntax::XPath),
            subfields: vec![
                ("bare".to_string(), Query::new("h1", Syntax::XPath)),
                ("desc".to_string(), Query::new(".//h1/text()", Syntax::XPath)),
                ("css".to_string(), Query::new("h1::text", Syntax::Css)),
            ],
        };
        let audit = audit_schema(&[], std::slice::from_ref(&group));
        assert!(audit.groups[0].container.is_supported());
        assert!(!audit.groups[0].subfields[0].is_supported());
        assert!(audit.groups[0].subfields[1].is_supported());
        assert!(audit.groups[0].subfields[2].is_supported());
        let rows = extract_grouped(html, &[], &[group], None, None).1.pop().unwrap();
        assert_eq!(rows, vec![vec![v(&[]), v(&["T"]), v(&["T"])]]);
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
                ("child".to_string(), "./h3[last()]/text()".to_string()), // unsupported (deferred group)
            ],
        }];
        let a = audit_schema(&queries, &groups);
        assert!(a.flat[0].is_supported());
        assert!(a.flat[1].reason().unwrap().contains(":has()"));
        assert!(a.flat[2].reason().unwrap().contains("positional"));
        assert!(a.groups[0].container.is_supported());
        assert!(a.groups[0].subfields[0].is_supported());
        assert!(a.groups[0].subfields[1].reason().unwrap().contains("sibling combinator"));
        assert!(a.groups[0].subfields[2].reason().unwrap().contains("deferred-close"));
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
                container: "div:has(a)",
                subfields: vec![("x".into(), "div::text")],
            },
            GroupQuery {
                container: ".root",
                subfields: vec![
                    ("has".into(), "p:has(a)::text"),
                    ("text".into(), ".//p[contains(.,\"x\")]/text()"),
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
            let cols = extract(html.as_bytes(), &queries, None, None);
            assert_eq!(cols.len(), 65);
            assert!(cols.iter().all(|c| c == &v(&[expected])), "{q}: last={:?}", cols.last());
        }
    }

    /// `:has()` takes a relative selector LIST — `div:has(a, img)`, the shape cssselect#138 names as
    /// unsupported. `:has(a, b)` is the union of `:has(a)` and `:has(b)` (an element matches if ANY
    /// member is found), which is what makes the two single-member spellings a usable oracle for it.
    #[test]
    fn has_takes_a_selector_list() {
        let h = "<div id=d1><a>x</a></div><div id=d2><img src=s></div><div id=d3><b>y</b></div>\
                 <div id=d4><a>z</a><img src=t></div>";
        // OR across members, in document order, and an element satisfying BOTH appears once
        assert_eq!(ex(h, "div:has(a, img)::attr(id)"), v(&["d1", "d2", "d4"]));
        assert_eq!(ex(h, "div:has(img, a)::attr(id)"), v(&["d1", "d2", "d4"]), "member order is irrelevant");
        // ...and each member alone is the single-compound spelling parsel can evaluate
        assert_eq!(ex(h, "div:has(a)::attr(id)"), v(&["d1", "d4"]));
        assert_eq!(ex(h, "div:has(img)::attr(id)"), v(&["d2", "d4"]));
        // three members, and members of different KINDS (the widened inners) inside one list
        assert_eq!(ex(h, "div:has(a, img, b)::attr(id)"), v(&["d1", "d2", "d3", "d4"]));
        assert_eq!(ex(h, "div:has([src], #nope)::attr(id)"), v(&["d2", "d4"]));
        // child-scoped list: only DIRECT children count, so a nested match does not qualify
        let nested = "<div id=p><span><a>deep</a></span></div><div id=q><a>direct</a></div>";
        assert_eq!(ex(nested, "div:has(> a, > img)::attr(id)"), v(&["q"]));
        assert_eq!(ex(nested, "div:has(a, img)::attr(id)"), v(&["p", "q"]));
        // a member that matches nothing contributes nothing, and an all-miss list yields an empty column
        assert_eq!(ex(h, "div:has(a, nosuch)::attr(id)"), v(&["d1", "d4"]));
        assert_eq!(ex(h, "div:has(nosuch, alsonot)::attr(id)"), Vec::<String>::new());
        // the list composes with the subtree / descendant value tiers, like a single-compound inner
        assert_eq!(ex(h, "div:has(a, img) ::text"), v(&["x", "z"]));
        assert_eq!(ex(h, "div:has(a, img) a::text"), v(&["x", "z"]));
        // MIXED relative combinators have no faithful `Has` to build, so they are REPORTED, not guessed
        assert!(!audit_schema(&["div:has(> a, img)::attr(id)"], &[]).ok());
        assert!(audit_schema(&["div:has(a, img)::attr(id)"], &[]).ok());
    }

    /// `:not()` takes a selector list too (Selectors 4). `:not(a, b)` is exactly `:not(a):not(b)` — the
    /// chained spelling cssselect accepts — so every case here is checked against it.
    #[test]
    fn not_takes_a_selector_list() {
        let h = "<p class=a>A</p><p class=b>B</p><p class=c>C</p><p>D</p>";
        for (list, chained) in [
            ("p:not(.a, .b)::text", "p:not(.a):not(.b)::text"),
            ("p:not(.a, .b, .c)::text", "p:not(.a):not(.b):not(.c)::text"),
            ("p:not(.a)::text", "p:not(.a)::text"),
        ] {
            assert_eq!(ex(h, list), ex(h, chained), "{list}");
        }
        assert_eq!(ex(h, "p:not(.a, .b)::text"), v(&["C", "D"]));
        // a comma inside an attribute VALUE is data, not a member separator
        let av = "<p title=\"x, y\">A</p><p title=z>B</p>";
        assert_eq!(ex(av, "p:not([title=\"x, y\"])::text"), v(&["B"]));
        // an empty member is a syntax error to cssselect, so it is REPORTED rather than ignored
        assert!(!audit_schema(&["p:not(.a, )::text"], &[]).ok());
        assert!(audit_schema(&["p:not(.a, .b)::text"], &[]).ok());
    }

    /// The Selectors 4 case-sensitivity flag, `[a=v i]`. cssselect rejects it outright, so the oracle is
    /// the semantics: ASCII folding of the VALUE, on every operator, and nothing else about the compare
    /// changes (an empty operand still matches nothing; `~=` still splits on ASCII whitespace).
    #[test]
    fn attribute_case_insensitive_flag() {
        let h = "<a id=1 href=\"/Doc.PDF\" rel=\"NoFollow Me\" hreflang=\"EN-us\" type=\"SubMit\">x</a>";
        for q in [
            "[type=submit i]",
            "[type=\"submit\" i]",
            "[href^=\"/doc\" i]",
            "[href$=\".pdf\" i]",
            "[href*=\"OC.p\" i]",
            "[rel~=nofollow i]",
            "[hreflang|=en i]",
        ] {
            assert_eq!(ex(h, &format!("{q}::attr(id)")), v(&["1"]), "{q}");
        }
        // ...and without the flag every one of them is case-SENSITIVE, i.e. matches nothing here
        for q in ["[type=submit]", "[href^=\"/doc\"]", "[href$=\".pdf\"]", "[href*=\"OC.p\"]",
                  "[rel~=nofollow]", "[hreflang|=en]"] {
            assert_eq!(ex(h, &format!("{q}::attr(id)")), Vec::<String>::new(), "{q}");
        }
        // `s` is the DEFAULT (case-sensitive), so it parses and changes nothing
        assert_eq!(ex(h, "[type=\"SubMit\" s]::attr(id)"), v(&["1"]));
        assert_eq!(ex(h, "[type=\"submit\" s]::attr(id)"), Vec::<String>::new());
        // the flag folds ASCII ONLY, as CSS defines it — `É`/`é` are one character apart in Unicode and
        // must not match, or the engine would be doing a Unicode fold no browser does here
        let acc = "<p id=2 data-x=\"CAFÉ\">t</p>";
        assert_eq!(ex(acc, "[data-x=\"café\" i]::attr(id)"), Vec::<String>::new());
        assert_eq!(ex(acc, "[data-x=\"CAFÉ\" i]::attr(id)"), v(&["2"]));
        // a multi-byte value must not panic a prefix/suffix compare on a non-char-boundary index
        assert_eq!(ex(acc, "[data-x^=\"ca\" i]::attr(id)"), v(&["2"]));
        assert_eq!(ex(acc, "[data-x$=\"É\" i]::attr(id)"), v(&["2"]));
        assert_eq!(ex(acc, "[data-x*=\"AF\" i]::attr(id)"), v(&["2"]));
        // an empty operand still matches nothing, flag or not (the CSS rule, unchanged)
        assert_eq!(ex(h, "[href^=\"\" i]::attr(id)"), Vec::<String>::new());
        // `i` is only a flag after whitespace: `[type=SubMiti]` is a five-character value
        assert_eq!(ex(h, "[type=SubMiti]::attr(id)"), Vec::<String>::new());
        assert!(audit_schema(&["[type=submit i]::attr(id)"], &[]).ok());
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
        // positional INSIDE :not() must turn the counters on too (miss it and the
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

    /// A comma group may MIX a deferred text-predicate member with streamed ones. The two produce their
    /// values at different times — during the pass, and at the subject's close — so the column is merged
    /// by document OFFSET and deduped by node. Every expectation here was read off parsel.
    #[test]
    fn comma_group_mixing_a_deferred_member_merges_in_document_order() {
        let h = "<h2>Head</h2><p id=a class=a>alpha</p><p id=b class=b>beta</p>";
        // disjoint members: DOCUMENT order, not member order — both spellings give the same column
        assert_eq!(ex(h, "h2::text, p:contains(\"alpha\")::text"), v(&["Head", "alpha"]));
        assert_eq!(ex(h, "p:contains(\"alpha\")::text, h2::text"), v(&["Head", "alpha"]));
        // overlapping members: `p::text` and the `:contains` member select the SAME text node, and a
        // union is node-deduped — three pushes, two values
        assert_eq!(ex(h, "p::text, p:contains(\"alpha\")::text"), v(&["alpha", "beta"]));
        assert_eq!(ex(h, "p:contains(\"alpha\")::text, p::text"), v(&["alpha", "beta"]));
        // one attribute NAME across the group: the attr node is the element's offset, so it dedupes
        assert_eq!(ex(h, "p::attr(id), p:contains(\"alpha\")::attr(id)"), v(&["a", "b"]));
        // three members, one deferred
        assert_eq!(
            ex("<h2>H</h2><p>alpha</p><span>s</span>", "h2::text, span::text, p:contains(\"alpha\")::text"),
            v(&["H", "alpha", "s"])
        );
        // and two deferred members with no streamed one at all
        assert_eq!(ex(h, "p:contains(\"alpha\")::text, p:contains(\"zzz\")::text"), v(&["alpha"]));

        // REFUSED, because an offset would not name one node. Two different attributes of one element
        // are two nodes in lxml's union (it returns both, in source order) sharing the element's offset,
        // so a `(col, offset)` dedupe would drop one.
        assert!(!audit_schema(&["p::attr(id), p:contains(\"alpha\")::attr(class)"], &[]).ok());
        assert!(!audit_schema(&["p::text, p:contains(\"alpha\")::attr(id)"], &[]).ok());
        // and a member whose value comes from a SUBTREE re-scan carries no document offset at all
        assert!(!audit_schema(&["h2::text, div:contains(\"alpha\") ::text"], &[]).ok());
        assert!(!audit_schema(&["h2::text, div:contains(\"alpha\") a::attr(href)"], &[]).ok());
        // the other deferred tiers stay out of a comma group
        assert!(!audit_schema(&["h2::text, div:has(a) p::text"], &[]).ok());
        assert!(!audit_schema(&["h2::text, li:last-child::text"], &[]).ok());
        // every refusal above is REPORTED, so none of them is a silently empty column
        assert!(audit_schema(&["h2::text, p:contains(\"alpha\")::text"], &[]).ok());
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
        // The `./…` child anchor is UNSUPPORTED, not approximated: the matcher cannot enforce a child
        // anchor on a segment's first compound, and treating `./step` as `.//step` would return values
        // the oracle does not have. Empty column, never a wrong value.
        //
        // flat: parsel `./h3/text()` on the doc root is [] (h3 is not a child of the document node);
        // rejecting it (empty column) matches that oracle exactly.
        let html = "<html><body><div><h3>A</h3></div><h3>B</h3></body></html>";
        assert_eq!(ex(html, "./h3/text()"), Vec::<String>::new());
        assert_eq!(ex(html, "./body/h3/text()"), Vec::<String>::new());
        // grouped direct-child `./h3/text()` selects only the immediate child, excluding the nested
        // `NEST`, while the `.//h3/text()` descendant control collects both, in document order.
        let ghtml = "<html><body><div class=card><h3>A</h3><div><h3>NEST</h3></div></div></body></html>";
        let rows = grp(ghtml, ".card", &["./h3/text()", ".//h3/text()"]);
        assert_eq!(rows, vec![vec![v(&["A"]), v(&["A", "NEST"])]]);
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
        let (flat, grouped) = extract_grouped(html.as_bytes(), &["h1::text".to_string()], &[g], None, None);
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
    // libxml2 2.15 NESTS a same-tag dt/dd repeat instead of auto-closing it (unlike `li`/`td`/`option`
    // above), and never auto-closes ruby annotations at all. A rule table ported from the HTML5 spec
    // asserts the opposite for both and over-closes, which is why this one is derived from the oracle.
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
        // ...and they do NOT close an open <p> either. `div > p::text` cannot tell: it is p's DIRECT
        // text, which is `a` whether the <rt> nests or becomes a sibling. `div > rt` discriminates,
        // matching only if the <rt> was lifted out of the <p>.
        assert_eq!(ex("<div><p>a<rt>b</div>", "div > rt::text"), v(&[]));
        assert_eq!(ex("<div><p>a<option>b</div>", "div > option::text"), v(&[]));
    }

    /// libxml2's `htmlStartClose` NAME-pair table (`implied_close::start_closes`).
    ///
    /// This is a different rule from the implied-close cross product, at a finer granularity than the
    /// engine's tag ids: it closes an open `<b>` for an incoming `<td>` but NOT an open `<em>`, and an
    /// open `<h1>` for an incoming `<table>` but NOT an open `<div>` — pairs a coarser id space lumps
    /// together.
    /// These cases match libxml2 2.15. `tools/audit_tree_rules.py` re-checks the complete
    /// (open x incoming) table, and `tools/mutate_rules.py` checks that flipping a cell is noticed.
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
    // The tag is not always a trailing stray. A page whose head ends `</head></html><body><header>…` has
    // a MISPLACED `</html>` with 14 of its 17 KB after it, where libxml2 keeps 2 elements out of 100+: a
    // Parsel spider sees an almost empty page and the engine, like a browser, sees the site. JSON-LD
    // `<script>` elements are hidden the same way. The divergence runs in the direction that recovers a
    // page rather than losing one.
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
        // it. Without frame synthesis it is an unmatched end tag on a bare fragment, which coalesces the
        // runs either side instead.
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

    /// These legacy names were omitted by a hand-written start-close table, which is why the audit's
    /// universe contains every element name rather than the ones someone remembers:
    /// `<listing>`/`<xmp>` in man-page and README-to-HTML output, and a `<title>` after an unclosed
    /// `<p>`.
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

    /// libxml2 treats the obsolete `basefont`/`bgsound`/`frame`/`isindex`/`keygen` and the HTML5
    /// `embed`/`source`/`track`/`wbr` as EMPTY elements. Treating them as containers would nest
    /// everything after one a level too deep.
    #[test]
    fn uncommon_void_elements_hold_no_children() {
        for t in ["basefont", "bgsound", "frame", "isindex", "keygen", "embed", "source", "track", "wbr"] {
            let h = format!("<div><{t}><span>x</span></div>");
            assert_eq!(ex(&h, "div > span::text"), v(&["x"]), "<{t}> must be void");
            assert_eq!(ex(&h, &format!("{t} span::text")), Vec::<String>::new());
        }
    }

    // ---- an end tag with nothing to match is DROPPED, and does not split the text node ----
    // libxml2 discards it and keeps the character data either side as ONE text node. Emitting two
    // values instead silently TRUNCATES a `One`-cardinality field (`.get()` -> "HELLO", not
    // "HELLOWORLD") — the one failure mode no-fallback is supposed to rule out. Real pages hit this:
    // Sphinx emits stray `</p>`. See `Matcher::buffer_text`.
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
            extract(b"<div>\xc3</p>\xa9</div>", &["div::text".to_string()], Some("utf-8"), None).pop().unwrap(),
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
        // through combinators/brackets — `split_structural` must not read bytes as chars (`c as char`),
        // which mangles multi-byte UTF-8. Oracle: parsel/cssselect accept all of these. (T6)
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
        // Per the no-fallback contract it must be an empty column, NOT an implicit `*` that dumps the
        // whole document into the field.
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
        let cols = extract(html.as_bytes(), &queries, None, None);
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
            let cols = extract(html, &qs, None, None);
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
        let cols = extract(html, &qs, None, None);
        assert!(cols.iter().all(|c| !c.is_empty()), "in-budget schema lost a column");
    }

    #[test]
    fn budget_members_over_128_is_safe_empty() {
        // 130 flat members: columns 0..127 are live, 128/129 are over the 128-member budget (dead).
        let queries: Vec<String> = (0..130).map(|i| format!(".c{i}::text")).collect();
        let html = "<b class=\"c0\">first</b><b class=\"c129\">last</b>";
        let cols = extract(html.as_bytes(), &queries, None, None);
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
            extract(b"<p class=\"c\">caf\xe9</p>", &["p::text"], Some("windows-1252"), None)[0],
            v(&["café"])
        );
        // Shift_JIS (日本 = 0x93FA 0x967B), explicit label, in an attribute value too
        assert_eq!(
            extract(b"<a title=\"\x93\xfa\x96\x7b\">x</a>", &["a::attr(title)"], Some("shift_jis"), None)[0],
            v(&["日本"])
        );
        // <meta charset> sniff (no label)
        assert_eq!(
            extract(
                b"<html><head><meta charset=windows-1252></head><body><p>caf\xe9</p></body></html>",
                &["p::text"],
                None, None,
            )[0],
            v(&["café"])
        );
    }
    /// An attribute NAME is raw page bytes while a selector's is UTF-8, so it must be decoded before the
    /// comparison or it agrees only on a UTF-8 page: `[data-año]` would match there and return NOTHING
    /// for the same document in windows-1252, where lxml — which decodes before tokenizing — matches.
    /// Values are unaffected either way (already decoded before comparison), which is what makes the
    /// name half silent.
    #[test]
    fn encoding_non_ascii_attribute_names_are_decoded_before_comparison() {
        // windows-1252: `data-año` is `data-a\xf1o`, one byte where UTF-8 writes two
        let w1252 = b"<p data-a\xf1o=\"v\">t</p>";
        assert_eq!(
            extract(w1252, &["[data-año]::text"], Some("windows-1252"), None)[0],
            v(&["t"])
        );
        // the predicate's VALUE and the `::attr()` terminal read the same materialized name
        assert_eq!(
            extract(w1252, &["[data-año=\"v\"]::attr(data-año)"], Some("windows-1252"), None)[0],
            v(&["v"])
        );
        // shift_jis: a name whose bytes hold no ASCII at all
        assert_eq!(
            extract(
                "<p 属性=\"v\">t</p>".as_bytes(),
                &["[属性]::text"],
                Some("utf-8"), None,
            )[0],
            v(&["t"])
        );
        assert_eq!(
            extract(
                &encoding_rs::SHIFT_JIS.encode("<p 属性=\"v\">t</p>").0,
                &["[属性]::text"],
                Some("shift_jis"), None,
            )[0],
            v(&["t"])
        );
        // a name the page spells in a DIFFERENT legacy encoding still must not match
        assert_eq!(
            extract(w1252, &["[data-año]::text"], Some("shift_jis"), None)[0],
            v(&[])
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
            extract(body, &["p::text"], Some("iso-2022-jp"), None)[0],
            v(&["株式会社"])
        );
        // and nothing downstream was reshaped by the phantom tag
        assert_eq!(
            extract(body, &["div::text", "r::text"], Some("iso-2022-jp"), None),
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
        assert_eq!(extract(&body, &["p::text"], None, None)[0], v(&["hi"]));
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
        assert_eq!(extract(&body, &["div::text"], None, None)[0], v(&["hi"]));
        assert_eq!(extract(&body, &["div#ab::text"], None, None)[0], v(&["hi"]));
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
    /// The rest of libxml2's data modes. Tokenizing any of these elements' CONTENT as markup is worse
    /// than losing a value: it fabricates elements that are not in the document and then honours the
    /// wrong end tag, desynchronizing every offset after it.
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

    /// libxml2 accepts `<html>`/`<head>`/`<body>` only as the document frame. Both halves of that rule
    /// have observable consequences, so both are pinned here.
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
    /// frame, and so does the engine.
    ///
    /// All three have optional start *and* end tags, so
    /// `<!DOCTYPE html><title>T</title><h1>a</h1><p>b</p>` is a conformant document with no frame in the
    /// byte stream at all. Without synthesis, every frame-anchored selector (`body h1`), every top-level
    /// sibling combinator (`h1 + p`, which needs a shared parent) and all root-level text would come back
    /// empty while lxml answers. The rules below are split into their own tests; this one owns the basic
    /// shape.
    #[test]
    fn the_document_frame_is_synthesized_when_the_page_omits_it() {
        // the conformant-but-frameless document from docs/COMPATIBILITY.md
        let doc = "<!DOCTYPE html><title>T</title><h1>a</h1><p>b</p>";
        assert_eq!(ex(doc, "head > title::text"), v(&["T"]));
        assert_eq!(ex(doc, "body h1::text"), v(&["a"]));
        assert_eq!(ex(doc, "h1 + p::text"), v(&["b"])); // needs a shared parent to be siblings
        assert_eq!(ex(doc, "html > body > p::text"), v(&["b"]));
        assert_eq!(ex(doc, "body > :first-child::text"), v(&["a"]));

        // a bare fragment gets a body, and root-level text is body text rather than dropped
        assert_eq!(ex("<div>a</div>", "body div::text"), v(&["a"]));
        assert_eq!(ex("abc", "body::text"), v(&["abc"]));
        assert_eq!(ex("abc<div>d</div>", "body::text"), v(&["abc"]));
        // ...but whitespace before the frame starts is not content and starts nothing
        assert_eq!(ex("   \n <div>a</div>", "body > :first-child::text"), v(&["a"]));
        assert_eq!(ex("   ", "body::text"), Vec::<String>::new());

        // an explicit frame is still used as written — nothing is invented on top of it
        assert_eq!(ex("<html><head><title>T</title></head><body><p>b</p></body></html>",
                      "html > head > title::text"), v(&["T"]));
        assert_eq!(ex("<html><div>d</div></html>", "html > body > div::text"), v(&["d"]));
        assert_eq!(ex("<html>  <meta id=M>", "html > head > meta::attr(id)"), v(&["M"]));
    }

    /// WHICH part of a synthesized frame a bare element opens is DERIVED from the oracle over the whole
    /// element universe (`implied_close::frame_content`), because it is not the relation it looks like:
    /// only `base`/`link`/`meta`/`script`/`style`/`title` open a `<head>`, while `input`/`noscript`/
    /// `template`/`basefont`/`bgsound`/`object` stay inside a head that is already open and open none.
    #[test]
    fn only_the_head_names_open_a_head() {
        // the head is opened only by the names that open one, and only before the body starts
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
    }

    /// A frameset document has no `<body>` at all — including when the `<frameset>` is written INSIDE the
    /// head, where it ends the head like any other non-head content but must NOT start a body. A real
    /// frameset page does exactly that, and wrapping it in an invented body put the whole frameset (and
    /// the `<body>` written after it) somewhere libxml2 never puts them.
    #[test]
    fn a_frameset_document_has_no_body() {
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
        // Head content inside an open `<frameset>` has no head to go in, so libxml2 opens a BODY for it —
        // the same one it opens for ordinary content there. Exactly the six `FrameContent::Head` names
        // change answer inside a frameset; found by `tools/seq_sweep.py`, then derived over the universe.
        for head_content in ["title", "meta", "style", "link", "base", "script"] {
            let doc = format!("<frameset><{head_content} id=Z>y");
            assert_eq!(ex(&doc, "//body//*/@id"), v(&["Z"]),
                       "<{head_content}> in a frameset belongs to a body");
            assert_eq!(ex(&doc, "//head//*/@id"), Vec::<String>::new());
        }
    }

    /// A written `<body>` is redundant only while one is OPEN — not merely because something else is.
    /// Two crawled pages needed this: a frameset writing its no-frames fallback, and a `<body>` after a
    /// `</body>` inside a table cell, where ignoring it left a whole trailing table nested in the earlier
    /// cell. `<head>` has the OTHER rule — it belongs to the phase before any body content, so anything
    /// else being open ends it whether or not a head is currently open.
    #[test]
    fn a_written_body_is_redundant_only_while_one_is_open() {
        assert_eq!(ex("<frameset><body><p>gb</p></body></frameset>", "body > p::text"), v(&["gb"]));
        assert_eq!(ex("<frameset><frameset><body><p>gb</p></frameset></frameset>", "body p::text"),
                   v(&["gb"]));
        assert_eq!(ex("<body id=1></body><td><body id=2>x", "//body/@id"), v(&["1", "2"]));
        assert_eq!(ex("<body id=1></body><div><body id=2>x", "//body/@id"), v(&["1", "2"]));
        // ...and while one IS open it is ignored, however deep
        assert_eq!(ex("<body id=1><div><body id=2>x</div>", "//body/@id"), v(&["1"]));
        assert_eq!(ex("<td><body id=Z>y", "body::attr(id)"), Vec::<String>::new());
        // the `<head>` rule
        assert_eq!(ex("<frameset><head id=Z><title>t</title></frameset>", "head::attr(id)"),
                   Vec::<String>::new());
        assert_eq!(ex("<head id=1></head><div><head id=2>x", "//head/@id"), v(&["1"]));
        // Character data ends an open head even when there is no body to move it into: a `<head>`
        // written after `</body>` keeps nothing but its elements.
        assert_eq!(ex("<body id=0></body><head>y</head>", "head::text"), Vec::<String>::new());
        assert_eq!(ex("<body id=0></body><head><title>t</title>y</head>", "head > title::text"),
                   v(&["t"]));
    }

    /// A page whose FIRST tag is `<head>` or `<body>` writes no `<html>` — and still gets one. A frame
    /// tag that builds only its own part leaves the head at the root with no parent and a second `<html>`
    /// around whatever follows `</head>`, so `html > head`, `html > body` and `head + script` all come
    /// back empty while the values under them look right.
    #[test]
    fn a_page_whose_first_tag_is_a_frame_part_still_gets_an_html() {
        assert_eq!(ex("<head id=H><title>t</title></head><p>y</p>", "html > head::attr(id)"),
                   v(&["H"]));
        assert_eq!(ex("<body id=B><p>y</p></body>", "html > body::attr(id)"), v(&["B"]));
        assert_eq!(ex("<head id=H></head><body id=B><p>y</p>", "html > head + body::attr(id)"),
                   v(&["B"]));
        // libxml2 leaves what follows an explicit `</head>` at `<html>` level (html5lib puts it back in
        // the head; the oracle here is libxml2), so the script is the head's SIBLING.
        assert_eq!(ex("<head><meta id=M></head><script>s</script><p>y</p>", "head + script::text"),
                   v(&["s"]));
    }

    /// Content after `</html>` gets a SECOND ROOT `<html>`, the same shape libxml2 builds, and no body,
    /// since one is already established. Without that frame a trailing `<script>` would be reachable as
    /// `//script` but not as `//html/script`, and text there would be dropped outright because the empty
    /// stack provides nothing to attach it to.
    ///
    /// The second root is verified against a page that self-closes `<html/>` inside a downlevel-revealed
    /// conditional comment, whose lxml tree has two roots both carrying the attributes. Browsers keep
    /// one element instead, which is why parsel's own CSS (scoped to the first root) and XPath disagree
    /// on such a document; the TREE is the oracle here.
    #[test]
    fn content_after_the_document_closes_gets_a_second_root() {
        assert_eq!(ex("<html a=1 /><html a=2><p>x</p>", "//html/@a"), v(&["1", "2"]));
        assert_eq!(ex("<html a=1></html><html a=2><p>x</p>", "//html/@a"), v(&["1", "2"]));
        // ...and the tail still gets a parent, so the values under it stay reachable
        assert_eq!(ex("<html a=1></html><p>x</p>", "html > body > p::text"), v(&["x"]));
        assert_eq!(ex("<div id=0></html>x", "//html/text()"), v(&["x"]));
        assert_eq!(ex("<html><body>y</body></html>tail", "//html/text()"), v(&["tail"]));
        assert_eq!(ex("<html><body>x</body></html><script>s</script>", "//html/script/text()"),
                   v(&["s"]));
        assert_eq!(ex("<html><body>x</body></html><p>y</p>", "//html/p/text()"), v(&["y"]));
        assert_eq!(ex("<html><body>x</body></html><p>y</p>", "//html/body/p/text()"),
                   Vec::<String>::new());
    }

    #[test]
    fn document_roots_share_sibling_triggers() {
        for q in ["html + html::text", "html ~ html::text", ":contains(\"alpha\") + ::text",
                  "html:has(body) + html::text"] {
            assert_eq!(ex("alpha</html>>", q), v(&[">"]), "{q}");
        }
        let html = "<html id=a><body><p class=hit>alpha</p></body></html>\
                    <html id=b><p>beta</p></html><html id=c><p>gamma</p>";
        assert_eq!(ex(html, "html#a + html::attr(id)"), v(&["b"]));
        assert_eq!(ex(html, "html#a ~ html::attr(id)"), v(&["b", "c"]));
        assert_eq!(ex(html, "html#a + html + html::attr(id)"), v(&["c"]));
        for pred in [":contains(\"alpha\")", ":has(.hit)"] {
            assert_eq!(ex(html, &format!("html{pred} + html p::text")), v(&["beta"]));
            assert_eq!(ex(html, &format!("html{pred} ~ html p::text")), v(&["beta", "gamma"]));
        }
    }

    /// `</head>` is an unconditional closer like `</body>`/`</html>`: libxml2 gives it the same end
    /// priority, so no open element out-ranks it. With `head` left at priority 0 an open `<tr>` blocked
    /// it and everything after the `</head>` stayed inside the head. Found by `tools/seq_sweep.py`.
    #[test]
    fn a_frame_end_tag_is_never_out_ranked() {
        assert_eq!(ex("<head id=0><tr id=1></head><div id=3>", "//*[@id=\"0\"]//*/@id"), v(&["1"]));
        assert_eq!(ex("<head id=0><td id=1></head><div id=3>", "//*[@id=\"0\"]//*/@id"), v(&["1"]));
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

        // frame-anchored and positional selectors are the ones this reshapes, in both directions
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
    /// Collapsing the END-TAG-OPEN branches into "scan a name, skip to `>`" would break the distinction,
    /// because `is_name_char` accepts `%`/`1`/`-`. Then `</%>` would become an unmatched end tag, JOINING
    /// the text either side where libxml2 keeps a bogus-comment node that SPLITS it:
    /// `Copyright 1991-2026</%> VECMAR Corporation` would be one text node instead of two. `</>` would
    /// fail the other way round, producing no event and leaving the runs un-joined where libxml2 ignores
    /// the whole thing and keeps ONE node.
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
        assert_eq!(extract(lead, &["p::text"], None, None)[0], v(&["x"]));
        let mid = b"<p>\xEF\xBB\xBFx</p>"; // U+FEFF mid-text -> preserved
        assert_eq!(extract(mid, &["p::text"], None, None)[0], v(&["\u{FEFF}x"]));
        // and in an attribute value
        let attr = "<a title=\"\u{FEFF}hi\">t</a>".as_bytes();
        assert_eq!(extract(attr, &["a::attr(title)"], None, None)[0], v(&["\u{FEFF}hi"]));
    }
    /// An INDENTED BOM still counts as the document BOM, because Parsel parses `text.strip()` — see
    /// [`document_bounds`]. What makes this worth a test rather than a footnote is the size of the
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
        assert_eq!(extract(doc, &q, None, None), vec![v(&["T"]), v(&["x"]), v(&["b"])]);
        // ...but only whitespace may precede it: after anything else the frame is already open, so the
        // U+FEFF is content — libxml2 keeps it and so does Parsel, whose strip cannot reach it.
        let after = b"z\xEF\xBB\xBF<!DOCTYPE HTML><html xmlns=\"x\"><head><title>T</title></head><body>b";
        assert_eq!(
            extract(after, &q, None, None),
            vec![v(&[]), v(&[]), v(&["z\u{FEFF}", "b"])]
        );
        // and a non-UTF-8 page decodes those three bytes as three characters, exactly as Parsel does
        let cp1252 = b"  \xEF\xBB\xBF<html><head><title>T</title></head><body>b";
        assert_eq!(
            extract(cp1252, &q, Some("windows-1252"), None)[0],
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

    /// A start tag the response ends inside is DROPPED, whole; libxml2 and html5lib agree. Keeping
    /// whatever was scanned is the false-positive direction: an element, with an attribute value holding
    /// the rest of the document, that no other parser reports.
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
    /// Parsel removed it before libxml2 ever saw it — see [`document_bounds`]. Whitespace-only, so it
    /// changes no value, but it does change the ROW COUNT of a `::text` column.
    #[test]
    fn trailing_whitespace_is_not_a_text_node() {
        let doc = b"<select><option>a<option class=c>\n\t ";
        assert_eq!(extract(doc, &["option::text"], None, None)[0], v(&["a"]));
        // ...and a document that is nothing but whitespace has no content at all
        assert_eq!(extract(b"  \n ", &["p::text"], None, None)[0], v(&[]));
        // non-whitespace at the end is untouched, trailing whitespace INSIDE it too
        let kept = b"<p>a </p>\n";
        assert_eq!(extract(kept, &["p::text"], None, None)[0], v(&["a "]));
    }

    /// VERTICAL TAB is in Python's strip set and not in Rust's `is_ascii_whitespace`. Omitting it here
    /// would leave character data before the frame and open the `<body>`, losing the head. See
    /// [`is_strip_ws`].
    #[test]
    fn vertical_tab_is_stripped_like_python_does() {
        let q: Vec<String> = ["head title::text", "html::attr(a)"].iter().map(|s| s.to_string()).collect();
        let want = vec![v(&["T"]), v(&["1"])];
        let page = |lead: &str| format!("{lead}<html a=1><head><title>T</title></head><body><p>p</p>");
        for lead in ["\u{0b}", " \u{0b}\n", "\u{0c}"] {
            assert_eq!(extract(page(lead).as_bytes(), &q, None, None), want, "lead {lead:?}");
            // ...in a single-byte encoding too: Parsel strips the DECODED text, so the label is
            // irrelevant to this half — only the BOM below is UTF-8-gated.
            assert_eq!(extract(page(lead).as_bytes(), &q, Some("windows-1252"), None), want, "cp1252 {lead:?}");
        }
        // stripping a vertical tab can expose the BOM, exactly as stripping a space can
        assert_eq!(extract(page("\u{0b}\u{feff}").as_bytes(), &q, None, None), want);
        // trailing, the other end of the same strip: no text node for the last option
        let doc = b"<select><option>a<option class=c>\x0b";
        assert_eq!(extract(doc, &["option::text"], None, None)[0], v(&["a"]));
        // ...but a vertical tab INSIDE the document is ordinary character data, not whitespace to
        // strip and not HTML whitespace either — it must survive in the value verbatim.
        assert_eq!(ex("<p>a\u{0b}b</p>", "p::text"), v(&["a\u{0b}b"]));
    }

    /// NUL is deleted AFTER the ends are trimmed, matching `Selector(text=…)` — the path Scrapy and
    /// web-poet are on. Recomputing the bounds afterwards would match `Selector(body=…)` instead and
    /// diverge from every Scrapy scraper; see [`normalize`].
    #[test]
    fn nul_is_deleted_after_the_ends_are_trimmed() {
        // the NUL is the last byte, so it blocks the strip and the space before it survives
        assert_eq!(extract(b"<option>x \x00", &["option::text"], None, None)[0], v(&["x "]));
        // and here it blocks the strip that would otherwise promote the U+FEFF to offset 0, leaving it
        // a character — which opens the body, so the head is gone
        let doc = b"\x00 \xEF\xBB\xBF<html a=1><head><title>T</title></head><body><p>p</p>";
        let q: Vec<String> = ["head title::text", "html::attr(a)", "p::text"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        assert_eq!(extract(doc, &q, None, None), vec![v(&[]), v(&[]), v(&["p"])]);
        // without the NUL the same page strips down to the BOM and keeps its head
        let stripped = b" \xEF\xBB\xBF<html a=1><head><title>T</title></head><body><p>p</p>";
        assert_eq!(extract(stripped, &q, None, None), vec![v(&["T"]), v(&["1"]), v(&["p"])]);
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

/// Browser mode's own vectors. Every expected value was measured in Chrome 156.0.8078.80 with a
/// CSP-served page load (`node tools/browser_oracle/probe.cjs chrome … claims`), the oracle
/// docs/BROWSER_MODE.md section 5 describes; the comments quote what the browser returned.
#[cfg(test)]
mod browser_mode_tests {
    use super::*;

    fn cols(html: &[u8], qs: &[&str]) -> FlatColumns {
        let qs: Vec<String> = qs.iter().map(|q| q.to_string()).collect();
        Plan::compile(&qs, &[]).extract(html, None, None).0
    }
    fn ex(html: &[u8], q: &str) -> Vec<String> {
        cols(html, &[q]).pop().unwrap()
    }
    fn lxml(html: &[u8], q: &str) -> Vec<String> {
        let lxml = CompileOptions { mode: ParseMode::Lxml, first_only: &[] };
        Plan::compile_with(&[q], &[], lxml).extract(html, None, None).0.pop().unwrap()
    }
    fn v(a: &[&str]) -> Vec<String> {
        a.iter().map(|s| s.to_string()).collect()
    }
    const NONE: &[&str] = &[];

    #[test]
    fn browser_is_the_default_mode() {
        assert_eq!(ParseMode::default(), ParseMode::Browser);
        assert_eq!(Plan::compile::<&str>(&[], &[]).mode(), ParseMode::Browser);
        for mode in [ParseMode::Browser, ParseMode::Lxml] {
            assert_eq!(ParseMode::from_name(mode.as_str()), Some(mode));
        }
        assert_eq!(ParseMode::from_name("html5"), None);
        assert_eq!(ParseMode::from_name("LXML"), None);
    }

    /// A browser parses with scripting on, so `<noscript>` holds raw text (entities NOT decoded), in
    /// the body and in the head, and a first-tag `<noscript>` is head content.
    #[test]
    fn noscript_is_raw_text() {
        let h = b"<!DOCTYPE html><body><noscript><p id=t>x &amp; y</p></noscript><p id=after>a</p>";
        assert_eq!(ex(h, "#t::text"), NONE); // []
        assert_eq!(ex(h, "noscript::text"), v(&["<p id=t>x &amp; y</p>"]));
        assert_eq!(ex(h, "#after::text"), v(&["a"]));
        assert_eq!(ex(h, "body > noscript::text").len(), 1); // parentNode: body
        assert_eq!(lxml(h, "#t::text"), v(&["x & y"]), "lxml mode keeps libxml2's markup reading");

        let head = b"<!DOCTYPE html><html><head><noscript><link id=l></noscript><title>T</title></head>";
        assert_eq!(ex(head, "head > noscript::text"), v(&["<link id=l>"]));
        assert_eq!(ex(head, "head title::text"), v(&["T"]));
        assert_eq!(ex(head, "#l::attr(id)"), NONE); // 0 elements

        let first = b"<noscript><p id=t>x</p></noscript><p id=a>a</p>";
        assert_eq!(ex(first, "head > noscript::text"), v(&["<p id=t>x</p>"])); // parentNode: head
        assert_eq!(ex(first, "body > #a::text"), v(&["a"]));
    }

    /// An HTML `<template>` element matches; its content is not in the document.
    #[test]
    fn template_content_is_inert() {
        let h = b"<!DOCTYPE html><body><div id=d>a<template id=t>b<i>i</i></template>c</div>";
        assert_eq!(ex(h, "#d::text"), v(&["a", "c"])); // "a|c"
        assert_eq!(ex(h, "#t::text"), NONE); // [""]: the template has no text node
        assert_eq!(ex(h, "#t::attr(id)"), v(&["t"]));
        assert_eq!(ex(h, "#d i::text"), NONE);
        assert_eq!(ex(h, "#d > *::attr(id)"), v(&["t"])); // one child element
        assert_eq!(ex(h, "normalize-space(//div[@id='d'])"), v(&["ac"])); // textContent "ac"
        assert_eq!(ex(h, "#d ::text"), v(&["a", "c"]));

        // content is neither a child nor a sibling of anything outside; the template itself is both
        let li = b"<!DOCTYPE html><body><ul><li id=a>1</li><template><li id=b>2</li></template><li id=c>3</li></ul>";
        assert_eq!(ex(li, "li::text"), v(&["1", "3"])); // 2 elements
        assert_eq!(ex(li, "li:nth-child(3)::text"), v(&["3"]));
        assert_eq!(ex(li, "li + li::text"), NONE);
        assert_eq!(ex(li, "template + li::text"), v(&["3"]));
        assert_eq!(ex(li, "li:last-child::text"), v(&["3"]));

        // the template is a boundary: an end tag inside it closes nothing outside
        let o = b"<!DOCTYPE html><body><div id=o><template id=t><p></div><b id=b>x</b></template><i id=i>y</i></div>";
        assert_eq!(ex(o, "#o > #i::text"), v(&["y"]));
        assert_eq!(ex(o, "#b::text"), NONE);

        let nested = b"<!DOCTYPE html><body><template id=t1><template id=t2><p id=y>z</p></template>\
                       <p id=w>w</p></template><p id=after>a</p>";
        assert_eq!(ex(nested, "#t1::attr(id)"), v(&["t1"]));
        assert_eq!(ex(nested, "#t2::attr(id), #w::text, #y::text"), NONE);
        assert_eq!(ex(nested, "body > #after::text"), v(&["a"]));

        // raw source keeps the content, as `innerHTML` serializes it
        let k = b"<!DOCTYPE html><body><div id=d><template id=t class=k><b>x</b></template></div>";
        assert_eq!(ex(k, "template.k"), v(&["<template id=t class=k><b>x</b></template>"]));
        // a template is head content
        let head = b"<!DOCTYPE html><html><head><template id=t><p id=y>z</p></template><title>T</title></head><body>";
        assert_eq!(ex(head, "head > template::attr(id)"), v(&["t"]));
        assert_eq!(ex(head, "head title::text"), v(&["T"]));
        let first = b"<template id=t><p>z</p></template><title>T</title><p id=a>a</p>";
        assert_eq!(ex(first, "head > template::attr(id)"), v(&["t"]));
        assert_eq!(ex(first, "head > title::text"), v(&["T"]));
        // `<noscript>` inside content is content too
        let ns = b"<!DOCTYPE html><body><template id=t><noscript><p id=y>x</noscript></p></template><p id=a>a</p>";
        assert_eq!(ex(ns, "#y::text"), NONE);
        assert_eq!(ex(ns, "body > *::attr(id)"), v(&["t", "a"]));
    }

    /// Template suppression keys on the namespace: an SVG `<template>` is an ordinary element, while one
    /// at an HTML integration point is HTML again.
    #[test]
    fn template_inertness_follows_the_namespace() {
        let svg = b"<!DOCTYPE html><body><svg><template id=t><text id=y>z</text></template></svg>";
        assert_eq!(ex(svg, "#y::text"), v(&["z"]));
        assert_eq!(ex(svg, "svg template text::text"), v(&["z"]));
        for h in [
            &b"<!DOCTYPE html><body><svg><foreignObject><template id=t><p id=y>z</p></template></foreignObject></svg>"[..],
            b"<!DOCTYPE html><body><math><mi><template id=t><p id=y>z</p></template></mi></math>",
            b"<!DOCTYPE html><body><svg><title><template id=t><p id=y>z</p></template></title></svg>",
        ] {
            assert_eq!(ex(h, "#y::text"), NONE, "{}", String::from_utf8_lossy(h)); // XHTML namespace
        }
    }

    /// HTML5 ignores `/>` on a non-void HTML element, so a raw-text element still reads its text and a
    /// template still has content. A foreign element honours it.
    #[test]
    fn self_closing_does_not_cancel_an_html_text_mode() {
        let title = b"<!DOCTYPE html><html><head><title/>a<b>x</b></title></head><body><p>p</p>";
        assert_eq!(ex(title, "head > title::text"), v(&["a<b>x</b>"]));
        assert_eq!(ex(title, "b"), NONE);
        assert_eq!(lxml(title, "b::text"), v(&["x"]), "lxml mode closes the title at `/>`");
        let body = b"<!DOCTYPE html><body><script/>a<b>x</b></script><textarea/>a<b>x</b>&amp;</textarea>\
                     <style/>a<b>x</b>&amp;</style><p>p</p>";
        assert_eq!(ex(body, "script::text"), v(&["a<b>x</b>"]));
        assert_eq!(ex(body, "textarea::text"), v(&["a<b>x</b>&"]));
        assert_eq!(ex(body, "style::text"), v(&["a<b>x</b>&amp;"]));
        assert_eq!(ex(body, "b"), NONE);
        let more = b"<!DOCTYPE html><body><xmp/>a<b>x</b></xmp><iframe/>c<i>y</i></iframe><noembed/>e<u>z</u>\
                     </noembed><noscript/>n<s>s</s></noscript><plaintext/>f<s>w</s>";
        assert_eq!(ex(more, "xmp::text"), v(&["a<b>x</b>"]));
        assert_eq!(ex(more, "iframe::text"), v(&["c<i>y</i>"]));
        assert_eq!(ex(more, "noembed::text"), v(&["e<u>z</u>"]));
        assert_eq!(ex(more, "noscript::text"), v(&["n<s>s</s>"]));
        assert_eq!(ex(more, "plaintext::text"), v(&["f<s>w</s>"]));
        assert_eq!(ex(more, "b, i, u, s"), NONE);
        // a self-closed template still opens its content
        let t = b"<!DOCTYPE html><body><template id=\"t\"/><p id=y>x</p>";
        assert_eq!(ex(t, "#t::attr(id)"), v(&["t"]));
        assert_eq!(ex(t, "#y::text"), NONE);
        assert_eq!(ex(t, "body > *::attr(id)"), v(&["t"]));
        // foreign: `/>` closes, and what follows is ordinary markup
        let svg = b"<!DOCTYPE html><body><svg><title/><text id=x>t</text></svg>";
        assert_eq!(ex(svg, "#x::text"), v(&["t"]));
        assert_eq!(ex(b"<!DOCTYPE html><body><svg><style/><text id=x>t</text></svg>", "#x::text"), v(&["t"]));
        assert_eq!(ex(b"<!DOCTYPE html><body><math><style/><mi id=x>t</mi></math>", "#x::text"), v(&["t"]));
        assert_eq!(ex(b"<!DOCTYPE html><body><svg><template id=\"t\"/><text id=y>x</text></svg>", "#y::text"), v(&["x"]));
        // ...unless an integration point makes it HTML again
        let fo = b"<!DOCTYPE html><body><svg><foreignObject><title/>a<b>x</b></title></foreignObject></svg>";
        assert_eq!(ex(fo, "title::text"), v(&["a<b>x</b>"]));
        assert_eq!(ex(fo, "b"), NONE);
    }

    /// NUL per HTML5: a NUL in a name stays (so `<di\0v>` is no `div`), markup-mode text drops it, and
    /// every other value reads it as U+FFFD.
    #[test]
    fn nul_follows_html5() {
        let name = b"<!DOCTYPE html><body><di\0v id=x>X</di\0v>";
        assert_eq!(ex(name, "div::text"), NONE); // localName "di\u{FFFD}v", 0 divs
        assert_eq!(ex(name, "#x::text"), v(&["X"]));
        assert_eq!(lxml(name, "div::text"), v(&["X"]), "lxml mode deletes NUL, as Parsel does");

        let text = b"<!DOCTYPE html><body><p id=p>a\0b</p><p id=q>\0</p><p id=r>\0 </p>";
        assert_eq!(ex(text, "#p::text"), v(&["ab"]));
        assert_eq!(ex(text, "#q::text"), NONE); // childNodes.length 0
        assert_eq!(ex(text, "#r::text"), v(&[" "]));

        let attr = b"<!DOCTYPE html><body><a id=a href='x\0y' da\0ta=z>t</a><a id=b href=x\0y>u</a>";
        assert_eq!(ex(attr, "#a::attr(href)"), v(&["x\u{FFFD}y"]));
        assert_eq!(ex(attr, "#b::attr(href)"), v(&["x\u{FFFD}y"]));
        assert_eq!(ex(attr, "a::attr(data)"), NONE); // name "da\u{FFFD}ta"
        assert_eq!(ex(attr, "[data]"), NONE);

        let raw = b"<!DOCTYPE html><html><head><title>a\0b</title><style>c\0d</style><script>e\0f</script></head>\
                    <body><textarea>g\0h</textarea><xmp>i\0j</xmp><noscript>k\0l</noscript><iframe>m\0n</iframe>";
        let got = cols(raw, &["title::text", "style::text", "script::text", "textarea::text", "xmp::text",
                              "noscript::text", "iframe::text"]);
        let want: Vec<Vec<String>> =
            ["a\u{FFFD}b", "c\u{FFFD}d", "e\u{FFFD}f", "g\u{FFFD}h", "i\u{FFFD}j", "k\u{FFFD}l", "m\u{FFFD}n"]
                .iter()
                .map(|s| v(&[s]))
                .collect();
        assert_eq!(got, want);
        assert_eq!(ex(b"<!DOCTYPE html><body><plaintext>a\0b", "plaintext::text"), v(&["a\u{FFFD}b"]));
        // a character reference to NUL is U+FFFD in both modes
        assert_eq!(ex(b"<!DOCTYPE html><body><p>&#0;x</p>", "p::text"), v(&["\u{FFFD}x"]));
        // table and select text drop it like body text
        assert_eq!(ex(b"<!DOCTYPE html><body><table id=t>\0<tr><td>a\0b</td></tr></table>", "td::text"), v(&["ab"]));
        assert_eq!(ex(b"<!DOCTYPE html><body><table id=t>\0<tr><td>a</td></tr></table>", "table::text"), NONE);
        assert_eq!(ex(b"<!DOCTYPE html><body><select><option>a\0b</option></select>", "option::text"), v(&["ab"]));
        // a dropped end tag still joins the runs either side
        assert_eq!(ex(b"<!DOCTYPE html><body><div>a\0</span>b</div>", "div::text"), v(&["ab"]));
    }

    /// Before the body the tree builder ignores a NUL outright: it neither opens the body nor ends the
    /// head, so `\0<title>` keeps the title in the head.
    #[test]
    fn nul_before_the_body_is_ignored() {
        let lead = b"\0<title>T</title><p id=x>x</p>";
        assert_eq!(ex(lead, "head > title::text"), v(&["T"]));
        assert_eq!(ex(lead, "body > p#x"), v(&["<p id=x>x</p>"])); // body: 1 child, head: 1 child
        let head = b"<!DOCTYPE html><html><head>\n\0<title>T</title></head><body><p>x</p>";
        assert_eq!(ex(head, "head::text"), v(&["\n"])); // ["\n", "title"]
        assert_eq!(ex(head, "head > title::text"), v(&["T"]));
        assert_eq!(ex(b" \0 <title>T</title>", "head > title::text"), v(&["T"]));
        // ...but the content after it still does
        let x = b"<!DOCTYPE html><html><head> \0x<title>T</title>";
        assert_eq!(ex(x, "head::text"), v(&[" "]));
        assert_eq!(ex(x, "body::text"), v(&["x"]));
        assert_eq!(ex(x, "body > title::text"), v(&["T"]));
        // and a frameset is still a frameset document
        let fs = b"<html>\0<frameset></frameset>";
        assert_eq!(ex(fs, "frameset"), v(&["<frameset></frameset>"]));
        assert_eq!(ex(fs, "body"), NONE);
    }

    /// No strip: only a BOM at offset 0 is consumed. An indented BOM and a leading vertical tab are
    /// characters, and a character opens the body.
    #[test]
    fn the_input_is_not_stripped() {
        let bom = "    \u{FEFF}<!DOCTYPE HTML><html a=1><head><title>T</title></head><body><p>x</p>";
        assert_eq!(ex(bom.as_bytes(), "body > title::text"), v(&["T"])); // parentNode: body
        assert_eq!(ex(bom.as_bytes(), "body::text").first().map(String::as_str), Some("\u{FEFF}"));
        assert_eq!(lxml(bom.as_bytes(), "head > title::text"), v(&["T"]), "lxml mode strips, as Parsel does");
        let vt = b"\x0b<html a=1><head><title>T</title></head><body><p>x</p>";
        assert_eq!(ex(vt, "body > title::text"), v(&["T"]));
        assert_eq!(ex("\u{FEFF}<!DOCTYPE html><title>T</title>".as_bytes(), "head > title::text"), v(&["T"]));
        let ws = b"\n\n  <!DOCTYPE html><html><head><title>T</title></head><body><p>x</p></body></html>\n\n";
        assert_eq!(ex(ws, "head > title::text"), v(&["T"]));
    }

    /// The lxml-mode shape browser mode refuses, with its reason, and the neighbours it keeps. A
    /// deferred winner's subtree is among the neighbours: it is read by a second whole-document pass.
    #[test]
    fn browser_mode_refuses_synthesized_outer_html() {
        let refused = [
            "div:has(a) tbody",
            "li:last-child *",
            "body",
            "html",
            "*",
            "//body",
            "body:has(p)",
            "p, body",
            "tbody",
            "tr",
            "p",
        ];
        let kept = [
            "div:has(a) ::text",
            "div:has(a) a::attr(href)",
            "li:last-child b::text",
            "li:last-child ::text",
            "//div[contains(., 'x')]//p/text()",
            "li:last-child a",
            "div:has(a)::attr(id)",
            "li:last-child::text",
            "body.page",
            "div",
            "body::text",
            "div:has(b)",
            "tbody.x",
            "p.lead",
            "span",
            "a::attr(href)",
            "a",
            "b.k",
            ".x",
        ];
        let all: Vec<&str> = refused.iter().chain(&kept).copied().collect();
        let browser = audit_schema_with(&all, &[], ParseMode::Browser);
        let lxml_mode = audit_schema_with(&all, &[], ParseMode::Lxml);
        for (i, q) in all.iter().enumerate() {
            assert!(lxml_mode.flat[i].is_supported(), "{q} is in the lxml contract");
            let reason = browser.flat[i].reason();
            if i < refused.len() {
                assert!(reason.is_some_and(|r| r.contains("browser mode")), "{q}: {reason:?}");
            } else {
                assert!(reason.is_none(), "{q}: {reason:?}");
            }
        }
        assert_eq!(audit_schema(&all, &[]).flat, browser.flat, "the default mode is browser");
        // a refused column is empty, never a value
        let h = b"<body><div id=x><a href=/1>l</a></div><ul><li><b>B</b></li></ul></body>";
        assert!(cols(h, &refused).iter().all(Vec::is_empty));
        // a grouped outer-HTML sub-field is refused the same way, under a container that can be a frame
        // element (a synthesized one only ever sits inside another)
        let group = |container: &str| GroupQuery {
            container: container.to_string(),
            subfields: vec![("self".to_string(), ".".to_string()), ("span".to_string(), "span".to_string())],
        };
        let a = audit_schema_with(
            &[] as &[String],
            &[group("html"), group("frameset.x"), group("div")],
            ParseMode::Browser,
        );
        for g in &a.groups[..2] {
            assert!(g.subfields[0].reason().is_some_and(|r| r.contains("browser mode")));
            assert!(g.subfields[1].is_supported());
        }
        assert!(a.groups[2].subfields.iter().all(Support::is_supported), "a <div> holds no frame element");
        assert!(audit_schema_with(&[] as &[String], &[group("html")], ParseMode::Lxml).ok());
        // any container can hold an implied <tbody>, and `.` is the container
        let synth = |container: &str, sub: &str| GroupQuery {
            container: container.to_string(),
            subfields: vec![("x".to_string(), sub.to_string())],
        };
        let b = audit_schema_with(
            &[] as &[String],
            &[synth("div", "b"), synth("div", "tbody"), synth("div", "tbody.x"), synth("tr", "."), synth("div", ".")],
            ParseMode::Browser,
        );
        let refused: Vec<bool> = b.groups.iter().map(|g| !g.subfields[0].is_supported()).collect();
        assert_eq!(refused, [false, true, false, true, false]);
    }

    /// A deferred winner's subtree is read from the whole document, so every element in it has the
    /// context the page gave it. Each expected value is Chrome 156's (native `querySelectorAll`, XPath
    /// through the lossless bridge); lxml mode's span re-scan differs from it wherever the context comes
    /// from outside the winner.
    #[test]
    fn a_deferred_tail_reads_the_winner_in_its_context() {
        let rows = b"<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td><i>d</i></td></tr></table>";
        assert_eq!(ex(rows, "tr:last-child td::text"), v(&["c"]));
        assert_eq!(ex(rows, "tr:last-child td i::text"), v(&["d"]));
        assert_eq!(ex(rows, "tr:has(i) td::text"), v(&["c"]));
        assert_eq!(ex(rows, "//tr[last()]//td/text()"), v(&["c"]));
        // an implied <tbody> and <tr> are winners like any other element
        assert_eq!(ex(b"<table><tr><td>a</td></tr></table>", "tbody:last-child td::text"), v(&["a"]));
        assert_eq!(ex(b"<table><td>a<td>b</table>", "tr:only-child ::text"), v(&["a", "b"]));
        // reconstruction copies a <b> the winner never wrote into it
        let fmt = b"<p><b>x</p><ul><li>one</li><li>two</li></ul>";
        assert_eq!(ex(fmt, "li:last-child b::text"), v(&["two"]));
        assert_eq!(ex(fmt, "li:has(b) ::text"), v(&["one", "two"]));
        assert_eq!(ex(fmt, "li:nth-last-child(2) b::text"), v(&["one"]));
        assert_eq!(lxml(fmt, "li:last-child b::text"), NONE);
        let fmt = b"<p><i>x</p><div>y<span>z</span></div>";
        assert_eq!(ex(fmt, "div:last-child i span::text"), v(&["z"]));
        assert_eq!(ex(fmt, r#"div:contains("y") i::text"#), v(&["y"]));
        // an SVG <title> is an HTML integration point, so its <b> is an element and not RCDATA text
        let svg = b"<svg><desc><b>x</b></desc><title><b>y</b></title></svg>";
        assert_eq!(ex(svg, "title:last-child b::text"), v(&["y"]));
        assert_eq!(ex(svg, "desc:has(b) ::text"), v(&["x"]));
        assert_eq!(lxml(svg, "title:last-child b::text"), NONE);
        // the form pointer: the inner <form> was never inserted
        let form = b"<form id=f><div><form id=g><input id=i></div></form>";
        assert_eq!(ex(form, "div:has(input) ::attr(id)"), v(&["i"]));
        // nested winners return each node once, in document order
        let nest = b"<ul><li>a<span>1</span></li><li>b<span>2</span><ul><li>c<span>3</span></li></ul></li></ul>";
        assert_eq!(ex(nest, "li:last-child span::text"), v(&["2", "3"]));
        assert_eq!(ex(nest, "li:last-child ::text"), v(&["b", "2", "c", "3"]));
        let nest = b"<div><a href=/1>x</a><div><a href=/2>y</a></div></div>";
        assert_eq!(ex(nest, "div:has(a) a::attr(href)"), v(&["/1", "/2"]));
        // a formatting element's copy wins as itself
        assert_eq!(ex(b"<p><b class=k>1</p><b>2</b>", "b:last-child ::text"), v(&["1", "2"]));
        // no winner, no second pass, and an empty column
        assert_eq!(ex(b"<ul><li>a</li></ul>", "div:has(a) ::text"), NONE);
    }

    /// Several tails in one schema share ONE second pass, each column with its own winners, and a column
    /// that shares a slot gets exactly what it gets alone.
    #[test]
    fn tails_share_one_pass_and_keep_their_own_winners() {
        let h = b"<table><tr><td>a</td></tr><tr><td><b>b</b></td></tr></table><ul><li>x</li><li><a href=/y>y</a></li></ul>";
        let qs = [
            "tr:last-child td::text",
            "tr:last-child b::text",
            "li:last-child a::attr(href)",
            "li:has(a) ::text",
            "tr:has(b) ::text",
            "td:only-child b::text",
        ];
        let together = cols(h, &qs);
        for (q, got) in qs.iter().zip(&together) {
            assert_eq!(got, &ex(h, q), "{q}");
        }
        assert_eq!(together, [v(&[]), v(&["b"]), v(&["/y"]), v(&["y"]), v(&["b"]), v(&["b"])]);
    }

    // ---- HTML5 tree construction (Phase 2). Each comment gives Chrome's tree for the document.

    /// The frame: a head is always built, head content written after `</head>` goes back into it,
    /// content after `</html>` lands in the body, and `</br>` and `<image>` are renamed.
    #[test]
    fn the_frame_is_html5s() {
        // html > [head, body > p#x > "x"]
        let p = b"<p id=x>x";
        assert_eq!(ex(p, "html > head + body > #x::text"), v(&["x"]));
        // head > [title, script#s > "1"], "\n\n", body > p > "a"
        let late = b"<title>T</title></head>\n<script id=s>1</script>\n<body><p>a";
        assert_eq!(ex(late, "head > title + #s::text"), v(&["1"]));
        assert_eq!(ex(late, "//html/text()"), v(&["\n\n"])); // one node: the script is in the head
        assert_eq!(ex(late, "body > p::text"), v(&["a"]));
        // body > [p > "a", div#a > "late"]
        let after = b"<p>a</p></body></html><div id=a>late</div>";
        assert_eq!(ex(after, "body > p + #a::text"), v(&["late"]));
        // body > ["a", br, "b", img#i]
        let renames = b"a</br>b<image id=i src=x>";
        assert_eq!(ex(renames, "body::text"), v(&["a", "b"]));
        assert_eq!(ex(renames, "body > br + img::attr(id)"), v(&["i"]));
    }

    /// In-body rules: HTML5's `<p>` closers, the button-scope walk, a stray `</p>`, repeated
    /// `<dt>`/headings/`<button>`/`<nobr>` and the leading newline in all three spellings.
    #[test]
    fn in_body_rules_are_html5s() {
        // p#p > "a"; section#s > "b"; "c"
        let closers = b"<p id=p>a<section id=s>b</section>c";
        assert_eq!(ex(closers, "#p::text"), v(&["a"]));
        assert_eq!(ex(closers, "body > #p + #s::text"), v(&["b"]));
        // p#p > b#b; div#d > b#b > "x" (the <div> closes the <p> through the <b>)
        let deep = b"<p id=p><b id=b><div id=d>x";
        assert_eq!(ex(deep, "#p #d"), NONE);
        assert_eq!(ex(deep, "body > #d > b::text"), v(&["x"]));
        // div#d > ["A", p, "B"]
        let stray = b"<div id=d>A</p>B</div>";
        assert_eq!(ex(stray, "#d::text"), v(&["A", "B"]));
        // dl > [dt#a, dt#b, dd#c]
        let dl = b"<dl><dt id=a>a<dt id=b>b<dd id=c>c</dl>";
        assert_eq!(ex(dl, "dl > *::attr(id)"), v(&["a", "b", "c"]));
        for h in [&b"<h1 id=a>a<h2 id=b>b"[..], b"<button id=a>x<button id=b>y", b"<nobr id=a>x<nobr id=b>y"] {
            assert_eq!(ex(h, "body > *::attr(id)"), v(&["a", "b"]), "{}", String::from_utf8_lossy(h));
        }
        // pre > "x", textarea > "y", listing > "z": the newline after each start tag is dropped
        let lf = b"<pre id=p>\nx</pre><textarea id=t>\ny</textarea><listing id=l>&#10;z</listing>";
        assert_eq!(ex(lf, "#p::text, #t::text, #l::text"), v(&["x", "y", "z"]));
    }

    /// The form pointer, and an element that leaves the stack of open elements while it is still a DOM
    /// ancestor of what is open.
    #[test]
    fn the_form_pointer_is_html5s() {
        // form#a > input#i (the second <form> is ignored)
        let second = b"<form id=a><form id=b><input id=i>";
        assert_eq!(ex(second, "#a > #i::attr(id)"), v(&["i"]));
        assert_eq!(ex(second, "#b::attr(id)"), NONE);
        // table > [form#f, tbody > tr > td > input#i]
        let t = b"<table><form id=f><tr><td><input id=i></table>";
        assert_eq!(ex(t, "table > #f + tbody #i::attr(id)"), v(&["i"]));
        assert_eq!(ex(t, "#f #i"), NONE);
        // form#f > div#d > ["a", p#after > "x"]
        let ghost = b"<form id=f><div id=d>a</form><p id=after>x</p>";
        assert_eq!(ex(ghost, "#f #after::text"), v(&["x"]));
        assert_eq!(ex(ghost, "#d::text"), v(&["a"]));
    }

    /// Tables: implied `<tbody>`, `<tr>` and `<colgroup>`, table parts and `<frame>` outside their
    /// context ignored, hidden inputs kept, and fostered content outside the table.
    #[test]
    fn tables_are_html5s() {
        // table#t > tbody > tr > td > "c"
        assert_eq!(ex(b"<table id=t><tr><td>c</table>", "#t > tbody > tr > td::text"), v(&["c"]));
        // table#t > [colgroup > col#c, caption > "x", tbody > tr > th > "h"]
        let parts = b"<table id=t><col id=c><caption>x</caption><tr><th>h</table>";
        assert_eq!(ex(parts, "#t > colgroup > #c::attr(id)"), v(&["c"]));
        assert_eq!(ex(parts, "#t > colgroup + caption::text"), v(&["x"]));
        // body > "xyz": the cell, the row and the frame are ignored, and the text is one node
        let stray = b"<td id=a>x<tr id=b>y<frame id=f>z";
        assert_eq!(ex(stray, "body::text"), v(&["xyz"]));
        assert_eq!(ex(stray, "td, tr, frame"), NONE);
        // body > [input#v, table#t > input#h]
        let input = b"<table id=t><input id=h type=hidden><input id=v>";
        assert_eq!(ex(input, "#t > input::attr(id)"), v(&["h"]));
        assert_eq!(ex(input, "body > #v::attr(id)"), v(&["v"]));
        // body > [p#m > "moved", table#t]
        let foster = b"<table id=t><p id=m>moved</table>";
        assert_eq!(ex(foster, "body > #m::text"), v(&["moved"]));
        assert_eq!(ex(foster, "#t #m"), NONE);
    }

    /// `<select>` as Chrome parses it since customisable select: a second one closes the first, and
    /// the content model is otherwise open.
    #[test]
    fn select_is_chromes() {
        // select#a > option#o > "1"; "x"
        let nested = b"<select id=a><option id=o>1<select id=b>x";
        assert_eq!(ex(nested, "body::text"), v(&["x"]));
        assert_eq!(ex(nested, "#a > #o::text"), v(&["1"]));
        assert_eq!(ex(nested, "#b"), NONE);
        // select > div > "d"; input#i; "z"
        let open = b"<select><div>d</div><input id=i>z";
        assert_eq!(ex(open, "select > div::text"), v(&["d"]));
        assert_eq!(ex(open, "body > select + #i::attr(id)"), v(&["i"]));
    }

    /// Foreign content: integration points, the start tags that break out of it, CDATA as text, and
    /// `/>` honoured.
    #[test]
    fn foreign_content_is_html5s() {
        // svg#s > title#t > b#b > "x"
        assert_eq!(ex(b"<svg id=s><title id=t><b id=b>x</b></title></svg>", "#t > #b::text"), v(&["x"]));
        // svg#s > g#g; p#p > "x"
        let out = b"<svg id=s><g id=g><p id=p>x";
        assert_eq!(ex(out, "body > #s + #p::text"), v(&["x"]));
        // svg > text#t > "ab<cc"
        assert_eq!(ex(b"<svg id=s><text id=t>a<![CDATA[b<c]]>c</text></svg>", "#t::text"), v(&["ab<cc"]));
        // svg > font#f > "a"; font#g (color) > "b" in the body
        let font = b"<svg id=s><font id=f>a</font><font id=g color=red>b";
        assert_eq!(ex(font, "#s > #f::text"), v(&["a"]));
        assert_eq!(ex(font, "body > #g::text"), v(&["b"]));
        // math > [mi > b > "x", annotation-xml (text/html) > div > "y"]
        let math = b"<math id=m><mi id=i><b id=b>x</b></mi><annotation-xml id=a encoding=text/html><div id=d>y";
        assert_eq!(ex(math, "#i > #b::text"), v(&["x"]));
        assert_eq!(ex(math, "#a > #d::text"), v(&["y"]));
        // svg > [path, rect]
        assert_eq!(ex(b"<svg id=s><path id=\"p\"/><rect id=r></svg>", "#s > *::attr(id)"), v(&["p", "r"]));
    }

    /// Active formatting elements: reconstruction (a clone carries the original's attributes), Noah's
    /// Ark, and the adoption agency's forward state.
    #[test]
    fn formatting_elements_are_html5s() {
        // p > b#b.k > "1"; b#b.k > "2"
        let clone = b"<p><b id=b class=k>1</p>2";
        assert_eq!(ex(clone, "b.k::text"), v(&["1", "2"]));
        assert_eq!(ex(clone, "body > b::text"), v(&["2"]));
        // the copy's raw source is the start tag it was copied from, then its own content
        assert_eq!(ex(clone, "b.k"), v(&["<b id=b class=k>1", "<b id=b class=k>2"]));
        // after </p>, three of the four identical <b>s reopen, nested, around "x"
        assert_eq!(ex(b"<p><b>1<b>2<b>3<b>4</p>x", "body > b > b > b::text"), v(&["x"]));
        assert_eq!(ex(b"<p><b>1<b>2<b>3<b>4</p>x", "body > b > b > b > b"), NONE);
        // b#b > ["1", i#i > "2"]; i#i > "3"
        assert_eq!(ex(b"<b id=b>1<i id=i>2</b>3", "body > i::text"), v(&["3"]));
        // b#b > "1"; p#p > [b#b > "2", "3", i#x > "y"]: after </b> the paragraph takes the content
        let aa = b"<b id=b>1<p id=p>2</b>3<i id=x>y</i>";
        assert_eq!(ex(aa, "p > #x::text"), v(&["y"]));
        // a#a > "1"; div#d > [a#a > "2", "3", i#x > "y"]
        let a = b"<a id=a href=x>1<div id=d>2</a>3<i id=x>y</i></div>";
        assert_eq!(ex(a, "#d > #x::text"), v(&["y"]));
    }

    /// The document mode, `<frameset>`, and a second `<body>` tag's attributes.
    #[test]
    fn document_mode_frameset_and_merged_attributes() {
        // quirks: p#p > table#t; with a doctype: p#p, table#t
        assert_eq!(ex(b"<p id=p><table id=t>", "#p > #t::attr(id)"), v(&["t"]));
        assert_eq!(ex(b"<!DOCTYPE html><p id=p><table id=t>", "#p + #t::attr(id)"), v(&["t"]));
        // html > [head, frameset#f > [frame#a, frame#b]]
        let fs = b"<frameset id=f><frame id=a><frame id=b></frameset>";
        assert_eq!(ex(fs, "html > head + frameset > frame::attr(id)"), v(&["a", "b"]));
        // body.late > [p > "x", div#d > "y"]: the merged class reaches what follows the tag
        let merged = b"<body><p>x</p><body class=late><div id=d>y";
        assert_eq!(ex(merged, "body.late #d::text"), v(&["y"]));
    }

    /// CSS matches the values of HTML's listed attributes ASCII-case-insensitively on an HTML element;
    /// XPath, an SVG element and an unlisted attribute compare exactly.
    #[test]
    fn css_folds_htmls_listed_attribute_values() {
        let h = b"<!DOCTYPE html><input id=a type=SUBMIT><a id=b rel=\"NoFollow External\" hreflang=EN-us href=x>l</a>\
                  <div id=c lang=EN title=Up></div><svg><g id=g type=SUBMIT/></svg>";
        // Chrome: a; b; b; c; [] for each
        assert_eq!(ex(h, "[type=\"submit\"]::attr(id)"), v(&["a"]));
        assert_eq!(ex(h, "[rel~=\"nofollow\"]::attr(id)"), v(&["b"]));
        assert_eq!(ex(h, "[hreflang|=\"en\"]::attr(id)"), v(&["b"]));
        assert_eq!(ex(h, "[lang^=\"e\"]::attr(id)"), v(&["c"]));
        assert_eq!(ex(h, "[title=\"up\"]::attr(id)"), NONE);
        assert_eq!(ex(h, "//*[@type='submit']/@id"), NONE);
        assert_eq!(lxml(h, "[type=\"submit\"]::attr(id)"), NONE, "lxml mode compares exactly");
    }

    /// Past 512 open elements Chrome makes a new element the current node's sibling; text still goes
    /// into the current node.
    #[test]
    fn the_tree_is_at_most_as_deep_as_chromes() {
        let mut h = b"<!DOCTYPE html>".to_vec();
        for i in 0..515 {
            h.extend(format!("<div id=d{i}>").bytes());
        }
        h.extend(b"x<p id=p>y</p>z</div>w");
        // d509 > [d510, d511, d512, d513 > "w", d514 > "xz", p > "y"]
        assert_eq!(ex(&h, "#d509 > *::attr(id)"), v(&["d510", "d511", "d512", "d513", "d514", "p"]));
        assert_eq!(ex(&h, "#d509 ::text"), v(&["w", "xz", "y"]), "in document order");
        assert_eq!(ex(&h, "#d514::text"), v(&["xz"]));
        assert_eq!(ex(&h, "#d513::text"), v(&["w"]));
        assert_eq!(ex(&h, "#d508 > *::attr(id)"), v(&["d509"]));
    }

    /// The class lxml mode's builder gives an element never collides with browser mode's three namespace
    /// marks, which switch on browser mode's attribute-name rules.
    #[test]
    fn libxml2_classes_leave_the_namespace_marks_free() {
        for name in ["a", "p", "div", "table", "td", "tr", "body", "html", "head", "li", "dd", "x-y", "option"] {
            assert!(implied_close::classify(name.as_bytes()).1 < u8::MAX - 2, "{name}");
        }
    }

    /// A raw-text, RCDATA or script end tag the input ends on right after its name is text in both
    /// modes, as in Chrome and libxml2. `<noscript>` is raw text in browser mode only.
    #[test]
    fn a_raw_end_tag_the_input_ends_on_stays_text() {
        for (doc, q, want) in [
            (&b"<title>a</title"[..], "title::text", "a</title"),
            (b"<textarea>a</textarea", "textarea::text", "a</textarea"),
            (b"<xmp>a<b>b</xmp", "xmp::text", "a<b>b</xmp"),
            (b"<script>a</script", "script::text", "a</script"),
        ] {
            assert_eq!(ex(doc, q), v(&[want]), "{q}");
            assert_eq!(lxml(doc, q), v(&[want]), "{q}");
        }
        assert_eq!(ex(b"<noscript>a</noscript", "noscript::text"), v(&["a</noscript"]));
        assert_eq!(lxml(b"<noscript>a</noscript", "noscript::text"), v(&["a"]));
        assert_eq!(ex(b"<title>a</title x", "title::text"), v(&["a"]));
    }

    /// Attribute names as Chrome's DOM has them: lowercased, SVG's camel case and MathML's
    /// `definitionURL` restored, and `xlink:`/`xml:`/`xmlns` attributes of a foreign element in a
    /// namespace. `::attr()` reads an attribute as `getAttribute()` does (the name lowercased on an HTML
    /// element, the DOM name exactly on a foreign one) and XPath's `@` compares the DOM name exactly; a
    /// CSS attribute selector compares ASCII-case-insensitively and names no namespaced attribute.
    #[test]
    fn foreign_attribute_names_are_adjusted() {
        let h = b"<!DOCTYPE html><svg VIEWBOX=\"0 0 1 1\" id=s fill=red><text textlength=5 id=t>x</text>\
                  <a xlink:href=u xml:lang=en id=a>y</a></svg><math definitionurl=d id=m></math>\
                  <div viewBox=1 xlink:href=h id=d DATA-K=v></div>";
        // Chrome: CSS
        assert_eq!(ex(h, "[viewBox]::attr(id)"), v(&["s", "d"]));
        assert_eq!(ex(h, "[viewbox]::attr(id)"), v(&["s", "d"]));
        assert_eq!(ex(h, "svg::attr(viewBox)"), v(&["0 0 1 1"]));
        assert_eq!(ex(h, "svg::attr(viewbox)"), NONE);
        assert_eq!(ex(h, "text::attr(textLength)"), v(&["5"]));
        assert_eq!(ex(h, "text::attr(textlength)"), NONE);
        assert_eq!(ex(h, "[definitionurl]::attr(id)"), v(&["m"]));
        assert_eq!(ex(h, "math::attr(definitionURL)"), v(&["d"]));
        assert_eq!(ex(h, "math::attr(definitionurl)"), NONE);
        assert_eq!(ex(h, "[FILL]::attr(id)"), v(&["s"]));
        // getAttribute() lowercases the name on an HTML element and on no other
        assert_eq!(ex(h, "div::attr(viewBox)"), v(&["1"]));
        assert_eq!(ex(h, "div::attr(viewbox)"), v(&["1"]));
        assert_eq!(ex(h, "div::attr(ID)"), v(&["d"]));
        assert_eq!(ex(h, "div::attr(Data-K)"), v(&["v"]));
        assert_eq!(ex(h, "div::attr(XLINK:HREF)"), v(&["h"]));
        assert_eq!(ex(h, "svg::attr(ID)"), NONE);
        assert_eq!(ex(h, "svg::attr(VIEWBOX)"), NONE);
        assert_eq!(ex(h, "text::attr(TEXTLENGTH)"), NONE);
        assert_eq!(ex(h, "math::attr(DEFINITIONURL)"), NONE);
        assert_eq!(ex(h, "svg a::attr(xlink:href)"), v(&["u"]));
        assert_eq!(ex(h, "[href]::attr(id)"), NONE);
        // svg#s > g#g, math#m, div#d, each with an xmlns: in a namespace on the foreign ones
        let ns = b"<!DOCTYPE html><svg xmlns=\"http://www.w3.org/2000/svg\" id=s><g xmlns=q id=g></g></svg>\
                   <math xmlns=m id=m></math><div xmlns=q id=d></div>";
        assert_eq!(ex(ns, "[xmlns]::attr(id)"), v(&["d"]));
        assert_eq!(ex(ns, "*::attr(xmlns)"), v(&["http://www.w3.org/2000/svg", "q", "m", "q"]));
        // Chrome's tree through the XPath bridge. An XPath name is lowercase in both modes, so it reaches
        // no camel-cased name, element or attribute.
        assert_eq!(ex(h, "//*/@viewbox"), v(&["1"]));
        assert_eq!(ex(h, "//*[@viewbox]/@id"), v(&["d"]));
        assert_eq!(ex(h, "//*[@textlength]/@id"), NONE);
        assert_eq!(ex(h, "//*[@fill]/@id"), v(&["s"]));
        let camel = b"<!DOCTYPE html><svg><clipPath id=c></clipPath></svg>";
        assert_eq!(ex(camel, "//clippath/@id"), NONE);
        assert_eq!(ex(camel, "clippath::attr(id)"), v(&["c"]));
        // lxml mode: libxml2 lowercases every name, and parsel's `::attr()` is XPath's exact `@`
        assert_eq!(lxml(h, "svg::attr(viewbox)"), v(&["0 0 1 1"]));
        assert_eq!(lxml(h, "svg::attr(viewBox)"), NONE);
        assert_eq!(lxml(h, "div::attr(ID)"), NONE);
        assert_eq!(lxml(h, "div::attr(id)"), v(&["d"]));
    }

    /// A declarative shadow root takes the `<template>` and its content out of the document, on a host
    /// that can have one.
    #[test]
    fn declarative_shadow_roots_follow_the_host_conditions() {
        // div#h > "ab" (shadow root holds p#s)
        let div = b"<div id=h>a<template id=t shadowrootmode=open><p id=s>x</p></template>b</div>";
        assert_eq!(ex(div, "#h::text"), v(&["ab"]));
        assert_eq!(ex(div, "#t, #s"), NONE);
        // a <table> or a <b> cannot host one: the template stays, as an ordinary template
        let table = b"<table id=h><template id=t shadowrootmode=open><p id=s>x</p></template></table>";
        assert_eq!(ex(table, "#h > #t::attr(id)"), v(&["t"]));
        let b = b"<b id=h><template id=t shadowrootmode=open>x</template></b>";
        assert_eq!(ex(b, "#h > #t::attr(id)"), v(&["t"]));
        // a custom element can; a second template on the same host stays
        let two = b"<x-y id=h><template id=t shadowrootmode=OPEN>x</template><template id=u shadowrootmode=open>y</template></x-y>";
        assert_eq!(ex(two, "#h > template::attr(id)"), v(&["u"]));
    }

    // The retroactive rules. A document where one fires is replayed over its finished tree, so what was
    // matched before the rule fired comes out as the browser has it. Every expected value below is
    // Chrome 156's (tools/browser_oracle/client.py).

    /// A later `<html>` or `<body>` tag's attributes reach the element itself and everything matched
    /// before the tag.
    #[test]
    fn a_later_frame_tag_merges_into_what_was_matched() {
        // html.k[lang=en] > [head, body#b.late[title=t] > [p#p > "a", div#d > "y"]]
        let h = b"<!DOCTYPE html><p id=p>a</p><html lang=en class=k><body id=b><body class=late title=t><div id=d>y</div>";
        assert_eq!(ex(h, "html.k #p::text"), v(&["a"]));
        assert_eq!(ex(h, "html::attr(class)"), v(&["k"]));
        assert_eq!(ex(h, "body.late#b > *::attr(id)"), v(&["p", "d"]));
        assert_eq!(ex(h, "body::attr(title)"), v(&["t"]));
        assert_eq!(ex(h, "[title=t] div::text"), v(&["y"]));
        // an attribute no selector asks about changes nothing, and costs no replay
        assert_eq!(ex(b"<p>a</p><body lang=en>", "body p::text"), v(&["a"]));
    }

    /// A later `<html>` or `<body>` tag replays the document only when its attributes change an answer:
    /// a compound that can match the element matches it differently, or a terminal reads one of them.
    #[test]
    fn a_later_frame_tag_replays_only_when_it_changes_an_answer() {
        let merge = |q: &str| {
            let h = b"<!DOCTYPE html><p class=price id=p>a</p><body class=late title=t>";
            Plan::compile(&[q], &[]).retro_census(h, EncodingHints::default()) & 1 != 0
        };
        // `.price` can match the body, but `late` is not `price`; `p.late` cannot match it at all
        for q in [".price::text", "p.late::text", "div::attr(title)", "[id=p]::text", "#p::attr(class)"] {
            assert!(!merge(q), "{q}");
        }
        for q in ["body.late p::text", ".late::attr(id)", "body::attr(title)", ":not(.late) > p::text", "* ::attr(title)"] {
            assert!(merge(q), "{q}");
        }
    }

    /// Successive frame tags merge first tag first, and a later one is judged with the earlier ones'
    /// attributes in place, so a compound only their attributes together match is answered whatever else
    /// the schema asks.
    #[test]
    fn successive_frame_tags_merge_together() {
        for (h, q) in [
            (&b"<body><p>X</p><body class=a><body id=b>"[..], "body.a#b p::text"),
            (b"<p>X</p><html class=a><html id=b>", "html.a#b p::text"),
            (b"<body class=a><p>X</p><body id=b><body lang=c>", "body.a#b[lang=c] p::text"),
        ] {
            assert_eq!(ex(h, q), v(&["X"]), "{q}");
            let both = cols(h, &[q, "body::attr(class)"]);
            assert_eq!(both[0], v(&["X"]), "{q} beside another column");
        }
        assert_eq!(ex(b"<p>X</p><body class=a><body class=b id=b>", "body.b p::text"), NONE);
    }

    /// Body text drops NUL, except a NUL right after a `<` read as text, which Chrome makes U+FFFD.
    #[test]
    fn a_nul_after_a_text_lt_is_replaced_in_browser_mode() {
        assert_eq!(ex(b"<p>x<\0y", "p::text"), v(&["x<\u{FFFD}y"]));
        assert_eq!(ex(b"<p>x<\0\0y", "p::text"), v(&["x<\u{FFFD}y"]));
        assert_eq!(ex(b"<p>x<<\0y", "p::text"), v(&["x<<\u{FFFD}y"]));
        assert_eq!(ex(b"<p>x&lt;\0y", "p::text"), v(&["x<y"]));
        assert_eq!(ex(b"<p>x\0y", "p::text"), v(&["xy"]));
        assert_eq!(ex(b"<p><b>x<\0</b>y", "b::text"), v(&["x<\u{FFFD}"]));
        assert_eq!(lxml(b"<p>x<\0y", "p::text"), v(&["x"])); // NUL deleted first, so `<y` is a tag
    }

    /// A tag whose whitespace run before `>` has a CR as its second character swallows the LF after the
    /// `>`, as in Chrome; a CR anywhere else in the run does not.
    #[test]
    fn a_cr_second_in_a_tags_whitespace_swallows_the_next_lf() {
        assert_eq!(ex(b"<p><span \r>\nx", "p ::text"), v(&["x"]));
        assert_eq!(ex(b"<p><span \r\n>\nx", "p ::text"), v(&["x"]));
        assert_eq!(ex(b"<p>a</b\r\r>\nx", "p ::text"), v(&["ax"]));
        assert_eq!(ex(b"<p><span\r>\nx", "p ::text"), v(&["\nx"]));
        assert_eq!(ex(b"<p><span  \r>\nx", "p ::text"), v(&["\nx"]));
        assert_eq!(ex(b"<p><span \r a>\nx", "p ::text"), v(&["\nx"]));
        assert_eq!(ex(b"<pre \r>\n\nx</pre>", "pre::text"), v(&["x"]));
        assert_eq!(lxml(b"<p><span \r>\nx", "span::text"), v(&["\nx"]));
    }

    /// A formatting element with an `is` attribute is never on the list of active formatting elements in
    /// Chrome: it is not reconstructed, and its end tag is any other end tag.
    #[test]
    fn a_formatting_element_with_is_is_not_an_active_formatting_element() {
        assert_eq!(ex(b"<div><b is></div>x", "b::text"), NONE);
        assert_eq!(ex(b"<div><b iss></div>x", "b::text"), v(&["x"]));
        assert_eq!(ex(b"<b is>1<p>2</b>3", "p::text"), v(&["23"]));
        assert_eq!(ex(b"<a is>1<a>2", "a a::text"), v(&["2"]));
    }

    /// lxml mode recovers a deferred winner's tail from its span, where the winner is never its own
    /// descendant and an implied winner (`*` matching the `<body>`) roots the tail at itself.
    #[test]
    fn a_deferred_tail_is_rooted_at_its_winner_in_lxml_mode() {
        let h = b"<div><div><b>x</b></div></div>";
        assert_eq!(lxml(h, "div:has(b) div"), v(&["<div><b>x</b></div>"]));
        assert_eq!(lxml(h, "div:has(> b) div"), NONE);
        let h = b"<li>caf\xc3\xa9<r>b";
        assert_eq!(lxml(h, "li:has(r) li::text"), NONE);
        assert_eq!(lxml(h, "*:has(r) li::text"), v(&["caf\u{e9}"]));
        assert_eq!(lxml(h, "//*[text()=\"caf\u{e9}\"]//li"), NONE);
    }

    /// A `::` inside an XPath literal is part of the value, not an axis.
    #[test]
    fn a_double_colon_in_an_xpath_literal_is_not_an_axis() {
        let h = b"<div id=\"a::b\"><p id=x>t</p></div><ul><li id=l1>1</li><li id=l2>2</li></ul>";
        assert_eq!(ex(h, "//*[@id=\"a::b\"]//*/@id"), v(&["x"]));
        assert_eq!(lxml(h, "//*[@id='a::b']/p/text()"), v(&["t"]));
        assert_eq!(ex(h, "//li[@id=\"l1\"]/following-sibling::li/@id"), v(&["l2"]));
    }

    /// Whitespace after `</body>` goes into the current node without reconstructing the active formatting
    /// elements, as in Chrome; other text after it reconstructs them, as in the body.
    #[test]
    fn whitespace_after_body_reconstructs_nothing() {
        let h = b"<div><font></div></body>\n";
        assert_eq!(ex(h, "font::text"), NONE);
        let h = b"<div><font></div></body>\nX";
        assert_eq!(ex(h, "font::text"), v(&["X"]));
        assert_eq!(ex(b"<div><font></div></body></html> ", "font::text"), NONE);
        assert_eq!(ex(b"<div><font></div><span></body>\n", "span::text"), v(&["\n"]));
    }

    /// A valueless attribute is `""` in browser mode, as in Chrome, where lxml mode gives libxml2's
    /// minimized booleans their own name.
    #[test]
    fn a_valueless_attribute_is_empty_in_browser_mode() {
        let h = b"<script defer src=x></script><input disabled><select multiple></select>";
        assert_eq!(ex(h, "script::attr(defer)"), v(&[""]));
        assert_eq!(ex(h, "input::attr(disabled)"), v(&[""]));
        assert_eq!(ex(h, "//select/@multiple"), v(&[""]));
        assert_eq!(ex(h, "[disabled=disabled]::attr(id)"), NONE);
        assert_eq!(lxml(h, "script::attr(defer)"), v(&["defer"]));
    }

    /// A `<select>`'s default option skips a disabled one, and an option is disabled by its own attribute
    /// or by the nearest `<optgroup>` around it, as in Chrome.
    #[test]
    fn a_disabled_optgroup_disables_its_options() {
        let select = |options: &str| {
            let h = format!("<select><button><selectedcontent></selectedcontent></button>{options}</select>");
            ex(h.as_bytes(), "selectedcontent::text")
        };
        assert_eq!(select("<optgroup disabled><option>A</option></optgroup><option>B</option>"), v(&["B"]));
        assert_eq!(select("<optgroup disabled><div><option>A</option></div></optgroup><option>B</option>"), v(&["B"]));
        assert_eq!(select("<optgroup disabled><option>A</option></optgroup><option disabled>B</option>"), NONE);
        assert_eq!(select("<optgroup disabled><optgroup><option>A</option></optgroup></optgroup>"), v(&["A"]));
        assert_eq!(select("<optgroup disabled><option selected>A</option></optgroup><option>B</option>"), v(&["A"]));
    }

    /// The outer HTML of a frame element the page never wrote starts with the later tag that gave it
    /// attributes, and that tag appears once.
    #[test]
    fn a_merged_frame_element_has_its_later_tag_once() {
        assert_eq!(ex(b"<p>x</p><body class=late>", "body.late"), v(&["<body class=late><p>x</p>"]));
        assert_eq!(ex(b"<p>x</p><html class=b>", "html.b"), v(&["<html class=b><p>x</p>"]));
        assert_eq!(ex(b"<p><b class=k>1</p>2", "b"), v(&["<b class=k>1", "<b class=k>2"]));
    }

    /// The attribute values tree construction reads are compared decoded, so a character reference
    /// builds the tree its literal does.
    #[test]
    fn tree_rules_read_decoded_attribute_values() {
        let h = b"<table><input type=\"hidd&#101;n\" id=x></table>";
        assert_eq!(ex(h, "table > input::attr(id)"), v(&["x"]));
        assert_eq!(ex(b"<input type=\"hidd&#101;n\"><frameset id=f>", "frameset::attr(id)"), v(&["f"]));
        assert_eq!(ex(b"<div><template shadowrootmode=\"o&#112;en\" id=t></template></div>", "template::attr(id)"), NONE);
        let h = b"<math><annotation-xml encoding=\"text&#47;html\"><b id=b>x</b></annotation-xml></math>";
        assert_eq!(ex(h, "annotation-xml > b::attr(id)"), v(&["b"]));
        let select = |size: &str| {
            let h = format!("<select size=\"{size}\"><button><selectedcontent></selectedcontent></button><option>A</option></select>");
            ex(h.as_bytes(), "selectedcontent::text")
        };
        // HTML's non-negative integer, into Chrome's 32 bits: a size above 1 shows several options
        for size in ["&#50;", "2px", " 2", "+2", "4294967295"] {
            assert_eq!(select(size), NONE, "{size}");
        }
        for size in ["", "1", "0", "-0", "-2", "0x2", "1.5", "4294967296", "\u{a0}2"] {
            assert_eq!(select(size), v(&["A"]), "{size}");
        }
    }

    /// Fostered content goes before its table: in document order, in the table's sibling positions, and
    /// into the text node before the table.
    #[test]
    fn fostered_content_comes_before_its_table() {
        // body > ["xy", div#d > "F", "z", table#t > tbody > tr > td#c > "a", "w"]
        let h = b"<!DOCTYPE html>x<table id=t><tr><td id=c>a</td></tr>y<div id=d>F</div>z</table>w";
        assert_eq!(ex(h, "body > *::attr(id)"), v(&["d", "t"]));
        assert_eq!(ex(h, "body::text"), v(&["xy", "z", "w"]));
        assert_eq!(ex(h, "#d + #t::attr(id)"), v(&["t"]));
        assert_eq!(ex(h, "#t:nth-child(2)::attr(id)"), v(&["t"]));
        assert_eq!(ex(h, "#t ~ div::attr(id)"), NONE);
        assert_eq!(ex(h, "#d:first-child::attr(id)"), v(&["d"]));
        assert_eq!(ex(h, "body ::text"), v(&["xy", "F", "z", "a", "w"]));
        assert_eq!(ex(h, "td, div"), v(&["<div id=d>F</div>", "<td id=c>a</td>"]));
    }

    /// A `<frameset>` takes the body out of the document with everything in it, while frameset-ok holds,
    /// and U+FFFD, however it is spelled, leaves frameset-ok set in Chrome.
    #[test]
    fn a_frameset_removes_the_body() {
        // html > [head, frameset#f > frame#g]
        let h = b"<a id=a><div id=d></div> <frameset id=f><frame id=g></frameset>";
        assert_eq!(ex(h, "#a, #d"), NONE);
        assert_eq!(ex(h, "html > *::attr(id)"), v(&["f"]));
        assert_eq!(ex(h, "head + frameset > frame::attr(id)"), v(&["g"]));
        for lead in ["&#0;", "&#xFFFD;", "\u{FFFD}", "&#xD800;", "&#x110000;", "<div>&#0;</div>"] {
            let h = format!("{lead}<frameset id=f>");
            assert_eq!(ex(h.as_bytes(), "html > frameset::attr(id)"), v(&["f"]), "{lead}");
        }
        // ...and so does the one the decoder makes of bytes the page's encoding does not map
        for (lead, label) in [(&b"\xff"[..], "utf-8"), (b"\x81 ", "shift_jis"), (b"\xa4", "euc-jp")] {
            let h = [lead, b"<frameset id=f>"].concat();
            let got = Plan::compile(&["html > frameset::attr(id)"], &[]).extract(&h, Some(label), None).0;
            assert_eq!(got, vec![v(&["f"])], "{label}");
        }
        // any other character ends frameset-ok, and the frameset is ignored
        assert_eq!(ex(b"&#x80;<frameset id=f>", "frameset"), NONE);
    }

    /// The adoption agency with a furthest block: the block leaves the formatting element, and what it
    /// held moves into a copy of that element inside it.
    #[test]
    fn the_adoption_agency_moves_what_was_matched() {
        // body > [b#b > "1", p#p > [b#b > ["2", i#i > "3"], i#i > "4"]]
        let h = b"<!DOCTYPE html><b id=b>1<p id=p>2<i id=i>3</b>4</p>";
        assert_eq!(ex(h, "b p::text"), NONE);
        assert_eq!(ex(h, "p > b::text"), v(&["2"]));
        assert_eq!(ex(h, "#b + #p::attr(id)"), v(&["p"]));
        assert_eq!(ex(h, "#p > b > #i::text"), v(&["3"]));
        assert_eq!(ex(h, "#p::text"), NONE);
        assert_eq!(ex(h, "b::text"), v(&["1", "2"]));
        assert_eq!(ex(h, "i::text"), v(&["3", "4"]));
        // body > [a#a > "1", div#d > [a#a > "2", a#e > "3"]]
        let a = b"<!DOCTYPE html><a id=a href=x>1<div id=d>2<a id=e href=y>3</a></div>";
        assert_eq!(ex(a, "a::attr(id)"), v(&["a", "a", "e"]));
        assert_eq!(ex(a, "#d > a::text"), v(&["2", "3"]));
        assert_eq!(ex(a, "a div::attr(id)"), NONE);
        assert_eq!(ex(a, "body > *::attr(id)"), v(&["a", "d"]));
        // a copy's outer HTML is the start tag it was copied from, then its own content's source
        assert_eq!(ex(h, "b"), v(&["<b id=b>1", "<b id=b>2<i id=i>3</b>"]));
        assert_eq!(ex(a, "a"), v(&["<a id=a href=x>1", "<a id=a href=x>2", "<a id=e href=y>3</a>"]));
    }

    /// Chrome copies the selected option's content into its select's `<selectedcontent>` elements: when
    /// the option leaves the stack, and when a `<selectedcontent>` is inserted after it.
    #[test]
    fn the_selected_option_is_copied_into_selectedcontent() {
        // select#s > [button > selectedcontent#c > "B", option#o > [b#x > "A", " t"], option#q > "B"]
        let h = b"<!DOCTYPE html><select id=s><button><selectedcontent id=c></selectedcontent></button>\
                  <option id=o><b id=x>A</b> t</option><option selected id=q>B</option></select>";
        assert_eq!(ex(h, "#c::text"), v(&["B"]));
        assert_eq!(ex(h, "[id=x]::text"), v(&["A"]));
        assert_eq!(ex(h, "option::text"), v(&[" t", "B"]));
        // the copy is a copy, ids included: select > [button > selectedcontent > [b#x > "A", " t"], option]
        let copy = b"<select><button><selectedcontent></button><option><b id=x>A</b> t</option></select>";
        assert_eq!(ex(copy, "[id=x]::text"), v(&["A", "A"]));
        assert_eq!(ex(copy, "selectedcontent::text"), v(&[" t"]));
        // inserted after the option, it takes a copy and keeps its own text: one node "AZ"
        let after = b"<select><option>A</option><button><selectedcontent>Z</selectedcontent></button></select>";
        assert_eq!(ex(after, "selectedcontent::text"), v(&["AZ"]));
        // the first option that is not disabled is the selected one; a multiple select has none
        let disabled = b"<select><button><selectedcontent></selectedcontent></button><option disabled>A</option><option>B</option></select>";
        assert_eq!(ex(disabled, "selectedcontent::text"), v(&["B"]));
        let multiple = b"<select multiple><button><selectedcontent></selectedcontent></button><option>A</option></select>";
        assert_eq!(ex(multiple, "selectedcontent::text"), NONE);
    }

    /// Early exit stops only where nothing later can change a value already accepted. Each of these
    /// accepts a first value before a retroactive rule fires that changes it.
    #[test]
    fn early_exit_waits_for_what_can_still_change() {
        let first = |html: &[u8], q: &str| {
            Plan::compile_first_only(&[q], &[], &[true]).extract(html, None, None).0.pop().unwrap()
        };
        // the paragraph leaves the <b> at </b>
        assert_eq!(first(b"<!DOCTYPE html><b><p>x<span>y</span></b>", "b p::text"), NONE);
        // the body takes the class at the later tag
        assert_eq!(first(b"<!DOCTYPE html><p>a</p><p>b</p><body class=x>", "body:not(.x) p::text"), NONE);
        // the second <div> is fostered before the table that holds the first
        let foster = b"<!DOCTYPE html><table><tr><td><div>A</div></td></tr><div>B</div></table>";
        assert_eq!(first(foster, "div::text"), v(&["B"]));
        // the frameset removes the body
        assert_eq!(first(b"<a id=x></a><div></div><frameset><frame></frameset>", "a::attr(id)"), NONE);
        // and where nothing can change it, the first value stands
        assert_eq!(first(b"<!DOCTYPE html><p>a</p><p>b</p>", "p::text"), v(&["a"]));
    }

    /// A deferred tail is read by a second pass that must open what the first pass opened, so it runs
    /// under the replay whenever the first pass did.
    #[test]
    fn a_deferred_tail_follows_the_replay() {
        // li:last-child > [b > "F", table > tbody > tr > td > "c"]
        let foster = b"<!DOCTYPE html><ul><li>a</li><li><table><tr><td>c</td></tr><b>F</b></table></li></ul>";
        assert_eq!(ex(foster, "li:last-child b::text"), v(&["F"]));
        assert_eq!(ex(foster, "li:last-child ::text"), v(&["F", "c"]));
        // div#d > [b > "1", p > [b > "2", "3"]]
        let aa = b"<!DOCTYPE html><div id=d><b>1<p>2</b>3</p></div><div><i>4</i></div>";
        assert_eq!(ex(aa, "div:has(b) ::text"), v(&["1", "2", "3"]));
        assert_eq!(ex(aa, "p:has(b) ::text"), v(&["2", "3"]));
        // body.x > ul > [li > "a", li > ["b", i > "c"]]
        let merged = b"<!DOCTYPE html><ul><li>a</li><li>b<i>c</i></li></ul><body class=x>";
        assert_eq!(ex(merged, "body.x li:last-child ::text"), v(&["b", "c"]));
        assert_eq!(ex(merged, ".x li:last-child i::text"), v(&["c"]));
        // the body is gone
        let fs = b"<div id=w><span id=a></span><span id=l></span></div><frameset><frame></frameset>";
        assert_eq!(ex(fs, "div:has(span) span::attr(id)"), NONE);
        assert_eq!(ex(fs, "span:last-child::attr(id)"), NONE);
        // select > button > selectedcontent > [b > "A", "t"]
        let sc = b"<!DOCTYPE html><select><button><selectedcontent></selectedcontent></button><option><b>A</b>t</option></select>";
        assert_eq!(ex(sc, "selectedcontent:has(b) ::text"), v(&["A", "t"]));
        assert_eq!(ex(sc, "button:has(b) b::text"), v(&["A"]));
    }

    /// At end of input the insertion mode's "anything else" still runs, so a document that ends before
    /// the body has all three frame elements, streamed and replayed. A grouped row is how an element
    /// with no attribute and no text shows: Chrome answers `[[[]]]` for every group below.
    #[test]
    fn the_frame_is_finished_at_end_of_input() {
        let rows = |html: &[u8], container: &str| {
            let g = GroupQuery { container: container.to_string(), subfields: vec![("t".to_string(), "::text".to_string())] };
            Plan::compile::<String>(&[], &[g]).extract(html, None, None).1.pop().unwrap().len()
        };
        for h in [&b""[..], b"<!DOCTYPE html>", b"<!--c-->", b"<title>T</title>", b"<title>T", b"<head></head>", b"</head>"] {
            for frame in ["html", "head", "body"] {
                assert_eq!(rows(h, frame), 1, "{frame} in {:?}", std::str::from_utf8(h));
            }
        }
        // the head is not the last child
        assert_eq!(ex(b"<head id=h></head>", "#h:last-child::attr(id)"), NONE);
        assert_eq!(ex(b"<head id=h></head>", "#h:only-child::attr(id)"), NONE);
        assert_eq!(ex(b"<head id=h></head>", "#h:first-child::attr(id)"), v(&["h"]));
        // an unterminated text element and template content close, and the head then ends
        assert_eq!(ex(b"<html id=r><title id=t>T", "#r:has(> body)::attr(id)"), v(&["r"]));
        assert_eq!(ex(b"<html id=r><title id=t>T", "head > #t::text"), v(&["T"]));
        assert_eq!(ex(b"<html id=r><template id=t>x", "#r:has(> body)::attr(id)"), v(&["r"]));
        assert_eq!(ex(b"<html id=r><template id=t>x", "#t:last-child::attr(id)"), v(&["t"]));
        assert_eq!(ex(b"<textarea id=t>x", "body > #t::text"), v(&["x"]));
        // a frameset has no body
        assert_eq!(ex(b"<html id=r><head></head><frameset id=f>", "#r:has(> body)::attr(id)"), NONE);
        assert_eq!(ex(b"<html id=r><head></head><frameset id=f>", "#r > :last-child::attr(id)"), v(&["f"]));
        // the replay (a later <html> adds an attribute a selector asks about) finishes it too
        assert_eq!(ex(b"<html id=r><html class=k><title>T</title>", ".k:has(> body)::attr(id)"), v(&["r"]));
        assert_eq!(ex(b"<html id=r><html class=k><head id=h></head>", ".k > :last-child::attr(id)"), NONE);
        assert_eq!(ex(b"<html id=r><html class=k><head id=h></head>", ".k:has(> body)::attr(class)"), v(&["k"]));
    }

    /// A comment in template content written after `</head>` is the template's: it leaves the
    /// template open and `<html>`'s whitespace held. (A debug build used to panic here.)
    #[test]
    fn a_comment_in_template_content_after_the_head_stays_there() {
        let h = b"<!DOCTYPE html><html><head></head> <template id=t><!--c--><p>x</p></template> <body><p id=p>y</p>";
        assert_eq!(ex(h, "#t"), v(&["<template id=t><!--c--><p>x</p></template>"]));
        assert_eq!(ex(h, "html::text"), v(&["  "]));
        assert_eq!(ex(h, "head > #t::attr(id)"), v(&["t"]));
        assert_eq!(ex(h, "#p::text"), v(&["y"]));
    }

    /// A comment in "after head" is a node of `<html>`, so it ends `<html>`'s whitespace node there. Head
    /// content written after the node still goes into the head, so it comes first in document order.
    #[test]
    fn a_comment_after_the_head_splits_the_html_whitespace() {
        let h = b"<!DOCTYPE html><html><head></head> <!--c--><script></script> <body>";
        assert_eq!(ex(h, "html::text"), v(&[" ", " "]));
        let h = b"<html><head></head> <!--c-->\t<style>s</style>\n<body>";
        assert_eq!(ex(h, "*::text"), v(&["s", " ", "\t\n"]));
        assert_eq!(ex(h, "html::text"), v(&[" ", "\t\n"]));
        let h = b"<html><head></head>\t<style>s</style> <!--c-->\n<style>u</style>\r<body>";
        assert_eq!(ex(h, "*::text"), v(&["s", "u", "\t ", "\n\n"]));
        // at end of input, with no body tag
        let h = b"<html><head></head>\t<!--c--> <script>s</script>";
        assert_eq!(ex(h, "*::text"), v(&["s", "\t", " "]));
    }

    /// A formatting element's copy and the copy reconstruction makes inside it begin at one offset, with
    /// the text that made them; every column orders them as the document does, outer first.
    #[test]
    fn values_at_one_offset_come_out_in_document_order() {
        // body > [p > b#b > ["1", i#i > "2"], b#b > i#i > "3"]
        let h = b"<!DOCTYPE html><p><b id=b>1<i id=i>2</p>3";
        assert_eq!(ex(h, "*:last-child::attr(id)"), v(&["b", "i", "b", "i"]));
        assert_eq!(ex(h, "[id]:nth-last-child(1)::attr(id)"), v(&["b", "i", "b", "i"]));
        // raw source: a copy's outer HTML is its original's start tag and its own content
        assert_eq!(ex(h, "b, i"), v(&["<b id=b>1<i id=i>2", "<i id=i>2", "<b id=b>3", "<i id=i>3"]));
        let c = b"<!DOCTYPE html><p><b id=b>1<i id=i>2</p>3<b id=c>";
        assert_eq!(ex(c, "b, i"), v(&["<b id=b>1<i id=i>2", "<i id=i>2", "<b id=b>3<b id=c>", "<i id=i>3<b id=c>", "<b id=c>"]));
        assert_eq!(ex(c, "b:has(i)::attr(id)"), v(&["b", "b"]));
    }

    /// The adoption agency can move all of a formatting element's content into its copy; the replay
    /// still gives the element its own start tag as its outer HTML, and the copy the content it was given
    /// (Chrome: `<a id="a" href="x"></a>`, `<a id="a" href="x">t</a>`).
    #[test]
    fn an_element_the_adoption_agency_empties_keeps_its_start_tag() {
        let a = b"<!DOCTYPE html><a id=a href=x><div id=d>t</a>t";
        assert_eq!(ex(a, "#a"), v(&["<a id=a href=x>", "<a id=a href=x>t</a>"]));
        assert_eq!(ex(a, "#a::text"), v(&["t"]));
        let b = b"<!DOCTYPE html><b id=b><div id=d></b>";
        assert_eq!(ex(b, "#b"), v(&["<b id=b>", "<b id=b></b>"]));
    }
}
