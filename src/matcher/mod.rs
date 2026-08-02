//! The corrected-stack matcher. It consumes `TokenSink` events, maintains an open-element stack
//! reshaped by HTML implied-end-tag rules (so combinators match the tree lxml *would* build), and
//! evaluates the supported selectors off that stack.
//!
//! Model: each open element carries a `matched` bitset — bit k set iff the element is a *subject
//! match* for selector k (for a sibling selector, that includes the per-parent sibling gate). Value
//! extraction reads `matched`. Sibling combinators (`+`/`~`, incl. chains) are driven by per-parent
//! `seen`/`prev` frames, updated as each child streams.
//!
//! Zero-copy hot path: elements hold **borrowed** `&'a str` tag names and only the attributes some
//! selector actually references (the "interesting" set), entity-decoded lazily into a `Cow` that
//! **borrows** the input when clean (the common case). Text is entity-decoded only when it matches a
//! selector. Assumes ≤128 member-selectors (`matched`/output columns) and ≤64 sibling trigger bits —
//! comma groups expand to one member each, so the member count can exceed the query count.

use std::borrow::Cow;

use encoding_rs::Encoding;

use frame::{
    frame_slot, frameset_is_open, head_is_current, head_is_open, nothing_open, only_html_open,
    tag_is_redundant, DocumentFrame,
};
use crate::implied_close::{
    blocks_end_tag, classify, end_tag_discardable, frame_content, start_closes,
    FrameContent,
};
use crate::selector::{
    AttrPred, Comb, Compound, Has, ReversePos, Selector, Terminal, TextPred,
};
#[cfg(test)]
use crate::selector::{TextAxis, TextOp};
use crate::tokenizer::TokenSink;
use crate::{FlatColumns, GroupRows};

mod decode;
mod compile;
mod frame;
mod deferred;
mod matching;

use compile::{
    any_has, any_reverse, any_text_pred, deferrable_has, deferrable_reverse,
    deferrable_text_pred, sibling_pred_boundary,
};
use decode::{decode_attr, finalize};
use deferred::TextMatchState;
#[cfg(test)]
use deferred::TextAccum;
use matching::{compound_matches, eval_one, gate_open, reverse_matches, seg_match, sub_hits};

/// libxml2's HTML4 minimized-boolean attribute set (`htmlIsBooleanAttr`). A valueless attribute in
/// this set has its lowercased name as its value (`<input disabled>` -> `"disabled"`); an explicitly
/// empty value (`disabled=""`) remains empty. The rule is name-only, regardless of the element.
fn is_minimized_boolean_attr(name: &[u8]) -> bool {
    [
        b"checked".as_slice(),
        b"compact",
        b"declare",
        b"defer",
        b"disabled",
        b"ismap",
        b"multiple",
        b"nohref",
        b"noresize",
        b"noshade",
        b"nowrap",
        b"readonly",
        b"selected",
    ]
    .iter()
    .any(|candidate| name.eq_ignore_ascii_case(candidate))
}

/// One `Many`/`One` group as compiled input: `(container, subfields)`. `None` = unsupported selector
/// (a `None` container never opens; a `None` subfield never matches — empty column, no fallback).
pub type GroupInput = (Option<Selector>, Vec<Option<Selector>>);

/// Fixed-width bitset budgets: `matched: u128` gives ≤128 member selectors (columns + group
/// containers), `seen`/`prev: u64` give ≤64 sibling trigger bits. These are a deliberate perf lever
/// (see docs), not a soft ceiling — an entry that would exceed either is compiled DEAD (never
/// matches; deterministic empty column) rather than overflow-shifting (debug panic / release
/// bit-aliasing) or silently aliasing another selector's column.
pub const MAX_MEMBERS: usize = 128;
pub const MAX_SIB_BITS: usize = 64;

/// The (member-selector, sibling-bit) demand a compiled schema *would* place on the bitsets, before
/// any DEAD-clamping — the raw totals. Members = flat member selectors + supported group containers
/// (each consumes one `matched` bit); sibling bits = one per adjacent/general combinator across all
/// of those (subs are evaluated on demand and cost neither). Callers can compare against
/// [`MAX_MEMBERS`]/[`MAX_SIB_BITS`] to reject an over-budget schema loudly instead of silently
/// getting empty columns (the Python binding does exactly this).
pub fn budget_usage(queries: &[Vec<Selector>], groups: &[GroupInput]) -> (usize, usize) {
    let mut members = 0usize;
    let mut sib = 0usize;
    let mut tally = |sel: &Selector| {
        members += 1;
        // one sibling bit per adjacent/general boundary == segments-1 (see `to_segments`)
        sib += sel.combs.iter().filter(|c| matches!(c, Comb::Adjacent | Comb::General)).count();
    };
    for group in queries {
        for sel in group {
            tally(sel);
        }
    }
    for (container, _subs) in groups {
        if let Some(c) = container {
            tally(c);
        }
    }
    // Output columns are addressed by BIT position (`1u128 << col`) and a column IS its query index, so
    // the query count is itself a member budget: a schema of 128 UNSUPPORTED queries (which parse to no
    // members) followed by a valid one otherwise reports 1/128 while silently dropping the valid column.
    (members.max(queries.len()), sib)
}

pub struct OpenElem<'a> {
    // `pub(super)` fields are the ones the read-only matching kernel (`matching`) reads.
    pub(super) tag: &'a [u8], // raw bytes (possibly mixed-case); matched case-insensitively
    /// Start-close class (`implied_close::sc`), the id space libxml2's `htmlStartClose` pair table
    /// actually needs: it distinguishes names a coarser one lumps together — `<td>` closes an open
    /// `<b>` but not an open `<em>`.
    scid: u8,
    attrs: Vec<(&'a [u8], Cow<'a, str>)>, // only "interesting" attrs; value entity-decoded (lazy Cow)
    matched: u128, // bit k: this element is a subject match for member-selector k (≤128 members)
    matched_tree: u128, // OR of `matched` over this element and its open ancestors
    text_cols: u128, // flat output columns that want a text event directly under this element
    pub(super) seen: u64, // sibling trigger bits (≤64) matched by any child so far (for `~`)
    pub(super) prev: u64, // sibling trigger bits matched by the immediately-preceding child (for `+`)
    pub(super) anchor: u64, // sibling anchor bits: bit seg_bits[i] set iff this element is a valid head for
                   // the segment right of boundary i (its head compound matched AND a preceding sibling
                   // matched the previous segment) — set once at open, read by descendant subjects
    start: usize,  // byte offset of this element's `<` (for raw-source outer-HTML capture)
    cap_cols: u128, // output columns wanting this element's raw source (OuterHtml terminal)
    insts: usize,  // number of group instances this element opened (popped when it closes)
    gcaps: Vec<(u64, usize)>, // grouped outer-HTML captures: (instance seq, sub) this element feeds
    // 1-based sibling positions, set at open from the parent's counters (0 = not tracked). Read by the
    // matching kernel for `:nth-child`/`:nth-of-type` and XPath `[N]`. `of_type_index` is populated only
    // for tags that a positional selector references (see `CompiledSchema::positional_tags`).
    pub(super) child_index: u32,
    pub(super) of_type_index: u32,
    // Reverse-positional (`:last-*`/`:only-*`) bookkeeping — all empty/zero unless `has_reverse`.
    rev_subj: u128, // bit r: this element is a PROVISIONAL subject for reverse-entry r (routes captures)
    rev_buf: Vec<(u32, usize, String)>, // (rev-entry, offset, value) captured while this el is a subject
    rev_pending: Vec<RevPend>, // candidates promoted from this element's children, resolved at its close
    // `:has()` bookkeeping — all empty/zero unless `has_has`. Resolved at THIS element's own close
    // (its descendants are all known then), unlike reverse (resolved at the parent's close).
    has_subj: u128, // bit h: this element is a PROVISIONAL subject for has-entry h (structural match)
    has_done: u128, // bit h: a qualifying descendant/child was found -> the `:has(h)` constraint holds
    has_buf: Vec<(u32, usize, String)>, // (has-entry, offset, value) captured while this el is a subject
    // Text-content predicate bookkeeping — empty/zero unless `has_text_pred`. Predicate evaluation is
    // streaming and bounded by the needle length; only prospective terminal values are retained.
    txt_subj: u128,
    txt_states: Vec<TextMatchState>,
    txt_emit: Vec<(u32, usize, String)>, // (entry, offset, prospective attr/direct-text value)
}

impl<'a> OpenElem<'a> {
    /// A bare element for matching-kernel tests: a tag, no attributes, no bookkeeping. Only the
    /// `matching` unit tests build elements directly; the scan always goes through `start_tag`.
    #[cfg(test)]
    pub(super) fn for_test(tag: &'a [u8]) -> Self {
        OpenElem {
            tag,
            scid: 0,
            attrs: Vec::new(),
            matched: 0,
            matched_tree: 0,
            text_cols: 0,
            seen: 0,
            prev: 0,
            anchor: 0,
            start: 0,
            cap_cols: 0,
            insts: 0,
            gcaps: Vec::new(),
            child_index: 0,
            of_type_index: 0,
            rev_subj: 0,
            rev_buf: Vec::new(),
            rev_pending: Vec::new(),
            has_subj: 0,
            has_done: 0,
            has_buf: Vec::new(),
            txt_subj: 0,
            txt_states: Vec::new(),
            txt_emit: Vec::new(),
        }
    }

    pub(super) fn attr(&self, name: &str) -> Option<&str> {
        self.attrs
            .iter()
            .find(|(n, _)| n.eq_ignore_ascii_case(name.as_bytes()))
            .map(|(_, v)| v.as_ref())
    }
    /// ASCII whitespace, NOT `split_whitespace`'s Unicode set — HTML splits a class list on space, tab,
    /// LF, FF and CR and nothing else. Splitting on the Unicode set invented classes: a crawled page
    /// whose `class="ctsListWrap fadein　clearfix"` separates two of them with an IDEOGRAPHIC SPACE
    /// (U+3000, ordinary in Japanese markup) has one token `fadein　clearfix`, and the engine matched
    /// both `.fadein` and `.clearfix` on it — elements a scraper's selector should never have seen.
    /// Same for NBSP, the em space, and U+000B, which is not ASCII whitespace in HTML.
    pub(super) fn has_class(&self, cls: &str) -> bool {
        self.attr("class").is_some_and(|v| v.split_ascii_whitespace().any(|t| t == cls))
    }
}

/// Recursively collect the attribute names a compound references (incl. inside `:not(...)`), so the
/// tokenizer/matcher materializes exactly those. `class`/`id` are attribute-backed here.
fn collect_interesting(c: &Compound, out: &mut Vec<Box<str>>) {
    // A plain fn (not a closure) so it doesn't hold a borrow of `out` across the recursion below.
    fn add(out: &mut Vec<Box<str>>, name: &str) {
        if !out.iter().any(|x| x.eq_ignore_ascii_case(name)) {
            out.push(name.to_ascii_lowercase().into_boxed_str());
        }
    }
    if !c.classes.is_empty() {
        add(out, "class");
    }
    if c.id.is_some() {
        add(out, "id");
    }
    for p in &c.attrs {
        match p {
            AttrPred::Exists(n)
            | AttrPred::Eq(n, _)
            | AttrPred::Prefix(n, _)
            | AttrPred::Suffix(n, _)
            | AttrPred::Substr(n, _)
            | AttrPred::Includes(n, _)
            | AttrPred::DashMatch(n, _) => add(out, n),
        }
    }
    for neg in &c.negations {
        collect_interesting(neg, out);
    }
    if let Some(h) = &c.has {
        collect_interesting(&h.inner, out); // the `:has(x)` inner compound's attrs must be materialized
    }
    for group in &c.is_groups {
        for alt in group {
            collect_interesting(alt, out); // `:is(...)` alternatives' attrs must be materialized too
        }
    }
}

/// Scan a selector's compounds (INCLUDING those inside `:not(...)`) for OF-TYPE positional constraints
/// (`:nth-of-type`, XPath `tag[N]`) and record each referenced concrete tag, so the matcher tracks a
/// per-parent same-tag sibling count for exactly those tags. Sets `any` if the selector uses ANY
/// position (of-type or nth-child) anywhere — that's what turns the counter machinery on.
fn collect_positional(sel: &Selector, tags: &mut Vec<Box<[u8]>>, any: &mut bool) {
    fn scan(c: &Compound, tags: &mut Vec<Box<[u8]>>, any: &mut bool) {
        // A reverse position (`:last-of-type`) needs the same per-parent same-tag count as a forward
        // one — and any position at all turns on the counter machinery, whose totals a reverse position
        // reads at the parent's close.
        let of_type_tag = match (&c.positional, &c.reverse) {
            (Some(nth), _) => {
                *any = true;
                nth.of_type
            }
            (_, Some(rev)) => {
                *any = true;
                rev.of_type
            }
            _ => false,
        };
        if of_type_tag {
            if let Some(tag) = c.tag.as_deref().filter(|t| *t != "*") {
                let lower = tag.to_ascii_lowercase().into_bytes().into_boxed_slice();
                if !tags.iter().any(|t| **t == *lower) {
                    tags.push(lower);
                }
            }
        }
        for neg in &c.negations {
            scan(neg, tags, any); // `:not(:first-child)` / `:not(li:nth-of-type(2))`
        }
    }
    for c in &sel.parts {
        scan(c, tags, any);
    }
}

/// A run of compounds joined by descendant/child combinators (a selector split at sibling `+`/`~`
/// boundaries). The matching predicates over it live in [`matching`].
struct Segment {
    pub(super) parts: Vec<Compound>,
    pub(super) combs: Vec<Comb>, // Descendant | Child only
    pub(super) strict: bool, // leftmost compound must bind strictly below `floor` (relative `.//` descendant)
}

