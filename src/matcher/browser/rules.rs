//! The insertion modes (WHATWG 13.2.6.4) and the rules for foreign content (13.2.6.5), as Chrome 156
//! runs them: the `<select>` rules are the in-body ones of customisable select, and the scripting flag
//! is on. Each function is one section of the spec; "process using the rules for X" is a call, and
//! "reprocess" a call after switching modes.

use super::tags::{breaks_out, font_breaks_out, lower, Tag, MAX_NAME};
use super::lists::Bound;
use super::{
    ends_frameset_ok, has_content, ws_at, ws_prefix, Afe, Browser, EndTok, Loc, Mode, Node, Ns, StartTok, Vis, CENSUS_MERGE_ANY, HAS_CHILD,
    RETRO_ADOPTION, RETRO_FRAMESET, RETRO_MERGE, RETRO_SELECTEDCONTENT, SHADOW_HOST, SYNTH,
};
use crate::implied_close::SPECIAL;
use crate::matcher::{Matcher, SourcePrefix};
use crate::tokenizer::DataMode;

const HEADINGS: [Tag; 6] = [Tag::H1, Tag::H2, Tag::H3, Tag::H4, Tag::H5, Tag::H6];
const TABLE_SECTIONS: [Tag; 3] = [Tag::Tbody, Tag::Tfoot, Tag::Thead];
const CELLS: [Tag; 2] = [Tag::Td, Tag::Th];

/// The names `attachShadow()` accepts as a host, besides a custom element.
fn valid_shadow_host(name: &[u8]) -> bool {
    let mut buf = [0; MAX_NAME];
    let Some(low) = lower(name, &mut buf) else { return custom_element_name(name) };
    matches!(
        low,
        b"article" | b"aside" | b"blockquote" | b"body" | b"div" | b"footer" | b"h1" | b"h2" | b"h3" | b"h4"
            | b"h5" | b"h6" | b"header" | b"main" | b"nav" | b"p" | b"section" | b"span"
    ) || custom_element_name(low)
}

/// A valid custom element name, as far as a parsed tag name can be one: a lowercase ASCII letter, a
/// hyphen somewhere, and none of the reserved SVG and MathML names.
fn custom_element_name(name: &[u8]) -> bool {
    name.first().is_some_and(u8::is_ascii_lowercase)
        && name.contains(&b'-')
        && !name.iter().any(u8::is_ascii_uppercase)
        && !matches!(
            name,
            b"annotation-xml" | b"color-profile" | b"font-face" | b"font-face-src" | b"font-face-uri"
                | b"font-face-format" | b"font-face-name" | b"missing-glyph"
        )
}

fn attr<'x>(attrs: &[(&'x [u8], Option<&'x [u8]>)], name: &[u8]) -> Option<&'x [u8]> {
    attrs.iter().find(|(n, _)| n.eq_ignore_ascii_case(name)).map(|(_, v)| v.unwrap_or(b""))
}

impl<'a, const REPLAY: bool> Browser<'a, REPLAY> {
    // ================================================================== start tags, HTML content

    pub(super) fn html_start(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        match self.mode {
            Mode::Initial => {
                self.no_doctype();
                self.html_start(m, t)
            }
            Mode::BeforeHtml => {
                if t.tag == Tag::Html {
                    self.insert_html(m, t);
                    self.mode = Mode::BeforeHead;
                } else {
                    self.imply_html(m, t.start);
                    self.html_start(m, t)
                }
            }
            Mode::BeforeHead => match t.tag {
                Tag::Html => self.in_body_start(m, t),
                Tag::Head => {
                    self.insert_html(m, t);
                    self.head = self.stack.last().copied();
                    self.mode = Mode::InHead;
                }
                _ => {
                    self.imply_head(m, t.start);
                    self.html_start(m, t)
                }
            },
            Mode::InHead => self.in_head_start(m, t),
            Mode::AfterHead => match t.tag {
                Tag::Html => self.in_body_start(m, t),
                Tag::Body => {
                    self.finish_head(m, t.start);
                    self.insert_html(m, t);
                    self.frameset_ok = false;
                    self.mode = Mode::InBody;
                }
                Tag::Frameset => {
                    self.finish_head(m, t.start);
                    self.insert_html(m, t);
                    self.mode = Mode::InFrameset;
                }
                Tag::Base | Tag::Basefont | Tag::Bgsound | Tag::Link | Tag::Meta | Tag::Noframes | Tag::Script
                | Tag::Style | Tag::Template | Tag::Title => {
                    let Some(head) = self.head else { return };
                    if !REPLAY && self.path.len() == 1 && self.html_text.is_none() {
                        self.html_text = m.take_text();
                    }
                    self.stack.push(head);
                    self.in_head_start(m, t);
                    if let Some(k) = self.stack.index_of(head.uid) {
                        self.stack.remove(k);
                    }
                }
                Tag::Head => {}
                _ => {
                    self.imply_body(m, t.start);
                    self.html_start(m, t)
                }
            },
            Mode::InBody => self.in_body_start(m, t),
            // The tokenizer reads a text element's content as one run and ends it with the end tag.
            Mode::Text => {}
            Mode::InTable => self.in_table_start(m, t),
            Mode::InCaption => match t.tag {
                Tag::Caption | Tag::Col | Tag::Colgroup | Tag::Tbody | Tag::Td | Tag::Tfoot | Tag::Th
                | Tag::Thead | Tag::Tr => {
                    if self.end_caption(m, t.start, t.start) {
                        self.html_start(m, t)
                    }
                }
                _ => self.in_body_start(m, t),
            },
            Mode::InColumnGroup => match t.tag {
                Tag::Html => self.in_body_start(m, t),
                Tag::Col => self.insert_void(m, t),
                Tag::Template => self.in_head_start(m, t),
                _ => {
                    if self.cur_is(Tag::Colgroup) {
                        self.pop(m, t.start);
                        self.mode = Mode::InTable;
                        self.html_start(m, t)
                    }
                }
            },
            Mode::InTableBody => match t.tag {
                Tag::Tr => {
                    self.clear_to(m, &TABLE_SECTIONS, t.start);
                    self.insert_html(m, t);
                    self.mode = Mode::InRow;
                }
                Tag::Th | Tag::Td => {
                    self.clear_to(m, &TABLE_SECTIONS, t.start);
                    self.insert_synth(m, b"tr", Tag::Tr, t.start);
                    self.mode = Mode::InRow;
                    self.html_start(m, t)
                }
                Tag::Caption | Tag::Col | Tag::Colgroup | Tag::Tbody | Tag::Tfoot | Tag::Thead => {
                    if self.end_table_body(m, t.start) {
                        self.html_start(m, t)
                    }
                }
                _ => self.in_table_start(m, t),
            },
            Mode::InRow => match t.tag {
                Tag::Th | Tag::Td => {
                    self.clear_to(m, &[Tag::Tr], t.start);
                    self.insert_html(m, t);
                    self.mode = Mode::InCell;
                    self.afe.push(Afe::Marker);
                }
                Tag::Caption | Tag::Col | Tag::Colgroup | Tag::Tbody | Tag::Tfoot | Tag::Thead | Tag::Tr => {
                    if self.end_row(m, t.start, t.start) {
                        self.html_start(m, t)
                    }
                }
                _ => self.in_table_start(m, t),
            },
            Mode::InCell => match t.tag {
                Tag::Caption | Tag::Col | Tag::Colgroup | Tag::Tbody | Tag::Td | Tag::Tfoot | Tag::Th
                | Tag::Thead | Tag::Tr => {
                    if self.in_table_scope(Tag::Td).is_some() || self.in_table_scope(Tag::Th).is_some() {
                        self.close_cell(m, &CELLS, t.start, t.start);
                        self.html_start(m, t)
                    }
                }
                _ => self.in_body_start(m, t),
            },
            Mode::InTemplate => {
                let next = match t.tag {
                    Tag::Base | Tag::Basefont | Tag::Bgsound | Tag::Link | Tag::Meta | Tag::Noframes
                    | Tag::Script | Tag::Style | Tag::Template | Tag::Title => return self.in_head_start(m, t),
                    Tag::Caption | Tag::Colgroup | Tag::Tbody | Tag::Tfoot | Tag::Thead => Mode::InTable,
                    Tag::Col => Mode::InColumnGroup,
                    Tag::Tr => Mode::InTableBody,
                    Tag::Td | Tag::Th => Mode::InRow,
                    _ => Mode::InBody,
                };
                self.templates.pop();
                self.templates.push(next);
                self.mode = next;
                self.html_start(m, t)
            }
            Mode::AfterBody | Mode::AfterAfterBody => {
                if t.tag == Tag::Html {
                    self.in_body_start(m, t)
                } else {
                    self.mode = Mode::InBody;
                    self.html_start(m, t)
                }
            }
            Mode::InFrameset => match t.tag {
                Tag::Html => self.in_body_start(m, t),
                Tag::Frameset => self.insert_html(m, t),
                Tag::Frame => self.insert_void(m, t),
                Tag::Noframes => self.in_head_start(m, t),
                _ => {}
            },
            Mode::AfterFrameset | Mode::AfterAfterFrameset => match t.tag {
                Tag::Html => self.in_body_start(m, t),
                Tag::Noframes => self.in_head_start(m, t),
                _ => {}
            },
        }
    }

