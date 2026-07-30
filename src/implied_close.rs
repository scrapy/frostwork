//! HTML implied-end-tag rules — the one tree-construction behavior Frostwork ports (see the
//! tree-construction contract in `docs/COMPATIBILITY.md`). The `tag`/`tag_id`/`implies_close_id`
//! table is adapted from
//! `parsel-stream-core/src/implicit_close.rs`; here it drives an *inline stack reshape* in the
//! matcher instead of a detect-and-reparse. `is_void` is added for the tokenizer/matcher.
//!
//! CAUTION: the ported table asserted a *symmetric* `dd`/`dt` and `rt`/`rp` auto-close. That is the
//! HTML5 rule, but it is NOT what libxml2 2.14 does, and the oracle wins here (see AGENTS.md). Every
//! arm below is now verified against libxml2 2.14 directly — `dt`/`dd` auto-close only on the CROSS
//! pair, and `rt`/`rp` never auto-close at all. Do not re-port the upstream table over this one.

pub mod tag {
    pub const OTHER: u8 = 0;
    pub const LI: u8 = 1;
    pub const DD: u8 = 2;
    pub const DT: u8 = 3;
    pub const OPTION: u8 = 4;
    pub const OPTGROUP: u8 = 5;
    pub const TR: u8 = 6;
    pub const TD: u8 = 7;
    pub const TH: u8 = 8;
    pub const THEAD: u8 = 9;
    pub const TBODY: u8 = 10;
    pub const TFOOT: u8 = 11;
    pub const CAPTION: u8 = 12;
    pub const RT: u8 = 13;
    pub const RP: u8 = 14;
    pub const P: u8 = 15;
    pub const BLOCK: u8 = 16; // a block-level start tag that closes an open <p>
    pub const TABLE: u8 = 17; // a BLOCK for <p>-closing, but also a table SCOPE boundary
}

/// Table-scoped elements. libxml2 will not pop through one of these for an ordinary end tag — see
/// [`end_tag_discardable`]. (`table` needs its own id rather than folding into `BLOCK` for exactly this.)
pub fn is_table_scoped(tid: u8) -> bool {
    use tag::*;
    matches!(tid, TABLE | CAPTION | THEAD | TBODY | TFOOT | TR | TD | TH)
}

/// True iff an end tag named `name` must be IGNORED rather than pop the stack, given that at least one
/// table-scoped element is open above its match.
///
/// libxml2 refuses to unwind a table for a stray `</div>`: in `<div><table><tr><td>A</div>B`, the
/// `</div>` is discarded and `A`/`B` stay one text node inside the cell. Unbalanced `<div>`s around
/// tables are among the commonest real-world malformations, so this is not an exotic path. Table-scoped
/// end tags (`</table>`, `</tr>`, `</tbody>`, …) unwind normally, and `</body>`/`</html>` still close
/// the document — verified cell-by-cell against libxml2 2.14.6.
pub fn end_tag_discardable(name: &[u8]) -> bool {
    let (tid, _) = classify(name);
    if is_table_scoped(tid) {
        return false;
    }
    !(name.eq_ignore_ascii_case(b"body") || name.eq_ignore_ascii_case(b"html"))
}

/// Map a lowercased ASCII tag name to its `tag::*` id.
pub fn tag_id(name: &str) -> u8 {
    match name {
        "li" => tag::LI,
        "dd" => tag::DD,
        "dt" => tag::DT,
        "option" => tag::OPTION,
        "optgroup" => tag::OPTGROUP,
        "tr" => tag::TR,
        "td" => tag::TD,
        "th" => tag::TH,
        "thead" => tag::THEAD,
        "tbody" => tag::TBODY,
        "tfoot" => tag::TFOOT,
        "caption" => tag::CAPTION,
        "rt" => tag::RT,
        "rp" => tag::RP,
        "p" => tag::P,
        // Block-level start tags that close an open <p>. This is libxml2 2.14's set (the HTML4 block
        // list) — NOT the HTML5 list: libxml2 predates HTML5 and treats sectioning elements
        // (section/article/aside/header/footer/nav/main/figure/details/hgroup) as unknown, so they do
        // NOT close <p>. We match libxml2 because it is the oracle. Verified empirically against lxml.
        "table" => tag::TABLE, // a BLOCK for <p>-closing, but distinct: it is a scope boundary
        "address" | "blockquote" | "center" | "dir" | "div" | "dl" | "fieldset" | "form" | "h1"
        | "h2" | "h3" | "h4" | "h5" | "h6" | "hr" | "menu" | "ol" | "pre" | "ul" => tag::BLOCK,
        _ => tag::OTHER,
    }
}

