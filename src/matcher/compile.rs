//! Selector routing eligibility for schema compilation.
//!
//! Deferred-close features parse into the shared selector model, but only particular shapes can be
//! executed faithfully. Keeping those decisions together prevents normal matching from accidentally
//! ignoring a deferred constraint and makes this the single routing policy consumed by compilation.

use crate::selector::{Comb, Compound, Selector, Terminal};

use super::to_segments;

fn compound_any_reverse(c: &Compound) -> bool {
    c.reverse.is_some() || c.negations.iter().any(compound_any_reverse)
}

pub(super) fn any_reverse(sel: &Selector) -> bool {
    sel.parts.iter().any(compound_any_reverse)
}

fn compound_any_has(c: &Compound) -> bool {
    c.has.is_some() || c.negations.iter().any(compound_any_has)
}

pub(super) fn any_has(sel: &Selector) -> bool {
    sel.parts.iter().any(compound_any_has)
}

fn compound_any_text_pred(c: &Compound) -> bool {
    c.text_pred.is_some() || c.negations.iter().any(compound_any_text_pred)
}

pub(super) fn any_text_pred(sel: &Selector) -> bool {
    sel.parts.iter().any(compound_any_text_pred)
}

pub(super) fn deferrable_has(sel: &Selector) -> bool {
    let Some(subject) = sel.parts.last() else { return false };
    if subject.has.is_none()
        || subject.negations.iter().any(compound_any_has)
        || any_reverse(sel)
        || any_text_pred(sel)
        || sel.parts[..sel.parts.len() - 1].iter().any(compound_any_has)
    {
        return false;
    }
    matches!(
        sel.terminal,
        Terminal::Text { subtree: false } | Terminal::Attr { subtree: false, .. } | Terminal::OuterHtml
    ) && to_segments(sel).0.len() == 1
}

pub(super) fn deferrable_text_pred(sel: &Selector) -> bool {
    let Some(subject) = sel.parts.last() else { return false };
    if subject.text_pred.is_none()
        || subject.negations.iter().any(compound_any_text_pred)
        || any_has(sel)
        || any_reverse(sel)
        || sel.parts[..sel.parts.len() - 1].iter().any(compound_any_text_pred)
    {
        return false;
    }
    matches!(
        sel.terminal,
        Terminal::Text { subtree: false } | Terminal::Attr { subtree: false, .. } | Terminal::OuterHtml
    ) && to_segments(sel).0.len() == 1
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

pub(super) fn deferrable_reverse(sel: &Selector) -> bool {
    let Some(subject) = sel.parts.last() else { return false };
    if subject.reverse.is_none()
        || subject.negations.iter().any(compound_any_reverse)
        || any_has(sel)
        || any_text_pred(sel)
        || sel.parts[..sel.parts.len() - 1].iter().any(compound_any_reverse)
    {
        return false;
    }
    matches!(
        sel.terminal,
        Terminal::Text { subtree: false } | Terminal::Attr { subtree: false, .. }
    ) && to_segments(sel).0.len() == 1
}
