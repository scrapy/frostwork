//! Encoding resolution: BOM -> explicit override (HTTP/caller) -> `<meta>` charset prescan ->
//! UTF-8 default. Ported from `parsel-stream-core/src/encoding.rs` (proven to match Parsel).
//!
//! We never transcode the whole document for ASCII-compatible encodings — the tokenizer runs on raw
//! bytes, and the matcher decodes only emitted *values* with the resolved encoding. That works because
//! in an ASCII-compatible encoding a byte below 0x80 always IS that ASCII character, so every HTML
//! structural delimiter is unambiguous. Where it does not hold, the caller transcodes to UTF-8 up
//! front (see `lib.rs`) — and which encodings those are is `Encoding::is_ascii_compatible`'s answer,
//! not a list written here. Naming the family was the bug: "the UTF-16 family" omitted ISO-2022-JP,
//! whose `ESC $ B` mode packs `社` into the two bytes `<R`, so a crawled page grew a start tag out of
//! the middle of a Japanese word.

use encoding_rs::Encoding;

/// Tokenize one `<meta …>` tag's attributes, bounded, starting just past `<meta`. Returns
/// `(attributes, offset just past the tag)`. Names are lowercased; values are returned as-is.
///
/// A substring scan cannot do this job and got it wrong in BOTH directions: `data-http-equiv` looked
/// like `http-equiv`, a `charset=` inside any attribute value looked like a declaration, and the first
/// raw `>` "ended" the tag even inside a quoted value.
fn meta_attrs(head: &[u8], from: usize) -> (Vec<(String, String)>, usize) {
    const WS: [u8; 5] = [b' ', b'\t', b'\n', b'\r', 0x0c];
    let n = head.len();
    let mut i = from;
    let mut attrs = Vec::new();
    loop {
        while i < n && (WS.contains(&head[i]) || head[i] == b'/') {
            i += 1;
        }
        if i >= n || head[i] == b'>' {
            return (attrs, (i + 1).min(n));
        }
        let ns = i;
        while i < n && !WS.contains(&head[i]) && !matches!(head[i], b'=' | b'>' | b'/') {
            i += 1;
        }
        let name = String::from_utf8_lossy(&head[ns..i]).to_ascii_lowercase();
        // whitespace is allowed on BOTH sides of `=` (`charset\n=\nwindows-1252` is a real declaration)
        while i < n && WS.contains(&head[i]) {
            i += 1;
        }
        if i >= n || head[i] != b'=' {
            if !name.is_empty() {
                attrs.push((name, String::new())); // valueless attribute
            }
            continue;
        }
        i += 1;
        while i < n && WS.contains(&head[i]) {
            i += 1;
        }
        let value = if i < n && (head[i] == b'"' || head[i] == b'\'') {
            let q = head[i];
            i += 1;
            let vs = i;
            while i < n && head[i] != q {
                i += 1;
            }
            let v = String::from_utf8_lossy(&head[vs..i]).to_string();
            i = (i + 1).min(n);
            v
        } else {
            let vs = i;
            while i < n && !WS.contains(&head[i]) && head[i] != b'>' {
                i += 1;
            }
            String::from_utf8_lossy(&head[vs..i]).to_string()
        };
        if !name.is_empty() {
            attrs.push((name, value));
        }
    }
}

/// The `charset=` parameter of a `content="text/html; charset=…"` value.
fn charset_param(content: &str) -> Option<String> {
    let lower = content.to_ascii_lowercase();
    let at = lower.find("charset")?;
    let rest = content[at + "charset".len()..].trim_start();
    let rest = rest.strip_prefix('=')?.trim_start();
    let end = rest
        .find(|c: char| c == ';' || c == '"' || c == '\'' || c.is_ascii_whitespace())
        .unwrap_or(rest.len());
    Some(rest[..end].to_string())
}