struct CSel {
    col: usize,
    // `pub(super)` fields are read by the matching kernel (`matching::eval_one`).
    pub(super) segments: Vec<Segment>,
    adj: Vec<bool>,
    pub(super) seg_bits: Vec<usize>,
    terminal: Terminal,
    emit: bool, // false for a group's container entry: it consumes a `matched` bit (to open instances)
                // but must not push a flat value / outer-HTML capture of its own.
    pub(super) dead: bool, // over-budget (>128 members or would need a sibling bit >= 64): never matches,
                // so its column stays deterministically empty — no overflow-shift, no aliasing, no panic.
}

/// A single sub-field selector inside a `Many`/`One` group. MVP: one segment (descendant/child
/// combinators allowed, no sibling `+`/`~`), so it needs only `seg_match` — no per-parent sibling
/// frames — which sidesteps cross-instance frame contamination when same-group containers nest.
struct SubSel {
    seg: Option<Segment>, // None = unsupported (multi-segment / sibling) -> never matches (empty column)
    terminal: Terminal,
}

struct GroupSpec {
    subs: Vec<SubSel>,
}

/// A REVERSE-positional flat selector (`:last-child`/`:last-of-type`/`:only-child`/`:only-of-type`)
/// held out of the normal `entries` list because it can't be decided at open. `seg` is its single
/// subject segment (the subject compound carries `reverse`); `compound_matches` ignores the reverse
/// bit, so `seg_match(seg, …)` is the *provisional* (structural) match. Resolution happens at the
/// subject's parent close, comparing the subject's index to the parent's total (see `matcher::reverse`).
/// Scope: single segment, subject-only reverse, `::text`/`::attr` (self or subtree), a flat column.
/// Anything else → the column is empty. `single_slot` marks the O(1) cases (`:last-*`/`:only-*` =
/// `nth-last(1)`): only the last-positioned candidate can win, so the parent keeps ONE candidate
/// (overwrite) instead of buffering all. General `:nth-last-*(An+B)` buffers every matching child of a
/// parent (bounded by that parent's subtree, freed at its close).
struct RevEntry {
    col: usize,
    seg: Segment,
    terminal: Terminal, // Text{..} | Attr{..}
    rev: ReversePos,
    of_type_tag: Option<usize>, // slot in `positional_tags` for the subject tag (of-type variants)
    single_slot: bool,          // keep only the last candidate per parent (last/only), not all
    dead: bool,                 // over the 64 reverse-entry budget: never matches (empty column)
    // `Some(slot)` when the value is NOT this element's own — a subtree terminal or a value-bearing
    // descendant. Nothing streams then; the winner's raw span is re-scanned with
    // `schema.tail_schemas[slot]` at finish. See `split_deferred`.
    tail: Option<usize>,
}

/// A deferred reverse candidate awaiting its parent's close. `idx` is the subject's 1-based sibling
/// index (child or of-type, per the entry); `vals` are its captured `(offset, value)` pairs. For
/// `single_slot` entries a parent keeps one candidate (overwrite); otherwise it collects all, resolved
/// by `reverse_matches` at the close.
struct RevCand {
    idx: u32,
    vals: Vec<(usize, String)>,
    /// The subject's raw source span. Unused for an attached terminal (`vals` carries the answer); for a
    /// SUBTREE terminal it is the whole answer — two integers instead of a buffered subtree.
    span: (usize, usize),
}

struct RevPend {
    entry: u32,
    cands: Vec<RevCand>,
}

/// A `:has()` flat selector held out of `entries` (like [`RevEntry`]) because its subject can't be
/// decided at open — the `:has` constraint needs the element's descendants, known only at its own
/// close. `seg` is the single subject segment (its subject compound carries `has`; `compound_matches`
/// ignores it, so `seg_match(seg, …)` is the *provisional* structural match). `has` is the constraint;
/// resolution scans for a qualifying descendant (`rel == Descendant`) or direct child (`rel == Child`)
/// matching `has.inner`. Scope: single segment, subject-only `:has`, `::text`/`::attr` (attached) or
/// bare-element outer-HTML, a flat column. `dead` only when over the shared 128-member budget.
struct HasEntry {
    col: usize,
    seg: Segment,
    terminal: Terminal, // Text{subtree:false} | Attr{subtree:false} | OuterHtml
    has: Has,
    dead: bool,
    // When `Some(bit)`, this entry emits NO value; instead, at the subject's close (if `:has` holds) it
    // fires sibling-trigger `bit` on the parent — this is the deferred-predicate-on-a-preceding-sibling
    // case (`C:has(..) ~ S`), where a *later* sibling `S` (a normal entry anchored to `bit`) is the
    // value-bearer. `None` = the ordinary flat `:has` column (`col`/`terminal`).
    trigger: Option<usize>,
    /// As [`RevEntry::tail`]: `Some(slot)` when the value comes from this element's subtree rather than
    /// the element itself, recovered by re-scanning its span.
    tail: Option<usize>,
}

/// An XPath text-content-predicate flat selector held out of `entries` (like [`HasEntry`]): its subject
/// can't be decided at open — the predicate tests the element's text, known only at its close. `seg` is
/// the single subject segment (its subject compound carries `text_pred`, which `compound_matches`
/// ignores). At close the buffered text is evaluated per [`TextPred`]; if it holds, the subject's
/// buffered value commits. Scope mirrors `:has`: single segment, subject-only, `::text`/`::attr`
/// (attached) or outer-HTML terminal, a flat column. `dead` only when over the shared 128-member budget.
struct TextEntry {
    col: usize,
    seg: Segment,
    terminal: Terminal,
    pred: TextPred,
    dead: bool,
    // As in [`HasEntry`]: `Some(bit)` makes this a preceding-sibling trigger (`C[.="x"] ~ S`) that fires
    // sibling-trigger `bit` on the parent at the subject's close (if the predicate holds) instead of
    // emitting a value; `None` = the ordinary flat text-predicate column.
    trigger: Option<usize>,
    /// As [`RevEntry::tail`]: `Some(slot)` when the value comes from this element's subtree.
    tail: Option<usize>,
}

/// One open container instance: a scope for its group's sub-selectors. `buckets[sub]` accumulates the
/// (text/attr) values of sub-field `sub` while this instance is open; outer-HTML sub-values arrive via
/// deferred captures keyed by `seq`. `depth` is the container element's stack depth (the scope floor).
struct OpenInstance {
    group: usize,
    depth: usize,
    seq: u64,
    buckets: Vec<Vec<String>>,
}

/// Where a captured raw-source (outer-HTML) span belongs: a flat output column, or a group instance's
/// sub-field (keyed by the instance's document-order `seq`).
#[derive(Clone, Copy)]
enum Dest {
    Flat(usize),
    Grouped { seq: u64, sub: usize }, // seq is globally unique -> identifies the (group, row)
}

/// A compiled tail: the sub-schema that recovers a deferred winner's values from its raw span, paired
/// with the output column those values belong to. One per deferred entry that has a tail.
struct TailSchema {
    schema: CompiledSchema,
    col: usize,
}

/// Split a deferred selector at the compound carrying its predicate (`k`).
///
/// Returns the PREFIX segment — compounds `0..=k`, matched in context to find the deferred element — and
/// the slot of the TAIL schema that recovers the values from that element's span, if the values are not
/// the element's own. Three cases:
///   * value is the element's own (`div:has(a)::attr(id)`) — no tail, the value streams as today;
///   * value is its whole subtree (`li:last-child ::text`) — tail is `* ::text`, descendant-or-**self**
///     (not `strict_desc`), so the element's own text counts;
///   * value is a DESCENDANT's (`div:has(a) a::attr(href)`) — tail is the compounds after `k`, with
///     `strict_desc` so the span's root is excluded (a *proper* descendant).
///
/// The `bool` is "the tail is unsupported" — then the caller marks the entry DEAD, so the audit keeps
/// reporting the whole selector unsupported instead of the column silently coming back empty.
fn split_deferred(
    sel: &Selector,
    k: usize,
    col: usize,
    tail_schemas: &mut Vec<TailSchema>,
) -> (Segment, Option<usize>, bool) {
    let full = to_segments(sel).0.into_iter().next().expect("deferrable => 1 segment");
    let prefix = Segment {
        parts: full.parts[..=k].to_vec(),
        combs: full.combs[..k].to_vec(),
        strict: full.strict,
    };
    let members = match compile::tail_selector(sel, k) {
        Some(t) => Some(vec![t]),
        None => match &sel.terminal {
            Terminal::Text { subtree: true } => Some(crate::selector::parse_list("* ::text")),
            Terminal::Attr { subtree: true, name } => {
                Some(crate::selector::parse_list(&format!("* ::attr({name})")))
            }
            _ => None,
        },
    };
    let Some(members) = members else { return (prefix, None, false) };
    let schema = CompiledSchema::compile(std::slice::from_ref(&members), &[]);
    if !schema.flat_col_supported(0) {
        return (prefix, None, true);
    }
    tail_schemas.push(TailSchema { schema, col });
    let slot = tail_schemas.len() - 1;
    (prefix, Some(slot), false)
}

fn to_segments(sel: &Selector) -> (Vec<Segment>, Vec<bool>) {
    let mut segs = Vec::new();
    let mut adj = Vec::new();
    let mut parts = vec![sel.parts[0].clone()];
    let mut combs = Vec::new();
    for (k, comb) in sel.combs.iter().enumerate() {
        match comb {
            Comb::Adjacent | Comb::General => {
                segs.push(Segment { parts: std::mem::take(&mut parts), combs: std::mem::take(&mut combs), strict: false });
                adj.push(*comb == Comb::Adjacent);
                parts.push(sel.parts[k + 1].clone());
            }
            c => {
                combs.push(*c);
                parts.push(sel.parts[k + 1].clone());
            }
        }
    }
    segs.push(Segment { parts, combs, strict: false });
    // `strict_desc` constrains the selector's LEFTMOST compound (the head of the first segment), which
    // is the only place the context-node anchor applies. Relative XPath (`.//`) never has sibling
    // combinators, so there is exactly one segment — but scope the flag to segment 0 regardless.
    if sel.strict_desc {
        segs[0].strict = true;
    }
    (segs, adj)
}


/// A schema compiled ONCE, reusable across any number of pages. It holds only page-independent state —
/// the lowered selector entries, group specs, and the derived interesting-attribute set / fast-path
/// flags — so a caller that runs the same selectors over many responses (the `Page`/`FrostPage` usage
/// model) pays the parse + lowering cost a single time. A [`Matcher`] borrows it per page and adds the
/// mutable scan state. See [`crate::Plan`] for the string-level, encoding-resolving wrapper.
pub struct CompiledSchema {
    entries: Vec<CSel>,
    interesting: Vec<Box<str>>, // attr names any selector references (lowercased)
    has_outer: bool,            // any OuterHtml selector (flat OR grouped)? (skips capture bookkeeping otherwise)
    has_sibling: bool,          // any multi-segment (sibling `+`/`~`) entry? (skips the per-element anchor pass otherwise)
    groups: Vec<GroupSpec>,     // Many/One sub-field schemas
    container_entries: Vec<(usize, usize)>, // (entry index, group): entries whose match opens an instance
    n_flat_cols: usize,         // number of flat output columns (== query count) — sizes `results`
    n_groups: usize,            // number of groups — sizes `group_rows`
    has_ns: bool,               // any `normalize-space(...)` entry? (skips the ns capture passes otherwise)
    has_ns_element: bool,       // any ns entry over an element string-value? (skips per-open/close ns scan)
    has_positional: bool,       // any `:nth-*`/`[N]` positional? (skips the per-open sibling counting)
    positional_tags: Vec<Box<[u8]>>, // tags whose OF-TYPE sibling index a positional selector needs
    has_reverse: bool,          // any reverse entry? (skips all deferred-reverse work otherwise)
    reverse_entries: Vec<RevEntry>, // deferred reverse-positional flat selectors (held out of `entries`)
    has_has: bool,              // any `:has()` entry? (skips all deferred-has work otherwise)
    has_entries: Vec<HasEntry>, // deferred `:has()` flat selectors (held out of `entries`)
    has_text_pred: bool,        // any text-content-predicate entry? (skips the deferred-text work otherwise)
    text_entries: Vec<TextEntry>, // deferred text-content-predicate flat selectors (held out of `entries`)
    // TAIL schemas for deferred entries whose value is NOT the deferred element's own: a subtree terminal
    // (`li:last-child ::text`) or a value-bearing DESCENDANT (`div:has(a) a::attr(href)`). Nothing is
    // buffered for these during the pass — the winner's raw span is re-scanned with its tail schema at
    // finish (`Matcher::resolve_tail_spans`). Entries hold a slot index; a tail is itself deferral-free,
    // so this nests exactly one level.
    tail_schemas: Vec<TailSchema>,
    // `!` of the Case-B deferred-boundary bitset: `eval`'s open-time trigger set is ANDed with this so a
    // deferred (preceding-sibling-predicate) boundary fires only at its `C`'s close, not at open. All
    // ones when no Case-B selector is compiled — the hot sibling path is then unchanged.
    trig_immediate_mask: u64,
}

