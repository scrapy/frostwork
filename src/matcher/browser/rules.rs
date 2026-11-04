//! The insertion modes (WHATWG 13.2.6.4) and the rules for foreign content (13.2.6.5), as Chrome 156
//! runs them: the `<select>` rules are the in-body ones of customisable select, and the scripting flag
//! is on. Each function is one section of the spec; "process using the rules for X" is a call, and
//! "reprocess" a call after switching modes.

use super::tags::{breaks_out, font_breaks_out, Tag};
use super::{has_content, ws_at, ws_prefix, Afe, Browser, EndTok, Mode, Node, Ns, StartTok, Vis, HAS_CHILD, SHADOW_HOST};
use crate::implied_close::{SCOPE, SPECIAL};
use crate::tokenizer::DataMode;
use crate::matcher::Matcher;

/// The names `attachShadow()` accepts as a host, besides a custom element.
fn valid_shadow_host(name: &[u8]) -> bool {
    let mut buf = [0; 24];
    let Some(low) = super::tags::lower(name, &mut buf) else { return custom_element_name(name) };
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

impl<'a> Browser<'a> {
    // ================================================================== start tags, HTML content

    pub(super) fn html_start(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
        match self.mode {
            Mode::Initial => {
                self.quirks = true;
                self.mode = Mode::BeforeHtml;
                self.html_start(m, t)
            }
            Mode::BeforeHtml => {
                if t.tag == Tag::Html {
                    self.insert_html(m, t);
                } else {
                    self.insert_synth(m, b"html", Tag::Html, t.start);
                    self.mode = Mode::BeforeHead;
                    return self.html_start(m, t);
                }
                self.mode = Mode::BeforeHead;
            }
            Mode::BeforeHead => match t.tag {
                Tag::Html => self.in_body_start(m, t),
                Tag::Head => {
                    self.insert_html(m, t);
                    self.head = self.stack.last().copied();
                    self.mode = Mode::InHead;
                }
                _ => {
                    self.insert_synth(m, b"head", Tag::Head, t.start);
                    self.head = self.stack.last().copied();
                    self.mode = Mode::InHead;
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
                    if self.path.len() == 1 && self.html_text.is_none() {
                        self.html_text = m.take_text();
                    }
                    self.stack.push(head);
                    self.in_head_start(m, t);
                    if let Some(k) = self.index_of(head.uid) {
                        self.stack.remove(k);
                    }
                }
                Tag::Head => {}
                _ => {
                    self.finish_head(m, t.start);
                    self.insert_synth(m, b"body", Tag::Body, t.start);
                    self.frameset_ok = true;
                    self.mode = Mode::InBody;
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
                    self.clear_to(m, &[Tag::Tbody, Tag::Tfoot, Tag::Thead], t.start);
                    self.insert_html(m, t);
                    self.mode = Mode::InRow;
                }
                Tag::Th | Tag::Td => {
                    self.clear_to(m, &[Tag::Tbody, Tag::Tfoot, Tag::Thead], t.start);
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
                        self.close_cell(m, t.start, t.start);
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
            && valid_shadow_host(h.name)
            && !matches!(self.location(), super::Loc::Foster(_));
        if attaches {
            self.stack[host].flags |= SHADOW_HOST;
            let mut node = self.new_node(t.name, t.tag, Ns::Html, t.attrs);
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
        if let Some(k) = self.stack.iter().rposition(|n| n.tag == Tag::Template) {
            self.pop_to(m, k, t.start, t.end);
        }
        self.clear_afe_to_marker();
        self.templates.pop();
        self.reset_mode();
    }

    pub(super) fn in_body_start(&mut self, m: &mut Matcher<'a>, t: &StartTok<'a, '_>) {
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
                    // The body and everything in it leave the document. Already matched, they stand.
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
                if self.stack.last().is_some_and(|n| is_heading(n.tag)) {
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
                let in_template = self.has_template();
                if self.form.is_some() && !in_template {
                    return;
                }
                self.close_p_if_in_button_scope(m, at);
                self.insert_html(m, t);
                if !in_template {
                    self.form = Some(self.cur().uid);
                }
            }
            Tag::Li | Tag::Dd | Tag::Dt => {
                self.frameset_ok = false;
                for i in (0..self.stack.len()).rev() {
                    let n = self.stack[i];
                    let closes = match t.tag {
                        Tag::Li => n.tag == Tag::Li,
                        _ => matches!(n.tag, Tag::Dd | Tag::Dt),
                    };
                    if closes {
                        self.implied_end(m, Some(n.tag), at);
                        if let Some(k) = self.stack.iter().rposition(|x| x.tag == n.tag) {
                            self.pop_to(m, k, at, at);
                        }
                        break;
                    }
                    if n.cat & SPECIAL != 0 && !matches!(n.tag, Tag::Address | Tag::Div | Tag::P) {
                        break;
                    }
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
                    self.implied_end(m, None, at);
                    if let Some(k) = self.stack.iter().rposition(|n| n.tag == Tag::Button) {
                        self.pop_to(m, k, at, at);
                    }
                }
                self.reconstruct(m, at);
                self.insert_html(m, t);
                self.frameset_ok = false;
            }
            Tag::A => {
                let from = self.afe_last_marker();
                let open_a = self.afe[from..].iter().rev().find_map(|e| match *e {
                    Afe::El { uid, tok } if self.toks[tok as usize].tag == Tag::A => Some(uid),
                    _ => None,
                });
                if let Some(uid) = open_a {
                    self.adoption_agency(m, EndTok { name: t.name, tag: Tag::A, start: at, end: at });
                    if let Some(k) = self.afe_index_of(uid) {
                        self.afe.remove(k);
                    }
                    if let Some(k) = self.index_of(uid) {
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
                let img = StartTok { name: b"img", tag: Tag::Img, ..*t };
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

    /// A second `<html>` or `<body>` start tag: its attributes join the open element's.
    fn merge_into(&mut self, m: &mut Matcher<'a>, i: usize, t: &StartTok<'a, '_>) {
        let n = self.stack[i];
        if n.vis == Vis::Live && self.path.get(n.depth as usize) == Some(&n.uid) {
            m.merge_attrs(n.depth as usize, t.attrs);
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
                let in_template = self.has_template();
                if self.form.is_some() && !in_template {
                    return;
                }
                self.insert_html(m, t);
                if !in_template {
                    self.form = Some(self.cur().uid);
                }
                self.pop(m, t.end);
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

    /// End the caption, if one is in table scope. `false` when the token is to be ignored.
    fn end_caption(&mut self, m: &mut Matcher<'a>, ghost_end: usize, end: usize) -> bool {
        let Some(k) = self.in_table_scope(Tag::Caption) else { return false };
        self.implied_end(m, None, ghost_end);
        let k = self.stack.iter().rposition(|n| n.tag == Tag::Caption).unwrap_or(k);
        self.pop_to(m, k, ghost_end, end);
        self.clear_afe_to_marker();
        self.mode = Mode::InTable;
        true
    }

    /// End the open `<tbody>`, `<thead>` or `<tfoot>`. `false` when there is none in table scope.
    fn end_table_body(&mut self, m: &mut Matcher<'a>, at: usize) -> bool {
        if [Tag::Tbody, Tag::Thead, Tag::Tfoot].iter().all(|&g| self.in_table_scope(g).is_none()) {
            return false;
        }
        self.clear_to(m, &[Tag::Tbody, Tag::Tfoot, Tag::Thead], at);
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

    fn close_cell(&mut self, m: &mut Matcher<'a>, ghost_end: usize, end: usize) {
        self.implied_end(m, None, ghost_end);
        if let Some(k) = self.stack.iter().rposition(|n| matches!(n.tag, Tag::Td | Tag::Th)) {
            self.pop_to(m, k, ghost_end, end);
        }
        self.clear_afe_to_marker();
        self.mode = Mode::InRow;
    }

    // ================================================================== end tags, HTML content

    pub(super) fn html_end(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        match self.mode {
            Mode::Initial => {
                self.quirks = true;
                self.mode = Mode::BeforeHtml;
                self.html_end(m, t)
            }
            Mode::BeforeHtml => {
                if matches!(t.tag, Tag::Head | Tag::Body | Tag::Html | Tag::Br) {
                    self.insert_synth(m, b"html", Tag::Html, t.start);
                    self.mode = Mode::BeforeHead;
                    self.html_end(m, t)
                }
            }
            Mode::BeforeHead => {
                if matches!(t.tag, Tag::Head | Tag::Body | Tag::Html | Tag::Br) {
                    self.insert_synth(m, b"head", Tag::Head, t.start);
                    self.head = self.stack.last().copied();
                    self.mode = Mode::InHead;
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
                    self.finish_head(m, t.start);
                    self.insert_synth(m, b"body", Tag::Body, t.start);
                    self.frameset_ok = true;
                    self.mode = Mode::InBody;
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
                        self.clear_to(m, &[Tag::Tbody, Tag::Tfoot, Tag::Thead], t.start);
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
                        self.implied_end(m, None, t.start);
                        if let Some(k) = self.stack.iter().rposition(|n| n.tag == t.tag) {
                            self.pop_to(m, k, t.start, t.end);
                        }
                        self.clear_afe_to_marker();
                        self.mode = Mode::InRow;
                    }
                }
                Tag::Body | Tag::Caption | Tag::Col | Tag::Colgroup | Tag::Html => {}
                Tag::Table | Tag::Tbody | Tag::Tfoot | Tag::Thead | Tag::Tr => {
                    if self.in_table_scope(t.tag).is_some() {
                        self.close_cell(m, t.start, t.start);
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
                    self.implied_end(m, None, t.start);
                    if let Some(k) = self.stack.iter().rposition(|n| n.tag == t.tag) {
                        self.pop_to(m, k, t.start, t.end);
                    }
                }
            }
            Tag::Form => {
                if !self.has_template() {
                    let node = self.form.take();
                    let Some(k) = node.and_then(|uid| self.scope_by(SCOPE, |n| n.uid == uid)) else { return };
                    self.implied_end(m, None, t.start);
                    let k = node.and_then(|uid| self.index_of(uid)).unwrap_or(k);
                    if k == self.stack.len() - 1 {
                        self.pop(m, t.end);
                    } else {
                        // The form leaves the stack and stays the DOM parent of what is open in it.
                        self.stack.remove(k);
                    }
                } else if self.in_scope(Tag::Form).is_some() {
                    self.implied_end(m, None, t.start);
                    if let Some(k) = self.stack.iter().rposition(|n| n.tag == Tag::Form) {
                        self.pop_to(m, k, t.start, t.end);
                    }
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
                    self.implied_end(m, Some(Tag::Li), t.start);
                    if let Some(k) = self.stack.iter().rposition(|n| n.tag == Tag::Li) {
                        self.pop_to(m, k, t.start, t.end);
                    }
                }
            }
            Tag::Dd | Tag::Dt => {
                if self.in_scope(t.tag).is_some() {
                    self.implied_end(m, Some(t.tag), t.start);
                    if let Some(k) = self.stack.iter().rposition(|n| n.tag == t.tag) {
                        self.pop_to(m, k, t.start, t.end);
                    }
                }
            }
            Tag::H1 | Tag::H2 | Tag::H3 | Tag::H4 | Tag::H5 | Tag::H6 => {
                if self.scope_by(SCOPE, |n| is_heading(n.tag)).is_some() {
                    self.implied_end(m, None, t.start);
                    if let Some(k) = self.stack.iter().rposition(|n| is_heading(n.tag)) {
                        self.pop_to(m, k, t.start, t.end);
                    }
                }
            }
            Tag::A | Tag::B | Tag::Big | Tag::Code | Tag::Em | Tag::Font | Tag::I | Tag::Nobr | Tag::S
            | Tag::Small | Tag::Strike | Tag::Strong | Tag::Tt | Tag::U => self.adoption_agency(m, t),
            Tag::Applet | Tag::Marquee | Tag::Object => {
                if self.in_scope(t.tag).is_some() {
                    self.implied_end(m, None, t.start);
                    if let Some(k) = self.stack.iter().rposition(|n| n.tag == t.tag) {
                        self.pop_to(m, k, t.start, t.end);
                    }
                    self.clear_afe_to_marker();
                }
            }
            Tag::Br => {
                // `</br>` is a `<br>` with no attributes.
                let br = StartTok { name: b"br", tag: Tag::Br, attrs: &[], self_closing: false, start: t.start, end: t.end };
                self.reconstruct(m, t.start);
                self.insert_void(m, &br);
                self.frameset_ok = false;
            }
            _ => self.any_other_end(m, t),
        }
    }

    fn any_other_end(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        for i in (0..self.stack.len()).rev() {
            let n = self.stack[i];
            let same = n.ns == Ns::Html
                && if t.tag == Tag::Other { n.tag == Tag::Other && n.name.eq_ignore_ascii_case(t.name) } else { n.tag == t.tag };
            if same {
                let except = if t.tag == Tag::Other { None } else { Some(t.tag) };
                self.implied_end(m, except, t.start);
                if i < self.stack.len() {
                    self.pop_to(m, i, t.start, t.end);
                }
                return;
            }
            if n.cat & SPECIAL != 0 {
                return;
            }
        }
    }

    /// The adoption agency algorithm (13.2.6.4.7), for an end tag `t` of a formatting element.
    ///
    /// The stack of open elements and the list come out exactly as the spec's do, so what is open and
    /// where later content goes are the browser's. With a furthest block the algorithm also moves
    /// elements already matched (the block leaves the formatting element, and its content moves into a
    /// clone); those keep the ancestors they opened under, and the elements removed from the stack stay
    /// their matcher ancestors until the insertion point leaves them.
    fn adoption_agency(&mut self, m: &mut Matcher<'a>, t: EndTok<'a>) {
        if let Some(c) = self.stack.last() {
            if c.ns == Ns::Html && c.tag == t.tag && self.afe_index_of(c.uid).is_none() {
                return self.pop(m, t.end);
            }
        }
        for _ in 0..8 {
            let from = self.afe_last_marker();
            let found = self.afe[from..].iter().rposition(|e| match *e {
                Afe::El { tok, .. } => self.toks[tok as usize].tag == t.tag,
                Afe::Marker => false,
            });
            let Some(fi) = found.map(|k| k + from) else { return self.any_other_end(m, t) };
            let Afe::El { uid: fuid, tok: ftok } = self.afe[fi] else { unreachable!() };
            let Some(si) = self.index_of(fuid) else {
                self.afe.remove(fi);
                return;
            };
            if self.scope_by(SCOPE, |n| n.uid == fuid).is_none() {
                return;
            }
            let Some(bi) = (si + 1..self.stack.len()).find(|&k| self.stack[k].cat & SPECIAL != 0) else {
                self.pop_to(m, si, t.start, t.end);
                if let Some(k) = self.afe_index_of(fuid) {
                    self.afe.remove(k);
                }
                return;
            };
            let empty = self.stack[bi].flags & HAS_CHILD == 0;
            let clone_at = self.furthest_block(si, fi, bi, ftok);
            if empty {
                // The clone is appended to a block with nothing in it yet, which is an insertion at the
                // current position like any other.
                self.materialize(m, clone_at, t.start);
            }
        }
    }

    /// The furthest-block half of the adoption agency, on the stack and the list. Clones are pending:
    /// they get matcher elements only if something is inserted into them. Returns the stack index of
    /// the formatting element's clone, which the furthest block now holds.
    fn furthest_block(&mut self, si: usize, fi: usize, bi: usize, ftok: u32) -> usize {
        let common_inert = self.stack[si - 1].vis == Vis::Inert;
        let fuid = self.stack[si].uid;
        let block_uid = self.stack[bi].uid;
        let mut bookmark = fi;
        let mut bookmark_moved = false;
        let mut node_i = bi;
        let mut inner = 0;
        loop {
            inner += 1;
            node_i -= 1;
            let n = self.stack[node_i];
            if n.uid == fuid {
                break;
            }
            let mut ai = self.afe_index_of(n.uid);
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
                continue;
            };
            let Afe::El { tok, .. } = self.afe[ai] else { unreachable!() };
            let clone = self.clone_node(tok, common_inert);
            self.afe[ai] = Afe::El { uid: clone.uid, tok };
            self.stack[node_i] = clone;
            if !bookmark_moved {
                bookmark = ai + 1;
                bookmark_moved = true;
            }
        }
        let clone = self.clone_node(ftok, common_inert);
        let fi = self.afe_index_of(fuid).expect("the formatting element is on the list");
        self.afe.remove(fi);
        let bookmark = if fi < bookmark { bookmark - 1 } else { bookmark };
        self.afe.insert(bookmark.min(self.afe.len()), Afe::El { uid: clone.uid, tok: ftok });
        let si = self.index_of(fuid).expect("the formatting element is open");
        self.stack.remove(si);
        let bi = self.index_of(block_uid).expect("the furthest block is open");
        self.stack.insert(bi + 1, clone);
        bi + 1
    }

    fn clone_node(&mut self, tok: u32, inert: bool) -> Node<'a> {
        let (name, tag) = (self.toks[tok as usize].name, self.toks[tok as usize].tag);
        let mut node = self.new_node(name, tag, Ns::Html, &[]);
        node.vis = if inert { Vis::Inert } else { Vis::Pending(tok) };
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
        if has_content(b) {
            self.frameset_ok = false;
        }
    }

    pub(super) fn html_text(&mut self, m: &mut Matcher<'a>, mut b: &'a [u8], dm: DataMode, mut s: usize) {
        loop {
            if b.is_empty() {
                return;
            }
            let lead = |b: &[u8], nul: bool| ws_prefix(b, nul);
            match self.mode {
                Mode::Initial | Mode::BeforeHtml | Mode::BeforeHead => {
                    let k = lead(b, true);
                    if k > 0 {
                        m.absorb_into_text_gap(s, s + k);
                        (b, s) = (&b[k..], s + k);
                        continue;
                    }
                    match self.mode {
                        Mode::Initial => {
                            self.quirks = true;
                            self.mode = Mode::BeforeHtml;
                        }
                        Mode::BeforeHtml => {
                            self.insert_synth(m, b"html", Tag::Html, s);
                            self.mode = Mode::BeforeHead;
                        }
                        _ => {
                            self.insert_synth(m, b"head", Tag::Head, s);
                            self.head = self.stack.last().copied();
                            self.mode = Mode::InHead;
                        }
                    }
                }
                Mode::InHead | Mode::AfterHead => {
                    let k = lead(b, true);
                    if k > 0 {
                        if self.mode == Mode::AfterHead {
                            self.release_html_text(m, s);
                        }
                        self.insert_text(m, &b[..k], dm, s);
                        (b, s) = (&b[k..], s + k);
                        continue;
                    }
                    if self.mode == Mode::InHead {
                        self.leave_head();
                    } else {
                        self.finish_head(m, s);
                        self.insert_synth(m, b"body", Tag::Body, s);
                        self.frameset_ok = true;
                        self.mode = Mode::InBody;
                    }
                }
                Mode::InBody | Mode::InCaption | Mode::InCell | Mode::InTemplate => return self.body_text(m, b, dm, s),
                Mode::InTable | Mode::InTableBody | Mode::InRow => return self.table_text(m, b, dm, s),
                Mode::InColumnGroup => {
                    let k = lead(b, false);
                    if k > 0 {
                        self.insert_text(m, &b[..k], dm, s);
                        (b, s) = (&b[k..], s + k);
                        continue;
                    }
                    if !self.cur_is(Tag::Colgroup) {
                        return m.absorb_into_text_gap(s, s + b.len());
                    }
                    self.pop(m, s);
                    self.mode = Mode::InTable;
                }
                Mode::AfterBody | Mode::AfterAfterBody => {
                    let k = lead(b, false);
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
                        let k = lead(b, false);
                        if k > 0 {
                            if self.mode == Mode::AfterAfterFrameset {
                                self.body_text(m, &b[..k], dm, s);
                            } else {
                                self.insert_text(m, &b[..k], dm, s);
                            }
                        }
                        let j = (k..b.len()).find(|&p| ws_at(&b[p..], false) > 0).unwrap_or(b.len());
                        m.absorb_into_text_gap(s + k, s + j);
                        (b, s) = (&b[j..], s + j);
                    }
                    return;
                }
                Mode::Text => return self.insert_text(m, b, dm, s),
            }
        }
    }

    /// Character tokens under the in-body rules.
    fn body_text(&mut self, m: &mut Matcher<'a>, b: &'a [u8], dm: DataMode, s: usize) {
        if dm == DataMode::Normal && b.iter().all(|&c| c == 0) {
            return m.absorb_into_text_gap(s, s + b.len());
        }
        self.reconstruct(m, s);
        self.insert_text(m, b, dm, s);
        if has_content(b) {
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
                return m.absorb_into_text_gap(s, s + b.len());
            }
            return self.insert_text(m, b, dm, s);
        }
        self.foster = true;
        self.body_text(m, b, dm, s);
        self.foster = false;
    }
}

fn is_heading(tag: Tag) -> bool {
    matches!(tag, Tag::H1 | Tag::H2 | Tag::H3 | Tag::H4 | Tag::H5 | Tag::H6)
}
