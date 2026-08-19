//! One-sided 64-bit signatures: a Bloom filter that lets the matcher reject most (element, compound)
//! pairs before it touches a single string. The shape is WebKit/Blink's `SelectorFilter`, narrowed to
//! this engine's kernel.
//!
//! Every start tag builds a signature from the three things a compound can require *positively* — its
//! tag name, its `id`, and each of its `class` tokens — mapping each to TWO bit positions and ORing
//! them in. Each compiled compound gets a `req` built by the same hash over the same three sources. A
//! compound whose `req` has a bit the element lacks cannot match, so `compound_matches` opens with one
//! AND and one compare (see [`super::matching::compound_matches`]).
//!
//! # The two rules this file holds
//!
//! **One-sided.** A set bit is NECESSARY, never sufficient: collisions and the OR make false POSITIVES
//! normal, and they cost only the exact comparisons that always ran. A false NEGATIVE is a silently
//! dropped value, which the no-fallback contract cannot absorb. This module may only turn a `false` into
//! a faster `false`.
//!
//! **The hash mirrors the comparison it guards.** `compound_matches` tests the tag with
//! `eq_ignore_ascii_case` and the id/class tokens with `==`, so tag bytes are ASCII-folded on both sides
//! and id/class bytes hashed verbatim on both. The class TOKENIZER is part of that: the element side
//! hashes the tokens [`super::OpenElem::class_tokens`] yields and `has_class` compares the tokens it
//! yields, from the one function. A wider split (Unicode whitespace) would hash `Foo` and `bar` out of
//! `class="Foo\u{a0}bar"` while the predicate compares one token, so `.Foo\u{a0}bar` would be filtered
//! out of a page it matches.
//!
//! Each source has its own hash basis, so a class `header` does not satisfy a `header` tag requirement.
//! That is selectivity, not correctness.
//!
//! Signatures are built per START TAG, so each KIND of bit is included only for a schema that performs at
//! least [`BITS_MIN`] tests of that kind — dropped from the element side and every `req` together, which
//! is what keeps it sound.

use crate::selector::Compound;

const FNV_PRIME: u64 = 0x0000_0100_0000_01b3;
const FNV_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
// Per-source bases (FNV's basis, perturbed) keep the three kinds in separate hash spaces.
const BASIS_TAG: u64 = FNV_BASIS;
const BASIS_ID: u64 = FNV_BASIS ^ 0x9e37_79b9_7f4a_7c15;
const BASIS_CLASS: u64 = FNV_BASIS ^ 0xc2b2_ae3d_27d4_eb4f;

/// FNV-1a over `bytes`, ASCII-lowercasing as it goes when `fold`. Folding must match the guarded
/// predicate exactly: `eq_ignore_ascii_case` folds only ASCII, and so does `to_ascii_lowercase` here —
/// a non-ASCII byte hashes verbatim on both sides.
///
fn digest(basis: u64, bytes: &[u8], fold: bool) -> u64 {
    let mut h = basis;
    for &b in bytes {
        let b = if fold { b.to_ascii_lowercase() } else { b };
        h = (h ^ b as u64).wrapping_mul(FNV_PRIME);
    }
    h
}

/// The two bits a token claims. FNV-1a's low bits are not independent enough to slice twice, so the
/// digest gets a finalizing mix first; the two 6-bit picks are then effectively independent, which is
/// what makes a 64-bit filter with `k = 2` worth more than one with `k = 1`.
fn bits(h: u64) -> u64 {
    let mut m = h;
    m ^= m >> 33;
    m = m.wrapping_mul(0xff51_afd7_ed55_8ccd);
    m ^= m >> 33;
    (1u64 << (m & 63)) | (1u64 << ((m >> 6) & 63))
}

/// Bits for an element or compound TAG NAME — ASCII-folded (`eq_ignore_ascii_case`).
pub(super) fn tag_bits(tag: &[u8]) -> u64 {
    bits(digest(BASIS_TAG, tag, true))
}

/// Bits for an `id` value — verbatim (`compound_matches` compares ids with `==`).
pub(super) fn id_bits(id: &[u8]) -> u64 {
    bits(digest(BASIS_ID, id, false))
}

/// Bits for one `class` token — verbatim (`has_class` compares tokens with `==`).
pub(super) fn class_bits(cls: &[u8]) -> u64 {
    bits(digest(BASIS_CLASS, cls, false))
}

/// The signature bits compound `c` REQUIRES of an element, from its own positive tag/id/classes.
///
/// Everything else on the compound contributes nothing, and each omission is load-bearing:
///   * `tag == None`/`"*"` — the universal selector requires no tag;
///   * `negations` — a `:not(.x)` requirement is INVERTED; requiring `.x`'s bits would reject exactly
///     the elements that match;
///   * `is_groups` — `:is(a, b)` is an OR, so no single alternative is required (their own `req`s are
///     set for when `compound_matches` recurses into them, where each IS the whole question);
///   * `attrs` — `[href^="/"]` is a substring/prefix test, not the token equality the hash models
///     (and `[class~=x]`/`[id=x]` go through the generic attribute path, whose value the signature
///     does not summarize);
///   * `positional`/`reverse`/`has`/`text_pred` — not element-identity at all.
pub(super) fn compound_req(c: &Compound, o: Opts) -> u64 {
    let mut req = 0u64;
    if let (true, Some(t)) = (o.tag, c.tag.as_deref().filter(|t| *t != "*")) {
        req |= tag_bits(t.as_bytes());
    }
    if let Some(id) = &c.id {
        req |= id_bits(id.as_bytes());
    }
    if o.class {
        for cl in &c.classes {
            req |= class_bits(cl.as_bytes());
        }
    }
    req
}

