//! Minimal, correctness-first HTML tokenizer. It implements only the states needed to avoid a
//! *global* offset desync (rawtext/RCDATA modes, script's escaped states, comments, CDATA/DOCTYPE/PI
//! skipping, attribute parsing, `<`-not-a-tag as text); it does NOT do tree construction.
//! It drives the source-agnostic `TokenSink` (start/end/text), so the matcher is decoupled from it.
//!
//! Operates on RAW BYTES (`&[u8]`) — no up-front UTF-8 validation of the whole document (that was
//! ~⅓ of extract time). Names/text/attributes are borrowed `&'a [u8]` slices; the matcher validates
//! (`from_utf8_lossy`) only the small values it actually emits. All slicing is at ASCII delimiter
//! positions (`<`, `>`, quotes, `=`, whitespace), so a multi-byte UTF-8 char is never split.

pub trait TokenSink<'a> {
    /// `attrs` are RAW (name, optional value) byte slices — not lowercased or entity-decoded. `None`
    /// distinguishes a minimized attribute (`disabled`) from an explicitly empty one (`disabled=""`).
    /// `span_start` is the offset of the `<`; `open_end` is just past the start tag's `>`/`/>`.
    fn start_tag(
        &mut self,
        name: &'a [u8],
        attrs: &[(&'a [u8], Option<&'a [u8]>)],
        self_closing: bool,
        span_start: usize,
        open_end: usize,
    );
    /// `close_start` is the offset of the end tag's `<`; `close_end` is just past its `>`.
    fn end_tag(&mut self, name: &'a [u8], close_start: usize, close_end: usize);
    /// One text RUN. `allows_entities` is false for RAWTEXT/PLAINTEXT. `start` is the
    /// run's offset in the document — the sink needs it both to rank the value in document order and
    /// to detect two runs separated by nothing but a DROPPED end tag (which libxml2 coalesces into a
    /// single text node; see `Matcher::text`). A run is NOT always a text node on its own.
    fn text(&mut self, text: &'a [u8], allows_entities: bool, start: usize);
    /// Markup over `[from, to)` that libxml2 keeps NO node for and that does not end the surrounding
    /// text node either, so `<div>a…b</div>` is the single text node `ab`. The sink is told so it can
    /// re-join across it, exactly as it does across a dropped end tag.
    ///
    /// Two constructs qualify, and only two: `<!DOCTYPE …>` and `</>` (HTML5's "missing end tag name",
    /// where the whole thing is ignored). A comment, a CDATA section, a PI, a bogus `<!foo>` and a
    /// BOGUS COMMENT such as `</%>` are all nodes and DO end the run — measured against the oracle.
    fn invisible_markup(&mut self, from: usize, to: usize) {
        let _ = (from, to);
    }
}

fn is_ws(c: u8) -> bool {
    matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0c)
}

/// Is `c` part of a TAG NAME? libxml2's `htmlParseHTMLName` ends a name at exactly three things —
/// whitespace, `>` and `/` — and keeps every other byte, so `<p<img src=s>` is one element NAMED
/// `p<img` rather than a `<p>` with a strange attribute, and `<p.x>` is not a `<p>` either.
///
/// Restricting this to `[A-Za-z0-9_:-]` (which is what it used to be) does not merely mis-name such an
/// element, it invents one that is not in the document: the name came out as `p`, so a `p` selector
/// matched and returned a value lxml never had. A FALSE POSITIVE is the one outcome the no-fallback
/// rule is meant to exclude — an unsupported query is allowed to return nothing, a supported one is not
/// allowed to return something that is not there. A crawled page writing `<p<mip-img …>` is what
/// surfaced it.
///
/// `find_raw_end` already ended a rawtext close tag on exactly this set, so the tokenizer's two name
/// scanners used to disagree with each other as well as with the oracle.
fn is_name_char(c: u8) -> bool {
    !is_ws(c) && c != b'>' && c != b'/'
}

