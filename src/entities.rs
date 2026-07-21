//! Character-reference decoding that matches libxml2/Parsel. Copied verbatim from
//! `parsel-stream-core/src/entities.rs` to keep this exploration crate standalone while staying
//! byte-identical to Parsel on entities (so entity handling is never a source of spurious
//! divergence in the differential harness). See that file for the rationale.

use std::borrow::Cow;

static LEGACY_ENTITIES: &[(&str, &str)] = &[
    ("AElig", "\u{c6}"),
    ("AMP", "\u{26}"),
    ("Aacute", "\u{c1}"),
    ("Acirc", "\u{c2}"),
    ("Agrave", "\u{c0}"),
    ("Aring", "\u{c5}"),
    ("Atilde", "\u{c3}"),
    ("Auml", "\u{c4}"),
    ("COPY", "\u{a9}"),
    ("Ccedil", "\u{c7}"),
    ("ETH", "\u{d0}"),
    ("Eacute", "\u{c9}"),
    ("Ecirc", "\u{ca}"),
    ("Egrave", "\u{c8}"),
    ("Euml", "\u{cb}"),
    ("GT", "\u{3e}"),
    ("Iacute", "\u{cd}"),
    ("Icirc", "\u{ce}"),
    ("Igrave", "\u{cc}"),
    ("Iuml", "\u{cf}"),
    ("LT", "\u{3c}"),
    ("Ntilde", "\u{d1}"),
    ("Oacute", "\u{d3}"),
    ("Ocirc", "\u{d4}"),
    ("Ograve", "\u{d2}"),
    ("Oslash", "\u{d8}"),
    ("Otilde", "\u{d5}"),
    ("Ouml", "\u{d6}"),
    ("QUOT", "\u{22}"),
    ("REG", "\u{ae}"),
    ("THORN", "\u{de}"),
    ("Uacute", "\u{da}"),
    ("Ucirc", "\u{db}"),
    ("Ugrave", "\u{d9}"),
    ("Uuml", "\u{dc}"),
    ("Yacute", "\u{dd}"),
    ("aacute", "\u{e1}"),
    ("acirc", "\u{e2}"),
    ("acute", "\u{b4}"),
    ("aelig", "\u{e6}"),
    ("agrave", "\u{e0}"),
    ("amp", "\u{26}"),
    ("aring", "\u{e5}"),
    ("atilde", "\u{e3}"),
    ("auml", "\u{e4}"),
    ("brvbar", "\u{a6}"),
    ("ccedil", "\u{e7}"),
    ("cedil", "\u{b8}"),
    ("cent", "\u{a2}"),
    ("copy", "\u{a9}"),
    ("curren", "\u{a4}"),
    ("deg", "\u{b0}"),
    ("divide", "\u{f7}"),
    ("eacute", "\u{e9}"),
    ("ecirc", "\u{ea}"),
    ("egrave", "\u{e8}"),
    ("eth", "\u{f0}"),
    ("euml", "\u{eb}"),
    ("frac12", "\u{bd}"),
    ("frac14", "\u{bc}"),
    ("frac34", "\u{be}"),
    ("gt", "\u{3e}"),
    ("iacute", "\u{ed}"),
    ("icirc", "\u{ee}"),
    ("iexcl", "\u{a1}"),
    ("igrave", "\u{ec}"),
    ("iquest", "\u{bf}"),
    ("iuml", "\u{ef}"),
    ("laquo", "\u{ab}"),
    ("lt", "\u{3c}"),
    ("macr", "\u{af}"),
    ("micro", "\u{b5}"),
    ("middot", "\u{b7}"),
    ("nbsp", "\u{a0}"),
    ("not", "\u{ac}"),
    ("ntilde", "\u{f1}"),
    ("oacute", "\u{f3}"),
    ("ocirc", "\u{f4}"),
    ("ograve", "\u{f2}"),
    ("ordf", "\u{aa}"),
    ("ordm", "\u{ba}"),
    ("oslash", "\u{f8}"),
    ("otilde", "\u{f5}"),
    ("ouml", "\u{f6}"),
    ("para", "\u{b6}"),
    ("plusmn", "\u{b1}"),
    ("pound", "\u{a3}"),
    ("quot", "\u{22}"),
    ("raquo", "\u{bb}"),
    ("reg", "\u{ae}"),
    ("sect", "\u{a7}"),
    ("shy", "\u{ad}"),
    ("sup1", "\u{b9}"),
    ("sup2", "\u{b2}"),
    ("sup3", "\u{b3}"),
    ("szlig", "\u{df}"),
    ("thorn", "\u{fe}"),
    ("times", "\u{d7}"),
    ("uacute", "\u{fa}"),
    ("ucirc", "\u{fb}"),
    ("ugrave", "\u{f9}"),
    ("uml", "\u{a8}"),
    ("uuml", "\u{fc}"),
    ("yacute", "\u{fd}"),
    ("yen", "\u{a5}"),
    ("yuml", "\u{ff}"),
];
const LEGACY_MAX_LEN: usize = 6;

