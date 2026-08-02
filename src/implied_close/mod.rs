//! HTML tree-construction rules — the ones Frostwork ports (see the contract in
//! `docs/COMPATIBILITY.md`). They drive an *inline stack reshape* in the matcher rather than a
//! detect-and-reparse. This file is the HAND-WRITTEN half: the rules that combine the derived tables and
//! the name lookup they share. The tables themselves are in `generated.rs`, written by
//! `tools/gen_tree_rules.py` from libxml2 over a fixed element universe — nothing in this file may be
//! derived there, and nothing there may be edited here (`--check` fails on drift).
//!
//! There used to be a second, hand-written close table here (`tag`/`tag_id`/`implies_close_id`, ported
//! from `parsel-stream-core`) ORed with the generated one. Once the generated relation was derived over
//! the whole universe it closed every pair the ported table did and 163 more, so the port was deleted:
//! it could only mask the real table, never correct it. Its history is worth keeping in mind — it
//! asserted a *symmetric* `dd`/`dt` and `rt`/`rp` auto-close because that is the HTML5 rule, and
//! libxml2 2.14 does neither. The oracle wins here; do not re-port a table over the generated one.

mod generated;

// The derived tables live in `generated.rs` — a whole file, written by `tools/gen_tree_rules.py` from
// libxml2 itself. They are re-exported here so callers see one `implied_close` module: the split is about
// who may EDIT what, not about who may call what.
pub use generated::*;

/// True iff an end tag named `name` must be IGNORED rather than pop the stack, given that something
/// out-ranking it is open above its match.
///
/// libxml2 refuses to unwind a table for a stray `</div>`: in `<div><table><tr><td>A</div>B`, the
/// `</div>` is discarded and `A`/`B` stay one text node inside the cell. Unbalanced `<div>`s around
/// tables are among the commonest real-world malformations, so this is not an exotic path.
/// The three FRAME end tags are the unconditional document closers — every other end tag defers to
/// [`blocks_end_tag`]. They are exempt HERE rather than in the generated priority table because the
/// derivation cannot observe them: nothing is ever open above the document frame, and content after
/// `</html>` is discarded by libxml2 (a documented divergence), so no probe can read those cells.
///
/// `head` was missing from this list, and the sequence sweep found it: in `<head><tr></head><div>` the
/// open `<tr>` out-ranks a priority-0 `</head>`, so the end tag was discarded and everything after it
/// stayed inside the head. libxml2 gives `head` the same end priority as `body` (above `table`), so no
/// open element blocks it either.
pub fn end_tag_discardable(name: &[u8]) -> bool {
    !(name.eq_ignore_ascii_case(b"body")
        || name.eq_ignore_ascii_case(b"html")
        || name.eq_ignore_ascii_case(b"head"))
}

/// Does an open element above the matching ancestor put that ancestor out of scope for `</closing>`?
///
/// libxml2's rule is a COMPARISON, not a set of boundary elements: every name carries an
/// [`end_priority`] and a misplaced end tag may only unwind elements that do not out-rank it. Reading it
/// as a set (a table-scoped set blocking ordinary end tags, plus `div`) kept the two coarsest cells and
/// lost the ORDER inside the table machinery — `</tr>` cannot unwind an open `<tbody>`. A crawled page
/// whose table generator emits `<tr><strong><tbody><td>…</strong><tbody></tr>` rows then closed each row
/// here while libxml2 kept it open, and the cells after the first `</tr>` were lost.
pub fn blocks_end_tag(open_name: &[u8], closing: &[u8]) -> bool {
    end_priority_of(open_name) > end_priority_of(closing)
}

/// The longest tag name any table above recognizes. `tools/gen_tree_rules.py` refuses to generate an
/// element universe that exceeds it, so a longer name here is an unrecognized one, not a missed lookup.
const MAX_TAG_NAME: usize = 10;