/// Per-page scan state, borrowing a compiled [`CompiledSchema`] and the document bytes. One is built,
/// driven through the tokenizer, and consumed per page; the schema behind it is unchanged and reused.
pub struct Matcher<'a> {
    schema: &'a CompiledSchema,
    results: Vec<Vec<String>>,
    stack: Vec<OpenElem<'a>>,
    input: &'a [u8],            // for slicing raw-source outer-HTML fragments
    captures: Vec<(usize, usize, Dest)>, // (start, end, dest) raw-source spans, sorted at finish
    enc: &'static Encoding,     // resolved encoding for decoding emitted values
    open_instances: Vec<OpenInstance>,      // stack of currently-open container instances (LIFO with elements)
    group_rows: Vec<Vec<(u64, Vec<Vec<String>>)>>, // per group: finished rows (seq, sub-columns), sorted at finish
    next_seq: u64,              // monotonic document-order rank for instances
    ns: Vec<NsState>,           // per-entry `normalize-space` capture state (empty unless `has_ns`)
    // Per-open-container sibling counters (empty unless `has_positional`). A stack of frames, one per
    // OPEN element plus a leading document frame; each frame is `[n_child_elems, count(tag0), …]` over
    // `positional_tags`. Frame index = the child's own stack depth (`top`); frame 0 = the document.
    pos: Vec<u32>,
    // Deferred reverse-positional output: resolved `(col, offset, value)` triples, sorted by offset per
    // column at finish (a reverse value can be committed OUT of document order — e.g. a nested last-child
    // resolves before an outer one). Empty unless `has_reverse`.
    pending: Vec<(usize, usize, String)>,
    // Reverse candidates whose subject is a TOP-LEVEL element (parent = the document); resolved at finish
    // against the document frame's totals, since there is no parent `OpenElem` to hang them on.
    doc_rev_pending: Vec<RevPend>,
    // Count of currently-open text-content-predicate subjects, so `text_event`'s ancestor walk runs only
    // while at least one subject is open (0 elsewhere, even when the schema has text-pred entries).
    txt_open: usize,
    // `(tail slot, span_start, span_end)` for each deferred winner whose value lives in its subtree.
    // Two integers per winner is the whole retention cost — values are recovered by re-scanning at finish.
    tail_spans: Vec<(usize, usize, usize)>,
    // The text run not yet delivered to the consumers, held back by one event so that a run split by
    // DROPPED end tags can be re-joined first (libxml2 makes it ONE text node; see `text`). Flushed by
    // the next start tag / matched end tag / EOF, so ordering vs. tag events is unchanged.
    pending_text: Option<PendingText<'a>>,
    // Everything the document-frame rules know that the stack cannot tell them, plus the questions they
    // ask about it — see `frame.rs`, which explains why this is one owner of NAMED questions rather than
    // the insertion-mode enum it looks like it should be.
    frame: DocumentFrame,
}

/// The offset of the first byte that is not HTML whitespace, if any — where character data becomes
/// CONTENT for the document-frame rules.
fn first_non_ws(text: &[u8]) -> Option<usize> {
    text.iter().position(|c| !matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0c))
}

/// A buffered text node. In the common case it is ONE borrowed run (`joined` empty, no allocation).
///
/// When dropped end tags join several runs into one node, each run is decoded on its own and the
/// STRINGS are concatenated into `joined` — never the raw bytes. The oracle decodes entities and
/// characters while tokenizing, i.e. before tree construction, so a construct split across a discarded
/// tag must NOT reassemble: `<div>A&am</p>p;B</div>` is `A&amp;B` in lxml (the unterminated `&am` stays
/// literal), and the bytes `C3 </p> A9` are two replacement characters, not `é`. Joining bytes first
/// manufactured both.
/// One text node handed to the consumers: either a raw run still to be decoded (the common case, so
/// unselected text costs nothing) or a value already decoded run-by-run because dropped end tags joined
/// it. `finalize` is called only where a consumer actually wants the value.
#[derive(Clone, Copy)]
enum TextVal<'t> {
    Raw { bytes: &'t [u8], entities: bool },
    Decoded(&'t str),
}

impl TextVal<'_> {
    fn finalize(&self, enc: &'static Encoding) -> String {
        match *self {
            TextVal::Raw { bytes, entities } => finalize(bytes, entities, enc),
            TextVal::Decoded(s) => s.to_string(),
        }
    }
}

struct PendingText<'a> {
    bytes: std::borrow::Cow<'a, [u8]>,
    /// Decoded text of runs 1..n once a join has happened; empty while the node is a single run.
    joined: String,
    allows_entities: bool,
    /// Document offset of the run's first byte — ranks the value in document order.
    start: usize,
    /// End of the buffered node's GAP: the run's own end, advanced over each dropped end tag that abuts
    /// it. A following run joins iff it starts exactly here, which is what makes the join "nothing but
    /// dropped tags in between" rather than "exactly one dropped tag" — `<div>A</p></p>B</div>` is one
    /// text node in libxml2, and tracking a single dropped tag would still split it. Anything that is a
    /// real node in libxml2 (comment, CDATA, PI, element) leaves a byte gap and so breaks the join.
    gap_end: usize,
}

/// Per-entry capture state for a `normalize-space(...)` terminal. Only the FIRST matched node's
/// string-value is kept; `value` is finalized (normalized) and holds until finish, where it becomes the
/// entry's single output column (empty string if never set). For an element string-value, `pending`
/// accumulates the first matched element's subtree text between its open and close.
#[derive(Clone, Default)]
struct NsState {
    pending: Option<(usize, String)>, // (element depth, accumulated subtree text) — Element mode only
    value: Option<String>,            // finalized normalized string of the first matched node
}

// NOTE: a subject-tag dispatch index was prototyped here and MEASURED NEUTRAL (bench_matrix.py):
// real scraping selectors are heavily class-led (no subject tag), so they can't be tag-bucketed, and
// the per-element HashMap lookup cancels the savings on tag-led selectors over common tags. Removed —
// don't re-add without a class-aware index and a workload that shows a win.

