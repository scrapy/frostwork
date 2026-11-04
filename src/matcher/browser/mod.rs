//! The browser tree builder: HTML5 tree construction (WHATWG 13.2.6) as browser mode runs it, graded
//! against Chrome (docs/BROWSER_MODE.md, sections 3 to 5). The insertion modes are in `rules`; this
//! module holds the builder's state and how it drives the matcher.
//!
//! # The parser stack is not the DOM path
//!
//! Selector matching reads ancestry, positions and sibling state off [`Matcher`]'s stack, so in browser
//! mode that stack is the DOM PATH to the current insertion point: every element on it is the DOM parent
//! of the one above. The HTML5 stack of open elements is the builder's own (`stack`), and the two differ
//! in four ways:
//!
//! * **Ghosts.** An element can leave the HTML5 stack while it is still a DOM ancestor of what is open:
//!   `</form>` removes a form whose `<div>` is still open, so `#f #after` matches in
//!   `<form id=f><div>a</form><p id=after>`. Its matcher element stays on the matcher stack, with no
//!   builder entry, and closes when the insertion point next moves below it ([`Browser::place`]).
//! * **Parked elements.** Foster parenting inserts beside the last table, in the table's DOM parent,
//!   while the table and its rows stay open. The table's matcher elements are [`Matcher::park`]ed, so
//!   fostered content is a child of the table's parent and not a descendant of the table, and they are
//!   put back when the next insertion targets them. The head is parked the same way in "after head",
//!   where whitespace goes to `<html>` but a later `<script>` still goes into the head.
//! * **Inert elements.** Template content and a declarative shadow root are parsed with the full rules
//!   but never reach the matcher; their tokens are absorbed into the text gap, so the text either side
//!   of a shadow-root `<template>` is one node, as in the browser.
//! * **Pending elements.** The adoption agency's clones enter the HTML5 stack without a matcher element
//!   (their content was already matched under the originals) and get one only if something is later
//!   inserted into them ([`Browser::materialize`]).
//!
//! Each builder entry records its matcher depth and a uid, and `path` holds the uid of every matcher
//! element, so "is this entry's element open, and where" is one comparison. Lxml mode never runs any of
//! this: `OpenElem` and the libxml2 builder are unchanged.
//!
//! What streams exactly and what does not: everything that decides where a token goes, and every
//! element's ancestry at the moment it opens, follows the browser. Four rules change decisions already
//! made, and only their forward state is exact here: the adoption agency with a furthest block (the
//! moved elements keep the ancestors they opened under), the document order of fostered content (it
//! counts as following its table), a second `<html>`/`<body>` (its attributes reach later matches, not
//! the element's own), and a `<frameset>` replacing a body already parsed (the body's matches stand).
//! A fifth, an `<option>`'s content cloned into `<selectedcontent>`, is a DOM mutation of an element
//! already closed.

mod doctype;
mod rules;
mod tags;

use super::tree::TreeBuilder;
use super::{Matcher, Parked};
use crate::implied_close::{
    html5_cat, html5_data_mode, BUTTON_SCOPE, IMPLIED_END, IMPLIED_END_THOROUGH, LIST_ITEM_SCOPE, SCOPE,
    SPECIAL, TABLE_SCOPE,
};
use crate::tokenizer::DataMode;
use tags::{lower, tag_of, Tag};

type Attrs<'a> = [(&'a [u8], Option<&'a [u8]>)];

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Ns {
    Html,
    Svg,
    MathMl,
}

/// The insertion modes. "in table text" and "in head noscript" have none: a text run is a whole
/// pending-table-text list, and a browser parses with scripting on.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Mode {
    Initial,
    BeforeHtml,
    BeforeHead,
    InHead,
    AfterHead,
    InBody,
    Text,
    InTable,
    InCaption,
    InColumnGroup,
    InTableBody,
    InRow,
    InCell,
    InTemplate,
    AfterBody,
    InFrameset,
    AfterFrameset,
    AfterAfterBody,
    AfterAfterFrameset,
}

/// What a builder entry is on the matcher side (see the module header).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Vis {
    /// It has a matcher element, at `Node::depth`, open or parked.
    Live,
    /// Template content or a declarative shadow root: never matched.
    Inert,
    /// An adoption-agency clone with no matcher element yet; the index is its token in `toks`.
    Pending(u32),
}

