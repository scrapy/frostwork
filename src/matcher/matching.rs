//! The matching kernel: pure, read-only predicates that answer "does this compiled selector match at
//! this stack position?" over a borrowed open-element stack. Nothing here mutates — the stateful scan
//! (stack maintenance, value emission, group routing) lives in the parent module and calls into these.
//! Keeping the predicates separate makes the matcher's *logic* legible apart from its *bookkeeping*.

use crate::selector::{AttrPred, Comb, Compound, Nth, ReversePos};

use super::{CSel, OpenElem, Segment};

/// Does 1-based position `p` satisfy `∃k ≥ 0: a·k + b == p`? (the `An+B` membership test)
fn anpb_matches(a: i32, b: i32, p: u32) -> bool {
    if p == 0 {
        return false; // 0 = the element's position isn't tracked (never a valid 1-based position)
    }
    let (i, a, b) = (p as i64, a as i64, b as i64);
    if a == 0 {
        return i == b;
    }
    let num = i - b;
    num % a == 0 && num / a >= 0
}

/// Does 1-based sibling index `idx` satisfy the forward `An+B` position?
fn nth_matches(nth: &Nth, idx: u32) -> bool {
    anpb_matches(nth.a, nth.b, idx)
}

/// Does an element at 1-based sibling `idx` (child or of-type, per `rev.of_type`) satisfy the REVERSE
/// position, given the parent's `total` (of that axis)? `:only-*` is `total == 1`; otherwise the
/// element's position FROM THE END is `total - idx + 1`, tested against `An+B`. Decidable only once
/// `total` is known (the parent's close).
pub(super) fn reverse_matches(rev: &ReversePos, idx: u32, total: u32) -> bool {
    if rev.only {
        return total == 1;
    }
    if idx == 0 || idx > total {
        return false;
    }
    anpb_matches(rev.a, rev.b, total - idx + 1)
}

/// Does compound `c` (tag / id / classes / attribute predicates / `:not()`) match element `el`?
pub(super) fn compound_matches(c: &Compound, el: &OpenElem) -> bool {
    if let Some(t) = &c.tag {
        if t != "*" && !el.tag.eq_ignore_ascii_case(t.as_bytes()) {
            return false;
        }
    }
    if let Some(id) = &c.id {
        if el.attr("id") != Some(id.as_str()) {
            return false;
        }
    }
    for cl in &c.classes {
        if !el.has_class(cl) {
            return false;
        }
    }
    for p in &c.attrs {
        // Substring/prefix/suffix ops with an EMPTY operand match nothing (CSS spec, verified vs
        // lxml); `~=` with an empty/whitespace operand also never matches (no empty token).
        let ok = match p {
            AttrPred::Exists(n) => el.attr(n).is_some(),
            AttrPred::Eq(n, v) => el.attr(n) == Some(v.as_str()),
            AttrPred::Prefix(n, v) => !v.is_empty() && el.attr(n).is_some_and(|a| a.starts_with(v.as_str())),
            AttrPred::Suffix(n, v) => !v.is_empty() && el.attr(n).is_some_and(|a| a.ends_with(v.as_str())),
            AttrPred::Substr(n, v) => !v.is_empty() && el.attr(n).is_some_and(|a| a.contains(v.as_str())),
            AttrPred::Includes(n, v) => {
                !v.is_empty() && el.attr(n).is_some_and(|a| a.split_whitespace().any(|t| t == v))
            }
            AttrPred::DashMatch(n, v) => el.attr(n).is_some_and(|a| {
                a == v || (a.len() > v.len() && a.starts_with(v.as_str()) && a.as_bytes()[v.len()] == b'-')
            }),
        };
        if !ok {
            return false;
        }
    }
    // `:not(<compound>)` — excluded if the element matches any negation arg
    for neg in &c.negations {
        if compound_matches(neg, el) {
            return false;
        }
    }
    // `:is(...)`/`:where(...)` — for each group the element must match at least one alternative (OR
    // within a group, AND across groups). Alternatives are plain compounds (no positional/deferred).
    for group in &c.is_groups {
        if !group.iter().any(|alt| compound_matches(alt, el)) {
            return false;
        }
    }
    // `:nth-child`/`:nth-of-type` / XPath `[N]`: the element's 1-based sibling index (set at open) must
    // satisfy the position. `of_type` reads the same-tag index; else the all-element-children index.
    if let Some(nth) = &c.positional {
        let idx = if nth.of_type { el.of_type_index } else { el.child_index };
        if !nth_matches(nth, idx) {
            return false;
        }
    }
    true
}

/// Match `seg` against `stack[si]`, with all compounds constrained to depth `>= floor`. `floor` is
/// the scope floor: `0` for a flat (unscoped) selector, or a container element's depth for a
/// selector scoped inside a `Many`/`One` group (descendant-or-**self** of the container — the
/// leftmost compound may bind at `floor` itself). Flat selectors pass `floor = 0`, which makes the
/// descendant walk `(0..si)` — identical to before.
pub(super) fn seg_match(seg: &Segment, stack: &[OpenElem], si: usize, floor: usize) -> bool {
    seg_match_anchored(seg, stack, si, floor, None)
}

