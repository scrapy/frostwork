//! Minimal, correctness-first HTML tokenizer. It implements only the states needed to avoid a
//! *global* offset desync (rawtext/RCDATA for `script`/`style`/`textarea`/`title`, comments, CDATA/
//! DOCTYPE/PI skipping, attribute parsing, `<`-not-a-tag as text); it does NOT do tree construction.
//! It drives the source-agnostic `TokenSink` (start/end/text), so the matcher is decoupled from it.
//!
//! Operates on RAW BYTES (`&[u8]`) — no up-front UTF-8 validation of the whole document (that was
//! ~⅓ of extract time). Names/text/attributes are borrowed `&'a [u8]` slices; the matcher validates
//! (`from_utf8_lossy`) only the small values it actually emits. All slicing is at ASCII delimiter
//! positions (`<`, `>`, quotes, `=`, whitespace), so a multi-byte UTF-8 char is never split.

pub trait TokenSink<'a> {
    /// `attrs` are RAW (name, value) byte slices — not lowercased, not entity-decoded. `span_start`
    /// is the offset of the `<`; `open_end` is just past the start tag's `>`/`/>`.
    fn start_tag(
        &mut self,
        name: &'a [u8],
        attrs: &[(&'a [u8], &'a [u8])],
        self_closing: bool,
        span_start: usize,
        open_end: usize,
    );
    /// `close_start` is the offset of the end tag's `<`; `close_end` is just past its `>`.
    fn end_tag(&mut self, name: &'a [u8], close_start: usize, close_end: usize);
    /// One text RUN. `allows_entities` is false only for RAWTEXT (`script`/`style`). `start` is the
    /// run's offset in the document — the sink needs it both to rank the value in document order and
    /// to detect two runs separated by nothing but a DROPPED end tag (which libxml2 coalesces into a
    /// single text node; see `Matcher::text`). A run is NOT always a text node on its own.
    fn text(&mut self, text: &'a [u8], allows_entities: bool, start: usize);
}

fn is_ws(c: u8) -> bool {
    matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0c)
}

fn is_name_char(c: u8) -> bool {
    c.is_ascii_alphanumeric() || c == b'-' || c == b'_' || c == b':'
}

/// `Some(rcdata)` if `name` is a rawtext/RCDATA element (case-insensitive); rcdata=true decodes
/// entities (`textarea`/`title`), false is raw (`script`/`style`).
fn rawtext_kind(name: &[u8]) -> Option<bool> {
    if name.eq_ignore_ascii_case(b"script") || name.eq_ignore_ascii_case(b"style") {
        Some(false)
    } else if name.eq_ignore_ascii_case(b"textarea") || name.eq_ignore_ascii_case(b"title") {
        Some(true)
    } else {
        None
    }
}

enum Markup {
    Start,
    End,
    Decl,
    Bogus,
}

fn markup_kind(b: &[u8], p: usize) -> Option<Markup> {
    match b.get(p + 1) {
        Some(&c) if c.is_ascii_alphabetic() => Some(Markup::Start),
        Some(b'/') => Some(Markup::End),
        Some(b'!') => Some(Markup::Decl),
        Some(b'?') => Some(Markup::Bogus),
        _ => None,
    }
}

/// Scan an HTML comment `<!-- ... -->` starting at the `<` (caller guarantees `b[p..]` begins with
/// `<!--`). Returns the offset just past the close. Matches libxml2's close rules (differential-proven):
/// the two abrupt-close start states — `<!-->` and `<!--->` — terminate immediately, and both `-->`
/// and `--!>` (comment-end-bang) close a running comment. An unterminated comment consumes to EOF.
/// Getting this exactly right is non-negotiable: a missed close swallows the rest of the document as
/// comment content and globally desyncs every downstream offset.
fn scan_comment(b: &[u8], p: usize) -> usize {
    let n = b.len();
    let i = p + 4; // first byte of comment content, past "<!--"
    // comment-start / comment-start-dash abrupt close: `<!-->` and `<!--->`
    if i < n && b[i] == b'>' {
        return i + 1;
    }
    if i + 1 < n && b[i] == b'-' && b[i + 1] == b'>' {
        return i + 2;
    }
    // running comment: earliest of `-->` or `--!>` closes (extra leading dashes fold into the match).
    let mut j = i;
    while j < n {
        if b[j..].starts_with(b"-->") {
            return j + 3;
        }
        if b[j..].starts_with(b"--!>") {
            return j + 4;
        }
        j += 1;
    }
    n
}

fn skip_to_gt(b: &[u8], from: usize) -> usize {
    match memchr::memchr(b'>', &b[from..]) {
        Some(k) => from + k + 1,
        None => b.len(),
    }
}

