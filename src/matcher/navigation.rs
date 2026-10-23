//! The base URL and meta refresh of a document, as a browser reads them.
//!
//! Unlike the rest of the engine, the standard here is the BROWSER, not lxml: these two values decide
//! which URL a scraper resolves links against and which page it moves on to, so the right answer is
//! the one Chromium and Firefox act on. Where the two browsers disagree, Firefox is followed. Every rule
//! below was measured in both (the `navigation_browser_table_*` tests list the cases), and the whole
//! document is read, as browsers do: a `<base>` or a refresh after the first few kilobytes counts.
//!
//! The collector is fed from the matcher's start-tag handling, after implied closes have run, so the
//! open-element stack it is shown is exactly the element's ancestors.

use std::borrow::Cow;

use encoding_rs::Encoding;
use url::Url;

/// What [`crate::navigation`] and [`crate::Plan::extract_with_navigation`] collect.
#[derive(Clone, Copy, Debug)]
pub struct NavOptions {
    /// Report the document's base URL.
    pub base_url: bool,
    /// Report the meta refresh.
    pub meta_refresh: bool,
    /// Read the document as a browser with JavaScript enabled does, where the content of `<noscript>`
    /// is text. With `false`, a `<base>` or refresh inside `<noscript>` counts.
    pub scripting: bool,
}

/// The base URL and meta refresh of one document. A field the caller did not ask for is `None`.
#[derive(Clone, Debug, PartialEq)]
pub struct Navigation {
    /// The document's base URL: its first usable `<base href>`, resolved, or the document URL when it
    /// has none.
    pub base_url: Option<String>,
    /// The refresh a browser would follow, as `(delay in seconds, absolute URL)`.
    pub meta_refresh: Option<(u64, String)>,
}

pub(crate) struct NavCollector {
    opts: NavOptions,
    /// The document URL, or `None` when the caller's is not absolute, in which case only absolute URLs
    /// in the document resolve.
    doc_url: Option<Url>,
    /// The document's own encoding, which the query of a `<base href>` is encoded in. It differs from
    /// the encoding attribute values are decoded with when the document was transcoded to UTF-8.
    doc_encoding: &'static Encoding,
    /// Whether the first `<base>` with an `href` has been seen, which settles the base URL.
    base_seen: bool,
    /// The base URL that `<base>` set, or `None` for the document URL.
    base: Option<Url>,
    /// The refresh to follow, with `None` for the document itself.
    refresh: Option<(u64, Option<Url>)>,
}

impl NavCollector {
    pub(crate) fn new(doc_url: Option<Url>, doc_encoding: &'static Encoding, opts: NavOptions) -> Self {
        NavCollector { opts, doc_url, doc_encoding, base_seen: false, base: None, refresh: None }
    }

    /// Whether a start tag named `name` can change the answer. Checked before anything else, so every
    /// other element costs one comparison of its name length.
    pub(crate) fn wants(&self, name: &[u8]) -> bool {
        (!self.base_seen && name.eq_ignore_ascii_case(b"base"))
            || (self.opts.meta_refresh && name.eq_ignore_ascii_case(b"meta"))
    }

    /// Whether the rest of the document can no longer change the answer.
    pub(crate) fn done(&self) -> bool {
        !self.opts.meta_refresh && self.base_seen
    }

