//! The libxml2 tree builder: lxml mode's tree-construction decisions, made over the shared matcher
//! primitives (see [`super::tree`]). Everything here is a DECISION about the tree (which elements an
//! incoming tag closes, where the document frame comes from, which end tags are discarded and which text
//! ends the head), matched empirically to libxml2 2.15. The work every mode shares (evaluating an opened
//! element, finalizing a closed one, buffering text) lives on [`Matcher`].
//!
//! The rule tables come from `crate::implied_close` (derived from the oracle) and the frame questions
//! from [`super::frame`]; this module only combines them.

use super::frame::{
    frame_slot, frameset_is_open, head_is_current, head_is_open, nothing_open, only_html_open,
    tag_is_redundant, DocumentFrame,
};
use super::tree::TreeBuilder;
use super::Matcher;
use crate::implied_close::{
    blocks_end_tag, classify, data_mode_of, end_tag_discardable, frame_content_of, start_closes,
    FrameContent,
};
use crate::tokenizer::DataMode;

/// The offset of the first byte that is CONTENT for the document-frame rules, if any: neither HTML
/// whitespace nor NUL. Browsers ignore a NUL character before the body as if it were not there
/// (measured: `\0<title>` keeps the title in the head), and lxml mode deletes every NUL before
/// tokenizing, so skipping it here is the same rule in both modes.
fn first_content_byte(text: &[u8]) -> Option<usize> {
    text.iter().position(|c| !matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0c | 0))
}

/// libxml2's tree construction, as lxml mode runs it.
#[derive(Default)]
pub(super) struct Libxml2 {
    /// Everything the document-frame rules know that the stack cannot tell them, plus the questions they
    /// ask about it — see `frame.rs`, which explains why this is one owner of NAMED questions rather than
    /// the insertion-mode enum it looks like it should be.
    frame: DocumentFrame,
}

