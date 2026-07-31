//! Advisory diagnostics for the schema-audit API. The supported/unsupported **decision** is always
//! made by the real compiler (`compile_query`/`compile_one` produce an empty result for an unsupported
//! query — see [`crate::Support`]); this module only explains a query the compiler already rejected.
//!
//! The reason is therefore *best-effort*: it pattern-matches the query against the documented
//! unsupported taxonomy (see `docs/COMPATIBILITY.md`) to name the likely cause, falling back to a
//! generic message. It is never consulted on the hot extraction path and never contradicts the
//! authoritative decision — at worst it is vague. Keeping it out of the parser preserves the
//! "one matching implementation" rule; this is a separate explainer, not a second parser.

/// A best-effort human explanation for why `q` is unsupported. Assumes the caller already determined
/// (via the real compiler) that `q` does not compile; the string is advisory only.
pub fn reason(q: &str) -> String {
    let qt = q.trim();
    if qt.is_empty() {
        return "empty selector".to_string();
    }
    // Route exactly as the compiler does: a leading `/`, `./`, or a `normalize-space(...)` wrapper is
    // XPath, else CSS.
    if qt.starts_with('/') || qt.starts_with("./") || qt.starts_with("normalize-space(") {
        xpath_reason(qt)
    } else if qt.starts_with('.') && looks_like_xpath_step(qt) {
        // `.foo` is a CSS class; but `.` / `./` / `.//` are XPath context paths. The compiler only
        // treats a leading `./` as XPath, so a bare `.`-path that is not `./…` is reported as CSS.
        xpath_reason(qt)
    } else {
        css_reason(qt)
    }
}

/// A bare `.`-rooted path that the compiler does NOT route to XPath (only `./…` is routed). `.foo` is
/// a CSS class selector, so this is conservative: only `.` alone reads as a stray context step.
fn looks_like_xpath_step(qt: &str) -> bool {
    qt == "."
}

fn contains_ci(hay: &str, needle: &str) -> bool {
    hay.to_ascii_lowercase().contains(needle)
}

/// Does any tag/attr NAME token carry an uppercase ASCII letter? libxml2 lowercases HTML names while
/// XPath name-tests are case-sensitive, so an uppercase literal matches nothing (see `xpath::valid_name`).
fn has_uppercase_name(qt: &str) -> bool {
    // Look at identifier runs that are not inside a quoted attribute VALUE (values may be uppercase).
    let b = qt.as_bytes();
    let mut i = 0;
    let mut quote = 0u8;
    while i < b.len() {
        let c = b[i];
        if quote != 0 {
            if c == quote {
                quote = 0;
            }
            i += 1;
            continue;
        }
        match c {
            b'"' | b'\'' => quote = c,
            b'A'..=b'Z' => return true,
            _ => {}
        }
        i += 1;
    }
    false
}

