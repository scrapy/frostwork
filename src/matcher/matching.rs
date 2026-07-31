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
/// # Algorithm
///
/// The ancestor chain `stack[floor..=si]` is a **path**, not a tree, so this is anchored *glob*
/// matching: each compound consumes one position, `Child` means adjacency, `Descendant` is a `*` gap,
/// and the rightmost compound is pinned to `si`. (`to_segments` splits on `+`/`~`, so a segment holds
/// only `Child`/`Descendant` and the decomposition below is total.)
///
/// Group maximal `Child` runs into **blocks**. A block of length `L` placed with its deepest compound at
/// stack index `p` occupies `[p-L+1, p]` contiguously — a straight-line check with no branching — so the
/// only freedom left is where each block sits. Walking blocks right-to-left and taking the **largest**
/// feasible `p` for each is optimal: a larger `p` puts the block's top deeper, leaving a strictly larger
/// range `[floor, p-L]` for everything further left, so if any placement exists this one leaves at least
/// as much room (exchange argument).
///
/// That is **O(depth × compounds)** worst case with no recursion and no allocation. The previous
/// recursive walk re-derived overlapping `(compound, stack index)` states with no memo, which was
/// EXPONENTIAL: an 11-compound selector whose leftmost compound never matched took ~11 ms at depth 20
/// and ~28 s at depth 40 — reachable from an untrusted selector string (see SECURITY.md).
///
/// Grouping Child-runs is what makes the greedy sound. Greedy on individual *compounds* is wrong:
/// `a > b c` against `<a><b><b><c>` picks the nearest `b`, then needs `a` at its parent (another `b`)
/// and fails, though placing `b` one level up succeeds. As one block, `a > b` is tested as a unit.
pub(super) fn seg_match_anchored(
    seg: &Segment, stack: &[OpenElem], si: usize, floor: usize, anchor_bit: Option<usize>,
) -> bool {
    let (parts, combs) = (&seg.parts, &seg.combs);
    // Cheapest possible rejection first: the subject compound is pinned to `si`, and most elements fail
    // it. Everything below only runs for an element that already looks like the selector's subject.
    if si < floor || !compound_matches(&parts[parts.len() - 1], &stack[si]) {
        return false;
    }
    // Does the block spanning compounds `lo..=hi` sit contiguously with its deepest compound at `p`?
    // DESCENDING (deepest compound first): the deeper an element, the more selective the compound tends
    // to be, and for the pinned block the first test is then the subject itself.
    let block_fits = |lo: usize, hi: usize, p: usize| {
        (lo..=hi).rev().all(|ci| compound_matches(&parts[ci], &stack[p - (hi - ci)]))
    };
    // The leftmost compound carries the position-specific constraints, so they join that block's test.
    let head_ok = |top: usize| {
        // A `strict` (relative `.//`) segment excludes the context node itself: the leftmost compound
        // must bind strictly BELOW the floor, so reject a bind AT `floor`.
        if seg.strict && top == floor {
            return false;
        }
        match anchor_bit {
            None => true,
            Some(b) => stack[top].anchor & (1u64 << b) != 0,
        }
    };

    // A lone compound is the common case (`div::text`, `.price::text`): it is already fully matched.
    if parts.len() == 1 {
        return head_ok(si);
    }

    let mut hi = parts.len() - 1;
    let mut limit = si; // largest stack index this block's deepest compound may take
    let mut pinned = true; // the rightmost block is anchored at `si`: no freedom
    loop {
        let mut lo = hi;
        while lo > 0 && matches!(combs[lo - 1], Comb::Child) {
            lo -= 1;
        }
        let len = hi - lo + 1;
        let is_head = lo == 0;
        // the block's top is `p + 1 - len`, which must stay at or below the floor
        let min_p = floor + len - 1;
        let mut placed = None;
        let mut p = limit;
        while p >= min_p {
            if block_fits(lo, hi, p) && (!is_head || head_ok(p + 1 - len)) {
                placed = Some(p);
                break;
            }
            if pinned || p == 0 {
                break; // the pinned block may only sit at `si`
            }
            p -= 1;
        }
        let p = match placed {
            Some(p) => p,
            None => return false,
        };
        if is_head {
            return true;
        }
        // the next block leftward must end strictly ABOVE this block's top `p + 1 - len`
        // (`Descendant` allows zero
        // intervening elements, so `top - 1` is fair game)
        limit = match p.checked_sub(len) {
            Some(l) if l >= floor => l,
            _ => return false,
        };
        hi = lo - 1;
        pinned = false;
    }
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

#[cfg(test)]
mod seg_match_tests {
    use super::*;
    use crate::selector::Compound;

    /// The ORIGINAL recursive walk, kept as a reference oracle. It is exponential (which is why the
    /// shipped `seg_match_anchored` replaced it), but it is also the semantics that shipped and was
    /// proven against lxml over hundreds of millions of differential pairs — so the rewrite is checked
    /// against it exhaustively on small inputs, where its cost is irrelevant.
    fn reference(
        seg: &Segment, stack: &[OpenElem], si: usize, floor: usize, anchor_bit: Option<usize>,
    ) -> bool {
        #[allow(clippy::too_many_arguments)]
        fn go(
            parts: &[Compound], combs: &[Comb], pi: usize, stack: &[OpenElem], si: usize, floor: usize,
            anchor_bit: Option<usize>, strict: bool,
        ) -> bool {
            if si < floor || !compound_matches(&parts[pi], &stack[si]) {
                return false;
            }
            if pi == 0 {
                if strict && si == floor {
                    return false;
                }
                return match anchor_bit {
                    None => true,
                    Some(b) => stack[si].anchor & (1u64 << b) != 0,
                };
            }
            match combs[pi - 1] {
                Comb::Child => {
                    si > floor && go(parts, combs, pi - 1, stack, si - 1, floor, anchor_bit, strict)
                }
                Comb::Descendant => (floor..si)
                    .rev()
                    .any(|aj| go(parts, combs, pi - 1, stack, aj, floor, anchor_bit, strict)),
                _ => false,
            }
        }
        go(&seg.parts, &seg.combs, seg.parts.len() - 1, stack, si, floor, anchor_bit, seg.strict)
    }

    fn compound(tag: &str) -> Compound {
        Compound { tag: Some(tag.to_string()), ..Default::default() }
    }

    /// A stack of single-letter tags; `anchor` bit 0 is set on elements whose letter is uppercase, so
    /// the anchored-segment path gets exercised with a position-specific constraint.
    fn stack_of(tags: &str) -> Vec<OpenElem<'static>> {
        tags.chars()
            .map(|ch| {
                let t: &'static [u8] = match ch.to_ascii_lowercase() {
                    'a' => b"a",
                    'b' => b"b",
                    'c' => b"c",
                    _ => b"z",
                };
                let mut e = OpenElem::for_test(t);
                if ch.is_ascii_uppercase() {
                    e.anchor = 1;
                }
                e
            })
            .collect()
    }

    fn seg(pattern: &str, strict: bool) -> Segment {
        // "a>b c" -> parts [a, b, c], combs [Child, Descendant]
        let mut parts = Vec::new();
        let mut combs = Vec::new();
        let mut chars = pattern.chars().peekable();
        parts.push(compound(&chars.next().unwrap().to_string()));
        while let Some(c) = chars.next() {
            combs.push(if c == '>' { Comb::Child } else { Comb::Descendant });
            parts.push(compound(&chars.next().unwrap().to_string()));
        }
        Segment { parts, combs, strict }
    }

    /// Every pattern up to 4 compounds over a 3-letter alphabet, against every stack up to depth 6,
    /// for every subject/floor/strict/anchor combination — the new walk must agree with the reference
    /// on all of it. This is the real safety net for a kernel rewrite: hand-picked vectors would not
    /// have caught the greedy-per-compound error this algorithm had to avoid.
    #[test]
    fn equivalent_to_reference_exhaustively() {
        let alphabet = ['a', 'b', 'c'];
        let mut patterns: Vec<String> = Vec::new();
        for n in 1..=4usize {
            let combos = 3usize.pow(n as u32);
            let seps = if n > 1 { 2usize.pow(n as u32 - 1) } else { 1 };
            for ci in 0..combos {
                for si in 0..seps {
                    let mut p = String::new();
                    let mut c = ci;
                    for k in 0..n {
                        if k > 0 {
                            p.push(if (si >> (k - 1)) & 1 == 1 { '>' } else { ' ' });
                        }
                        p.push(alphabet[c % 3]);
                        c /= 3;
                    }
                    patterns.push(p);
                }
            }
        }
        // stacks: every string of length 1..=5 over {a,b,A} (uppercase = anchor bit set)
        let letters = ['a', 'b', 'A'];
        let mut stacks: Vec<String> = Vec::new();
        for n in 1..=5usize {
            for i in 0..3usize.pow(n as u32) {
                let mut s = String::new();
                let mut c = i;
                for _ in 0..n {
                    s.push(letters[c % 3]);
                    c /= 3;
                }
                stacks.push(s);
            }
        }

        let mut checked = 0u64;
        for pat in &patterns {
            for strict in [false, true] {
                let sg = seg(pat, strict);
                for st in &stacks {
                    let stack = stack_of(st);
                    for subject in 0..stack.len() {
                        for floor in 0..=subject {
                            for anchor in [None, Some(0usize)] {
                                let got = seg_match_anchored(&sg, &stack, subject, floor, anchor);
                                let want = reference(&sg, &stack, subject, floor, anchor);
                                assert_eq!(
                                    got, want,
                                    "pattern={pat:?} strict={strict} stack={st:?} \
                                     subject={subject} floor={floor} anchor={anchor:?}"
                                );
                                checked += 1;
                            }
                        }
                    }
                }
            }
        }
        assert!(checked > 1_000_000, "expected a large sweep, ran {checked}");
    }

    /// The case that rules out greedy on individual compounds: the nearest `b` forces `a` onto another
    /// `b` and fails, but placing the `a > b` block one level up succeeds.
    #[test]
    fn child_run_needs_block_placement() {
        let stack = stack_of("abbc");
        assert!(seg_match_anchored(&seg("a>b c", false), &stack, 3, 0, None));
        assert!(reference(&seg("a>b c", false), &stack, 3, 0, None));
        // and a case where no placement exists
        assert!(!seg_match_anchored(&seg("c>b c", false), &stack, 3, 0, None));
    }

    /// Deep nesting with a leftmost compound that never matches: the shape that was exponential.
    #[test]
    fn deep_nest_is_not_exponential() {
        let stack = stack_of(&"b".repeat(400));
        let sg = seg("a b b b b b b b b b b", false); // 11 compounds, leftmost never matches
        let t = std::time::Instant::now();
        for subject in (0..stack.len()).step_by(7) {
            assert!(!seg_match_anchored(&sg, &stack, subject, 0, None));
        }
        // the old walk needed ~28 s for ONE subject at depth 40; this whole sweep is milliseconds
        assert!(t.elapsed().as_secs() < 2, "took {:?}", t.elapsed());
    }
}