const HTML_IP: u8 = 1 << 0;
const MATHML_TEXT_IP: u8 = 1 << 1;
const ANNOTATION_XML: u8 = 1 << 2;
const SHADOW_HOST: u8 = 1 << 3;
/// An element or text node has been inserted into the element.
const HAS_CHILD: u8 = 1 << 4;

/// One entry of the HTML5 stack of open elements.
#[derive(Clone, Copy, Debug)]
struct Node<'a> {
    /// The element's name as the matcher sees it: as written, or the camel-case SVG name.
    name: &'a [u8],
    tag: Tag,
    ns: Ns,
    /// `html5_cat` for an HTML element; special and scope for a foreign integration point.
    cat: u16,
    flags: u8,
    uid: u32,
    vis: Vis,
    /// Its depth on the matcher stack, while `vis` is `Live`.
    depth: u32,
}

/// The start tag a formatting element was created for, kept so a clone can be made from it.
struct FmtTok<'a> {
    name: &'a [u8],
    tag: Tag,
    attrs: Vec<(&'a [u8], Option<&'a [u8]>)>,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Afe {
    Marker,
    El { uid: u32, tok: u32 },
}

/// A parked run of matcher elements: the uids that were `path[base..]`.
struct Seg<'a> {
    base: usize,
    uids: Vec<u32>,
    parked: Parked<'a>,
}

struct StartTok<'a, 't> {
    name: &'a [u8],
    tag: Tag,
    attrs: &'t Attrs<'a>,
    self_closing: bool,
    start: usize,
    end: usize,
}

#[derive(Clone, Copy)]
struct EndTok<'a> {
    name: &'a [u8],
    tag: Tag,
    start: usize,
    end: usize,
}

/// Where the next node goes.
enum Loc {
    /// Into the document: the `<html>` element.
    Document,
    /// Into the stack entry at this index.
    Into(usize),
    /// Before the table at this stack index, into its DOM parent.
    Foster(usize),
    /// Nowhere the document can see.
    Inert,
}

pub(super) struct Browser<'a> {
    mode: Mode,
    original: Mode,
    templates: Vec<Mode>,
    stack: Vec<Node<'a>>,
    afe: Vec<Afe>,
    toks: Vec<FmtTok<'a>>,
    /// The head element pointer, as the entry it had on the stack.
    head: Option<Node<'a>>,
    /// The head's matcher element is still open for "after head" to insert into.
    head_live: bool,
    form: Option<u32>,
    frameset_ok: bool,
    quirks: bool,
    foster: bool,
    /// The previous token was `<pre>`, `<listing>` or `<textarea>`: a leading newline is dropped.
    skip_lf: bool,
    /// The uid of every element on the matcher stack, bottom first.
    path: Vec<u32>,
    parked: Vec<Seg<'a>>,
    next_uid: u32,
    /// The current token changed the matcher stack. A tag that did not is no node, so the text either
    /// side of it is one node.
    touched: bool,
    /// The state the current start tag's content is tokenized in.
    data: DataMode,
}

impl Default for Browser<'_> {
    fn default() -> Self {
        Browser {
            mode: Mode::Initial,
            original: Mode::InBody,
            templates: Vec::new(),
            stack: Vec::new(),
            afe: Vec::new(),
            toks: Vec::new(),
            head: None,
            head_live: false,
            form: None,
            frameset_ok: true,
            quirks: false,
            foster: false,
            skip_lf: false,
            path: Vec::new(),
            parked: Vec::new(),
            next_uid: 1,
            touched: false,
            data: DataMode::Normal,
        }
    }
}

fn is_ws(c: u8) -> bool {
    matches!(c, b'\t' | b'\n' | 0x0c | b'\r' | b' ')
}

/// A numeric character reference at the start of `b` (`&#10;`, `&#x0a`): its code point and length.
fn numeric_ref(b: &[u8]) -> Option<(u32, usize)> {
    let rest = b.strip_prefix(b"&#")?;
    let (hex, digits) = match rest {
        [b'x' | b'X', d @ ..] => (true, d),
        d => (false, d),
    };
    let n = digits.iter().take_while(|c| if hex { c.is_ascii_hexdigit() } else { c.is_ascii_digit() }).count();
    if n == 0 {
        return None;
    }
    let mut v: u32 = 0;
    for &c in &digits[..n] {
        let d = (c as char).to_digit(16).expect("a digit");
        v = v.saturating_mul(if hex { 16 } else { 10 }).saturating_add(d);
    }
    let used = 2 + usize::from(hex) + n;
    Some((v, used + usize::from(b.get(used) == Some(&b';'))))
}

