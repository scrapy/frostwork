//! HTML5's element categories for browser mode's tree builder (`crate::matcher::browser`), written by
//! hand from the WHATWG parsing section (13.2.4.2, 13.2.4.3, 13.2.6.3) for the HTML namespace. The
//! insertion modes ask every category-membership question through [`html5_cat`] and
//! [`html5_data_mode`], so a table derived from the browser can replace this file behind the same API.
//!
//! The scope flags name the BOUNDARY list of each "has an element in … scope" walk, whole: an element
//! carrying [`LIST_ITEM_SCOPE`] ends a list-item-scope walk, which is every [`SCOPE`] boundary plus
//! `ol` and `ul`. Chrome 156 parses `<select>` with the in-body rules of customisable select, which
//! ask the default scope, so [`SELECT_SCOPE`] is the same set as [`SCOPE`].

use crate::tokenizer::DataMode;

/// Elements with special parsing rules: an end tag cannot unwind past one, and the adoption agency
/// looks for one as its furthest block.
pub(crate) const SPECIAL: u16 = 1 << 0;
/// Elements that go on the list of active formatting elements.
pub(crate) const FORMATTING: u16 = 1 << 1;
/// Boundary of "has an element in scope".
pub(crate) const SCOPE: u16 = 1 << 2;
/// Boundary of "has an element in list item scope".
pub(crate) const LIST_ITEM_SCOPE: u16 = 1 << 3;
/// Boundary of "has an element in button scope".
pub(crate) const BUTTON_SCOPE: u16 = 1 << 4;
/// Boundary of "has an element in table scope".
pub(crate) const TABLE_SCOPE: u16 = 1 << 5;
/// Boundary of "has an element in select scope": the default scope's, in Chrome 156.
pub(crate) const SELECT_SCOPE: u16 = 1 << 6;
/// Popped by "generate implied end tags".
pub(crate) const IMPLIED_END: u16 = 1 << 7;
/// Popped by "generate all implied end tags thoroughly".
pub(crate) const IMPLIED_END_THOROUGH: u16 = 1 << 8;
/// No content and no end tag.
pub(crate) const VOID: u16 = 1 << 9;

const IN_SCOPE: u16 = SCOPE | LIST_ITEM_SCOPE | BUTTON_SCOPE | SELECT_SCOPE;
const ENDS: u16 = IMPLIED_END | IMPLIED_END_THOROUGH;

/// The categories of the HTML-namespace element `name`, which is ASCII-lowercase.
pub(crate) fn html5_cat(name: &[u8]) -> u16 {
    let s = SPECIAL;
    match name {
        b"option" | b"optgroup" => ENDS,
        b"dd" | b"dt" | b"li" | b"p" => ENDS | s,
        b"rb" | b"rp" | b"rt" | b"rtc" => ENDS,
        b"caption" | b"td" | b"th" => IMPLIED_END_THOROUGH | IN_SCOPE | SPECIAL,
        b"colgroup" | b"tbody" | b"tfoot" | b"thead" | b"tr" => IMPLIED_END_THOROUGH | s,
        b"html" | b"table" | b"template" => IN_SCOPE | TABLE_SCOPE | SPECIAL,
        b"applet" | b"marquee" | b"object" | b"select" => IN_SCOPE | SPECIAL,
        b"ol" | b"ul" => LIST_ITEM_SCOPE | s,
        b"button" => BUTTON_SCOPE | s,
        b"area" | b"base" | b"basefont" | b"bgsound" | b"br" | b"col" | b"embed" | b"frame" | b"hr"
        | b"img" | b"input" | b"keygen" | b"link" | b"meta" | b"param" | b"source" | b"track"
        | b"wbr" => VOID | s,
        b"address" | b"article" | b"aside" | b"blockquote" | b"body" | b"center" | b"details"
        | b"dir" | b"div" | b"dl" | b"fieldset" | b"figcaption" | b"figure" | b"footer" | b"form"
        | b"frameset" | b"h1" | b"h2" | b"h3" | b"h4" | b"h5" | b"h6" | b"head" | b"header"
        | b"hgroup" | b"iframe" | b"listing" | b"main" | b"menu" | b"nav" | b"noembed"
        | b"noframes" | b"noscript" | b"plaintext" | b"pre" | b"script" | b"section"
        | b"style" | b"summary" | b"textarea" | b"title" | b"xmp" => s,
        b"a" | b"b" | b"big" | b"code" | b"em" | b"font" | b"i" | b"nobr" | b"s" | b"small"
        | b"strike" | b"strong" | b"tt" | b"u" => FORMATTING,
        _ => 0,
    }
}

/// The state an HTML-namespace element's content is tokenized in, with scripting on (a browser's page
/// load), for the ASCII-lowercase `name`. Only an element the tree builder INSERTS gets it: a foreign
/// element, or one the insertion mode ignores, holds markup.
pub(crate) fn html5_data_mode(name: &[u8]) -> DataMode {
    match name {
        b"title" | b"textarea" => DataMode::Rcdata,
        b"style" | b"xmp" | b"iframe" | b"noembed" | b"noframes" | b"noscript" | b"script" => DataMode::Rawtext,
        b"plaintext" => DataMode::Plaintext,
        _ => DataMode::Normal,
    }
}

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
            assert_eq!(html5_cat(n) & ENDS, IMPLIED_END_THOROUGH);
        }
        for n in [&b"dd"[..], b"dt", b"li", b"optgroup", b"option", b"p", b"rb", b"rp", b"rt", b"rtc"] {
            assert_eq!(html5_cat(n) & ENDS, ENDS);
        }
    }
}