/// Scan RAWTEXT/RCDATA content for the matching `</name` end tag (case-insensitive). Returns
/// (text_end, after_end_tag).
fn find_raw_end(b: &[u8], from: usize, name: &[u8]) -> (usize, usize) {
    let n = b.len();
    let mut i = from;
    loop {
        match memchr::memchr(b'<', &b[i..]) {
            None => return (n, n),
            Some(k) => {
                let p = i + k;
                if b.get(p + 1) == Some(&b'/') {
                    let mut q = p + 2;
                    let mut ok = q + name.len() <= n;
                    if ok {
                        for &nc in name {
                            if (b[q] | 0x20) != (nc | 0x20) {
                                ok = false;
                                break;
                            }
                            q += 1;
                        }
                    }
                    if ok && (q >= n || is_ws(b[q]) || b[q] == b'>' || b[q] == b'/') {
                        return (p, skip_to_gt(b, q));
                    }
                }
                i = p + 1;
            }
        }
    }
}

pub fn tokenize<'a, S: TokenSink<'a>>(b: &'a [u8], sink: &mut S) {
    let n = b.len();
    let mut attr_buf: Vec<(&'a [u8], &'a [u8])> = Vec::new(); // reused across start tags
    let mut i = 0;
    let mut text_from = 0;
    while i < n {
        match memchr::memchr(b'<', &b[i..]) {
            None => break,
            Some(k) => {
                let p = i + k;
                match markup_kind(b, p) {
                    None => {
                        i = p + 1; // literal '<' — stays part of the current text run
                    }
                    Some(kind) => {
                        if p > text_from {
                            sink.text(&b[text_from..p], true, text_from);
                        }
                        let after = handle_markup(kind, b, p, sink, &mut attr_buf);
                        i = after;
                        text_from = after;
                    }
                }
            }
        }
    }
    if n > text_from {
        sink.text(&b[text_from..n], true, text_from);
    }
}

fn handle_markup<'a, S: TokenSink<'a>>(
    kind: Markup,
    b: &'a [u8],
    p: usize,
    sink: &mut S,
    attr_buf: &mut Vec<(&'a [u8], &'a [u8])>,
) -> usize {
    match kind {
        Markup::Decl => {
            if b[p..].starts_with(b"<!--") {
                scan_comment(b, p)
            } else {
                skip_to_gt(b, p)
            }
        }
        Markup::Bogus => skip_to_gt(b, p),
        Markup::End => {
            let n = b.len();
            let mut i = p + 2;
            let start = i;
            while i < n && is_name_char(b[i]) {
                i += 1;
            }
            let name = &b[start..i];
            let after = skip_to_gt(b, i);
            if !name.is_empty() {
                sink.end_tag(name, p, after); // p = '<' of the end tag
            }
            after
        }
        Markup::Start => handle_start(b, p, sink, attr_buf),
    }
}