impl Libxml2 {
    /// Insert a start tag under libxml2's rules: build whatever of the document frame it needs, run the
    /// implied closes, and open the element (or ignore a redundant frame tag). A void element and a
    /// self-closed one close again at once.
    fn insert<'a>(
        &mut self,
        m: &mut Matcher<'a>,
        name: &'a [u8],
        raw_attrs: &[(&'a [u8], Option<&'a [u8]>)],
        self_closing: bool,
        span_start: usize,
        open_end: usize,
    ) {
        let (void, scid) = classify(name);
        let frame = frame_slot(name);
        // Build whatever of the document frame this tag needs and the byte stream did not write.
        self.ensure_frame(m, name, frame, span_start);
        // A document-frame tag that is BOTH out of place and closes nothing is completely invisible to
        // libxml2, so it must not split the text node either: absorb it into the pending node's gap the
        // way a dropped end tag is absorbed. Decided before `flush_text`, which would end the node.
        if frame.is_some() && tag_is_redundant(name, &m.stack) && !closes_top(m, name, scid) {
            self.frame.note_ignored();
            if self_closing {
                self_close_with_no_element_of_its_own(m, span_start);
                return;
            }
            m.absorb_into_text_gap(span_start, open_end);
            return;
        }
        // Any buffered text belongs to the element open BEFORE this tag reshapes the stack.
        m.flush_text();
        // inline implied-close reshape: a popped element's raw source ends where this tag begins
        let mut popped_head = false;
        while let Some(top) = m.stack.last() {
            let closes = start_closes(scid, top.scid);
            if !crate::mutate::closes(name, top.tag, closes) {
                break;
            }
            popped_head |= self.frame.head_seen() && top.tag.eq_ignore_ascii_case(b"head");
            m.close_top(span_start);
        }
        // A start tag that ENDED the head also STARTS the body, and everything from here on — including
        // the remaining `<meta>`/`<link>`/`<title>` — belongs to it. libxml2 and html5lib agree on this
        // exactly. Closing the head correctly but leaving the content under `<html>` is the near miss
        // here, and it is what makes `head + body`, `html > body` and `:first-child` disagree.
        //
        // Only for an IMPLICIT end. After an explicit `</head>` the two oracles part company (libxml2
        // leaves a following `<meta>` at `<html>` level, html5lib puts it back in the head), so that path
        // keeps libxml2's shape, which is what the engine already produces.
        //
        // A real `<body>`/`<html>`/`<head>` is excluded because it opens the frame itself — and `<body>`
        // in particular reaches here having just popped the head through the same relation. So is a tag
        // that opens NEITHER part: a frameset document has no `<body>` at all, and `<frameset>` ends the
        // head like any other non-head content. Wrapping that in an invented body would put the whole
        // frameset inside one, where libxml2 makes it a child of `<html>`. `frame_content` is the question
        // to ask here, not "is this a frame tag".
        if popped_head
            && frame.is_none()
            && !self.frame.body_established()
            && frame_content_of(name) == FrameContent::Body
        {
            self.insert(m, b"body", &[], false, span_start, span_start);
        }
        // ... and once the implied closes have run, a redundant frame tag inserts NOTHING. libxml2
        // merges its attributes onto the element that already exists; there is nowhere to merge them
        // here, and inventing a second `<body>` inside the first is the observable error (it gave
        // `<div>d<html>y</div>` a spurious `html` child, and `<p>x<body>y` two text nodes).
        // Re-evaluated HERE, not before the reshape: `<body>` closes an open `<head>`, and until that
        // pop has happened the head still makes the tag look redundant — which swallowed the `<body>` of
        // every document that omits `</head>`.
        if let Some(slot) = frame {
            if tag_is_redundant(name, &m.stack) {
                self.frame.note_ignored();
                if self_closing {
                    self_close_with_no_element_of_its_own(m, span_start);
                }
                return;
            }
            // this frame element is being INSERTED, so the window an implied `<body>` can open in either
            // begins (`<head>`) or ends (`<body>`, real or the one synthesized just above)
            self.frame.note_inserted(slot);
        }
        // The navigation collector sees the element's ancestors, after the implied closes.
        if let Some(nav) = m.nav.as_mut() {
            if self_closing {
                nav.self_closed(name, &m.stack, span_start, open_end, m.input.bytes());
            }
            if nav.wants(name) {
                nav.start_tag(name, raw_attrs, &m.stack, span_start, m.input.encoding());
            }
        }
        m.open_element_inline(name, scid, raw_attrs, span_start);
        if self_closing || void {
            m.close_top(open_end); // no children; raw source = input[start..open_end]
        }
    }

    /// Open whatever part of the document frame this token needs and the page did not write.
    ///
    /// `<html>`, `<head>` and `<body>` all have optional start AND end tags, so a conformant document
    /// may contain none of them — and libxml2 builds the frame regardless, so this does too. Without it
    /// there is nothing to anchor `body h1` on, no shared parent to make `h1 + p` siblings, and nowhere
    /// to put root-level text.
    ///
    /// Which part a given start tag opens is [`frame_content_of`], derived from the oracle over the whole
    /// element universe. The frame tags themselves open no PART — but they still need the element they
    /// belong in: a page whose first tag is `<head>` or `<body>` writes no `<html>`, and libxml2 (and
    /// html5lib, and every browser) still wraps it in one. Leaving that out puts the `<head>` at the root
    /// with no parent and builds a SECOND, later `<html>` around whatever follows `</head>`, so
    /// `html > head`, `html > body` and `head + script` all come back empty against lxml while the values
    /// underneath them look right. Real pages do this — a crawled page can open with a bare `<head>`.
    fn ensure_frame<'a>(&mut self, m: &mut Matcher<'a>, name: &[u8], frame: Option<usize>, at: usize) {
        // The `<html>` comes FIRST, before the body check: content after `</html>` still needs an element
        // to sit in, and libxml2 gives it one, a SECOND ROOT `<html>`, the same shape it builds for a
        // second `<html>` start tag. If ordered after the `body_established` return instead, the tail
        // would be parentless and `//html/script` would miss a trailing script lxml finds. The values
        // would still be there; only the frame around them would be missing.
        if nothing_open(&m.stack) && !name.eq_ignore_ascii_case(b"html") {
            self.insert(m, b"html", &[], false, at, at);
        }
        if self.frame.body_established() {
            return; // past the body no PART of the frame is synthesized any more
        }
        if frame.is_some() {
            return; // a frame tag builds its own part, now that it has an `<html>` to sit in
        }
        // A head that is already OPEN takes everything it accepts; what it does not accept closes it
        // through the start-close relation, and the body is opened on that pop instead (see `insert`)
        // — which is why this is not the same question as `frame_content`.
        if head_is_open(&m.stack) {
            return;
        }
        // Inside a frameset there is no head for head content to go in, so libxml2 opens a BODY for it —
        // the same one it opens for ordinary content there. Only the six `Head` names are affected; the
        // rest already matched.
        let part = match frame_content_of(name) {
            FrameContent::Head if frameset_is_open(&m.stack) => FrameContent::Body,
            other => other,
        };
        match part {
            // ...and once a `</head>` has been seen, libxml2 does NOT reopen the head for a later
            // head-only tag: it leaves it at `<html>` level. (html5lib puts it back in the head; the
            // tree oracle here is libxml2. See docs/COMPATIBILITY.md.)
            FrameContent::Head if !self.frame.head_seen() => self.insert(m, b"head", &[], false, at, at),
            FrameContent::Body => self.insert(m, b"body", &[], false, at, at),
            FrameContent::Head | FrameContent::Frameset => {}
        }
    }

    /// Where a text run starts the body, if it does: the offset of its first content byte.
    ///
    /// Two shapes, one answer. Either a `<head>` is open and this is the character data that ends it
    /// (the run splits there — the leading whitespace is still the head's), or no body exists yet and
    /// this is the first content in the document, which opens `<html>`/`<body>` around it.
    ///
    /// Whitespace ALONE is not content in either shape: it leaves an open head open, and before the
    /// frame exists libxml2 drops it entirely (`   <div>` has no text node before the div).
    /// Text inside a head element (`<title>T</title>`) belongs to that element, hence "head is the
    /// CURRENT open element" rather than "a head is open somewhere".
    fn body_text_split(&self, m: &Matcher<'_>, text: &[u8]) -> Option<usize> {
        if self.frame.body_established() {
            return None; // the common path, once a body exists: one bool test and out
        }
        // outside a head, only text that is not yet inside ANY element can start the frame
        if !head_is_current(&m.stack) && !only_html_open(&m.stack) {
            return None;
        }
        first_content_byte(text)
    }

    /// Where character data ends an open `<head>` that CANNOT start a body, if it does.
    ///
    /// The same rule as above minus its second half: character data always ends an open head, but once a
    /// body exists there is no body to move it into, so libxml2 pops the head and leaves the text at
    /// `<html>` level. Only a `<head>` written after `</body>` reaches here, which is why this is its own
    /// function: if folded into the half above and gated on `body_established()`, the head would keep the
    /// text.
    fn late_head_text_split(&self, m: &Matcher<'_>, text: &[u8]) -> Option<usize> {
        if !self.frame.body_established() || !head_is_current(&m.stack) {
            return None;
        }
        first_content_byte(text)
    }

    /// A text run where the document frame may still be decided: it can start the body or end the head.
    #[inline(never)]
    fn frame_text<'a>(&mut self, m: &mut Matcher<'a>, text: &'a [u8], mode: DataMode, start: usize) {
        // Character data can START THE BODY, and it does so MID-RUN: the leading whitespace stays where
        // it was (the head's, or dropped if no frame exists yet) and the first non-space character opens
        // the body. Both oracles split it exactly there. The tokenizer hands us one run, so split it
        // here and let each half take the ordinary path.
        if let Some(k) = self.body_text_split(m, text) {
            if k > 0 {
                m.buffer_text(&text[..k], mode, start);
            }
            // `<body>` closes an open `<head>` through the start-close relation, so opening one here
            // pops the head as well — no separate unwind — and `ensure_frame` supplies the `<html>`
            // above it when the document never wrote one.
            self.ensure_frame(m, b"body", None, start + k);
            self.insert(m, b"body", &[], false, start + k, start + k);
            m.buffer_text(&text[k..], mode, start + k);
            return;
        }
        // ...and with a body already established the head still ends here, it just has nowhere to move
        // the text to. Popped directly rather than through `end_tag`, which would spend a `</head>`
        // phantom left by an ignored duplicate instead of closing the real element.
        if let Some(k) = self.late_head_text_split(m, text) {
            if k > 0 {
                m.buffer_text(&text[..k], mode, start);
            }
            m.flush_text();
            m.close_top(start + k);
            m.buffer_text(&text[k..], mode, start + k);
            return;
        }
        // Character data after `</html>` needs the same second root a START TAG gets there, or it is
        // lost outright: with the stack empty `emit_text` has nothing to attach it to and returns. The
        // start-tag path builds that root in `ensure_frame`; the text path reaches neither split above
        // once a body exists, so it has to ask for itself.
        if nothing_open(&m.stack) && self.frame.body_established() && first_content_byte(text).is_some() {
            self.insert(m, b"html", &[], false, start, start);
        }
        m.buffer_text(text, mode, start);
    }
}