/// Turn a label read out of the DOCUMENT (a `<meta>` or an XML declaration) into an encoding, applying
/// the two WHATWG corrections that exist because the prescan could only READ the declaration by treating
/// the bytes as ASCII-compatible:
///
/// * **`utf-16`/`utf-16le`/`utf-16be` -> UTF-8.** The declaration contradicts itself; a real UTF-16
///   document is caught by the BOM (or the BOM-less XML prefix) long before the prescan runs.
/// * **`x-user-defined` -> windows-1252** ("get an encoding from a meta element", step 5). The label is a
///   legacy hack for byte-preserving XHR, and treating it literally maps every high byte into the private
///   use area — `caf\xe9` came out as `caf\u{f7e9}` instead of `café`.
///
/// Both are deliberate differences from w3lib, which honours the label as written; see the encoding
/// section of docs/COMPATIBILITY.md.
fn prescan_label(label: &str) -> Option<&'static Encoding> {
    let enc = Encoding::for_label(label.trim().as_bytes())?;
    if enc == encoding_rs::UTF_16LE || enc == encoding_rs::UTF_16BE {
        return Some(encoding_rs::UTF_8);
    }
    if enc == encoding_rs::X_USER_DEFINED {
        return Some(encoding_rs::WINDOWS_1252);
    }
    Some(enc)
}

/// The `encoding="…"` pseudo-attribute of an XML declaration, which is only legal at offset 0.
///
/// Browsers honour this for compatibility with XHTML-era pages served as `text/html`, and so does w3lib
/// (its body-encoding regex has an `<?xml … encoding=…>` alternative), so a page whose only declaration
/// is an XML one decoded as mojibake here. It is read in the same left-to-right prescan as `<meta>`, and
/// since it can only appear first, an XML declaration wins over a later `<meta>` — matching w3lib.
///
/// libxml2's HTML parser is NOT the oracle for this one: it ignores the declaration and then recovers
/// invalid UTF-8 byte-wise, which happens to render legacy text but is not an encoding decision.
fn xml_decl_encoding(head: &[u8]) -> Option<&'static Encoding> {
    if !head.starts_with(b"<?xml") || !matches!(head.get(5), Some(&c) if is_ws(c)) {
        return None;
    }
    let end = memchr::memmem::find(head, b"?>").map(|i| i + 2).unwrap_or(head.len());
    // the pseudo-attributes have the same syntax as a tag's, so the same bounded tokenizer reads them
    let (attrs, _) = meta_attrs(&head[..end], b"<?xml".len());
    let label = attrs.iter().find(|(n, _)| n == "encoding").map(|(_, v)| v.clone())?;
    prescan_label(&label)
}

fn is_ws(c: u8) -> bool {
    matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0c)
}

/// WHATWG's prescan budget, and the measured floor below which a browser honours a declaration
/// wherever it sits — including inside the body. See [`meta_prescan`] for the measurement.
const PRESCAN_FLOOR: usize = 1024;

/// Elements that may appear in `<head>`. A start tag outside this set is the one that ENDS the head
/// and starts the body, which is where a browser stops re-decoding (see [`meta_prescan`]).
fn is_head_element(name: &[u8]) -> bool {
    // `head` and `html` are here because they nest the head rather than end it.
    const HEAD: [&[u8]; 10] = [
        b"html", b"head", b"title", b"base", b"link", b"meta", b"style", b"script", b"noscript",
        b"template",
    ];
    HEAD.iter().any(|h| h.eq_ignore_ascii_case(name))
}

/// Elements whose CONTENT is text, not markup. A `<div>` inside `<script>` is a string, and to a
/// browser it is never a tag — so it neither ends the head nor declares anything. Skipping them is
/// not optional: an inline `<script>` in the head holding an HTML string (`document.write('<div>')`,
/// a JSON-LD blob) is ordinary, and reading its text as tags ends the head early and throws away the
/// page's real declaration.
fn is_raw_text(name: &[u8]) -> bool {
    const RAW: [&[u8]; 4] = [b"script", b"style", b"title", b"textarea"];
    RAW.iter().any(|r| r.eq_ignore_ascii_case(name))
}

/// Position just past `</name>`, or the end of the document if it never closes.
fn skip_raw_text(head: &[u8], lt: usize, name: &[u8]) -> usize {
    let mut i = lt + 1 + name.len();
    while let Some(rel) = memchr::memmem::find(&head[i..], b"</") {
        let close = i + rel;
        match head.get(close + 2..close + 2 + name.len()) {
            Some(n) if n.eq_ignore_ascii_case(name) => {
                return memchr::memchr(b'>', &head[close..])
                    .map(|g| close + g + 1)
                    .unwrap_or(head.len());
            }
            Some(_) => i = close + 2,
            None => return head.len(),
        }
    }
    head.len()
}

/// The tag name starting at `lt` (the `<`), if this is a START tag. `None` for `</x>`, `<!x`, `<?x`.
fn start_tag_name(head: &[u8], lt: usize) -> Option<&[u8]> {
    let first = *head.get(lt + 1)?;
    if !first.is_ascii_alphabetic() {
        return None;
    }
    let mut e = lt + 1;
    while e < head.len() && !is_ws(head[e]) && !matches!(head[e], b'>' | b'/') {
        e += 1;
    }
    Some(&head[lt + 1..e])
}

