//! The two lists HTML5 tree construction keeps, the stack of open elements (13.2.4.3) and the list of
//! active formatting elements (13.2.4.4), each with the indexes its questions are answered from. A scope
//! check ("is a `<p>` in button scope"), a lookup by element and Noah's Ark are asked on every tag, so a
//! walk costs the depth of the stack or the length of the list each time: quadratic on a deep or a long
//! document. Here each is a comparison of two positions or a count, and the indexes change only where
//! the list does.
//!
//! In a test build every entry either list hands out is counted ([`reads`]), so a test can bound the
//! work a document costs rather than its wall time.

use std::collections::HashMap;

use super::tags::Tag;
use super::{Afe, FmtTok, Node, Ns};
use crate::implied_close::{BUTTON_SCOPE, LIST_ITEM_SCOPE, SCOPE, SPECIAL, TABLE_SCOPE};

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
    by_name: HashMap<u64, Vec<u32>>,
    bounds: [Vec<u32>; BOUNDS],
}

impl<'a> OpenStack<'a> {
    pub(super) fn len(&self) -> usize {
        self.nodes.len()
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

    fn index(&mut self, node: &Node<'a>) {
        let uid = node.uid as usize;
        if self.at.len() <= uid {
            self.at.resize(uid + 1, NOT_OPEN);
        }
        if node.tag != Tag::Other {
            let t = node.tag as usize;
            if self.by_tag.len() <= t {
                self.by_tag.resize_with(t + 1, Vec::new);
            }
        }
    }

    /// Put `uid` into `list` (uids in stack order) where its stack index `k` goes.
    fn file(&self, list: &mut Vec<u32>, uid: u32, k: usize) {
        let mut j = list.len();
        while j > 0 && self.at[list[j - 1] as usize] as usize > k {
            read(1);
            j -= 1;
        }
        list.insert(j, uid);
    }

    fn unfile(list: &mut Vec<u32>, uid: u32) {
        if let Some(j) = list.iter().rposition(|&u| {
            read(1);
            u == uid
        }) {
            list.remove(j);
        }
    }

    pub(super) fn push(&mut self, node: Node<'a>) {
        self.index(&node);
        self.at[node.uid as usize] = self.nodes.len() as u32;
        if node.tag != Tag::Other {
            self.by_tag[node.tag as usize].push(node.uid);
        }
        if let Some(h) = other_name(&node) {
            self.by_name.entry(h).or_default().push(node.uid);
        }
        for (b, is) in bounds_of(&node).into_iter().enumerate() {
            if is {
                self.bounds[b].push(node.uid);
            }
        }
        self.nodes.push(node);
    }

    pub(super) fn pop(&mut self) -> Option<Node<'a>> {
        let node = self.nodes.pop()?;
        self.at[node.uid as usize] = NOT_OPEN;
        if node.tag != Tag::Other {
            let popped = self.by_tag[node.tag as usize].pop();
            debug_assert_eq!(popped, Some(node.uid), "the stack's tag index is out of order");
        }
        if let Some(h) = other_name(&node) {
            let list = self.by_name.get_mut(&h).expect("an open element's name is indexed");
            let popped = list.pop();
            debug_assert_eq!(popped, Some(node.uid), "the stack's name index is out of order");
            if list.is_empty() {
                self.by_name.remove(&h);
            }
        }
        for (b, is) in bounds_of(&node).into_iter().enumerate() {
            if is {
                let popped = self.bounds[b].pop();
                debug_assert_eq!(popped, Some(node.uid), "the stack's boundary index is out of order");
            }
        }
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
        if node.tag != Tag::Other {
            Self::unfile(&mut self.by_tag[node.tag as usize], node.uid);
        }
        if let Some(h) = other_name(&node) {
            let list = self.by_name.get_mut(&h).expect("an open element's name is indexed");
            Self::unfile(list, node.uid);
            if list.is_empty() {
                self.by_name.remove(&h);
            }
        }
        for (b, is) in bounds_of(&node).into_iter().enumerate() {
            if is {
                Self::unfile(&mut self.bounds[b], node.uid);
            }
        }
        node
    }