/// True iff a `start` start-tag (by id) implicitly closes an open `top` element (by id).
pub fn implies_close_id(start: u8, top: u8) -> bool {
    use tag::*;
    match top {
        LI => start == LI,
        // CROSS pair only: libxml2 2.14 auto-closes `<dt>` on `<dd>` (and vice versa), but NESTS a
        // same-tag repeat — `<dl><dt>a<dt>b</dl>` is `<dt>a<dt>b</dt></dt>`, not two siblings. HTML5
        // says otherwise; libxml2 is the oracle. Verified empirically against lxml (libxml2 2.14.6).
        DT => start == DD,
        DD => start == DT,
        OPTION => matches!(start, OPTION | OPTGROUP),
        // `<optgroup>` NESTS a same-tag repeat. This one bites real pages: a grouped `<select>` with
        // omitted `</optgroup>` (`<optgroup label=A>…<optgroup label=B>…`) is ordinary markup.
        OPTGROUP => false,
        // Table sections close an open row/cell — but `<thead>` does NOT (it nests), so the three are
        // not interchangeable the way a single THEAD|TBODY|TFOOT arm implies.
        TR => matches!(start, TR | TBODY | TFOOT),
        TD | TH => matches!(start, TD | TH | TR | TBODY | TFOOT),
        THEAD | TBODY => matches!(start, TBODY | TFOOT),
        TFOOT => start == TBODY, // closed by <tbody>, but NESTS a second <tfoot> and a <thead>
        CAPTION => matches!(start, THEAD | TBODY | TFOOT | TR),
        // Ruby annotations NEVER auto-close in libxml2 2.14 — neither same-tag nor cross-tag:
        // `<ruby><rt>a<rp>b</ruby>` nests the `<rp>` INSIDE the `<rt>`.
        RT | RP => false,
        // An open `<p>` is closed by the block set plus list/table ITEMS — but NOT by `option`,
        // `optgroup`, `thead`, `rt` or `rp`, which nest inside it. The previous blanket `start != OTHER`
        // over-closed on all five. Every entry here is verified cell-by-cell against libxml2 2.14.6.
        P => matches!(start, BLOCK | TABLE | LI | DD | DT | TR | TD | TH | TBODY | TFOOT | CAPTION | P),
        _ => false,
    }
}

/// Void elements: no end tag, no children. This is libxml2 2.14's set, NOT HTML5's — libxml2 does
/// NOT treat the HTML5-era `embed`/`source`/`track`/`wbr` as void (it keeps them open as ordinary
/// containers), and we match libxml2 because it is the oracle. Verified empirically against lxml.
pub fn is_void(name: &str) -> bool {
    matches!(
        name,
        "area" | "base" | "br" | "col" | "hr" | "img" | "input" | "link" | "meta" | "param"
    )
}

/// Case-insensitive `(tag_id, is_void)` for a raw (possibly mixed-case) tag name, with **no heap
/// allocation** — lowercases into a stack buffer. Every recognized tag name is ≤10 ASCII bytes, so a
/// longer name is unrecognized (`OTHER`, non-void) without copying. Hot path: called per start tag.
pub fn classify(name: &[u8]) -> (u8, bool) {
    if name.len() > 10 {
        return (tag::OTHER, false);
    }
    let mut buf = [0u8; 10];
    for (i, &c) in name.iter().enumerate() {
        buf[i] = c.to_ascii_lowercase();
    }
    // recognized tag names are ASCII; a non-ASCII (invalid-UTF-8) name is unrecognized -> OTHER.
    let low = std::str::from_utf8(&buf[..name.len()]).unwrap_or("");
    (tag_id(low), is_void(low))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn implies(start: &str, top: &str) -> bool {
        implies_close_id(tag_id(start), tag_id(top))
    }
    #[test]
    fn rule_table() {
        assert!(implies("li", "li"));
        assert!(implies("dd", "dt")); // CROSS pair auto-closes
        assert!(implies("dt", "dd"));
        assert!(implies("tr", "td"));
        assert!(!implies("li", "ul"));
        assert!(!implies("td", "tr"));
        // the whole `<p>`-closing column lives in `arms_without_generator_coverage` below, which owns it
        // cell by cell — duplicating rows here just means a rule change fails twice.
        // libxml2 2.14 NESTS a same-tag dt/dd repeat, and never auto-closes ruby annotations at all.
        assert!(!implies("dt", "dt"));
        assert!(!implies("dd", "dd"));
        assert!(!implies("rt", "rt"));
        assert!(!implies("rp", "rp"));
        assert!(!implies("rt", "rp"));
        assert!(!implies("rp", "rt"));
        assert!(!implies("optgroup", "optgroup"));
        assert!(!implies("thead", "thead"));
        assert!(!implies("tfoot", "tfoot"));
        assert!(!implies("caption", "caption"));
    }

    /// The `<p>`-closing set and the table-section arms, cell by cell against libxml2 2.14.6. These
    /// arms had NO differential coverage (the generators never emitted `optgroup`/`thead`/`tfoot`/
    /// `caption`, and never put `<option>`/`<rt>` after an unclosed `<p>`), and 19 of them were wrong.
    #[test]
    fn arms_without_generator_coverage() {
        // <p> is closed by blocks and list/table ITEMS ...
        for t in ["div", "h1", "hr", "table", "ul", "li", "dd", "dt", "tr", "td", "th", "tbody",
                  "tfoot", "caption", "p"] {
            assert!(implies(t, "p"), "<{t}> must close an open <p>");
        }
        // ... but NOT by these, which nest inside it (the old blanket rule closed on all five)
        for t in ["option", "optgroup", "thead", "rt", "rp", "span", "a", "section"] {
            assert!(!implies(t, "p"), "<{t}> must NOT close an open <p>");
        }
        // table sections close a row/cell; <thead> nests instead
        for t in ["tbody", "tfoot"] {
            assert!(implies(t, "tr"), "<{t}> must close an open <tr>");
            assert!(implies(t, "td"), "<{t}> must close an open <td>");
            assert!(implies(t, "th"), "<{t}> must close an open <th>");
        }
        assert!(!implies("thead", "tr"));
        assert!(!implies("thead", "td"));
        assert!(!implies("thead", "tbody"));
        assert!(!implies("thead", "tfoot"));
        // caption is closed by sections and rows, but nests a cell or a second caption
        for t in ["thead", "tbody", "tfoot", "tr"] {
            assert!(implies(t, "caption"), "<{t}> must close an open <caption>");
        }
        assert!(!implies("td", "caption"));
        assert!(!implies("th", "caption"));
        // tbody closes a second tbody; tfoot does not close a second tfoot
        assert!(implies("tbody", "tbody"));
        assert!(implies("tfoot", "tbody"));
        assert!(implies("tbody", "tfoot"));
    }
}
