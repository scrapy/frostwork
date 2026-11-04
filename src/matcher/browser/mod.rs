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
//! * **Parked elements.** The head is [`Matcher::park`]ed in "after head", where whitespace goes to
//!   `<html>` but a later `<script>` still goes into the head, and so is the current node past Chrome's
//!   depth cap, where a new element becomes its sibling: the parked elements stay open, and are put back
//!   when the next insertion targets them. Foster parenting parks the table the same way, for the rest
//!   of the token that fostered; the streaming pass ends there (below).
//! * **Inert elements.** Template content and a declarative shadow root are parsed with the full rules
//!   but never reach the matcher; their tokens are absorbed into the text gap, so the text either side
//!   of a shadow-root `<template>` is one node, as in the browser.
//! * **Pending elements.** The adoption agency's clones enter the HTML5 stack without a matcher element
//!   and get one only if the same token inserts into them ([`Browser::materialize`]); the streaming pass
//!   ends after it (below).
//!
//! Each builder entry records its matcher depth and a uid, and `path` holds the uid of every matcher
//! element, so "is this entry's element open, and where" is one comparison. Lxml mode never runs any of
//! this: `OpenElem` and the libxml2 builder are unchanged.
//!
//! # Retroactive rules and the replay
//!
//! Everything that decides where a token goes, and every element's ancestry at the moment it opens,
//! streams. Five rules change a decision the matcher has already made:
//!
//! * a second `<html>` or `<body>` tag merges its attributes into an element already matched;
//! * foster parenting puts content before a table that is already open, so it precedes the table in
//!   document order and in the table's sibling positions, and fostered text joins the text node before
//!   the table;
//! * a `<frameset>` removes the body and everything in it;
//! * the adoption agency with a furthest block moves the block out of the formatting element, and the
//!   block's content into a copy of that element;
//! * Chrome copies the selected `<option>`'s content into its select's `<selectedcontent>` elements,
//!   which usually precede it.
//!
//! None of them fires on most pages: over 30,000 Common Crawl pages (`tools/retro_census.py`) a merge
//! that adds an attribute the page's own selectors ask about fires on 1.5%, the adoption agency with a
//! furthest block on 1.1%, foster parenting on 0.5%, the frameset on one page and `<selectedcontent>`
//! on none, 3.0% in all. So the streaming pass is unchanged on the common path, and the first of them to
//! fire ([`Browser::retro`]) ends it; [`run`] then answers the document with the REPLAY. That is the
//! same builder with `REPLAY` set: the same rules, but each insertion is a DOM operation on an arena of
//! the finished tree ([`dom::Dom`]: append, insert before the table, move, copy, detach), and
//! [`dom::Dom::walk`] then drives the matcher over it in pre-order, with each node's rank as its
//! position, so every value comes out in document order. A merge counts only when it adds an attribute
//! a selector asks about, and it is the one rule whose firing depends on the schema.
//!
//! The other design, a second pass that replays the tokens with hints about what is to come (a body
//! inert from the start, a table's subtree deferred past its fostered content, the adoption agency's
//! final parents applied when the block opens), needs every event known before it starts, so it runs
//! the builder over the whole document first, and it needs a mechanism per rule where the tree needs
//! one for all five. On a replayed page `extract` costs a median 1.35 and a 90th percentile 2.0 times
//! one streaming pass (200 such pages, minimum of 15 runs each), against 1.06 on pages that stream.
//!
//! Early exit is sound because the first rule to fire ends the streaming pass, so what it accepted was
//! the browser's; it stops only where no later token can reach back ([`Browser::settled`]), and a
//! replay walks a finished tree. The tail pass of `Matcher::resolve_tail_pass` must open what the first
//! pass opened, so it replays exactly when the first pass did ([`run_tail`]).

mod doctype;
mod dom;
mod lists;
mod rules;
mod tags;

use super::tree::{Scan, TreeBuilder};
use super::{CompiledSchema, EncodedInput, HeldText, Matcher, Parked};
use crate::{FlatColumns, GroupRows};
use crate::implied_close::{
    html5_cat, html5_data_mode, BUTTON_SCOPE, IMPLIED_END, IMPLIED_END_THOROUGH, LIST_ITEM_SCOPE, SCOPE,
    SPECIAL,
};
use lists::{Bound, Formatting, OpenStack};
use crate::tokenizer::DataMode;
use tags::{tag_of, tag_of_lower, Tag};
pub(super) use tags::{lower, namespaced_foreign_attr, svg_attr_name, svg_local_name, MAX_NAME};

type Attrs<'a> = [(&'a [u8], Option<&'a [u8]>)];

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Ns {
    Html,
    Svg,
    MathMl,
}

impl Ns {
    /// The matcher's class id for an element of this namespace.
    fn class(self) -> u8 {
        match self {
            Ns::Html => super::HTML_ELEMENT,
            Ns::Svg => super::SVG_ELEMENT,
            Ns::MathMl => super::MATHML_ELEMENT,
        }
    }
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
/// The page never wrote the element's start tag.
const SYNTH: u8 = 1 << 5;

/// One entry of the HTML5 stack of open elements.
#[derive(Clone, Copy)]
struct Node<'a> {
    /// The element's name as the matcher sees it: as written, or the camel-case SVG name.
    name: &'a [u8],
    tag: Tag,
    ns: Ns,
    /// `html5_cat` for an HTML element; special and scope for a foreign integration point.
    cat: u16,
    flags: u8,
    /// How the tokenizer reads the element's content.
    data: DataMode,
    uid: u32,
    vis: Vis,
    /// While `vis` is `Live`: its depth on the matcher stack when streaming, its node in the replay's
    /// tree when replaying.
    depth: u32,
}

