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
fn last_comment_open_before(lower: &[u8], at: usize) -> Option<usize> {
    let open = memchr::memmem::rfind(&lower[..at], b"<!--")?;
    (comment_end(lower, open) > at).then_some(open)
}

/// Offset just past the `-->` closing the comment that starts at `open`, or the end of the head.
fn comment_end(lower: &[u8], open: usize) -> usize {
    let from = open + b"<!--".len();
    match memchr::memmem::find(&lower[from.min(lower.len())..], b"-->") {
        Some(k) => from + k + b"-->".len(),
        None => lower.len(),
    }
}

/// The value of a genuine `charset` ATTRIBUTE on this tag (`<meta charset=…>`), as opposed to a
/// `charset=` appearing inside some other attribute's value such as `content="…; charset=…"`.
fn attr_charset(head: &[u8], lower: &[u8], tag_start: usize, tag_end: usize) -> Option<usize> {
    let mut from = tag_start;
    while let Some(rel) = memchr::memmem::find(&lower[from..tag_end], b"charset") {
        let at = from + rel;
        // An attribute NAME sits outside any quoted value and is followed by `=`. Tracking the quote
        // state is what separates `<meta charset=x>` from `<meta content="text/html; charset=x">`: in the
        // latter the `charset` is inside `content`'s value, where only `http-equiv` makes it a
        // declaration. A "preceded by whitespace" test alone accepts both (`; charset=`).
        let mut quote = 0u8;
        for &c in &head[tag_start..at] {
            match c {
                b'"' | b'\'' if quote == 0 => quote = c,
                c if c == quote => quote = 0,
                _ => {}
            }
        }
        let mut j = at + b"charset".len();
        while j < tag_end && matches!(head[j], b' ' | b'\t') {
            j += 1;
        }
        let name_start = at == tag_start
            || matches!(head[at - 1], b' ' | b'\t' | b'\n' | b'\r' | 0x0c | b'/');
        if quote == 0 && name_start && j < tag_end && head[j] == b'=' {
            return Some(at);
        }
        from = at + b"charset".len();
    }
    None
}

fn meta_prescan(head: &[u8]) -> Option<&'static Encoding> {
    let lower: Vec<u8> = head.iter().map(|b| b.to_ascii_lowercase()).collect();
    let mut mfrom = 0usize;
    while let Some(mrel) = memchr::memmem::find(&lower[mfrom..], b"<meta") {
        let hit = mfrom + mrel;
        // A COMMENT is skipped wholesale: the prescan runs the tokenizer's comment state, so
        // `<!-- <meta charset=big5> -->` declares nothing. Scanning raw bytes for `<meta` honoured it and
        // switched the decode, corrupting every value on an otherwise-UTF-8 page.
        if let Some(c) = last_comment_open_before(&lower, hit) {
            mfrom = comment_end(&lower, c);
            continue;
        }
        let tag_start = hit + b"<meta".len();
        // require a tag-name terminator after `<meta` so `<metadata …>` isn't treated as a meta tag
        if !matches!(head.get(tag_start), Some(b' ' | b'\t' | b'\n' | b'\r' | 0x0c | b'/' | b'>')) {
            mfrom = tag_start;
            continue;
        }
        // the tag ends at the next `>` (or the end of the scanned head)
        let tag_end = memchr::memchr(b'>', &head[tag_start..]).map_or(head.len(), |k| tag_start + k);
        // The `content="…; charset=…"` form declares an encoding ONLY with `http-equiv` present — a bare
        // `<meta content="text/html; charset=big5">` is inert for WHATWG/w3lib, so honouring it diverged.
        let bare_charset = attr_charset(head, &lower, tag_start, tag_end);
        if bare_charset.is_none()
            && memchr::memmem::find(&lower[tag_start..tag_end], b"http-equiv").is_none()
        {
            mfrom = tag_end;
            continue;
        }
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
                        // WHATWG: a document that declares a UTF-16 encoding in `<meta>` is read as
                        // UTF-8 (the bytes reaching the prescan are ASCII-compatible by construction —
                        // real UTF-16 is caught by the BOM). Transcoding as UTF-16 instead turned an
                        // ASCII document into mojibake and every selector returned empty.
                        if enc == encoding_rs::UTF_16LE || enc == encoding_rs::UTF_16BE {
                            return Some(encoding_rs::UTF_8);
                        }
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