impl CompiledSchema {
    /// Compile a schema ONCE for reuse across pages. `queries[i]` is the member selectors of query `i`
    /// (1 for a plain selector, N for a comma group — all sharing output column `i`); an empty vec
    /// means query `i` is unsupported. Each group's `(container, subs)` is a `Many`/`One`: every
    /// element matching `container` opens an instance whose single-segment `subs` are matched **scoped**
    /// to it. `subs` share the flat 128-member budget only through their container (subs are evaluated
    /// on demand, not as members), so grouping is cheap on the bit budget.
    pub fn compile(queries: &[Vec<Selector>], groups: &[GroupInput]) -> CompiledSchema {
        let mut entries = Vec::new();
        let mut n_sib = 0usize;
        let mut interesting: Vec<Box<str>> = Vec::new();
        let mut has_outer = false;
        // note the attr name / outer-html use of one selector's terminal
        let note_terminal = |t: &Terminal, interesting: &mut Vec<Box<str>>, has_outer: &mut bool| {
            match t {
                Terminal::Attr { name, .. } => {
                    if !interesting.iter().any(|x| x.eq_ignore_ascii_case(name)) {
                        interesting.push(name.to_ascii_lowercase().into_boxed_str());
                    }
                }
                Terminal::OuterHtml => *has_outer = true,
                Terminal::Text { .. } => {}
                // `normalize-space(inner)`: collect the inner attr name if any; a ns-element string
                // value is subtree text, NOT a raw-source capture, so it never sets `has_outer`.
                Terminal::NormalizeSpace(inner) => {
                    if let Terminal::Attr { name, .. } = &**inner {
                        if !interesting.iter().any(|x| x.eq_ignore_ascii_case(name)) {
                            interesting.push(name.to_ascii_lowercase().into_boxed_str());
                        }
                    }
                }
            }
        };
        let push_entry =
            |sel: &Selector, col: usize, emit: bool, entries: &mut Vec<CSel>, n_sib: &mut usize| {
                let (segments, adj) = to_segments(sel);
                let n_seg_bits = segments.len().saturating_sub(1);
                // DEAD if this entry would exceed either budget: index >= 128 can't fit `matched`
                // (u128), and a sibling bit >= 64 would overflow-shift `seen`/`prev` (u64) — panic in
                // debug, silently alias bit `n & 63` in release (cross-selector contamination -> wrong
                // values). A dead entry keeps its column but never matches: deterministic empty.
                // `col >= MAX_MEMBERS` matters as much as the member count: `text_cols`/`cap` address
                // output columns by BIT (`1u128 << cs.col`), so a query past column 127 could never be
                // delivered. It used to compile live and return empty while the audit called it
                // supported — the one outcome the no-fallback contract rules out.
                // `col >= MAX_MEMBERS` matters as much as the member count: `text_cols`/`cap` address
                // output columns by BIT (`1u128 << cs.col`), so a query past column 127 could never be
                // delivered — it used to compile live and return empty while the audit called it
                // supported, the one outcome the no-fallback contract rules out. `emit == false` entries
                // (group containers, `col == usize::MAX`) address no output column and are exempt.
                let dead = entries.len() >= MAX_MEMBERS
                    || (emit && col >= MAX_MEMBERS)
                    || *n_sib + n_seg_bits > MAX_SIB_BITS;
                let seg_bits: Vec<usize> = if dead {
                    Vec::new()
                } else {
                    (0..n_seg_bits)
                        .map(|_| {
                            let b = *n_sib;
                            *n_sib += 1;
                            b
                        })
                        .collect()
                };
                entries.push(CSel { col, segments, adj, seg_bits, terminal: sel.terminal.clone(), emit, dead });
            };

        // Reverse-positional queries are deferred (resolved at a parent's close), so they are held OUT of
        // `entries` and built into `reverse_entries` below (after `positional_tags` is known — an of-type
        // reverse needs its subject tag's slot). `(col, member)` collected here.
        let mut rev_pending_sels: Vec<(usize, &Selector)> = Vec::new();
        let mut has_pending_sels: Vec<(usize, &Selector)> = Vec::new();
        let mut text_pending_sels: Vec<(usize, &Selector)> = Vec::new();
        // Case-B preceding-sibling triggers: `(boundary bit, selector, k)` where `k` is the index of the
        // sibling combinator whose LEFT compound (`parts[k]`) carries the deferred predicate.
        let mut sibling_pending: Vec<(usize, &Selector, usize)> = Vec::new();
        for (i, members) in queries.iter().enumerate() {
            // A reverse query must be a single member in the deferrable shape (see `deferrable_reverse`).
            // A subject `reverse` in ANY other shape (multi-member, subtree/outer terminal, sibling
            // combinator, reverse-in-`:not`) is UNSUPPORTED — skip the whole query so its column stays
            // empty. Routing it to normal matching would over-match (the reverse bit is ignored there).
            if members.len() == 1 && deferrable_reverse(&members[0]) {
                for c in &members[0].parts {
                    collect_interesting(c, &mut interesting);
                }
                note_terminal(&members[0].terminal, &mut interesting, &mut has_outer);
                rev_pending_sels.push((i, &members[0]));
                continue;
            }
            if members.iter().any(any_reverse) {
                continue; // unsupported reverse form -> empty column (no fallback, never wrong)
            }
            // Same routing for `:has()`: a single deferrable member -> the deferred has path; any other
            // shape carrying a `:has` -> unsupported (empty), never normal matching (which ignores `has`).
            if members.len() == 1 && deferrable_has(&members[0]) {
                for c in &members[0].parts {
                    collect_interesting(c, &mut interesting);
                }
                note_terminal(&members[0].terminal, &mut interesting, &mut has_outer);
                has_pending_sels.push((i, &members[0]));
                continue;
            }
            // Same routing for text-content predicates on the subject.
            if members.len() == 1 && deferrable_text_pred(&members[0]) {
                for c in &members[0].parts {
                    collect_interesting(c, &mut interesting);
                }
                note_terminal(&members[0].terminal, &mut interesting, &mut has_outer);
                text_pending_sels.push((i, &members[0]));
                continue;
            }
            // Case B — a deferred predicate on a PRECEDING-SIBLING compound (`C[.="x"] ~ S`,
            // `C:has(..) + S`). `C` closes before `S` opens, so the value subject `S` is a NORMAL entry
            // (anchored to the sibling boundary, emits normally) and `C`'s predicate fires that boundary
            // at `C`'s close (a trigger entry, built below). Push the value entry now and capture its
            // boundary bit; the trigger fires it pred-gated instead of at open (see `deferred_boundary_mask`).
            if members.len() == 1 {
                if let Some(k) = sibling_pred_boundary(&members[0]) {
                    let sel = &members[0];
                    for c in &sel.parts {
                        collect_interesting(c, &mut interesting);
                    }
                    note_terminal(&sel.terminal, &mut interesting, &mut has_outer);
                    push_entry(sel, i, true, &mut entries, &mut n_sib);
                    // 2 segments -> exactly 1 seg bit; `None` iff the entry is DEAD (over budget), in
                    // which case it never matches anyway so no trigger is needed.
                    if let Some(&bit) = entries.last().and_then(|e| e.seg_bits.first()) {
                        sibling_pending.push((bit, sel, k));
                    }
                    continue;
                }
            }
            if members.iter().any(|s| any_has(s) || any_text_pred(s)) {
                continue; // unsupported `:has` / text-pred form -> empty column
            }
            for sel in members {
                for c in &sel.parts {
                    collect_interesting(c, &mut interesting);
                }
                note_terminal(&sel.terminal, &mut interesting, &mut has_outer);
                push_entry(sel, i, true, &mut entries, &mut n_sib);
            }
        }

        // groups: the container is an entry (consumes a matched bit; emit=false); sub-selectors are
        // stored per group and evaluated on demand against each open instance's scope floor.
        let mut group_specs = Vec::with_capacity(groups.len());
        let mut container_entries = Vec::new();
        for (g, (container, subs)) in groups.iter().enumerate() {
            // An unsupported container compiles to `None`: the group simply never opens (0 rows).
            // Reverse positions inside a group (container or sub) are out of the prototype's scope —
            // treat them as unsupported (container `None` -> never opens; sub `None` -> empty column).
            if let Some(container) =
                container.as_ref().filter(|c| !any_reverse(c) && !any_has(c) && !any_text_pred(c))
            {
                for c in &container.parts {
                    collect_interesting(c, &mut interesting);
                }
                container_entries.push((entries.len(), g));
                push_entry(container, usize::MAX, false, &mut entries, &mut n_sib);
            }
            let mut subsels = Vec::with_capacity(subs.len());
            for sub in subs {
                // MVP: sub-selectors are single-segment (no sibling `+`/`~`). Unsupported or
                // multi-segment subs -> `seg = None`, which never matches (empty column, no fallback).
                let (seg, terminal) = match sub {
                    Some(s) if !any_reverse(s) && !any_has(s) && !any_text_pred(s) => {
                        for c in &s.parts {
                            collect_interesting(c, &mut interesting);
                        }
                        note_terminal(&s.terminal, &mut interesting, &mut has_outer);
                        let (segments, _adj) = to_segments(s);
                        let seg = if segments.len() == 1 { segments.into_iter().next() } else { None };
                        (seg, s.terminal.clone())
                    }
                    _ => (None, Terminal::Text { subtree: false }),
                };
                subsels.push(SubSel { seg, terminal });
            }
            group_specs.push(GroupSpec { subs: subsels });
        }

        // Positional (`:nth-*` / XPath `[N]`): note whether any selector uses a position, and which
        // concrete tags need an of-type sibling count. Scan the source selectors (flat + containers +
        // subs) — the compounds carry the parsed `positional`.
        let mut has_positional = false;
        let mut positional_tags: Vec<Box<[u8]>> = Vec::new();
        for members in queries {
            for sel in members {
                collect_positional(sel, &mut positional_tags, &mut has_positional);
            }
        }
        for (container, subs) in groups {
            if let Some(c) = container {
                collect_positional(c, &mut positional_tags, &mut has_positional);
            }
            for sub in subs.iter().flatten() {
                collect_positional(sub, &mut positional_tags, &mut has_positional);
            }
        }

        // Build the deferred reverse entries now that `positional_tags` is final (an of-type reverse
        // reads its subject tag's per-parent count via that slot). Deferred subject masks are `u128`,
        // sharing the advertised 128-member ceiling instead of imposing a hidden 64-entry limit.
        let mut tail_schemas: Vec<TailSchema> = Vec::new();
        // ONE shared member allocation across every tier. `entries` (normal), reverse, `:has` and
        // text-predicate each used their OWN counter, so a 129-member schema of 100 normal members plus
        // 29 reverse selectors left all tiers live even though `budget_usage` reported it over budget and
        // the Python layer rejected it — contradicting COMPATIBILITY.md's promise that an over-budget
        // Rust entry "compiles dead". `entries.len()` is already committed by the time the deferred tiers
        // are built, so seeding from it makes the order explicit: normal members first, then deferred.
        let mut members_used = entries.len();
        let mut take_member = |dead_already: bool| {
            let over = members_used >= MAX_MEMBERS;
            members_used += 1;
            dead_already || over
        };
        let mut reverse_entries: Vec<RevEntry> = Vec::new();
        for (col, sel) in rev_pending_sels {
            let k = compile::deferrable_reverse_at(sel).expect("routed as deferrable_reverse");
            let (seg, tail, tail_dead) = split_deferred(sel, k, col, &mut tail_schemas);
            // the reverse position belongs to compound `k`, which need NOT be the subject
            let anchor = &sel.parts[k];
            let rev = anchor.reverse.expect("deferrable_reverse => compound k has reverse");
            let of_type_tag = if rev.of_type {
                let tag = anchor.tag.as_deref().unwrap_or("").to_ascii_lowercase();
                positional_tags.iter().position(|t| **t == *tag.as_bytes())
            } else {
                None
            };
            // `:last-*`/`:only-*` (== nth-last(1)) keep a single candidate; general `:nth-last-*(An+B)`
            // must buffer every matching child (any of them could be the nth from the end).
            let single_slot = rev.only || (rev.a == 0 && rev.b == 1);
            let dead = take_member(tail_dead);
            reverse_entries.push(RevEntry {
                col,
                seg,
                terminal: sel.terminal.clone(),
                rev,
                of_type_tag,
                single_slot,
                dead,
                tail,
            });
        }
        let has_reverse = !reverse_entries.is_empty();

        // `:has()` entries: the subject segment (structural, `:has` ignored by `compound_matches`) plus
        // the constraint. `has_subj`/`has_done` are `u128`, under the shared member ceiling.
        let mut has_entries: Vec<HasEntry> = Vec::new();
        for (col, sel) in has_pending_sels {
            let k = compile::deferrable_has_at(sel).expect("routed as deferrable_has");
            let (seg, tail, tail_dead) = split_deferred(sel, k, col, &mut tail_schemas);
            let has = sel.parts[k].has.clone().expect("deferrable_has => compound k has `:has`");
            let dead = take_member(tail_dead);
            has_entries.push(HasEntry {
                col,
                seg,
                terminal: sel.terminal.clone(),
                has,
                dead,
                trigger: None,
                tail,
            });
        }

        // Text-content-predicate entries: the subject segment (structural, the predicate ignored by
        // `compound_matches`) plus the predicate. `txt_subj` is `u128`, under the shared ceiling.
        let mut text_entries: Vec<TextEntry> = Vec::new();
        for (col, sel) in text_pending_sels {
            let k = compile::deferrable_text_pred_at(sel).expect("routed as deferrable_text_pred");
            let (seg, tail, tail_dead) = split_deferred(sel, k, col, &mut tail_schemas);
            let pred =
                sel.parts[k].text_pred.clone().expect("deferrable_text_pred => compound k has text_pred");
            let dead = take_member(tail_dead);
            text_entries.push(TextEntry {
                col,
                seg,
                terminal: sel.terminal.clone(),
                pred,
                dead,
                trigger: None,
                tail,
            });
        }

        // Case-B preceding-sibling TRIGGER entries: each carries `C`'s deferred predicate and, at `C`'s
        // close, fires its sibling boundary bit on the parent (instead of emitting) so the later value
        // subject `S` matches. Reuse the same has/text accumulation machinery — only the resolution
        // differs (see `close_elem`). `deferred_boundary_mask` holds these bits so `eval` does NOT fire
        // them at open (they are pred-gated at close).
        let mut deferred_boundary_mask = 0u64;
        for (bit, sel, k) in sibling_pending {
            deferred_boundary_mask |= 1u64 << bit;
            // `segments[0]` is the C-part (the first segment, ending at `parts[k]` = the pred-bearer).
            let seg = to_segments(sel).0.into_iter().next().expect("case B has >= 2 segments");
            let c = &sel.parts[k];
            if let Some(has) = c.has.clone() {
                let dead = has_entries.len() >= MAX_MEMBERS;
                has_entries.push(HasEntry {
                    col: usize::MAX,
                    seg,
                    terminal: Terminal::Text { subtree: false }, // unused (trigger emits no value)
                    has,
                    dead,
                    trigger: Some(bit),
                    tail: None, // a trigger emits no value
                });
            } else if let Some(pred) = c.text_pred.clone() {
                let dead = text_entries.len() >= MAX_MEMBERS;
                text_entries.push(TextEntry {
                    col: usize::MAX,
                    seg,
                    terminal: Terminal::Text { subtree: false },
                    pred,
                    dead,
                    trigger: Some(bit),
                    tail: None, // a trigger emits no value
                });
            }
        }
        let has_has = !has_entries.is_empty();
        let has_text_pred = !text_entries.is_empty();
        // `eval`'s open-time trigger set is masked by this so deferred (Case-B) boundaries fire only at
        // `C`'s close; all-ones when no Case-B selector exists (zero behaviour change on the hot path).
        let trig_immediate_mask = !deferred_boundary_mask;

        let has_sibling = entries.iter().any(|cs| cs.segments.len() > 1);
        let has_ns = entries.iter().any(|cs| matches!(cs.terminal, Terminal::NormalizeSpace(_)));
        let has_ns_element = entries
            .iter()
            .any(|cs| matches!(&cs.terminal, Terminal::NormalizeSpace(inner) if matches!(**inner, Terminal::OuterHtml)));
        CompiledSchema {
            n_flat_cols: queries.len(),
            n_groups: group_specs.len(),
            entries,
            interesting,
            has_outer,
            has_sibling,
            groups: group_specs,
            container_entries,
            has_ns,
            has_ns_element,
            has_positional,
            positional_tags,
            has_reverse,
            reverse_entries,
            has_has,
            has_entries,
            has_text_pred,
            text_entries,
            tail_schemas,
            trig_immediate_mask,
        }
    }

    /// Run this compiled schema over one page's `bytes` (already encoding-resolved / transcoded), with
    /// `enc` the encoding used to decode emitted values. One streaming pass; the schema is untouched
    /// and reusable. Returns `(flat_columns, grouped)` — see [`Matcher::finish_grouped`].
    pub fn run(&self, bytes: &[u8], enc: &'static Encoding) -> (FlatColumns, Vec<GroupRows>) {
        let mut m = Matcher::new(self, bytes, enc);
        crate::tokenizer::tokenize(bytes, &mut m);
        m.finish_grouped()
    }

    /// Does flat column `col` have a LIVE (matchable) entry — a normal member or a deferred reverse
    /// member that survived compile-time routing? The audit uses this so its supported/unsupported
    /// verdict is EXACTLY what the matcher does: a reverse form the compiler drops to empty (subtree
    /// terminal, comma group, reverse-on-ancestor) reads as unsupported, not "parsed, therefore fine".
    pub(crate) fn flat_col_supported(&self, col: usize) -> bool {
        self.entries.iter().any(|cs| cs.emit && !cs.dead && cs.col == col)
            || self.reverse_entries.iter().any(|re| !re.dead && re.col == col)
            || self.has_entries.iter().any(|he| !he.dead && he.col == col)
            || self.text_entries.iter().any(|te| !te.dead && te.col == col)
    }

    /// Did compilation route this flat column at all, independent of whole-schema bitset budget?
    /// Audit reports budget separately, so syntactically supported entries remain `Supported` even
    /// when a caller has made the overall schema too large.
    pub(crate) fn flat_col_routed(&self, col: usize) -> bool {
        self.entries.iter().any(|cs| cs.emit && cs.col == col)
            || self.reverse_entries.iter().any(|re| re.col == col)
            || self.has_entries.iter().any(|he| he.col == col)
            || self.text_entries.iter().any(|te| te.col == col)
    }

    /// Did group `group` receive a container route? Deferred predicates/reverse constraints are
    /// deliberately rejected for grouped containers, so absence here is authoritative.
    pub(crate) fn group_container_routed(&self, group: usize) -> bool {
        self.container_entries.iter().any(|&(_, g)| g == group)
    }

    /// Did grouped sub-field `(group, sub)` compile to the on-demand single-segment matcher?
    pub(crate) fn group_sub_routed(&self, group: usize, sub: usize) -> bool {
        self.groups
            .get(group)
            .and_then(|g| g.subs.get(sub))
            .is_some_and(|s| s.seg.is_some())
    }
}

