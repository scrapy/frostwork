//! Encoding resolution, one precedence table for every entry point (see [`resolve_explained`]):
//!
//! 1. a BOM (UTF-8, UTF-16LE, UTF-16BE);
//! 2. BOM-less UTF-16, from the `<?` prefix;
//! 3. the caller's own label (`encoding=`);
//! 4. the `charset` parameter of the response's `Content-Type` header (`content_type=`);
//! 5. the document's declarations: an XML declaration at offset 0, then `<meta>` in document order;
//! 6. autodetection: for valid UTF-8, the default for the URL's TLD when the URL is http(s), as
//!    browsers read it, else UTF-8; for anything else, chardetng with the URL's TLD.
//!
//! A label a rank cannot use is ignored and resolution continues with the next rank.
//!
//! An ASCII-compatible encoding is never transcoded. The tokenizer runs on raw bytes and the matcher
//! decodes only emitted *values* with the resolved encoding, which works because in an ASCII-compatible
//! encoding a byte below 0x80 always IS that ASCII character, so every HTML structural delimiter is
//! unambiguous. Where that does not hold, the caller transcodes to UTF-8 up front (see `lib.rs`).
//!
//! Which encodings those are is `Encoding::is_ascii_compatible`'s answer, never a list written here.
//! "The UTF-16 family" is the wrong answer: ISO-2022-JP is not ASCII-compatible either, because in its
//! `ESC $ B` mode `社` is the two bytes `<R`, and a byte tokenizer would grow a start tag out of the
//! middle of a Japanese word.

use encoding_rs::Encoding;

/// Tokenize one `<meta …>` tag's attributes, bounded, starting just past `<meta`. Returns
/// `(attributes, offset just past the tag)`. Names are lowercased; values are returned as-is.
///
/// A substring scan cannot do this job and got it wrong in BOTH directions: `data-http-equiv` looked
/// like `http-equiv`, a `charset=` inside any attribute value looked like a declaration, and the first
/// raw `>` "ended" the tag even inside a quoted value.
///
/// `from_utf8_lossy` is lossless *for this purpose*, even though the document's encoding is still
/// undecided here (that is what the prescan decides). What it reads is an encoding LABEL, and every label
/// is ASCII, matched ASCII-case-insensitively. A non-ASCII byte becomes U+FFFD, which matches no label
/// and no attribute name this cares about, so a mangled byte can only turn "not a declaration" into
/// "still not a declaration", the answer a browser reaches too.
fn meta_attrs(head: &[u8], from: usize) -> (Vec<(String, String)>, usize) {
    let mut attrs = Vec::new();
    let after = walk_attrs(head, from, |name, value| {
        attrs.push((
            String::from_utf8_lossy(name).to_ascii_lowercase(),
            String::from_utf8_lossy(value).to_string(),
        ));
    });
    (attrs, after)
}