    /// Put `node` into the stack at `k`.
    pub(super) fn insert(&mut self, k: usize, node: Node<'a>) {
        self.index(&node);
        for n in &self.nodes[k..] {
            read(1);
            self.at[n.uid as usize] += 1;
        }
        self.at[node.uid as usize] = k as u32;
        self.nodes.insert(k, node);
        if node.tag != Tag::Other {
            let mut list = std::mem::take(&mut self.by_tag[node.tag as usize]);
            self.file(&mut list, node.uid, k);
            self.by_tag[node.tag as usize] = list;
        }
        if let Some(h) = other_name(&node) {
            let mut list = self.by_name.remove(&h).unwrap_or_default();
            self.file(&mut list, node.uid, k);
            self.by_name.insert(h, list);
        }
        for (b, is) in bounds_of(&node).into_iter().enumerate() {
            if is {
                let mut list = std::mem::take(&mut self.bounds[b]);
                self.file(&mut list, node.uid, k);
                self.bounds[b] = list;
            }
        }
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

/// The key Noah's Ark compares two formatting elements by: their tag, and their attributes as a set of
/// (lowercased name, decoded value) pairs, the first of a repeated name. Equal elements have equal keys;
/// [`super::Browser::same_attrs`] settles a collision.
pub(super) fn ark_key<'x>(tag: Tag, attrs: impl Iterator<Item = (&'x [u8], &'x str)>) -> u64 {
    // splitmix64's finalizer: each pair's hash is spread over all 64 bits before the order-free sum
    fn mix(mut h: u64) -> u64 {
        h = (h ^ (h >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
        h = (h ^ (h >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
        h ^ (h >> 31)
    }
    let (mut sum, mut n) = (0u64, 0u64);
    for (name, value) in attrs {
        // 0xff ends the name: no lowercased name byte is one
        let h = fnv(fnv(FNV_SEED, name.iter().map(u8::to_ascii_lowercase).chain([0xff])), value.bytes());
        sum = sum.wrapping_add(mix(h));
        n += 1;
    }
    mix(sum ^ mix(n << 8 | tag as u64))
}

/// The two kinds of count the list keeps per segment (the entries after a marker, or all of them before
/// the first): entries by [`ark_key`], and entries by tag.
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
enum Key {
    Ark(u64),
    Tag(u8),
}

/// The list of active formatting elements. Besides the entries it keeps where each marker is and, per
/// segment, how many entries it holds of each tag and of each [`ark_key`], so Noah's Ark and "is there an
/// `<a>` after the last marker" are counts rather than scans.
#[derive(Default)]
pub(super) struct Formatting {
    list: Vec<Afe>,
    markers: Vec<usize>,
    counts: HashMap<(u32, Key), u32>,
}

impl Formatting {
    pub(super) fn len(&self) -> usize {
        self.list.len()
    }

    pub(super) fn last(&self) -> Option<&Afe> {
        read(1);
        self.list.last()
    }

    /// The index of the first entry after the last marker.
    pub(super) fn after_last_marker(&self) -> usize {
        self.markers.last().map_or(0, |&k| k + 1)
    }

    /// How many entries after the last marker have the key `ark`.
    pub(super) fn alike(&self, ark: u64) -> u32 {
        self.counts.get(&(self.markers.len() as u32, Key::Ark(ark))).copied().unwrap_or(0)
    }

    /// Is there an entry with `tag` after the last marker?
    pub(super) fn has_tag(&self, tag: Tag) -> bool {
        self.counts.contains_key(&(self.markers.len() as u32, Key::Tag(tag as u8)))
    }

    /// The segment the entry at `k` is in: the number of markers before it.
    fn segment(&self, k: usize) -> u32 {
        self.markers.partition_point(|&m| m < k) as u32
    }

    fn count(&mut self, seg: u32, e: Afe, toks: &[FmtTok<'_>], up: bool) {
        let Afe::El { tok, .. } = e else { return };
        let t = &toks[tok as usize];
        for key in [Key::Ark(t.ark), Key::Tag(t.tag as u8)] {
            let c = self.counts.entry((seg, key)).or_insert(0);
            if up {
                *c += 1;
            } else {
                *c -= 1;
                if *c == 0 {
                    self.counts.remove(&(seg, key));
                }
            }
        }
    }

    pub(super) fn push(&mut self, e: Afe, toks: &[FmtTok<'_>]) {
        if e == Afe::Marker {
            self.markers.push(self.list.len());
        } else {
            self.count(self.markers.len() as u32, e, toks, true);
        }
        self.list.push(e);
    }

    /// Clear the list up to the last marker.
    pub(super) fn clear_to_marker(&mut self, toks: &[FmtTok<'_>]) {
        while let Some(e) = self.list.pop() {
            read(1);
            if e == Afe::Marker {
                self.markers.pop();
                break;
            }
            self.count(self.markers.len() as u32, e, toks, false);
        }
    }

    /// Take the element entry at `k` out of the list (a marker leaves only by [`Self::clear_to_marker`]).
    pub(super) fn remove(&mut self, k: usize, toks: &[FmtTok<'_>]) -> Afe {
        let e = self.list.remove(k);
        debug_assert!(e != Afe::Marker, "a marker leaves only by clearing to it");
        let seg = self.segment(k);
        self.count(seg, e, toks, false);
        for m in self.markers.iter_mut().filter(|m| **m > k) {
            *m -= 1;
        }
        e
    }

    /// Put an element entry into the list at `k`.
    pub(super) fn insert(&mut self, k: usize, e: Afe, toks: &[FmtTok<'_>]) {
        debug_assert!(e != Afe::Marker, "a marker is only ever pushed");
        for m in self.markers.iter_mut().filter(|m| **m >= k) {
            *m += 1;
        }
        let seg = self.segment(k);
        self.count(seg, e, toks, true);
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

    /// The entries from `from`, in order.
    pub(super) fn from(&self, from: usize) -> impl DoubleEndedIterator<Item = &Afe> + ExactSizeIterator {
        self.list[from..].iter().inspect(|_| read(1))
    }
}

impl std::ops::Index<usize> for Formatting {
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