impl<'a> Matcher<'a> {
    /// Per-page scan state over a compiled `schema` and the document `input` (borrowed for raw-source
    /// outer-HTML spans); `enc` decodes emitted values. Prefer [`CompiledSchema::run`].
    pub fn new(schema: &'a CompiledSchema, input: &'a [u8], enc: &'static Encoding) -> Matcher<'a> {
        Matcher {
            results: vec![Vec::new(); schema.n_flat_cols],
            group_rows: vec![Vec::new(); schema.n_groups],
            ns: if schema.has_ns { vec![NsState::default(); schema.entries.len()] } else { Vec::new() },
            // start with the document frame (all elements are its children); frame stride = 1 + tags
            pos: if schema.has_positional { vec![0u32; 1 + schema.positional_tags.len()] } else { Vec::new() },
            schema,
            stack: Vec::new(),
            input,
            captures: Vec::new(),
            enc,
            open_instances: Vec::new(),
            next_seq: 0,
            pending: Vec::new(),
            doc_rev_pending: Vec::new(),
            txt_open: 0,
            tail_spans: Vec::new(),
            pending_text: None,
            frame: DocumentFrame::default(),
        }
    }

    /// Recover the values of every deferred winner whose value is NOT its own element's, by re-scanning
    /// that element's raw span. Shared by all three deferred tiers (reverse, `:has`, text-predicate) —
    /// they differ only in WHEN a winner is known, not in how its subtree is read.
    ///
    /// Nothing was buffered for these during the pass — only `(start, end)`. That is sound because the
    /// span is SELF-CONTAINED: an end tag inside it that matched an ancestor would have ended the span,
    /// and one discarded by table scope behaves identically standalone. It is the same re-parse
    /// equivalence the differential already proves for outer-HTML node queries.
    ///
    /// Two details matter. The re-scan runs the REAL engine (`schema.tail_schemas[slot]`), so it inherits
    /// dropped-end-tag coalescing, table scope and implied close rather than re-deriving them — a
    /// hand-rolled collector here would silently re-introduce the split-text bug. And winners NEST (a
    /// last-child inside a last-child, a `:has` div inside a `:has` div), so a contained span's values are
    /// a subset of its container's and would double-count: element spans only nest or are disjoint, so
    /// keeping the MAXIMAL ones de-duplicates exactly — which also bounds the work to one extra pass.
    fn resolve_tail_spans(&mut self) {
        let mut spans = std::mem::take(&mut self.tail_spans);
        // Sorting by `(slot, start, end)` groups each entry's winners in document order. Within a slot
        // the starts are DISTINCT (one per element), so a container always sorts before what it contains
        // — which means "contained in some kept winner" is just "ends at or before the furthest end kept
        // so far". A running max is therefore enough; scanning all spans per span made this quadratic in
        // winner count, and disjoint winners (a page of sibling cards — the normal shape) never
        // short-circuit, so `div:has(a) ::text` over thousands of cards paid it in full.
        spans.sort_unstable();
        let (mut cur_slot, mut max_end) = (usize::MAX, 0usize);
        for (slot, s, e) in spans {
            if slot != cur_slot {
                (cur_slot, max_end) = (slot, 0);
            }
            if e <= max_end {
                continue; // contained in an earlier winner of this slot (or an exact duplicate)
            }
            max_end = e;
            let (cols, _) = self.schema.tail_schemas[slot].schema.run(&self.input[s..e], self.enc);
            let vals = cols.into_iter().next().expect("a tail schema has exactly one column");
            self.results[self.schema.tail_schemas[slot].col].extend(vals);
        }
    }

    /// Finalize a just-popped element `e` whose raw source runs `[e.start, end)`: record its flat and
    /// grouped outer-HTML captures, and pop any group instances it opened into their group's rows
    /// (an empty container still yields a row). Called at every close site (implied-close / end tag /
    /// self-close / EOF), so instance lifetime tracks element lifetime with no depth scan.
    fn close_elem(&mut self, e: OpenElem, end: usize) {
        let mut cols = e.cap_cols;
        while cols != 0 {
            let col = cols.trailing_zeros() as usize;
            cols &= cols - 1;
            self.captures.push((e.start, end, Dest::Flat(col)));
        }
        for &(seq, sub) in &e.gcaps {
            self.captures.push((e.start, end, Dest::Grouped { seq, sub }));
        }
        for _ in 0..e.insts {
            let oi = self.open_instances.pop().expect("instance stack tracks element stack");
            self.group_rows[oi.group].push((oi.seq, oi.buckets));
        }
        // RESOLVE reverse candidates that were waiting for THIS element's close: `e` is their parent, so
        // its children's totals are now known (its child counter frame is the last `stride` block of
        // `self.pos`, still present until the truncate below). Commit each candidate whose from-end
        // position satisfies the entry (see `reverse_matches`) to the deferred `pending` output.
        if self.schema.has_reverse && !e.rev_pending.is_empty() {
            let stride = 1 + self.schema.positional_tags.len();
            let base = self.pos.len() - stride;
            let total_children = self.pos[base];
            for pend in &e.rev_pending {
                let re = &self.schema.reverse_entries[pend.entry as usize];
                let total = match re.of_type_tag {
                    Some(j) => self.pos[base + 1 + j],
                    None => total_children,
                };
                for cand in &pend.cands {
                    if reverse_matches(&re.rev, cand.idx, total) {
                        if let Some(slot) = re.tail {
                            // value lives in the winner's subtree: remember the span; re-scanned at
                            // finish, once nested winners can be de-duplicated against each other
                            self.tail_spans.push((slot, cand.span.0, cand.span.1));
                        } else {
                            for (off, v) in &cand.vals {
                                self.pending.push((re.col, *off, v.clone()));
                            }
                        }
                    }
                }
            }
        }
        // drop this element's sibling-counter frame (its children's counts die with it). Balanced with
        // the push in `positional_open`; the leading document frame is never truncated.
        if self.schema.has_positional {
            let stride = 1 + self.schema.positional_tags.len();
            self.pos.truncate(self.pos.len() - stride);
        }
        // PROMOTE this element (if a provisional reverse subject) to its parent's pending list — the
        // parent is the element whose close will resolve it (or the document, for a top-level subject).
        // `single_slot` entries (`:last-*`/`:only-*`) keep only the last-positioned candidate (overwrite,
        // O(1)); general `:nth-last-*` collects every matching child, since any could be the nth from end.
        if self.schema.has_reverse && e.rev_subj != 0 {
            let mut s = e.rev_subj;
            while s != 0 {
                let r = s.trailing_zeros();
                s &= s - 1;
                let re = &self.schema.reverse_entries[r as usize];
                let (of_type, single_slot) = (re.rev.of_type, re.single_slot);
                let idx = if of_type { e.of_type_index } else { e.child_index };
                let vals: Vec<(usize, String)> = e
                    .rev_buf
                    .iter()
                    .filter(|(er, _, _)| *er == r)
                    .map(|(_, o, v)| (*o, v.clone()))
                    .collect();
                let cand = RevCand { idx, vals, span: (e.start, end) };
                let target = match self.stack.last_mut() {
                    Some(parent) => &mut parent.rev_pending,
                    None => &mut self.doc_rev_pending,
                };
                match target.iter_mut().find(|p| p.entry == r) {
                    Some(slot) => {
                        if single_slot {
                            slot.cands.clear();
                        }
                        slot.cands.push(cand);
                    }
                    None => target.push(RevPend { entry: r, cands: vec![cand] }),
                }
            }
        }
        // RESOLVE `:has()` for THIS element: all its descendants are now known, so `has_done` is final.
        // For every entry whose constraint held, commit the element's buffered values (attached
        // text/attr captured during its lifetime, outer-HTML sliced now) to the offset-sorted `pending`.
        if self.schema.has_has && e.has_subj != 0 {
            let mut s = e.has_subj;
            while s != 0 {
                let h = s.trailing_zeros();
                s &= s - 1;
                if e.has_done & (1u128 << h) == 0 {
                    continue; // no qualifying descendant/child -> element does not match `:has`
                }
                let he = &self.schema.has_entries[h as usize];
                // Preceding-sibling trigger (`C:has(..) ~ S`): fire the boundary bit on the parent so a
                // later sibling (a normal entry anchored to it) matches; emit no value here. `C` closes
                // before any following sibling opens, so the trigger is visible in time.
                if let Some(bit) = he.trigger {
                    if let Some(parent) = self.stack.last_mut() {
                        parent.seen |= 1u64 << bit;
                        parent.prev |= 1u64 << bit;
                    }
                    continue;
                }
                if let Some(slot) = he.tail {
                    // value lives in this element's subtree (`div:has(a) ::text`, `div:has(a) a::attr(..)`)
                    self.tail_spans.push((slot, e.start, end));
                } else if matches!(he.terminal, Terminal::OuterHtml) {
                    let val = self.enc.decode_without_bom_handling(&self.input[e.start..end]).0.into_owned();
                    self.pending.push((he.col, e.start, val));
                } else {
                    for (eh, off, v) in &e.has_buf {
                        if *eh == h {
                            self.pending.push((he.col, *off, v.clone()));
                        }
                    }
                }
            }
        }
        // RESOLVE text-content predicates for THIS element from its bounded streaming state. Commit
        // retained prospective attr/direct-text output, or slice outer HTML now.
        if self.schema.has_text_pred && e.txt_subj != 0 {
            self.txt_open -= 1;
            let mut s = e.txt_subj;
            while s != 0 {
                let t = s.trailing_zeros();
                s &= s - 1;
                let te = &self.schema.text_entries[t as usize];
                let holds = e
                    .txt_states
                    .iter()
                    .find(|state| state.entry == t)
                    .is_some_and(|state| state.holds(&te.pred));
                if !holds {
                    continue;
                }
                // Preceding-sibling trigger (`C[.="x"] ~ S`): fire the boundary bit on the parent (see
                // the `:has` case above); emit no value here.
                if let Some(bit) = te.trigger {
                    if let Some(parent) = self.stack.last_mut() {
                        parent.seen |= 1u64 << bit;
                        parent.prev |= 1u64 << bit;
                    }
                    continue;
                }
                if let Some(slot) = te.tail {
                    // value lives in this element's subtree (`//div[contains(.,"x")]/a/@href`)
                    self.tail_spans.push((slot, e.start, end));
                    continue;
                }
                match te.terminal {
                    Terminal::OuterHtml => {
                        let val =
                            self.enc.decode_without_bom_handling(&self.input[e.start..end]).0.into_owned();
                        self.pending.push((te.col, e.start, val));
                    }
                    Terminal::Attr { .. } | Terminal::Text { .. } => {
                        for (et, off, v) in &e.txt_emit {
                            if *et == t {
                                self.pending.push((te.col, *off, v.clone()));
                            }
                        }
                    }
                    Terminal::NormalizeSpace(_) => {}
                }
            }
        }
        // finalize any `normalize-space(//el)` accumulator whose element is the one just closed. After
        // the pop, `self.stack.len()` is the closed element's depth (it sat at index depth while open).
        if self.schema.has_ns_element {
            let depth = self.stack.len();
            for st in &mut self.ns {
                if matches!(&st.pending, Some((d, _)) if *d == depth) {
                    let buf = st.pending.take().unwrap().1;
                    st.value = Some(decode::normalize_space(&buf));
                }
            }
        }
    }

    /// `(flat_columns, grouped)` where `grouped[g]` is group `g`'s rows in document order, each row a
    /// `Vec` of sub-field value-columns (`[group][row][sub][value]`).
    pub fn finish_grouped(mut self) -> (FlatColumns, Vec<GroupRows>) {
        // deliver the trailing text run before the stack unwinds out from under it
        self.flush_text();
        // close any elements still open at EOF (raw source runs to end of input)
        let end = self.input.len();
        while let Some(e) = self.stack.pop() {
            self.close_elem(e, end);
        }
        // Reverse-positional output. First resolve any TOP-LEVEL subjects (parent = document) against the
        // document counter frame, which is all that remains in `self.pos` now. Then scatter every deferred
        // reverse value into its column sorted by byte offset = document order (values can commit out of
        // order when parents close inner-first). A reverse column holds only its lone reverse member, so
        // `results[col]` is otherwise empty here.
        if self.schema.has_reverse || self.schema.has_has || self.schema.has_text_pred {
            if !self.doc_rev_pending.is_empty() {
                let total_children = self.pos.first().copied().unwrap_or(0);
                let doc_pending = std::mem::take(&mut self.doc_rev_pending);
                for pend in &doc_pending {
                    let re = &self.schema.reverse_entries[pend.entry as usize];
                    let total = match re.of_type_tag {
                        Some(j) => self.pos.get(1 + j).copied().unwrap_or(0),
                        None => total_children,
                    };
                    for cand in &pend.cands {
                        if reverse_matches(&re.rev, cand.idx, total) {
                            if let Some(slot) = re.tail {
                                self.tail_spans.push((slot, cand.span.0, cand.span.1));
                            } else {
                                for (off, v) in &cand.vals {
                                    self.pending.push((re.col, *off, v.clone()));
                                }
                            }
                        }
                    }
                }
            }
            self.resolve_tail_spans();
            if !self.pending.is_empty() {
                let mut pend = std::mem::take(&mut self.pending);
                pend.sort_by_key(|&(col, off, _)| (col, off));
                for (col, _off, v) in pend {
                    self.results[col].push(v);
                }
            }
        }
        // Place any outer-HTML captures. `seq` is dense (0..next_seq), so index each instance's
        // finished row by seq with a Vec (beats a HashMap); only built when there are captures.
        if !self.captures.is_empty() {
            let mut pos: Vec<Option<(usize, usize)>> = vec![None; self.next_seq as usize];
            for (g, rows) in self.group_rows.iter().enumerate() {
                for (idx, (seq, _)) in rows.iter().enumerate() {
                    pos[*seq as usize] = Some((g, idx));
                }
            }
            // outer-HTML fragments emit in element START order (document order = lxml node order);
            // pop order is inner-first, so sort by start before scattering.
            self.captures.sort_by_key(|&(start, _, _)| start);
            for (start, end, dest) in std::mem::take(&mut self.captures) {
                let val = self.enc.decode_without_bom_handling(&self.input[start..end]).0.into_owned();
                match dest {
                    Dest::Flat(col) => self.results[col].push(val),
                    Dest::Grouped { seq, sub } => {
                        if let Some((g, idx)) = pos[seq as usize] {
                            self.group_rows[g][idx].1[sub].push(val);
                        }
                    }
                }
            }
        }
        // `normalize-space(...)` columns are scalar: exactly one value (the first matched node's
        // normalized string-value, or "" if nothing matched — matching XPath's `['']`). ns entries
        // never push during the stream, so each such column is set here to its single value.
        if self.schema.has_ns {
            for (k, cs) in self.schema.entries.iter().enumerate() {
                if matches!(cs.terminal, Terminal::NormalizeSpace(_)) && cs.col < self.results.len() {
                    self.results[cs.col].clear();
                    self.results[cs.col].push(self.ns[k].value.clone().unwrap_or_default());
                }
            }
        }
        // per group: rows in document order (by container start = seq), sub-columns only
        let grouped: Vec<GroupRows> = self
            .group_rows
            .into_iter()
            .map(|mut rows| {
                rows.sort_by_key(|&(seq, _)| seq);
                rows.into_iter().map(|(_, cols)| cols).collect()
            })
            .collect();
        (self.results, grouped)
    }

    fn is_interesting(&self, name: &[u8]) -> bool {
        self.schema.interesting.iter().any(|x| x.as_bytes().eq_ignore_ascii_case(name))
    }
}

impl<'a> Matcher<'a> {
    /// Sibling anchor bits for `stack[top]`, computed at its open. For each boundary `i` of each
    /// multi-segment entry, bit `seg_bits[i]` is set iff `top` matches the head compound of segment
    /// `i+1` AND its parent's `prev`/`seen` carry `seg_bits[i]` (a preceding sibling was a subject of
    /// segment `i`). Read at open (pre-update), so `prev`/`seen` still exclude `top` itself — the fix
    /// for a sibling followed by a descendant/child step, where the anchor is the sibling element, not
    /// the subject. Only run when `has_sibling` (bits are globally unique per boundary, no collision).
    fn compute_anchors(&self, top: usize) -> u64 {
        let parent = if top >= 1 { Some(&self.stack[top - 1]) } else { None };
        let mut anchor = 0u64;
        for cs in &self.schema.entries {
            if cs.dead || cs.segments.len() == 1 {
                continue;
            }
            for i in 0..cs.segments.len() - 1 {
                if gate_open(parent, cs.seg_bits[i], cs.adj[i])
                    && compound_matches(&cs.segments[i + 1].parts[0], &self.stack[top])
                {
                    anchor |= 1u64 << cs.seg_bits[i];
                }
            }
        }
        anchor
    }

