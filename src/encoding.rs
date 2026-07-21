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
fn meta_prescan(head: &[u8]) -> Option<&'static Encoding> {
    let lower: Vec<u8> = head.iter().map(|b| b.to_ascii_lowercase()).collect();
    let mut mfrom = 0usize;
    while let Some(mrel) = memchr::memmem::find(&lower[mfrom..], b"<meta") {
        let tag_start = mfrom + mrel + b"<meta".len();
        // require a tag-name terminator after `<meta` so `<metadata …>` isn't treated as a meta tag
        if !matches!(head.get(tag_start), Some(b' ' | b'\t' | b'\n' | b'\r' | 0x0c | b'/' | b'>')) {
            mfrom = tag_start;
            continue;
        }
        // the tag ends at the next `>` (or the end of the scanned head)
        let tag_end = memchr::memchr(b'>', &head[tag_start..]).map_or(head.len(), |k| tag_start + k);
        let mut from = tag_start;
        while let Some(rel) = memchr::memmem::find(&lower[from..tag_end], b"charset") {
            let mut j = from + rel + b"charset".len();
            while j < tag_end && matches!(head[j], b' ' | b'\t') {
                j += 1;
            }
            if j < tag_end && head[j] == b'=' {
                j += 1;
                while j < tag_end && matches!(head[j], b' ' | b'\t' | b'"' | b'\'') {
                    j += 1;
                }
                let start = j;
                while j < tag_end
                    && !matches!(head[j], b'"' | b'\'' | b';' | b' ' | b'\t' | b'\r' | b'\n' | b'>' | b'/')
                {
                    j += 1;
                }
                if j > start {
                    if let Some(enc) = Encoding::for_label(&head[start..j]) {
                        return Some(enc);
                    }
                }
            }
            from = from + rel + b"charset".len();
        }
        mfrom = tag_end;
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
