//! The replay's tree: the document a browser builds, as an arena, for a document where a retroactive
//! rule fired (see the module header of `browser`). The builder inserts into it with the DOM
//! operations the spec names (append, insert before a table, move, clone, detach), so text nodes merge
//! where a browser merges them and nothing is ever placed by token order. [`Dom::walk`] then drives the
//! matcher over it in pre-order.

use super::super::{Matcher, SourcePrefix};
use crate::tokenizer::DataMode;

/// No node.
pub(super) const NIL: u32 = u32::MAX;
/// The end of an element that has not left the stack of open elements by a pop.
const OPEN: usize = usize::MAX;

type RawAttrs<'a> = Vec<(&'a [u8], Option<&'a [u8]>)>;

#[derive(Clone)]
pub(super) struct Elem<'a> {
    pub(super) name: &'a [u8],
    /// The matcher's class id: the element's namespace (`HTML_ELEMENT` and the others).
    pub(super) class: u8,
    pub(super) attrs: RawAttrs<'a>,
    /// Raw source: from the start tag, or the token that inserted an element the page never wrote, to
    /// where it was popped. An element that leaves the stack another way ends with its content, and no
    /// earlier than its own start tag (`open_end`). A copy begins where its content does when that is
    /// earlier ([`Dom::source_start`]).
    start: usize,
    end: usize,
    open_end: usize,
    /// The start tag a copy was made from, read before its own source.
    prefix: Option<SourcePrefix>,
}

/// One run of a text node: its bytes, the state it was read in, and its offset.
type Run<'a> = (&'a [u8], DataMode, usize);

#[derive(Clone)]
enum Kind<'a> {
    Element(Elem<'a>),
    Text(Vec<Run<'a>>),
    Comment,
}

#[derive(Clone, Copy)]
struct Link {
    parent: u32,
    prev: u32,
    next: u32,
    first: u32,
    last: u32,
}

const UNLINKED: Link = Link { parent: NIL, prev: NIL, next: NIL, first: NIL, last: NIL };

pub(super) struct Dom<'a> {
    kinds: Vec<Kind<'a>>,
    links: Vec<Link>,
    /// The `<html>` element.
    root: u32,
}

impl<'a> Dom<'a> {
    pub(super) fn new() -> Self {
        Dom { kinds: Vec::new(), links: Vec::new(), root: NIL }
    }

    fn push(&mut self, kind: Kind<'a>) -> u32 {
        self.kinds.push(kind);
        self.links.push(UNLINKED);
        (self.kinds.len() - 1) as u32
    }

    /// A new element, not yet in the tree.
    pub(super) fn element(&mut self, name: &'a [u8], class: u8, attrs: RawAttrs<'a>, start: usize) -> u32 {
        self.push(Kind::Element(Elem { name, class, attrs, start, end: OPEN, open_end: start, prefix: None }))
    }