    fn eval(&self, top: usize) -> (u128, u64) {
        let mut matched = 0u128;
        let mut trig = 0u64;
        for (k, cs) in self.schema.entries.iter().enumerate() {
            let (subj, tg) = eval_one(cs, &self.stack, top);
            if subj && k < 128 {
                matched |= 1u128 << k;
            }
            trig |= tg;
        }
        (matched, trig)
    }

    fn emit_attrs(&mut self) {
        let top = self.stack.len() - 1;
        // A comma group can mix `::attr(<different names>)` with `::text` (see `parse_list`). `::attr`
        // values emit in the element's SOURCE attribute order — lxml orders a union by document
        // position, and an element's attribute nodes are in source order — deduped per (column, name)
        // so the same attribute node selected by several members appears once. `::text` members of the
        // same column emit separately in `text()`; both stream in document order, so the merged column
        // matches lxml's union. Collect first, then push, to keep the `self.stack` borrow read-only.
        let mut pushes: Vec<(usize, String)> = Vec::new();
        {
            let el = &self.stack[top];
            let mut done: Vec<(usize, &[u8])> = Vec::new(); // (col, attr-name) already emitted here
            for entry in &el.attrs {
                let aname: &[u8] = entry.0;
                for (k, cs) in self.schema.entries.iter().enumerate().take(128) {
                    let (name, subtree) = match &cs.terminal {
                        Terminal::Attr { name, subtree } => (name, *subtree),
                        _ => continue,
                    };
                    // a group container never emits a flat value; a >=128 column can't be tracked
                    if !cs.emit || cs.col >= 128 || !aname.eq_ignore_ascii_case(name.as_bytes()) {
                        continue;
                    }
                    let hit = if subtree {
                        el.matched_tree & (1u128 << k) != 0
                    } else {
                        el.matched & (1u128 << k) != 0
                    };
                    if !hit || done.iter().any(|&(c, n)| c == cs.col && n.eq_ignore_ascii_case(aname)) {
                        continue;
                    }
                    done.push((cs.col, aname));
                    pushes.push((cs.col, entry.1.to_string()));
                }
            }
        }
        for (col, v) in pushes {
            self.results[col].push(v);
        }
        if self.schema.has_ns {
            self.ns_attr(top);
        }
        self.emit_grouped_attrs(top);
    }

    /// Capture the first matched attribute value for a `normalize-space(//el/@a)` entry.
    fn ns_attr(&mut self, top: usize) {
        for k in 0..self.schema.entries.len().min(128) {
            let (name, subtree) = match &self.schema.entries[k].terminal {
                Terminal::NormalizeSpace(inner) => match &**inner {
                    Terminal::Attr { name, subtree } => (name.as_str(), *subtree),
                    _ => continue,
                },
                _ => continue,
            };
            if self.ns[k].value.is_some() {
                continue;
            }
            let hit = if subtree {
                self.stack[top].matched_tree & (1u128 << k) != 0
            } else {
                self.stack[top].matched & (1u128 << k) != 0
            };
            if hit {
                if let Some(v) = self.stack[top].attr(name) {
                    self.ns[k].value = Some(decode::normalize_space(v));
                }
            }
        }
    }

    /// Assign `stack[top]`'s 1-based sibling positions from its parent's running counts, then bump the
    /// parent's counts and push a fresh (zeroed) counter frame for `stack[top]`'s own children. The
    /// parent's frame is index `top` (the document is frame 0); `of_type_index` is filled only when the
    /// element's tag is one a positional selector counts.
    fn positional_open(&mut self, top: usize, name: &[u8]) {
        let stride = 1 + self.schema.positional_tags.len();
        let parent = top * stride;
        let child_index = self.pos[parent] + 1;
        self.pos[parent] = child_index;
        let mut of_type_index = 0;
        if let Some(j) = self.schema.positional_tags.iter().position(|t| t.eq_ignore_ascii_case(name)) {
            of_type_index = self.pos[parent + 1 + j] + 1;
            self.pos[parent + 1 + j] = of_type_index;
        }
        self.stack[top].child_index = child_index;
        self.stack[top].of_type_index = of_type_index;
        self.pos.resize(self.pos.len() + stride, 0); // this element's own children start at zero
    }

    /// Register PROVISIONAL reverse-subject matches for `stack[top]` and capture its `::attr` value(s)
    /// now (attributes are known at open; `::text` arrives later). Provisional = the subject segment
    /// matches STRUCTURALLY (`compound_matches` ignores the reverse bit); whether the element is actually
    /// last/only is decided at its parent's close. The subject's captured values ride on its own
    /// `rev_buf` until it closes, then get promoted to the parent (see `close_elem`).
    fn reverse_open(&mut self, top: usize) {
        // provisional (structural) subject matches for THIS element, plus its `::attr` value up front
        // (attributes are known at open; `::text` arrives in `reverse_text`). Reverse subjects use only
        // attached (`subtree == false`) terminals, so a match's value is always the element's own.
        let mut subj = 0u128;
        let start = self.stack[top].start;
        let mut caps: Vec<(u32, usize, String)> = Vec::new();
        for (r, re) in self.schema.reverse_entries.iter().enumerate() {
            if re.dead || !seg_match(&re.seg, &self.stack, top, 0) {
                continue;
            }
            subj |= 1u128 << r;
            // A subtree entry streams nothing — its values are recovered from the winner's span later.
            if let (None, Terminal::Attr { name, subtree: false }) = (re.tail, &re.terminal) {
                if let Some(v) = self.stack[top].attr(name) {
                    caps.push((r as u32, start, v.to_string()));
                }
            }
        }
        self.stack[top].rev_subj = subj;
        self.stack[top].rev_buf.extend(caps);
    }

    /// Capture this text node for any reverse entry whose (attached) `::text` subject is `stack[top]`.
    /// The value is held on the subject's `rev_buf` keyed by its byte offset — reverse values can be
    /// committed out of document order (a nested last-child resolves before an outer one), so the offset
    /// is what re-sorts each column back into document order at finish.
    fn reverse_text(&mut self, top: usize, val: TextVal<'_>, off: usize) {
        let mut want = 0u128;
        let mut m = self.stack[top].rev_subj;
        while m != 0 {
            let r = m.trailing_zeros();
            m &= m - 1;
            let re = &self.schema.reverse_entries[r as usize];
            if re.tail.is_none() && matches!(re.terminal, Terminal::Text { subtree: false }) {
                want |= 1u128 << r;
            }
        }
        if want == 0 {
            return;
        }
        let out = val.finalize(self.enc);
        let mut w = want;
        while w != 0 {
            let r = w.trailing_zeros();
            w &= w - 1;
            self.stack[top].rev_buf.push((r, off, out.clone()));
        }
    }

    /// Register PROVISIONAL `:has()` subject matches for `stack[top]` (structural — `compound_matches`
    /// ignores the `has`) and capture its `::attr` value up front. Whether the `:has` constraint holds
    /// is decided at THIS element's OWN close, as `has_descendant_check` sets `has_done` while the
    /// subtree streams. Attached (`subtree == false`) / outer terminals only, so a match's value is the
    /// element's own (attr now, `::text` in `has_text`, outer source at close).
    fn has_open(&mut self, top: usize) {
        let mut subj = 0u128;
        let start = self.stack[top].start;
        let mut caps: Vec<(u32, usize, String)> = Vec::new();
        for (h, he) in self.schema.has_entries.iter().enumerate() {
            if he.dead || !seg_match(&he.seg, &self.stack, top, 0) {
                continue;
            }
            subj |= 1u128 << h;
            // a tail entry streams nothing — its values come from the span re-scan at resolution
            if let (None, Terminal::Attr { name, .. }) = (he.tail, &he.terminal) {
                if let Some(v) = self.stack[top].attr(name) {
                    caps.push((h as u32, start, v.to_string()));
                }
            }
        }
        self.stack[top].has_subj = subj;
        self.stack[top].has_buf.extend(caps);
    }

    /// If `stack[top]` matches the `:has` INNER compound of some entry, mark that entry's `has_done` bit
    /// on the enclosing provisional subject(s): for `rel == Descendant` every ancestor subject (`top` is
    /// a strict descendant of each), for `rel == Child` only the direct parent. Called at each open, so
    /// by the time a subject closes its `has_done` reflects its whole subtree.
    fn has_descendant_check(&mut self, top: usize) {
        for (h, he) in self.schema.has_entries.iter().enumerate() {
            if he.dead || !compound_matches(&he.has.inner, &self.stack[top]) {
                continue;
            }
            let bit = 1u128 << h;
            match he.has.rel {
                Comb::Child => {
                    // a direct child: the subject must be `top`'s immediate parent
                    if top >= 1 && self.stack[top - 1].has_subj & bit != 0 {
                        self.stack[top - 1].has_done |= bit;
                    }
                }
                _ => {
                    // descendant: every ancestor that is a provisional subject of this entry qualifies
                    for a in 0..top {
                        if self.stack[a].has_subj & bit != 0 {
                            self.stack[a].has_done |= bit;
                        }
                    }
                }
            }
        }
    }

    /// Capture this text node for any `:has` entry whose (attached) `::text` subject is `stack[top]` —
    /// held on `has_buf` keyed by byte offset (values commit out of document order when subjects nest,
    /// so the offset re-sorts each column at finish). Mirrors [`Matcher::reverse_text`].
    fn has_text(&mut self, top: usize, val: TextVal<'_>, off: usize) {
        let mut want = 0u128;
        let mut m = self.stack[top].has_subj;
        while m != 0 {
            let h = m.trailing_zeros();
            m &= m - 1;
            let he = &self.schema.has_entries[h as usize];
            if he.tail.is_none() && matches!(he.terminal, Terminal::Text { subtree: false }) {
                want |= 1u128 << h;
            }
        }
        if want == 0 {
            return;
        }
        let out = val.finalize(self.enc);
        let mut w = want;
        while w != 0 {
            let h = w.trailing_zeros();
            w &= w - 1;
            self.stack[top].has_buf.push((h, off, out.clone()));
        }
    }

    /// Register PROVISIONAL text-content-predicate subject matches for `stack[top]` (structural), create
    /// bounded predicate state, and capture its prospective `::attr` output. Bumps the open counter.
    fn text_open(&mut self, top: usize) {
        let mut subj = 0u128;
        let start = self.stack[top].start;
        let mut states = Vec::new();
        let mut emit = Vec::new();
        for (t, te) in self.schema.text_entries.iter().enumerate() {
            if te.dead || !seg_match(&te.seg, &self.stack, top, 0) {
                continue;
            }
            subj |= 1u128 << t;
            states.push(TextMatchState::new(t as u32, &te.pred));
            if let Terminal::Attr { name, .. } = &te.terminal {
                if let Some(v) = self.stack[top].attr(name) {
                    emit.push((t as u32, start, v.to_string()));
                }
            }
        }
        if subj != 0 {
            self.txt_open += 1;
        }
        self.stack[top].txt_subj = subj;
        self.stack[top].txt_states.extend(states);
        self.stack[top].txt_emit.extend(emit);
    }

    /// Stream this text node through every open predicate state it belongs to. Only direct text values
    /// that may become terminal output are retained; descendant predicate input stays bounded.
    fn text_event(&mut self, top: usize, val: TextVal<'_>, off: usize) {
        let out = val.finalize(self.enc);
        for e in 0..=top {
            if self.stack[e].txt_subj == 0 {
                continue;
            }
            let direct = e == top;
            let mut emit_entries = Vec::new();
            {
                let elem = &mut self.stack[e];
                for state in &mut elem.txt_states {
                    let te = &self.schema.text_entries[state.entry as usize];
                    state.update(&te.pred, &out, direct);
                    if direct && te.tail.is_none() && matches!(te.terminal, Terminal::Text { subtree: false }) {
                        emit_entries.push(state.entry);
                    }
                }
            }
            for entry in emit_entries {
                self.stack[e].txt_emit.push((entry, off, out.clone()));
            }
        }
    }

    /// Feed this text node into the `normalize-space(...)` accumulators: append to any open element
    /// string-value (`//el`), and capture the first matched text node for a `text()` inner.
    fn ns_text(&mut self, top: usize, val: TextVal<'_>) {
        let enc = self.enc;
        for k in 0..self.schema.entries.len().min(128) {
            let inner = match &self.schema.entries[k].terminal {
                Terminal::NormalizeSpace(inner) => &**inner,
                _ => continue,
            };
            match inner {
                // element string-value: all text while the first matched element is open is its subtree
                Terminal::OuterHtml => {
                    if let Some((_d, buf)) = &mut self.ns[k].pending {
                        buf.push_str(&val.finalize(enc));
                    }
                }
                // first matched text node (self or descendant, per the inner subtree flag)
                Terminal::Text { subtree } if self.ns[k].value.is_none() => {
                    let hit = if *subtree {
                        self.stack[top].matched_tree & (1u128 << k) != 0
                    } else {
                        self.stack[top].matched & (1u128 << k) != 0
                    };
                    if hit {
                        self.ns[k].value =
                            Some(decode::normalize_space(&val.finalize(enc)));
                    }
                }
                _ => {}
            }
        }
    }