/// The length of the whitespace character token at the start of `b`, or 0. The tree builder sees a
/// character reference as the character it stands for, so `&#10;` is as much whitespace as a newline.
/// A NUL counts when `nul` (Chrome drops a NUL before the body as if it were not there).
fn ws_at(b: &[u8], nul: bool) -> usize {
    match b.first() {
        Some(&c) if is_ws(c) || (nul && c == 0) => 1,
        Some(b'&') => {
            if let Some((v, len)) = numeric_ref(b) {
                return if matches!(v, 0x09 | 0x0a | 0x0c | 0x0d | 0x20) { len } else { 0 };
            }
            [&b"&Tab;"[..], b"&NewLine;"].iter().find(|r| b.starts_with(r)).map_or(0, |r| r.len())
        }
        _ => 0,
    }
}

/// Length of the leading run of whitespace character tokens (see [`ws_at`]).
fn ws_prefix(b: &[u8], nul: bool) -> usize {
    let mut i = 0;
    while i < b.len() {
        match ws_at(&b[i..], nul) {
            0 => break,
            k => i += k,
        }
    }
    i
}

/// Does the run hold a character token that is neither whitespace nor NUL?
fn has_content(b: &[u8]) -> bool {
    ws_prefix(b, true) < b.len()
}

/// The leading newline `<pre>`, `<listing>` and `<textarea>` drop: a LF, a CR or CRLF (the input stream
/// makes both a LF), or a character reference to U+000A, which reaches the tree builder as the same
/// character token.
fn leading_lf(b: &[u8]) -> usize {
    match b {
        [b'\r', b'\n', ..] => 2,
        [b'\n' | b'\r', ..] => 1,
        _ if b.starts_with(b"&NewLine;") => 9,
        _ => numeric_ref(b).filter(|&(v, _)| v == 0x0a).map_or(0, |(_, len)| len),
    }
}

impl<'a> Browser<'a> {
    // ------------------------------------------------------------------ nodes