/// Simplified HTML5 `<meta>` charset prescan, over the WHOLE document. The `charset` token is only
/// honored INSIDE a `<meta …>` tag — WHATWG's prescan (and w3lib, which Scrapy uses) require
/// attribute context, so a `<!-- saved from url … charset=windows-1252 -->` banner or `charset=big5`
/// in early visible text must NOT switch the decode.
///
/// Comment boundaries come from the TOKENIZER's own `scan_comment`, not a second copy here. A local
/// `-->`-only search missed the abrupt closes libxml2 honours (`<!-->`, `<!--->`, and `--!>`), so a
/// perfectly live `<meta charset>` after one of them looked like it was still commented out and the
/// declaration was ignored. One implementation, differential-proven, used by both.
///
/// ONE left-to-right pass, allocation-free, skipping comments on the way past them.
///
/// Both properties are load-bearing. Lowercasing the document to search it would be an allocation and
/// a memcpy the size of the page on every label-less parse; and finding each `<meta` first, then
/// searching BACKWARDS for an enclosing comment, is `O(document)` per hit — fine inside a small
/// window, quadratic over a megabyte of `<meta>`s. `<` is the only byte that can start either token,
/// and `memchr` over it is one SIMD pass.
///
/// # How far it scans
///
/// **The first [`PRESCAN_FLOOR`] bytes unconditionally, and past that only while still in the
/// `<head>`.** Both halves are measured in Chrome rather than read off the standard, because the two
/// disagree — the browser is the standard here (see the encoding rule in AGENTS.md):
///
/// * a declaration in the **head** is honoured at 1 KB, 4 KB, 16 KB, 64 KB, 256 KB and **1 MB** —
///   the prescan's byte budget is not a correctness cap, because a browser that meets the `<meta>`
///   later runs "change the encoding" and re-decodes what it already has.
/// * a declaration in the **body** is honoured at byte 0, 100 and 512 and IGNORED from 1024 on —
///   once real content is parsed the browser will not re-decode, so past the floor the head is the
///   boundary.
///
/// A flat 4096-byte window (w3lib's number, and what this used to be) is wrong in both directions:
/// it drops the head declarations real pages carry behind a producer comment or a block of `og:`
/// metas, and it honours body declarations no browser does. It also cost a measured ~35µs on every
/// label-less page — one whole extra pass over the document — which the head bound gives back.
fn meta_prescan(head: &[u8]) -> Option<&'static Encoding> {
    if let Some(enc) = xml_decl_encoding(head) {
        return enc.into();
    }
    let mut mfrom = 0usize;
    // Raised to `max(PRESCAN_FLOOR, here)` the moment a tag ends the head; `MAX` while still inside it.
    let mut limit = usize::MAX;
    while let Some(rel) = memchr::memchr(b'<', &head[mfrom..]) {
        let hit = mfrom + rel;
        if hit >= limit {
            return None;
        }
        // The head ends at `</head>` or at the first start tag that may not appear in it — and with
        // it ends the browser's willingness to re-decode, once past the floor.
        if let Some(name) = start_tag_name(head, hit) {
            if is_raw_text(name) {
                mfrom = skip_raw_text(head, hit, name).max(hit + 1);
                continue;
            }
            if !is_head_element(name) {
                limit = limit.min(PRESCAN_FLOOR.max(hit));
            }
        } else if head[hit..].len() >= 7 && head[hit..hit + 7].eq_ignore_ascii_case(b"</head>") {
            limit = limit.min(PRESCAN_FLOOR.max(hit));
        }
        // a COMMENT declares nothing: `<!-- <meta charset=big5> -->` must not switch the decode.
        // `scan_comment` owns the abrupt-close shapes (`<!-->`, `--!>`); `.max` guarantees progress.
        if head[hit..].starts_with(b"<!--") {
            mfrom = crate::tokenizer::scan_comment(head, hit).max(hit + 1);
            continue;
        }
        match head.get(hit + 1..hit + 5) {
            Some(name) if name.eq_ignore_ascii_case(b"meta") => {}
            // no 4 bytes left to compare: no later `<` can match either
            None => return None,
            Some(_) => {
                mfrom = hit + 1;
                continue;
            }
        }
        let tag_start = hit + b"<meta".len();
        // require a tag-name terminator so `<metadata …>` is not a meta tag
        if !matches!(head.get(tag_start), Some(b' ' | b'\t' | b'\n' | b'\r' | 0x0c | b'/' | b'>')) {
            mfrom = tag_start;
            continue;
        }
        let (attrs, after) = meta_attrs(head, tag_start);
        let get = |k: &str| attrs.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
        // Exactly TWO declaration forms (WHATWG "get an encoding from a meta element"):
        //   1. a `charset` ATTRIBUTE                        -> `<meta charset=utf-8>`
        //   2. `http-equiv=content-type` + `content`'s `charset` parameter
        // Anything else declares nothing: `data-http-equiv`, `http-equiv=refresh`, or a `charset=`
        // sitting inside some unrelated attribute's value.
        let label = get("charset").filter(|v| !v.is_empty()).or_else(|| {
            let he = get("http-equiv")?;
            if !he.trim().eq_ignore_ascii_case("content-type") {
                return None;
            }
            charset_param(&get("content")?)
        });
        // An UNKNOWN label declares nothing and the scan CONTINUES to a later declaration (WHATWG
        // prescan: "if it is failure, continue"). w3lib stops at the first regex hit instead, so a page
        // with a typo'd charset followed by a real one is mojibake there — see docs/COMPATIBILITY.md.
        if let Some(enc) = label.and_then(|l| prescan_label(&l)) {
            return Some(enc);
        }
        mfrom = after.max(tag_start + 1);
    }
    None
}

