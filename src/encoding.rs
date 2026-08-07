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

/// Simplified HTML5 `<meta>` charset prescan over the document head ([`PRESCAN_WINDOW`] bytes). The
/// `charset` token is only honored INSIDE a `<meta …>` tag — WHATWG's prescan (and w3lib, which
/// Scrapy uses) require attribute context, so a `<!-- saved from url … charset=windows-1252 -->`
/// banner or `charset=big5` in early visible text must NOT switch the decode. Within a meta tag the
/// loose `charset=` scan still covers both bare `charset=` and `http-equiv`+`content="…; charset=…"`.
/// Start of an unclosed `<!--` before `at`, if `at` sits inside a comment.
///
/// Comment boundaries come from the TOKENIZER's own `scan_comment`, not a second copy here. A local
/// `-->`-only search missed the abrupt closes libxml2 honours (`<!-->`, `<!--->`, and `--!>`), so a
/// perfectly live `<meta charset>` after one of them looked like it was still commented out and the
/// declaration was ignored. One implementation, differential-proven, used by both.
fn last_comment_open_before(head: &[u8], at: usize) -> Option<usize> {
    let open = memchr::memmem::rfind(&head[..at], b"<!--")?;
    (crate::tokenizer::scan_comment(head, open) > at).then_some(open)
}

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

fn meta_prescan(head: &[u8]) -> Option<&'static Encoding> {
    if let Some(enc) = xml_decl_encoding(head) {
        return enc.into();
    }
    let lower: Vec<u8> = head.iter().map(|b| b.to_ascii_lowercase()).collect();
    let mut mfrom = 0usize;
    while let Some(mrel) = memchr::memmem::find(&lower[mfrom..], b"<meta") {
        let hit = mfrom + mrel;
        // a COMMENT declares nothing: `<!-- <meta charset=big5> -->` must not switch the decode
        if let Some(c) = last_comment_open_before(head, hit) {
            mfrom = crate::tokenizer::scan_comment(head, c);
            continue;
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

/// How far into the document a `<meta>`/XML-declaration charset is still a declaration. Matches w3lib,
/// the sniffing oracle; see the note in [`resolve`] for why WHATWG's 1024 is not the number to use.
const PRESCAN_WINDOW: usize = 4096;

/// BOM → BOM-less UTF-16 XML prefix → caller/HTTP label → `<meta>`/XML-declaration prescan (first
/// [`PRESCAN_WINDOW`] bytes) → UTF-8. The intentional differences from w3lib (Scrapy's decoder) are in
/// the encoding section of docs/COMPATIBILITY.md and gated in `tools/enc_check.py`; each one is a place
/// where w3lib and browsers disagree and Frostwork follows the browser.
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
    // WHATWG's 1024 is a STREAMING budget, not a correctness cap — user agents are "encouraged to use the
    // prescan algorithm ... on the first 1024 bytes, but not to stall beyond that", i.e. do not block
    // first paint waiting for more bytes off the network. Frostwork is handed the whole document at once
    // and has nothing to stall on, so that budget buys nothing here and only loses pages: a legacy site
    // that opens with a producer comment or a block of `og:` metas puts its `Content-Type` past byte 1024,
    // and the page then decoded as UTF-8 with a U+FFFD in every value. So the window matches w3lib's 4096
    // ("we allow for more"), which is also what libxml2's own sniffing finds on those pages.
    //
    // Deliberately NOT stopped at `<body>` (w3lib's regex does), because a real page can carry a late
    // `<meta charset>` inside the body and browsers still honour it.
    if let Some(enc) = meta_prescan(&html[..html.len().min(PRESCAN_WINDOW)]) {
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

    /// A declaration is honoured wherever it is in the head, not only in the first 1024 bytes.
    ///
    /// Found on real pages, three in one 1000-page Common Crawl sample: a legacy site opens with a
    /// producer comment or a block of `og:`/`keywords` `<meta>`s and the `Content-Type` lands at byte
    /// 1080/1532/1611. The engine read 1024, missed it, and decoded a whole windows-1252 page as UTF-8 —
    /// U+FFFD in every value on the page, which is what 57 of that sample's divergences were.
    ///
    /// The 1024 in WHATWG is a STREAMING budget, not a correctness cap: user agents are "encouraged to
    /// use the prescan algorithm ... on the first 1024 bytes, but **not to stall beyond that**" — i.e. do
    /// not block first paint waiting for more bytes off the network. Frostwork is handed the whole
    /// document at once and has nothing to stall on, so the budget buys nothing and only loses pages.
    /// Both oracles honour these declarations: w3lib reads 4096 ("we allow for more"), and libxml2's own
    /// sniffing decodes all three pages' titles correctly.
    #[test]
    fn a_declaration_past_1024_bytes_is_still_honoured() {
        let pad = |n: usize| -> Vec<u8> {
            let mut v = b"<html><head><!--".to_vec();
            v.resize(v.len() + n, b'x');
            v.extend_from_slice(b"-->");
            v
        };
        let decl = br#"<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">"#;

        // the shape the crawl found: filler, then the declaration past the old window
        let mut late = pad(1100);
        late.extend_from_slice(decl);
        assert!(late.iter().position(|&b| b == b'C').unwrap() > 1024);
        assert_eq!(resolve(&late, None), encoding_rs::WINDOWS_1252);

        // still found at the far edge of the widened window...
        let mut edge = pad(3900);
        edge.extend_from_slice(decl);
        assert_eq!(resolve(&edge, None), encoding_rs::WINDOWS_1252);

        // ...and the window is still bounded, so a declaration past it is not a declaration
        let mut beyond = pad(4200);
        beyond.extend_from_slice(decl);
        assert_eq!(resolve(&beyond, None), encoding_rs::UTF_8);

        // an explicit caller/HTTP label still outranks any prescan, near or far
        assert_eq!(resolve(&late, Some("big5")), encoding_rs::BIG5);
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