/// How libxml2 tokenizes an element's CONTENT. Which names take which mode is DERIVED from the oracle —
/// the table is [`crate::implied_close::data_mode`], written by `tools/gen_tree_rules.py` — so the
/// variants are documented here and the name list is not written anywhere by hand.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum DataMode {
    /// Ordinary markup.
    Normal,
    /// Character data to the matching end tag, entities NOT decoded. `script` is in this mode and
    /// additionally needs [`find_script_end`].
    Rawtext,
    /// Character data to the matching end tag, entities decoded.
    Rcdata,
    /// Character data to END OF FILE. The element's own end tag does not end it; nothing can.
    Plaintext,
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
pub(crate) fn scan_comment(b: &[u8], p: usize) -> usize {
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

/// Case-insensitive tag-name match at `p`, including the delimiter required after the name.
fn tag_name_at(b: &[u8], p: usize, prefix: &[u8], name: &[u8]) -> Option<usize> {
    if p + prefix.len() + name.len() > b.len()
        || !b[p..].starts_with(prefix)
    {
        return None;
    }
    let mut q = p + prefix.len();
    for &nc in name {
        if (b[q] | 0x20) != (nc | 0x20) {
            return None;
        }
        q += 1;
    }
    (q >= b.len() || is_ws(b[q]) || b[q] == b'>' || b[q] == b'/').then_some(q)
}

/// libxml2-compatible SCRIPT scan, including the legacy escaped/double-escaped states used by old IE
/// conditional-comment wrappers. Inside `<!-- ...`, a nested `<script>` enters double-escaped state:
/// its first `</script>` is text (it only returns to escaped), so it must not close the outer script.
/// Missing this state turns the remainder of a real page into markup and globally desynchronizes it.
fn find_script_end(b: &[u8], from: usize) -> (usize, usize) {
    #[derive(Clone, Copy)]
    enum State {
        Data,
        Escaped,
        DoubleEscaped,
    }

    let n = b.len();
    let mut state = State::Data;
    let mut i = from;
    while i < n {
        if matches!(state, State::Escaped | State::DoubleEscaped) && b[i..].starts_with(b"-->") {
            state = State::Data;
            i += 3;
            continue;
        }
        if b[i] != b'<' {
            i += 1;
            continue;
        }
        match state {
            State::Data => {
                if b[i..].starts_with(b"<!--") {
                    state = State::Escaped;
                    i += 4;
                    continue;
                }
                if let Some(q) = tag_name_at(b, i, b"</", b"script") {
                    return (i, skip_to_gt(b, q));
                }
            }
            State::Escaped => {
                if let Some(q) = tag_name_at(b, i, b"</", b"script") {
                    return (i, skip_to_gt(b, q));
                }
                if let Some(q) = tag_name_at(b, i, b"<", b"script") {
                    state = State::DoubleEscaped;
                    i = q;
                    continue;
                }
            }
            State::DoubleEscaped => {
                if let Some(q) = tag_name_at(b, i, b"</", b"script") {
                    state = State::Escaped;
                    i = q;
                    continue;
                }
            }
        }
        i += 1;
    }
    (n, n)
}

pub fn tokenize<'a, S: TokenSink<'a>>(b: &'a [u8], sink: &mut S) {
    let n = b.len();
    let mut attr_buf: Vec<(&'a [u8], Option<&'a [u8]>)> = Vec::new(); // reused across start tags
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
    attr_buf: &mut Vec<(&'a [u8], Option<&'a [u8]>)>,
) -> usize {
    match kind {
        Markup::Decl => {
            if b[p..].starts_with(b"<!--") {
                scan_comment(b, p)
            } else {
                let after = skip_to_gt(b, p);
                // ONLY a doctype is invisible to the text node either side of it (measured against the
                // oracle: `<!foo>`, `<![CDATA[…]]>`, `<?x?>` and `<!>` all break the run, a doctype
                // does not). Case-insensitive, and the name has to be complete.
                if b[p..].len() > 9 && b[p + 2..p + 9].eq_ignore_ascii_case(b"doctype") {
                    sink.invisible_markup(p, after);
                }
                after
            }
        }
        Markup::Bogus => skip_to_gt(b, p),
        Markup::End => {
            // HTML5's end-tag-open state, which libxml2 follows exactly (verified case by case). Only
            // an ASCII ALPHA starts an end tag; the other two branches are not end tags at all, and
            // collapsing all three into "scan a name, skip to `>`" got both of them wrong:
            //
            //   `</%>`, `</1>`, `</-x>` -> a BOGUS COMMENT. libxml2 keeps a comment node, so it SPLITS
            //      the text either side. The engine read `%` as a tag name (`is_name_char` accepts it),
            //      dropped it as unmatched and JOINED the runs instead — a real page's copyright line
            //      came back as one node where lxml has two.
            //   `</>` -> "missing end tag name": the whole thing is ignored, and being no node at all it
            //      does NOT split the run. The engine emitted no event, which left the runs
            //      un-joined — the same bug the other way round.
            //   `</` at EOF -> character data.
            match b.get(p + 2) {
                Some(&c) if c.is_ascii_alphabetic() => {
                    let n = b.len();
                    let mut i = p + 2;
                    let start = i;
                    while i < n && is_name_char(b[i]) {
                        i += 1;
                    }
                    let after = skip_to_gt(b, i);
                    sink.end_tag(&b[start..i], p, after); // p = '<' of the end tag
                    after
                }
                Some(b'>') => {
                    sink.invisible_markup(p, p + 3);
                    p + 3
                }
                // EOF right after `</`: those two bytes are CHARACTER DATA, and adjoin the run before
                // them (`<span>a</` is the single node `a</` in libxml2). The one shape of truncated
                // input where the engine LOST a value rather than keeping an extra one.
                None => {
                    sink.text(&b[p..], true, p);
                    b.len()
                }
                _ => skip_to_gt(b, p),
            }
        }
        Markup::Start => handle_start(b, p, sink, attr_buf),
    }
}

fn handle_start<'a, S: TokenSink<'a>>(
    b: &'a [u8],
    p: usize,
    sink: &mut S,
    attr_buf: &mut Vec<(&'a [u8], Option<&'a [u8]>)>,
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
    let mut terminated = false;
    loop {
        while i < n && is_ws(b[i]) {
            i += 1;
        }
        if i >= n {
            break;
        }
        if b[i] == b'>' {
            i += 1;
            terminated = true;
            break;
        }
        if b[i] == b'/' {
            if b.get(i + 1) == Some(&b'>') {
                self_closing = true;
                i += 2;
                terminated = true;
                break;
            }
            i += 1;
            continue;
        }
        let as_ = i;
        // An `=` where an attribute NAME should start is the first character of that name, not a
        // separator (HTML5 calls this `unexpected-equals-sign-before-attribute-name`; libxml2 and
        // html5lib agree, the latter naming the attribute `U0003D`). Reading it as a separator gave the
        // attribute an EMPTY name, which is dropped — and swallowed the real attribute after it as its
        // value: a crawled page's `<div = class='background-bg-internas'>` lost its class entirely, so
        // `div::attr(class)` came back one row short. Only reachable here, since whitespace, `>` and `/`
        // were all consumed above.
        if b[i] == b'=' {
            i += 1;
        }
        while i < n && !is_ws(b[i]) && b[i] != b'=' && b[i] != b'>' && b[i] != b'/' {
            i += 1;
        }
        let aname = &b[as_..i];
        let mut aval: Option<&[u8]> = None;
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
                aval = Some(&b[vs..j]);
                if j < n {
                    j += 1;
                }
            } else {
                let vs = j;
                while j < n && !is_ws(b[j]) && b[j] != b'>' {
                    j += 1;
                }
                aval = Some(&b[vs..j]);
            }
            i = j;
        }
        if !aname.is_empty() {
            attr_buf.push((aname, aval));
        }
    }

    if !terminated {
        // EOF before the closing `>`: the tag is DROPPED, whole. Not emitted, and not turned back into
        // text either — libxml2 and html5lib agree on that for every shape (`<a`, `<a href`, `<a href=`,
        // `<a href="x`), and the text before it is untouched. The engine used to emit whatever it had
        // scanned, which is the FALSE-POSITIVE direction: on a crawled page cut off inside
        // `<a href="login.…` it reported an `<a>` element, with an `href` holding the rest of the
        // document, that no other parser sees at all. Truncated responses are not a rare shape in a
        // crawl — this was 6 divergent columns on one page and the largest remaining group in the
        // malformed-HTML fuzzer.
        return n;
    }

    let mode = crate::implied_close::data_mode_of(name);
    sink.start_tag(name, attr_buf, self_closing, p, i);

    if mode != DataMode::Normal && !self_closing {
        // PLAINTEXT has no end: the rest of the document is its text, and libxml2 emits no end tag
        // (the element is closed by end-of-document, like any element left open).
        let (text_end, after) = match mode {
            DataMode::Plaintext => (n, n),
            // `script` needs the escaped/double-escaped states on top of "find the end tag"; every
            // other mode ends at the first matching end tag.
            _ if name.eq_ignore_ascii_case(b"script") => find_script_end(b, i),
            _ => find_raw_end(b, i, name),
        };
        if text_end > i {
            sink.text(&b[i..text_end], mode == DataMode::Rcdata, i);
        }
        if after > text_end {
            sink.end_tag(name, text_end, after);
        }
        return after;
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
            attrs: &[(&'a [u8], Option<&'a [u8]>)],
            self_closing: bool,
            _s: usize,
            _e: usize,
        ) {
            let a: Vec<String> = attrs
                .iter()
                .map(|(n, v)| {
                    format!(
                        "{}={}",
                        String::from_utf8_lossy(n),
                        v.map(String::from_utf8_lossy).unwrap_or_default()
                    )
                })
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

    /// HTML5's END-TAG-OPEN state, pinned at the TOKEN layer where it belongs.
    ///
    /// This costs no oracle and no tree: the question "is `</%>` an end tag?" is answerable from the
    /// event stream alone, and at this layer the three branches are three lines instead of a tree-shape
    /// diff. The bug that motivated it (`</%>` scanned as an end tag named `%`) reached production
    /// because every check above this one goes through selector VALUES, where it showed up only as two
    /// text nodes becoming one on a page that happened to contain it.
    #[test]
    fn end_tag_open_only_starts_a_tag_on_a_letter() {
        // an ASCII letter starts an end tag
        assert_eq!(toks(b"a</p>b"), ["T:a", "E:p", "T:b"]);
        // anything else is a BOGUS COMMENT: no end tag, and it consumes to the first `>` — note the two
        // text runs stay SEPARATE events, which is what makes libxml2's comment node split the text
        for bogus in [&b"a</%>b"[..], &b"a</1>b"[..], &b"a</-x>b"[..], &b"a</ >b"[..]] {
            assert_eq!(toks(bogus), ["T:a", "T:b"], "{}", String::from_utf8_lossy(bogus));
        }
        // ...to the FIRST `>`, quotes and all — the same span libxml2 consumes, so no offset desync
        assert_eq!(toks(br#"a</% x=">">b"#), ["T:a", "T:\">b"]);
        // `</>` is ignored entirely: no end tag, and the surrounding runs are re-joinable
        assert_eq!(toks(b"a</>b"), ["T:a", "T:b"]);
        // EOF right after `</` is character data
        assert_eq!(toks(b"a</"), ["T:a", "T:</"]);
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
        // libxml2 treats noframes content as raw text too, including markup-looking bytes.
        assert_eq!(
            toks(b"<noframes><body><a href=x>fallback</a></body></noframes>"),
            ["S:noframes[]", "T!:<body><a href=x>fallback</a></body>", "E:noframes"]
        );
        // ...and so are `iframe`, `noembed` and the obsolete `xmp`. A missing mode here does not lose a
        // value, it INVENTS the elements inside and then honours the wrong end tag.
        assert_eq!(
            toks(b"<iframe><div>fake</div></iframe>x"),
            ["S:iframe[]", "T!:<div>fake</div>", "E:iframe", "T:x"]
        );
        assert_eq!(
            toks(b"<noembed><div>fake</div></noembed>"),
            ["S:noembed[]", "T!:<div>fake</div>", "E:noembed"]
        );
        assert_eq!(toks(b"<xmp>a<b>c</XMP >d"), ["S:xmp[]", "T!:a<b>c", "E:xmp", "T:d"]);
        // `listing` LOOKS like it belongs in that set and does not: libxml2 parses its content as
        // markup, so this is a control, not a rounding error.
        assert_eq!(toks(b"<listing>a<b>c</b></listing>"),
                   ["S:listing[]", "T:a", "S:b[]", "T:c", "E:b", "E:listing"]);
    }

    #[test]
    fn plaintext_runs_to_end_of_file_with_no_end_tag() {
        // PLAINTEXT has no end: `</plaintext>` is text, and the element is closed by end of document,
        // so the tokenizer emits no end tag at all.
        assert_eq!(
            toks(b"<plaintext><div>a</div></plaintext>b"),
            ["S:plaintext[]", "T!:<div>a</div></plaintext>b"]
        );
        // an EMPTY plaintext at EOF emits the start tag and nothing else
        assert_eq!(toks(b"<plaintext>"), ["S:plaintext[]"]);
    }

    #[test]
    fn script_double_escaped_end_tag_does_not_close_outer_script() {
        assert_eq!(
            toks(b"<script><!--<script></script>--><h4>x"),
            ["S:script[]", "T!:<!--<script></script>--><h4>x"]
        );
        assert_eq!(
            toks(b"<script><!--<script></script>--></script><p>x</p>"),
            [
                "S:script[]",
                "T!:<!--<script></script>-->",
                "E:script",
                "S:p[]",
                "T:x",
                "E:p"
            ]
        );
        // Without a nested <script>, an end tag in the escaped state still closes normally.
        assert_eq!(
            toks(b"<script><!--x</script><p>x</p>"),
            ["S:script[]", "T!:<!--x", "E:script", "S:p[]", "T:x", "E:p"]
        );
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