/// BOM → BOM-less UTF-16 XML prefix → caller/HTTP label → `<meta>`/XML-declaration prescan (the whole
/// document) → UTF-8. The intentional differences from w3lib (Scrapy's decoder) are in the encoding
/// section of docs/COMPATIBILITY.md and gated in `tools/enc_check.py`; each one is a place where w3lib
/// and browsers disagree and Frostwork follows the browser.
///
/// No UTF-32: the WHATWG Encoding Standard has no UTF-32, and neither does any browser, so a UTF-32 BOM
/// is not a BOM here. w3lib does recognize it (its BOM table predates the standard).
pub fn resolve(html: &[u8], override_label: Option<&str>) -> &'static Encoding {
    if html.starts_with(&[0xEF, 0xBB, 0xBF]) {
        return encoding_rs::UTF_8;
    }
    if html.starts_with(&[0xFF, 0xFE]) {
        return encoding_rs::UTF_16LE;
    }
    if html.starts_with(&[0xFE, 0xFF]) {
        return encoding_rs::UTF_16BE;
    }
    // A UTF-16 document with NO BOM, detected the way XML 1.0 Appendix F (and libxml2's own
    // `xmlDetectEncoding`) does it: `<?` encoded as UTF-16 is `3C 00 3F 00` / `00 3C 00 3F`. This sits with
    // the BOM checks rather than after the label, on the same reasoning WHATWG gives a BOM priority — the
    // bytes are unambiguous, and no ASCII-compatible document can begin with a NUL. Without it the whole
    // document decoded as mojibake, and libxml2 (the value oracle) reads these files correctly.
    if html.starts_with(&[0x3C, 0x00, 0x3F, 0x00]) {
        return encoding_rs::UTF_16LE;
    }
    if html.starts_with(&[0x00, 0x3C, 0x00, 0x3F]) {
        return encoding_rs::UTF_16BE;
    }
    if let Some(label) = override_label {
        if let Some(enc) = Encoding::for_label(label.as_bytes()) {
            return enc;
        }
    }
    // The prescan is NOT bounded by a byte window, because a browser's is not. WHATWG's 1024 is a
    // STREAMING budget — user agents are "encouraged to use the prescan algorithm ... on the first 1024
    // bytes, but not to stall beyond that", i.e. do not block first paint waiting for more bytes off the
    // network — and a browser that meets a `<meta charset>` after it simply runs "change the encoding"
    // and re-decodes what it already has. Measured rather than assumed: Chrome honours a declaration at
    // 1 KB, 4 KB, 16 KB, 64 KB, 256 KB and 1 MB alike, so there is no bound to match. Frostwork holds the
    // whole document and has nothing to stall on either.
    //
    // A window is therefore a divergence from the browser, and this subsystem has no budget for one (see
    // the encoding rule in AGENTS.md). The old 4096 was w3lib's number, which cost real pages: a legacy
    // site that opens with a producer comment or a block of `og:` metas puts its `Content-Type` past it
    // and the page decoded as UTF-8 with a U+FFFD in every value.
    //
    // Deliberately NOT stopped at `<body>` (w3lib's regex does), because a real page can carry a late
    // `<meta charset>` inside the body and browsers still honour it.
    if let Some(enc) = meta_prescan(html) {
        return enc;
    }
    encoding_rs::UTF_8
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prescan_requires_meta_tag_context() {
        // a real <meta charset> is honored
        assert_eq!(meta_prescan(b"<meta charset=windows-1252>"), Some(encoding_rs::WINDOWS_1252));
        assert_eq!(
            meta_prescan(b"<meta http-equiv=\"Content-Type\" content=\"text/html; charset=big5\">"),
            Some(encoding_rs::BIG5)
        );
        // `charset=` OUTSIDE a meta tag must be ignored (T7): comments and visible text
        assert_eq!(meta_prescan(b"<!-- saved from url charset=big5 -->"), None);
        assert_eq!(meta_prescan(b"<p>pricing in charset=windows-1252 fonts</p>"), None);
        // `<metadata charset=...>` is not a `<meta>` tag
        assert_eq!(meta_prescan(b"<metadata charset=big5>"), None);
        // a decoy comment before a real meta still resolves to the real one
        assert_eq!(
            meta_prescan(b"<!-- charset=big5 --><meta charset=shift_jis>"),
            Some(encoding_rs::SHIFT_JIS)
        );
    }

    #[test]
    fn resolve_ignores_charset_in_comment() {
        // the T7 repro: comment banner must NOT switch the decode away from UTF-8
        let html = b"<!-- charset=big5 --><html><head></head><body><p>caf\xc3\xa9</p></body></html>";
        assert_eq!(resolve(html, None), encoding_rs::UTF_8);
    }
}

