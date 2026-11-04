//! The document mode a `<!DOCTYPE …>` sets: HTML5's DOCTYPE tokenizer states (13.2.5.53 to 13.2.5.68)
//! read over the token's bytes, then the "initial" insertion mode's quirks and limited-quirks tables.
//! Only quirks mode reaches the tree (a `<table>` does not close an open `<p>`); limited quirks is
//! returned for completeness.

use super::is_ws;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(super) enum DocMode {
    NoQuirks,
    LimitedQuirks,
    Quirks,
}

/// A parsed DOCTYPE token. Identifiers are raw bytes, `None` when missing.
#[derive(Default, Debug)]
struct Doctype<'a> {
    name: Option<Vec<u8>>,
    public: Option<&'a [u8]>,
    system: Option<&'a [u8]>,
    force_quirks: bool,
}

/// Read a DOCTYPE token from `raw`, which runs from `<!` to just past the `>` that ends it (or to end of
/// input). HTML5 ends a DOCTYPE at the first `>` even inside a quoted identifier, which is where the
/// tokenizer ended `raw`.
fn parse(raw: &[u8]) -> Doctype<'_> {
    let mut d = Doctype::default();
    let n = raw.len();
    let closed = raw.last() == Some(&b'>');
    let end = if closed { n - 1 } else { n };
    let b = &raw[..end];
    // past `<!DOCTYPE`
    let mut i = 9.min(b.len());
    while i < b.len() && is_ws(b[i]) {
        i += 1;
    }
    if i >= b.len() {
        d.force_quirks = true; // missing name, or EOF
        return d;
    }
    let s = i;
    while i < b.len() && !is_ws(b[i]) {
        i += 1;
    }
    d.name = Some(b[s..i].to_ascii_lowercase());
    let eof = |d: &mut Doctype| {
        if !closed {
            d.force_quirks = true;
        }
    };
    while i < b.len() && is_ws(b[i]) {
        i += 1;
    }
    if i >= b.len() {
        eof(&mut d);
        return d;
    }
    let kw = &b[i..b.len().min(i + 6)];
    let public = kw.eq_ignore_ascii_case(b"public");
    if !public && !kw.eq_ignore_ascii_case(b"system") {
        d.force_quirks = true; // bogus DOCTYPE
        return d;
    }
    i += 6;
    // A quoted identifier, after optional whitespace; `None` with force-quirks set if what follows is
    // not a quote (including a `>` or end of input there).
    fn quoted<'a>(b: &'a [u8], i: &mut usize, d: &mut Doctype) -> Option<&'a [u8]> {
        while *i < b.len() && is_ws(b[*i]) {
            *i += 1;
        }
        let q = *b.get(*i).filter(|&&c| c == b'"' || c == b'\'')?;
        let s = *i + 1;
        match b[s..].iter().position(|&c| c == q) {
            Some(k) => {
                *i = s + k + 1;
                Some(&b[s..s + k])
            }
            None => {
                // abrupt: the `>` that ended the token is inside the quotes, or input ended
                d.force_quirks = true;
                *i = b.len();
                Some(&b[s..])
            }
        }
    }
    let first = quoted(b, &mut i, &mut d);
    if first.is_none() {
        d.force_quirks = true;
        return d;
    }
    if public {
        d.public = first;
        if d.force_quirks {
            return d;
        }
        while i < b.len() && is_ws(b[i]) {
            i += 1;
        }
        if i < b.len() {
            match quoted(b, &mut i, &mut d) {
                Some(sys) => d.system = Some(sys),
                None => {
                    d.force_quirks = true;
                    return d;
                }
            }
        }
    } else {
        d.system = first;
    }
    if !d.force_quirks {
        eof(&mut d);
    }
    d
}

fn starts_ci(s: &[u8], prefix: &str) -> bool {
    s.len() >= prefix.len() && s[..prefix.len()].eq_ignore_ascii_case(prefix.as_bytes())
}