    /// The text insertion mode, entered for an element whose content the tokenizer reads as text.
    fn enter_text(&mut self) {
        self.original = self.mode;
        self.mode = Mode::Text;
    }

    fn in_head_start(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        match t.tag {
            Tag::Html => self.in_body_start(m, t),
            Tag::Base | Tag::Basefont | Tag::Bgsound | Tag::Link | Tag::Meta => self.insert_void(m, t),
            Tag::Title | Tag::Noscript | Tag::Noframes | Tag::Style | Tag::Script => {
                self.insert_html(m, t);
                self.enter_text();
            }
            Tag::Template => self.template_start(m, t),
            Tag::Head => {}
            _ => {
                self.leave_head();
                self.html_start(m, t)
            }
        }
    }

    /// A `<template>` start tag, in the in-head rules. One that declares a shadow root on a host that
    /// can take one is not inserted at all: it and its content leave the document tree.
    fn template_start(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        self.afe.push(Afe::Marker);
        self.frameset_ok = false;
        self.mode = Mode::InTemplate;
        self.templates.push(Mode::InTemplate);
        let host = self.stack.len() - 1;
        let declares = attr(t.attrs, b"shadowrootmode")
            .is_some_and(|v| v.eq_ignore_ascii_case(b"open") || v.eq_ignore_ascii_case(b"closed"));
        let h = &self.stack[host];
        let attaches = declares
            && host > 0
            && h.ns == Ns::Html
            && h.flags & SHADOW_HOST == 0
            && valid_shadow_host(h.name);
        if attaches {
            self.stack[host].flags |= SHADOW_HOST;
            let mut node = self.new_node(t.name, t.low, t.tag, Ns::Html, t.attrs);
            node.vis = Vis::Inert;
            self.stack.push(node);
        } else {
            self.insert_html(m, t);
        }
    }

    /// The `</template>` end tag, in the in-head rules.
    fn template_end(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        if !self.has_template() {
            return;
        }
        self.implied_end_thorough(m, t.start);
        if let Some(k) = self.stack.topmost(Tag::Template) {
            self.pop_to(m, k, t.start, t.end);
        }
        self.afe.clear_to_marker();
        self.templates.pop();
        self.reset_mode();
    }