    pub(crate) fn start_tag<'t>(
        &mut self,
        name: &[u8],
        attrs: &[(&[u8], Option<&[u8]>)],
        mut ancestors: impl Iterator<Item = &'t [u8]>,
        value_encoding: &'static Encoding,
    ) {
        let is_base = name.eq_ignore_ascii_case(b"base");
        // `<template>` content is not part of the document. A `<noscript>` is text to a browser running
        // scripts. A `<base>` inside `<svg>` or `<math>` is a foreign element, while a `<meta>` breaks out
        // of foreign content and stays an HTML element.
        let ignored = |tag: &[u8]| {
            tag.eq_ignore_ascii_case(b"template")
                || (self.opts.scripting && tag.eq_ignore_ascii_case(b"noscript"))
                || (is_base && (tag.eq_ignore_ascii_case(b"svg") || tag.eq_ignore_ascii_case(b"math")))
        };
        if ancestors.any(ignored) {
            return;
        }
        // The tokenizer reports duplicate attributes, and browsers keep the first one.
        let attr = |wanted: &[u8]| {
            attrs
                .iter()
                .find(|(n, _)| n.eq_ignore_ascii_case(wanted))
                .map(|&(_, v)| v.map_or(Cow::Borrowed(""), |v| super::decode::decode_attr(v, value_encoding)))
        };
        if is_base {
            if let Some(href) = attr(b"href") {
                self.base = self.parse_base(&href);
                self.base_seen = true;
            }
            return;
        }
        if !attr(b"http-equiv").is_some_and(|v| v.eq_ignore_ascii_case("refresh")) {
            return;
        }
        let Some(content) = attr(b"content") else { return };
        let Some((delay, target)) = parse_refresh(&content) else { return };
        let url = match target {
            None => None,
            // Firefox encodes the query as UTF-8, where Chromium uses the document encoding.
            Some(target) => match parse_url(Url::options(), self.current_base(), target) {
                Ok(url) if !matches!(url.scheme(), "javascript" | "data") => Some(url),
                // A refresh browsers cannot follow leaves any earlier one in place.
                _ => return,
            },
        };
        // The refresh that fires first wins, and of two with the same delay, the later one.
        if self.refresh.as_ref().is_none_or(|(best, _)| delay <= *best) {
            self.refresh = Some((delay, url));
        }
    }

    /// The base URL as of the current position: a refresh resolves against the base URL in effect when
    /// its `<meta>` is parsed, so a `<base>` written after it does not apply to it.
    fn current_base(&self) -> Option<&Url> {
        self.base.as_ref().or(self.doc_url.as_ref())
    }

    /// The base URL `href` sets, or `None` when it is unusable and the document URL stays.
    fn parse_base(&self, href: &str) -> Option<Url> {
        let encoding = self.doc_encoding.output_encoding();
        let encode: &dyn Fn(&str) -> Cow<'_, [u8]> = &|s| encoding.encode(s).0;
        let parsed = parse_url(Url::options().encoding_override(Some(encode)), self.doc_url.as_ref(), href);
        parsed.ok().filter(|url| !matches!(url.scheme(), "javascript" | "data"))
    }

    pub(crate) fn finish(self, given_url: &str) -> Navigation {
        let document = || document_url(self.doc_url.as_ref(), given_url);
        let url = |url: Option<Url>| url.map_or_else(document, String::from);
        Navigation {
            base_url: self.opts.base_url.then(|| url(self.base.clone())),
            meta_refresh: if self.opts.meta_refresh {
                self.refresh.clone().map(|(delay, target)| (delay, url(target)))
            } else {
                None
            },
        }
    }
}

/// The document URL as reported: serialized when the caller's `given` URL parsed as `doc_url`, and as
/// given otherwise, since a URL that does not parse has no serialization.
pub(crate) fn document_url(doc_url: Option<&Url>, given: &str) -> String {
    doc_url.map_or_else(|| given.to_owned(), |url| url.as_str().to_owned())
}

/// Parse `input` against `base` as the URL standard does.
///
/// The `url` crate strips exactly two of the leading slashes of a relative URL against a special base,
/// so `///example.com/x` gets an empty host and fails, where the standard skips them all. Against such
/// a base, two or more leading slashes mean the base's scheme followed by the input, which the crate
/// parses correctly.
fn parse_url(
    options: url::ParseOptions<'_>,
    base: Option<&Url>,
    input: &str,
) -> Result<Url, url::ParseError> {
    let is_slash = |c: char| matches!(c, '/' | '\\');
    let trimmed = input.trim_start_matches(|c: char| c <= ' ');
    if let Some(base) = base.filter(|b| matches!(b.scheme(), "http" | "https" | "ws" | "wss" | "ftp")) {
        if trimmed.starts_with(is_slash) {
            // The standard also drops every tab and newline before reading the slashes.
            let rest: String = trimmed.chars().filter(|c| !matches!(c, '\t' | '\n' | '\r')).collect();
            if rest.chars().take(2).filter(|&c| is_slash(c)).count() == 2 {
                return options.parse(&format!("{}:{rest}", base.scheme()));
            }
        }
    }
    options.base_url(base).parse(input)
}