fn xpath_reason(qt: &str) -> String {
    let msg = if crate::xpath::has_variable_ref(qt) {
        "XPath variable reference (`$name`) is unsupported — Frostwork's API takes no variable \
         bindings (unlike `sel.xpath(q, name=…)`); inline the value as a quoted literal"
    } else if has_unquoted_comparand(qt) {
        "comparison against an unquoted operand — XPath reads `[@a=2]` numerically (`@a=\"02\"` \
         matches) and `[@a=b]` as a node-set compare against child `<b>` elements, neither of which is \
         a byte compare; quote the value (`[@a=\"2\"]`) if a literal is what you meant"
    } else if qt.contains("::") && !qt.contains("()") {
        "unsupported XPath axis — supported axes are child (`/`), descendant (`//`), \
         `following-sibling::` (after a single `/`), and `ancestor::`/`parent::` as an absolute two-step \
         path (`//INNER/ancestor::E`); `preceding[-sibling]::`, `ancestor-or-self::`, `following::` and \
         `child::`/`descendant::` synonyms are not"
    } else if has_positional_predicate(qt) {
        "positional predicate (`[position()<n]`, or `[N]`/`[last()]` combined with another predicate) \
         is unsupported; a SOLE `[N]` (`//li[2]`) or `[last()]`/`[last()-k]` is supported"
    } else if predicate_has_text_test(qt) {
        "this text-content predicate form is unsupported; a SOLE `[.=\"v\"]`, `[contains(.,\"v\")]`, \
         `[text()=\"v\"]`, or `[contains(text(),\"v\")]` on the SUBJECT step is supported (not combined \
         with another predicate, `!=`/`<`/`>`, `normalize-space(...)`, or on a non-subject step)"
    } else if has_unsupported_function(qt) {
        "XPath function (`normalize-space()`, `count()`, `string()`, …) is unsupported; only \
         `contains()`/`starts-with()` over an attribute are supported"
    } else if is_relative_child_anchor(qt) {
        "relative child anchor (`./x`) can't be enforced by the streaming matcher; use the descendant \
         form `.//x` instead"
    } else if is_bare_absolute_nonroot(qt) {
        "a single-slash `/x` selects only the document root element; use `//x` (any depth) or \
         `/html/…`"
    } else if has_uppercase_name(qt) {
        "uppercase name in the path — libxml2 lowercases HTML names but XPath name-tests are \
         case-sensitive, so it matches nothing; lowercase the tag/attribute name"
    } else {
        "outside the supported downward-XPath subset (child/descendant steps, `*`/tag tests, \
         attribute predicates, `text()`/`@attr` terminals)"
    };
    msg.to_string()
}

fn css_reason(qt: &str) -> String {
    let lower = qt.to_ascii_lowercase();
    let msg = if lower.contains(":has(") {
        ":has() is supported as `:has(<compound>)` / `:has(> <compound>)` on ONE compound of a lone \
         selector — the value may be that element's own (`div:has(a)::attr(id)`), its subtree \
         (`div:has(a) ::text`), or a DESCENDANT's (`div:has(a) a::attr(href)`). The inner may be a \
         tag/`*`/id/class/attribute/`:not` compound (id/attribute/`:not` inners are a divergence in our \
         favor; cssselect rejects them). Unsupported: a chain/sibling inside (`:has(.a .b)`, \
         `:has(a + b)`), a comma list, a nested/second `:has`, a positional inner, a group member, or a \
         CHILD step into the value tail (`div:has(a) > p::text` — use the descendant form)"
    } else if lower.contains(":is(") || lower.contains(":where(") {
        ":is()/:where() alternatives must be plain compounds (tag/`*`/class/id/attr/`:not`); a \
         combinator (`:is(a b)`), a positional/reverse/`:has` inside an alternative, or a nested `:is` \
         is unsupported. Combined forms like `div.a:is(.x, .y)` ARE supported (with correct AND \
         semantics — a documented divergence from cssselect 1.4.0's mis-translation)"
    } else if lower.contains(":contains(") {
        ":contains() is unsupported (Frostwork does not match on text content)"
    } else if lower.contains(":nth-last-")
        || lower.contains(":last-child")
        || lower.contains(":last-of-type")
        || lower.contains(":only-child")
        || lower.contains(":only-of-type")
    {
        "a reverse position (`:last-*`/`:only-*`/`:nth-last-*`) is supported on ONE compound of a lone \
         selector, with the value being that element's own (`li:last-child::text`), its subtree \
         (`li:last-child ::text`), or a DESCENDANT's (`li:last-child b::text`); a comma group, a group \
         sub-field, `*`-of-type, or a CHILD step into the value tail (`li:last-child > b::text` — use \
         the descendant form) isn't"
    } else if lower.contains(":nth-") {
        "`:nth-child()`/`:nth-of-type()` on the universal `*` (e.g. `*:nth-of-type(2)`) is \
         unsupported; name the element (`li:nth-of-type(2)`) — that form is supported"
    } else if lower.contains(":not(") && not_arg_has_combinator(qt) {
        ":not() with a combinator argument (e.g. `:not(a b)`) is unsupported; a compound argument \
         (`:not(.x)`) is fine"
    } else if has_case_flag(qt) {
        "case-insensitive attribute flag (`[a=b i]`) is unsupported"
    } else if has_namespace_prefix(qt) {
        "namespace prefix (`ns|tag`) is unsupported"
    } else if lower.contains("::before") || lower.contains("::after") {
        "pseudo-element (`::before`, `::after`) is unsupported"
    } else if has_other_pseudo(qt) {
        "pseudo-class/element is unsupported; supported terminals are `::text` and `::attr(name)`"
    } else if qt.contains(',') {
        "comma group is unsupported here — a member is unsupported, or element (outer-HTML) and value \
         (`::text`/`::attr`) terminals are mixed (which breaks document order)"
    } else {
        "invalid or unsupported CSS selector for the Frostwork subset (tag/`*`, `.class`, `#id`, \
         `[attr]`/`[attr=v]`, descendant/`>`/`+`/`~`, `::text`/`::attr(name)`)"
    };
    msg.to_string()
}