    fn in_body_start(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        let at = t.start;
        match t.tag {
            Tag::Html => {
                if !self.has_template() {
                    self.merge_into(m, 0, t);
                }
            }
            Tag::Base | Tag::Basefont | Tag::Bgsound | Tag::Link | Tag::Meta | Tag::Noframes | Tag::Script
            | Tag::Style | Tag::Template | Tag::Title => self.in_head_start(m, t),
            Tag::Body => {
                if self.stack.len() > 1 && self.stack[1].tag == Tag::Body && !self.has_template() {
                    self.frameset_ok = false;
                    self.merge_into(m, 1, t);
                }
            }
            Tag::Frameset => {
                if self.stack.len() > 1 && self.stack[1].tag == Tag::Body && self.frameset_ok {
                    // The body and everything in it leave the document.
                    self.retro |= RETRO_FRAMESET;
                    if REPLAY {
                        self.dom.detach(self.stack[1].depth);
                    }
                    self.pop_to(m, 1, at, at);
                    self.insert_html(m, t);
                    self.mode = Mode::InFrameset;
                }
            }
            Tag::Address | Tag::Article | Tag::Aside | Tag::Blockquote | Tag::Center | Tag::Details | Tag::Dialog
            | Tag::Dir | Tag::Div | Tag::Dl | Tag::Fieldset | Tag::Figcaption | Tag::Figure | Tag::Footer
            | Tag::Header | Tag::Hgroup | Tag::Main | Tag::Menu | Tag::Nav | Tag::Ol | Tag::P | Tag::Search
            | Tag::Section | Tag::Summary | Tag::Ul => {
                self.close_p_if_in_button_scope(m, at);
                self.insert_html(m, t);
            }
            Tag::H1 | Tag::H2 | Tag::H3 | Tag::H4 | Tag::H5 | Tag::H6 => {
                self.close_p_if_in_button_scope(m, at);
                if self.stack.last().is_some_and(|n| HEADINGS.contains(&n.tag)) {
                    self.pop(m, at);
                }
                self.insert_html(m, t);
            }
            Tag::Pre | Tag::Listing => {
                self.close_p_if_in_button_scope(m, at);
                self.insert_html(m, t);
                self.skip_lf = true;
                self.frameset_ok = false;
            }
            Tag::Form => {
                if !self.form_ignored() {
                    self.close_p_if_in_button_scope(m, at);
                    self.insert_form(m, t);
                }
            }
            Tag::Li | Tag::Dd | Tag::Dt => {
                self.frameset_ok = false;
                // the nearest one to close, unless a special element other than address, div and p is nearer
                let nearest = match t.tag {
                    Tag::Li => self.stack.topmost(Tag::Li),
                    _ => self.stack.topmost_of(&[Tag::Dd, Tag::Dt]),
                };
                if let Some(i) = self.stack.scoped(nearest, Bound::ListItemStart) {
                    let tag = self.stack[i].tag;
                    self.close_element(m, &[tag], Some(tag), at, at);
                }
                self.close_p_if_in_button_scope(m, at);
                self.insert_html(m, t);
            }
            Tag::Plaintext => {
                self.close_p_if_in_button_scope(m, at);
                self.insert_html(m, t);
            }
            Tag::Button => {
                if self.in_scope(Tag::Button).is_some() {
                    self.close_element(m, &[Tag::Button], None, at, at);
                }
                self.reconstruct(m, at);
                self.insert_html(m, t);
                self.frameset_ok = false;
            }
            Tag::A => {
                if let Some(Afe::El { uid, .. }) = self.afe.last_of(Tag::A).map(|k| self.afe[k]) {
                    self.adoption_agency(m, EndTok { name: t.name, tag: Tag::A, start: at, end: at });
                    if let Some(k) = self.afe.index_of(uid) {
                        self.afe.remove(k);
                    }
                    if let Some(k) = self.stack.index_of(uid) {
                        self.stack.remove(k);
                    }
                }
                self.reconstruct(m, at);
                self.insert_formatting(m, t);
            }
            Tag::B | Tag::Big | Tag::Code | Tag::Em | Tag::Font | Tag::I | Tag::S | Tag::Small | Tag::Strike
            | Tag::Strong | Tag::Tt | Tag::U => {
                self.reconstruct(m, at);
                self.insert_formatting(m, t);
            }
            Tag::Nobr => {
                self.reconstruct(m, at);
                if self.in_scope(Tag::Nobr).is_some() {
                    self.adoption_agency(m, EndTok { name: t.name, tag: Tag::Nobr, start: at, end: at });
                    self.reconstruct(m, at);
                }
                self.insert_formatting(m, t);
            }
            Tag::Applet | Tag::Marquee | Tag::Object => {
                self.reconstruct(m, at);
                self.insert_html(m, t);
                self.afe.push(Afe::Marker);
                self.frameset_ok = false;
            }
            Tag::Table => {
                if !self.quirks {
                    self.close_p_if_in_button_scope(m, at);
                }
                self.insert_html(m, t);
                self.frameset_ok = false;
                self.mode = Mode::InTable;
            }
            Tag::Area | Tag::Br | Tag::Embed | Tag::Img | Tag::Keygen | Tag::Wbr => {
                self.reconstruct(m, at);
                self.insert_void(m, t);
                self.frameset_ok = false;
            }
            Tag::Input => {
                if let Some(k) = self.in_scope(Tag::Select) {
                    self.pop_to(m, k, at, at);
                }
                self.reconstruct(m, at);
                self.insert_void(m, t);
                if !attr(t.attrs, b"type").is_some_and(|v| v.eq_ignore_ascii_case(b"hidden")) {
                    self.frameset_ok = false;
                }
            }
            Tag::Param | Tag::Source | Tag::Track => self.insert_void(m, t),
            Tag::Hr => {
                self.close_p_if_in_button_scope(m, at);
                if self.in_scope(Tag::Select).is_some() {
                    self.implied_end(m, None, at);
                }
                self.insert_void(m, t);
                self.frameset_ok = false;
            }
            Tag::Image => {
                let img = StartTok { name: b"img", low: Some(b"img"), tag: Tag::Img, ..*t };
                self.in_body_start(m, &img)
            }
            Tag::Textarea => {
                self.insert_html(m, t);
                self.skip_lf = true;
                self.enter_text();
                self.frameset_ok = false;
            }
            Tag::Xmp => {
                self.close_p_if_in_button_scope(m, at);
                self.reconstruct(m, at);
                self.frameset_ok = false;
                self.insert_html(m, t);
                self.enter_text();
            }
            Tag::Iframe => {
                self.frameset_ok = false;
                self.insert_html(m, t);
                self.enter_text();
            }
            Tag::Noembed | Tag::Noscript => {
                self.insert_html(m, t);
                self.enter_text();
            }
            Tag::Select => {
                if let Some(k) = self.in_scope(Tag::Select) {
                    self.pop_to(m, k, at, at);
                } else {
                    self.reconstruct(m, at);
                    self.insert_html(m, t);
                    self.frameset_ok = false;
                }
            }
            Tag::Option | Tag::Optgroup => {
                if self.in_scope(Tag::Select).is_some() {
                    let except = if t.tag == Tag::Option { Some(Tag::Optgroup) } else { None };
                    self.implied_end(m, except, at);
                } else if self.cur_is(Tag::Option) {
                    self.pop(m, at);
                }
                self.reconstruct(m, at);
                self.insert_html(m, t);
                if REPLAY && t.tag == Tag::Option && self.cur().vis == Vis::Live {
                    self.option_inserted(self.cur().depth);
                }
            }
            Tag::Selectedcontent => {
                self.reconstruct(m, at);
                self.insert_html(m, t);
                let n = *self.cur();
                if n.vis == Vis::Live {
                    self.retro |= RETRO_SELECTEDCONTENT;
                    if REPLAY {
                        self.selectedcontent_inserted(n.depth);
                    }
                }
            }
            Tag::Rb | Tag::Rtc | Tag::Rp | Tag::Rt => {
                if self.in_scope(Tag::Ruby).is_some() {
                    let except = if matches!(t.tag, Tag::Rp | Tag::Rt) { Some(Tag::Rtc) } else { None };
                    self.implied_end(m, except, at);
                }
                self.insert_html(m, t);
            }
            Tag::Math | Tag::Svg => {
                self.reconstruct(m, at);
                let ns = if t.tag == Tag::Svg { Ns::Svg } else { Ns::MathMl };
                self.insert_foreign(m, t, ns);
            }
            Tag::Caption | Tag::Col | Tag::Colgroup | Tag::Frame | Tag::Head | Tag::Tbody | Tag::Td | Tag::Tfoot
            | Tag::Th | Tag::Thead | Tag::Tr => {}
            _ => {
                self.reconstruct(m, at);
                self.insert_html(m, t);
            }
        }
    }