    fn new_node(&mut self, name: &'a [u8], tag: Tag, ns: Ns, attrs: &Attrs<'a>) -> Node<'a> {
        let uid = self.next_uid;
        self.next_uid += 1;
        let mut buf = [0; 24];
        let low = lower(name, &mut buf);
        let ip = SPECIAL | SCOPE | LIST_ITEM_SCOPE | BUTTON_SCOPE;
        let (tag, cat, flags) = match ns {
            Ns::Html => (tag, low.map_or_else(|| html5_cat(b""), html5_cat), 0),
            Ns::Svg => match low {
                Some(b"foreignobject" | b"desc" | b"title") => (Tag::Other, ip, HTML_IP),
                _ => (Tag::Other, 0, 0),
            },
            Ns::MathMl => match low {
                Some(b"mi" | b"mo" | b"mn" | b"ms" | b"mtext") => (Tag::Other, ip, MATHML_TEXT_IP),
                Some(b"annotation-xml") => {
                    let html = attrs.iter().find(|(n, _)| n.eq_ignore_ascii_case(b"encoding")).is_some_and(|(_, v)| {
                        let v = v.unwrap_or(b"");
                        v.eq_ignore_ascii_case(b"text/html") || v.eq_ignore_ascii_case(b"application/xhtml+xml")
                    });
                    (Tag::Other, ip, ANNOTATION_XML | if html { HTML_IP } else { 0 })
                }
                _ => (Tag::Other, 0, 0),
            },
        };
        Node { name, tag, ns, cat, flags, uid, vis: Vis::Live, depth: 0 }
    }

    fn cur(&self) -> &Node<'a> {
        self.stack.last().expect("the stack of open elements is empty")
    }

    fn cur_is(&self, tag: Tag) -> bool {
        self.stack.last().is_some_and(|n| n.tag == tag)
    }

    fn index_of(&self, uid: u32) -> Option<usize> {
        self.stack.iter().rposition(|n| n.uid == uid)
    }

    fn has_template(&self) -> bool {
        self.stack.iter().any(|n| n.tag == Tag::Template)
    }

    // ------------------------------------------------------------------ the matcher side

    /// Where the next node goes: the appropriate place for inserting a node, with foster parenting when
    /// it is enabled, and nowhere for template content.
    fn location(&self) -> Loc {
        let Some(i) = self.stack.len().checked_sub(1) else { return Loc::Document };
        let t = &self.stack[i];
        if t.vis == Vis::Inert || t.tag == Tag::Template {
            return Loc::Inert;
        }
        if self.foster && matches!(t.tag, Tag::Table | Tag::Tbody | Tag::Tfoot | Tag::Thead | Tag::Tr) {
            return match self.stack.iter().rposition(|n| matches!(n.tag, Tag::Table | Tag::Template)) {
                None => Loc::Into(0),
                Some(k) if self.stack[k].tag == Tag::Template || self.stack[k].vis == Vis::Inert => Loc::Inert,
                Some(k) => Loc::Foster(k),
            };
        }
        Loc::Into(i)
    }

    /// Make stack entry `i`'s matcher element the current one: close the ghosts above it, put back a
    /// parked run it is in, or give a pending clone its element. `at` is where a closed ghost ends.
    fn place(&mut self, m: &mut Matcher<'a>, i: usize, at: usize) {
        match self.stack[i].vis {
            Vis::Pending(_) => return self.materialize(m, i, at),
            Vis::Inert => return,
            Vis::Live => {}
        }
        let (uid, d) = (self.stack[i].uid, self.stack[i].depth as usize);
        loop {
            if self.path.get(d) == Some(&uid) {
                while self.path.len() > d + 1 {
                    self.drop_top(m, at);
                }
                return;
            }
            let Some(base) = self.parked.last().map(|s| s.base) else {
                debug_assert!(false, "a live element is neither open nor parked");
                return;
            };
            while self.path.len() > base {
                self.drop_top(m, at);
            }
            self.unpark_last(m);
        }
    }

    /// The matcher's current element has no builder entry: close it, or park the head while "after
    /// head" may still insert into it.
    fn drop_top(&mut self, m: &mut Matcher<'a>, at: usize) {
        m.flush_text();
        self.touched = true;
        let top = *self.path.last().expect("dropping with nothing open");
        if self.head_live && self.head.is_some_and(|h| h.uid == top) {
            self.park_from(m, self.path.len() - 1);
            return;
        }
        debug_assert!(
            !self.stack.iter().any(|n| n.uid == top && n.vis == Vis::Live),
            "closing an element the builder still has open"
        );
        m.close_top(at);
        self.path.pop();
    }

    fn park_from(&mut self, m: &mut Matcher<'a>, base: usize) {
        m.flush_text();
        self.touched = true;
        let uids = self.path.split_off(base);
        let parked = m.park(base);
        self.parked.push(Seg { base, uids, parked });
    }

    fn unpark_last(&mut self, m: &mut Matcher<'a>) {
        m.flush_text();
        self.touched = true;
        let seg = self.parked.pop().expect("unparking with nothing parked");
        debug_assert_eq!(self.path.len(), seg.base);
        m.unpark(seg.parked);
        self.path.extend(seg.uids);
    }

    /// Make the DOM parent of the table at stack index `t` the matcher's current element, parking the
    /// table and what is open in it. Consecutive fostered nodes find the table already parked.
    fn foster_place(&mut self, m: &mut Matcher<'a>, t: usize, at: usize) {
        let (tuid, base) = (self.stack[t].uid, self.stack[t].depth as usize);
        if self.parked.last().is_some_and(|s| s.base == base && s.uids.first() == Some(&tuid)) {
            while self.path.len() > base {
                self.drop_top(m, at);
            }
            return;
        }
        let cur = self.stack.len() - 1;
        self.place(m, cur, at);
        debug_assert_eq!(self.path.get(base), Some(&tuid), "the table is not on the DOM path");
        self.park_from(m, base);
    }

    /// Give the pending clone at stack index `i` its matcher element, inside the entry below it.
    fn materialize(&mut self, m: &mut Matcher<'a>, i: usize, at: usize) {
        let Vis::Pending(tok) = self.stack[i].vis else { return };
        if i == 0 || self.stack[i - 1].vis == Vis::Inert {
            self.stack[i].vis = Vis::Inert;
            return;
        }
        self.place(m, i - 1, at);
        let attrs = std::mem::take(&mut self.toks[tok as usize].attrs);
        let mut node = self.stack[i];
        self.open(m, &mut node, &attrs, at);
        self.stack[i] = node;
        self.toks[tok as usize].attrs = attrs;
    }

    /// Open `node` as the matcher's next element, at the current insertion point.
    fn open(&mut self, m: &mut Matcher<'a>, node: &mut Node<'a>, attrs: &Attrs<'a>, at: usize) {
        m.flush_text();
        m.open_element(node.name, 0, attrs, at);
        node.vis = Vis::Live;
        node.depth = (m.stack.len() - 1) as u32;
        self.path.push(node.uid);
        self.touched = true;
    }

    /// Close the matcher element of `n`, an entry that has just left the stack: the ghosts above it end
    /// at `ghost_end`, the element at `end`.
    fn close_node(&mut self, m: &mut Matcher<'a>, n: Node<'a>, ghost_end: usize, end: usize) {
        if n.vis != Vis::Live {
            return;
        }
        let d = n.depth as usize;
        loop {
            if self.path.get(d) == Some(&n.uid) {
                while self.path.len() > d + 1 {
                    self.drop_top(m, ghost_end);
                }
                m.flush_text();
                m.close_top(end);
                self.path.pop();
                self.touched = true;
                return;
            }
            match self.parked.last() {
                Some(seg) if seg.uids.contains(&n.uid) => {
                    let base = seg.base;
                    while self.path.len() > base {
                        self.drop_top(m, ghost_end);
                    }
                    self.unpark_last(m);
                }
                _ => return,
            }
        }
    }

    // ------------------------------------------------------------------ inserting

    /// Insert `node` where the next node goes and push it onto the stack of open elements.
    fn insert_node(&mut self, m: &mut Matcher<'a>, mut node: Node<'a>, attrs: &Attrs<'a>, at: usize) {
        match self.location() {
            Loc::Inert => node.vis = Vis::Inert,
            Loc::Document => self.open(m, &mut node, attrs, at),
            Loc::Into(i) => {
                self.place(m, i, at);
                self.open(m, &mut node, attrs, at);
                self.stack[i].flags |= HAS_CHILD;
            }
            Loc::Foster(t) => {
                self.foster_place(m, t, at);
                self.open(m, &mut node, attrs, at);
            }
        }
        if node.ns == Ns::Html {
            let mut buf = [0; 24];
            self.data = lower(node.name, &mut buf).map_or(DataMode::Normal, html5_data_mode);
        } else {
            self.data = DataMode::Normal;
        }
        self.stack.push(node);
    }

    /// Insert an HTML element for a start tag.
    fn insert_html(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        let node = self.new_node(t.name, t.tag, Ns::Html, t.attrs);
        self.insert_node(m, node, t.attrs, t.start);
    }

    /// Insert an element the page never wrote (no attributes, no source of its own), at `at`.
    fn insert_synth(&mut self, m: &mut Matcher<'a>, name: &'static [u8], tag: Tag, at: usize) {
        let node = self.new_node(name, tag, Ns::Html, &[]);
        self.insert_node(m, node, &[], at);
    }

    /// Insert an HTML element for a start tag and pop it at once (a void element, `<input type=hidden>`
    /// in a table, a form in a table).
    fn insert_void(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        self.insert_html(m, t);
        self.pop(m, t.end);
    }

    fn insert_foreign(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>, ns: Ns) {
        let name = match ns {
            Ns::Svg => tags::svg_local_name(t.name).unwrap_or(t.name),
            _ => t.name,
        };
        let node = self.new_node(name, Tag::Other, ns, t.attrs);
        self.insert_node(m, node, t.attrs, t.start);
        if t.self_closing {
            self.pop(m, t.end);
        }
    }

    /// Insert a character run where the next node goes.
    fn insert_text(&mut self, m: &mut Matcher<'a>, b: &'a [u8], dm: DataMode, s: usize) {
        match self.location() {
            Loc::Inert | Loc::Document => m.absorb_into_text_gap(s, s + b.len()),
            Loc::Into(i) => {
                self.place(m, i, s);
                m.buffer_text(b, dm, s);
                self.stack[i].flags |= HAS_CHILD;
            }
            Loc::Foster(t) => {
                self.foster_place(m, t, s);
                m.buffer_text(b, dm, s);
            }
        }
    }

    // ------------------------------------------------------------------ popping

    /// Pop the current node; its matcher element (and the ghosts above it) end at `end`.
    fn pop(&mut self, m: &mut Matcher<'a>, end: usize) {
        self.pop_with(m, end, end);
    }

    fn pop_with(&mut self, m: &mut Matcher<'a>, ghost_end: usize, end: usize) {
        let n = self.stack.pop().expect("popping an empty stack");
        self.close_node(m, n, ghost_end, end);
    }

    /// Pop until the entry at index `k` has been popped. Entries above it end at `ghost_end`, it at `end`.
    fn pop_to(&mut self, m: &mut Matcher<'a>, k: usize, ghost_end: usize, end: usize) {
        while self.stack.len() > k + 1 {
            self.pop_with(m, ghost_end, ghost_end);
        }
        self.pop_with(m, ghost_end, end);
    }

    /// Generate implied end tags, except for `except`.
    fn implied_end(&mut self, m: &mut Matcher<'a>, except: Option<Tag>, at: usize) {
        while let Some(n) = self.stack.last() {
            if n.ns != Ns::Html || n.cat & IMPLIED_END == 0 || Some(n.tag) == except {
                break;
            }
            self.pop(m, at);
        }
    }

    fn implied_end_thorough(&mut self, m: &mut Matcher<'a>, at: usize) {
        while let Some(n) = self.stack.last() {
            if n.ns != Ns::Html || n.cat & IMPLIED_END_THOROUGH == 0 {
                break;
            }
            self.pop(m, at);
        }
    }

    // ------------------------------------------------------------------ scopes

    /// The index of the nearest entry `pred` accepts, if no entry carrying `boundary` is nearer.
    fn scope_by(&self, boundary: u16, pred: impl Fn(&Node<'a>) -> bool) -> Option<usize> {
        for (i, n) in self.stack.iter().enumerate().rev() {
            if pred(n) {
                return Some(i);
            }
            if n.cat & boundary != 0 {
                return None;
            }
        }
        None
    }

    fn in_scope(&self, tag: Tag) -> Option<usize> {
        self.scope_by(SCOPE, |n| n.tag == tag)
    }

    fn in_button_scope(&self, tag: Tag) -> Option<usize> {
        self.scope_by(BUTTON_SCOPE, |n| n.tag == tag)
    }

    fn in_list_scope(&self, tag: Tag) -> Option<usize> {
        self.scope_by(LIST_ITEM_SCOPE, |n| n.tag == tag)
    }

    fn in_table_scope(&self, tag: Tag) -> Option<usize> {
        self.scope_by(TABLE_SCOPE, |n| n.tag == tag)
    }

    /// Close a `<p>` element: implied end tags except `p`, then pop through the `<p>`.
    fn close_p(&mut self, m: &mut Matcher<'a>, ghost_end: usize, end: usize) {
        self.implied_end(m, Some(Tag::P), ghost_end);
        if let Some(k) = self.stack.iter().rposition(|n| n.tag == Tag::P) {
            self.pop_to(m, k, ghost_end, end);
        }
    }

    fn close_p_if_in_button_scope(&mut self, m: &mut Matcher<'a>, at: usize) {
        if self.in_button_scope(Tag::P).is_some() {
            self.close_p(m, at, at);
        }
    }

    // ------------------------------------------------------------------ active formatting elements

    fn afe_last_marker(&self) -> usize {
        self.afe.iter().rposition(|e| *e == Afe::Marker).map_or(0, |k| k + 1)
    }

    fn afe_index_of(&self, uid: u32) -> Option<usize> {
        self.afe.iter().rposition(|e| matches!(e, Afe::El { uid: u, .. } if *u == uid))
    }

    fn clear_afe_to_marker(&mut self) {
        while let Some(e) = self.afe.pop() {
            if e == Afe::Marker {
                break;
            }
        }
    }

    /// Do two formatting tokens carry the same attributes, as Noah's Ark compares them: by name, the
    /// first of a repeated name, in any order, with equal values.
    fn same_attrs(&self, m: &Matcher<'a>, a: &FmtTok<'a>, b: &FmtTok<'a>) -> bool {
        fn firsts<'x>(attrs: &'x [(&'x [u8], Option<&'x [u8]>)]) -> Vec<(&'x [u8], &'x [u8])> {
            let mut out: Vec<(&[u8], &[u8])> = Vec::with_capacity(attrs.len());
            for &(n, v) in attrs {
                if !out.iter().any(|(o, _)| o.eq_ignore_ascii_case(n)) {
                    out.push((n, v.unwrap_or(b"")));
                }
            }
            out
        }
        let (fa, fb) = (firsts(&a.attrs), firsts(&b.attrs));
        fa.len() == fb.len()
            && fa.iter().all(|(n, v)| {
                fb.iter().any(|(n2, v2)| {
                    n.eq_ignore_ascii_case(n2) && (v == v2 || m.input.attr_value(v) == m.input.attr_value(v2))
                })
            })
    }

    /// Push the element `uid`, made for token `tok`, onto the list of active formatting elements.
    fn push_afe(&mut self, m: &Matcher<'a>, uid: u32, tok: u32) {
        let from = self.afe_last_marker();
        let t = &self.toks[tok as usize];
        let mut same = 0;
        let mut earliest = None;
        for k in from..self.afe.len() {
            if let Afe::El { tok: o, .. } = self.afe[k] {
                let other = &self.toks[o as usize];
                if other.tag == t.tag && other.name.eq_ignore_ascii_case(t.name) && self.same_attrs(m, other, t) {
                    same += 1;
                    earliest.get_or_insert(k);
                }
            }
        }
        if same >= 3 {
            self.afe.remove(earliest.expect("three of a kind"));
        }
        self.afe.push(Afe::El { uid, tok });
    }

    /// Insert a formatting element for a start tag and push it onto the list.
    fn insert_formatting(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        let tok = self.toks.len() as u32;
        self.toks.push(FmtTok { name: t.name, tag: t.tag, attrs: t.attrs.to_vec() });
        self.insert_html(m, t);
        let uid = self.cur().uid;
        self.push_afe(m, uid, tok);
    }

    /// Reconstruct the active formatting elements: reopen, as clones carrying the original's
    /// attributes, every one opened since the last marker that is no longer open. A clone has no
    /// source of its own; it begins at `at`.
    fn reconstruct(&mut self, m: &mut Matcher<'a>, at: usize) {
        let open = |b: &Self, e: &Afe| match *e {
            Afe::Marker => true,
            Afe::El { uid, .. } => b.index_of(uid).is_some(),
        };
        let Some(last) = self.afe.last() else { return };
        if open(self, last) {
            return;
        }
        let mut k = self.afe.len() - 1;
        while k > 0 && !open(self, &self.afe[k - 1]) {
            k -= 1;
        }
        for j in k..self.afe.len() {
            let Afe::El { tok, .. } = self.afe[j] else { unreachable!("a marker is open") };
            let (name, tag) = (self.toks[tok as usize].name, self.toks[tok as usize].tag);
            let attrs = std::mem::take(&mut self.toks[tok as usize].attrs);
            let node = self.new_node(name, tag, Ns::Html, &attrs);
            let uid = node.uid;
            self.insert_node(m, node, &attrs, at);
            self.toks[tok as usize].attrs = attrs;
            self.afe[j] = Afe::El { uid, tok };
        }
    }

    // ------------------------------------------------------------------ the frame

    /// Leave "in head": pop the head off the stack, keeping its matcher element for "after head".
    fn leave_head(&mut self) {
        let n = self.stack.pop().expect("in head with an empty stack");
        debug_assert_eq!(n.tag, Tag::Head);
        self.head_live = n.vis == Vis::Live;
        self.mode = Mode::AfterHead;
    }

    /// Close the head's matcher element: nothing goes into it any more.
    fn finish_head(&mut self, m: &mut Matcher<'a>, at: usize) {
        if !std::mem::take(&mut self.head_live) {
            return;
        }
        if let Some(h) = self.head {
            self.close_node(m, h, at, at);
        }
    }

    fn reset_mode(&mut self) {
        for i in (0..self.stack.len()).rev() {
            let last = i == 0;
            let n = &self.stack[i];
            self.mode = match n.tag {
                Tag::Td | Tag::Th if !last => Mode::InCell,
                Tag::Tr => Mode::InRow,
                Tag::Tbody | Tag::Thead | Tag::Tfoot => Mode::InTableBody,
                Tag::Caption => Mode::InCaption,
                Tag::Colgroup => Mode::InColumnGroup,
                Tag::Table => Mode::InTable,
                Tag::Template => *self.templates.last().unwrap_or(&Mode::InBody),
                Tag::Head if !last => Mode::InHead,
                Tag::Body => Mode::InBody,
                Tag::Frameset => Mode::InFrameset,
                Tag::Html if self.head.is_none() => Mode::BeforeHead,
                Tag::Html => Mode::AfterHead,
                _ if last => Mode::InBody,
                _ => continue,
            };
            return;
        }
        self.mode = Mode::InBody;
    }

    // ------------------------------------------------------------------ dispatch

    fn start(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        let html = match self.stack.last() {
            None => true,
            Some(c) => {
                c.ns == Ns::Html
                    || (c.flags & MATHML_TEXT_IP != 0 && !matches!(t.tag, Tag::Mglyph | Tag::Malignmark))
                    || (c.flags & ANNOTATION_XML != 0 && t.tag == Tag::Svg)
                    || c.flags & HTML_IP != 0
            }
        };
        if html {
            self.html_start(m, t)
        } else {
            self.foreign_start(m, t)
        }
    }

    fn end(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        if self.stack.last().is_none_or(|c| c.ns == Ns::Html) {
            self.html_end(m, t)
        } else {
            self.foreign_end(m, t)
        }
    }

    fn text_in_html(&self) -> bool {
        self.stack.last().is_none_or(|c| c.ns == Ns::Html || c.flags & (HTML_IP | MATHML_TEXT_IP) != 0)
    }
}