/// The retroactive rules (see the module header), as the bits of [`Browser::retro`].
const RETRO_MERGE: u8 = 1 << 0;
const RETRO_FOSTER: u8 = 1 << 1;
const RETRO_FRAMESET: u8 = 1 << 2;
const RETRO_ADOPTION: u8 = 1 << 3;
const RETRO_SELECTEDCONTENT: u8 = 1 << 4;
/// Not a rule: a later `<html>` or `<body>` tag carried attributes, whether or not a selector asks
/// about them. Only [`retro_census`] reports it.
const CENSUS_MERGE_ANY: u8 = 1 << 5;
/// Past Chrome's depth cap an element opens beside the current node, which stays open, so later content
/// of the current node precedes the new element in document order. Not one of the five rules: the cap
/// is 512 open elements, and the replay is simply the exact answer there too.
const RETRO_DEPTH: u8 = 1 << 6;
const RETRO_RULES: u8 =
    RETRO_MERGE | RETRO_FOSTER | RETRO_FRAMESET | RETRO_ADOPTION | RETRO_SELECTEDCONTENT | RETRO_DEPTH;

/// The start tag a formatting element was created for, kept so a clone can be made from it. `span` is
/// the tag's source, which a clone's outer HTML begins with; `ark` is what Noah's Ark compares it by
/// ([`lists::ark_key`]).
struct FmtTok<'a> {
    name: &'a [u8],
    tag: Tag,
    attrs: Vec<(&'a [u8], Option<&'a [u8]>)>,
    span: (usize, usize),
    ark: u64,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Afe {
    Marker,
    El { uid: u32, tok: u32 },
}

/// A parked run of matcher elements: the uids that were `path[base..]`, and the text node the last of
/// them was buffering. Nothing is inserted into a parked element, so when it is put back that node is
/// still its last child and the next run continues it, as a browser appends to a trailing text node.
struct Seg<'a> {
    base: usize,
    uids: Vec<u32>,
    parked: Parked<'a>,
    held: Option<HeldText<'a>>,
}

struct StartTok<'a, 't> {
    name: &'a [u8],
    /// `name` ASCII-lowercased, or `None` when it is longer than any name the tables know.
    low: Option<&'t [u8]>,
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

/// The builder. `REPLAY` picks what it builds: the matcher's stack, streamed (`false`), or the replay's
/// tree (`true`). The rules are the same code either way; only the insertion primitives differ.
pub(super) struct Browser<'a, const REPLAY: bool> {
    mode: Mode,
    original: Mode,
    templates: Vec<Mode>,
    stack: OpenStack<'a>,
    afe: Formatting,
    toks: Vec<FmtTok<'a>>,
    /// The head element pointer, as the entry it had on the stack.
    head: Option<Node<'a>>,
    /// The head's matcher element is still open for "after head" to insert into.
    head_live: bool,
    /// Whitespace of `<html>` in "after head", held while head content goes into the head: the
    /// whitespace either side of it is one text node.
    html_text: Option<HeldText<'a>>,
    /// Text nodes of `<html>` in "after head" that a comment has ended, held with `html_text`: head
    /// content written after them still precedes them in document order.
    html_done: Vec<HeldText<'a>>,
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
    /// The retroactive rules that have fired (`RETRO_*`). Streaming, the first one ends the pass.
    retro: u8,
    /// Stream to the end whatever fires: to count the rules ([`retro_census`]), and for the tail pass of
    /// a document the first pass streamed ([`run_tail`]).
    run_through: bool,
    /// The replay's tree.
    dom: dom::Dom<'a>,
    /// The replay's selected option of each `<select>`, as `(select, option)` nodes.
    selected: Vec<(u32, u32)>,
    /// Where the next `<html>` or `<body>` tag-like bytes are at or after the offset of the last search,
    /// as `(searched from, found at)`; see [`Self::settled`].
    frame_tag: std::cell::Cell<(usize, usize)>,
}

impl<const REPLAY: bool> Default for Browser<'_, REPLAY> {
    fn default() -> Self {
        Browser {
            mode: Mode::Initial,
            original: Mode::InBody,
            templates: Vec::new(),
            stack: OpenStack::default(),
            afe: Formatting::default(),
            toks: Vec::new(),
            head: None,
            head_live: false,
            html_text: None,
            html_done: Vec::new(),
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
            retro: 0,
            run_through: false,
            dom: dom::Dom::new(),
            selected: Vec::new(),
            frame_tag: std::cell::Cell::new((usize::MAX, 0)),
        }
    }
}

/// How many open elements Chrome nests an element under (`kMaximumHTMLParserDOMTreeDepth`): with more
/// open, a new element goes into the current node's parent instead.
const MAX_DEPTH: usize = 512;

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

