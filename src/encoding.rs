//! Encoding resolution: BOM -> explicit override (HTTP/caller) -> `<meta>` charset prescan ->
//! UTF-8 default. Ported from `parsel-stream-core/src/encoding.rs` (proven to match Parsel).
//!
//! We never transcode the whole document for ASCII-compatible encodings — the tokenizer runs on raw
//! bytes (every HTML structural delimiter is `< 0x40`, unambiguous for every WHATWG encoding except
//! the UTF-16 family), and the matcher decodes only emitted *values* with the resolved encoding.
//! UTF-16LE/BE (not ASCII-compatible) are transcoded to UTF-8 up front by the caller (see lib.rs).

use encoding_rs::Encoding;

/// Simplified HTML5 `<meta>` charset prescan over the document head (first ~1024 bytes). The
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

fn meta_prescan(head: &[u8]) -> Option<&'static Encoding> {
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
        if let Some(enc) = label.and_then(|l| Encoding::for_label(l.trim().as_bytes())) {
            // WHATWG: a document DECLARING UTF-16 is read as UTF-8 (bytes reaching the prescan are
            // ASCII-compatible by construction; real UTF-16 is caught by the BOM).
            if enc == encoding_rs::UTF_16LE || enc == encoding_rs::UTF_16BE {
                return Some(encoding_rs::UTF_8);
            }
            return Some(enc);
        }
        mfrom = after.max(tag_start + 1);
    }
    None
}

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
    if let Some(label) = override_label {
        if let Some(enc) = Encoding::for_label(label.as_bytes()) {
            return enc;
        }
    }
    if let Some(enc) = meta_prescan(&html[..html.len().min(1024)]) {
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
}
