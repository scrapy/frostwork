//! The document frame — `<html>`/`<head>`/`<body>` state and the questions the rules ask about it.
//!
//! **Why this is a module.** If frame state were spread across call sites as ad-hoc combinations of bare
//! booleans, counters and inline stack scans, rules could ask PROXY questions instead of the real ones.
//! These are the distinctions that must remain explicit:
//!
//! | rule | asked | needed |
//! |---|---|---|
//! | `ensure_frame` | "is this tag a frame tag" | "is anything open to hold what follows" |
//! | `<body>` redundancy | "is anything else open" | "is a `<body>` open" |
//! | head-ending text | `body_established` | that, *and* separately "is a head the current top" |
//! | phantoms | a count per NAME | one count: libxml2 keeps a stack SLOT, not a named token |
//!
//! Every question is named here, once, so a rule that reaches for the wrong one is visible in the diff
//! rather than buried in a `stack.iter().any(...)` at the call site.
//!
//! **Why not an insertion-mode enum**, which is the obvious shape: several questions are not phases, and
//! a linear phase collapses exactly the distinctions the engine has to preserve.
//! [`head_is_current`] ("the head is the CURRENT top, so this text is the head's own") is not
//! [`head_is_open`] ("a head is open somewhere") and neither is [`DocumentFrame::head_seen`] ("a head has
//! existed, so a later head-only tag is not put back in one") — `<head><title>` is all three of open,
//! seen and NOT current. And [`body_is_open`] ("a `<body>` is on the
//! stack right now, which `</body>` ends and a written `<body>` restarts anywhere, even inside a `<td>`")
//! is not [`DocumentFrame::body_established`] ("a body has existed, so nothing synthesizes a frame part
//! any more"). HTML5 can use one mode variable because it also carries a stack of open elements and an
//! explicit `head element pointer`; this engine answers the same questions off its own stack.

use super::OpenElem;

/// The three frame slots. `html` is 0 so [`only_html_open`] can name it.
pub(super) const HTML_FRAME_SLOT: usize = 0;
pub(super) const HEAD_FRAME_SLOT: usize = 1;
pub(super) const BODY_FRAME_SLOT: usize = 2;

/// `<html>`/`<head>`/`<body>` — the three tags libxml2 accepts only as the document frame — as a slot
/// index. `None` for every other name (the hot path: one length compare).
pub(super) fn frame_slot(name: &[u8]) -> Option<usize> {
    if name.len() != 4 {
        return None;
    }
    if name.eq_ignore_ascii_case(b"html") {
        Some(HTML_FRAME_SLOT)
    } else if name.eq_ignore_ascii_case(b"head") {
        Some(HEAD_FRAME_SLOT)
    } else if name.eq_ignore_ascii_case(b"body") {
        Some(BODY_FRAME_SLOT)
    } else {
        None
    }
}