    /// Route `::attr(name)` sub-fields into every open instance whose scope contains `top`.
    fn emit_grouped_attrs(&mut self, top: usize) {
        if self.open_instances.is_empty() {
            return;
        }
        for ii in 0..self.open_instances.len() {
            let (g, depth) = (self.open_instances[ii].group, self.open_instances[ii].depth);
            for (sub_idx, sub) in self.schema.groups[g].subs.iter().enumerate() {
                if let Terminal::Attr { name, subtree } = &sub.terminal {
                    if sub_hits(&sub.seg, *subtree, &self.stack, top, depth) {
                        if let Some(v) = self.stack[top].attr(name) {
                            self.open_instances[ii].buckets[sub_idx].push(v.to_string());
                        }
                    }
                }
            }
        }
    }
    /// `<html/>`, `<head/>` and `<body/>` written where the frame already exists: the tag INSERTS
    /// nothing, but the `/>` still fires a close — and it lands on the enclosing element.
    ///
    /// libxml2's `endElement` pops the CURRENT node by position, not by name. A redundant frame tag
    /// pushes no element of its own, so the pop takes whatever the tag was written inside: one level,
    /// after any implied closes this tag already ran. `<div><b>x<html/>y` is `<div><b>x</b>y</div>`,
    /// and a second `<html/>` then takes the `<div>`. The phantom is NOT consumed — a later `</body>`
    /// is still absorbed by it — which is what separates this from `<html></html>`, where the written
    /// end tag pops the phantom and the enclosing element survives.
    ///
    /// Found on a crawled page that opens a stray `<strong>` before its doctype and then writes
    /// `<html xmlns=… />`. libxml2 ends the `<strong>` there; leaving it open parented the page's
    /// entire body inside it, so every `body > *` a scraper asks for was empty while `body *` was
    /// intact — the frame wrong, not the values.
    fn self_close_with_no_element_of_its_own(&mut self, span_start: usize) {
        self.flush_text();
        if let Some(e) = self.stack.pop() {
            self.close_elem(e, span_start);
        }
    }
}

impl<'a> TokenSink<'a> for Matcher<'a> {
    /// A doctype leaves no node AND does not end the surrounding text node, so it is absorbed into the
    /// pending node's gap the way a dropped end tag is: the runs either side of it are ONE node.
    fn invisible_markup(&mut self, from: usize, to: usize) {
        if let Some(p) = &mut self.pending_text {
            if p.gap_end == from {
                p.gap_end = to;
            }
        }
    }

    fn start_tag(
        &mut self,
        name: &'a [u8],
        raw_attrs: &[(&'a [u8], Option<&'a [u8]>)],
        self_closing: bool,
        span_start: usize,
        open_end: usize,
    ) {
        let (void, scid) = classify(name);
        let frame = frame_slot(name);
        // Build whatever of the document frame this tag needs and the byte stream did not write.
        self.ensure_frame(name, frame, span_start);
        // A document-frame tag that is BOTH out of place and closes nothing is completely invisible to
        // libxml2, so it must not split the text node either: absorb it into the pending node's gap the
        // way a dropped end tag is absorbed. Decided before `flush_text`, which would end the node.
        if frame.is_some() && tag_is_redundant(name, &self.stack) && !self.closes_top(name, scid) {
            self.frame.note_ignored();
            if self_closing {
                self.self_close_with_no_element_of_its_own(span_start);
                return;
            }
            if let Some(p) = &mut self.pending_text {
                if p.gap_end == span_start {
                    p.gap_end = open_end;
                }
            }
            return;
        }
        // Any buffered text belongs to the element open BEFORE this tag reshapes the stack.
        self.flush_text();
        // inline implied-close reshape: a popped element's raw source ends where this tag begins
        let mut popped_head = false;
        while let Some(top) = self.stack.last() {
            let closes = start_closes(scid, top.scid);
            if crate::mutate::closes(name, top.tag, closes) {
                let e = self.stack.pop().unwrap();
                popped_head |= self.frame.head_seen() && e.tag.eq_ignore_ascii_case(b"head");
                self.close_elem(e, span_start);
            } else {
                break;
            }
        }
        // A start tag that ENDED the head also STARTS the body, and everything from here on — including
        // the remaining `<meta>`/`<link>`/`<title>` — belongs to it. libxml2 and html5lib agree on this
        // exactly; the engine used to close the head correctly and then leave the content under `<html>`,
        // which is what made `head + body`, `html > body` and `:first-child` disagree.
        //
        // Only for an IMPLICIT end. After an explicit `</head>` the two oracles part company (libxml2
        // leaves a following `<meta>` at `<html>` level, html5lib puts it back in the head), so that path
        // keeps libxml2's shape, which is what the engine already produces.
        //
        // A real `<body>`/`<html>`/`<head>` is excluded because it opens the frame itself — and `<body>`
        // in particular reaches here having just popped the head through the same relation. So is a tag
        // that opens NEITHER part: a frameset document has no `<body>` at all, and `<frameset>` ends the
        // head like any other non-head content. Wrapping it in an invented body put a real page's whole
        // frameset inside one, where libxml2 makes it a child of `<html>` — which `frame_content` already
        // knew and only `ensure_frame` was asking.
        if popped_head
            && frame.is_none()
            && !self.frame.body_established()
            && frame_content(&String::from_utf8_lossy(name).to_ascii_lowercase())
                == FrameContent::Body
        {
            self.start_tag(b"body", &[], false, span_start, span_start);
        }
        // ... and once the implied closes have run, a redundant frame tag inserts NOTHING. libxml2
        // merges its attributes onto the element that already exists; there is nowhere to merge them
        // here, and inventing a second `<body>` inside the first is the observable error (it gave
        // `<div>d<html>y</div>` a spurious `html` child, and `<p>x<body>y` two text nodes).
        // Re-evaluated HERE, not before the reshape: `<body>` closes an open `<head>`, and until that
        // pop has happened the head still makes the tag look redundant — which swallowed the `<body>` of
        // every document that omits `</head>`.
        if let Some(slot) = frame {
            if tag_is_redundant(name, &self.stack) {
                self.frame.note_ignored();
                if self_closing {
                    self.self_close_with_no_element_of_its_own(span_start);
                }
                return;
            }
            // this frame element is being INSERTED, so the window an implied `<body>` can open in either
            // begins (`<head>`) or ends (`<body>`, real or the one synthesized just above)
            self.frame.note_inserted(slot);
        }
        // materialize only the attributes some selector references, decoding lazily (borrowed Cow
        // when the value is clean valid UTF-8 — the common case, so usually zero allocation).
        let mut attrs: Vec<(&'a [u8], Cow<'a, str>)> = Vec::new();
        for &(an, av) in raw_attrs {
            if self.is_interesting(an) {
                let value = match av {
                    Some(value) => decode_attr(value, self.enc),
                    None if is_minimized_boolean_attr(an) => {
                        Cow::Owned(String::from_utf8_lossy(an).to_ascii_lowercase())
                    }
                    None => Cow::Borrowed(""),
                };
                attrs.push((an, value));
            }
        }
        self.stack.push(OpenElem {
            tag: name,
            scid,
            attrs,
            matched: 0,
            matched_tree: 0,
            text_cols: 0,
            seen: 0,
            prev: 0,
            anchor: 0,
            start: span_start,
            cap_cols: 0,
            insts: 0,
            gcaps: Vec::new(),
            child_index: 0,
            of_type_index: 0,
            rev_subj: 0,
            rev_buf: Vec::new(),
            rev_pending: Vec::new(),
            has_subj: 0,
            has_done: 0,
            has_buf: Vec::new(),
            txt_subj: 0,
            txt_states: Vec::new(),
            txt_emit: Vec::new(),
        });
        let top = self.stack.len() - 1;
        // Assign this element's 1-based sibling positions from its parent's running counts, BEFORE
        // `eval` (positional compounds read them). Only when the schema uses positions.
        if self.schema.has_positional {
            self.positional_open(top, name);
        }
        // Establish this element's sibling anchors BEFORE evaluating subjects: the anchor gate reads
        // the parent's `prev`/`seen`, which still reflect the PRECEDING siblings (they're updated with
        // this element's own trigger only after `eval`). Subjects (incl. a single-compound last segment
        // matching here) then read `anchor` — including this element's own.
        if self.schema.has_sibling {
            self.stack[top].anchor = self.compute_anchors(top);
        }
        let (matched, trig) = self.eval(top);
        self.stack[top].matched = matched;
        let inherited = if top == 0 { 0 } else { self.stack[top - 1].matched_tree };
        self.stack[top].matched_tree = inherited | matched;
        // Precompute flat text routing once per element. Previously every text event scanned every
        // selector and, for subtree terminals, the whole ancestor stack. This mask makes the common
        // text path O(number of emitted columns), independent of selector count and nesting depth.
        let mut text_cols = 0u128;
        for (k, cs) in self.schema.entries.iter().enumerate().take(128) {
            if !cs.emit || cs.col >= 128 {
                continue;
            }
            if let Terminal::Text { subtree } = &cs.terminal {
                let hit = if *subtree {
                    self.stack[top].matched_tree & (1u128 << k) != 0
                } else {
                    matched & (1u128 << k) != 0
                };
                if hit {
                    text_cols |= 1u128 << cs.col;
                }
            }
        }
        self.stack[top].text_cols = text_cols;
        // Reverse positions can't be decided now (they need this element's parent's total sibling count,
        // known only at the parent's close). Register a PROVISIONAL subject match — structural only —
        // and capture any `::attr` value up front; `::text` is captured in `text()`.
        if self.schema.has_reverse {
            self.reverse_open(top);
        }
        // `:has()`: register this element as a provisional subject, and check whether it satisfies an
        // enclosing subject's inner constraint (marking `has_done` up the stack). Resolved at each
        // subject's own close.
        if self.schema.has_has {
            self.has_open(top);
            self.has_descendant_check(top);
        }
        // Text-content predicate: register this element as a provisional subject (resolved at its close).
        if self.schema.has_text_pred {
            self.text_open(top);
        }
        // open a group instance for every container this element is a subject match for (its
        // sub-fields are then scoped to it until it closes). Do this BEFORE grouped sub-eval so a
        // container's own descendant-or-self sub-fields (incl. matching itself) are in scope.
        let mut insts = 0usize;
        for &(k, g) in &self.schema.container_entries {
            if k < 128 && matched & (1u128 << k) != 0 {
                let seq = self.next_seq;
                self.next_seq += 1;
                let n = self.schema.groups[g].subs.len();
                self.open_instances.push(OpenInstance { group: g, depth: top, seq, buckets: vec![Vec::new(); n] });
                insts += 1;
            }
        }
        self.stack[top].insts = insts;
        // which output columns want this element's raw source (bare-element OuterHtml matches)
        if self.schema.has_outer {
            let mut cap = 0u128;
            for (k, cs) in self.schema.entries.iter().enumerate().take(128) {
                if cs.emit && matches!(cs.terminal, Terminal::OuterHtml) && matched & (1u128 << k) != 0 && cs.col < 128 {
                    cap |= 1u128 << cs.col;
                }
            }
            self.stack[top].cap_cols = cap;
            // grouped outer-HTML sub-fields: stamp the destination NOW (capture is materialized at
            // close, by which time the current-instance pointer would be gone).
            for ii in 0..self.open_instances.len() {
                let (g, depth) = (self.open_instances[ii].group, self.open_instances[ii].depth);
                let seq = self.open_instances[ii].seq;
                for (sub_idx, sub) in self.schema.groups[g].subs.iter().enumerate() {
                    if matches!(sub.terminal, Terminal::OuterHtml)
                        && sub_hits(&sub.seg, false, &self.stack, top, depth)
                    {
                        self.stack[top].gcaps.push((seq, sub_idx));
                    }
                }
            }
        }
        // `normalize-space(//el)`: the first matched element opens a pending accumulator; its subtree
        // text is collected in `text()` and finalized at its close. Only the first (value still unset,
        // no pending yet) — later matches are ignored.
        if self.schema.has_ns_element {
            for k in 0..self.schema.entries.len().min(128) {
                if matches!(&self.schema.entries[k].terminal, Terminal::NormalizeSpace(inner) if matches!(**inner, Terminal::OuterHtml))
                    && matched & (1u128 << k) != 0
                    && self.ns[k].value.is_none()
                    && self.ns[k].pending.is_none()
                {
                    self.ns[k].pending = Some((top, String::new()));
                }
            }
        }
        if top >= 1 {
            // Mask out Case-B deferred boundaries: their trigger is fired pred-gated at `C`'s close, not
            // here at open. `trig_immediate_mask` is all-ones unless a Case-B selector is compiled, so
            // this is a no-op (one AND) on the common hot path.
            let applied = trig & self.schema.trig_immediate_mask;
            let p = &mut self.stack[top - 1];
            p.seen |= applied;
            p.prev = applied;
        }
        self.emit_attrs();
        if self_closing || void {
            let e = self.stack.pop().unwrap(); // no children; raw source = input[start..open_end]
            self.close_elem(e, open_end);
        }
    }