const QUIRKS_PREFIXES: [&str; 55] = [
    "+//Silmaril//dtd html Pro v0r11 19970101//",
    "-//AS//DTD HTML 3.0 asWedit + extensions//",
    "-//AdvaSoft Ltd//DTD HTML 3.0 asWedit + extensions//",
    "-//IETF//DTD HTML 2.0 Level 1//",
    "-//IETF//DTD HTML 2.0 Level 2//",
    "-//IETF//DTD HTML 2.0 Strict Level 1//",
    "-//IETF//DTD HTML 2.0 Strict Level 2//",
    "-//IETF//DTD HTML 2.0 Strict//",
    "-//IETF//DTD HTML 2.0//",
    "-//IETF//DTD HTML 2.1E//",
    "-//IETF//DTD HTML 3.0//",
    "-//IETF//DTD HTML 3.2 Final//",
    "-//IETF//DTD HTML 3.2//",
    "-//IETF//DTD HTML 3//",
    "-//IETF//DTD HTML Level 0//",
    "-//IETF//DTD HTML Level 1//",
    "-//IETF//DTD HTML Level 2//",
    "-//IETF//DTD HTML Level 3//",
    "-//IETF//DTD HTML Strict Level 0//",
    "-//IETF//DTD HTML Strict Level 1//",
    "-//IETF//DTD HTML Strict Level 2//",
    "-//IETF//DTD HTML Strict Level 3//",
    "-//IETF//DTD HTML Strict//",
    "-//IETF//DTD HTML//",
    "-//Metrius//DTD Metrius Presentational//",
    "-//Microsoft//DTD Internet Explorer 2.0 HTML Strict//",
    "-//Microsoft//DTD Internet Explorer 2.0 HTML//",
    "-//Microsoft//DTD Internet Explorer 2.0 Tables//",
    "-//Microsoft//DTD Internet Explorer 3.0 HTML Strict//",
    "-//Microsoft//DTD Internet Explorer 3.0 HTML//",
    "-//Microsoft//DTD Internet Explorer 3.0 Tables//",
    "-//Netscape Comm. Corp.//DTD HTML//",
    "-//Netscape Comm. Corp.//DTD Strict HTML//",
    "-//O'Reilly and Associates//DTD HTML 2.0//",
    "-//O'Reilly and Associates//DTD HTML Extended 1.0//",
    "-//O'Reilly and Associates//DTD HTML Extended Relaxed 1.0//",
    "-//SQ//DTD HTML 2.0 HoTMetaL + extensions//",
    "-//SoftQuad Software//DTD HoTMetaL PRO 6.0::19990601::extensions to HTML 4.0//",
    "-//SoftQuad//DTD HoTMetaL PRO 4.0::19971010::extensions to HTML 4.0//",
    "-//Spyglass//DTD HTML 2.0 Extended//",
    "-//Sun Microsystems Corp.//DTD HotJava HTML//",
    "-//Sun Microsystems Corp.//DTD HotJava Strict HTML//",
    "-//W3C//DTD HTML 3 1995-03-24//",
    "-//W3C//DTD HTML 3.2 Draft//",
    "-//W3C//DTD HTML 3.2 Final//",
    "-//W3C//DTD HTML 3.2//",
    "-//W3C//DTD HTML 3.2S Draft//",
    "-//W3C//DTD HTML 4.0 Frameset//",
    "-//W3C//DTD HTML 4.0 Transitional//",
    "-//W3C//DTD HTML Experimental 19960712//",
    "-//W3C//DTD HTML Experimental 970421//",
    "-//W3C//DTD W3 HTML//",
    "-//W3O//DTD W3 HTML 3.0//",
    "-//WebTechs//DTD Mozilla HTML 2.0//",
    "-//WebTechs//DTD Mozilla HTML//",
];

/// The document mode the DOCTYPE token in `raw` sets, from the "initial" insertion mode's tables.
pub(super) fn doc_mode(raw: &[u8]) -> DocMode {
    let d = parse(raw);
    let public = d.public.unwrap_or(b"");
    let system_missing_or_empty = d.system.is_none_or(|s| s.is_empty());
    let quirks = d.force_quirks
        || d.name.as_deref() != Some(b"html")
        || [&b"-//W3O//DTD W3 HTML Strict 3.0//EN//"[..], b"-/W3C/DTD HTML 4.0 Transitional/EN", b"HTML"]
            .iter()
            .any(|p| d.public.is_some_and(|x| x.eq_ignore_ascii_case(p)))
        || d.system.is_some_and(|s| {
            s.eq_ignore_ascii_case(b"http://www.ibm.com/data/dtd/v11/ibmxhtml1-transitional.dtd")
        })
        || (d.public.is_some() && QUIRKS_PREFIXES.iter().any(|p| starts_ci(public, p)))
        || (d.public.is_some()
            && system_missing_or_empty
            && (starts_ci(public, "-//W3C//DTD HTML 4.01 Frameset//")
                || starts_ci(public, "-//W3C//DTD HTML 4.01 Transitional//")));
    if quirks {
        return DocMode::Quirks;
    }
    let limited = d.public.is_some()
        && (starts_ci(public, "-//W3C//DTD XHTML 1.0 Frameset//")
            || starts_ci(public, "-//W3C//DTD XHTML 1.0 Transitional//")
            || (!system_missing_or_empty
                && (starts_ci(public, "-//W3C//DTD HTML 4.01 Frameset//")
                    || starts_ci(public, "-//W3C//DTD HTML 4.01 Transitional//"))));
    if limited {
        DocMode::LimitedQuirks
    } else {
        DocMode::NoQuirks
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn document_modes() {
        let q = |s: &str| doc_mode(s.as_bytes());
        assert_eq!(q("<!DOCTYPE html>"), DocMode::NoQuirks);
        assert_eq!(q("<!doctype HTML>"), DocMode::NoQuirks);
        assert_eq!(q("<!DOCTYPE html SYSTEM \"about:legacy-compat\">"), DocMode::NoQuirks);
        assert_eq!(q("<!DOCTYPE>"), DocMode::Quirks);
        assert_eq!(q("<!DOCTYPE foo>"), DocMode::Quirks);
        assert_eq!(q("<!DOCTYPE html"), DocMode::Quirks); // EOF in the doctype
        assert_eq!(q("<!DOCTYPE html bogus>"), DocMode::Quirks);
        assert_eq!(q("<!DOCTYPE html PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\">"), DocMode::Quirks);
        assert_eq!(
            q("<!DOCTYPE html PUBLIC \"-//W3C//DTD HTML 4.01 Transitional//EN\" \"http://www.w3.org/TR/html4/loose.dtd\">"),
            DocMode::LimitedQuirks
        );
        assert_eq!(
            q("<!DOCTYPE html PUBLIC \"-//W3C//DTD XHTML 1.0 Strict//EN\" \"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd\">"),
            DocMode::NoQuirks
        );
        assert_eq!(q("<!DOCTYPE html PUBLIC \"-//W3C//DTD XHTML 1.0 Transitional//EN\" \"x\">"), DocMode::LimitedQuirks);
        assert_eq!(q("<!DOCTYPE html PUBLIC \"html\">"), DocMode::Quirks);
        assert_eq!(q("<!DOCTYPE html PUBLIC \"x>"), DocMode::Quirks); // abrupt identifier
        assert_eq!(q("<!DOCTYPE html PUBLIC>"), DocMode::Quirks);
        assert_eq!(q("<!DOCTYPE html SYSTEM \"x\" junk>"), DocMode::NoQuirks); // bogus after the system id
    }
}