/// Parse the `content` of a refresh `<meta>` into its delay and, if it names one, its URL, following
/// the HTML standard's shared declarative refresh steps, which both browsers implement.
fn parse_refresh(content: &str) -> Option<(u64, Option<&str>)> {
    let is_ws = |c: char| matches!(c, ' ' | '\t' | '\n' | '\x0c' | '\r');
    let s = content.trim_start_matches(is_ws);
    let digits = s.len() - s.trim_start_matches(|c: char| c.is_ascii_digit()).len();
    if digits == 0 && !s.starts_with('.') {
        return None;
    }
    let delay = s[..digits]
        .bytes()
        .fold(0u64, |n, d| n.saturating_mul(10).saturating_add(u64::from(d - b'0')));
    // A fractional part is read and discarded.
    let mut s = s[digits..].trim_start_matches(|c: char| c.is_ascii_digit() || c == '.');
    if s.is_empty() {
        return Some((delay, None));
    }
    if !s.starts_with([';', ',']) && !s.starts_with(is_ws) {
        return None;
    }
    s = s.trim_start_matches(is_ws);
    s = s.strip_prefix([';', ',']).unwrap_or(s).trim_start_matches(is_ws);
    if s.is_empty() {
        return Some((delay, None));
    }
    Some((delay, Some(url_part(s))))
}

/// The URL of a refresh `content` after its delay. A leading `url=` is dropped, and so is a leading
/// quote, along with everything from its closing match on. A `u`, `ur` or `url` that no `=` follows
/// belongs to the URL, so `urlx=/a` is the relative URL `urlx=/a`.
fn url_part(s: &str) -> &str {
    let is_ws = |c: char| matches!(c, ' ' | '\t' | '\n' | '\x0c' | '\r');
    let rest = if s.starts_with(['u', 'U']) {
        let prefix = s.get(..3).filter(|p| p.eq_ignore_ascii_case("url"));
        match prefix.and_then(|_| s[3..].trim_start_matches(is_ws).strip_prefix('=')) {
            Some(rest) => rest.trim_start_matches(is_ws),
            None => return s,
        }
    } else {
        s
    };
    match rest.chars().next() {
        Some(quote @ ('\'' | '"')) => {
            let rest = &rest[1..];
            rest.find(quote).map_or(rest, |end| &rest[..end])
        }
        _ => rest,
    }
}

#[cfg(test)]
mod tests {
    use crate::{NavOptions, Navigation};

    const DOC: &str = "http://h/p";

    fn nav(html: &[u8], encoding: Option<&str>, scripting: bool) -> Navigation {
        let options = NavOptions { base_url: true, meta_refresh: true, scripting };
        crate::navigation(html, encoding, DOC, options)
    }

    fn page(head: &str, body: &str) -> String {
        format!("<!doctype html><html><head>{head}</head><body>{body}</body></html>")
    }

