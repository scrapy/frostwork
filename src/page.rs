//! Ergonomic page-object layer over the one-pass [`extract`](crate::extract) primitive.
//!
//! A *page object* is a `{field: selector}` schema. You declare the fields once, then fill an entire
//! [`Item`] from a page in a **single** streaming scan: [`Page::extract`] makes exactly one `extract`
//! call regardless of field count, sharing the document scan while matching work grows with the schema.
//! Values are the same Parsel-identical strings the engine emits (one
//! per text node / entity-decoded attribute); this layer only *names* those columns, picks a
//! cardinality, and assembles the item — it adds no matching logic and no new divergence.
//!
//! ```
//! use frostwork::Page;
//!
//! let page = Page::new()
//!     .field("title", "h1::text")             // first match  -> get() yields Some/None
//!     .field("price", ".price::text")
//!     .field_all("images", "img::attr(src)"); // every match  -> get_all() yields a list
//!
//! let html = b"<div class=product><h1>Widget</h1>\
//!              <span class=price>$9</span>\
//!              <img src=/a.png><img src=/b.png></div>";
//! let item = page.extract(html);              // ONE streaming pass fills every field
//!
//! assert_eq!(item.get("title"), Some("Widget"));
//! assert_eq!(item.get("price"), Some("$9"));
//! assert_eq!(item.get_all("images"), ["/a.png", "/b.png"]);
//! assert_eq!(item.get("missing"), None);
//! ```

use core::fmt::Write as _;
use std::sync::OnceLock;

/// How a field distils its selector's (possibly multi-match) column into a value.
#[derive(Clone, Debug)]
enum Card {
    /// First match, or nothing — [`Page::field`].
    First,
    /// Every match, in document order — [`Page::field_all`].
    All,
    /// Every match, concatenated with the stored separator — [`Page::field_join`].
    Join(String),
}

/// A declarative `{field: selector}` schema. Build it once with the `field*` methods, then call
/// [`extract`](Page::extract) per page. The compiled plan is cached after the first extraction; cloning
/// copies the schema with a fresh cache.
///
/// The engine's ≤128 member-selector budget is shared across a page's fields (a comma-group field
/// expands to one member per terminal), so a single page tops out at 128 simple fields.
pub struct Page {
    names: Vec<String>,
    queries: Vec<String>,
    cards: Vec<Card>,
    plan: OnceLock<crate::Plan>,
}

impl Default for Page {
    fn default() -> Self {
        Self { names: Vec::new(), queries: Vec::new(), cards: Vec::new(), plan: OnceLock::new() }
    }
}

impl Clone for Page {
    fn clone(&self) -> Self {
        // Cloning a schema is cheap and intentionally starts with an empty cache. Sharing the compiled
        // plan would require `Arc` and would make a subsequent consuming builder call surprising.
        Self {
            names: self.names.clone(),
            queries: self.queries.clone(),
            cards: self.cards.clone(),
            plan: OnceLock::new(),
        }
    }
}

impl core::fmt::Debug for Page {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("Page")
            .field("names", &self.names)
            .field("queries", &self.queries)
            .field("cards", &self.cards)
            .field("compiled", &self.plan.get().is_some())
            .finish()
    }
}

impl Page {
    /// A page object with no fields.
    pub fn new() -> Self {
        Self::default()
    }

    fn push(mut self, name: impl Into<String>, selector: impl Into<String>, card: Card) -> Self {
        // A Page can be extended after it has already extracted. Consuming builder methods invalidate
        // the cache so the new schema is never run through the old plan.
        self.plan = OnceLock::new();
        self.names.push(name.into());
        self.queries.push(selector.into());
        self.cards.push(card);
        self
    }

    /// Add a **single-valued** field: [`Item::get`] returns its first match, or `None`.
    /// [`Item::get_all`] returns at most that first match; use [`Page::field_all`] for every match.
    pub fn field(self, name: impl Into<String>, selector: impl Into<String>) -> Self {
        self.push(name, selector, Card::First)
    }

    /// Add a **multi-valued** field: [`Item::get_all`] returns every match in document order.
    pub fn field_all(self, name: impl Into<String>, selector: impl Into<String>) -> Self {
        self.push(name, selector, Card::All)
    }