/// Prescan vectors oracled against **w3lib.encoding.html_to_unicode** — what Scrapy actually uses to
/// pick a response encoding. The earlier fix used Parsel as the oracle, whose UTF-8 default agreed by
/// accident on several of these, so the gate reported parity while the prescan was both over- and
/// under-triggering. Each expectation below was read off w3lib directly.
#[cfg(test)]
mod w3lib_oracle_tests {
    use super::*;

    fn scan(head: &[u8]) -> Option<&'static Encoding> {
        meta_prescan(head)
    }

    /// FALSE POSITIVES: shapes that declare nothing, but a substring search for `http-equiv`/`charset=`
    /// treated as a declaration — switching a UTF-8 page to Big5 and mojibaking every value.
    #[test]
    fn declares_nothing_must_not_switch_encoding() {
        // `data-http-equiv` is not `http-equiv`
        assert_eq!(scan(br#"<meta data-http-equiv="content-type" content="text/html; charset=big5">"#), None);
        // http-equiv must be content-type; a refresh URL carrying `charset=` is not a declaration
        assert_eq!(scan(br#"<meta http-equiv="refresh" content="0; url=/x?charset=big5">"#), None);
        // `charset=` inside an unrelated attribute is not a declaration
        assert_eq!(
            scan(br#"<meta http-equiv="content-type" content="text/html" data-note="charset=big5">"#),
            None
        );
    }

    /// FALSE NEGATIVES: valid declarations the scan missed, leaving the page on the UTF-8 default and
    /// producing replacement characters for real windows-1252 bytes.
    #[test]
    fn valid_declarations_must_be_found() {
        // a quoted `>` inside an attribute value does NOT end the tag
        assert_eq!(
            scan(br#"<meta http-equiv="content-type" content="text/html; charset=windows-1252" title="a>b">"#),
            Some(encoding_rs::WINDOWS_1252)
        );
        // whitespace (incl. newlines) is allowed around `=`
        assert_eq!(scan(b"<meta charset\n=\nwindows-1252>"), Some(encoding_rs::WINDOWS_1252));
        // an abrupt comment close (`<!-->`) ends the comment, so the following meta IS live
        assert_eq!(scan(b"<!--><meta charset=windows-1252>"), Some(encoding_rs::WINDOWS_1252));
        // ...as does `--!>`
        assert_eq!(scan(b"<!--x--!><meta charset=windows-1252>"), Some(encoding_rs::WINDOWS_1252));
    }

    /// The behaviour already established must not regress.
    #[test]
    fn established_prescan_behaviour_holds() {
        assert_eq!(scan(b"<!-- <meta charset=big5> -->"), None); // inside a comment
        assert_eq!(scan(br#"<meta content="text/html; charset=big5">"#), None); // no http-equiv
        assert_eq!(scan(b"<meta charset=utf-16>"), Some(encoding_rs::UTF_8)); // UTF-16 decl -> UTF-8
        assert_eq!(scan(b"<metadata charset=big5>"), None); // not a <meta> tag
    }

    /// Browser behaviour w3lib also implements, and the prescan did not.
    #[test]
    fn xml_declaration_is_a_charset_declaration() {
        assert_eq!(
            scan(br#"<?xml version="1.0" encoding="ISO-8859-1"?><html>"#),
            Some(encoding_rs::WINDOWS_1252) // WHATWG maps the iso-8859-1 LABEL to windows-1252
        );
        assert_eq!(scan(b"<?xml version='1.0' encoding='big5'?>"), Some(encoding_rs::BIG5));
        // read in document order, so a declaration that comes FIRST wins over a later `<meta>`
        assert_eq!(
            scan(br#"<?xml version="1.0" encoding="big5"?><meta charset="utf-8">"#),
            Some(encoding_rs::BIG5)
        );
        // ...and one that declares nothing usable falls through to the `<meta>` scan
        assert_eq!(
            scan(br#"<?xml version="1.0"?><meta charset="big5">"#),
            Some(encoding_rs::BIG5)
        );
        assert_eq!(
            scan(br#"<?xml version="1.0" encoding="not-a-charset"?><meta charset="big5">"#),
            Some(encoding_rs::BIG5)
        );
        // a processing instruction that is not an XML declaration declares nothing
        assert_eq!(scan(br#"<?xml-stylesheet encoding="big5"?>"#), None);
        assert_eq!(scan(br#"<?php $encoding="big5"; ?>"#), None);
    }

    /// A `<meta charset=x-user-defined>` means windows-1252 ("get an encoding from a meta element"), not
    /// the byte-preserving private-use mapping the label names.
    #[test]
    fn meta_x_user_defined_means_windows_1252() {
        assert_eq!(scan(b"<meta charset=x-user-defined>"), Some(encoding_rs::WINDOWS_1252));
        assert_eq!(scan(b"<?xml version='1.0' encoding='x-user-defined'?>"),
                   Some(encoding_rs::WINDOWS_1252));
        // an explicit HTTP/caller label is NOT a meta declaration, so it keeps its literal meaning
        assert_eq!(resolve(b"<p>x</p>", Some("x-user-defined")), encoding_rs::X_USER_DEFINED);
    }

    /// An unknown label declares nothing and the scan CONTINUES (w3lib stops at its first regex hit).
    #[test]
    fn an_invalid_charset_does_not_end_the_prescan() {
        assert_eq!(
            scan(b"<meta charset=not-a-charset><meta charset=big5>"),
            Some(encoding_rs::BIG5)
        );
        assert_eq!(scan(b"<meta charset=><meta charset=shift_jis>"), Some(encoding_rs::SHIFT_JIS));
    }

    /// UTF-16 with NO BOM, detected from the XML declaration's byte pattern (XML 1.0 Appendix F, and
    /// what libxml2 itself does). Without this the whole document decoded as mojibake.
    #[test]
    fn bomless_utf16_is_detected_from_the_xml_prefix() {
        let le: Vec<u8> = r#"<?xml version="1.0"?><p>x</p>"#.encode_utf16()
            .flat_map(|u| u.to_le_bytes())
            .collect();
        let be: Vec<u8> = r#"<?xml version="1.0"?><p>x</p>"#.encode_utf16()
            .flat_map(|u| u.to_be_bytes())
            .collect();
        assert_eq!(resolve(&le, None), encoding_rs::UTF_16LE);
        assert_eq!(resolve(&be, None), encoding_rs::UTF_16BE);
        // bytes-don't-lie: the sniff sits with the BOM checks, so it outranks a wrong caller label
        assert_eq!(resolve(&le, Some("windows-1252")), encoding_rs::UTF_16LE);
        // an ordinary ASCII-compatible document is untouched by it
        assert_eq!(resolve(br#"<?xml version="1.0"?><p>x</p>"#, None), encoding_rs::UTF_8);
    }

    /// The `iso-8859-1` LABEL means windows-1252, wherever it arrives from — including an explicit
    /// HTTP/caller charset, which skips the prescan entirely.
    ///
    /// This is the WHATWG label table, it is what browsers do, and it is also what Scrapy does:
    /// `w3lib.encoding.resolve_encoding("iso-8859-1")` is `cp1252`. Only a *raw* Parsel
    /// `Selector(body=…, encoding="iso-8859-1")` — which bypasses w3lib — applies Python's literal
    /// latin-1 codec and leaves the C1 range as controls. A real page in the crawl sample
    /// (`charset=iso-8859-1` in its HTTP header, an en dash written as the single byte 0x96) is
    /// readable text here and in Scrapy, and 7 mojibake values under raw Parsel.
    #[test]
    fn the_iso_8859_1_label_is_windows_1252() {
        assert_eq!(resolve(b"<p>x</p>", Some("iso-8859-1")), encoding_rs::WINDOWS_1252);
        assert_eq!(resolve(b"<p>x</p>", Some("latin1")), encoding_rs::WINDOWS_1252);
        assert_eq!(resolve(b"<p>x</p>", Some("ISO_8859-1:1987")), encoding_rs::WINDOWS_1252);
        // so the C1 bytes are the printable windows-1252 characters, not controls:
        // 0x96 is an en dash, not U+0096
        let (text, _, _) = resolve(b"", Some("iso-8859-1")).decode(b"Milano\x96Malpensa");
        assert_eq!(text, "Milano\u{2013}Malpensa");
    }

    /// How far a declaration is still a declaration — both halves measured in Chrome, because the
    /// browser is the standard for encoding and it does not match WHATWG's suggested budget.
    ///
    /// In the HEAD: honoured at any depth. Found on real pages, three in one 1000-page Common Crawl
    /// sample — a legacy site opens with a producer comment or a block of `og:`/`keywords` metas and
    /// the `Content-Type` lands at byte 1080/1532/1611. The engine read 1024, missed it, and decoded a
    /// whole windows-1252 page as UTF-8, which is what 57 of that sample's divergences were.
    ///
    /// In the BODY: honoured only within the first 1024 bytes. Past that the browser has committed to
    /// content it will not re-decode, and neither do we.
    #[test]
    fn a_head_declaration_is_honoured_at_any_depth() {
        let pad = |n: usize| -> Vec<u8> {
            let mut v = b"<html><head><!--".to_vec();
            v.resize(v.len() + n, b'x');
            v.extend_from_slice(b"-->");
            v
        };
        let decl = br#"<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">"#;

        // the shape the crawl found: filler, then the declaration past WHATWG's streaming budget
        let mut late = pad(1100);
        late.extend_from_slice(decl);
        assert!(late.iter().position(|&b| b == b'C').unwrap() > 1024);
        assert_eq!(resolve(&late, None), encoding_rs::WINDOWS_1252);

        // ...and past every depth a bounded window would have cut off. Each was measured in Chrome.
        for depth in [3900usize, 4200, 16 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024] {
            let mut deep = pad(depth);
            deep.extend_from_slice(decl);
            assert_eq!(
                resolve(&deep, None),
                encoding_rs::WINDOWS_1252,
                "a HEAD declaration at depth {depth} must still be a declaration"
            );
        }

        // an explicit caller/HTTP label still outranks any prescan, near or far
        assert_eq!(resolve(&late, Some("big5")), encoding_rs::BIG5);
    }

    /// An inline `<script>`/`<style>` in the head holds TEXT, not markup: the `<div>` in
    /// `document.write('<div>')` is a string, so it neither ends the head nor declares anything.
    /// Reading raw-text content as tags ended the head at the first HTML string in an analytics
    /// snippet and threw away the page's real declaration.
    #[test]
    fn raw_text_content_is_not_markup() {
        let decl = br#"<meta charset="windows-1252">"#;
        let pad = b"<!--".to_vec();
        let far = |inner: &[u8]| -> Vec<u8> {
            let mut v = b"<html><head>".to_vec();
            v.extend_from_slice(inner);
            v.extend_from_slice(&pad);
            v.resize(v.len() + 1100, b'x');
            v.extend_from_slice(b"-->");
            v.extend_from_slice(decl);
            v
        };
        // each of these would have "started the body" if its content were read as markup
        for inner in [
            &b"<script>document.write('<div class=\"x\">hi</div>');</script>"[..],
            &b"<style>/* <p> */ .a{color:red}</style>"[..],
            &b"<title>A <b>bold</b> title</title>"[..],
            &b"<script>var s = '</scr' + 'ipt><p>';</script>"[..],
        ] {
            assert_eq!(
                resolve(&far(inner), None),
                encoding_rs::WINDOWS_1252,
                "raw-text content must not end the head: {:?}",
                std::str::from_utf8(inner).unwrap()
            );
        }
        // an unclosed <script> swallows the rest of the document, declaration included — which is
        // what a browser does too
        let mut unclosed = b"<html><head><script>x".to_vec();
        unclosed.extend_from_slice(decl);
        assert_eq!(resolve(&unclosed, None), encoding_rs::UTF_8);
    }

    /// The other half: once the BODY has started, a declaration past the 1024-byte floor is ignored,
    /// because a browser will not re-decode content it has already parsed. Measured in Chrome at body
    /// offsets 0, 100, 512 (honoured) and 1024, 2048, 4096, 64K, 512K (ignored).
    #[test]
    fn a_body_declaration_counts_only_within_the_floor() {
        let page = |pad: usize| -> Vec<u8> {
            let mut v = b"<!DOCTYPE html><html><head><title>t</title></head><body>".to_vec();
            while v.len() < pad {
                v.extend_from_slice(b"<p>lorem ipsum dolor sit amet consectetur</p>");
            }
            v.extend_from_slice(br#"<meta charset="windows-1252">"#);
            v
        };
        for near in [0usize, 100, 512] {
            assert_eq!(
                resolve(&page(near), None),
                encoding_rs::WINDOWS_1252,
                "a BODY declaration at {near} is inside the floor and is honoured"
            );
        }
        for far in [1024usize, 2048, 4096, 64 * 1024] {
            assert_eq!(
                resolve(&page(far), None),
                encoding_rs::UTF_8,
                "a BODY declaration at {far} is past the floor and declares nothing"
            );
        }
    }

    /// The unbounded single-pass scan must still match the tag NAME in any case, reject a
    /// look-alike, survive a truncated tail, and skip an arbitrary number of comments on the way.
    #[test]
    fn the_unbounded_scan_matches_only_real_meta_tags() {
        assert_eq!(resolve(b"<p><META charset=big5>", None), encoding_rs::BIG5);
        assert_eq!(resolve(b"<p><MeTa charset=big5>", None), encoding_rs::BIG5);
        assert_eq!(resolve(b"<metadata charset=big5>", None), encoding_rs::UTF_8);
        assert_eq!(resolve(b"<p><div><met", None), encoding_rs::UTF_8); // truncated tail
        assert_eq!(
            resolve(b"<html><head><title>x</title><meta charset=big5>", None),
            encoding_rs::BIG5
        );
        // Many comments, each holding a decoy: none declares anything, and the real one after them is
        // still reached because all of it is still the HEAD. This is the shape that was O(document)
        // per hit before the rewrite — 500 backwards `rfind`s over a growing buffer.
        let mut many = b"<html><head>".to_vec();
        for _ in 0..500 {
            many.extend_from_slice(b"<!-- <meta charset=shift_jis> --><link rel=x>");
        }
        many.extend_from_slice(b"<meta charset=big5>");
        assert_eq!(resolve(&many, None), encoding_rs::BIG5);

        // ...and the same shape with BODY content in it stops at the floor instead, because the
        // first `<p>` ends the head.
        let mut body = b"<html><head>".to_vec();
        for _ in 0..500 {
            body.extend_from_slice(b"<!-- <meta charset=shift_jis> --><p>x</p>");
        }
        body.extend_from_slice(b"<meta charset=big5>");
        assert_eq!(resolve(&body, None), encoding_rs::UTF_8);
    }

    /// The WHATWG Encoding Standard has no UTF-32, so a UTF-32 BOM is not a BOM. w3lib recognizes one.
    #[test]
    fn utf32_boms_are_not_recognized() {
        // UTF-32LE begins FF FE 00 00 — the first two bytes ARE the UTF-16LE BOM, and per the standard
        // that is what it means, so such a document is read as UTF-16LE (browsers do the same).
        let le32: Vec<u8> = [0xFFu8, 0xFE, 0x00, 0x00].into_iter().collect();
        assert_eq!(resolve(&le32, None), encoding_rs::UTF_16LE);
        // UTF-32BE begins 00 00 FE FF, which is no BOM at all -> the ordinary prescan/UTF-8 path
        let be32: Vec<u8> = [0x00u8, 0x00, 0xFE, 0xFF].into_iter().collect();
        assert_eq!(resolve(&be32, None), encoding_rs::UTF_8);
    }
}