/// Look a raw (possibly mixed-case) tag name up in a table keyed by its LOWERCASE form, with **no heap
/// allocation** — lowercases into a stack buffer. A name longer than [`MAX_TAG_NAME`] cannot be
/// recognized, so it takes `unrecognized` without copying. Hot path: called per start tag.
fn lookup<T>(name: &[u8], unrecognized: T, f: impl FnOnce(&str) -> T) -> T {
    if name.len() > MAX_TAG_NAME {
        return unrecognized;
    }
    let mut buf = [0u8; MAX_TAG_NAME];
    for (i, &c) in name.iter().enumerate() {
        buf[i] = c.to_ascii_lowercase();
    }
    // recognized tag names are ASCII; a non-ASCII (invalid-UTF-8) name is unrecognized.
    match std::str::from_utf8(&buf[..name.len()]) {
        Ok(low) => f(low),
        Err(_) => unrecognized,
    }
}

/// Case-insensitive `(is_void, sc_id)` for a raw tag name.
pub fn classify(name: &[u8]) -> (bool, u8) {
    lookup(name, (false, sc::OTHER), |low| (is_void(low), sc_id(low)))
}

/// Case-insensitive [`end_priority`] for a raw tag name — an unrecognized name out-ranks nothing.
pub fn end_priority_of(name: &[u8]) -> u8 {
    lookup(name, 0, end_priority)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn implies(start: &str, top: &str) -> bool {
        start_closes(sc_id(start), sc_id(top))
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
        // `<colgroup>` had NO rule at all, so an omitted `</colgroup>` nested the sections inside it
        for t in ["colgroup", "thead", "tbody", "tfoot", "tr"] {
            assert!(implies(t, "colgroup"), "<{t}> must close an open <colgroup>");
        }
        for t in ["col", "caption", "td", "th", "p", "li", "option"] {
            assert!(!implies(t, "colgroup"), "<{t}> must NOT close an open <colgroup>");
        }
        assert!(implies("colgroup", "caption")); // a colgroup closes an open caption ...
        assert!(implies("colgroup", "p")); // ... and an open <p>
    }

    /// Misplaced-end-tag scope is a priority COMPARISON, not a set of boundary elements. Read as a set it
    /// lost the ORDER inside the table machinery: `</tr>` may not unwind an open `<tbody>`, which is what
    /// a real table generator emits (`<tr><strong><tbody><td>…</strong><tbody></tr>`) and where the
    /// engine closed each row while libxml2 kept it open.
    #[test]
    fn end_tag_scope_is_a_priority_comparison() {
        let blocks = |open: &str, closing: &str| blocks_end_tag(open.as_bytes(), closing.as_bytes());
        // a bare `<div><caption>A</div>B` honours the `</div>`: caption/colgroup out-rank nothing
        for t in ["caption", "colgroup", "li", "p", "option", "span", "ul"] {
            assert_eq!(end_priority(t), 0, "<{t}> must out-rank nothing");
        }
        // the chain, one link at a time — and every link is one-way
        for (over, under) in [
            ("table", "tbody"),
            ("tbody", "tr"),
            ("tr", "td"),
            ("td", "div"),
            ("div", "p"),
        ] {
            assert!(blocks(over, under), "an open <{over}> must discard </{under}>");
            assert!(!blocks(under, over), "an open <{under}> must NOT discard </{over}>");
        }
        // equal priority does not block: a cell does not shield another cell's end tag, the three
        // sections are interchangeable, and a nested table does not shield the outer `</table>`
        for (a, b) in [("td", "th"), ("thead", "tbody"), ("tfoot", "thead"), ("table", "table")] {
            assert!(!blocks(a, b), "<{a}> and <{b}> have equal priority");
        }
        // `</body>`/`</html>` close the document whatever is open above them
        for t in ["body", "html"] {
            assert!(!end_tag_discardable(t.as_bytes()), "</{t}> must always close");
        }
        for t in ["div", "table", "td", "p"] {
            assert!(end_tag_discardable(t.as_bytes()), "</{t}> is discardable when out-ranked");
        }
    }
}

