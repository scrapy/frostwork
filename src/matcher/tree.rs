//! The seam between tree construction and matching.
//!
//! A [`TreeBuilder`] decides what the token stream MEANS as a tree: which elements a start tag closes or
//! synthesizes, which end tags are honoured, where text lands, and which tokenizer state an element's
//! content is read in. [`Matcher`] does the work every mode shares, through four primitives the
//! builders call: [`Matcher::open_element`] (positions, anchors, selector evaluation, captures, group
//! instances, attribute values; also for an element the page never wrote), [`Matcher::close_top`]
//! (the element's raw source, deferred resolution), [`Matcher::buffer_text`] / [`Matcher::flush_text`]
//! (one text node to every consumer) and [`Matcher::absorb_into_text_gap`] (markup that leaves no node).
//!
//! Each [`crate::ParseMode`] has its own builder: `super::libxml2` for lxml mode and `super::browser`
//! for browser mode. A schema is compiled for one mode, so [`super::CompiledSchema::run`] picks the
//! builder once per document and [`Scan`] is monomorphised over it; nothing on the per-token path asks
//! which mode it is in.

use super::{CompiledSchema, EncodedInput, Matcher};
use crate::tokenizer::{DataMode, TokenSink};
use crate::{FlatColumns, GroupRows};

/// One parse mode's tree construction. The tokenizer's events arrive here, and the builder answers them
/// by calling the matcher's primitives; it never evaluates a selector itself.
pub(super) trait TreeBuilder<'a> {
    /// Insert, ignore or reshape for one start tag, and return the state its content is tokenized in.
    fn start_tag(
        &mut self,
        m: &mut Matcher<'a>,
        name: &'a [u8],
        raw_attrs: &[(&'a [u8], Option<&'a [u8]>)],
        self_closing: bool,
        span_start: usize,
        open_end: usize,
    ) -> DataMode;

    /// Honour or discard one end tag.
    fn end_tag(&mut self, m: &mut Matcher<'a>, name: &'a [u8], close_start: usize, close_end: usize);

    /// Place one text run, read in `mode`.
    fn text(&mut self, m: &mut Matcher<'a>, text: &'a [u8], mode: DataMode, start: usize);

    /// Is a `<![CDATA[` section character data at this point? See [`TokenSink::cdata_is_text`].
    fn cdata_is_text(&self, m: &Matcher<'a>) -> bool;

    /// A `<!DOCTYPE …>`: no node, and the text either side of it stays one node.
    fn doctype(&mut self, m: &mut Matcher<'a>, raw: &'a [u8], from: usize, to: usize) {
        let _ = raw;
        m.absorb_into_text_gap(from, to)
    }

    /// A comment node, which ends the text node before it unless the builder puts it elsewhere.
    fn comment(&mut self, m: &mut Matcher<'a>, from: usize, to: usize) {
        let _ = (m, from, to);
    }

    /// End of input, before the matcher closes what is still open.
    fn finish(&mut self, m: &mut Matcher<'a>) {
        let _ = m;
    }

    /// May the scan stop before the next token, which starts at `at`? See [`TokenSink::done`].
    fn stop(&self, m: &Matcher<'a>, at: usize) -> bool {
        let _ = at;
        m.done()
    }
}

/// One document's scan: the matcher's state plus the tree builder of the schema's mode.
pub(super) struct Scan<'a, B> {
    pub(super) m: Matcher<'a>,
    pub(super) builder: B,
}

impl<'a, B: TreeBuilder<'a>> Scan<'a, B> {
    /// Tokenize `input` through `builder` until the end or until the builder stops the scan. `marks` are
    /// the deferred winners a browser-mode tail pass marks (`Matcher::resolve_tail_pass`); every other
    /// run passes none.
    pub(super) fn tokenize(
        schema: &'a CompiledSchema,
        input: EncodedInput<'a>,
        builder: B,
        marks: Vec<(u32, String)>,
    ) -> Self {
        let mut m = Matcher::new(schema, input);
        m.marks = marks;
        let mut scan = Scan { m, builder };
        crate::tokenizer::tokenize(input.bytes(), &mut scan);
        scan
    }

    /// End of input: the builder's, then the matcher's.
    pub(super) fn finish(mut self) -> (FlatColumns, Vec<GroupRows>) {
        self.builder.finish(&mut self.m);
        self.m.finish_grouped()
    }
}

impl<'a, B: TreeBuilder<'a>> TokenSink<'a> for Scan<'a, B> {
    #[inline]
    fn start_tag(
        &mut self,
        name: &'a [u8],
        raw_attrs: &[(&'a [u8], Option<&'a [u8]>)],
        self_closing: bool,
        span_start: usize,
        open_end: usize,
    ) -> DataMode {
        self.builder.start_tag(&mut self.m, name, raw_attrs, self_closing, span_start, open_end)
    }

    #[inline]
    fn end_tag(&mut self, name: &'a [u8], close_start: usize, close_end: usize) {
        self.builder.end_tag(&mut self.m, name, close_start, close_end)
    }

    #[inline]
    fn text(&mut self, text: &'a [u8], mode: DataMode, start: usize) {
        self.builder.text(&mut self.m, text, mode, start)
    }

    /// `</>` and a CDATA section's delimiters leave no node AND do not end the surrounding text node in
    /// either parser, so they are absorbed into the pending node's gap the way a dropped end tag is: the
    /// runs either side of them are ONE node.
    #[inline]
    fn invisible_markup(&mut self, from: usize, to: usize) {
        self.m.absorb_into_text_gap(from, to)
    }

    #[inline]
    fn doctype(&mut self, raw: &'a [u8], from: usize, to: usize) {
        self.builder.doctype(&mut self.m, raw, from, to)
    }

    #[inline]
    fn comment(&mut self, from: usize, to: usize) {
        self.builder.comment(&mut self.m, from, to)
    }

    #[inline]
    fn done(&self, at: usize) -> bool {
        self.builder.stop(&self.m, at)
    }

    fn cdata_is_text(&self) -> bool {
        self.builder.cdata_is_text(&self.m)
    }
}