impl<'a> TreeBuilder<'a> for Libxml2 {
    /// Content is read in the mode libxml2 gives the NAME, and `/>` cancels it: a self-closed
    /// `<title/>` holds no character data, because libxml2 has already closed it.
    fn start_tag(
        &mut self,
        m: &mut Matcher<'a>,
        name: &'a [u8],
        raw_attrs: &[(&'a [u8], Option<&'a [u8]>)],
        self_closing: bool,
        span_start: usize,
        open_end: usize,
    ) -> DataMode {
        self.insert(m, name, raw_attrs, self_closing, span_start, open_end);
        if self_closing {
            DataMode::Normal
        } else {
            data_mode_of(name)
        }
    }

    fn end_tag(&mut self, m: &mut Matcher<'a>, name: &'a [u8], close_start: usize, close_end: usize) {
        if let Some(nav) = m.nav.as_mut() {
            nav.end_tag(name, &m.stack);
        }
        // A frame end tag matching an earlier IGNORED frame start tag pops that phantom, not the
        // document: `<div><body>x</body>tail</div>` keeps `xtail` inside the div (verified against
        // libxml2, which keeps a stack entry for the start tag it merged away). Checked before the
        // stack, because a real `<body>` IS open in that shape and would otherwise close.
        if self.frame.absorbs_end_tag(name) {
            m.absorb_into_text_gap(close_start, close_end);
            return;
        }
        // Is this tag DISCARDED rather than honored? Either it matches no open element, or something
        // still open above the match OUT-RANKS it — libxml2 refuses to unwind that (END-TAG SCOPE), so
        // `<td>A</div>B` keeps `AB` in the cell. Both cases leave the stack alone and extend the
        // buffered text node's gap.
        let matched = m.stack.iter().rposition(|e| e.tag.eq_ignore_ascii_case(name));
        let discarded = match matched {
            None => true,
            Some(k) => {
                m.stack[k + 1..].iter().any(|e| blocks_end_tag(e.tag, name)) && end_tag_discardable(name)
            }
        };
        if discarded {
            // Absorb the tag into the pending node's gap so a following run still joins across it —
            // consecutive drops (`<div>A</p></p>B</div>`) chain, since each abuts the previous gap end.
            m.absorb_into_text_gap(close_start, close_end);
            return;
        }
        let k = matched.expect("not discarded => matched");
        // Buffered text belongs inside the element about to close, so deliver it first.
        m.flush_text();
        // elements ABOVE the match are implicitly closed — their raw source ends at the end
        // tag's `<` (close_start); the matching element itself ends after its `>` (close_end).
        while m.stack.len() > k + 1 {
            m.close_top(close_start);
        }
        m.close_top(close_end);
    }