    pub(super) fn elem(&self, id: u32) -> Option<&Elem<'a>> {
        match &self.kinds[id as usize] {
            Kind::Element(e) => Some(e),
            _ => None,
        }
    }

    fn elem_mut(&mut self, id: u32) -> &mut Elem<'a> {
        match &mut self.kinds[id as usize] {
            Kind::Element(e) => e,
            _ => unreachable!("not an element"),
        }
    }

    pub(super) fn set_root(&mut self, id: u32) {
        self.root = id;
    }

    pub(super) fn parent(&self, id: u32) -> u32 {
        self.links[id as usize].parent
    }

    pub(super) fn first_child(&self, id: u32) -> u32 {
        self.links[id as usize].first
    }

    pub(super) fn next_sibling(&self, id: u32) -> u32 {
        self.links[id as usize].next
    }

    pub(super) fn has_prefix(&self, id: u32) -> bool {
        self.elem(id).is_some_and(|e| e.prefix.is_some())
    }

    pub(super) fn set_prefix(&mut self, id: u32, prefix: SourcePrefix) {
        self.elem_mut(id).prefix = Some(prefix);
    }

    /// The element's start tag ends at `end`.
    pub(super) fn set_open_end(&mut self, id: u32, end: usize) {
        self.elem_mut(id).open_end = end;
    }

    /// The element has left the stack of open elements at `end` (the first time only: an element is
    /// popped once).
    pub(super) fn set_end(&mut self, id: u32, end: usize) {
        let e = self.elem_mut(id);
        if e.end == OPEN {
            e.end = end;
        }
    }

    /// A later `<html>` or `<body>` start tag: its attributes the element does not have yet.
    pub(super) fn merge(&mut self, id: u32, attrs: &[(&'a [u8], Option<&'a [u8]>)]) {
        let e = self.elem_mut(id);
        for &(n, v) in attrs {
            if !e.attrs.iter().any(|(o, _)| o.eq_ignore_ascii_case(n)) {
                e.attrs.push((n, v));
            }
        }
    }

    // ------------------------------------------------------------------ tree surgery

    /// Take `id` out of its parent's children.
    pub(super) fn detach(&mut self, id: u32) {
        let Link { parent, prev, next, .. } = self.links[id as usize];
        if parent == NIL {
            return;
        }
        match prev {
            NIL => self.links[parent as usize].first = next,
            p => self.links[p as usize].next = next,
        }
        match next {
            NIL => self.links[parent as usize].last = prev,
            n => self.links[n as usize].prev = prev,
        }
        let l = &mut self.links[id as usize];
        l.parent = NIL;
        l.prev = NIL;
        l.next = NIL;
    }

    /// Make `id` the last child of `parent`, moving it if it is in the tree.
    pub(super) fn append(&mut self, parent: u32, id: u32) {
        self.detach(id);
        let last = self.links[parent as usize].last;
        {
            let l = &mut self.links[id as usize];
            l.parent = parent;
            l.prev = last;
        }
        match last {
            NIL => self.links[parent as usize].first = id,
            p => self.links[p as usize].next = id,
        }
        self.links[parent as usize].last = id;
    }

    /// Insert `id` into `before`'s parent, immediately before it.
    pub(super) fn insert_before(&mut self, before: u32, id: u32) {
        self.detach(id);
        let Link { parent, prev, .. } = self.links[before as usize];
        {
            let l = &mut self.links[id as usize];
            l.parent = parent;
            l.prev = prev;
            l.next = before;
        }
        match prev {
            NIL => self.links[parent as usize].first = id,
            p => self.links[p as usize].next = id,
        }
        self.links[before as usize].prev = id;
    }

    /// Move every child of `from`, in order, to the end of `to`.
    pub(super) fn move_children(&mut self, from: u32, to: u32) {
        let mut c = self.links[from as usize].first;
        while c != NIL {
            let next = self.links[c as usize].next;
            self.append(to, c);
            c = next;
        }
    }

    /// Remove every child of `id`.
    pub(super) fn clear_children(&mut self, id: u32) {
        while self.links[id as usize].first != NIL {
            self.detach(self.links[id as usize].first);
        }
    }

    /// A copy of `id` and everything in it, not yet in the tree. The copies keep the originals' source,
    /// which is what their raw-source outer HTML reads.
    pub(super) fn deep_clone(&mut self, id: u32) -> u32 {
        let copy = self.push(self.kinds[id as usize].clone());
        let mut c = self.links[id as usize].first;
        while c != NIL {
            let k = self.deep_clone(c);
            self.append(copy, k);
            c = self.links[c as usize].next;
        }
        copy
    }

    // ------------------------------------------------------------------ character data and comments

    /// Insert a text run at the end of `parent`: it continues the last child when that is a text node.
    pub(super) fn append_text(&mut self, parent: u32, run: Run<'a>) {
        let last = self.links[parent as usize].last;
        if last != NIL {
            if let Kind::Text(runs) = &mut self.kinds[last as usize] {
                runs.push(run);
                return;
            }
        }
        let t = self.push(Kind::Text(vec![run]));
        self.append(parent, t);
    }

    /// Insert a text run immediately before `before`: it continues the node before it when that is a
    /// text node, as foster-parented text does.
    pub(super) fn text_before(&mut self, before: u32, run: Run<'a>) {
        let prev = self.links[before as usize].prev;
        if prev != NIL {
            if let Kind::Text(runs) = &mut self.kinds[prev as usize] {
                runs.push(run);
                return;
            }
        }
        let t = self.push(Kind::Text(vec![run]));
        self.insert_before(before, t);
    }

    pub(super) fn append_comment(&mut self, parent: u32) {
        let c = self.push(Kind::Comment);
        self.append(parent, c);
    }

    /// The elements of `id`'s subtree (not `id` itself) that `pred` accepts, in tree order.
    pub(super) fn descendants_where(&self, id: u32, pred: impl Fn(&Elem<'a>) -> bool) -> Vec<u32> {
        let mut out = Vec::new();
        // An explicit pre-order walk: the next sibling of every node descended into waits on `stack`.
        let mut stack: Vec<u32> = Vec::new();
        let mut c = self.links[id as usize].first;
        loop {
            while c == NIL {
                match stack.pop() {
                    Some(n) => c = n,
                    None => return out,
                }
            }
            if self.elem(c).is_some_and(&pred) {
                out.push(c);
            }
            let Link { first, next, .. } = self.links[c as usize];
            if first != NIL {
                if next != NIL {
                    stack.push(next);
                }
                c = first;
            } else {
                c = next;
            }
        }
    }

    /// Where `id`'s raw source begins. A copy the adoption agency makes is inserted at the end tag, after
    /// the content it is given, so its source begins at its first child's when that is earlier.
    fn source_start(&self, id: u32) -> usize {
        let Kind::Element(e) = &self.kinds[id as usize] else { unreachable!("not an element") };
        if e.prefix.is_none() {
            return e.start;
        }
        let mut c = self.links[id as usize].first;
        while c != NIL {
            let start = match &self.kinds[c as usize] {
                Kind::Element(_) => Some(self.source_start(c)),
                Kind::Text(runs) => runs.first().map(|&(_, _, at)| at),
                Kind::Comment => None,
            };
            if let Some(start) = start {
                return start.min(e.start);
            }
            c = self.links[c as usize].next;
        }
        e.start
    }

    // ------------------------------------------------------------------ the walk

    /// Drive the matcher over the tree in pre-order, as the streaming builder drives it over the token
    /// stream: open, text, close. Each node's position is its pre-order rank, so everything the matcher
    /// orders by position comes out in document order; an element's raw source is registered against
    /// its rank. Stops early once the matcher is done, which over a finished tree is always sound.
    pub(super) fn walk(&self, m: &mut Matcher<'a>) {
        if self.root == NIL {
            return;
        }
        m.begin_replay();
        // Per open element: its rank, and the furthest end of anything in it so far.
        let mut open: Vec<(usize, usize)> = Vec::new();
        let mut rank = 0usize;
        let mut cur = self.root;
        loop {
            // Enter `cur`.
            let mut first = NIL;
            match &self.kinds[cur as usize] {
                Kind::Element(e) => {
                    m.flush_text();
                    m.open_element(e.name, e.class, &e.attrs, rank);
                    if let Some(prefix) = e.prefix {
                        m.set_source_prefix(prefix);
                    }
                    open.push((rank, e.start));
                    first = self.links[cur as usize].first;
                }
                Kind::Text(runs) => {
                    m.flush_text();
                    let mut at = rank;
                    for &(bytes, mode, _) in runs {
                        m.buffer_text(bytes, mode, at);
                        at += bytes.len();
                    }
                    if let (Some(&(b, _, s)), Some(top)) = (runs.last(), open.last_mut()) {
                        top.1 = top.1.max(s + b.len());
                    }
                }
                Kind::Comment => m.flush_text(),
            }
            rank += 1;
            if first != NIL {
                cur = first;
                continue;
            }
            // `cur` has nothing (more) in it: leave it, and the ancestors it was the last child of.
            loop {
                if let Some(e) = self.elem(cur) {
                    let (r, furthest) = open.pop().expect("an element is open");
                    let end = if e.end == OPEN { furthest.max(e.open_end) } else { e.end };
                    if let Some(top) = open.last_mut() {
                        top.1 = top.1.max(end);
                    }
                    m.flush_text();
                    m.register_span(r, self.source_start(cur), end);
                    m.close_top(end);
                }
                if m.done() || cur == self.root {
                    return;
                }
                let next = self.links[cur as usize].next;
                if next != NIL {
                    cur = next;
                    break;
                }
                cur = self.links[cur as usize].parent;
            }
            if m.done() {
                return;
            }
        }
    }
}