/// Call `f` with the raw name and value of each attribute of the tag whose name ends at `from`, and
/// return the offset just past the tag.
fn walk_attrs(head: &[u8], from: usize, mut f: impl FnMut(&[u8], &[u8])) -> usize {
    const WS: [u8; 5] = [b' ', b'\t', b'\n', b'\r', 0x0c];
    let n = head.len();
    let mut i = from;
    loop {
        while i < n && (WS.contains(&head[i]) || head[i] == b'/') {
            i += 1;
        }
        if i >= n || head[i] == b'>' {
            return (i + 1).min(n);
        }
        let ns = i;
        while i < n && !WS.contains(&head[i]) && !matches!(head[i], b'=' | b'>' | b'/') {
            i += 1;
        }
        let name = &head[ns..i];
        // whitespace is allowed on BOTH sides of `=` (`charset\n=\nwindows-1252` is a real declaration)
        while i < n && WS.contains(&head[i]) {
            i += 1;
        }
        if i >= n || head[i] != b'=' {
            if !name.is_empty() {
                f(name, b""); // valueless attribute
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
            let v = &head[vs..i];
            i = (i + 1).min(n);
            v
        } else {
            let vs = i;
            while i < n && !WS.contains(&head[i]) && head[i] != b'>' {
                i += 1;
            }
            &head[vs..i]
        };
        if !name.is_empty() {
            f(name, value);
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
/// Comment boundaries come from the TOKENIZER's own `scan_comment`, not a second copy here. libxml2
/// honours abrupt closes (`<!-->`, `<!--->`, `--!>`), so a local `-->`-only search would read a live
/// `<meta charset>` after one of them as still commented out and ignore the declaration.
///
/// ONE left-to-right pass, allocation-free outside `<meta>` tags, skipping comments and every other
/// tag on the way past them.
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
/// A flat byte window is wrong in both directions: it would drop late head declarations and honour late
/// body declarations no browser does. It also costs ~35µs on every label-less page, a whole extra pass
/// over the document, which stopping at the real head boundary does not.
pub(crate) fn meta_prescan(head: &[u8]) -> Option<&'static Encoding> {
    let mut mfrom = 0usize;
    // Raised to `max(PRESCAN_FLOOR, here)` the moment a tag ends the head; `MAX` while still inside it.
    let mut limit = usize::MAX;
    while let Some(rel) = memchr::memchr(b'<', &head[mfrom..]) {
        let hit = mfrom + rel;
        if hit >= limit {
            return None;
        }
        // a COMMENT declares nothing: `<!-- <meta charset=big5> -->` must not switch the decode.
        // `scan_comment` owns the abrupt-close shapes (`<!-->`, `--!>`); `.max` guarantees progress.
        if head[hit..].starts_with(b"<!--") {
            mfrom = crate::tokenizer::scan_comment(head, hit).max(hit + 1);
            continue;
        }
        // `</` + letter is an end tag, whose name `start_tag_name` finds one byte further on
        let end_tag = head.get(hit + 1) == Some(&b'/');
        let Some(name) = start_tag_name(head, hit + usize::from(end_tag)) else {
            mfrom = hit + 1;
            continue;
        };
        let tag_start = hit + usize::from(end_tag) + 1 + name.len();
        if !end_tag && is_raw_text(name) {
            mfrom = skip_raw_text(head, hit, name).max(hit + 1);
            continue;
        }
        // The head ends at `</head>` or at the first start tag that may not appear in it — and with
        // it ends the browser's willingness to re-decode, once past the floor.
        if (end_tag && name.eq_ignore_ascii_case(b"head")) || (!end_tag && !is_head_element(name)) {
            limit = limit.min(PRESCAN_FLOOR.max(hit));
        }
        // Any other tag is skipped WITH its attributes, as WHATWG's prescan does, so a `<meta` inside
        // a quoted value (`<img alt="<meta charset=big5>">`) is text.
        if end_tag || !name.eq_ignore_ascii_case(b"meta") {
            mfrom = walk_attrs(head, tag_start, |_, _| {}).max(hit + 1);
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

/// What a caller knows about a response besides its bytes: the inputs to ranks 3, 4 and 6 of the
/// precedence table in [`resolve_explained`].
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct EncodingHints<'a> {
    /// The caller's own label, for a user who forced an encoding. Never a framework's resolved
    /// encoding: that is a decision, and passed here it would outrank the document's declarations.
    pub encoding: Option<&'a str>,
    /// The raw `Content-Type` header value. Only its `charset` parameter is read.
    pub content_type: Option<&'a str>,
    /// The response URL. An http(s) scheme gives a valid UTF-8 document that declares nothing the
    /// default of the host's top-level domain, which also informs chardetng's guess for anything else.
    pub url: Option<&'a str>,
}

/// Which rank of the precedence table decided an encoding.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EncodingSource {
    /// A UTF-8, UTF-16LE or UTF-16BE byte order mark.
    Bom,
    /// A BOM-less UTF-16 document, recognised by `<?` encoded as UTF-16.
    Utf16Prefix,
    /// The caller's label ([`EncodingHints::encoding`]).
    Encoding,
    /// The `charset` parameter of [`EncodingHints::content_type`].
    ContentType,
    /// An XML declaration at offset 0.
    XmlDeclaration,
    /// A `<meta charset>` or `<meta http-equiv=content-type>`.
    Meta,
    /// No label or declaration, and the document is valid UTF-8.
    Utf8,
    /// No label or declaration, the document is valid UTF-8, and its URL is http(s), for which browsers
    /// refuse to detect UTF-8: the default for the URL's top-level domain, windows-1252 for a generic one.
    Default,
    /// No label or declaration, and chardetng's guess.
    Autodetect,
}

impl EncodingSource {
    /// The name the Python binding reports (`detect_encoding(..., explain=True)`).
    pub fn as_str(self) -> &'static str {
        match self {
            EncodingSource::Bom => "bom",
            EncodingSource::Utf16Prefix => "utf16-prefix",
            EncodingSource::Encoding => "encoding",
            EncodingSource::ContentType => "content-type",
            EncodingSource::XmlDeclaration => "xml-declaration",
            EncodingSource::Meta => "meta",
            EncodingSource::Utf8 => "utf8",
            EncodingSource::Default => "default",
            EncodingSource::Autodetect => "autodetect",
        }
    }
}

/// The `charset` parameter of a `Content-Type` header value: the first non-empty parameter whose name
/// is `charset` in any case, quoted or not, with whitespace around the name, the `=` and the value
/// ignored. `None` when there is none. The header is server data, so nothing here can fail.
fn content_type_charset(value: &str) -> Option<String> {
    let (_essence, mut rest) = value.split_once(';')?;
    loop {
        let i = rest.find(['=', ';'])?;
        let (name, after) = (&rest[..i], &rest[i..]);
        let name = name.trim_matches(is_http_ws);
        if let Some(after) = after.strip_prefix(';') {
            rest = after;
            continue;
        }
        let after = after[1..].trim_start_matches(is_http_ws);
        let (param, next) = if let Some(quoted) = after.strip_prefix('"') {
            let mut param = String::new();
            let mut chars = quoted.char_indices();
            let mut end = quoted.len();
            while let Some((i, c)) = chars.next() {
                match c {
                    '"' => {
                        end = i + 1;
                        break;
                    }
                    '\\' => match chars.next() {
                        Some((_, escaped)) => param.push(escaped),
                        None => param.push('\\'),
                    },
                    c => param.push(c),
                }
            }
            let tail = &quoted[end..];
            (param, tail.find(';').map(|i| &tail[i + 1..]))
        } else {
            match after.find(';') {
                Some(i) => (after[..i].trim_end_matches(is_http_ws).to_string(), Some(&after[i + 1..])),
                None => (after.trim_end_matches(is_http_ws).to_string(), None),
            }
        };
        if name.eq_ignore_ascii_case("charset") && !param.is_empty() {
            return Some(param);
        }
        rest = next?;
    }
}

fn is_http_ws(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\r')
}

/// The encoding `html` is scanned with, and the rank that decided it. Each rank either decides or, if it
/// has nothing usable, hands over to the next:
///
/// 1. a BOM;
/// 2. BOM-less UTF-16 from the `<?` prefix;
/// 3. `hints.encoding`, resolved with the WHATWG label table (a label it does not know is ignored here;
///    the Python binding refuses one no codec knows before it reaches this point);
/// 4. the `charset` parameter of `hints.content_type`, resolved the same way and taken literally, so
///    `x-user-defined` and the `replacement` labels keep their meaning;
/// 5. the document's own declarations ([`declaration`]);
/// 6. autodetection ([`autodetect`]), with `hints.url`.
///
/// The intentional differences from w3lib (Scrapy's decoder) are in the encoding section of
/// docs/COMPATIBILITY.md and gated in `tools/enc_check.py`; each one is a place where w3lib and browsers
/// disagree and Frostwork follows the browser.
///
/// No UTF-32: the WHATWG Encoding Standard has no UTF-32, and neither does any browser, so a UTF-32 BOM
/// is not a BOM here. w3lib does recognize it (its BOM table predates the standard).
pub fn resolve_explained(html: &[u8], hints: EncodingHints<'_>) -> (&'static Encoding, EncodingSource) {
    if let Some((enc, len)) = bom(html) {
        let source = if len == 0 { EncodingSource::Utf16Prefix } else { EncodingSource::Bom };
        return (enc, source);
    }
    if let Some(enc) = hints.encoding.and_then(|label| Encoding::for_label(label.as_bytes())) {
        return (enc, EncodingSource::Encoding);
    }
    let header = hints.content_type.and_then(content_type_charset);
    if let Some(enc) = header.and_then(|label| Encoding::for_label(label.as_bytes())) {
        return (enc, EncodingSource::ContentType);
    }
    if let Some(found) = declaration(html) {
        return found;
    }
    autodetect(html, hints.url)
}

/// [`resolve_explained`] with a caller label and a URL, for the unit tests below.
#[cfg(test)]
fn resolve(html: &[u8], label: Option<&str>, url: Option<&str>) -> &'static Encoding {
    resolve_explained(html, EncodingHints { encoding: label, url, ..EncodingHints::default() }).0
}

/// The document's own declaration: an XML declaration at offset 0, else the first usable `<meta>`.
///
/// The prescan is NOT bounded by a byte window, because a browser's is not. WHATWG's 1024 is a
/// STREAMING budget — user agents are "encouraged to use the prescan algorithm ... on the first 1024
/// bytes, but not to stall beyond that", i.e. do not block first paint waiting for more bytes off the
/// network — and a browser that meets a `<meta charset>` after it simply runs "change the encoding"
/// and re-decodes what it already has. Measured rather than assumed: Chrome honours a declaration at
/// 1 KB, 4 KB, 16 KB, 64 KB, 256 KB and 1 MB alike, so there is no bound to match. Frostwork holds the
/// whole document and has nothing to stall on either.
///
/// A window is therefore a divergence from the browser, and this subsystem has no budget for one (see
/// the encoding rule in AGENTS.md). A fixed window would miss declarations on pages that open with a
/// producer comment or a block of metadata, decoding their values with the wrong encoding.
///
/// Deliberately NOT stopped at `<body>` (w3lib's regex does), because a real page can carry a late
/// `<meta charset>` inside the body and browsers still honour it.
pub(crate) fn declaration(html: &[u8]) -> Option<(&'static Encoding, EncodingSource)> {
    if let Some(enc) = xml_decl_encoding(html) {
        return Some((enc, EncodingSource::XmlDeclaration));
    }
    meta_prescan(html).map(|enc| (enc, EncodingSource::Meta))
}

/// The encoding the start of `html` fixes before any label is consulted, with the length of the BOM
/// that announced it: a UTF-8 or UTF-16 BOM, or the BOM-less UTF-16 start of an XML declaration
/// (length 0).
///
/// No UTF-32: the WHATWG Encoding Standard has no UTF-32, and neither does any browser, so a UTF-32 BOM
/// is not a BOM here. w3lib does recognize it (its BOM table predates the standard).
pub(crate) fn bom(html: &[u8]) -> Option<(&'static Encoding, usize)> {
    if html.starts_with(&[0xEF, 0xBB, 0xBF]) {
        return Some((encoding_rs::UTF_8, 3));
    }
    if html.starts_with(&[0xFF, 0xFE]) {
        return Some((encoding_rs::UTF_16LE, 2));
    }
    if html.starts_with(&[0xFE, 0xFF]) {
        return Some((encoding_rs::UTF_16BE, 2));
    }
    // A UTF-16 document with NO BOM, detected the way XML 1.0 Appendix F (and libxml2's own
    // `xmlDetectEncoding`) does it: `<?` encoded as UTF-16 is `3C 00 3F 00` / `00 3C 00 3F`. This sits with
    // the BOM checks rather than after the label, on the same reasoning WHATWG gives a BOM priority — the
    // bytes are unambiguous, and no ASCII-compatible document can begin with a NUL. Without it the whole
    // document decoded as mojibake, and libxml2 (the value oracle) reads these files correctly.
    if html.starts_with(&[0x3C, 0x00, 0x3F, 0x00]) {
        return Some((encoding_rs::UTF_16LE, 0));
    }
    if html.starts_with(&[0x00, 0x3C, 0x00, 0x3F]) {
        return Some((encoding_rs::UTF_16BE, 0));
    }
    None
}

/// WHATWG's optional frequency-analysis step.
///
/// A valid UTF-8 document (a body cut off inside a UTF-8 sequence still counts) is UTF-8 unless `url`
/// is http(s). Chrome and Firefox refuse to detect UTF-8 for a page from the network and keep a
/// default instead: the legacy encoding of the URL's top-level domain (Shift_JIS for `.jp`,
/// windows-1251 for `.ru`), and windows-1252 for a generic one. Both read a `file:` page as UTF-8, and
/// without a URL Frostwork reads the document as a file, since such a document is most often one stored
/// on disk. Anything that is not valid UTF-8 goes to chardetng, Firefox's detector, with the top-level
/// domain of `url` as its hint.
///
/// The default is chardetng's too: with UTF-8 denied it answers a valid UTF-8 document from the
/// top-level domain alone, so an unfed detector gives it without reading the document. Chrome's table
/// differs from Firefox's on a few domains, and its detector (CED) runs on each network chunk until one
/// gives a verdict, so its guess for the same bytes moves with how they arrive. See "Deliberate
/// differences from browsers" in docs/COMPATIBILITY.md.
///
/// The decode is `simdutf8::compat`, which returns what `std::str::from_utf8` does. Not `basic`: it
/// reads the whole document before failing and has no `error_len()` to tell a truncated tail apart.
pub(crate) fn autodetect(html: &[u8], url: Option<&str>) -> (&'static Encoding, EncodingSource) {
    let valid_utf8 = match simdutf8::compat::from_utf8(html) {
        Ok(_) => true,
        Err(e) => e.error_len().is_none(),
    };
    if valid_utf8 && !url.is_some_and(is_http_url) {
        return (encoding_rs::UTF_8, EncodingSource::Utf8);
    }
    let mut detector = chardetng::EncodingDetector::new(chardetng::Iso2022JpDetection::Deny);
    let source = if valid_utf8 {
        EncodingSource::Default
    } else {
        detector.feed(html, true);
        EncodingSource::Autodetect
    };
    let tld = url.and_then(tld);
    (detector.guess(tld.as_deref(), chardetng::Utf8Detection::Deny), source)
}

/// Whether `url`'s scheme is `http` or `https`, in any case, read as the WHATWG URL parser reads it:
/// leading C0 controls and spaces stripped, and ASCII tab and newline removed wherever they are.
fn is_http_url(url: &str) -> bool {
    let url = url.trim_start_matches(|c: char| c <= ' ');
    let Some((scheme, _)) = url.split_once(':') else {
        return false;
    };
    let scheme = scheme.bytes().filter(|b| !matches!(b, b'\t' | b'\n' | b'\r')).map(|b| b.to_ascii_lowercase());
    scheme.clone().eq(*b"http") || scheme.eq(*b"https")
}

/// The last label of `url`'s host, lowercased, in the form chardetng accepts: ASCII without periods.
/// An IP address or a non-ASCII host yields `None`, as does a string without a scheme.
pub(crate) fn tld(url: &str) -> Option<Vec<u8>> {
    let (_, rest) = url.split_once("://")?;
    let authority = rest.split(['/', '\\', '?', '#']).next()?;
    let host = authority.rsplit('@').next()?;
    if host.starts_with('[') {
        return None;
    }
    let host = host.split(':').next()?.trim_end_matches('.');
    let label = host.rsplit('.').next()?;
    if label.is_empty()
        || !label.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
        || label.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    Some(label.to_ascii_lowercase().into_bytes())
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
        assert_eq!(resolve(html, None, None), encoding_rs::UTF_8);
    }

    #[test]
    fn the_content_type_charset_parameter_is_parsed_leniently() {
        let cs = content_type_charset;
        assert_eq!(cs("text/html; charset=windows-1252"), Some("windows-1252".into()));
        assert_eq!(cs("text/html;CHARSET=Big5"), Some("Big5".into()));
        assert_eq!(cs("text/html; charset=\"utf-8\""), Some("utf-8".into()));
        assert_eq!(cs("text/html; charset = shift_jis ; q=1"), Some("shift_jis".into()));
        assert_eq!(cs("text/html; foo=\"a;charset=big5\"; charset=koi8-r"), Some("koi8-r".into()));
        assert_eq!(cs("text/html; charset=\"a\\\"b\""), Some("a\"b".into()));
        assert_eq!(cs("text/html; charset=; charset=gbk"), Some("gbk".into()));
        assert_eq!(cs("text/html; charset=euc-jp; charset=big5"), Some("euc-jp".into()));
        assert_eq!(cs("text/html"), None);
        assert_eq!(cs("text/html; charset"), None);
        assert_eq!(cs("windows-1252"), None); // a bare label is not a header value
        assert_eq!(cs(""), None);
    }

    #[test]
    fn one_precedence_table_decides_every_encoding() {
        use EncodingSource as S;
        let r = |html: &[u8], encoding, content_type| {
            let hints = EncodingHints { encoding, content_type, ..EncodingHints::default() };
            let (enc, src) = resolve_explained(html, hints);
            (enc.name(), src)
        };
        let meta = b"<meta charset=koi8-r><p>caf\xe9</p>";
        assert_eq!(r(b"\xef\xbb\xbf<p>x</p>", Some("windows-1252"), None), ("UTF-8", S::Bom));
        let le: Vec<u8> = "<?xml version='1.0'?>".encode_utf16().flat_map(u16::to_le_bytes).collect();
        assert_eq!(r(&le, Some("big5"), Some("text/html; charset=big5")), ("UTF-16LE", S::Utf16Prefix));
        assert_eq!(r(meta, Some("big5"), Some("text/html; charset=gbk")), ("Big5", S::Encoding));
        assert_eq!(r(meta, None, Some("text/html; charset=gbk")), ("GBK", S::ContentType));
        // an unusable label at rank 3 or 4 hands over to the next rank
        assert_eq!(r(meta, Some("utf-7"), Some("text/html; charset=gbk")), ("GBK", S::ContentType));
        assert_eq!(r(meta, None, Some("text/html; charset=nonsense")), ("KOI8-R", S::Meta));
        assert_eq!(r(meta, None, Some("text/html")), ("KOI8-R", S::Meta));
        assert_eq!(r(b"<?xml version='1.0' encoding='big5'?><p>x</p>", None, None), ("Big5", S::XmlDeclaration));
        assert_eq!(r("<p>café</p>".as_bytes(), None, None), ("UTF-8", S::Utf8));
        let french = b"<p>Le caf\xe9 est d\xe9j\xe0 pr\xeat, tr\xe8s chaud et tr\xe8s bon.</p>";
        assert_eq!(r(french, None, None), ("windows-1252", S::Autodetect));
        // the header label is taken literally: no `<meta>` rewrite of x-user-defined or utf-16
        assert_eq!(r(b"<p>x</p>", None, Some("text/html; charset=x-user-defined")), ("x-user-defined", S::ContentType));
        assert_eq!(r(b"<p>x</p>", None, Some("text/html; charset=utf-16")), ("UTF-16LE", S::ContentType));
        assert_eq!(r(b"<p>x</p>", None, Some("text/html; charset=iso-2022-kr")), ("replacement", S::ContentType));
    }

    #[test]
    fn the_tld_is_the_last_label_of_the_host() {
        let t = |url| tld(url).map(|t| String::from_utf8(t).unwrap());
        assert_eq!(t("https://www.Example.CO.JP/a.b?c.d#e.f"), Some("jp".into()));
        assert_eq!(t("http://user:pw@example.ru:8080"), Some("ru".into()));
        assert_eq!(t("http://example.de./"), Some("de".into()));
        assert_eq!(t("http://xn--p1ai\\path"), Some("xn--p1ai".into()));
        assert_eq!(t("http://127.0.0.1/"), None);
        assert_eq!(t("http://[::1]:80/"), None);
        assert_eq!(t("http://пример.рф/"), None);
        assert_eq!(t("example.jp"), None);
        assert_eq!(t("http:///path"), None);
    }

    #[test]
    fn an_undeclared_document_is_autodetected() {
        assert_eq!(resolve("<p>café</p>".as_bytes(), None, None), encoding_rs::UTF_8);
        assert_eq!(resolve(b"<p>caf\xc3", None, None), encoding_rs::UTF_8); // truncated mid-character
        let french = b"<p>Le caf\xe9 est d\xe9j\xe0 pr\xeat, tr\xe8s chaud et tr\xe8s bon.</p>";
        assert_eq!(resolve(french, None, None), encoding_rs::WINDOWS_1252);
        let (japanese, _, _) = encoding_rs::SHIFT_JIS.encode("<p>日本語のページです。こんにちは、世界。</p>");
        assert_eq!(resolve(&japanese, None, None), encoding_rs::SHIFT_JIS);
        // too short to tell apart without the hint
        assert_eq!(resolve(b"<p>\x82\xa0</p>", None, None), encoding_rs::IBM866);
        assert_eq!(resolve(b"<p>\x82\xa0</p>", None, Some("https://example.jp/")), encoding_rs::SHIFT_JIS);
        // a declaration still wins over the guess
        assert_eq!(resolve(&[b"<meta charset=utf-8>", &japanese[..]].concat(), None, None), encoding_rs::UTF_8);
        // the same two answers past the first SIMD block: a truncated tail is UTF-8, a bad byte is not
        let long = "<p>café €</p>".repeat(40).into_bytes();
        assert_eq!(resolve(&[&long[..], b"\xe2\x82"].concat(), None, None), encoding_rs::UTF_8);
        assert_ne!(resolve(&[&long[..], b"\xe9t\xe9</p>"].concat(), None, None), encoding_rs::UTF_8);
    }

    /// Measured in Chrome 156 and Firefox 159 (en-GB, top-level loads): each of these pages, served with
    /// no label, is its domain's default over http, windows-1252 on a generic one, and UTF-8 from a
    /// `file:` URL.
    #[test]
    fn valid_utf8_over_http_is_the_domains_default() {
        use EncodingSource as S;
        let r = |html: &[u8], url| {
            let (enc, src) = resolve_explained(html, EncodingHints { url, ..EncodingHints::default() });
            (enc.name(), src)
        };
        for text in ["café", "日本語のテキストです。東京都", "Съешь же ещё этих мягких французских булок", "这是一个中文网页", "price 10€ 😀"] {
            let page = format!("<!DOCTYPE html><html><head></head><body><p id=\"t\">{text}</p></body></html>");
            let page = page.as_bytes();
            assert_eq!(r(page, Some("http://127.0.0.1:8765/c/x")), ("windows-1252", S::Default), "{text}");
            assert_eq!(r(page, Some("https://mojibake.wtf/c/x")), ("windows-1252", S::Default), "{text}");
            assert_eq!(r(page, Some("file:///tmp/x.html")), ("UTF-8", S::Utf8), "{text}");
            assert_eq!(r(page, None), ("UTF-8", S::Utf8), "{text}");
        }
        // a country-code domain has its own default, whatever the text (Chrome 156 and Firefox 159)
        for text in ["café", "日本語のテキストです。東京都", "Съешь же ещё этих мягких французских булок"] {
            let page = format!("<p>{text}</p>");
            let page = page.as_bytes();
            assert_eq!(r(page, Some("http://example.jp/")), ("Shift_JIS", S::Default), "{text}");
            assert_eq!(r(page, Some("https://example.ru/x")), ("windows-1251", S::Default), "{text}");
            assert_eq!(r(page, Some("http://example.gr/")), ("ISO-8859-7", S::Default), "{text}");
            assert_eq!(r(page, Some("file:///example.jp/x.html")), ("UTF-8", S::Utf8), "{text}");
        }
        // ASCII and a truncated tail are valid UTF-8 too
        assert_eq!(r(b"<p>x</p>", Some("http://example.com/")), ("windows-1252", S::Default));
        assert_eq!(r(b"<p>caf\xc3", Some("http://example.com/")), ("windows-1252", S::Default));
        // the scheme is read as a URL parser reads it
        assert_eq!(r(b"<p>x</p>", Some(" HTTPS://example.com/")), ("windows-1252", S::Default));
        assert_eq!(r(b"<p>x</p>", Some("httpx://example.com/")), ("UTF-8", S::Utf8));
        assert_eq!(r(b"<p>x</p>", Some("example.com/http:")), ("UTF-8", S::Utf8));
        // tab and newline vanish from a URL wherever they are, and a special scheme needs no slashes
        assert_eq!(r(b"<p>x</p>", Some("ht\ntp\ts://example.com/")), ("windows-1252", S::Default));
        assert_eq!(r(b"<p>x</p>", Some("http:example.com")), ("windows-1252", S::Default));
        assert_eq!(r(b"<p>x</p>", Some("ht tp://example.com/")), ("UTF-8", S::Utf8));
        // a declaration outranks the rule, and invalid UTF-8 goes to chardetng
        assert_eq!(r("<meta charset=utf-8><p>café</p>".as_bytes(), Some("http://a.com/")), ("UTF-8", S::Meta));
        let french = b"<p>Le caf\xe9 est d\xe9j\xe0 pr\xeat, tr\xe8s chaud et tr\xe8s bon.</p>";
        assert_eq!(r(french, Some("http://a.com/")), ("windows-1252", S::Autodetect));
    }

    #[test]
    fn autodetection_reads_the_whole_document() {
        // Firefox 144 reports windows-1251 for this page: the 46 000 Shift_JIS bytes at the start do not
        // decide the guess.
        let japanese = "<p>日本語のページです。こんにちは、世界。</p>\n".repeat(1000);
        let russian = "<p>Привет мир, это страница.</p>\n".repeat(10_000);
        let (japanese, _, _) = encoding_rs::SHIFT_JIS.encode(&japanese);
        let (russian, _, _) = encoding_rs::WINDOWS_1251.encode(&russian);
        let guess = |html: &[u8]| autodetect(html, None).0;
        assert_eq!(guess(&japanese), encoding_rs::SHIFT_JIS);
        assert_eq!(guess(&[&japanese[..], &russian[..]].concat()), encoding_rs::WINDOWS_1251);
    }
}

/// Prescan regression vectors. Cases where browsers and w3lib agree are oracled against
/// **w3lib.encoding.html_to_unicode**, which is what Scrapy uses to pick a response encoding. Named
/// divergences follow browser measurements and are also gated in `tools/enc_check.py`; Parsel is not an
/// oracle for the prescan because its UTF-8 default can agree by accident.
#[cfg(test)]
mod w3lib_oracle_tests {
    use super::*;

    fn scan(head: &[u8]) -> Option<&'static Encoding> {
        declaration(head).map(|(enc, _)| enc)
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

    /// Other tags are skipped with their attributes, so markup inside an attribute value is text.
    #[test]
    fn a_meta_inside_an_attribute_value_declares_nothing() {
        assert_eq!(scan(br#"<img alt="<meta charset=big5>">"#), None);
        assert_eq!(scan(br#"</p title='<meta charset=big5>'>"#), None);
        assert_eq!(
            scan(br#"<img alt="<meta charset=big5>"><meta charset=shift_jis>"#),
            Some(encoding_rs::SHIFT_JIS)
        );
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
        assert_eq!(resolve(b"<p>x</p>", Some("x-user-defined"), None), encoding_rs::X_USER_DEFINED);
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
        assert_eq!(resolve(&le, None, None), encoding_rs::UTF_16LE);
        assert_eq!(resolve(&be, None, None), encoding_rs::UTF_16BE);
        // bytes-don't-lie: the sniff sits with the BOM checks, so it outranks a wrong caller label
        assert_eq!(resolve(&le, Some("windows-1252"), None), encoding_rs::UTF_16LE);
        // an ordinary ASCII-compatible document is untouched by it
        assert_eq!(resolve(br#"<?xml version="1.0"?><p>x</p>"#, None, None), encoding_rs::UTF_8);
    }

    /// The `iso-8859-1` LABEL means windows-1252, wherever it arrives from — including an explicit
    /// HTTP/caller charset, which skips the prescan entirely.
    ///
    /// This is the WHATWG label table, it is what browsers do, and it is also what Scrapy does:
    /// `w3lib.encoding.resolve_encoding("iso-8859-1")` is `cp1252`. Only a *raw* Parsel
    /// `Selector(body=…, encoding="iso-8859-1")` — which bypasses w3lib — applies Python's literal
    /// latin-1 codec and leaves the C1 range as controls. A page whose HTTP header says
    /// `charset=iso-8859-1` and writes an en dash as the single byte 0x96 is readable text here and in
    /// Scrapy, and mojibake under raw Parsel.
    #[test]
    fn the_iso_8859_1_label_is_windows_1252() {
        assert_eq!(resolve(b"<p>x</p>", Some("iso-8859-1"), None), encoding_rs::WINDOWS_1252);
        assert_eq!(resolve(b"<p>x</p>", Some("latin1"), None), encoding_rs::WINDOWS_1252);
        assert_eq!(resolve(b"<p>x</p>", Some("ISO_8859-1:1987"), None), encoding_rs::WINDOWS_1252);
        // so the C1 bytes are the printable windows-1252 characters, not controls:
        // 0x96 is an en dash, not U+0096
        let (text, _, _) = resolve(b"", Some("iso-8859-1"), None).decode(b"Milano\x96Malpensa");
        assert_eq!(text, "Milano\u{2013}Malpensa");
    }

    /// How far a declaration is still a declaration — both halves measured in Chrome, because the
    /// browser is the standard for encoding and it does not match WHATWG's suggested budget.
    ///
    /// In the HEAD: honoured at any depth. A page can open with a producer comment or a block of metadata,
    /// so a bounded read would miss the declaration and decode the page with the wrong encoding.
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
        assert_eq!(resolve(&late, None, None), encoding_rs::WINDOWS_1252);

        // ...and past every depth a bounded window would cut off. These depths were measured in Chrome.
        for depth in [3900usize, 4200, 16 * 1024, 64 * 1024, 256 * 1024, 1024 * 1024] {
            let mut deep = pad(depth);
            deep.extend_from_slice(decl);
            assert_eq!(
                resolve(&deep, None, None),
                encoding_rs::WINDOWS_1252,
                "a HEAD declaration at depth {depth} must still be a declaration"
            );
        }

        // an explicit caller/HTTP label still outranks any prescan, near or far
        assert_eq!(resolve(&late, Some("big5"), None), encoding_rs::BIG5);
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
                resolve(&far(inner), None, None),
                encoding_rs::WINDOWS_1252,
                "raw-text content must not end the head: {:?}",
                std::str::from_utf8(inner).unwrap()
            );
        }
        // an unclosed <script> swallows the rest of the document, declaration included — which is
        // what a browser does too
        let mut unclosed = b"<html><head><script>x".to_vec();
        unclosed.extend_from_slice(decl);
        assert_eq!(resolve(&unclosed, None, None), encoding_rs::UTF_8);
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
                resolve(&page(near), None, None),
                encoding_rs::WINDOWS_1252,
                "a BODY declaration at {near} is inside the floor and is honoured"
            );
        }
        for far in [1024usize, 2048, 4096, 64 * 1024] {
            assert_eq!(
                resolve(&page(far), None, None),
                encoding_rs::UTF_8,
                "a BODY declaration at {far} is past the floor and declares nothing"
            );
        }
    }

    /// The unbounded single-pass scan must still match the tag NAME in any case, reject a
    /// look-alike, survive a truncated tail, and skip an arbitrary number of comments on the way.
    #[test]
    fn the_unbounded_scan_matches_only_real_meta_tags() {
        assert_eq!(resolve(b"<p><META charset=big5>", None, None), encoding_rs::BIG5);
        assert_eq!(resolve(b"<p><MeTa charset=big5>", None, None), encoding_rs::BIG5);
        assert_eq!(resolve(b"<metadata charset=big5>", None, None), encoding_rs::UTF_8);
        assert_eq!(resolve(b"<p><div><met", None, None), encoding_rs::UTF_8); // truncated tail
        assert_eq!(
            resolve(b"<html><head><title>x</title><meta charset=big5>", None, None),
            encoding_rs::BIG5
        );
        // Many comments, each holding a decoy: none declares anything, and the real one after them is
        // still reached because all of it is still the HEAD. A backwards search makes this quadratic by
        // rescanning a growing prefix for each comment.
        let mut many = b"<html><head>".to_vec();
        for _ in 0..500 {
            many.extend_from_slice(b"<!-- <meta charset=shift_jis> --><link rel=x>");
        }
        many.extend_from_slice(b"<meta charset=big5>");
        assert_eq!(resolve(&many, None, None), encoding_rs::BIG5);

        // ...and the same shape with BODY content in it stops at the floor instead, because the
        // first `<p>` ends the head.
        let mut body = b"<html><head>".to_vec();
        for _ in 0..500 {
            body.extend_from_slice(b"<!-- <meta charset=shift_jis> --><p>x</p>");
        }
        body.extend_from_slice(b"<meta charset=big5>");
        assert_eq!(resolve(&body, None, None), encoding_rs::UTF_8);
    }

    /// The WHATWG Encoding Standard has no UTF-32, so a UTF-32 BOM is not a BOM. w3lib recognizes one.
    #[test]
    fn utf32_boms_are_not_recognized() {
        // UTF-32LE begins FF FE 00 00 — the first two bytes ARE the UTF-16LE BOM, and per the standard
        // that is what it means, so such a document is read as UTF-16LE (browsers do the same).
        let le32: Vec<u8> = [0xFFu8, 0xFE, 0x00, 0x00].into_iter().collect();
        assert_eq!(resolve(&le32, None, None), encoding_rs::UTF_16LE);
        // UTF-32BE begins 00 00 FE FF, which is no BOM at all -> the ordinary prescan/autodetect path
        let be32: Vec<u8> = [0x00u8, 0x00, 0xFE, 0xFF].into_iter().collect();
        assert_eq!(resolve(&be32, None, None), autodetect(&be32, None).0);
    }
}