    fn refresh(content: &str) -> String {
        page(&format!(r#"<meta http-equiv="refresh" content="{content}">"#), "")
    }

    fn pad() -> String {
        format!("<p>{}</p>", "lorem ipsum dolor sit amet ".repeat(40000))
    }

    /// What Chromium 153 and Firefox 144 resolve each `<base>` to (2026-10-22), read through a link's
    /// `href`; where they differ, the comment says so and Firefox is the expectation.
    #[test]
    fn navigation_browser_table_base() {
        let late = page("<title>t</title>", &(pad() + r#"<a href="x">x</a><base href="/sub/">"#));
        let cases: &[(&str, String, &str)] = &[
            ("none", page("", ""), DOC),
            ("plain", page(r#"<base href="/sub/">"#, ""), "http://h/sub/"),
            ("late, in the body, after the link it affects", late, "http://h/sub/"),
            ("two", page(r#"<base href="/one/"><base href="/two/">"#, ""), "http://h/one/"),
            ("empty", page(r#"<base href="">"#, ""), DOC),
            ("valueless", page("<base href>", ""), DOC),
            ("surrounding whitespace", page(r#"<base href="  /sub/  ">"#, ""), "http://h/sub/"),
            ("relative", page(r#"<base href="sub/">"#, ""), "http://h/sub/"),
            // Chromium leaves every link unresolvable.
            ("unparseable", page(r#"<base href="http://[::1">"#, ""), DOC),
            ("data:", page(r#"<base href="data:text/html,x">"#, ""), DOC),
            ("javascript:", page(r#"<base href="javascript:void(0)">"#, ""), DOC),
            ("no href, then href", page(r#"<base target=_top><base href="/second/">"#, ""), "http://h/second/"),
            ("empty, then valid", page(r#"<base href=""><base href="/second/">"#, ""), DOC),
            ("unparseable, then valid", page(r#"<base href="http://[::1"><base href="/second/">"#, ""), DOC),
            ("noscript", page(r#"<noscript><base href="/ns/"></noscript>"#, ""), DOC),
            ("noscript in body", page("", r#"<noscript><base href="/ns/"></noscript>"#), DOC),
            ("template", page(r#"<template><base href="/tpl/"></template>"#, ""), DOC),
            ("svg", page("", r#"<svg><base href="/svg/"></base></svg>"#), DOC),
            ("entity", page(r#"<base href="/a&amp;b/">"#, ""), "http://h/a&b/"),
            ("tab and newline", page("<base href=\"/s\tu\nb/\">", ""), "http://h/sub/"),
            ("upper case", page(r#"<BASE HREF="/up/">"#, ""), "http://h/up/"),
            ("three slashes", page(r#"<base href="///other/x/">"#, ""), "http://other/x/"),
            ("backslashes and a tab", page("<base href=\"\\/\t\\other/x/\">", ""), "http://other/x/"),
            ("two slashes", page(r#"<base href="//other/x/">"#, ""), "http://other/x/"),
        ];
        for (name, html, expected) in cases {
            assert_eq!(nav(html.as_bytes(), None, true).base_url.as_deref(), Some(*expected), "{name}");
        }
        let noscript = page(r#"<noscript><base href="/ns/"></noscript>"#, "");
        assert_eq!(nav(noscript.as_bytes(), None, false).base_url.as_deref(), Some("http://h/ns/"));
    }

    /// The query of a `<base href>` is encoded in the document encoding, as is that of every link
    /// resolved against it (measured on the links; the base URL itself is not observable).
    #[test]
    fn base_query_uses_the_document_encoding() {
        let html = page(r#"<meta charset=windows-1252><base href="/sub/?b=é">"#, "");
        let (bytes, _, _) = encoding_rs::WINDOWS_1252.encode(&html);
        assert_eq!(nav(&bytes, None, true).base_url.as_deref(), Some("http://h/sub/?b=%E9"));
        let utf16: Vec<u8> = "\u{feff}<base href=\"/sub/?b=é\">".encode_utf16().flat_map(u16::to_le_bytes).collect();
        assert_eq!(nav(&utf16, None, true).base_url.as_deref(), Some("http://h/sub/?b=%C3%A9"));
    }

    /// What Chromium 153 and Firefox 144 request for each meta refresh (2026-10-22); `None` means no
    /// refresh. Where they differ, the comment says so and Firefox is the expectation.
    #[test]
    fn navigation_browser_table_refresh() {
        let a = Some("http://h/t/a");
        let cases: &[(&str, String, Option<&str>)] = &[
            ("plain", refresh("0;url=/t/a"), a),
            ("URL", refresh("0;URL=/t/a"), a),
            ("single quotes", refresh("0;url='/t/a'"), a),
            ("double quotes", refresh("0;url=&quot;/t/a&quot;"), a),
            ("comma", refresh("0,url=/t/a"), a),
            ("space", refresh("0 url=/t/a"), a),
            ("no url=", refresh("0;/t/a"), a),
            ("spaces around =", refresh("0; URL = /t/a"), a),
            (".5", refresh(".5;url=/t/a"), a),
            ("0.9.9", refresh("0.9.9;url=/t/a"), a),
            ("negative", refresh("-1;url=/t/a"), None),
            ("no delay", refresh(";url=/t/a"), None),
            ("exponent", refresh("1e0;url=/t/a"), None),
            ("digits, then junk", refresh("0x;url=/t/a"), None),
            ("leading space", refresh("  0 ; url=/t/a"), a),
            ("space inside the URL", refresh("0;url=/t/a b"), Some("http://h/t/a%20b")),
            ("trailing spaces", refresh("0;url=/t/a   "), a),
            ("unterminated quote", refresh("0;url='/t/a"), a),
            ("trailing quote", refresh("0;url=/t/a'"), Some("http://h/t/a'")),
            ("urlx=", refresh("0;urlx=/t/a"), Some("http://h/urlx=/t/a")),
            ("empty url=, a reload", refresh("0;url="), Some(DOC)),
            ("delay only, a reload", refresh("0"), Some(DOC)),
            ("entity", refresh("0;url=/t/a?x=1&amp;y=2"), Some("http://h/t/a?x=1&y=2")),
            ("tab", refresh("0;\turl=/t/a"), a),
            ("leading zeros", refresh("00000000000000000000000000000001;url=/t/a"), a),
            ("plus", refresh("+0;url=/t/a"), None),
            ("http-equiv upper case", page(r#"<meta http-equiv="REFRESH" content="0;url=/t/a">"#, ""), a),
            ("http-equiv with spaces", page(r#"<meta http-equiv=" refresh " content="0;url=/t/a">"#, ""), None),
            ("http-equiv suffix", page(r#"<meta http-equiv="refresh-x" content="0;url=/t/a">"#, ""), None),
            ("http-equiv prefix", page(r#"<meta http-equiv="x-refresh" content="0;url=/t/a">"#, ""), None),
            ("name instead", page(r#"<meta name="refresh" content="0;url=/t/a">"#, ""), None),
            ("content first", page(r#"<meta content="0;url=/t/a" http-equiv="refresh">"#, ""), a),
            ("two contents", page(r#"<meta http-equiv="refresh" content="0;url=/t/a" content="0;url=/t/b">"#, ""), a),
            ("two http-equivs", page(r#"<meta http-equiv="x" http-equiv="refresh" content="0;url=/t/a">"#, ""), None),
            ("no content", page(r#"<meta http-equiv="refresh">"#, ""), None),
            ("svg", page("", r#"<svg><meta http-equiv="refresh" content="0;url=/t/a"></meta></svg>"#), a),
            ("math", page("", r#"<math><meta http-equiv="refresh" content="0;url=/t/a"></meta></math>"#), a),
            ("title", page(r#"<title><meta http-equiv="refresh" content="0;url=/t/a"></title>"#, ""), None),
            ("textarea", page("", r#"<textarea><meta http-equiv="refresh" content="0;url=/t/a"></textarea>"#), None),
            ("comment", page(r#"<!-- <meta http-equiv="refresh" content="0;url=/t/a"> -->"#, ""), None),
            ("script", page(r#"<script>/*<meta http-equiv="refresh" content="0;url=/t/a">*/</script>"#, ""), None),
            ("template", page(r#"<template><meta http-equiv="refresh" content="0;url=/t/a"></template>"#, ""), None),
            ("noscript", page(r#"<noscript><meta http-equiv="refresh" content="0;url=/t/a"></noscript>"#, ""), None),
            ("noscript in body", page("", r#"<noscript><meta http-equiv="refresh" content="0;url=/t/a"></noscript>"#), None),
            ("relative", refresh("0;url=x"), Some("http://h/x")),
            ("three slashes", refresh("0;url=///other/x"), Some("http://other/x")),
            ("base before", page(r#"<base href="/sub/"><meta http-equiv="refresh" content="0;url=x">"#, ""), Some("http://h/sub/x")),
            ("base after", page(r#"<meta http-equiv="refresh" content="0;url=x"><base href="/sub/">"#, ""), Some("http://h/x")),
            ("fragment only", refresh("0;url=#frag"), Some("http://h/p#frag")),
            ("javascript:", refresh("0;url=javascript:location='/t/js'"), None),
            ("data:", refresh("0;url=data:text/html,hi"), None),
            ("late, in the body", page("<title>t</title>", &(pad() + r#"<meta http-equiv="refresh" content="0;url=/t/a">"#)), a),
            ("after </html>", page("", "x") + r#"<meta http-equiv="refresh" content="0;url=/t/a">"#, a),
        ];
        for (name, html, expected) in cases {
            let got = nav(html.as_bytes(), None, true).meta_refresh;
            assert_eq!(got.as_ref().map(|(_, url)| url.as_str()), *expected, "{name}");
        }
        let noscript = page("", r#"<noscript><meta http-equiv="refresh" content="0;url=/t/a"></noscript>"#);
        assert_eq!(nav(noscript.as_bytes(), None, false).meta_refresh, Some((0, "http://h/t/a".into())));
    }

    /// Of several refreshes, browsers follow the one that fires first, and of two with the same delay,
    /// the later one (14 orderings, both browsers, 2026-10-22).
    #[test]
    fn navigation_browser_table_several_refreshes() {
        let metas = |delays: &[(&str, &str)]| {
            delays.iter().map(|(label, d)| format!(r#"<meta http-equiv="refresh" content="{d};url=/t/{label}">"#)).collect::<String>()
        };
        let head = |delays: &[(&str, &str)]| page(&metas(delays), "");
        let late = |first: (&str, &str), second: (&str, &str)| page(&metas(&[first]), &(pad() + &metas(&[second])));
        let cases: &[(&str, String, &str)] = &[
            ("0 0", head(&[("a", "0"), ("b", "0")]), "b"),
            ("0 1", head(&[("a", "0"), ("b", "1")]), "a"),
            ("1 0", head(&[("a", "1"), ("b", "0")]), "b"),
            ("1 1", head(&[("a", "1"), ("b", "1")]), "b"),
            ("1 1 1", head(&[("a", "1"), ("b", "1"), ("c", "1")]), "c"),
            ("2 1 2", head(&[("a", "2"), ("b", "1"), ("c", "2")]), "b"),
            ("1 2 1", head(&[("a", "1"), ("b", "2"), ("c", "1")]), "c"),
            ("2 1 1", head(&[("a", "2"), ("b", "1"), ("c", "1")]), "c"),
            ("1 1 2", head(&[("a", "1"), ("b", "1"), ("c", "2")]), "b"),
            ("1.5 1", head(&[("a", "1.5"), ("b", "1")]), "b"),
            ("0, late 0", late(("a", "0"), ("b", "0")), "b"),
            ("0, late 1", late(("a", "0"), ("b", "1")), "a"),
            ("1, late 0", late(("a", "1"), ("b", "0")), "b"),
            ("1 in the body, late 1", page("", &(metas(&[("a", "1")]) + &pad() + &metas(&[("b", "1")]))), "b"),
            // Chromium follows neither.
            ("1, then an unparseable URL at 0", page(&(metas(&[("a", "1")]) + r#"<meta http-equiv="refresh" content="0;url=http://[::1">"#), ""), "a"),
        ];
        for (name, html, label) in cases {
            let expected = format!("http://h/t/{label}");
            assert_eq!(nav(html.as_bytes(), None, true).meta_refresh.map(|(_, url)| url), Some(expected), "{name}");
        }
        let nonsense = page(&(metas(&[("a", "1")]) + r#"<meta http-equiv="refresh" content="0;nonsense">"#), "");
        assert_eq!(nav(nonsense.as_bytes(), None, true).meta_refresh, Some((0, "http://h/nonsense".into())));
    }

    /// The query of a refresh URL is UTF-8 in Firefox whatever the document encoding (Chromium uses the
    /// document encoding), and a path is UTF-8 in both.
    #[test]
    fn refresh_query_is_utf8() {
        let cases: &[(&encoding_rs::Encoding, &str, &str, &str)] = &[
            (encoding_rs::WINDOWS_1252, "windows-1252", "/t/a?q=é", "http://h/t/a?q=%C3%A9"),
            (encoding_rs::KOI8_R, "koi8-r", "/t/a?q=ж", "http://h/t/a?q=%D0%B6"),
            (encoding_rs::WINDOWS_1252, "windows-1252", "/t/é", "http://h/t/%C3%A9"),
        ];
        for (encoding, label, url, expected) in cases {
            let html = page(&format!(r#"<meta charset={label}><meta http-equiv="refresh" content="0;url={url}">"#), "");
            let (bytes, _, _) = encoding.encode(&html);
            assert_eq!(nav(&bytes, None, true).meta_refresh.map(|(_, u)| u).as_deref(), Some(*expected), "{label} {url}");
        }
    }

    #[test]
    fn a_tag_name_joined_by_nul_deletion_still_counts() {
        let html = b"<ba\0se href=/sub/><me\0ta http-equiv=refresh content=0;url=x>";
        assert_eq!(nav(html, None, true), Navigation { base_url: Some("http://h/sub/".into()), meta_refresh: Some((0, "http://h/sub/x".into())) });
        let options = NavOptions { base_url: true, meta_refresh: false, scripting: true };
        assert_eq!(crate::navigation(b"<p>none</p>", None, "HTTP://H/a b", options).base_url.as_deref(), Some("http://h/a%20b"));
        let reload = crate::navigation(br#"<meta http-equiv="refresh" content="5">"#, None, "http://h", NavOptions { meta_refresh: true, ..options });
        assert_eq!(reload.meta_refresh, Some((5, "http://h/".into())));
    }

    #[test]
    fn delay_is_whole_seconds() {
        let delay = |content: &str| nav(refresh(content).as_bytes(), None, true).meta_refresh.map(|(d, _)| d);
        assert_eq!(delay("5;url=/x"), Some(5));
        assert_eq!(delay("1.9;url=/x"), Some(1));
        assert_eq!(delay("99999999999999999999999;url=/x"), Some(u64::MAX));
    }

    #[test]
    fn options_select_what_is_reported() {
        let html = page(r#"<base href="/sub/"><meta http-equiv="refresh" content="3;url=x">"#, "");
        let run = |base_url, meta_refresh| {
            crate::navigation(html.as_bytes(), None, DOC, NavOptions { base_url, meta_refresh, scripting: true })
        };
        assert_eq!(run(true, false), Navigation { base_url: Some("http://h/sub/".into()), meta_refresh: None });
        // The refresh still resolves against the base URL it does not report.
        assert_eq!(run(false, true), Navigation { base_url: None, meta_refresh: Some((3, "http://h/sub/x".into())) });
    }

    #[test]
    fn a_relative_document_url_resolves_only_absolute_urls() {
        let options = NavOptions { base_url: true, meta_refresh: true, scripting: true };
        let run = |html: &str| crate::navigation(html.as_bytes(), None, "/p", options);
        let relative = r#"<base href="/b/"><meta http-equiv="refresh" content="0;url=x">"#;
        assert_eq!(run(relative), Navigation { base_url: Some("/p".into()), meta_refresh: None });
        let absolute = r#"<base href="http://h/b/"><meta http-equiv="refresh" content="0;url=x">"#;
        assert_eq!(run(absolute), Navigation { base_url: Some("http://h/b/".into()), meta_refresh: Some((0, "http://h/b/x".into())) });
        assert_eq!(run(r#"<meta http-equiv="refresh" content="5">"#).meta_refresh, Some((5, "/p".into())));
    }

    #[test]
    fn rides_the_same_pass_as_the_fields() {
        let html = page(r#"<title>T</title><base href="/sub/"><meta http-equiv="refresh" content="3;url=x">"#, "<h1>H</h1>");
        let plan = crate::Plan::compile(&["title::text", "h1::text"], &[]);
        let options = NavOptions { base_url: true, meta_refresh: true, scripting: true };
        let (flat, _, nav) = plan.extract_with_navigation(html.as_bytes(), None, DOC, options);
        assert_eq!(flat, vec![vec!["T".to_owned()], vec!["H".to_owned()]]);
        assert_eq!(nav.base_url.as_deref(), Some("http://h/sub/"));
        assert_eq!(nav.meta_refresh, Some((3, "http://h/sub/x".into())));
        // An early-exit plan must not stop before a refresh later in the document.
        let late = page("<title>T</title>", &(pad() + r#"<meta http-equiv="refresh" content="0;url=/late">"#));
        let first = crate::Plan::compile_first_only(&["title::text"], &[], &[true]);
        let (flat, _, nav) = first.extract_with_navigation(late.as_bytes(), None, DOC, options);
        assert_eq!(flat, vec![vec!["T".to_owned()]]);
        assert_eq!(nav.meta_refresh, Some((0, "http://h/late".into())));
    }
}
