//! Value decoding: turn the raw byte span of a matched value (text node or attribute) into the final
//! `String`/`Cow` the engine emits. Pure `bytes + encoding -> value` with no dependency on any matcher
//! type. It runs only for values that actually match, a small fraction of the document, so no
//! whole-document decode or UTF-8 validation happens up front.
//!
//! [`EncodedInput`] carries that `bytes + encoding` pair as one value, keeping document-range
//! operations tied to the encoding chosen for the prepared buffer.

use std::borrow::Cow;
use std::ops::Range;

use encoding_rs::Encoding;

use crate::entities;

/// A document's bytes together with the encoding that decodes **those bytes**.
///
/// The engine scans a page in its own encoding instead of converting the whole document to UTF-8 first,
/// so "which encoding" is a property of one particular buffer and has to travel with it.
/// `crate::prepare_bytes` picks the pair: usually the page's own encoding over borrowed input, but a
/// non-ASCII-compatible page (UTF-16, ISO-2022-JP) is transcoded first and paired with UTF-8 instead.
/// Carried as two separate values those two cases can be crossed, and reading a transcoded buffer under
/// the label the page declared garbles every value on it.
///
/// A small `Copy` borrowed view that owns nothing. The prepared `Cow` remains alive in
/// `Plan::extract` for the scan, while elements borrow slices from this view, so it must not own the
/// buffer itself.
///
/// [`sub`](Self::sub) and [`raw_source`](Self::raw_source) always slice this input rather than accept
/// an independently supplied byte buffer. A value that already arrives as a slice (an attribute
/// value, a text run) passes [`encoding`](Self::encoding) to the free functions below.
#[derive(Clone, Copy)]
pub(crate) struct EncodedInput<'a> {
    bytes: &'a [u8],
    encoding: &'static Encoding,
}

impl<'a> EncodedInput<'a> {
    /// Pair a prepared buffer with the encoding that decodes it. Called once per page, by the entry
    /// point that produced both.
    pub(crate) fn new(bytes: &'a [u8], encoding: &'static Encoding) -> Self {
        Self { bytes, encoding }
    }

    /// The whole buffer — for the tokenizer, which reads bytes and never decodes.
    pub(super) fn bytes(self) -> &'a [u8] {
        self.bytes
    }

    pub(super) fn encoding(self) -> &'static Encoding {
        self.encoding
    }

    pub(super) fn len(self) -> usize {
        self.bytes.len()
    }

    /// The same document narrowed to `range`, same encoding: one element's span, re-scanned by a
    /// sub-schema to recover a deferred value.
    pub(super) fn sub(self, range: Range<usize>) -> Self {
        Self { bytes: &self.bytes[range], encoding: self.encoding }
    }

    /// Decode `range` of this document as an outer-HTML value (see the free `raw_source` below).
    pub(super) fn raw_source(self, range: Range<usize>) -> String {
        raw_source(&self.bytes[range], self.encoding)
    }
}

/// Bytes -> `str` under the resolved encoding, with UTF-8 short-circuited. For UTF-8
/// `decode_without_bom_handling` is *defined* to equal `from_utf8_lossy`, so `from_utf8` answers valid
/// input, the overwhelming case, without decoder setup.
///
/// **The short-circuit is for consistency, not a claimed speedup.** Value decoding is ~25% of the work
/// on a schema full of bare-element (outer-HTML) fields, which makes it look like a lever. A/B over the
/// real corpus says otherwise: median −0.5%, **0 of 15 cells above their own jitter**, signs mixed, a
/// null result, because encoding_rs's UTF-8 validation is already as fast as std's. Its value is one
/// definition of "decode" instead of two. Do not treat it as an optimization without a workload that
/// demonstrates one.
pub(super) fn decode_bytes<'a>(bytes: &'a [u8], enc: &'static Encoding) -> Cow<'a, str> {
    if enc == encoding_rs::UTF_8 {
        return match std::str::from_utf8(bytes) {
            Ok(s) => Cow::Borrowed(s),
            Err(_) => Cow::Owned(String::from_utf8_lossy(bytes).into_owned()),
        };
    }
    enc.decode_without_bom_handling(bytes).0
}

