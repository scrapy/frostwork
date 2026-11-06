//! The two lists HTML5 tree construction keeps, the stack of open elements (13.2.4.3) and the list of
//! active formatting elements (13.2.4.4), each with the indexes its questions are answered from. A scope
//! check ("is a `<p>` in button scope"), a lookup by element and Noah's Ark are asked on every tag, so a
//! walk costs the depth of the stack or the length of the list each time: quadratic on a deep or a long
//! document. Here each is a comparison of two positions or a count, and the indexes change only where
//! the list does.
//!
//! In a test build every entry either list hands out is counted ([`reads`]), so a test can bound the
//! work a document costs rather than its wall time.

use std::collections::hash_map::Entry;
use std::collections::HashMap;
use std::hash::{BuildHasher, Hasher};

use super::tags::{Tag, TAGS};
use super::{Node, Ns, StartTok};
use crate::implied_close::{BUTTON_SCOPE, LIST_ITEM_SCOPE, SCOPE, SPECIAL, TABLE_SCOPE};
use crate::matcher::{EncodedInput, SourcePrefix};

#[cfg(test)]
thread_local! {
    static READS: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
}

/// Count `n` entries read from either list (test builds only).
#[inline]
fn read(n: usize) {
    #[cfg(test)]
    READS.with(|r| r.set(r.get() + n as u64));
    #[cfg(not(test))]
    let _ = n;
}

/// The entries read from either list on this thread so far.
#[cfg(test)]
pub(crate) fn reads() -> u64 {
    READS.with(std::cell::Cell::get)
}

const NOT_OPEN: u32 = u32::MAX;

/// What a scope check stops at: the elements of one of the spec's scope lists, the special elements,
/// and the special elements other than `<address>`, `<div>` and `<p>` (where a `<li>`, `<dd>` or `<dt>`
/// start tag stops looking for one to close).
#[derive(Clone, Copy)]
pub(super) enum Bound {
    Scope,
    Button,
    ListItem,
    Table,
    Special,
    ListItemStart,
}

const BOUNDS: usize = 6;

fn bounds_of(n: &Node<'_>) -> [bool; BOUNDS] {
    let special = n.cat & SPECIAL != 0;
    [
        n.cat & SCOPE != 0,
        n.cat & BUTTON_SCOPE != 0,
        n.cat & LIST_ITEM_SCOPE != 0,
        n.cat & TABLE_SCOPE != 0,
        special,
        special && !matches!(n.tag, Tag::Address | Tag::Div | Tag::P),
    ]
}

fn fnv(h: u64, bytes: impl Iterator<Item = u8>) -> u64 {
    bytes.fold(h, |h, b| (h ^ u64::from(b)).wrapping_mul(0x100_0000_01b3))
}

const FNV_SEED: u64 = 0xcbf2_9ce4_8422_2325;

/// The hasher of the two lists' maps, whose keys are hashes already. Each word is mixed into a key drawn
/// once per list from the standard library's random state, so a page cannot aim its keys at one bucket,
/// for a few multiplications per lookup where SipHash takes several rounds.
#[derive(Clone, Copy)]
struct Keyed(u64);

impl Default for Keyed {
    fn default() -> Self {
        Keyed(std::hash::RandomState::new().hash_one(0u8))
    }
}

impl BuildHasher for Keyed {
    type Hasher = Mixer;

    fn build_hasher(&self) -> Mixer {
        Mixer(self.0)
    }
}

struct Mixer(u64);

impl Hasher for Mixer {
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.write_u64(u64::from(b));
        }
    }

    fn write_u32(&mut self, n: u32) {
        self.write_u64(u64::from(n));
    }

    fn write_u64(&mut self, n: u64) {
        self.0 = mix(self.0 ^ n);
    }

    fn finish(&self) -> u64 {
        self.0
    }
}

/// An HTML element no [`Tag`] names is indexed by a hash of its lowercased name.
fn other_name(n: &Node<'_>) -> Option<u64> {
    (n.tag == Tag::Other && n.ns == Ns::Html).then(|| fnv(FNV_SEED, n.name.iter().map(u8::to_ascii_lowercase)))
}