/// Which KINDS of bit a schema's signatures use. Applied to both sides — the element signature and
/// every `req` — which is what keeps a disabled kind sound: if no `req` carries class bits, an element
/// signature without class bits cannot fail to satisfy one. Soundness therefore does not depend on the
/// cost estimate below being right, only on ONE decision driving both sides.
#[derive(Clone, Copy)]
pub(super) struct Opts {
    pub tag: bool,
    pub class: bool,
}

/// Hashing a kind costs per element, so a schema performing a single test of that kind cannot amortize
/// it: unconditional class bits measured ~11% slower at one class-led field and ~4% faster at two.
/// Below the threshold the bits leave BOTH sides, which is what keeps it sound.
pub(super) const BITS_MIN: usize = 2;

/// `id` has no threshold: it is one bounded `attrs` lookup and one hash of one value, and a schema that
/// names an id is asking about a near-unique attribute — the filter's best case. The costly kinds are
/// the ones counted below.
///
/// How many tag / class-membership tests this compound performs, counting the nested compounds that get
/// their own `compound_matches` call. Feeds the [`BITS_MIN`] decision, which is a COST estimate:
/// over- or under-counting can only buy or forgo the optimization, never change a match (see [`Opts`]).
pub(super) fn tests(c: &Compound) -> (usize, usize) {
    let mut tag = usize::from(c.tag.as_deref().is_some_and(|t| t != "*"));
    let mut class = c.classes.len();
    let nested = c
        .negations
        .iter()
        .chain(c.is_groups.iter().flatten())
        .chain(c.has.as_ref().map(|h| &*h.inner));
    for n in nested {
        let (t, cl) = tests(n);
        tag += t;
        class += cl;
    }
    (tag, class)
}

/// What a compiled schema's compounds actually require of an element, accumulated by the same walk
/// that fills their `req`s ([`set_req`]).
///
/// Each flag OFF licenses the scan to skip building that part of the element signature, and the licence
/// is sound because the flag and the `req`s come out of ONE walk under one [`Opts`]: a flag is off
/// exactly when no `req` carries that kind of bit, so leaving those bits out of an element's signature
/// cannot make a `req` unsatisfiable. What would break it is a second, separately-derived answer to
/// "does this schema use classes?" — then the element side and the `req` side could disagree.
///
/// The flags are worth having because the work is per START TAG: class bits are `O(class tokens)` of
/// hashing, tag bits one short hash, id bits an `attrs` lookup — all wasted on a schema whose compounds
/// never ask.
#[derive(Clone, Copy, Default)]
pub(super) struct Wants {
    mask: u64,       // OR of every compiled `req`
    pub tag: bool,   // some compound names a concrete tag (and tag bits are enabled)
    pub class: bool, // some compound carries a class token, so `has_class` will be asked
    pub id: bool,    // some compound carries an id
}

impl Wants {
    /// Does any compound require any bit? If not, every `req` is 0 and the whole filter is a no-op, so
    /// the scan can skip signatures entirely.
    pub(super) fn any(&self) -> bool {
        self.mask != 0
    }

    /// Build every kind of bit — what the matching-kernel unit tests use, so their elements carry the
    /// most selective signature any schema could ask for (the strictest test of one-sidedness).
    #[cfg(test)]
    pub(super) fn all() -> Wants {
        Wants { mask: u64::MAX, tag: true, class: true, id: true }
    }
}

/// [`set_req`] for the kernel unit tests, which build compounds one at a time and get their elements'
/// signatures from [`Wants::all`] rather than from a compiled schema.
#[cfg(test)]
pub(super) fn set_req_for_test(c: &mut Compound) {
    set_req(c, Opts { tag: true, class: true }, &mut Wants::default());
}

/// Fill in `c.req` and the `req` of every compound nested inside it (`:not(...)` args and
/// `:is(...)`/`:where(...)` alternatives are matched by their own `compound_matches` call, so each is
/// its own filterable question), accumulating what the scan must build into `w`.
///
/// Setting the reqs and accumulating `w` in ONE walk is deliberate: a compound this pass never reaches
/// keeps `req == 0` (the filter is a no-op for it) *and* contributes nothing to `w`, so forgetting one
/// can only cost speed. `w` must never be re-derived from anything but the reqs actually set here —
/// that is the one way this filter turns into dropped values. (`o` is different in kind: a cost estimate
/// the caller may compute however it likes, because it constrains BOTH sides equally.)
pub(super) fn set_req(c: &mut Compound, o: Opts, w: &mut Wants) {
    c.req = compound_req(c, o);
    w.mask |= c.req;
    w.tag |= o.tag && c.tag.as_deref().is_some_and(|t| t != "*");
    w.class |= o.class && !c.classes.is_empty();
    w.id |= c.id.is_some();
    for neg in &mut c.negations {
        set_req(neg, o, w);
    }
    for group in &mut c.is_groups {
        for alt in group {
            set_req(alt, o, w);
        }
    }
}
