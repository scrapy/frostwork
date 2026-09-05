//! Selector routing eligibility for schema compilation.
//!
//! Deferred-close features parse into the shared selector model, but only particular shapes can be
//! executed faithfully. Keeping those decisions together prevents normal matching from accidentally
//! ignoring a deferred constraint and makes this the single routing policy consumed by compilation.

use crate::selector::{Comb, Compound, Selector, Terminal};

use super::to_segments;

/// Does any compound of `sel` carry this deferred predicate, INCLUDING inside a `:not()`? The
/// recursion is the point: a tier that ignored a nested one would route a selector as streamable and
/// then silently drop the constraint.
fn any_compound(sel: &Selector, carries: fn(&Compound) -> bool) -> bool {
    fn walk(c: &Compound, carries: fn(&Compound) -> bool) -> bool {
        carries(c) || c.negations.iter().any(|n| walk(n, carries))
    }
    sel.parts.iter().any(|c| walk(c, carries))
}

pub(super) fn any_reverse(sel: &Selector) -> bool {
    any_compound(sel, |c| c.reverse.is_some())
}

pub(super) fn any_has(sel: &Selector) -> bool {
    any_compound(sel, |c| c.has.is_some())
}

pub(super) fn any_text_pred(sel: &Selector) -> bool {
    any_compound(sel, |c| c.text_pred.is_some())
}

/// Shared shape check for all three deferred tiers: exactly one compound carries the predicate (at any
/// position, not just the subject), no OTHER kind of deferred predicate anywhere, none nested inside a
/// `:not()`, a single segment, and a tail — if any — recoverable from the deferred element's span.
fn deferrable_common(
    sel: &Selector,
    carries: impl Fn(&Compound) -> bool + Copy,
    others: [fn(&Selector) -> bool; 2],
) -> Option<usize> {
    let k = deferred_index(sel, carries)?;
    if sel.parts.iter().any(|c| c.negations.iter().any(&carries))
        || others.iter().any(|f| f(sel))
        || to_segments(sel).0.len() != 1
        || !tail_recoverable(sel, k)
    {
        return None;
    }
    Some(k)
}

/// `Some(k)` = the compound index carrying the deferred `:has()`. The value may be the subject's own
/// (`div:has(a)::attr(id)`), its whole subtree (`div:has(a) ::text`), or a DESCENDANT's
/// (`div:has(a) a::attr(href)`) — the latter two are recovered by re-scanning `k`'s span.
pub(super) fn deferrable_has_at(sel: &Selector) -> Option<usize> {
    let k = deferrable_common(sel, |c| c.has.is_some(), [any_reverse, any_text_pred])?;
    terminal_ok(sel, k, true).then_some(k)
}

pub(super) fn deferrable_has(sel: &Selector) -> bool {
    deferrable_has_at(sel).is_some()
}

pub(super) fn deferrable_text_pred_at(sel: &Selector) -> Option<usize> {
    let k = deferrable_common(sel, |c| c.text_pred.is_some(), [any_reverse, any_has])?;
    terminal_ok(sel, k, true).then_some(k)
}

pub(super) fn deferrable_text_pred(sel: &Selector) -> bool {
    deferrable_text_pred_at(sel).is_some()
}

/// A terminal on the DEFERRED compound itself must be attached (streamed) / subtree (span re-scan) /
/// outer-HTML (sliced at close). When the value comes from a descendant, the terminal belongs to the tail
/// and is validated by compiling the tail — but either way it must be a per-node value terminal.
///
/// `normalize-space(...)` is excluded deliberately: it is a SCALAR that must yield exactly one value
/// (`""` when nothing matched), sourced from the FIRST matching node document-wide. Neither property
/// survives per-span resolution — no winning span means no value at all, which would make `check()`
/// promise support for a column that comes back empty (the invariant `sel_fuzz` enforces).
fn terminal_ok(sel: &Selector, k: usize, attached_outer_html: bool) -> bool {
    match sel.terminal {
        Terminal::NormalizeSpace(_) => false,
        // outer-HTML is the element's own raw source; only tiers that can slice it at close accept it
        // ATTACHED, but any tier accepts it inside a tail (the tail's own subject supplies it).
        Terminal::OuterHtml => attached_outer_html || k + 1 < sel.parts.len(),
        Terminal::Text { .. } | Terminal::Attr { .. } => true,
    }
}