    /// A second `<html>` or `<body>` start tag: its attributes join the open element's. Streaming, the
    /// element was matched without them, so one a selector asks about makes the document a replay.
    fn merge_into(&mut self, m: &mut Matcher<'a>, i: usize, t: &StartTok<'a, '_>) {
        let n = self.stack[i];
        if n.vis != Vis::Live || t.attrs.is_empty() {
            return;
        }
        self.retro |= CENSUS_MERGE_ANY;
        if REPLAY {
            // An element the page never wrote reads its source from the first tag that gave it any.
            if n.flags & SYNTH != 0 && !self.dom.has_prefix(n.depth) {
                self.dom.set_prefix(n.depth, SourcePrefix::new(t.start, t.end));
            }
            self.dom.merge(n.depth, t.attrs);
        } else if self.path.get(n.depth as usize) != Some(&n.uid) || m.merge_adds(n.depth as usize, t.attrs) {
            self.retro |= RETRO_MERGE;
        }
    }

    fn in_table_start(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        let at = t.start;
        let table_ctx = [Tag::Table];
        match t.tag {
            Tag::Caption => {
                self.clear_to(m, &table_ctx, at);
                self.afe.push(Afe::Marker);
                self.insert_html(m, t);
                self.mode = Mode::InCaption;
            }
            Tag::Colgroup => {
                self.clear_to(m, &table_ctx, at);
                self.insert_html(m, t);
                self.mode = Mode::InColumnGroup;
            }
            Tag::Col => {
                self.clear_to(m, &table_ctx, at);
                self.insert_synth(m, b"colgroup", Tag::Colgroup, at);
                self.mode = Mode::InColumnGroup;
                self.html_start(m, t)
            }
            Tag::Tbody | Tag::Tfoot | Tag::Thead => {
                self.clear_to(m, &table_ctx, at);
                self.insert_html(m, t);
                self.mode = Mode::InTableBody;
            }
            Tag::Td | Tag::Th | Tag::Tr => {
                self.clear_to(m, &table_ctx, at);
                self.insert_synth(m, b"tbody", Tag::Tbody, at);
                self.mode = Mode::InTableBody;
                self.html_start(m, t)
            }
            Tag::Table => {
                if let Some(k) = self.in_table_scope(Tag::Table) {
                    self.pop_to(m, k, at, at);
                    self.reset_mode();
                    self.html_start(m, t)
                }
            }
            Tag::Style | Tag::Script | Tag::Template => self.in_head_start(m, t),
            Tag::Input if attr(t.attrs, b"type").is_some_and(|v| v.eq_ignore_ascii_case(b"hidden")) => {
                self.insert_void(m, t)
            }
            Tag::Form => {
                if !self.form_ignored() {
                    self.insert_form(m, t);
                    self.pop(m, t.end);
                }
            }
            _ => {
                self.foster = true;
                self.in_body_start(m, t);
                self.foster = false;
            }
        }
    }

    /// Pop until the current node is one of `tags`, a `<template>` or the `<html>`.
    fn clear_to(&mut self, m: &mut Matcher<'a>, tags: &[Tag], at: usize) {
        while let Some(n) = self.stack.last() {
            if tags.contains(&n.tag) || matches!(n.tag, Tag::Template | Tag::Html) {
                break;
            }
            self.pop(m, at);
        }
    }

    /// Is a `<form>` start tag ignored: the form element pointer is set, and no template is open?
    fn form_ignored(&self) -> bool {
        self.form.is_some() && !self.has_template()
    }

    /// Insert a `<form>`, which outside a template becomes the form element pointer.
    fn insert_form(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        self.insert_html(m, t);
        if !self.has_template() {
            self.form = Some(self.cur().uid);
        }
    }

    /// End the caption, if one is in table scope. `false` when the token is to be ignored.
    fn end_caption(&mut self, m: &mut Matcher<'a>, ghost_end: usize, end: usize) -> bool {
        if self.in_table_scope(Tag::Caption).is_none() {
            return false;
        }
        self.close_element(m, &[Tag::Caption], None, ghost_end, end);
        self.afe.clear_to_marker();
        self.mode = Mode::InTable;
        true
    }

    /// End the open `<tbody>`, `<thead>` or `<tfoot>`. `false` when there is none in table scope.
    fn end_table_body(&mut self, m: &mut Matcher<'a>, at: usize) -> bool {
        if TABLE_SECTIONS.iter().all(|&g| self.in_table_scope(g).is_none()) {
            return false;
        }
        self.clear_to(m, &TABLE_SECTIONS, at);
        self.pop(m, at);
        self.mode = Mode::InTable;
        true
    }