/// The stack of open elements, bottom first. Besides the entries it keeps, by element uid, where each
/// open one is, and per tag (per name hash for an HTML element no tag names) and per [`Bound`] the uids
/// of the open entries in stack order, so the topmost of each is its last uid.
#[derive(Default)]
pub(super) struct OpenStack<'a> {
    nodes: Vec<Node<'a>>,
    at: Vec<u32>,
    by_tag: Vec<Vec<u32>>,
    by_name: HashMap<u64, Vec<u32>, Keyed>,
    bounds: [Vec<u32>; BOUNDS],
}

impl<'a> OpenStack<'a> {
    pub(super) fn len(&self) -> usize {
        self.nodes.len()
    }

    pub(super) fn is_empty(&self) -> bool {
        self.nodes.is_empty()
    }

    pub(super) fn last(&self) -> Option<&Node<'a>> {
        read(1);
        self.nodes.last()
    }

    pub(super) fn first(&self) -> Option<&Node<'a>> {
        read(1);
        self.nodes.first()
    }

    pub(super) fn get(&self, i: usize) -> Option<&Node<'a>> {
        read(1);
        self.nodes.get(i)
    }

    /// Every entry, bottom first.
    pub(super) fn iter(&self) -> impl DoubleEndedIterator<Item = &Node<'a>> + ExactSizeIterator {
        self.nodes.iter().inspect(|_| read(1))
    }

    /// Where the open element `uid` is.
    pub(super) fn index_of(&self, uid: u32) -> Option<usize> {
        read(1);
        self.at.get(uid as usize).copied().filter(|&k| k != NOT_OPEN).map(|k| k as usize)
    }

    /// The topmost HTML element with `tag` (not [`Tag::Other`]).
    pub(super) fn topmost(&self, tag: Tag) -> Option<usize> {
        let uid = *self.by_tag.get(tag as usize)?.last()?;
        self.index_of(uid)
    }

    /// The topmost HTML element named `name` that no [`Tag`] names.
    pub(super) fn topmost_named(&self, name: &[u8]) -> Option<usize> {
        let list = self.by_name.get(&fnv(FNV_SEED, name.iter().map(u8::to_ascii_lowercase)))?;
        list.iter().rev().filter_map(|&uid| self.index_of(uid)).find(|&k| self[k].name.eq_ignore_ascii_case(name))
    }

    /// The topmost HTML element with any of `tags`.
    pub(super) fn topmost_of(&self, tags: &[Tag]) -> Option<usize> {
        tags.iter().filter_map(|&t| self.topmost(t)).max()
    }

    /// The open HTML elements with `tag`, bottom first.
    pub(super) fn all(&self, tag: Tag) -> impl Iterator<Item = usize> + '_ {
        self.by_tag.get(tag as usize).into_iter().flatten().filter_map(|&uid| self.index_of(uid))
    }

    /// `i`, if no element of `bound` is above it: "has an element in scope" for the entry at `i`, where
    /// the entry itself may be one of the boundary.
    pub(super) fn scoped(&self, i: Option<usize>, bound: Bound) -> Option<usize> {
        let i = i?;
        match self.bounds[bound as usize].last().and_then(|&uid| self.index_of(uid)) {
            Some(b) if b > i => None,
            _ => Some(i),
        }
    }

    /// Make room in the indexes for `node`, which is about to enter the stack at `k`.
    fn make_room(&mut self, node: &Node<'a>, k: usize) {
        let uid = node.uid as usize;
        if self.at.len() <= uid {
            self.at.resize(uid + 1, NOT_OPEN);
        }
        self.at[uid] = k as u32;
        if node.tag != Tag::Other && self.by_tag.len() <= node.tag as usize {
            self.by_tag.resize_with(node.tag as usize + 1, Vec::new);
        }
    }

    /// Apply `f`, with the stack index of every uid, to each index list `node` is filed in: its tag's
    /// (or its name's), and that of every [`Bound`] it is one of. A name's list leaves the map once empty.
    fn each_list(&mut self, node: &Node<'a>, mut f: impl FnMut(&mut Vec<u32>, &[u32])) {
        if node.tag != Tag::Other {
            f(&mut self.by_tag[node.tag as usize], &self.at);
        }
        if let Some(h) = other_name(node) {
            let list = self.by_name.entry(h).or_default();
            f(list, &self.at);
            if list.is_empty() {
                self.by_name.remove(&h);
            }
        }
        for (b, is) in bounds_of(node).into_iter().enumerate() {
            if is {
                f(&mut self.bounds[b], &self.at);
            }
        }
    }

    pub(super) fn push(&mut self, node: Node<'a>) {
        self.make_room(&node, self.nodes.len());
        self.each_list(&node, |list, _| list.push(node.uid));
        self.nodes.push(node);
    }

    pub(super) fn pop(&mut self) -> Option<Node<'a>> {
        let node = self.nodes.pop()?;
        self.at[node.uid as usize] = NOT_OPEN;
        self.each_list(&node, |list, _| {
            let popped = list.pop();
            debug_assert_eq!(popped, Some(node.uid), "the stack's indexes are out of order");
        });
        Some(node)
    }

    /// Take the entry at `k` out of the stack (it can be anywhere in it).
    pub(super) fn remove(&mut self, k: usize) -> Node<'a> {
        let node = self.nodes.remove(k);
        self.at[node.uid as usize] = NOT_OPEN;
        for n in &self.nodes[k..] {
            read(1);
            self.at[n.uid as usize] -= 1;
        }
        self.each_list(&node, |list, _| {
            if let Some(j) = list.iter().rposition(|&u| {
                read(1);
                u == node.uid
            }) {
                list.remove(j);
            }
        });
        node
    }

    /// Put `node` into the stack at `k`.
    pub(super) fn insert(&mut self, k: usize, node: Node<'a>) {
        for n in &self.nodes[k..] {
            read(1);
            self.at[n.uid as usize] += 1;
        }
        self.make_room(&node, k);
        self.nodes.insert(k, node);
        // A list holds uids in stack order, so the new one goes after every uid below `k`.
        self.each_list(&node, |list, at| {
            let mut j = list.len();
            while j > 0 && at[list[j - 1] as usize] as usize > k {
                read(1);
                j -= 1;
            }
            list.insert(j, node.uid);
        });
    }

    /// Make `node` the entry at `k`, in place of what was there.
    pub(super) fn replace(&mut self, k: usize, node: Node<'a>) {
        let old = self.nodes[k];
        if old.uid == node.uid {
            debug_assert!(old.tag == node.tag && old.cat == node.cat, "an entry changed what it indexes by");
            self.nodes[k] = node;
            return;
        }
        self.remove(k);
        self.insert(k, node);
    }
}