// ---- small structural probes (deliberately coarse; advisory only) ----

/// A `[...]` predicate whose body is a bare integer, `last()`, or `position(`.
fn has_positional_predicate(qt: &str) -> bool {
    each_predicate(qt).any(|p| {
        let t = p.trim();
        t.bytes().all(|b| b.is_ascii_digit()) && !t.is_empty()
            || contains_ci(t, "last()")
            || contains_ci(t, "position(")
    })
}

fn predicate_has_text_test(qt: &str) -> bool {
    each_predicate(qt).any(|p| contains_ci(p, "text()"))
}

/// A predicate comparing against an operand that is not a quoted literal — `[@a=2]`, `[@a=b]`,
/// `[contains(@a,2)]`. Coarse (advisory only): the RHS of the last `=`, or the last function argument, is
/// a bare name/number token (no quotes, no call). Positional bodies (`position()=last()`) carry a call on
/// the right and are left to `has_positional_predicate`.
fn has_unquoted_comparand(qt: &str) -> bool {
    each_predicate(qt).any(|p| {
        let body = p.trim().trim_end_matches(')');
        let rhs = match body.rsplit_once('=').or_else(|| body.rsplit_once(',')) {
            Some((_, r)) => r.trim(),
            None => return false,
        };
        !rhs.is_empty()
            && rhs.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | ':' | '$'))
    })
}

fn has_unsupported_function(qt: &str) -> bool {
    for f in ["normalize-space(", "count(", "string(", "not(", "concat(", "translate(", "substring("]
    {
        if contains_ci(qt, f) {
            return true;
        }
    }
    false
}

/// The path begins with a single-slash CHILD step from the context node (`./x`, not `.//x`).
fn is_relative_child_anchor(qt: &str) -> bool {
    let rest = match qt.strip_prefix('.') {
        Some(r) => r,
        None => return false,
    };
    rest.starts_with('/') && !rest.starts_with("//")
}

/// An absolute single-slash path whose first step is not `html` (`/div`, `/tr`) — matches only the
/// document root, so it is unsupported unless it is the `/html…` form.
fn is_bare_absolute_nonroot(qt: &str) -> bool {
    if !qt.starts_with('/') || qt.starts_with("//") {
        return false;
    }
    let after = qt[1..].trim_start();
    !(after.starts_with("html/") || after == "html" || after.starts_with("html["))
}

/// Iterate the bodies of each top-level `[...]` predicate in a path/selector.
fn each_predicate(qt: &str) -> impl Iterator<Item = &str> {
    let b = qt.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'[' {
            let start = i + 1;
            let mut depth = 1i32;
            i += 1;
            while i < b.len() && depth > 0 {
                match b[i] {
                    b'[' => depth += 1,
                    b']' => depth -= 1,
                    _ => {}
                }
                if depth == 0 {
                    break;
                }
                i += 1;
            }
            if i <= b.len() {
                out.push(&qt[start..i.min(qt.len())]);
            }
        }
        i += 1;
    }
    out.into_iter()
}