    /// End the open row. `false` when there is no `<tr>` in table scope.
    fn end_row(&mut self, m: &mut Matcher<'a>, ghost_end: usize, end: usize) -> bool {
        if self.in_table_scope(Tag::Tr).is_none() {
            return false;
        }
        self.clear_to(m, &[Tag::Tr], ghost_end);
        self.pop_with(m, ghost_end, end);
        self.mode = Mode::InTableBody;
        true
    }

    /// Close the cell: through the topmost of `tags`, the `<td>` or `<th>` in table scope.
    fn close_cell(&mut self, m: &mut Matcher<'a>, tags: &[Tag], ghost_end: usize, end: usize) {
        self.close_element(m, tags, None, ghost_end, end);
        self.afe.clear_to_marker();
        self.mode = Mode::InRow;
    }

    // ================================================================== end tags, HTML content

    pub(super) fn html_end(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        match self.mode {
            Mode::Initial => {
                self.no_doctype();
                self.html_end(m, t)
            }
            Mode::BeforeHtml => {
                if matches!(t.tag, Tag::Head | Tag::Body | Tag::Html | Tag::Br) {
                    self.imply_html(m, t.start);
                    self.html_end(m, t)
                }
            }
            Mode::BeforeHead => {
                if matches!(t.tag, Tag::Head | Tag::Body | Tag::Html | Tag::Br) {
                    self.imply_head(m, t.start);
                    self.html_end(m, t)
                }
            }
            Mode::InHead => match t.tag {
                Tag::Head => self.leave_head(),
                Tag::Body | Tag::Html | Tag::Br => {
                    self.leave_head();
                    self.html_end(m, t)
                }
                Tag::Template => self.template_end(m, t),
                _ => {}
            },
            Mode::AfterHead => match t.tag {
                Tag::Template => self.template_end(m, t),
                Tag::Body | Tag::Html | Tag::Br => {
                    self.imply_body(m, t.start);
                    self.html_end(m, t)
                }
                _ => {}
            },
            Mode::InBody => self.in_body_end(m, t),
            Mode::Text => {
                self.pop(m, t.end);
                self.mode = self.original;
            }
            Mode::InTable => self.in_table_end(m, t),
            Mode::InCaption => match t.tag {
                Tag::Caption => {
                    self.end_caption(m, t.start, t.end);
                }
                Tag::Table => {
                    if self.end_caption(m, t.start, t.start) {
                        self.html_end(m, t)
                    }
                }
                Tag::Body | Tag::Col | Tag::Colgroup | Tag::Html | Tag::Tbody | Tag::Td | Tag::Tfoot | Tag::Th
                | Tag::Thead | Tag::Tr => {}
                _ => self.in_body_end(m, t),
            },
            Mode::InColumnGroup => match t.tag {
                Tag::Colgroup => {
                    if self.cur_is(Tag::Colgroup) {
                        self.pop(m, t.end);
                        self.mode = Mode::InTable;
                    }
                }
                Tag::Col => {}
                Tag::Template => self.template_end(m, t),
                _ => {
                    if self.cur_is(Tag::Colgroup) {
                        self.pop(m, t.start);
                        self.mode = Mode::InTable;
                        self.html_end(m, t)
                    }
                }
            },
            Mode::InTableBody => match t.tag {
                Tag::Tbody | Tag::Tfoot | Tag::Thead => {
                    if self.in_table_scope(t.tag).is_some() {
                        self.clear_to(m, &TABLE_SECTIONS, t.start);
                        self.pop(m, t.end);
                        self.mode = Mode::InTable;
                    }
                }
                Tag::Table => {
                    if self.end_table_body(m, t.start) {
                        self.html_end(m, t)
                    }
                }
                Tag::Body | Tag::Caption | Tag::Col | Tag::Colgroup | Tag::Html | Tag::Td | Tag::Th | Tag::Tr => {}
                _ => self.in_table_end(m, t),
            },
            Mode::InRow => match t.tag {
                Tag::Tr => {
                    self.end_row(m, t.start, t.end);
                }
                Tag::Table => {
                    if self.end_row(m, t.start, t.start) {
                        self.html_end(m, t)
                    }
                }
                Tag::Tbody | Tag::Tfoot | Tag::Thead => {
                    if self.in_table_scope(t.tag).is_some() && self.end_row(m, t.start, t.start) {
                        self.html_end(m, t)
                    }
                }
                Tag::Body | Tag::Caption | Tag::Col | Tag::Colgroup | Tag::Html | Tag::Td | Tag::Th => {}
                _ => self.in_table_end(m, t),
            },
            Mode::InCell => match t.tag {
                Tag::Td | Tag::Th => {
                    if self.in_table_scope(t.tag).is_some() {
                        self.close_cell(m, &[t.tag], t.start, t.end);
                    }
                }
                Tag::Body | Tag::Caption | Tag::Col | Tag::Colgroup | Tag::Html => {}
                Tag::Table | Tag::Tbody | Tag::Tfoot | Tag::Thead | Tag::Tr => {
                    if self.in_table_scope(t.tag).is_some() {
                        self.close_cell(m, &CELLS, t.start, t.start);
                        self.html_end(m, t)
                    }
                }
                _ => self.in_body_end(m, t),
            },
            Mode::InTemplate => {
                if t.tag == Tag::Template {
                    self.template_end(m, t)
                }
            }
            Mode::AfterBody => {
                if t.tag == Tag::Html {
                    self.mode = Mode::AfterAfterBody;
                } else {
                    self.mode = Mode::InBody;
                    self.html_end(m, t)
                }
            }
            Mode::InFrameset => {
                if t.tag == Tag::Frameset && !self.cur_is(Tag::Html) {
                    self.pop(m, t.end);
                    if !self.cur_is(Tag::Frameset) {
                        self.mode = Mode::AfterFrameset;
                    }
                }
            }
            Mode::AfterFrameset => {
                if t.tag == Tag::Html {
                    self.mode = Mode::AfterAfterFrameset;
                }
            }
            Mode::AfterAfterBody => {
                self.mode = Mode::InBody;
                self.html_end(m, t)
            }
            Mode::AfterAfterFrameset => {}
        }
    }