impl<'a> std::ops::Index<usize> for OpenStack<'a> {
    type Output = Node<'a>;

    fn index(&self, i: usize) -> &Node<'a> {
        read(1);
        &self.nodes[i]
    }
}

/// Changing an entry's tag, category or uid in place would leave the indexes behind; that is
/// [`OpenStack::replace`]'s.
impl<'a> std::ops::IndexMut<usize> for OpenStack<'a> {
    fn index_mut(&mut self, i: usize) -> &mut Node<'a> {
        read(1);
        &mut self.nodes[i]
    }
}

pub(super) type RawAttr<'a> = (&'a [u8], Option<&'a [u8]>);

/// The start tag a formatting element was created for, kept so a clone can be made from it. Its
/// attributes are `attrs` of [`Formatting::attrs`]; `span` is the tag's source, which a clone's outer
/// HTML begins with; `ark` is what Noah's Ark compares it by ([`ark_key`]).
pub(super) struct FmtTok<'a> {
    pub(super) name: &'a [u8],
    pub(super) tag: Tag,
    attrs: std::ops::Range<usize>,
    pub(super) span: SourcePrefix,
    ark: u64,
}

/// An entry of the list: a marker, or the element `uid` made for the start tag `tok` (an index into
/// [`Formatting::toks`]).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(super) enum Afe {
    Marker,
    El { uid: u32, tok: u32 },
}

