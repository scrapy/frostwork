//! HTML implied-end-tag rules — the one tree-construction behavior Frostwork ports (see the
//! tree-construction contract in `docs/COMPATIBILITY.md`). The `tag`/`tag_id`/`implies_close_id`
//! table is adapted from
//! `parsel-stream-core/src/implicit_close.rs`; here it drives an *inline stack reshape* in the
//! matcher instead of a detect-and-reparse. `is_void` is added for the tokenizer/matcher.

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
        "address" | "blockquote" | "center" | "dir" | "div" | "dl" | "fieldset" | "form" | "h1"
        | "h2" | "h3" | "h4" | "h5" | "h6" | "hr" | "menu" | "ol" | "pre" | "table" | "ul" => {
            tag::BLOCK
        }
        _ => tag::OTHER,
    }
}

/// True iff a `start` start-tag (by id) implicitly closes an open `top` element (by id).
pub fn implies_close_id(start: u8, top: u8) -> bool {
    use tag::*;
    match top {
        LI => start == LI,
        DD | DT => matches!(start, DD | DT),
        OPTION => matches!(start, OPTION | OPTGROUP),
        OPTGROUP => start == OPTGROUP,
        TR => start == TR,
        TD | TH => matches!(start, TD | TH | TR),
        THEAD | TBODY | TFOOT => matches!(start, THEAD | TBODY | TFOOT),
        CAPTION => matches!(start, CAPTION | THEAD | TBODY | TFOOT | TR | TD | TH),
        RT | RP => matches!(start, RT | RP),
        // An open <p> is closed by any recognized block/item start tag (verified against libxml2:
        // li/dd/dt/td/th/tr/option/optgroup/caption/thead/tbody/tfoot and every BLOCK all close it).
        // Only inline/unknown tags (tag::OTHER — span, a, b, em, …) stay inside the <p>.
        P => start != OTHER,
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
        assert!(implies("dd", "dt"));
        assert!(implies("tr", "td"));
        assert!(implies("div", "p")); // block closes p
        assert!(implies("h2", "p"));
        assert!(implies("li", "p")); // list/table items close an open <p> too (libxml2)
        assert!(implies("td", "p"));
        assert!(implies("option", "p"));
        assert!(!implies("li", "ul"));
        assert!(!implies("span", "p")); // inline stays inside <p>
        assert!(!implies("a", "p"));
        assert!(!implies("td", "tr"));
    }
}