/// Deferred predicate immediately left of the only sibling boundary (`C[pred] ~ S`).
pub(super) fn sibling_pred_boundary(sel: &Selector) -> Option<usize> {
    let sibs: Vec<usize> = sel
        .combs
        .iter()
        .enumerate()
        .filter(|(_, c)| matches!(c, Comb::Adjacent | Comb::General))
        .map(|(i, _)| i)
        .collect();
    if sibs.len() != 1 {
        return None;
    }
    let k = sibs[0];
    let deferred = |c: &Compound| c.text_pred.is_some() || c.has.is_some();
    for (i, c) in sel.parts.iter().enumerate() {
        if c.reverse.is_some() || (deferred(c) && i != k) {
            return None;
        }
    }
    deferred(&sel.parts[k]).then_some(k)
}

/// Index of the ONE compound carrying a deferred predicate of this kind, or `None` if there isn't
/// exactly one. It need not be the subject: `div:has(a) a::attr(href)` defers on the `div` while the
/// value comes from a descendant.
pub(super) fn deferred_index(sel: &Selector, carries: impl Fn(&Compound) -> bool) -> Option<usize> {
    let mut found = None;
    for (i, c) in sel.parts.iter().enumerate() {
        if carries(c) {
            if found.is_some() {
                return None; // two deferred compounds in one selector: out of tier
            }
            found = Some(i);
        }
    }
    found
}

/// The selector to evaluate INSIDE the deferred element's span, or `None` when the deferred compound is
/// the subject (the value is then the element's own, or its whole subtree).
///
/// `strict_desc` is what makes this faithful: the span's root IS the deferred element, and
/// `div:has(a) div::text` means a *proper* descendant div, so the tail's leftmost compound must be
/// forbidden from binding at the root. Only a DESCENDANT combinator into the tail can be expressed this
/// way — a child anchor (`div:has(a) > p`) would need "depth exactly 1 in the fragment", which the
/// depth-agnostic matcher can't say (the same reason grouped sub-fields reject `./x`), so callers must
/// check `combs[k]` before using this.
pub(super) fn tail_selector(sel: &Selector, k: usize) -> Option<Selector> {
    if k + 1 >= sel.parts.len() {
        return None;
    }
    Some(Selector {
        parts: sel.parts[k + 1..].to_vec(),
        combs: sel.combs[k + 1..].to_vec(),
        terminal: sel.terminal.clone(),
        strict_desc: true,
        context_depth: None,
    })
}

/// STRUCTURAL half of "the tail can be recovered from `k`'s span": the step into the tail must be a
/// plain descendant, and the tail must be a single segment (no sibling combinator, which needs siblings
/// outside the span). Whether the tail is itself *supported* is answered where selectors are lowered, by
/// compiling it and asking `flat_col_supported` — an unsupported tail marks the entry dead so the audit
/// keeps reporting the whole selector unsupported rather than silently returning empty.
pub(super) fn tail_recoverable(sel: &Selector, k: usize) -> bool {
    if k + 1 >= sel.parts.len() {
        return true; // no tail — the deferred compound is the subject
    }
    matches!(sel.combs[k], Comb::Descendant)
        && tail_selector(sel, k).is_some_and(|t| to_segments(&t).0.len() == 1)
}

/// `Some(k)` = the compound index carrying the deferred reverse position. Three value locations, all in
/// tier: the subject's own (`li:last-child::text`, streamed), its subtree (`li:last-child ::text`), or a
/// DESCENDANT's (`li:last-child b::text`) — the last two re-scan `k`'s span. Reverse has no outer-HTML
/// form, so the terminal must be a value one.
pub(super) fn deferrable_reverse_at(sel: &Selector) -> Option<usize> {
    let k = deferrable_common(sel, |c| c.reverse.is_some(), [any_has, any_text_pred])?;
    // reverse has no ATTACHED outer-HTML form (nothing holds the element's raw source until the parent
    // close resolves it); a tail may still end in one.
    terminal_ok(sel, k, false).then_some(k)
}

pub(super) fn deferrable_reverse(sel: &Selector) -> bool {
    deferrable_reverse_at(sel).is_some()
}