impl<'a> TreeBuilder<'a> for Browser<'a> {
    fn start_tag(
        &mut self,
        m: &mut Matcher<'a>,
        name: &'a [u8],
        raw_attrs: &[(&'a [u8], Option<&'a [u8]>)],
        self_closing: bool,
        span_start: usize,
        open_end: usize,
    ) -> DataMode {
        self.skip_lf = false;
        self.touched = false;
        self.data = DataMode::Normal;
        let t = StartTok { name, tag: tag_of(name), attrs: raw_attrs, self_closing, start: span_start, end: open_end };
        let before = self.stack.last().map(|n| n.uid);
        self.start(m, &t);
        if !self.touched {
            m.absorb_into_text_gap(span_start, open_end);
        }
        // Only an element this tag inserted, and left open, reads its content as text.
        if self.stack.last().map(|n| n.uid) == before || self.stack.last().is_some_and(|n| n.ns != Ns::Html) {
            DataMode::Normal
        } else {
            self.data
        }
    }

    fn end_tag(&mut self, m: &mut Matcher<'a>, name: &'a [u8], close_start: usize, close_end: usize) {
        self.skip_lf = false;
        self.touched = false;
        self.end(m, EndTok { name, tag: tag_of(name), start: close_start, end: close_end });
        if !self.touched {
            m.absorb_into_text_gap(close_start, close_end);
        }
    }

    fn text(&mut self, m: &mut Matcher<'a>, text: &'a [u8], mode: DataMode, start: usize) {
        let (mut b, mut s) = (text, start);
        if std::mem::take(&mut self.skip_lf) {
            let k = leading_lf(b);
            if k > 0 {
                m.absorb_into_text_gap(s, s + k);
                b = &b[k..];
                s += k;
            }
        }
        if b.is_empty() {
            return;
        }
        if self.mode == Mode::Text && mode != DataMode::Normal {
            return self.insert_text(m, b, mode, s);
        }
        if self.text_in_html() {
            self.html_text(m, b, mode, s)
        } else {
            self.foreign_text(m, b, mode, s)
        }
    }

    fn cdata_is_text(&self, _m: &Matcher<'a>) -> bool {
        self.stack.last().is_some_and(|c| c.ns != Ns::Html)
    }

    fn doctype(&mut self, m: &mut Matcher<'a>, raw: &'a [u8], from: usize, to: usize) {
        self.skip_lf = false;
        if self.mode == Mode::Initial {
            self.quirks = doctype::doc_mode(raw) == doctype::DocMode::Quirks;
            self.mode = Mode::BeforeHtml;
        }
        m.absorb_into_text_gap(from, to);
    }

    fn comment(&mut self, m: &mut Matcher<'a>, from: usize, to: usize) {
        self.skip_lf = false;
        // A comment is a node of whatever it is inserted into. It ends the text node there; inserted
        // anywhere else (the document, the `<html>` after the body, a table whose text is fostered out
        // of it, template content) it leaves the pending text node open.
        let target = match self.mode {
            Mode::Initial | Mode::BeforeHtml | Mode::AfterAfterBody | Mode::AfterAfterFrameset => None,
            Mode::AfterBody => self.stack.first(),
            _ => self.stack.last(),
        };
        let splits =
            target.is_some_and(|n| n.vis == Vis::Live && n.tag != Tag::Template && self.path.last() == Some(&n.uid));
        if !splits {
            m.absorb_into_text_gap(from, to);
        }
    }

    fn finish(&mut self, m: &mut Matcher<'a>) {
        let end = m.input.len();
        m.flush_text();
        while let Some(base) = self.parked.last().map(|s| s.base) {
            while self.path.len() > base {
                m.close_top(end);
                self.path.pop();
            }
            self.unpark_last(m);
        }
    }
}