/// Is nothing open at all — so whatever comes next needs an `<html>` built around it? True again after
/// `</html>`, because the engine deliberately keeps parsing the tail (see docs/COMPATIBILITY.md).
pub(super) fn nothing_open(stack: &[OpenElem<'_>]) -> bool {
    stack.is_empty()
}

/// Is a `<head>` the CURRENT open element — so character data here is the head's own and ends it?
///
/// Deliberately named apart from [`head_is_open`], because the two answer different questions and the
/// text rules need this one: inside `<head><title>T</title>` a head IS open, but the text belongs to the
/// title, so an ancestor search would move it.
pub(super) fn head_is_current(stack: &[OpenElem<'_>]) -> bool {
    matches!(stack.last(), Some(e) if e.tag.eq_ignore_ascii_case(b"head"))
}

/// Is a `<head>` open ANYWHERE on the stack — so the frame has a head to put head content in?
pub(super) fn head_is_open(stack: &[OpenElem<'_>]) -> bool {
    stack.iter().any(|e| e.tag.eq_ignore_ascii_case(b"head"))
}

/// Is a `<body>` on the stack right now? Distinct from [`DocumentFrame::body_established`], and the
/// distinction is load-bearing: a `</body>` ends this while leaving that set, and libxml2 then starts a
/// second body wherever the next `<body>` is written.
pub(super) fn body_is_open(stack: &[OpenElem<'_>]) -> bool {
    stack.iter().any(|e| e.tag.eq_ignore_ascii_case(b"body"))
}

/// Is a `<frameset>` open? A frameset document has no `<head>` to put head content in, so libxml2 opens
/// a `<body>` for it there — the same one it opens for ordinary content. Derived over the whole element
/// universe: exactly the six `FrameContent::Head` names (`base link meta script style title`) change
/// answer inside a frameset, and every other name already agreed.
pub(super) fn frameset_is_open(stack: &[OpenElem<'_>]) -> bool {
    stack.iter().any(|e| e.tag.eq_ignore_ascii_case(b"frameset"))
}

/// Is nothing but `<html>` open — i.e. is character data here still outside every real element?
pub(super) fn only_html_open(stack: &[OpenElem<'_>]) -> bool {
    !stack.iter().any(|e| frame_slot(e.tag) != Some(HTML_FRAME_SLOT))
}

/// Is a `<html>`/`<head>`/`<body>` start tag REDUNDANT here — i.e. is the frame already established, so
/// libxml2 would ignore the tag rather than insert an element?
///
/// The three names do NOT share one rule. "Accepted while nothing but an `<html>` is open" holds for
/// `<head>` only, not for the other two; each arm says which oracle cell it stands on, and
/// `frame-in-element` in `tools/audit_tree_rules.py` sweeps all three over the whole
/// element universe crossed with "is a body open". A frame the page OMITS is a different question,
/// answered by `Matcher::ensure_frame`.
pub(super) fn tag_is_redundant(name: &[u8], stack: &[OpenElem<'_>]) -> bool {
    if name.eq_ignore_ascii_case(b"html") {
        // A second <html> is redundant however shallow the stack is. NOT once the first has closed,
        // though: libxml2 builds a SECOND ROOT `<html>` element there (verified on a crawled page that
        // self-closes `<html/>` inside a downlevel-revealed conditional comment — its tree has two roots,
        // both carrying the attributes), and libxml2 is the tree oracle. Browsers keep one element, and so
        // parsel's own CSS and XPath disagree about such a document: `.css('html')` is scoped to the first
        // root and sees one, `//html` sees both. Matching the TREE is what keeps `//html/@x` right; the CSS
        // side of that page is a parsel scoping artifact.
        !nothing_open(stack)
    } else if name.eq_ignore_ascii_case(b"body") {
        // A written `<body>` is redundant only while one is OPEN — NOT merely because something else is.
        // Once a `</body>` has closed it, libxml2 starts a second body wherever the next `<body>` is
        // written, whatever it is written inside: a `<td>`, a `<div>`, or a `<frameset>` (whose document
        // never had one — that is how a frameset page writes its no-frames fallback). Reading it as
        // "anything else open" dropped those, and on a crawled page the `<body>` after a `</body>` was
        // ignored so a whole trailing table stayed nested inside an earlier cell.
        body_is_open(stack)
    } else {
        // `<head>` belongs to the phase BEFORE any body content, so anything else being open means that
        // phase is over. (Its own close does not end it: `<head></head><head id=2>` inserts a second head
        // in libxml2, and `<div>` after that one does not.)
        !only_html_open(stack)
    }
}

/// What the frame rules remember that the stack cannot tell them.
///
/// Both flags are HISTORY, and that is exactly why neither can be folded into a stack predicate or into
/// one phase: they stay set after the element they describe has closed.
#[derive(Default)]
pub(super) struct DocumentFrame {
    head_seen: bool,
    body_established: bool,
    /// Ignored frame START tags waiting to absorb a frame END tag. libxml2 merges such a tag away but
    /// keeps a stack SLOT for it, so the next frame end tag pops that phantom instead of closing the
    /// document: `<div><body>x</body>tail</div>` keeps `xtail` inside the div. ONE counter, not one per
    /// name — the slot is not a named token, so any of the three end tags pops one left by any of the
    /// three, and counting per name matched only 2 of the 6 combinations.
    phantoms: u32,
}

impl DocumentFrame {
    /// Has a `<head>` been inserted? Once it has, libxml2 does not put a later head-only tag back in a
    /// head — it leaves it at `<html>` level. (html5lib disagrees; libxml2 is the oracle here.)
    pub(super) fn head_seen(&self) -> bool {
        self.head_seen
    }

    /// Has a `<body>` been inserted? Once one has, no part of the frame is synthesized any more, and
    /// character data can no longer open one.
    pub(super) fn body_established(&self) -> bool {
        self.body_established
    }

    /// Record that a frame element is being INSERTED (not ignored).
    pub(super) fn note_inserted(&mut self, slot: usize) {
        match slot {
            HEAD_FRAME_SLOT => self.head_seen = true,
            BODY_FRAME_SLOT => self.body_established = true,
            _ => {}
        }
    }

    /// Record that a frame start tag was IGNORED, leaving a phantom for a later frame end tag.
    pub(super) fn note_ignored(&mut self) {
        self.phantoms += 1;
    }

    /// Does this end tag pop a phantom rather than the document? Consumes one if so.
    pub(super) fn absorbs_end_tag(&mut self, name: &[u8]) -> bool {
        if frame_slot(name).is_some() && self.phantoms > 0 {
            self.phantoms -= 1;
            return true;
        }
        false
    }
}