    fn in_table_end(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        match t.tag {
            Tag::Table => {
                if let Some(k) = self.in_table_scope(Tag::Table) {
                    self.pop_to(m, k, t.start, t.end);
                    self.reset_mode();
                }
            }
            Tag::Body | Tag::Caption | Tag::Col | Tag::Colgroup | Tag::Html | Tag::Tbody | Tag::Td | Tag::Tfoot
            | Tag::Th | Tag::Thead | Tag::Tr => {}
            Tag::Template => self.template_end(m, t),
            _ => {
                self.foster = true;
                self.in_body_end(m, t);
                self.foster = false;
            }
        }
    }

    fn in_body_end(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        match t.tag {
            Tag::Template => self.template_end(m, t),
            Tag::Body => {
                if self.in_scope(Tag::Body).is_some() {
                    self.mode = Mode::AfterBody;
                }
            }
            Tag::Html => {
                if self.in_scope(Tag::Body).is_some() {
                    self.mode = Mode::AfterBody;
                    self.html_end(m, t)
                }
            }
            Tag::Address | Tag::Article | Tag::Aside | Tag::Blockquote | Tag::Button | Tag::Center | Tag::Details
            | Tag::Dialog | Tag::Dir | Tag::Div | Tag::Dl | Tag::Fieldset | Tag::Figcaption | Tag::Figure
            | Tag::Footer | Tag::Header | Tag::Hgroup | Tag::Listing | Tag::Main | Tag::Menu | Tag::Nav | Tag::Ol
            | Tag::Pre | Tag::Search | Tag::Section | Tag::Select | Tag::Summary | Tag::Ul => {
                if self.in_scope(t.tag).is_some() {
                    self.close_element(m, &[t.tag], None, t.start, t.end);
                }
            }
            Tag::Form => {
                if !self.has_template() {
                    let node = self.form.take();
                    let Some(k) = self.stack.scoped(node.and_then(|uid| self.stack.index_of(uid)), Bound::Scope) else {
                        return;
                    };
                    self.implied_end(m, None, t.start);
                    let k = node.and_then(|uid| self.stack.index_of(uid)).unwrap_or(k);
                    if k == self.stack.len() - 1 {
                        self.pop(m, t.end);
                    } else {
                        // The form leaves the stack and stays the DOM parent of what is open in it.
                        self.stack.remove(k);
                    }
                } else if self.in_scope(Tag::Form).is_some() {
                    self.close_element(m, &[Tag::Form], None, t.start, t.end);
                }
            }
            Tag::P => {
                if self.in_button_scope(Tag::P).is_none() {
                    self.insert_synth(m, b"p", Tag::P, t.start);
                }
                self.close_p(m, t.start, t.end);
            }
            Tag::Li => {
                if self.in_list_scope(Tag::Li).is_some() {
                    self.close_element(m, &[Tag::Li], Some(Tag::Li), t.start, t.end);
                }
            }
            Tag::Dd | Tag::Dt => {
                if self.in_scope(t.tag).is_some() {
                    self.close_element(m, &[t.tag], Some(t.tag), t.start, t.end);
                }
            }
            Tag::H1 | Tag::H2 | Tag::H3 | Tag::H4 | Tag::H5 | Tag::H6 => {
                if self.stack.scoped(self.stack.topmost_of(&HEADINGS), Bound::Scope).is_some() {
                    self.close_element(m, &HEADINGS, None, t.start, t.end);
                }
            }
            Tag::A | Tag::B | Tag::Big | Tag::Code | Tag::Em | Tag::Font | Tag::I | Tag::Nobr | Tag::S
            | Tag::Small | Tag::Strike | Tag::Strong | Tag::Tt | Tag::U => self.adoption_agency(m, t),
            Tag::Applet | Tag::Marquee | Tag::Object => {
                if self.in_scope(t.tag).is_some() {
                    self.close_element(m, &[t.tag], None, t.start, t.end);
                    self.afe.clear_to_marker();
                }
            }
            Tag::Br => {
                // `</br>` is a `<br>` with no attributes.
                let br = StartTok {
                    name: b"br",
                    low: Some(b"br"),
                    tag: Tag::Br,
                    attrs: &[],
                    self_closing: false,
                    start: t.start,
                    end: t.end,
                };
                self.reconstruct(m, t.start);
                self.insert_void(m, &br);
                self.frameset_ok = false;
            }
            _ => self.any_other_end(m, t),
        }
    }

    /// "Any other end tag": the nearest HTML element of the name closes, unless a special element is
    /// nearer.
    fn any_other_end(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        let nearest = if t.tag == Tag::Other { self.stack.topmost_named(t.name) } else { self.stack.topmost(t.tag) };
        let found = self.stack.scoped(nearest, Bound::Special);
        let Some(i) = found else { return };
        let except = if t.tag == Tag::Other { None } else { Some(t.tag) };
        self.implied_end(m, except, t.start);
        if i < self.stack.len() {
            self.pop_to(m, i, t.start, t.end);
        }
    }

    /// The adoption agency algorithm (13.2.6.4.7), for an end tag `t` of a formatting element.
    ///
    /// The stack of open elements and the list come out exactly as the spec's do, so what is open and
    /// where later content goes are the browser's. With a furthest block the algorithm also moves
    /// elements already matched (the block leaves the formatting element, and its content moves into a
    /// clone): streaming, that is a retroactive rule, and the replay's tree performs the moves.
    fn adoption_agency(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        if let Some(c) = self.stack.last() {
            if c.ns == Ns::Html && c.tag == t.tag && self.afe.index_of(c.uid).is_none() {
                return self.pop(m, t.end);
            }
        }
        for _ in 0..8 {
            let Some(fi) = self.afe.last_of(t.tag) else { return self.any_other_end(m, t) };
            let Afe::El { uid: fuid, tok: ftok } = self.afe[fi] else { unreachable!() };
            let Some(si) = self.stack.index_of(fuid) else {
                self.afe.remove(fi);
                return;
            };
            if self.stack.scoped(Some(si), Bound::Scope).is_none() {
                return;
            }
            let Some(bi) = (si + 1..self.stack.len()).find(|&k| self.stack[k].cat & SPECIAL != 0) else {
                self.pop_to(m, si, t.start, t.end);
                if let Some(k) = self.afe.index_of(fuid) {
                    self.afe.remove(k);
                }
                return;
            };
            if self.stack[bi].vis == Vis::Live {
                self.retro |= RETRO_ADOPTION;
            }
            let empty = self.stack[bi].flags & HAS_CHILD == 0;
            let clone_at = self.furthest_block(si, fi, bi, ftok, t.start);
            if !REPLAY && empty {
                // The clone is appended to a block with nothing in it yet, which is an insertion at the
                // current position like any other.
                self.materialize(m, clone_at, t.start);
            }
        }
    }