fn legacy_lookup(name: &str) -> Option<&'static str> {
    LEGACY_ENTITIES
        .binary_search_by(|(k, _)| k.cmp(&name))
        .ok()
        .map(|i| LEGACY_ENTITIES[i].1)
}

fn fixup_codepoint(cp: u32) -> char {
    const WIN1252: [u32; 32] = [
        0x20AC, 0x81, 0x201A, 0x192, 0x201E, 0x2026, 0x2020, 0x2021, 0x2C6, 0x2030, 0x160, 0x2039,
        0x152, 0x8D, 0x17D, 0x8F, 0x90, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014,
        0x2DC, 0x2122, 0x161, 0x203A, 0x153, 0x9D, 0x17E, 0x178,
    ];
    let cp = match cp {
        0 => 0xFFFD,
        0x80..=0x9F => WIN1252[(cp - 0x80) as usize],
        0xD800..=0xDFFF => 0xFFFD,
        c if c > 0x10FFFF => 0xFFFD,
        c => c,
    };
    char::from_u32(cp).unwrap_or('\u{FFFD}')
}

fn parse_numeric(rest: &str) -> Option<(String, usize)> {
    let b = rest.as_bytes();
    let mut j = 2;
    let hex = j < b.len() && (b[j] | 0x20) == b'x';
    if hex {
        j += 1;
    }
    let digits_start = j;
    let mut cp: u32 = 0;
    while j < b.len() {
        let d = match b[j] {
            c @ b'0'..=b'9' => (c - b'0') as u32,
            c @ b'a'..=b'f' if hex => (c - b'a' + 10) as u32,
            c @ b'A'..=b'F' if hex => (c - b'A' + 10) as u32,
            _ => break,
        };
        cp = cp.saturating_mul(if hex { 16 } else { 10 }).saturating_add(d);
        j += 1;
    }
    if j == digits_start {
        return None;
    }
    if j < b.len() && b[j] == b';' {
        j += 1;
    }
    Some((fixup_codepoint(cp).to_string(), j))
}

fn parse_ref(rest: &str, in_attribute: bool) -> Option<(Cow<'static, str>, usize)> {
    let b = rest.as_bytes();
    if b.len() < 2 {
        return None;
    }
    if b[1] == b'#' {
        return parse_numeric(rest).map(|(s, n)| (Cow::Owned(s), n));
    }
    let mut j = 1;
    while j < b.len() && b[j].is_ascii_alphanumeric() {
        j += 1;
    }
    if j == 1 {
        return None;
    }
    if j < b.len() && b[j] == b';' {
        let token = &rest[..=j];
        let dec = html_escape::decode_html_entities(token);
        if dec != token {
            return Some((Cow::Owned(dec.into_owned()), j + 1));
        }
    }
    let name = &rest[1..j];
    let maxlen = name.len().min(LEGACY_MAX_LEN);
    for plen in (1..=maxlen).rev() {
        if let Some(rep) = legacy_lookup(&name[..plen]) {
            if in_attribute
                && matches!(b.get(1 + plen), Some(&c) if c == b'=' || c.is_ascii_alphanumeric())
            {
                return None;
            }
            return Some((Cow::Borrowed(rep), 1 + plen));
        }
    }
    None
}

/// Decode character references (and drop raw NUL) the libxml2/Parsel way.
pub fn decode(s: &str, in_attribute: bool) -> Cow<'_, str> {
    if memchr::memchr2(b'&', 0, s.as_bytes()).is_none() {
        return Cow::Borrowed(s);
    }
    let bytes = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            0 => i += 1,
            b'&' => {
                if let Some((rep, len)) = parse_ref(&s[i..], in_attribute) {
                    out.push_str(&rep);
                    i += len;
                } else {
                    out.push('&');
                    i += 1;
                }
            }
            _ => {
                let start = i;
                i += 1;
                while i < bytes.len() && (bytes[i] & 0xC0) == 0x80 {
                    i += 1;
                }
                out.push_str(&s[start..i]);
            }
        }
    }
    Cow::Owned(out)
}
