//! The browser tree builder: browser mode's tree construction, graded against real browsers
//! (docs/BROWSER_MODE.md, section 3).
//!
//! At this stage it is libxml2's tree rules ([`Libxml2`]) plus the browser behaviours that need no
//! HTML5 insertion modes. Each was measured in Chrome 156 (tools/browser_oracle, claims mode):
//!
//! * `<noscript>` content is RAW TEXT, because a browser parses with scripting on, and `<noscript>` is
//!   head content as HTML5 makes it with scripting on;
//! * an HTML `<template>`'s content is INERT: the element matches, while nothing written inside it is an
//!   element, a text node, a child or a sibling of anything in the document;
//! * `/>` on a non-void HTML element is ignored, so `<title/>`, `<script/>`, `<textarea/>`, `<style/>`
//!   (and every other raw-text element) still read their text, and `<template/>` still has content.
//!   `<div/>` keeps libxml2's shape until the HTML5 builder, which is where that rule belongs;
//! * the input-level rules (no strip, NUL kept for HTML5's value rules) live in `crate::prepare_bytes`
//!   and the value decoders, and need nothing from a tree builder.
//!
//! The HTML5 builder replaces this module's internals, keeping its interface.

use super::libxml2::Libxml2;
use super::tree::TreeBuilder;
use super::{Matcher, OpenElem};
use crate::implied_close::{data_mode_of, frame_content_of, FrameContent};
use crate::tokenizer::DataMode;

pub(super) struct Browser {
    lxml: Libxml2,
    /// How many HTML `<template>` elements deep the tokenizer is INSIDE template content; 0 in the
    /// document. Content pushes nothing on the matcher stack, so when this returns to 0 the template
    /// that opened it is the current element again.
    template_content: u32,
}

impl Default for Browser {
    fn default() -> Self {
        Browser { lxml: Libxml2::with_frame_part(frame_part), template_content: 0 }
    }
}

/// Which part of an omitted frame a start tag opens: libxml2's answer, except for the two HTML5 head
/// elements browser mode models. Chrome puts a first-tag `<noscript>` or `<template>` in the `<head>`,
/// where libxml2 opens a body for it.
fn frame_part(name: &[u8]) -> FrameContent {
    if name.eq_ignore_ascii_case(b"noscript") || name.eq_ignore_ascii_case(b"template") {
        FrameContent::Head
    } else {
        frame_content_of(name)
    }
}

/// The names that make an SVG or MathML subtree's content HTML again: SVG's HTML integration points and
/// MathML's text integration points, lowercase. `annotation-xml` is one only with an HTML `encoding`,
/// which an open element does not keep, so it stays foreign here.
const INTEGRATION_POINTS: [&[u8]; 8] =
    [b"foreignobject", b"desc", b"title", b"mi", b"mo", b"mn", b"ms", b"mtext"];

/// Would an element opened here be SVG or MathML rather than HTML?
///
/// Read off the stack: the nearest `<svg>`/`<math>` makes it foreign, unless an integration point is
/// nearer. That is HTML5's namespace rule except for the start tags that break out of foreign content
/// (`<svg><p>`), which libxml2 leaves nested, so they read as foreign here. Foreign is the side that
/// keeps lxml mode's answer, so the approximation never produces a value lxml mode would not; the HTML5
/// builder replaces it with the namespace it tracks.
fn in_foreign_content(stack: &[OpenElem<'_>]) -> bool {
    for e in stack.iter().rev() {
        if e.tag.eq_ignore_ascii_case(b"svg") || e.tag.eq_ignore_ascii_case(b"math") {
            return true;
        }
        if INTEGRATION_POINTS.iter().any(|n| e.tag.eq_ignore_ascii_case(n)) {
            return false;
        }
    }
    false
}

/// The mode an element's content is read in inside template content, where nothing is on the stack to
/// tell an HTML element from a foreign one. libxml2's rule (`/>` cancels the mode) plus raw-text
/// `<noscript>`: inert content only has to find its `</template>`, and this is the rule lxml mode
/// already finds it with.
fn content_mode(name: &[u8], self_closing: bool) -> DataMode {
    if self_closing {
        DataMode::Normal
    } else if name.eq_ignore_ascii_case(b"noscript") {
        DataMode::Rawtext
    } else {
        data_mode_of(name)
    }
}

impl<'a> TreeBuilder<'a> for Browser {
    fn start_tag(
        &mut self,
        m: &mut Matcher<'a>,
        name: &'a [u8],
        raw_attrs: &[(&'a [u8], Option<&'a [u8]>)],
        self_closing: bool,
        span_start: usize,
        open_end: usize,
    ) -> DataMode {
        if self.template_content > 0 {
            // Template content: tokenized, so that its raw-text elements cannot desynchronize the
            // document, and otherwise invisible. A nested template only deepens the content.
            if name.eq_ignore_ascii_case(b"template") {
                self.template_content += 1;
            }
            return content_mode(name, self_closing);
        }
        let template = name.eq_ignore_ascii_case(b"template");
        let noscript = name.eq_ignore_ascii_case(b"noscript");
        let named_mode = data_mode_of(name);
        // The namespace decides all three rules below, and it is only needed for these tags.
        let foreign = (template || noscript || (self_closing && named_mode != DataMode::Normal))
            && in_foreign_content(&m.stack);
        let mode = if noscript && !foreign { DataMode::Rawtext } else { named_mode };
        // HTML5 ignores `/>` on a non-void HTML element and honours it on a foreign one.
        let honour_self_closing =
            self_closing && (foreign || !(template || mode != DataMode::Normal));
        self.lxml.insert(m, name, raw_attrs, honour_self_closing, span_start, open_end);
        if template && !foreign {
            self.template_content = 1;
        }
        if honour_self_closing {
            DataMode::Normal
        } else {
            mode
        }
    }

    fn end_tag(&mut self, m: &mut Matcher<'a>, name: &'a [u8], close_start: usize, close_end: usize) {
        if self.template_content == 0 {
            return self.lxml.end_tag(m, name, close_start, close_end);
        }
        // Inside template content no end tag reaches the document: the template is a boundary, so
        // `</div>` cannot close a `<div>` outside it (measured). Only `</template>` matters.
        if !name.eq_ignore_ascii_case(b"template") {
            return;
        }
        self.template_content -= 1;
        if self.template_content == 0 {
            debug_assert!(m.stack.last().is_some_and(|e| e.tag.eq_ignore_ascii_case(b"template")));
            m.close_top(close_end);
        }
    }

    fn text(&mut self, m: &mut Matcher<'a>, text: &'a [u8], mode: DataMode, start: usize) {
        // Template content has no text nodes the document can see, and the template no `::text`.
        if self.template_content == 0 {
            self.lxml.text(m, text, mode, start);
        }
    }

    fn cdata_is_text(&self, _m: &Matcher<'a>) -> bool {
        // Text in foreign content, which the HTML5 builder tracks; until then, libxml2's answer.
        false
    }
}