    /// Add a field that **joins** every match with `separator` into one string (e.g. the text nodes
    /// of an element via `E ::text`). An empty column joins to `""`, not `None`.
    pub fn field_join(
        self,
        name: impl Into<String>,
        selector: impl Into<String>,
        separator: impl Into<String>,
    ) -> Self {
        self.push(name, selector, Card::Join(separator.into()))
    }

    /// Number of declared fields.
    pub fn len(&self) -> usize {
        self.names.len()
    }

    /// Whether the schema has no fields.
    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }

    /// The declared field names, in order.
    pub fn field_names(&self) -> impl Iterator<Item = &str> {
        self.names.iter().map(String::as_str)
    }

    /// Compile this page's selectors eagerly into a reusable [`CompiledPage`]. Ordinary
    /// [`extract`](Page::extract) already compiles lazily and caches; this method is useful when callers
    /// want compilation to happen at an explicit initialization boundary.
    pub fn compile(&self) -> CompiledPage {
        CompiledPage {
            plan: self.compile_plan(),
            names: self.names.clone(),
            cards: self.cards.clone(),
        }
    }

    /// The plan behind both entry points, so the one-shot and compile-once paths cannot differ in what
    /// they tell the engine. Cardinality is passed down because it is what makes EARLY EXIT sound: a
    /// schema of nothing but [`Card::First`] fields is finished as soon as each has a value, and the
    /// engine can stop tokenizing (see [`crate::Plan::compile_first_only`]). Anything else — one
    /// `field_all`, one `field_join` — leaves the schema unarmed, since those consumers read the whole
    /// column.
    fn compile_plan(&self) -> crate::Plan {
        let first_only: Vec<bool> = self.cards.iter().map(|c| matches!(c, Card::First)).collect();
        crate::Plan::compile_first_only(&self.queries, &[], &first_only)
    }

    /// Fill an [`Item`] from `html` in one streaming pass, sniffing the encoding
    /// (BOM → `<meta>` → UTF-8). One-shot; to reuse the schema across pages, [`compile`](Page::compile).
    pub fn extract(&self, html: &[u8]) -> Item {
        self.extract_with_encoding(html, None)
    }

    /// Like [`extract`](Page::extract), but with an explicit caller/HTTP charset label (as Scrapy
    /// passes from the `Content-Type` header); `None` sniffs. See [`crate::extract`].
    pub fn extract_with_encoding(&self, html: &[u8], encoding: Option<&str>) -> Item {
        let plan = self.plan.get_or_init(|| self.compile_plan());
        let cols = plan.extract(html, encoding).0;
        Item { names: self.names.clone(), cards: self.cards.clone(), cols }
    }
}

/// A [`Page`] whose selectors are compiled ONCE (into a [`Plan`](crate::Plan)) for reuse across many
/// pages — the compile-once/extract-many form of [`Page`]. Build it with [`Page::compile`], then call
/// [`extract`](CompiledPage::extract) per response; the per-page selector recompile is gone.
pub struct CompiledPage {
    plan: crate::Plan,
    names: Vec<String>,
    cards: Vec<Card>,
}

impl CompiledPage {
    /// Fill an [`Item`] from `html` in one streaming pass, sniffing the encoding (BOM → `<meta>` → UTF-8).
    pub fn extract(&self, html: &[u8]) -> Item {
        self.extract_with_encoding(html, None)
    }

    /// Like [`extract`](CompiledPage::extract), with an explicit charset label; `None` sniffs.
    pub fn extract_with_encoding(&self, html: &[u8], encoding: Option<&str>) -> Item {
        // Columns come back aligned with the plan's queries, i.e. with our fields.
        let cols = self.plan.extract(html, encoding).0;
        Item {
            names: self.names.clone(),
            cards: self.cards.clone(),
            cols,
        }
    }
}

/// The values extracted for one page — one column per declared field, aligned by index. Own the
/// values; borrow nothing from the source. Look fields up by name with [`get`](Item::get) /
/// [`get_all`](Item::get_all), or walk them card-aware with [`iter`](Item::iter) / [`to_json`](Item::to_json).
#[derive(Clone, Debug)]
pub struct Item {
    names: Vec<String>,
    cards: Vec<Card>,
    cols: Vec<Vec<String>>, // one column per field, aligned with names/cards
}