    /// The furthest-block half of the adoption agency, on the stack, the list and the replay's tree.
    /// Streaming, clones are pending: they get matcher elements only if something is inserted into
    /// them. A clone made at `at` reads its source from the start tag it copies. Returns the stack index
    /// of the formatting element's clone, which the furthest block now holds.
    fn furthest_block(&mut self, si: usize, fi: usize, bi: usize, ftok: u32, at: usize) -> usize {
        // The common ancestor's index: the loop below changes only entries above it.
        let common = si - 1;
        // The block's world is inert when the formatting element is template content.
        let inert = self.stack[common].vis == Vis::Inert || self.stack[bi].vis == Vis::Inert;
        let fuid = self.stack[si].uid;
        let block = self.stack[bi];
        let mut bookmark = fi;
        let mut bookmark_moved = false;
        let mut last = block.depth;
        let mut node_i = bi;
        let mut inner = 0;
        loop {
            inner += 1;
            node_i -= 1;
            let n = self.stack[node_i];
            if n.uid == fuid {
                break;
            }
            let mut ai = self.afe.index_of(n.uid);
            if inner > 3 {
                if let Some(k) = ai {
                    self.afe.remove(k);
                    if k < bookmark {
                        bookmark -= 1;
                    }
                    ai = None;
                }
            }
            let Some(ai) = ai else {
                self.stack.remove(node_i);
                if REPLAY && n.tag == Tag::Option && n.vis == Vis::Live {
                    self.option_closed(n.depth);
                }
                continue;
            };
            let Afe::El { tok, .. } = self.afe[ai] else { unreachable!() };
            let clone = self.clone_node(tok, inert, at);
            self.afe.set(ai, Afe::El { uid: clone.uid, tok });
            self.stack.replace(node_i, clone);
            if !bookmark_moved {
                bookmark = ai + 1;
                bookmark_moved = true;
            }
            if REPLAY && !inert {
                self.dom.append(clone.depth, last);
                last = clone.depth;
            }
        }
        if REPLAY && !inert {
            self.adopt_into_common(common, last);
        }
        let clone = self.clone_node(ftok, inert, at);
        if REPLAY && !inert {
            self.dom.move_children(block.depth, clone.depth);
            self.dom.append(block.depth, clone.depth);
        }
        let fi = self.afe.index_of(fuid).expect("the formatting element is on the list");
        self.afe.remove(fi);
        let bookmark = if fi < bookmark { bookmark - 1 } else { bookmark };
        self.afe.insert(bookmark.min(self.afe.len()), Afe::El { uid: clone.uid, tok: ftok });
        let si = self.stack.index_of(fuid).expect("the formatting element is open");
        self.stack.remove(si);
        let bi = self.stack.index_of(block.uid).expect("the furthest block is open");
        self.stack.insert(bi + 1, clone);
        bi + 1
    }

    /// Insert the replay's node `last` at the appropriate place with the stack entry at `common` as the
    /// override target.
    fn adopt_into_common(&mut self, common: usize, last: u32) {
        match self.location_in(common) {
            Loc::Into(i) => self.dom.append(self.stack[i].depth, last),
            Loc::Foster(t) => self.dom_foster(t, last),
            Loc::Inert => self.dom.detach(last),
            Loc::Document => unreachable!("an override target is on the stack"),
        }
    }

