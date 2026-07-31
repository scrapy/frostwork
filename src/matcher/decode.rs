//! Value decoding: turn the raw byte span of a matched value (text node or attribute) into the final
//! `String`/`Cow` the engine emits. Pure `bytes + encoding -> value` with no dependency on any matcher
//! type — it runs only for values that actually match (a small fraction of the document), so the
//! whole-document decode/validation the old `&str` path paid up front is gone.

use std::borrow::Cow;

use encoding_rs::Encoding;

use crate::entities;

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
/// entity-decode. Called ONLY for values that actually match (a small fraction of the
/// document), so the whole-document decode/validation the old `&str` path paid up front is gone.
///
/// The NUL filtering below is now a backstop, not the mechanism: raw NUL is deleted from the whole
/// document before tokenizing (`crate::strip_nul`), because dropping it only from emitted values made
/// the engine and lxml disagree about the document's STRUCTURE. It stays because it costs one
/// `contains(&0)` on a path that already scans the value, and because a decoded U+0000 arriving from
/// somewhere else must not reach a column.
pub(super) fn finalize(bytes: &[u8], allows_entities: bool, enc: &'static Encoding) -> String {
    // `decode` (not `decode_without_bom_handling`) would strip a leading U+FEFF from every value —
    // wrong: the document BOM is already removed up front, and a U+FEFF mid-text is real content
    // that libxml2 preserves. UTF-8: == from_utf8_lossy; else transcode this value only.
    let t = normalize_crlf(enc.decode_without_bom_handling(bytes).0);
    if allows_entities {
        entities::decode(&t, false).into_owned()
    } else if t.as_bytes().contains(&0) {
        t.chars().filter(|&c| c != '\0').collect()
    } else {
        t.into_owned()
    }
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
    let decoded: Cow<'a, str> = if enc == encoding_rs::UTF_8 {
        match std::str::from_utf8(av) {
            Ok(s) => Cow::Borrowed(s),
            Err(_) => Cow::Owned(String::from_utf8_lossy(av).into_owned()),
        }
    } else {
        Cow::Owned(enc.decode_without_bom_handling(av).0.into_owned())
    };
    match normalize_crlf(decoded) {
        Cow::Borrowed(s) => entities::decode(s, true), // clean UTF-8, no CR: still zero-copy
        Cow::Owned(s) => Cow::Owned(entities::decode(&s, true).into_owned()),
    }
}