/// A cardinality-aware view of one field's value(s), as declared on the [`Page`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Field<'a> {
    /// From [`Page::field`] — the first match, or `None`.
    First(Option<&'a str>),
    /// From [`Page::field_all`] — every match, in document order.
    All(&'a [String]),
    /// From [`Page::field_join`] — every match joined by the field's separator.
    Join(String),
}

impl Item {
    fn index(&self, name: &str) -> Option<usize> {
        self.names.iter().position(|n| n == name)
    }

    /// The first matched value for `name`, cardinality-independent: `None` if the field is absent or
    /// matched nothing.
    pub fn get(&self, name: &str) -> Option<&str> {
        self.index(name)
            .and_then(|i| self.cols[i].first().map(String::as_str))
    }

    /// Raw values requested by `name`'s declaration, before joining: zero or one match for
    /// [`Page::field`], every match in document order for [`Page::field_all`] and [`Page::field_join`].
    /// This does not depend on whether the scan exits early. An empty slice if the field is absent
    /// or matched nothing.
    pub fn get_all(&self, name: &str) -> &[String] {
        self.index(name).map_or(&[], |i| {
            let col = self.cols[i].as_slice();
            if matches!(self.cards[i], Card::First) {
                &col[..col.len().min(1)]
            } else {
                col
            }
        })
    }

    /// A cardinality-aware view of `name` (respecting `First`/`All`/`Join`), or `None` if absent.
    pub fn field(&self, name: &str) -> Option<Field<'_>> {
        self.index(name).map(|i| self.view(i))
    }

    fn view(&self, i: usize) -> Field<'_> {
        let col = &self.cols[i];
        match &self.cards[i] {
            Card::First => Field::First(col.first().map(String::as_str)),
            Card::All => Field::All(col.as_slice()),
            Card::Join(sep) => Field::Join(col.join(sep)),
        }
    }

    /// Walk `(name, value)` in declared order, each value shaped by its field's cardinality.
    pub fn iter(&self) -> impl Iterator<Item = (&str, Field<'_>)> {
        (0..self.names.len()).map(move |i| (self.names[i].as_str(), self.view(i)))
    }

    /// Number of fields.
    pub fn len(&self) -> usize {
        self.names.len()
    }

    /// Whether the item has no fields.
    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }

    /// Serialize to a JSON object in declared field order — dependency-free, strings JSON-escaped:
    /// `field`/`field_join` → a string (or `null` when a `field` matched nothing), `field_all` → an
    /// array of strings. Keys are unique iff the schema's field names are.
    pub fn to_json(&self) -> String {
        let mut out = String::from("{");
        for (i, (name, val)) in self.iter().enumerate() {
            if i > 0 {
                out.push(',');
            }
            json_str(&mut out, name);
            out.push(':');
            match val {
                Field::First(None) => out.push_str("null"),
                Field::First(Some(s)) => json_str(&mut out, s),
                Field::Join(s) => json_str(&mut out, &s),
                Field::All(list) => {
                    out.push('[');
                    for (j, s) in list.iter().enumerate() {
                        if j > 0 {
                            out.push(',');
                        }
                        json_str(&mut out, s);
                    }
                    out.push(']');
                }
            }
        }
        out.push('}');
        out
    }
}

/// Append `s` as a JSON string literal (with quotes) to `out`. Raw UTF-8 passes through; only the
/// characters JSON requires escaping are escaped.
fn json_str(out: &mut String, s: &str) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                // control char with no short escape -> \u00XX
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

#[cfg(test)]
mod tests {
    use super::*;