/// splitmix64's finalizer, which spreads a word's bits over all 64.
fn mix(mut h: u64) -> u64 {
    h = (h ^ (h >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    h = (h ^ (h >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    h ^ (h >> 31)
}

/// The key Noah's Ark compares two formatting elements by: their tag, and their attributes as a set of
/// (lowercased name, decoded value) pairs, the first of a repeated name. Equal elements have equal keys;
/// [`same_attrs`] settles a collision.
fn ark_key<'x>(tag: Tag, attrs: impl Iterator<Item = (&'x [u8], std::borrow::Cow<'x, str>)>) -> u64 {
    let (mut sum, mut n) = (0u64, 0u64);
    for (name, value) in attrs {
        // 0xff ends the name: no lowercased name byte is one. Each pair is mixed before the order-free sum.
        let h = fnv(fnv(FNV_SEED, name.iter().map(u8::to_ascii_lowercase).chain([0xff])), value.bytes());
        sum = sum.wrapping_add(mix(h));
        n += 1;
    }
    mix(sum ^ mix(n << 8 | tag as u64))
}

/// Do two formatting tokens carry the same attributes, as Noah's Ark compares them: by name, the first
/// of a repeated name, in any order, with equal values.
fn same_attrs<'a>(input: EncodedInput<'a>, a: &[RawAttr<'a>], b: &[RawAttr<'a>]) -> bool {
    let first = |attrs: &[RawAttr<'a>], j: usize| !attrs[..j].iter().any(|(n, _)| n.eq_ignore_ascii_case(attrs[j].0));
    let names = |attrs: &[RawAttr<'a>]| (0..attrs.len()).filter(|&j| first(attrs, j)).count();
    names(a) == names(b) && (0..a.len()).filter(|&j| first(a, j)).all(|j| {
        let (n, v) = a[j];
        b.iter().find(|(n2, _)| n.eq_ignore_ascii_case(n2)).is_some_and(|&(_, v2)| {
            let (v, v2) = (v.unwrap_or(b""), v2.unwrap_or(b""));
            v == v2 || input.attr_value(v) == input.attr_value(v2)
        })
    })
}

/// The list of active formatting elements, and the start tags its elements were made for. Besides the
/// entries it keeps where each marker is and, per segment (the entries after a marker, or all of them
/// before the first), how many entries it holds of each tag and of each [`ark_key`], so Noah's Ark and
/// "is there an `<a>` after the last marker" are counts rather than scans.
#[derive(Default)]
pub(super) struct Formatting<'a> {
    list: Vec<Afe>,
    markers: Vec<usize>,
    /// Entries by tag, `TAGS` counts per segment: `tags[segment * TAGS + tag]`.
    tags: Vec<u32>,
    /// Entries by segment and Noah's Ark key.
    arks: HashMap<(u32, u64), u32, Keyed>,
    /// Every formatting element's start tag, in document order; an element and its copies share one.
    pub(super) toks: Vec<FmtTok<'a>>,
    /// The attributes of every start tag in `toks`, one after another.
    attrs: Vec<RawAttr<'a>>,
}

impl<'a> Formatting<'a> {
    pub(super) fn len(&self) -> usize {
        self.list.len()
    }

    pub(super) fn last(&self) -> Option<&Afe> {
        read(1);
        self.list.last()
    }

    /// The index of the first entry after the last marker.
    fn after_last_marker(&self) -> usize {
        self.markers.last().map_or(0, |&k| k + 1)
    }

    /// How many entries after the last marker have the key `ark`.
    fn alike(&self, ark: u64) -> u32 {
        self.arks.get(&(self.markers.len() as u32, ark)).copied().unwrap_or(0)
    }

    /// How many entries of segment `seg` were made for a `tag` start tag.
    fn tagged(&self, seg: u32, tag: Tag) -> u32 {
        self.tags.get(seg as usize * TAGS + tag as usize).copied().unwrap_or(0)
    }

    /// Is there an entry with `tag` after the last marker?
    fn has_tag(&self, tag: Tag) -> bool {
        self.tagged(self.markers.len() as u32, tag) > 0
    }

    /// The segment the entry at `k` is in: the number of markers before it.
    fn segment(&self, k: usize) -> u32 {
        self.markers.partition_point(|&m| m < k) as u32
    }

    fn count(&mut self, seg: u32, e: Afe, up: bool) {
        let Afe::El { tok, .. } = e else { return };
        let (ark, tag) = (self.toks[tok as usize].ark, self.toks[tok as usize].tag);
        let i = seg as usize * TAGS + tag as usize;
        if self.tags.len() <= i {
            self.tags.resize((seg as usize + 1) * TAGS, 0);
        }
        if up {
            self.tags[i] += 1;
            *self.arks.entry((seg, ark)).or_insert(0) += 1;
            return;
        }
        self.tags[i] -= 1;
        if let Entry::Occupied(mut c) = self.arks.entry((seg, ark)) {
            if *c.get() == 1 {
                c.remove();
            } else {
                *c.get_mut() -= 1;
            }
        }
    }

    /// Keep the start tag `t` of a formatting element, and return its index in [`Self::toks`].
    pub(super) fn add_token(&mut self, input: EncodedInput<'a>, t: &StartTok<'a, '_>) -> u32 {
        let tok = self.toks.len() as u32;
        let attrs = t.attrs;
        let firsts = (0..attrs.len()).filter(|&j| !attrs[..j].iter().any(|(n, _)| n.eq_ignore_ascii_case(attrs[j].0)));
        let ark = ark_key(t.tag, firsts.map(|j| (attrs[j].0, input.attr_value(attrs[j].1.unwrap_or(b"")))));
        let from = self.attrs.len();
        self.attrs.extend_from_slice(attrs);
        let span = SourcePrefix::new(t.start, t.end);
        self.toks.push(FmtTok { name: t.name, tag: t.tag, attrs: from..self.attrs.len(), span, ark });
        tok
    }

    /// The attributes of the start tag `tok`.
    pub(super) fn attrs_of(&self, tok: u32) -> &[RawAttr<'a>] {
        &self.attrs[self.toks[tok as usize].attrs.clone()]
    }

    /// Push the element `uid`, made for token `tok`. Noah's Ark: with three alike after the last marker
    /// already, the earliest of them leaves. The list counts entries by key, so only then is it searched,
    /// from the end, for as many as have the key.
    pub(super) fn push_element(&mut self, input: EncodedInput<'a>, uid: u32, tok: u32) {
        let t = &self.toks[tok as usize];
        // Three alike are three of the tag, which the tag count answers without hashing the key.
        let mut keyed = if self.tagged(self.markers.len() as u32, t.tag) >= 3 { self.alike(t.ark) } else { 0 };
        if keyed >= 3 {
            let (mut same, mut earliest) = (0, None);
            let mut k = self.len();
            while keyed > 0 && k > self.after_last_marker() {
                k -= 1;
                if let Afe::El { tok: o, .. } = self[k] {
                    let other = &self.toks[o as usize];
                    if other.ark == t.ark {
                        keyed -= 1;
                        if other.tag == t.tag && same_attrs(input, self.attrs_of(o), self.attrs_of(tok)) {
                            same += 1;
                            earliest = Some(k);
                        }
                    }
                }
            }
            if same >= 3 {
                self.remove(earliest.expect("three of a kind"));
            }
        }
        self.push(Afe::El { uid, tok });
    }

    pub(super) fn push(&mut self, e: Afe) {
        if e == Afe::Marker {
            self.markers.push(self.list.len());
        } else {
            self.count(self.markers.len() as u32, e, true);
        }
        self.list.push(e);
    }

    /// Clear the list up to the last marker.
    pub(super) fn clear_to_marker(&mut self) {
        while let Some(e) = self.list.pop() {
            read(1);
            if e == Afe::Marker {
                // The segment's counts are all back to zero, ready for the next segment at its depth.
                let seg = self.markers.len();
                debug_assert!(self.tags.iter().skip(seg * TAGS).take(TAGS).all(|&c| c == 0));
                self.markers.pop();
                break;
            }
            self.count(self.markers.len() as u32, e, false);
        }
    }

    /// Take the element entry at `k` out of the list (a marker leaves only by [`Self::clear_to_marker`]).
    pub(super) fn remove(&mut self, k: usize) -> Afe {
        let e = self.list.remove(k);
        debug_assert!(e != Afe::Marker, "a marker leaves only by clearing to it");
        let seg = self.segment(k);
        self.count(seg, e, false);
        for m in self.markers.iter_mut().filter(|m| **m > k) {
            *m -= 1;
        }
        e
    }

    /// Put an element entry into the list at `k`.
    pub(super) fn insert(&mut self, k: usize, e: Afe) {
        debug_assert!(e != Afe::Marker, "a marker is only ever pushed");
        for m in self.markers.iter_mut().filter(|m| **m >= k) {
            *m += 1;
        }
        let seg = self.segment(k);
        self.count(seg, e, true);
        self.list.insert(k, e);
    }

    /// Make the entry at `k` stand for another element made for the same token.
    pub(super) fn set(&mut self, k: usize, e: Afe) {
        debug_assert!(
            matches!((self.list[k], e), (Afe::El { tok: a, .. }, Afe::El { tok: b, .. }) if a == b),
            "an entry is replaced only by a copy of its element"
        );
        self.list[k] = e;
    }

    /// The index of the entry for element `uid`, searching from the end.
    pub(super) fn index_of(&self, uid: u32) -> Option<usize> {
        self.list.iter().rposition(|e| {
            read(1);
            matches!(e, Afe::El { uid: u, .. } if *u == uid)
        })
    }

    /// The index of the last entry after the last marker that was made for a `tag` start tag.
    pub(super) fn last_of(&self, tag: Tag) -> Option<usize> {
        if !self.has_tag(tag) {
            return None;
        }
        let from = self.after_last_marker();
        let found = self.list[from..].iter().rposition(|e| {
            read(1);
            matches!(*e, Afe::El { tok, .. } if self.toks[tok as usize].tag == tag)
        });
        found.map(|k| k + from)
    }
}

impl std::ops::Index<usize> for Formatting<'_> {
    type Output = Afe;

    fn index(&self, k: usize) -> &Afe {
        read(1);
        &self.list[k]
    }
}

#[cfg(test)]
mod tests {
    /// The entries of either list read to extract `p::text` from `doc` in browser mode.
    fn work(doc: &str) -> u64 {
        let before = super::reads();
        crate::Plan::compile(&["p::text"], &[]).extract(doc.as_bytes(), None, None);
        super::reads() - before
    }

    /// A document's builder work grows with its length however deep it nests and however long its list
    /// of active formatting elements gets: twice the tags read about twice the entries, where a walk per
    /// tag reads four times as many. Counted, not timed, so a shared machine cannot make it flaky.
    #[test]
    fn builder_work_is_linear_in_depth_and_list_length() {
        type Shape = (&'static str, fn(usize) -> String);
        let shapes: [Shape; 9] = [
            ("nested divs", |n| "<div>".repeat(n)),
            ("distinct formatting elements in one paragraph", |n| {
                "<p>".to_string() + &(0..n).map(|i| format!("<b class={i}>")).collect::<String>()
            }),
            ("identical formatting elements in one paragraph", |n| "<p>".to_string() + &"<b class=k>".repeat(n)),
            ("blocks under a paragraph's button", |n| "<p><button>".to_string() + &"<span>".repeat(n) + &"<div>".repeat(n)),
            ("end tags that close nothing under deep spans", |n| "<span>".repeat(n) + &"</em></x-y></div></p>".repeat(n)),
            ("list items under deep divs", |n| "<div>".repeat(n) + &"<li>x</li>".repeat(n)),
            ("tables under deep divs", |n| "<div>".repeat(n) + &"<table></table>".repeat(n)),
            ("forms and templates under deep divs", |n| "<div>".repeat(n) + &"<form></form><template></template>".repeat(n)),
            ("text after a formatting element deep down", |n| "<b>".to_string() + &"<div>x".repeat(n)),
        ];
        for (name, make) in shapes {
            let (a, b) = (work(&make(10_000)), work(&make(20_000)));
            assert!(b <= a * 5 / 2, "{name}: {a} entries read at 10000, {b} at 20000");
        }
    }
}