    /// A copy of the formatting element made for token `tok`: pending while streaming, a node of the
    /// replay's tree when replaying.
    fn clone_node(&mut self, tok: u32, inert: bool, at: usize) -> Node<'a> {
        let (name, tag) = (self.afe.toks[tok as usize].name, self.afe.toks[tok as usize].tag);
        let mut buf = [0; MAX_NAME];
        let mut node = self.new_node(name, lower(name, &mut buf), tag, Ns::Html, &[]);
        if inert {
            node.vis = Vis::Inert;
        } else if REPLAY {
            let attrs = self.afe.attrs_of(tok).to_vec();
            node.depth = self.dom.element(name, super::super::HTML_ELEMENT, attrs, at);
            self.dom.set_prefix(node.depth, self.afe.toks[tok as usize].span);
        } else {
            node.vis = Vis::Pending(tok);
        }
        node
    }

    // ================================================================== foreign content

    pub(super) fn foreign_start(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        if breaks_out(t.tag) || (t.tag == Tag::Font && font_breaks_out(t.attrs)) {
            self.pop_to_html(m, t.start);
            return self.html_start(m, t);
        }
        let ns = self.cur().ns;
        self.insert_foreign(m, t, ns);
    }

    pub(super) fn foreign_end(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        if matches!(t.tag, Tag::Br | Tag::P) {
            self.pop_to_html(m, t.start);
            return self.html_end(m, t);
        }
        let mut i = self.stack.len() - 1;
        loop {
            if i == 0 {
                return;
            }
            if self.stack[i].name.eq_ignore_ascii_case(t.name) {
                return self.pop_to(m, i, t.start, t.end);
            }
            i -= 1;
            if self.stack[i].ns == Ns::Html {
                return self.html_end(m, t);
            }
        }
    }

    /// Pop until the current node is HTML or an integration point, for a tag that ends foreign content.
    fn pop_to_html(&mut self, m: &mut Matcher<'a>, at: usize) {
        while let Some(c) = self.stack.last() {
            if c.ns == Ns::Html || c.flags & (super::HTML_IP | super::MATHML_TEXT_IP) != 0 {
                break;
            }
            self.pop(m, at);
        }
    }

    // ================================================================== character data

    pub(super) fn foreign_text(&mut self, m: &mut Matcher<'a>, b: &'a [u8], dm: DataMode, s: usize) {
        // Foreign content keeps a NUL as U+FFFD, which is how RCDATA text decodes.
        let dm = if dm == DataMode::Normal { DataMode::Rcdata } else { dm };
        self.insert_text(m, b, dm, s);
        self.clear_frameset_ok(m, b);
    }

    pub(super) fn html_text(&mut self, m: &mut Matcher<'a>, mut b: &'a [u8], dm: DataMode, mut s: usize) {
        loop {
            if b.is_empty() {
                return;
            }
            match self.mode {
                Mode::Initial | Mode::BeforeHtml | Mode::BeforeHead => {
                    let k = ws_prefix(b, true);
                    if k > 0 {
                        self.absorb(m, s, s + k);
                        (b, s) = (&b[k..], s + k);
                        continue;
                    }
                    match self.mode {
                        Mode::Initial => self.no_doctype(),
                        Mode::BeforeHtml => self.imply_html(m, s),
                        _ => self.imply_head(m, s),
                    }
                }
                Mode::InHead | Mode::AfterHead => {
                    let k = ws_prefix(b, true);
                    if k > 0 {
                        if self.mode == Mode::AfterHead {
                            self.continue_html_text(m, s);
                        }
                        self.insert_text(m, &b[..k], dm, s);
                        (b, s) = (&b[k..], s + k);
                        continue;
                    }
                    if self.mode == Mode::InHead {
                        self.leave_head();
                    } else {
                        self.imply_body(m, s);
                    }
                }
                Mode::InBody | Mode::InCaption | Mode::InCell | Mode::InTemplate => return self.body_text(m, b, dm, s),
                Mode::InTable | Mode::InTableBody | Mode::InRow => return self.table_text(m, b, dm, s),
                Mode::InColumnGroup => {
                    let k = ws_prefix(b, false);
                    if k > 0 {
                        self.insert_text(m, &b[..k], dm, s);
                        (b, s) = (&b[k..], s + k);
                        continue;
                    }
                    if !self.cur_is(Tag::Colgroup) {
                        return self.absorb(m, s, s + b.len());
                    }
                    self.pop(m, s);
                    self.mode = Mode::InTable;
                }
                Mode::AfterBody | Mode::AfterAfterBody => {
                    let k = ws_prefix(b, false);
                    if k > 0 {
                        self.body_text(m, &b[..k], dm, s);
                        (b, s) = (&b[k..], s + k);
                        continue;
                    }
                    self.mode = Mode::InBody;
                }
                Mode::InFrameset | Mode::AfterFrameset | Mode::AfterAfterFrameset => {
                    // Whitespace is inserted and everything else ignored, character by character.
                    while !b.is_empty() {
                        let k = ws_prefix(b, false);
                        if k > 0 {
                            if self.mode == Mode::AfterAfterFrameset {
                                self.body_text(m, &b[..k], dm, s);
                            } else {
                                self.insert_text(m, &b[..k], dm, s);
                            }
                        }
                        let j = (k..b.len()).find(|&p| ws_at(&b[p..], false) > 0).unwrap_or(b.len());
                        self.absorb(m, s + k, s + j);
                        (b, s) = (&b[j..], s + j);
                    }
                    return;
                }
                Mode::Text => return self.insert_text(m, b, dm, s),
            }
        }
    }

    // ================================================================== end of input

    /// An end-of-file token, at `at`, under each insertion mode's rules: the frame is finished however
    /// early the input ended (a `<body>` after a lone `<title>`, all three after nothing), a text element
    /// and open template content are closed, and then parsing stops.
    pub(super) fn eof(&mut self, m: &mut Matcher<'a>, at: usize) {
        loop {
            match self.mode {
                Mode::Initial => self.no_doctype(),
                Mode::BeforeHtml => self.imply_html(m, at),
                Mode::BeforeHead => self.imply_head(m, at),
                Mode::InHead => self.leave_head(),
                Mode::AfterHead => self.imply_body(m, at),
                Mode::Text => {
                    self.pop(m, at);
                    self.mode = self.original;
                }
                Mode::InBody | Mode::InTable | Mode::InCaption | Mode::InColumnGroup | Mode::InTableBody
                | Mode::InRow | Mode::InCell | Mode::InTemplate => {
                    let Some(k) = self.stack.topmost(Tag::Template) else { return };
                    self.pop_to(m, k, at, at);
                    self.afe.clear_to_marker();
                    self.templates.pop();
                    self.reset_mode();
                }
                Mode::AfterBody | Mode::InFrameset | Mode::AfterFrameset | Mode::AfterAfterBody
                | Mode::AfterAfterFrameset => return,
            }
        }
    }

    /// Character tokens under the in-body rules.
    fn body_text(&mut self, m: &mut Matcher<'a>, b: &'a [u8], dm: DataMode, s: usize) {
        if dm == DataMode::Normal && b.iter().all(|&c| c == 0) {
            return self.absorb(m, s, s + b.len());
        }
        self.reconstruct(m, s);
        self.insert_text(m, b, dm, s);
        self.clear_frameset_ok(m, b);
    }

    /// Inserting the run `b` clears frameset-ok, unless it holds only what Chrome keeps it set for.
    fn clear_frameset_ok(&mut self, m: &Matcher<'a>, b: &[u8]) {
        if self.frameset_ok && ends_frameset_ok(b, m.input.encoding() == encoding_rs::UTF_8) {
            self.frameset_ok = false;
        }
    }

    /// Character tokens in a table: whitespace stays in the table, and a run holding anything else is
    /// fostered out of it whole.
    fn table_text(&mut self, m: &mut Matcher<'a>, b: &'a [u8], dm: DataMode, s: usize) {
        let in_table_text = self
            .stack
            .last()
            .is_some_and(|n| matches!(n.tag, Tag::Table | Tag::Tbody | Tag::Template | Tag::Tfoot | Tag::Thead | Tag::Tr));
        if in_table_text && !has_content(b) {
            if b.iter().all(|&c| c == 0) {
                return self.absorb(m, s, s + b.len());
            }
            return self.insert_text(m, b, dm, s);
        }
        self.foster = true;
        self.body_text(m, b, dm, s);
        self.foster = false;
    }
}