    fn product() -> &'static [u8] {
        b"<div class=product><h1>Widget</h1>\
          <span class=price>$9</span>\
          <a href=/p/1>buy</a>\
          <img src=/a.png><img src=/b.png>\
          <div class=desc>Warm <b>and</b> dry.</div></div>"
    }

    #[test]
    fn single_pass_fills_every_field() {
        let page = Page::new()
            .field("title", "h1::text")
            .field("price", ".price::text")
            .field("link", "a::attr(href)")
            .field_all("images", "img::attr(src)");
        let item = page.extract(product());

        assert_eq!(item.get("title"), Some("Widget"));
        assert_eq!(item.get("price"), Some("$9"));
        assert_eq!(item.get("link"), Some("/p/1"));
        assert_eq!(item.get_all("images"), ["/a.png", "/b.png"]);
        assert_eq!(item.len(), 4);
    }

    #[test]
    fn compiled_page_reuses_plan_across_pages() {
        // A `CompiledPage` compiles the schema once and fills an `Item` per page, matching one-shot
        // `Page::extract` exactly on each.
        let page = Page::new().field("title", "h1::text").field_all("images", "img::attr(src)");
        let compiled = page.compile();
        let p1 = compiled.extract(product());
        assert_eq!(p1.get("title"), Some("Widget"));
        assert_eq!(p1.get_all("images"), ["/a.png", "/b.png"]);
        // reuse on a different page
        let p2 = compiled.extract(b"<h1>Other</h1><img src=/c.png>");
        assert_eq!(p2.get("title"), Some("Other"));
        assert_eq!(p2.get_all("images"), ["/c.png"]);
        // identical to the one-shot path
        assert_eq!(compiled.extract(product()).get("title"), page.extract(product()).get("title"));
    }

    #[test]
    fn page_caches_plan_and_builder_invalidates_it() {
        let page = Page::new().field("title", "h1::text");
        assert!(page.plan.get().is_none());
        assert_eq!(page.extract(b"<h1>A</h1>").get("title"), Some("A"));
        assert!(page.plan.get().is_some());
        assert_eq!(page.extract(b"<h1>B</h1>").get("title"), Some("B"));

        let extended = page.field("price", ".price::text");
        assert!(extended.plan.get().is_none());
        let item = extended.extract(b"<h1>C</h1><span class=price>$1</span>");
        assert_eq!(item.get("title"), Some("C"));
        assert_eq!(item.get("price"), Some("$1"));
    }

    #[test]
    fn missing_field_and_no_match() {
        let page = Page::new()
            .field("title", "h1::text")
            .field("subtitle", "h2::text"); // no <h2> in the page
        let item = page.extract(product());

        assert_eq!(item.get("subtitle"), None); // matched nothing
        assert_eq!(item.get_all("subtitle"), &[] as &[String]);
        assert_eq!(item.get("nope"), None); // not a declared field
        assert!(item.get_all("nope").is_empty());
        assert!(item.field("nope").is_none());
    }

    #[test]
    fn cardinality_views() {
        let page = Page::new()
            .field("title", "h1::text")
            .field_all("images", "img::attr(src)")
            .field_join("desc", ".desc ::text", " ");
        let item = page.extract(product());

        assert_eq!(item.field("title"), Some(Field::First(Some("Widget"))));
        assert_eq!(
            item.field("images"),
            Some(Field::All(&["/a.png".into(), "/b.png".into()]))
        );
        // subtree text nodes: "Warm ", "and", " dry." joined by a space
        assert_eq!(
            item.field("desc"),
            Some(Field::Join("Warm  and  dry.".into()))
        );
    }

    #[test]
    fn join_empty_is_empty_string() {
        let page = Page::new().field_join("body", ".none ::text", " ");
        let item = page.extract(product());
        assert_eq!(item.field("body"), Some(Field::Join(String::new())));
    }

    #[test]
    fn get_is_cardinality_independent() {
        // get() reads the first raw value regardless of how the field was declared
        let page = Page::new()
            .field_all("images", "img::attr(src)")
            .field_join("desc", ".desc ::text", " ");
        let item = page.extract(product());
        assert_eq!(item.get("images"), Some("/a.png")); // first of the list
        assert_eq!(item.get("desc"), Some("Warm ")); // first text node, raw
    }

    #[test]
    fn get_all_follows_cardinality_across_schemas() {
        let cases: &[(&[u8], &str, &[&str])] = &[
            (b"<p>a</p><p>b</p>", "p::text", &["a", "b"]),
            (b"<a href=''></a><a href='/product'></a>", "a::attr(href)", &["", "/product"]),
            (b"<p> </p><p>b</p>", "p::text", &[" ", "b"]),
            (b"<div><div>inner</div>outer</div>", "div",
             &["<div><div>inner</div>outer</div>", "<div>inner</div>"]),
            (b"<div><p>a</p></div><div><p>b</p></div>", "p:last-child::text", &["a", "b"]),
        ];
        for &(body, selector, raw) in cases {
            let body = [body, b"<aside>later</aside><aside>last</aside>"].concat();
            for card in [Card::First, Card::All, Card::Join("|".into())] {
                let first = matches!(card, Card::First);
                for companion in 0..4 {
                    let page = Page::new().push("value", selector, card.clone());
                    let page = match companion {
                        0 => page,
                        1 => page.field("other", "aside::text"),
                        2 => page.field("missing", "nosuchtag::text"),
                        _ => page.field_all("other", "aside::text"),
                    };
                    // Both extraction entry points, including reuse of their compiled plans.
                    let compiled = page.compile();
                    for _ in 0..2 {
                        for item in [page.extract(&body), compiled.extract(&body)] {
                            let expected = if first { &raw[..1] } else { raw };
                            assert_eq!(item.get_all("value"), expected,
                                       "{selector}, {card:?}, companion={companion}");
                            assert_eq!(item.get("value"), Some(raw[0]));
                            assert!(item.get_all("absent").is_empty());
                            assert!(item.get_all("missing").is_empty());
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn iter_preserves_declaration_order() {
        let page = Page::new()
            .field("b", "h1::text")
            .field("a", ".price::text")
            .field_all("c", "img::attr(src)");
        let item = page.extract(product());
        let names: Vec<&str> = item.iter().map(|(n, _)| n).collect();
        assert_eq!(names, ["b", "a", "c"]);
    }

    #[test]
    fn to_json_shapes_and_escaping() {
        let page = Page::new()
            .field("title", "h1::text")
            .field("subtitle", "h2::text") // no match -> null
            .field_all("images", "img::attr(src)");
        let item = page.extract(product());
        assert_eq!(
            item.to_json(),
            r#"{"title":"Widget","subtitle":null,"images":["/a.png","/b.png"]}"#
        );
    }

    #[test]
    fn to_json_escapes_control_and_quotes() {
        // entity + literal chars that must be JSON-escaped
        let html = b"<p>a\"b\\c\td</p>";
        let page = Page::new().field("t", "p::text");
        let item = page.extract(html);
        assert_eq!(item.to_json(), r#"{"t":"a\"b\\c\td"}"#);
    }

    #[test]
    fn to_json_join_field_is_a_string() {
        let page = Page::new().field_join("desc", ".desc ::text", "");
        let item = page.extract(product());
        // joined with "" -> "Warm and dry." (b's text abuts its neighbours)
        assert_eq!(item.to_json(), r#"{"desc":"Warm and dry."}"#);
    }

    #[test]
    fn explicit_encoding_label() {
        // windows-1252 é = 0xE9, passed as a label the way Scrapy would from Content-Type
        let page = Page::new().field("p", "p::text");
        let item = page.extract_with_encoding(b"<p class=c>caf\xe9</p>", Some("windows-1252"));
        assert_eq!(item.get("p"), Some("café"));
    }

    #[test]
    fn reusable_across_pages() {
        let page = Page::new().field("t", "h1::text");
        let a = page.extract(b"<h1>A</h1>");
        let b = page.extract(b"<h1>B</h1>");
        assert_eq!(a.get("t"), Some("A"));
        assert_eq!(b.get("t"), Some("B"));
    }

    #[test]
    fn xpath_and_css_fields_mix() {
        let page = Page::new()
            .field("css", "a::attr(href)")
            .field("xpath", "//a/@href");
        let item = page.extract(product());
        assert_eq!(item.get("css"), item.get("xpath"));
        assert_eq!(item.get("xpath"), Some("/p/1"));
    }

    /// EARLY EXIT: an all-`Card::First` schema stops scanning once every field has a value. Proven by
    /// what CANNOT be observed rather than by timing — a page whose tail is appended garbage that would
    /// change the answer of a field the schema does not declare, and a `field_all` that disarms it.
    #[test]
    fn all_first_fields_stop_the_scan_and_keep_the_same_values() {
        // The head answers both fields; the body then re-answers them and adds a third element.
        let head = b"<html><head><title>T</title><link rel=canonical href=/a></head><body>";
        let tail = b"<title>LATER</title><link rel=canonical href=/b><p>body</p></body></html>";
        let doc = [&head[..], &tail[..]].concat();
        let first = Page::new().field("t", "title::text").field("c", "link::attr(href)");
        let item = first.extract(&doc);
        assert_eq!(item.get("t"), Some("T"));
        assert_eq!(item.get("c"), Some("/a"));

        // ...and the values are what a FULL scan's cardinality reduction gives, which is the whole
        // contract: `extract` still sees the tail, and its first values are the same two.
        let cols = crate::extract(&doc, &["title::text".into(), "link::attr(href)".into()], None);
        assert_eq!(cols[0].first().map(String::as_str), item.get("t"));
        assert_eq!(cols[1].first().map(String::as_str), item.get("c"));
        assert_eq!(cols[0].len(), 2, "extract itself is never armed — it must still see the tail");

        // One multi-valued field disarms the whole schema, because its consumer reads the whole column.
        let mixed = Page::new().field("t", "title::text").field_all("all", "title::text");
        let item = mixed.extract(&doc);
        assert_eq!(item.get_all("all"), ["T".to_string(), "LATER".to_string()]);
        assert_eq!(item.get("t"), Some("T"));

        // A join field reads the whole column too, and must not be truncated. (`get` is
        // cardinality-INDEPENDENT — it is always the first value — so the join is read through `to_json`.)
        let joined = Page::new().field_join("j", "title::text", "|");
        assert_eq!(joined.extract(&doc).to_json(), r#"{"j":"T|LATER"}"#);

        // A field the page never matches leaves the mask unfilled, so the scan runs to EOF and the
        // fields that DO match are still complete — an unsatisfiable schema must not lose values.
        let missing = Page::new().field("t", "title::text").field("nope", "nosuchtag::text");
        let item = missing.extract(&doc);
        assert_eq!(item.get("t"), Some("T"));
        assert_eq!(item.get("nope"), None);

        // Shapes that are NOT armed still answer correctly: a deferred predicate needs a close that may
        // come after the mask fills, and `normalize-space` accumulates over a whole subtree.
        let d = b"<div>x<p>a</p><p>b</p></div><div>y<p>c</p></div>";
        assert_eq!(Page::new().field("x", "p:last-child::text").extract(d).get("x"), Some("b"));
        assert_eq!(Page::new().field("x", "div:has(p)::text").extract(d).get("x"), Some("x"));
        assert_eq!(
            Page::new().field("x", "normalize-space(//div)").extract(d).get("x"),
            Some("xab")
        );
    }

    /// An outer-HTML field IS armed — it is the shape early exit pays best on, since a bare-element
    /// value retains a whole element's source per match — but only while no capturing element is OPEN.
    /// NESTING is what makes that a real case rather than a hypothetical: the inner match satisfies the
    /// column while the outer one is still open, and captures are scattered in START order, so stopping
    /// there would hand the column the outer element's span measured to end-of-input.
    #[test]
    fn an_outer_html_first_field_stops_only_once_no_capture_is_open() {
        let flat = b"<div class=card>A</div><div class=card>B</div><p>tail</p>";
        let page = Page::new().field("c", "div.card");
        assert_eq!(page.extract(flat).get("c"), Some("<div class=card>A</div>"));

        // NESTED: the inner card closes first and satisfies the column while the outer is still open.
        let nested = b"<div class=card id=out><div class=card id=in>I</div>tail</div><p>after</p>";
        let got = page.extract(nested);
        // the full scan's first-by-start value is the OUTER card, with its real end tag
        let full = crate::extract(nested, &["div.card".to_string()], None);
        assert_eq!(got.get("c"), full[0].first().map(String::as_str));
        assert_eq!(
            got.get("c"),
            Some("<div class=card id=out><div class=card id=in>I</div>tail</div>"),
            "a span running to end-of-input would silently swallow the rest of the document"
        );

        // deeper nesting, and a second field so the mask needs more than the capture
        let two = Page::new().field("c", "div.card").field("t", "p::text");
        let doc = b"<div class=card id=a><div class=card id=b><div class=card id=c>x</div></div></div><p>P</p><p>Q</p>";
        let full = crate::extract(doc, &["div.card".to_string(), "p::text".to_string()], None);
        let item = two.extract(doc);
        assert_eq!(item.get("c"), full[0].first().map(String::as_str));
        assert_eq!(item.get("t"), full[1].first().map(String::as_str));
    }
}