/// Like [`seg_match`], but for a segment sitting to the RIGHT of a sibling combinator it also requires
/// that the segment's FIRST compound bind to an element carrying the sibling **anchor** bit `b` — i.e.
/// an element that, when it opened, matched this segment's head AND had a preceding sibling that
/// matched the previous segment (see `Matcher::compute_anchors`). Checking the anchor at the first
/// compound (not the subject) is what lets a descendant/child step follow a sibling: `a + b c` roots
/// its `b` at the sibling anchor, then descends to `c`. `anchor_bit == None` = the leftmost segment.
pub(super) fn seg_match_anchored(
    seg: &Segment, stack: &[OpenElem], si: usize, floor: usize, anchor_bit: Option<usize>,
) -> bool {
    #[allow(clippy::too_many_arguments)] // a tight recursive walk; threading state beats a struct here
    fn go(
        parts: &[Compound], combs: &[Comb], pi: usize, stack: &[OpenElem], si: usize, floor: usize,
        anchor_bit: Option<usize>, strict: bool,
    ) -> bool {
        if si < floor || !compound_matches(&parts[pi], &stack[si]) {
            return false;
        }
        if pi == 0 {
            // A `strict` (relative `.//`) segment excludes the context node itself: the leftmost
            // compound must bind strictly BELOW the floor, so reject a bind AT `floor`.
            if strict && si == floor {
                return false;
            }
            return match anchor_bit {
                None => true,
                Some(b) => stack[si].anchor & (1u64 << b) != 0,
            };
        }
        match combs[pi - 1] {
            Comb::Child => si > floor && go(parts, combs, pi - 1, stack, si - 1, floor, anchor_bit, strict),
            Comb::Descendant => {
                (floor..si).rev().any(|aj| go(parts, combs, pi - 1, stack, aj, floor, anchor_bit, strict))
            }
            _ => false,
        }
    }
    go(&seg.parts, &seg.combs, seg.parts.len() - 1, stack, si, floor, anchor_bit, seg.strict)
}

/// Does sub-selector `seg` (scoped at `floor`) hit `stack[top]`? For a `subtree` terminal (`E ::text`),
/// any element from the container depth down to `top` may be the subject; otherwise only `top` itself.
/// An unsupported sub (`None`) never hits.
pub(super) fn sub_hits(
    seg: &Option<Segment>, subtree: bool, stack: &[OpenElem], top: usize, floor: usize,
) -> bool {
    match seg {
        None => false,
        Some(s) if subtree => (floor..=top).any(|si| seg_match(s, stack, si, floor)),
        Some(s) => seg_match(s, stack, top, floor),
    }
}

/// Is sibling gate `bit` open on `parent`? `adjacent` reads the immediately-preceding child's trigger
/// (`+`), else any preceding child's (`~`).
pub(super) fn gate_open(parent: Option<&OpenElem>, bit: usize, adjacent: bool) -> bool {
    match parent {
        Some(p) => (if adjacent { p.prev } else { p.seen }) & (1u64 << bit) != 0,
        None => false,
    }
}

/// Evaluate one selector against `stack[top]`: `(is_subject_match, sibling_trigger_bits)`. Each
/// segment's sibling gate is checked at its own first compound (inside [`seg_match_anchored`]), so a
/// descendant/child step after a sibling combinator (`a + b c`) resolves against the sibling element's
/// parent, not the subject's.
pub(super) fn eval_one(cs: &CSel, stack: &[OpenElem], top: usize) -> (bool, u64) {
    // An over-budget entry never matches and (crucially) has no `seg_bits`, so short-circuit BEFORE
    // any `1 << seg_bits[..]` shift could run — that is the whole point of the DEAD flag.
    if cs.dead {
        return (false, 0);
    }
    // Flat/container selectors are unscoped: floor 0.
    if cs.segments.len() == 1 {
        return (seg_match(&cs.segments[0], stack, top, 0), 0);
    }
    // Each non-leftmost segment roots its first compound at a sibling anchor (bit seg_bits[i-1],
    // established at the anchor element's open by `compute_anchors`); a matched subject at `top` sets
    // that segment's own trigger for the boundary to its right.
    let mut trig = 0u64;
    if seg_match_anchored(&cs.segments[0], stack, top, 0, None) {
        trig |= 1 << cs.seg_bits[0];
    }
    let last = cs.segments.len() - 1;
    for i in 1..last {
        if seg_match_anchored(&cs.segments[i], stack, top, 0, Some(cs.seg_bits[i - 1])) {
            trig |= 1 << cs.seg_bits[i];
        }
    }
    let subj = seg_match_anchored(&cs.segments[last], stack, top, 0, Some(cs.seg_bits[last - 1]));
    (subj, trig)
}
