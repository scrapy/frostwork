//! HTML5's element categories for browser mode's tree builder (`crate::matcher::browser`): which
//! elements are special, formatting, void, popped by implied end tags, and the boundary of each "has an
//! element in … scope" walk. The insertion modes ask every category-membership question through
//! [`html5_cat`] and [`html5_data_mode`], whose answers are measured from Chrome in `generated_html5.rs`
//! (HTML namespace only; the builder adds the MathML and SVG integration points itself).
//!
//! Each scope flag names the walk's whole boundary list, so an element carrying [`LIST_ITEM_SCOPE`]
//! ends a list-item-scope walk: every [`SCOPE`] boundary plus `ol` and `ul`. Chrome 156 parses
//! `<select>` with the in-body rules of customisable select, which ask the default scope, so
//! [`SELECT_SCOPE`] is the same set as [`SCOPE`].

pub(crate) use super::generated_html5::*;

#[cfg(test)]
mod tests {
    use super::*;

    /// The scope lists nest as the spec builds them: each wider walk stops wherever the default one
    /// does, and table scope is its own three names.
    #[test]
    fn scope_lists_nest() {
        for name in [&b"applet"[..], b"caption", b"html", b"table", b"td", b"th", b"marquee", b"object",
                     b"select", b"template"] {
            let c = html5_cat(name);
            assert_eq!(c & (SCOPE | LIST_ITEM_SCOPE | BUTTON_SCOPE), SCOPE | LIST_ITEM_SCOPE | BUTTON_SCOPE);
        }
        assert_eq!(html5_cat(b"ol") & (SCOPE | LIST_ITEM_SCOPE | BUTTON_SCOPE), LIST_ITEM_SCOPE);
        assert_eq!(html5_cat(b"button") & (SCOPE | LIST_ITEM_SCOPE | BUTTON_SCOPE), BUTTON_SCOPE);
        let table: Vec<&[u8]> = [&b"html"[..], b"table", b"template", b"td", b"div"]
            .into_iter()
            .filter(|n| html5_cat(n) & TABLE_SCOPE != 0)
            .collect();
        assert_eq!(table, [&b"html"[..], b"table", b"template"]);
        assert_eq!(html5_cat(b"option") & SELECT_SCOPE, 0);
        assert_eq!(html5_cat(b"custom-el"), 0);
    }

    #[test]
    fn implied_end_lists() {
        let thorough_only = [&b"caption"[..], b"colgroup", b"tbody", b"td", b"tfoot", b"th", b"thead", b"tr"];
        for n in thorough_only {
            assert_eq!(html5_cat(n) & (IMPLIED_END | IMPLIED_END_THOROUGH), IMPLIED_END_THOROUGH);
        }
        for n in [&b"dd"[..], b"dt", b"li", b"optgroup", b"option", b"p", b"rb", b"rp", b"rt", b"rtc"] {
            assert_eq!(html5_cat(n) & (IMPLIED_END | IMPLIED_END_THOROUGH), IMPLIED_END | IMPLIED_END_THOROUGH);
        }
    }
}