fn handle_start<'a, S: TokenSink<'a>>(
    b: &'a [u8],
    p: usize,
    sink: &mut S,
    attr_buf: &mut Vec<(&'a [u8], &'a [u8])>,
) -> usize {
    let n = b.len();
    let mut i = p + 1;
    let ns = i;
    while i < n && is_name_char(b[i]) {
        i += 1;
    }
    let name = &b[ns..i];

    attr_buf.clear();
    let mut self_closing = false;
    loop {
        while i < n && is_ws(b[i]) {
            i += 1;
        }
        if i >= n {
            break;
        }
        if b[i] == b'>' {
            i += 1;
            break;
        }
        if b[i] == b'/' {
            if b.get(i + 1) == Some(&b'>') {
                self_closing = true;
                i += 2;
                break;
            }
            i += 1;
            continue;
        }
        let as_ = i;
        while i < n && !is_ws(b[i]) && b[i] != b'=' && b[i] != b'>' && b[i] != b'/' {
            i += 1;
        }
        let aname = &b[as_..i];
        let mut aval: &[u8] = b"";
        let mut j = i;
        while j < n && is_ws(b[j]) {
            j += 1;
        }
        if j < n && b[j] == b'=' {
            j += 1;
            while j < n && is_ws(b[j]) {
                j += 1;
            }
            if j < n && (b[j] == b'"' || b[j] == b'\'') {
                let q = b[j];
                j += 1;
                let vs = j;
                while j < n && b[j] != q {
                    j += 1;
                }
                aval = &b[vs..j];
                if j < n {
                    j += 1;
                }
            } else {
                let vs = j;
                while j < n && !is_ws(b[j]) && b[j] != b'>' {
                    j += 1;
                }
                aval = &b[vs..j];
            }
            i = j;
        }
        if !aname.is_empty() {
            attr_buf.push((aname, aval));
        }
    }

    let raw = rawtext_kind(name);
    sink.start_tag(name, attr_buf, self_closing, p, i);

    if let Some(rcdata) = raw {
        if !self_closing {
            let (text_end, after) = find_raw_end(b, i, name);
            if text_end > i {
                sink.text(&b[i..text_end], rcdata, i);
            }
            if after > text_end {
                sink.end_tag(name, text_end, after);
            }
            return after;
        }
    }
    i
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A `TokenSink` that records the event stream in a compact readable form, so L1 vectors can pin
    /// the exact tokenization of the "prevent global desync" states (docs/TESTING.md L1). A mistake in
    /// any of these is a *global* failure (offset desync), so they get locked here at the source.
    #[derive(Default)]
    struct Rec {
        ev: Vec<String>,
    }
    impl<'a> TokenSink<'a> for Rec {
        fn start_tag(
            &mut self,
            name: &'a [u8],
            attrs: &[(&'a [u8], &'a [u8])],
            self_closing: bool,
            _s: usize,
            _e: usize,
        ) {
            let a: Vec<String> = attrs
                .iter()
                .map(|(n, v)| format!("{}={}", String::from_utf8_lossy(n), String::from_utf8_lossy(v)))
                .collect();
            self.ev.push(format!(
                "S:{}{}[{}]",
                String::from_utf8_lossy(name),
                if self_closing { "/" } else { "" },
                a.join(",")
            ));
        }
        fn end_tag(&mut self, name: &'a [u8], _s: usize, _e: usize) {
            self.ev.push(format!("E:{}", String::from_utf8_lossy(name)));
        }
        fn text(&mut self, t: &'a [u8], allows_entities: bool, _start: usize) {
            // `!` marks RAWTEXT (script/style): entities are NOT expanded downstream
            self.ev.push(format!("T{}:{}", if allows_entities { "" } else { "!" }, String::from_utf8_lossy(t)));
        }
    }

    fn toks(html: &[u8]) -> Vec<String> {
        let mut r = Rec::default();
        tokenize(html, &mut r);
        r.ev
    }

    #[test]
    fn rawtext_keeps_markup_as_text() {
        // inside <script> (RAWTEXT), `<b>` is NOT a tag and `<` needn't be escaped; the run ends only
        // at </script>. Entities are not expandable here (the `!` marker).
        assert_eq!(
            toks(br#"<script>if (a<b) x="</b>"</script>"#),
            ["S:script[]", "T!:if (a<b) x=\"</b>\"", "E:script"]
        );
        // <title> is RCDATA: `<b>` is text too (but entities WOULD expand -> no `!`)
        assert_eq!(toks(b"<title>a<b>c</title>"), ["S:title[]", "T:a<b>c", "E:title"]);
    }

    #[test]
    fn comments_and_cdata_emit_no_tokens() {
        // a comment is skipped whole (its `<b>` is not a tag); text on either side is preserved
        assert_eq!(toks(b"<p>a<!-- <b>x</b> -->b</p>"), ["S:p[]", "T:a", "T:b", "E:p"]);
        // `<![CDATA[..]]>` is a bogus comment in HTML (libxml2 semantics) — skipped, not text
        assert_eq!(toks(b"<p>x<![CDATA[ y<z ]]>w</p>"), ["S:p[]", "T:x", "T:w", "E:p"]);
    }

    #[test]
    fn comment_close_boundary_no_desync() {
        // abrupt-close start states (`<!-->`, `<!--->`) and comment-end-bang (`--!>`) must terminate
        // the comment; missing any of these swallows the rest of the doc and globally desyncs offsets.
        // Differential-proven against libxml2 (see scan_comment).
        let after = ["S:p[]", "T:X", "E:p"];
        assert_eq!(toks(b"<!--><p>X</p>"), after); // abrupt empty comment
        assert_eq!(toks(b"<!---><p>X</p>"), after); // abrupt after one dash
        assert_eq!(toks(b"<!----><p>X</p>"), after); // normal empty comment
        assert_eq!(toks(b"<!--a--!><p>X</p>"), after); // --!> close
        // `--!` without `>` is NOT a close: comment runs to EOF, so nothing downstream tokenizes.
        assert_eq!(toks(b"<!--a-!><p>X</p>"), Vec::<String>::new());
        // `--!>` closes, then trailing `y-->z` is ordinary text before <p>.
        assert_eq!(toks(b"<!--x--!>y-->z<p>X</p>"), ["T:y-->z", "S:p[]", "T:X", "E:p"]);
    }

    #[test]
    fn entities_left_raw_for_the_matcher() {
        // the tokenizer does NOT decode entities — it emits raw text; the matcher decodes on emit
        assert_eq!(toks(b"<p>a&amp;b &#38; c</p>"), ["S:p[]", "T:a&amp;b &#38; c", "E:p"]);
    }

    #[test]
    fn attribute_parsing_forms() {
        // double- and single-quoted values, and a bare boolean attribute (empty value)
        assert_eq!(toks(br#"<a href="/x" data-k='v' flag>t</a>"#), ["S:a[href=/x,data-k=v,flag=]", "T:t", "E:a"]);
        // self-closing syntax is reported (the matcher closes it immediately)
        assert_eq!(toks(br#"<img src="a.png"/>x"#), ["S:img/[src=a.png]", "T:x"]);
    }

    #[test]
    fn lt_not_a_tag_is_text() {
        // `<` not starting a valid tag name stays literal text (no desync into a phantom tag)
        assert_eq!(toks(b"<p>a < b</p>"), ["S:p[]", "T:a < b", "E:p"]);
    }
}