    /// libxml2 drops an end tag with no open element to match (`<div>A</p>B</div>`) and the character
    /// data either side of it becomes ONE text node (`AB`); [`Matcher::buffer_text`] re-joins the runs.
    /// What this adds is the frame: character data can start the body or end the head.
    fn text(&mut self, m: &mut Matcher<'a>, text: &'a [u8], mode: DataMode, start: usize) {
        // Past the frame, a run in any element but a head is that element's text, which is every run
        // of a page once its body has started.
        if self.frame.body_established() && matches!(m.stack.last(), Some(e) if !e.tag.eq_ignore_ascii_case(b"head")) {
            return m.buffer_text(text, mode, start);
        }
        self.frame_text(m, text, mode, start)
    }

    fn cdata_is_text(&self, _m: &Matcher<'a>) -> bool {
        false // a bogus comment everywhere, foreign content included
    }
}

/// Would this start tag close the CURRENT open element? A read-only peek, so the caller can decide
/// whether an ignored tag is going to reshape the stack before committing the pending text node.
fn closes_top(m: &Matcher<'_>, name: &[u8], scid: u8) -> bool {
    match m.stack.last() {
        None => false,
        Some(top) => {
            let closes = start_closes(scid, top.scid);
            crate::mutate::closes(name, top.tag, closes)
        }
    }
}

/// `<html/>`, `<head/>` and `<body/>` written where the frame already exists: the tag INSERTS
/// nothing, but the `/>` still fires a close — and it lands on the enclosing element.
///
/// libxml2's `endElement` pops the CURRENT node by position, not by name. A redundant frame tag
/// pushes no element of its own, so the pop takes whatever the tag was written inside: one level,
/// after any implied closes this tag already ran. `<div><b>x<html/>y` is `<div><b>x</b>y</div>`,
/// and a second `<html/>` then takes the `<div>`. The phantom is NOT consumed — a later `</body>`
/// is still absorbed by it — which is what separates this from `<html></html>`, where the written
/// end tag pops the phantom and the enclosing element survives.
///
/// For a stray `<strong>` before the doctype followed by `<html xmlns=… />`, libxml2 ends the
/// `<strong>` at the self-close. Leaving it open would parent the whole body inside it, making every
/// `body > *` empty while leaving `body *` intact. The frame would be wrong, not the values.
fn self_close_with_no_element_of_its_own(m: &mut Matcher<'_>, span_start: usize) {
    m.flush_text();
    if !m.stack.is_empty() {
        m.close_top(span_start);
    }
}