/// Is there a `[attr=val i]` / `[attr=val s]` case flag (a trailing ` i`/` s` before `]`)?
fn has_case_flag(qt: &str) -> bool {
    each_predicate(qt).any(|p| {
        let t = p.trim_end();
        (t.ends_with(" i") || t.ends_with(" s") || t.ends_with(" I") || t.ends_with(" S"))
            && t.contains('=')
    })
}

/// A `|` used as a namespace separator inside a compound (not the XPath union, handled elsewhere).
fn has_namespace_prefix(qt: &str) -> bool {
    // crude: a `|` not doubled (`||` is the CSS column combinator, still unsupported but distinct)
    qt.contains('|') && !qt.contains("||")
}

fn not_arg_has_combinator(qt: &str) -> bool {
    // find `:not(` … matching `)` and look for a combinator char/space inside
    if let Some(idx) = qt.to_ascii_lowercase().find(":not(") {
        let inner = &qt[idx + 5..];
        let mut depth = 1i32;
        for (j, c) in inner.char_indices() {
            match c {
                '(' => depth += 1,
                ')' => {
                    depth -= 1;
                    if depth == 0 {
                        let arg = &inner[..j];
                        return arg.contains([' ', '>', '+', '~']);
                    }
                }
                _ => {}
            }
        }
    }
    false
}

/// A `:` or `::` pseudo that is not one of the supported value terminals.
fn has_other_pseudo(qt: &str) -> bool {
    let l = qt.to_ascii_lowercase();
    // strip the two supported terminals, then any remaining `:` is an unsupported pseudo
    let stripped = l.replace("::text", "").replace("::attr(", "@@ATTR@@(");
    stripped.contains(':')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn xpath_categories() {
        assert!(reason("//a[position()<3]").contains("positional"));
        assert!(reason("//a[last()]").contains("positional"));
        // a SUPPORTED text-pred form won't reach reason() in practice; an unsupported one (combined
        // with another predicate) does, and still classifies as a text-content predicate.
        assert!(reason("//p[@x][text()=\"y\"]").contains("text-content"));
        assert!(reason("//ancestor::div").contains("axis"));
        assert!(reason("count(//a)").contains("function") || reason("count(//a)").contains("CSS"));
        assert!(reason("./h3").contains("descendant"));
        assert!(reason("/div").contains("document root"));
        assert!(reason("//DIV").contains("case-sensitive"));
        assert!(reason("//*[@id=$pid]").contains("variable"));
        assert!(reason("//div[@id=foo]").contains("unquoted"));
        assert!(reason("//span[@x=2]/text()").contains("unquoted"));
        // the unquoted probe must not steal the more specific classifications
        assert!(reason("//li[@x][position()=last()]").contains("positional"));
        assert!(reason("//p[@x][text()=\"y\"]").contains("text-content"));
    }

    #[test]
    fn css_categories() {
        assert!(reason("div:has(a)").contains(":has()"));
        assert!(reason("div:contains('x')").contains(":contains()"));
        assert!(reason("li:nth-last-child(2)").contains("nth"));
        assert!(reason("li:last-child").contains("position"));
        assert!(reason("div:not(a b)").contains("combinator argument"));
        assert!(reason("a[href='x' i]").contains("case-insensitive"));
        assert!(reason("svg|rect").contains("namespace"));
        assert!(reason("div::before").contains("pseudo-element"));
        assert!(reason("div:hover").contains("pseudo"));
    }

    #[test]
    fn generic_fallbacks_do_not_panic() {
        for q in ["", "   ", ">>>", "[[[", "div >>> span", "//", "./", ".//"] {
            let _ = reason(q);
        }
    }
}