/// Does inserting the run clear frameset-ok? Chrome keeps it set for whitespace, NUL and U+FFFD, the
/// replacement character spelled as its UTF-8 bytes (`utf8`) or as a reference that resolves to it:
/// `&#0;`, a surrogate, a code point past U+10FFFF.
fn ends_frameset_ok(b: &[u8], utf8: bool) -> bool {
    let mut i = 0;
    while i < b.len() {
        let rest = &b[i..];
        let k = ws_at(rest, true);
        if k > 0 {
            i += k;
        } else if utf8 && rest.starts_with("\u{FFFD}".as_bytes()) {
            i += 3;
        } else if let Some((_, len)) =
            numeric_ref(rest).filter(|&(v, _)| v == 0 || v == 0xFFFD || (0xD800..=0xDFFF).contains(&v) || v > 0x10FFFF)
        {
            i += len;
        } else {
            return true;
        }
    }
    false
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

/// The offset of the first `<html` or `<body` at or after `from` that could start a tag (the name ends at
/// whitespace, `/`, `>` or the end of input), or `usize::MAX`. An occurrence in a comment or a script
/// only costs an early exit.
fn frame_tag_from(b: &[u8], from: usize) -> usize {
    let Some(rest) = b.get(from..) else { return usize::MAX };
    for i in memchr::memchr_iter(b'<', rest) {
        let name = &rest[i + 1..];
        let frame = name.get(..4).is_some_and(|n| n.eq_ignore_ascii_case(b"html") || n.eq_ignore_ascii_case(b"body"));
        if frame && name.get(4).is_none_or(|&c| is_ws(c) || c == b'/' || c == b'>') {
            return from + i;
        }
    }
    usize::MAX
}

/// Run `schema` over one document in browser mode. One streaming pass answers it unless a retroactive
/// rule fires; then the pass stops there, and the replay builds the document's tree and walks it.
pub(super) fn run<'a>(schema: &'a CompiledSchema, input: EncodedInput<'a>) -> (FlatColumns, Vec<GroupRows>) {
    let scan = Scan::tokenize(schema, input, Browser::<'a, false>::default(), Vec::new());
    if scan.builder.retro & RETRO_RULES == 0 {
        return scan.finish();
    }
    replay(schema, input, Vec::new())
}

/// The tail pass of a document (`Matcher::resolve_tail_pass`), which must open the elements the first
/// pass opened in the same order: replayed when the first pass was, and otherwise streamed to the end.
pub(super) fn run_tail<'a>(
    schema: &'a CompiledSchema,
    input: EncodedInput<'a>,
    marks: Vec<(u32, String)>,
    replayed: bool,
) -> (FlatColumns, Vec<GroupRows>) {
    if replayed {
        return replay(schema, input, marks);
    }
    Scan::tokenize(schema, input, Browser::<'a, false>::to_the_end(), marks).finish()
}

/// The replay: the whole tree, built with the DOM operations a browser performs, walked in pre-order.
fn replay<'a>(
    schema: &'a CompiledSchema,
    input: EncodedInput<'a>,
    marks: Vec<(u32, String)>,
) -> (FlatColumns, Vec<GroupRows>) {
    Scan::tokenize(schema, input, Browser::<'a, true>::default(), marks).finish()
}

/// Which retroactive rules fire on a document (`RETRO_*`, and [`CENSUS_MERGE_ANY`]), over the whole
/// document. A merge counts only when it adds an attribute `schema` selects on.
pub(super) fn retro_census(schema: &CompiledSchema, input: EncodedInput<'_>) -> u8 {
    Scan::tokenize(schema, input, Browser::<'_, false>::to_the_end(), Vec::new()).builder.retro
}

impl<const REPLAY: bool> Browser<'_, REPLAY> {
    /// A streaming builder that stays on to the end of input whatever fires (see `run_through`).
    fn to_the_end() -> Self {
        Browser { run_through: true, ..Browser::default() }
    }
}

impl<'a, const REPLAY: bool> Browser<'a, REPLAY> {
    // ------------------------------------------------------------------ nodes

    /// A stack entry for an element named `name`, which is `low` lowercased (see [`StartTok::low`]).
    fn new_node(&mut self, name: &'a [u8], low: Option<&[u8]>, tag: Tag, ns: Ns, attrs: &Attrs<'a>) -> Node<'a> {
        let uid = self.next_uid;
        self.next_uid += 1;
        let ip = SPECIAL | SCOPE | LIST_ITEM_SCOPE | BUTTON_SCOPE;
        let (tag, cat, flags) = match ns {
            Ns::Html => (tag, html5_cat(low.unwrap_or(b"")), 0),
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
        let data = match ns {
            Ns::Html => low.map_or(DataMode::Normal, html5_data_mode),
            Ns::Svg | Ns::MathMl => DataMode::Normal,
        };
        Node { name, tag, ns, cat, flags, data, uid, vis: Vis::Live, depth: 0 }
    }

    fn cur(&self) -> &Node<'a> {
        self.stack.last().expect("the stack of open elements is empty")
    }

    fn cur_is(&self, tag: Tag) -> bool {
        self.stack.last().is_some_and(|n| n.tag == tag)
    }

    fn index_of(&self, uid: u32) -> Option<usize> {
        self.stack.index_of(uid)
    }

    fn has_template(&self) -> bool {
        self.stack.topmost(Tag::Template).is_some()
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
            return match self.stack.topmost_of(&[Tag::Table, Tag::Template]) {
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
        while self.path.get(d) != Some(&uid) {
            if self.parked.is_empty() {
                debug_assert!(false, "a live element is neither open nor parked");
                return;
            }
            self.unpark_last(m, at);
        }
        self.drop_to(m, d + 1, at);
    }

    /// Close the matcher's current element, which ends at `at`.
    fn close_current(&mut self, m: &mut Matcher<'a>, at: usize) {
        m.flush_text();
        m.close_top(at);
        self.path.pop();
        self.touched = true;
    }

    /// Drop the matcher's elements above the first `len` (see [`Self::drop_top`]).
    fn drop_to(&mut self, m: &mut Matcher<'a>, len: usize, at: usize) {
        while self.path.len() > len {
            self.drop_top(m, at);
        }
    }

    /// The matcher's current element has no builder entry: close it, or park the head while "after
    /// head" may still insert into it.
    fn drop_top(&mut self, m: &mut Matcher<'a>, at: usize) {
        let top = *self.path.last().expect("dropping with nothing open");
        if self.head_live && self.head.is_some_and(|h| h.uid == top) {
            m.flush_text();
            return self.park_from(m, self.path.len() - 1);
        }
        debug_assert!(
            self.index_of(top).is_none_or(|k| self.stack[k].vis != Vis::Live),
            "closing an element the builder still has open"
        );
        self.close_current(m, at);
    }

    fn park_from(&mut self, m: &mut Matcher<'a>, base: usize) {
        self.touched = true;
        let held = m.take_text();
        let uids = self.path.split_off(base);
        let parked = m.park(base);
        self.parked.push(Seg { base, uids, parked, held });
    }

    /// Put the last parked run back, dropping what was opened since it was parked; its held text node
    /// continues to `at`.
    fn unpark_last(&mut self, m: &mut Matcher<'a>, at: usize) {
        let base = self.parked.last().expect("unparking with nothing parked").base;
        self.drop_to(m, base, at);
        m.flush_text();
        self.touched = true;
        let seg = self.parked.pop().expect("unparking with nothing parked");
        debug_assert_eq!(self.path.len(), seg.base);
        m.unpark(seg.parked);
        self.path.extend(seg.uids);
        if let Some(held) = seg.held {
            m.restore_text(held, at);
        }
    }

    /// Make the DOM parent of the table at stack index `t` the matcher's current element, parking the
    /// table and what is open in it. Consecutive fostered nodes find the table already parked.
    fn foster_place(&mut self, m: &mut Matcher<'a>, t: usize, at: usize) {
        let (tuid, base) = (self.stack[t].uid, self.stack[t].depth as usize);
        if self.parked.last().is_some_and(|s| s.base == base && s.uids.first() == Some(&tuid)) {
            return self.drop_to(m, base, at);
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
        m.set_source_prefix(self.toks[tok as usize].span);
        self.stack.replace(i, node);
        self.toks[tok as usize].attrs = attrs;
    }

    /// Open `node` as the matcher's next element, at the current insertion point.
    fn open(&mut self, m: &mut Matcher<'a>, node: &mut Node<'a>, attrs: &Attrs<'a>, at: usize) {
        m.flush_text();
        let class = node.ns.class();
        m.open_element(node.name, class, attrs, at);
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
        if REPLAY {
            self.dom.set_end(n.depth, end);
            if n.tag == Tag::Option {
                self.option_closed(n.depth);
            }
            return;
        }
        let d = n.depth as usize;
        while self.path.get(d) != Some(&n.uid) {
            // Parked: what was opened after it closes first, segment by segment, as ghosts. Past the
            // depth cap the adoption agency can leave an element open in a later segment, which streaming
            // cannot close before it; only a census pass streams that far, since the cap is a replay.
            let Some(k) = self.parked.iter().rposition(|seg| seg.uids.contains(&n.uid)) else { return };
            let seg = &self.parked[k];
            let above = seg.uids.iter().skip_while(|&&u| u != n.uid).skip(1);
            let mut later = self.path[seg.base..].iter().chain(above).chain(self.parked[k + 1..].iter().flat_map(|s| &s.uids));
            if later.any(|&u| self.index_of(u).is_some_and(|i| self.stack[i].vis == Vis::Live)) {
                return;
            }
            self.unpark_last(m, ghost_end);
        }
        self.drop_to(m, d + 1, ghost_end);
        self.close_current(m, end);
    }

    // ------------------------------------------------------------------ inserting

    /// Insert `node` where the next node goes and push it onto the stack of open elements.
    fn insert_node(&mut self, m: &mut Matcher<'a>, mut node: Node<'a>, attrs: &Attrs<'a>, at: usize) {
        match self.location() {
            Loc::Inert => node.vis = Vis::Inert,
            Loc::Document if REPLAY => {
                self.dom_element(&mut node, attrs, at);
                self.dom.set_root(node.depth);
            }
            Loc::Document => self.open(m, &mut node, attrs, at),
            Loc::Into(i) => {
                if !REPLAY {
                    self.place(m, i, at);
                }
                // Past Chrome's depth cap the element is the current node's sibling.
                let capped = self.stack.len() > MAX_DEPTH && i > 0 && self.stack[i].vis == Vis::Live;
                if !capped {
                    self.stack[i].flags |= HAS_CHILD;
                }
                if REPLAY {
                    let target = self.stack[i].depth;
                    let parent = if capped { self.dom.parent(target) } else { target };
                    self.dom_element(&mut node, attrs, at);
                    self.dom.append(parent, node.depth);
                } else {
                    if capped {
                        // The current node stays open, so it parks like a fostered table.
                        self.retro |= RETRO_DEPTH;
                        let d = self.stack[i].depth as usize;
                        self.park_from(m, d);
                    }
                    self.open(m, &mut node, attrs, at);
                }
            }
            Loc::Foster(t) => {
                self.retro |= RETRO_FOSTER;
                if REPLAY {
                    self.dom_element(&mut node, attrs, at);
                    self.dom_foster(t, node.depth);
                } else {
                    self.foster_place(m, t, at);
                    self.open(m, &mut node, attrs, at);
                }
            }
        }
        self.data = node.data;
        self.stack.push(node);
    }

    /// The replay's element for `node`, not yet in the tree.
    fn dom_element(&mut self, node: &mut Node<'a>, attrs: &Attrs<'a>, at: usize) {
        node.depth = self.dom.element(node.name, node.ns.class(), attrs.to_vec(), at);
        node.vis = Vis::Live;
    }

    /// Put the replay's node `id` where foster parenting puts it: before the table at stack index `t`,
    /// in the table's parent, or at the end of the element above the table when it has none.
    fn dom_foster(&mut self, t: usize, id: u32) {
        let table = self.stack[t].depth;
        if self.dom.parent(table) != dom::NIL {
            self.dom.insert_before(table, id);
        } else {
            self.dom.append(self.stack[t - 1].depth, id);
        }
    }

    /// Insert an HTML element for a start tag.
    fn insert_html(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        let node = self.new_node(t.name, t.low, t.tag, Ns::Html, t.attrs);
        self.insert_node(m, node, t.attrs, t.start);
        self.dom_open_end(t.end);
    }

    /// The replay's element just inserted has a start tag ending at `end`.
    fn dom_open_end(&mut self, end: usize) {
        if let Some(n) = self.stack.last().filter(|n| REPLAY && n.vis == Vis::Live) {
            self.dom.set_open_end(n.depth, end);
        }
    }

    /// Insert an element the page never wrote (no attributes, no source of its own), at `at`.
    fn insert_synth(&mut self, m: &mut Matcher<'a>, name: &'static [u8], tag: Tag, at: usize) {
        let mut node = self.new_node(name, Some(name), tag, Ns::Html, &[]);
        node.flags |= SYNTH;
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
            Ns::Svg => t.low.and_then(tags::svg_camel_case).unwrap_or(t.name),
            _ => t.name,
        };
        let node = self.new_node(name, t.low, Tag::Other, ns, t.attrs);
        self.insert_node(m, node, t.attrs, t.start);
        self.dom_open_end(t.end);
        if t.self_closing {
            self.pop(m, t.end);
        }
    }

    /// Insert a character run where the next node goes.
    fn insert_text(&mut self, m: &mut Matcher<'a>, b: &'a [u8], dm: DataMode, s: usize) {
        match self.location() {
            Loc::Inert | Loc::Document => self.absorb(m, s, s + b.len()),
            Loc::Into(i) => {
                if REPLAY {
                    if !Self::no_node(m, b, dm) {
                        self.dom.append_text(self.stack[i].depth, (b, dm, s));
                    }
                } else {
                    self.place(m, i, s);
                    m.buffer_text(b, dm, s);
                }
                self.stack[i].flags |= HAS_CHILD;
            }
            Loc::Foster(t) => {
                self.retro |= RETRO_FOSTER;
                if REPLAY {
                    if !Self::no_node(m, b, dm) {
                        let table = self.stack[t].depth;
                        if self.dom.parent(table) != dom::NIL {
                            self.dom.text_before(table, (b, dm, s));
                        } else {
                            self.dom.append_text(self.stack[t - 1].depth, (b, dm, s));
                        }
                    }
                } else {
                    self.foster_place(m, t, s);
                    m.buffer_text(b, dm, s);
                }
            }
        }
    }

    /// A run that is no text node: markup-mode NUL, which a browser drops (see [`Matcher::buffer_text`]).
    fn no_node(m: &Matcher<'a>, b: &[u8], dm: DataMode) -> bool {
        m.input.keeps_nul() && dm == DataMode::Normal && b.iter().all(|&c| c == 0)
    }

    /// Markup over `[from, to)` that leaves no node. Streaming, the text node around it continues
    /// across it; the replay's tree needs no telling.
    fn absorb(&self, m: &mut Matcher<'a>, from: usize, to: usize) {
        if !REPLAY {
            m.absorb_into_text_gap(from, to);
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

    /// The index of the topmost HTML element with `tag`, if it is in scope: no element of `bound`
    /// is above it.
    fn in_scope_of(&self, tag: Tag, bound: Bound) -> Option<usize> {
        self.stack.scoped(self.stack.topmost(tag), bound)
    }

    fn in_scope(&self, tag: Tag) -> Option<usize> {
        self.in_scope_of(tag, Bound::Scope)
    }

    fn in_button_scope(&self, tag: Tag) -> Option<usize> {
        self.in_scope_of(tag, Bound::Button)
    }

    fn in_list_scope(&self, tag: Tag) -> Option<usize> {
        self.in_scope_of(tag, Bound::ListItem)
    }

    fn in_table_scope(&self, tag: Tag) -> Option<usize> {
        self.in_scope_of(tag, Bound::Table)
    }

    /// Generate implied end tags except for `except`, then pop through the topmost element with one of
    /// `tags`. Entries above it end at `ghost_end`, it at `end`.
    fn close_element(&mut self, m: &mut Matcher<'a>, tags: &[Tag], except: Option<Tag>, ghost_end: usize, end: usize) {
        self.implied_end(m, except, ghost_end);
        if let Some(k) = self.stack.topmost_of(tags) {
            self.pop_to(m, k, ghost_end, end);
        }
    }

    /// Close a `<p>` element: implied end tags except `p`, then pop through the `<p>`.
    fn close_p(&mut self, m: &mut Matcher<'a>, ghost_end: usize, end: usize) {
        self.close_element(m, &[Tag::P], Some(Tag::P), ghost_end, end);
    }

    fn close_p_if_in_button_scope(&mut self, m: &mut Matcher<'a>, at: usize) {
        if self.in_button_scope(Tag::P).is_some() {
            self.close_p(m, at, at);
        }
    }

    // ------------------------------------------------------------------ active formatting elements

    fn afe_last_marker(&self) -> usize {
        self.afe.after_last_marker()
    }

    fn afe_index_of(&self, uid: u32) -> Option<usize> {
        self.afe.index_of(uid)
    }

    fn clear_afe_to_marker(&mut self) {
        self.afe.clear_to_marker(&self.toks);
    }

    fn afe_push(&mut self, e: Afe) {
        self.afe.push(e, &self.toks);
    }

    fn afe_remove(&mut self, k: usize) {
        self.afe.remove(k, &self.toks);
    }

    /// Do two formatting tokens carry the same attributes, as Noah's Ark compares them: by name, the
    /// first of a repeated name, in any order, with equal values.
    fn same_attrs(&self, m: &Matcher<'a>, a: &FmtTok<'a>, b: &FmtTok<'a>) -> bool {
        let first = |attrs: &[(&'a [u8], Option<&'a [u8]>)], j: usize| {
            !attrs[..j].iter().any(|(n, _)| n.eq_ignore_ascii_case(attrs[j].0))
        };
        let names = |attrs: &[(&'a [u8], Option<&'a [u8]>)]| (0..attrs.len()).filter(|&j| first(attrs, j)).count();
        names(&a.attrs) == names(&b.attrs) && (0..a.attrs.len()).filter(|&j| first(&a.attrs, j)).all(|j| {
            let (n, v) = a.attrs[j];
            b.attrs.iter().find(|(n2, _)| n.eq_ignore_ascii_case(n2)).is_some_and(|&(_, v2)| {
                let (v, v2) = (v.unwrap_or(b""), v2.unwrap_or(b""));
                v == v2 || m.input.attr_value(v) == m.input.attr_value(v2)
            })
        })
    }

    /// Push the element `uid`, made for token `tok`, onto the list of active formatting elements. Noah's
    /// Ark: with three alike after the last marker already, the earliest of them leaves. The list counts
    /// entries by key, so only then is it searched, from the end, for as many as have the key.
    fn push_afe(&mut self, m: &Matcher<'a>, uid: u32, tok: u32) {
        let t = &self.toks[tok as usize];
        let mut keyed = self.afe.alike(t.ark);
        if keyed >= 3 {
            let (mut same, mut earliest) = (0, None);
            let mut k = self.afe.len();
            while keyed > 0 && k > self.afe_last_marker() {
                k -= 1;
                if let Afe::El { tok: o, .. } = self.afe[k] {
                    let other = &self.toks[o as usize];
                    if other.ark == t.ark {
                        keyed -= 1;
                        if other.tag == t.tag && self.same_attrs(m, other, t) {
                            same += 1;
                            earliest = Some(k);
                        }
                    }
                }
            }
            if same >= 3 {
                self.afe_remove(earliest.expect("three of a kind"));
            }
        }
        self.afe_push(Afe::El { uid, tok });
    }

    /// Insert a formatting element for a start tag and push it onto the list.
    fn insert_formatting(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        let tok = self.toks.len() as u32;
        let attrs = t.attrs.to_vec();
        let firsts = (0..attrs.len()).filter(|&j| !attrs[..j].iter().any(|(n, _)| n.eq_ignore_ascii_case(attrs[j].0)));
        let values: Vec<(&[u8], std::borrow::Cow<'_, str>)> =
            firsts.map(|j| (attrs[j].0, m.input.attr_value(attrs[j].1.unwrap_or(b"")))).collect();
        let ark = lists::ark_key(t.tag, values.iter().map(|(n, v)| (*n, v.as_ref())));
        self.toks.push(FmtTok { name: t.name, tag: t.tag, attrs, span: (t.start, t.end), ark });
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
            let mut buf = [0; MAX_NAME];
            let node = self.new_node(name, lower(name, &mut buf), tag, Ns::Html, &attrs);
            let uid = node.uid;
            self.insert_node(m, node, &attrs, at);
            let span = self.toks[tok as usize].span;
            if REPLAY {
                let n = *self.cur();
                if n.vis == Vis::Live {
                    self.dom.set_prefix(n.depth, span);
                }
            } else if self.path.last() == Some(&uid) {
                m.set_source_prefix(span);
            }
            self.toks[tok as usize].attrs = attrs;
            self.afe.set(j, Afe::El { uid, tok });
        }
    }

    // ------------------------------------------------------------------ the frame

    /// A token before any doctype: the document is in quirks mode.
    fn no_doctype(&mut self) {
        self.quirks = true;
        self.mode = Mode::BeforeHtml;
    }

    /// Insert the `<html>` the page never wrote, at `at`.
    fn imply_html(&mut self, m: &mut Matcher<'a>, at: usize) {
        self.insert_synth(m, b"html", Tag::Html, at);
        self.mode = Mode::BeforeHead;
    }

    /// Insert the `<head>` the page never wrote, at `at`: the head element pointer.
    fn imply_head(&mut self, m: &mut Matcher<'a>, at: usize) {
        self.insert_synth(m, b"head", Tag::Head, at);
        self.head = self.stack.last().copied();
        self.mode = Mode::InHead;
    }

    /// Leave "after head" for a `<body>` the page never wrote, inserted at `at`.
    fn imply_body(&mut self, m: &mut Matcher<'a>, at: usize) {
        self.finish_head(m, at);
        self.insert_synth(m, b"body", Tag::Body, at);
        self.frameset_ok = true;
        self.mode = Mode::InBody;
    }

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
        // `<html>`'s open text node follows the ones a comment ended, so it waits for them.
        if !REPLAY && self.html_text.is_none() && !self.html_done.is_empty() && self.path.len() == 1 {
            self.html_text = m.take_text();
        }
        if let Some(h) = self.head {
            self.close_node(m, h, at, at);
        }
        self.release_html_text(m, at);
    }

    /// The head can take nothing more: deliver the held `<html>` text nodes in order, and put the open
    /// one back, continuing to `at`, with `<html>` the current element.
    fn release_html_text(&mut self, m: &mut Matcher<'a>, at: usize) {
        if REPLAY || (self.html_text.is_none() && self.html_done.is_empty()) {
            return;
        }
        self.place(m, 0, at);
        for held in std::mem::take(&mut self.html_done) {
            m.restore_text(held, at);
            m.flush_text();
        }
        if let Some(held) = self.html_text.take() {
            m.restore_text(held, at);
        }
    }

    /// Whitespace of `<html>` in "after head" continues its open text node: put that back, continuing
    /// to `at`, with `<html>` the current element. The nodes a comment ended stay held.
    fn continue_html_text(&mut self, m: &mut Matcher<'a>, at: usize) {
        if REPLAY {
            return;
        }
        if let Some(held) = self.html_text.take() {
            self.place(m, 0, at);
            m.restore_text(held, at);
        }
    }

    /// A comment of `<html>` in "after head" ends `<html>`'s open text node, held or buffered.
    fn end_html_text(&mut self, m: &mut Matcher<'a>) {
        let open = match self.html_text.take() {
            Some(held) => Some(held),
            None if self.path.len() == 1 => m.take_text(),
            None => None,
        };
        self.html_done.extend(open);
    }

    /// Reset the insertion mode appropriately, from the topmost element that decides it. The stack's
    /// bottom is always the `<html>`, so the spec's "last" cases (a fragment's context) never arise.
    fn reset_mode(&mut self) {
        const DECIDES: [Tag; 14] = [
            Tag::Td, Tag::Th, Tag::Tr, Tag::Tbody, Tag::Thead, Tag::Tfoot, Tag::Caption, Tag::Colgroup,
            Tag::Table, Tag::Template, Tag::Head, Tag::Body, Tag::Frameset, Tag::Html,
        ];
        let Some(i) = self.stack.topmost_of(&DECIDES) else {
            self.mode = Mode::InBody;
            return;
        };
        self.mode = match self.stack[i].tag {
            Tag::Td | Tag::Th => Mode::InCell,
            Tag::Tr => Mode::InRow,
            Tag::Tbody | Tag::Thead | Tag::Tfoot => Mode::InTableBody,
            Tag::Caption => Mode::InCaption,
            Tag::Colgroup => Mode::InColumnGroup,
            Tag::Table => Mode::InTable,
            Tag::Template => *self.templates.last().unwrap_or(&Mode::InBody),
            Tag::Head => Mode::InHead,
            Tag::Body => Mode::InBody,
            Tag::Frameset => Mode::InFrameset,
            Tag::Html if self.head.is_none() => Mode::BeforeHead,
            _ => Mode::AfterHead,
        };
    }

    // ------------------------------------------------------------------ early exit

    /// Can nothing after `at` change a value the matcher has already accepted? Before the first
    /// retroactive rule fires, every accepted value is the browser's; a rule that fires later reaches
    /// back only through what is open now, or through a later `<html>`/`<body>` start tag.
    fn settled(&self, m: &Matcher<'a>, at: usize) -> bool {
        // A `<frameset>` would remove the body, with everything in it.
        if self.frameset_ok && self.stack.get(1).is_some_and(|n| n.tag == Tag::Body) {
            return false;
        }
        // Content fostered out of an open table goes before everything the table holds.
        if self.stack.all(Tag::Table).any(|k| self.stack[k].vis == Vis::Live) {
            return false;
        }
        // A special element open inside a formatting element can still be its furthest block, moved
        // out of it with its content.
        let formatting = self.stack.iter().position(|n| n.vis != Vis::Inert && self.afe_index_of(n.uid).is_some());
        if formatting.is_some_and(|k| self.stack.iter().skip(k + 1).any(|n| n.vis != Vis::Inert && n.cat & SPECIAL != 0)) {
            return false;
        }
        // A later `<html>` or `<body>` start tag adds its attributes to the open one.
        let (from, found) = self.frame_tag.get();
        let found = if from <= at && found >= at {
            found
        } else {
            let found = frame_tag_from(m.input.bytes(), at);
            self.frame_tag.set((at, found));
            found
        };
        found == usize::MAX
    }

    // ------------------------------------------------------------------ the replay's other rules

    /// The `<select>` an option or a `<selectedcontent>` in the replay's tree belongs to: its nearest
    /// ancestor `<select>`, with no `<option>` in between, and for an option no `<datalist>` or `<hr>`
    /// and at most one `<optgroup>` either.
    fn owner_select(&self, id: u32, option: bool) -> Option<u32> {
        let mut optgroups = 0;
        let mut a = self.dom.parent(id);
        while a != dom::NIL {
            let e = self.dom.elem(a)?;
            if e.class == super::HTML_ELEMENT {
                let mut buf = [0; MAX_NAME];
                match lower(e.name, &mut buf) {
                    Some(b"select") => return Some(a),
                    Some(b"option") => return None,
                    Some(b"datalist" | b"hr") if option => return None,
                    Some(b"optgroup") if option => {
                        optgroups += 1;
                        if optgroups > 1 {
                            return None;
                        }
                    }
                    _ => {}
                }
            }
            a = self.dom.parent(a);
        }
        None
    }

    fn has_attr(&self, id: u32, name: &[u8]) -> bool {
        self.dom.elem(id).is_some_and(|e| e.attrs.iter().any(|(n, _)| n.eq_ignore_ascii_case(name)))
    }

    /// An option has been inserted into the replay's tree: its select's selectedness, as Chrome sets it
    /// while parsing. A `selected` option becomes the selected one; otherwise the first option that is
    /// not disabled does, in a select that shows one option (no `multiple`, `size` at most 1).
    fn option_inserted(&mut self, option: u32) {
        let Some(select) = self.owner_select(option, true) else { return };
        let current = self.selected.iter().position(|&(s, _)| s == select);
        let selected = if self.has_attr(option, b"selected") {
            true
        } else {
            current.is_none() && !self.has_attr(option, b"disabled") && self.shows_one(select)
        };
        match (selected, current) {
            (true, Some(k)) => self.selected[k].1 = option,
            (true, None) => self.selected.push((select, option)),
            (false, _) => {}
        }
    }

    /// Does the select show a single option, so that one is selected by default?
    fn shows_one(&self, select: u32) -> bool {
        let Some(e) = self.dom.elem(select) else { return false };
        if e.attrs.iter().any(|(n, _)| n.eq_ignore_ascii_case(b"multiple")) {
            return false;
        }
        let size = e.attrs.iter().find(|(n, _)| n.eq_ignore_ascii_case(b"size")).and_then(|(_, v)| *v);
        size.and_then(|v| std::str::from_utf8(v).ok()).and_then(|v| v.trim().parse::<u64>().ok()).is_none_or(|n| n <= 1)
    }

    /// The `<selectedcontent>` elements of `select` that take its selected option's content: none of a
    /// `multiple` select's, and none inside an option.
    fn selectedcontents(&self, select: u32) -> Vec<u32> {
        if self.has_attr(select, b"multiple") {
            return Vec::new();
        }
        let mut all = self.dom.descendants_where(select, |e| {
            e.class == super::HTML_ELEMENT && e.name.eq_ignore_ascii_case(b"selectedcontent")
        });
        all.retain(|&sc| self.owner_select(sc, false) == Some(select));
        all
    }

    /// Replace the content of `target` with copies of the option's.
    fn copy_option(&mut self, option: u32, target: u32) {
        self.dom.clear_children(target);
        let mut c = self.dom.first_child(option);
        while c != dom::NIL {
            let copy = self.dom.deep_clone(c);
            self.dom.append(target, copy);
            c = self.dom.next_sibling(c);
        }
    }

    /// An option has left the stack of open elements, popped or removed: if it is its select's selected
    /// option, Chrome copies its content into the select's `<selectedcontent>` elements.
    fn option_closed(&mut self, option: u32) {
        let Some(select) = self.owner_select(option, true) else { return };
        if !self.selected.iter().any(|&(s, o)| s == select && o == option) {
            return;
        }
        for sc in self.selectedcontents(select) {
            self.copy_option(option, sc);
        }
    }

    /// A `<selectedcontent>` has been inserted: it takes the content of its select's selected option.
    fn selectedcontent_inserted(&mut self, sc: u32) {
        let Some(select) = self.owner_select(sc, false) else { return };
        if self.has_attr(select, b"multiple") {
            return;
        }
        if let Some(&(_, option)) = self.selected.iter().find(|&&(s, _)| s == select) {
            self.copy_option(option, sc);
        }
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

impl<'a, const REPLAY: bool> TreeBuilder<'a> for Browser<'a, REPLAY> {
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
        let mut buf = [0; MAX_NAME];
        let low = lower(name, &mut buf);
        let tag = low.map_or(Tag::Other, tag_of_lower);
        let t = StartTok { name, low, tag, attrs: raw_attrs, self_closing, start: span_start, end: open_end };
        let before = self.stack.last().map(|n| n.uid);
        self.start(m, &t);
        if !self.touched {
            self.absorb(m, span_start, open_end);
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
            self.absorb(m, close_start, close_end);
        }
    }

    fn text(&mut self, m: &mut Matcher<'a>, text: &'a [u8], mode: DataMode, start: usize) {
        let (mut b, mut s) = (text, start);
        if std::mem::take(&mut self.skip_lf) {
            let k = leading_lf(b);
            if k > 0 {
                self.absorb(m, s, s + k);
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
        self.absorb(m, from, to);
    }

    fn comment(&mut self, m: &mut Matcher<'a>, from: usize, to: usize) {
        self.skip_lf = false;
        // A comment is a node of whatever it is inserted into. It ends the text node there; inserted
        // anywhere else (the document, the `<html>` after the body, a table whose text is fostered out
        // of it, template content) it leaves the pending text node open.
        let target = match self.mode {
            Mode::Initial | Mode::BeforeHtml | Mode::AfterAfterBody | Mode::AfterAfterFrameset => None,
            Mode::AfterBody => self.stack.first().copied(),
            _ => self.stack.last().copied(),
        };
        if REPLAY {
            if let Some(n) = target.filter(|n| n.vis == Vis::Live && n.tag != Tag::Template) {
                self.dom.append_comment(n.depth);
            }
            return;
        }
        if self.mode == Mode::AfterHead {
            return self.end_html_text(m);
        }
        let splits =
            target.is_some_and(|n| n.vis == Vis::Live && n.tag != Tag::Template && self.path.last() == Some(&n.uid));
        if !splits {
            m.absorb_into_text_gap(from, to);
        }
    }

    fn finish(&mut self, m: &mut Matcher<'a>) {
        let end = m.input.len();
        // An early exit stops before the end of input, once every value is final.
        if !m.done() {
            self.eof(m, end);
        }
        if REPLAY {
            // End of input pops everything, the current node first; then the finished tree is walked.
            while self.stack.len() > 0 {
                self.pop(m, end);
            }
            return self.dom.walk(m);
        }
        // Streaming, the matcher closes what is open when it finishes, and what is parked is put back
        // for it first.
        while let Some(base) = self.parked.last().map(|s| s.base) {
            while self.path.len() > base {
                self.close_current(m, end);
            }
            self.unpark_last(m, end);
        }
    }

    /// Streaming stops at the first retroactive rule, which a replay answers instead, and once the
    /// matcher is done if nothing that follows can change what it has seen ([`Self::settled`]). A
    /// replay builds the whole tree.
    fn stop(&self, m: &Matcher<'a>, at: usize) -> bool {
        !REPLAY && ((self.retro & RETRO_RULES != 0 && !self.run_through) || (m.done() && self.settled(m, at)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `query` over `html`, streamed to the end past every retroactive rule, as a census pass streams.
    fn stream_through(html: &[u8], query: &str) -> Vec<String> {
        let (flat, grouped) = crate::compile_schema(&[query.to_string()], &[]);
        let schema = CompiledSchema::compile(&flat, &grouped, crate::ParseMode::Browser);
        let input = EncodedInput::new(html, encoding_rs::UTF_8, true);
        run_tail(&schema, input, Vec::new(), false).0.remove(0)
    }

    /// Past the depth cap the adoption agency leaves the formatting element's matcher element parked
    /// after the block's; a streamed pass still closes each element where it is popped, not at the end
    /// of input. (Extraction answers such a document with the replay; only a census streams it.)
    #[test]
    fn a_streamed_pass_past_the_depth_cap_closes_what_it_pops() {
        let mut h: String = (0..511).map(|i| format!("<div id=d{i}>")).collect();
        h += "<b id=b>1<div id=x>2</b>3</div></div><p id=p>4</p>";
        assert_eq!(stream_through(h.as_bytes(), "#d510"), ["<div id=d510><b id=b>1<div id=x>2</b>3</div></div>"]);
        assert_eq!(stream_through(h.as_bytes(), "#x"), ["<div id=x>2</b>3</div>"]);
    }
}