    fn end_tag(&mut self, name: &[u8], close_start: usize, close_end: usize) {
        // A frame end tag matching an earlier IGNORED frame start tag pops that phantom, not the
        // document: `<div><body>x</body>tail</div>` keeps `xtail` inside the div (verified against
        // libxml2, which keeps a stack entry for the start tag it merged away). Checked before the
        // stack, because a real `<body>` IS open in that shape and would otherwise close.
        if self.frame.absorbs_end_tag(name) {
            if let Some(p) = &mut self.pending_text {
                if p.gap_end == close_start {
                    p.gap_end = close_end;
                }
            }
            return;
        }
        // Is this tag DISCARDED rather than honored? Either it matches no open element, or something
        // still open above the match OUT-RANKS it — libxml2 refuses to unwind that (END-TAG SCOPE), so
        // `<td>A</div>B` keeps `AB` in the cell. Both cases leave the stack alone and extend the
        // buffered text node's gap.
        let matched = self.stack.iter().rposition(|e| e.tag.eq_ignore_ascii_case(name));
        let discarded = match matched {
            None => true,
            Some(k) => {
                self.stack[k + 1..].iter().any(|e| blocks_end_tag(e.tag, name))
                    && end_tag_discardable(name)
            }
        };
        if discarded {
            // Absorb the tag into the pending node's gap so a following run still joins across it —
            // consecutive drops (`<div>A</p></p>B</div>`) chain, since each abuts the previous gap end.
            if let Some(p) = &mut self.pending_text {
                if p.gap_end == close_start {
                    p.gap_end = close_end;
                }
            }
            return;
        }
        let k = matched.expect("not discarded => matched");
        // Buffered text belongs inside the element about to close, so deliver it first.
        self.flush_text();
        // elements ABOVE the match are implicitly closed — their raw source ends at the end
        // tag's `<` (close_start); the matching element itself ends after its `>` (close_end).
        while self.stack.len() > k + 1 {
            let e = self.stack.pop().unwrap();
            self.close_elem(e, close_start);
        }
        let e = self.stack.pop().unwrap();
        self.close_elem(e, close_end);
    }

    /// Buffer one text RUN, delivering the PREVIOUS one — a run is not a text node until we know no
    /// dropped end tag follows it.
    ///
    /// libxml2 drops an end tag with no open element to match (`<div>A</p>B</div>`) and the character
    /// data either side of it becomes ONE text node (`AB`). The tokenizer, which has no stack, can only
    /// hand us two runs. Re-joining them here — rather than at each consumer — is what keeps
    /// `::text` columns, `/text()`, `normalize-space(...)` and the text-content predicates consistent:
    /// they all see the same single node lxml does. Comments/CDATA/PIs also split a run, but they split
    /// it in lxml too, so only source-ADJACENCY across the dropped tag may be re-joined.
    fn text(&mut self, text: &'a [u8], allows_entities: bool, start: usize) {
        // Character data can START THE BODY, and it does so MID-RUN: the leading whitespace stays where
        // it was (the head's, or dropped if no frame exists yet) and the first non-space character opens
        // the body. Both oracles split it exactly there. The tokenizer hands us one run, so split it
        // here and let each half take the ordinary path.
        if let Some(k) = self.body_text_split(text) {
            if k > 0 {
                self.buffer_text(&text[..k], allows_entities, start);
            }
            // `<body>` closes an open `<head>` through the start-close relation, so opening one here
            // pops the head as well — no separate unwind — and `ensure_frame` supplies the `<html>`
            // above it when the document never wrote one.
            self.ensure_frame(b"body", None, start + k);
            self.start_tag(b"body", &[], false, start + k, start + k);
            self.buffer_text(&text[k..], allows_entities, start + k);
            return;
        }
        // ...and with a body already established the head still ends here, it just has nowhere to move
        // the text to. Popped directly rather than through `end_tag`, which would spend a `</head>`
        // phantom left by an ignored duplicate instead of closing the real element.
        if let Some(k) = self.late_head_text_split(text) {
            if k > 0 {
                self.buffer_text(&text[..k], allows_entities, start);
            }
            self.flush_text();
            let head = self.stack.pop().expect("late_head_text_split checked the top");
            self.close_elem(head, start + k);
            self.buffer_text(&text[k..], allows_entities, start + k);
            return;
        }
        // Character data after `</html>` needs the same second root a START TAG gets there, or it is
        // lost outright: with the stack empty `emit_text` has nothing to attach it to and returns. The
        // start-tag path builds that root in `ensure_frame`; the text path reaches neither split above
        // once a body exists, so it has to ask for itself.
        if nothing_open(&self.stack) && self.frame.body_established() && first_non_ws(text).is_some() {
            self.start_tag(b"html", &[], false, start, start);
        }
        self.buffer_text(text, allows_entities, start);
    }
}

impl<'a> Matcher<'a> {
    /// Where a text run starts the body, if it does: the offset of its first non-whitespace byte.
    ///
    /// Two shapes, one answer. Either a `<head>` is open and this is the character data that ends it
    /// (the run splits there — the leading whitespace is still the head's), or no body exists yet and
    /// this is the first content in the document, which opens `<html>`/`<body>` around it.
    ///
    /// Whitespace ALONE is not content in either shape: it leaves an open head open, and before the
    /// frame exists libxml2 drops it entirely (`   <div>` has no text node before the div).
    /// Text inside a head element (`<title>T</title>`) belongs to that element, hence "head is the
    /// CURRENT open element" rather than "a head is open somewhere".
    fn body_text_split(&self, text: &[u8]) -> Option<usize> {
        if self.frame.body_established() {
            return None; // the common path, once a body exists: one bool test and out
        }
        // outside a head, only text that is not yet inside ANY element can start the frame
        if !head_is_current(&self.stack) && !only_html_open(&self.stack) {
            return None;
        }
        first_non_ws(text)
    }

    /// Where character data ends an open `<head>` that CANNOT start a body, if it does.
    ///
    /// The same rule as above minus its second half: character data always ends an open head, but once a
    /// body exists there is no body to move it into, so libxml2 pops the head and leaves the text at
    /// `<html>` level. Only a `<head>` written after `</body>` gets here — which is why it was missed:
    /// the two halves were one function gated on `body_established()`, so the head simply kept the text.
    fn late_head_text_split(&self, text: &[u8]) -> Option<usize> {
        if !self.frame.body_established() || !head_is_current(&self.stack) {
            return None;
        }
        first_non_ws(text)
    }

    /// Open whatever part of the document frame this token needs and the page did not write.
    ///
    /// `<html>`, `<head>` and `<body>` all have optional start AND end tags, so a conformant document
    /// may contain none of them — and libxml2 builds the frame regardless. Without this the engine had
    /// nothing to anchor `body h1` on, no shared parent to make `h1 + p` siblings, and nowhere to put
    /// root-level text, which is the largest divergence the contract used to list.
    ///
    /// Which part a given start tag opens is [`frame_content`], derived from the oracle over the whole
    /// element universe. The frame tags themselves open no PART — but they still need the element they
    /// belong in: a page whose first tag is `<head>` or `<body>` writes no `<html>`, and libxml2 (and
    /// html5lib, and every browser) still wraps it in one. Leaving that out put the `<head>` at the root
    /// with no parent and then built a SECOND, later `<html>` for whatever followed `</head>`, so
    /// `html > head`, `html > body` and `head + script` were all empty against lxml while the values
    /// underneath them looked right. Real pages do this — one crawled page opens with a bare `<head>`.
    fn ensure_frame(&mut self, name: &[u8], frame: Option<usize>, at: usize) {
        // The `<html>` comes FIRST, before the body check: content after `</html>` still needs an element
        // to sit in, and libxml2 gives it one — a SECOND ROOT `<html>`, which is the same shape it builds
        // for a second `<html>` start tag. Ordering this after the `body_established` return left the
        // tail parentless, so `//html/script` found a real page's trailing script in lxml and not here
        // (the values were all still there; only the frame around them was missing).
        if nothing_open(&self.stack) && !name.eq_ignore_ascii_case(b"html") {
            self.start_tag(b"html", &[], false, at, at);
        }
        if self.frame.body_established() {
            return; // past the body no PART of the frame is synthesized any more
        }
        if frame.is_some() {
            return; // a frame tag builds its own part, now that it has an `<html>` to sit in
        }
        // A head that is already OPEN takes everything it accepts; what it does not accept closes it
        // through the start-close relation, and the body is opened on that pop instead (see `start_tag`)
        // — which is why this is not the same question as `frame_content`.
        if head_is_open(&self.stack) {
            return;
        }
        // Inside a frameset there is no head for head content to go in, so libxml2 opens a BODY for it —
        // the same one it opens for ordinary content there. Only the six `Head` names are affected; the
        // rest already matched.
        let part = frame_content(&String::from_utf8_lossy(name).to_ascii_lowercase());
        let part = match part {
            FrameContent::Head if frameset_is_open(&self.stack) => FrameContent::Body,
            other => other,
        };
        match part {
            // ...and once a `</head>` has been seen, libxml2 does NOT reopen the head for a later
            // head-only tag: it leaves it at `<html>` level. (html5lib puts it back in the head; the
            // tree oracle here is libxml2. See docs/COMPATIBILITY.md.)
            FrameContent::Head if !self.frame.head_seen() => {
                self.start_tag(b"head", &[], false, at, at)
            }
            FrameContent::Body => self.start_tag(b"body", &[], false, at, at),
            _ => {}
        }
    }

    fn buffer_text(&mut self, text: &'a [u8], allows_entities: bool, start: usize) {
        // A run starting exactly at the buffered node's gap end continues it: everything between the two
        // is dropped end tags, which libxml2 discards. Any real node in the gap shifts `start` past
        // `gap_end` and so breaks the join.
        if let Some(p) = &mut self.pending_text {
            if p.gap_end == start && p.allows_entities == allows_entities {
                // decode this run on its own and append the STRING — see `PendingText`
                if p.joined.is_empty() {
                    p.joined = finalize(&p.bytes, allows_entities, self.enc);
                }
                p.joined.push_str(&finalize(text, allows_entities, self.enc));
                p.gap_end = start + text.len();
                return;
            }
        }
        self.flush_text();
        self.pending_text = Some(PendingText {
            bytes: std::borrow::Cow::Borrowed(text),
            joined: String::new(),
            allows_entities,
            start,
            gap_end: start + text.len(),
        });
    }
}

impl<'a> Matcher<'a> {
    /// Would this start tag close the CURRENT open element? A read-only peek, so the caller can decide
    /// whether an ignored tag is going to reshape the stack before committing the pending text node.
    fn closes_top(&self, name: &[u8], scid: u8) -> bool {
        match self.stack.last() {
            None => false,
            Some(top) => {
                let closes = start_closes(scid, top.scid);
                crate::mutate::closes(name, top.tag, closes)
            }
        }
    }


    /// Deliver the buffered text node (if any) to every consumer. Called before any event that could
    /// observe it out of order, and at EOF.
    fn flush_text(&mut self) {
        let Some(p) = self.pending_text.take() else {
            return;
        };
        let val = if p.joined.is_empty() {
            TextVal::Raw { bytes: &p.bytes, entities: p.allows_entities }
        } else {
            TextVal::Decoded(&p.joined)
        };
        self.emit_text(val, p.start);
    }

    fn emit_text(&mut self, val: TextVal<'_>, off: usize) {
        if self.stack.is_empty() {
            return;
        }
        let top = self.stack.len() - 1;
        if self.schema.has_ns {
            self.ns_text(top, val);
        }
        if self.schema.has_reverse && self.stack[top].rev_subj != 0 {
            self.reverse_text(top, val, off);
        }
        if self.schema.has_has && self.stack[top].has_subj != 0 {
            self.has_text(top, val, off);
        }
        if self.schema.has_text_pred && self.txt_open != 0 {
            self.text_event(top, val, off);
        }
        // Which output columns want this text node (deduped across comma-group members). Decided
        // FIRST, so `finalize` (validate+decode) runs only when the text is actually captured —
        // skipping the whole cost for text in unselected regions (most of the document).
        let colmask = self.stack[top].text_cols;
        // grouped ::text sub-fields: (instance index, sub index) targets for this text node
        let mut gtargets: Vec<(usize, usize)> = Vec::new();
        for ii in 0..self.open_instances.len() {
            let (g, depth) = (self.open_instances[ii].group, self.open_instances[ii].depth);
            for (sub_idx, sub) in self.schema.groups[g].subs.iter().enumerate() {
                if let Terminal::Text { subtree } = &sub.terminal {
                    if sub_hits(&sub.seg, *subtree, &self.stack, top, depth) {
                        gtargets.push((ii, sub_idx));
                    }
                }
            }
        }
        if colmask == 0 && gtargets.is_empty() {
            return;
        }
        let out = val.finalize(self.enc);
        for (ii, sub_idx) in gtargets {
            self.open_instances[ii].buckets[sub_idx].push(out.clone());
        }
        if colmask == 0 {
            return;
        }
        if colmask.count_ones() == 1 {
            self.results[colmask.trailing_zeros() as usize].push(out); // common case: move, no clone
        } else {
            let mut m = colmask;
            while m != 0 {
                let col = m.trailing_zeros() as usize;
                m &= m - 1;
                self.results[col].push(out.clone());
            }
        }
    }
}

#[cfg(test)]
mod deferred_state_tests {
    use super::*;

    #[test]
    fn string_contains_state_is_bounded_and_cross_chunk() {
        let pred = TextPred {
            axis: TextAxis::StringValue,
            op: TextOp::Contains,
            needle: "needle".into(),
        };
        let mut state = TextMatchState::new(0, &pred);
        state.update(&pred, &"x".repeat(1_000_000), false);
        match &state.accum {
            TextAccum::StringContains { tail, found } => {
                assert!(!found);
                assert!(tail.len() < pred.needle.len());
            }
            _ => unreachable!(),
        }
        state.update(&pred, "nee", false);
        state.update(&pred, "dle", true);
        assert!(state.holds(&pred));
    }

    #[test]
    fn string_equality_streams_across_chunks() {
        let pred = TextPred { axis: TextAxis::StringValue, op: TextOp::Eq, needle: "café".into() };
        let mut state = TextMatchState::new(0, &pred);
        state.update(&pred, "ca", true);
        state.update(&pred, "fé", false);
        assert!(state.holds(&pred));
        state.update(&pred, "!", false);
        assert!(!state.holds(&pred));
    }
}