/// HTML normalizes newlines in the *input stream* — `\r\n` and lone `\r` become `\n` — **before**
/// entity expansion (so `&#13;` still yields a real `\r`). Shared by text and attribute decoding.
/// Guarded on `\r` so the clean common path stays borrowed / zero-allocation.
fn normalize_crlf(s: Cow<str>) -> Cow<str> {
    if s.as_bytes().contains(&b'\r') {
        Cow::Owned(s.replace("\r\n", "\n").replace('\r', "\n"))
    } else {
        s
    }
}

/// Finalize an emitted value from raw bytes: decode with the resolved encoding, CRLF-normalize,
/// entity-decode. Called ONLY for values that actually match, a small fraction of the document, so no
/// whole-document decode or UTF-8 validation happens up front.
///
/// The NUL filtering below is a backstop, not the mechanism. Raw NUL is deleted from the whole document
/// before tokenizing (`crate::strip_nul`), because dropping it only from emitted values would make the
/// engine and lxml disagree about the document's STRUCTURE. It stays because it costs one
/// `contains(&0)` on a path that already scans the value, and because a decoded U+0000 arriving from
/// somewhere else must not reach a column.
pub(super) fn finalize(bytes: &[u8], allows_entities: bool, enc: &'static Encoding) -> String {
    // `decode` (not `decode_without_bom_handling`) would strip a leading U+FEFF from every value —
    // wrong: the document BOM is already removed up front, and a U+FEFF mid-text is real content
    // that libxml2 preserves. UTF-8: == from_utf8_lossy; else transcode this value only.
    let t = normalize_crlf(decode_bytes(bytes, enc));
    if allows_entities {
        entities::decode(&t, false).into_owned()
    } else if t.as_bytes().contains(&0) {
        t.chars().filter(|&c| c != '\0').collect()
    } else {
        t.into_owned()
    }
}

/// Finalize a captured RAW-SOURCE span (an outer-HTML value): decode, and CRLF-normalize but do NOT
/// entity-decode — `&amp;` stays as written, which is the whole point of raw source.
///
/// The newline normalization is not a compromise of "raw", it is the one part of raw that both oracles
/// also do: HTML normalizes `\r\n` and lone `\r` to `\n` in the INPUT STREAM, before any parsing, so
/// every node libxml2 or html5lib serializes carries `\n` no matter what the bytes said. The engine
/// already normalized text and attribute values ([`finalize`]) and only this path did not, which is an
/// inconsistency rather than a divergence — one CRLF-authored crawled page put `\r\n` in all eight of
/// its node columns. What stays divergent here is RE-SERIALIZATION (attribute order and quoting,
/// minimized booleans, entity escaping), because the engine has no tree to re-serialize from.
pub(super) fn raw_source(bytes: &[u8], enc: &'static Encoding) -> String {
    normalize_crlf(decode_bytes(bytes, enc)).into_owned()
}

/// XPath `normalize-space`: collapse each run of ASCII whitespace (space, tab, CR, LF) to a single
/// space and trim leading/trailing. Used for the `normalize-space(...)` value terminal.
pub(super) fn normalize_space(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut pending_space = false;
    for c in s.chars() {
        if matches!(c, ' ' | '\t' | '\r' | '\n') {
            pending_space = !out.is_empty(); // no leading space; mark an internal gap
        } else {
            if pending_space {
                out.push(' ');
                pending_space = false;
            }
            out.push(c);
        }
    }
    out // trailing whitespace never flushed -> trimmed
}

/// Decode an attribute value from raw bytes: same input-stream newline normalization as text (before
/// entities, mirroring `finalize`), then entity-decode. For UTF-8 (the overwhelming common case) it
/// borrows when the value is clean (zero allocation); other encodings transcode this value only. Only
/// "interesting" attrs reach here, so per-value cost is negligible.
pub(super) fn decode_attr<'a>(av: &'a [u8], enc: &'static Encoding) -> Cow<'a, str> {
    match normalize_crlf(decode_bytes(av, enc)) {
        Cow::Borrowed(s) => entities::decode(s, true), // clean UTF-8, no CR: still zero-copy
        Cow::Owned(s) => Cow::Owned(entities::decode(&s, true).into_owned()),
    }
}
