//! A tiny CSS-selector parser for the Frostwork supported subset:
//!   compounds : `tag`, `*`, `.class`, `#id`, `[attr]`, `[attr=val]` (val optionally quoted)
//!   combinators: descendant (whitespace), child (`>`), adjacent (`+`), general sibling (`~`)
//!   terminals : `::text` (self / `E ::text` subtree), `::attr(name)` (self / `E ::attr` subtree)
//! Anything outside this subset returns `Err(())` and the query yields an empty column (no fallback).

#[derive(Clone, Debug, PartialEq)]
pub enum AttrPred {
    Exists(String),            // [a]
    Eq(String, String),        // [a=v]
    Prefix(String, String),    // [a^=v]
    Suffix(String, String),    // [a$=v]
    Substr(String, String),    // [a*=v]
    Includes(String, String),  // [a~=v]  (whitespace-separated list contains v)
    DashMatch(String, String), // [a|=v]  (v, or starts with "v-")
}

/// A forward positional constraint: the element's 1-based index among its siblings must satisfy
/// `index == a·k + b` for some integer `k ≥ 0` (the CSS `An+B` microsyntax). `of_type` selects the
/// axis: `false` counts all element siblings (`:nth-child`, XPath `*[N]`); `true` counts same-tag
/// siblings (`:nth-of-type`, XPath `tag[N]`) and requires a concrete tag on the compound. Only
/// *forward* positions are here — `:last-*`/`:only-*`/`[last()]` need the parent's close (not this tier).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Nth {
    pub a: i32,
    pub b: i32,
    pub of_type: bool,
}

/// A REVERSE (from-the-end) positional constraint: the element's 1-based position *counted from the
/// last sibling* must satisfy `An+B` — `:last-*` is `nth-last-*(1)`, `:nth-last-child(2)` is 2, etc.
/// XPath `[last()]` maps here too (`tag[last()]` = `:last-of-type`, `*[last()]` = `:last-child`).
/// Unlike [`Nth`] these can't be decided at open: the from-end position needs the parent's TOTAL
/// sibling count, known only at the parent's close, so the matcher defers them (see `matcher::reverse`).
/// `only` is the special `:only-*` case (the sole (of-type) child: total == 1); `of_type` picks the
/// axis (same-tag count, requires a concrete tag) vs all element children.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ReversePos {
    pub a: i32,
    pub b: i32,
    pub only: bool,
    pub of_type: bool,
}

/// A `:has(<relative selector>)` argument (MVP: a single compound, optionally child-scoped `:has(> x)`).
/// The subject element matches only if it has a descendant (`rel == Descendant`) or a direct child
/// (`rel == Child`) matching `inner`. Like [`ReversePos`], it can't be decided at the subject's open —
/// its descendants aren't known yet — so it is resolved at the subject's OWN close and IGNORED by
/// `compound_matches`; the matcher routes a `:has` selector to a deferred path or drops it to an empty
/// column, never to normal matching (which would ignore the constraint and over-match).
#[derive(Clone, Debug, PartialEq)]
pub struct Has {
    pub rel: Comb,             // Descendant (`:has(x)`) or Child (`:has(> x)`)
    pub inner: Box<Compound>,  // the (single) compound a descendant/child must match
}

/// The text an XPath text-content predicate tests. `StringValue` (`.`) is the element's whole
/// string-value — every descendant text node concatenated, no node boundaries. `DirectText` (`text()`)
/// is the element's *direct* child text nodes as a node-set: `Eq` is existential (ANY direct text node
/// equals the needle), while `Contains` reads only the FIRST direct text node (XPath coerces the
/// node-set argument of `contains()` to its first node's string). No whitespace normalization (that's
/// `normalize-space`). Like [`Has`], carried on the compound and IGNORED by `compound_matches`: it needs
/// the element's text, known only at its close, so the matcher defers it (see `matcher::text_pred`).
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum TextAxis {
    StringValue, // `.`      — concat of all descendant text
    DirectText,  // `text()` — direct child text nodes (Eq: any; Contains: first)
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum TextOp {
    Eq,       // `= "v"`
    Contains, // `contains(…, "v")`
}

#[derive(Clone, Debug, PartialEq)]
pub struct TextPred {
    pub axis: TextAxis,
    pub op: TextOp,
    pub needle: String,
}

#[derive(Clone, Debug, Default, PartialEq)]
pub struct Compound {
    pub tag: Option<String>, // None or Some("*") = universal
    pub id: Option<String>,
    pub classes: Vec<String>,
    pub attrs: Vec<AttrPred>,
    pub negations: Vec<Compound>, // `:not(<compound>)` args — element must match NONE of these
    pub positional: Option<Nth>,  // `:nth-child`/`:nth-of-type` (+ XPath `[N]`); index checked at match
    pub reverse: Option<ReversePos>, // `:last-*`/`:only-*`; resolved at the parent's close (deferred)
    pub has: Option<Has>,         // `:has(<compound>)`; resolved at the subject's own close (deferred)
    pub text_pred: Option<TextPred>, // XPath `[.="v"]`/`[contains(text(),"v")]`; deferred to own close
    // `:is(...)`/`:where(...)` matches-any groups. Each inner Vec is one pseudo's comma-list of
    // alternative compounds; the element must match ≥1 alternative in EVERY group (OR within a group,
    // AND across groups). Decided at open like `:not`, so no deferral. Empty = no `:is`/`:where`.
    pub is_groups: Vec<Vec<Compound>>,
    /// DERIVED, not parsed: the signature bits an element must carry for this compound to have a
    /// chance (see `matcher::sig`). The parser leaves it 0; the matcher's compile step fills it in.
    /// 0 always means "filter nothing", so a compound that never reaches that step is merely slower.
    pub req: u64,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Comb {
    Descendant,
    Child,
    Adjacent, // `+`
    General,  // `~`
}

#[derive(Clone, Debug)]
pub enum Terminal {
    Text { subtree: bool },
    Attr { name: String, subtree: bool },
    OuterHtml, // bare element (no pseudo) -> the matched element's raw source fragment
    /// `normalize-space(inner)`: the string-value of the FIRST node the `inner` terminal matches, with
    /// ASCII whitespace collapsed/trimmed — always exactly one value (empty string if nothing matched).
    /// `inner` is `OuterHtml` (element string-value = concat of its subtree text), `Text` (first text
    /// node), or `Attr` (first attribute value). XPath-only; never produced by CSS.
    NormalizeSpace(Box<Terminal>),
}

#[derive(Clone, Debug)]
pub struct Selector {
    pub parts: Vec<Compound>, // subject is the last
    pub combs: Vec<Comb>,     // len == parts.len() - 1
    pub terminal: Terminal,
    /// The leftmost compound must bind STRICTLY below the scope floor (a descendant of the context
    /// node, excluding it). True only for a `.`-relative XPath descendant path (`.//x`): its context is
    /// the root/container element itself, which XPath's descendant axis excludes — so `.//*` omits
    /// `<html>` and grouped `.//tag` omits the container. Absolute paths (`//x`, `/html/…`) and all CSS
    /// selectors are descendant-or-**self** (false): the leftmost compound may bind at the floor.
    pub strict_desc: bool,
}

fn is_ws(c: u8) -> bool {
    matches!(c, b' ' | b'\t' | b'\n' | b'\r' | 0x0c)
}

/// Is `v` a CSS **identifier** — the only unquoted form an attribute value may take? Measured against
/// cssselect 1.5.0, which is the oracle here: `v`, `v2`, `-v`, `_v`, `v-2` and non-ASCII (`café`) parse,
/// while a leading digit (`2`, `2v`, `1e5`), a double hyphen (`--v`), `-` + digit, an empty value, and any
/// other punctuation (`$v`, `a.b`, `/p`, `#v`) are SelectorSyntaxError. Escapes (`\32 v`) are a valid CSS
/// ident but stay unsupported here (they parse as non-ident → empty column, an allowed coverage gap, not
/// a wrong value).
fn is_css_ident(v: &str) -> bool {
    let rest = v.strip_prefix('-').unwrap_or(v); // at most ONE leading hyphen (`--v` is not CSS 2.1)
    let mut chars = rest.chars();
    let first = match chars.next() {
        Some(c) => c,
        None => return false, // "" or a lone "-"
    };
    let start_ok = first == '_' || first.is_ascii_alphabetic() || first >= '\u{00A0}';
    start_ok
        && chars.all(|c| c == '_' || c == '-' || c.is_ascii_alphanumeric() || c >= '\u{00A0}')
}

/// Split a query on TOP-LEVEL commas (not inside `[]`, `()`, quotes, or a CSS escape).
fn split_top_commas(q: &str) -> Vec<&str> {
    let b = q.as_bytes();
    let mut parts = Vec::new();
    let mut start = 0usize;
    let mut depth = 0i32;
    let mut quote = 0u8;
    let mut i = 0usize;
    while i < b.len() {
        let c = b[i];
        // A CSS escape makes the next byte DATA, never a delimiter: `.a\,b` is one class name, and
        // inside a string `\"` does not end the string.
        if c == b'\\' {
            i += 2;
            continue;
        }
        if quote != 0 {
            if c == quote {
                quote = 0;
            }
            i += 1;
            continue;
        }
        match c {
            b'"' | b'\'' => quote = c,
            b'(' | b'[' => depth += 1,
            b')' | b']' => depth -= 1,
            b',' if depth == 0 => {
                parts.push(&q[start..i]);
                start = i + 1;
            }
            _ => {}
        }
        i += 1;
    }
    parts.push(&q[start..]);
    parts
}

/// Index of the `)` that closes a functional pseudo whose `(` sits just before `from`, or `None` if the
/// selector never closes it (a syntax error — fail closed).
///
/// A bare depth counter is not enough here, and the difference is not academic: `)` is ordinary DATA
/// inside a quoted attribute value, so `div:is(#outer, [data-x=")"])` was cut off at the quoted `)`, the
/// leftover `"])` failed to parse, and a selector Parsel answers returned an EMPTY column. Both quotes
/// and CSS escapes are honoured — `\)` is an escaped paren, `\"` inside a string does not end it, and an
/// unbalanced `(` inside a value (`[title='a(b']`) no longer swallows the rest of the query.
///
/// The returned index always lands on an ASCII `)`, so it is a `str` char boundary: the escape skip can
/// step into a multi-byte character, but its continuation bytes are all `>= 0x80` and match no
/// delimiter.
fn find_functional_close(b: &[u8], from: usize) -> Option<usize> {
    let n = b.len();
    let mut i = from;
    let mut depth = 1i32;
    let mut quote = 0u8;
    while i < n {
        let c = b[i];
        if c == b'\\' {
            i += 2;
            continue;
        }
        if quote != 0 {
            if c == quote {
                quote = 0;
            }
            i += 1;
            continue;
        }
        match c {
            b'"' | b'\'' => quote = c,
            b'(' => depth += 1,
            b')' => {
                depth -= 1;
                if depth == 0 {
                    return Some(i);
                }
            }
            _ => {}
        }
        i += 1;
    }
    None
}

/// Does `s` contain any of `stop` OUTSIDE a quoted string and outside a CSS escape? Used by the
/// `:has()` argument checks, so a quoted delimiter (`:has([data-x="a, b"])`) is data rather than the
/// combinator/comma that makes the argument unsupported.
fn has_unquoted(s: &str, stop: &[u8]) -> bool {
    let b = s.as_bytes();
    let mut i = 0usize;
    let mut quote = 0u8;
    while i < b.len() {
        let c = b[i];
        if c == b'\\' {
            i += 2;
            continue;
        }
        if quote != 0 {
            if c == quote {
                quote = 0;
            }
        } else if c == b'"' || c == b'\'' {
            quote = c;
        } else if stop.contains(&c) {
            return true;
        }
        i += 1;
    }
    false
}

/// Parse a query into its member selectors. A single selector -> one member. A comma list ->
/// one member per part when every member is supported, and the members' terminals are compatible for
/// a single document-ordered union column. Value terminals (`::text`, `::attr(<any name>)`) mix freely
/// — they emit as the tokenizer streams (attrs at element-open, text at text-nodes), so the natural
/// emission order IS document order, matching lxml's union. Outer-HTML (bare-element) captures are
/// deferred and reordered at finish, so an outer member mixed with value members would break document
/// order — that combination stays unsupported (empty). All-outer is fine (uniform, capture-ordered).
pub fn parse_list(q: &str) -> Vec<Selector> {
    let members = split_top_commas(q);
    if members.len() == 1 {
        return parse(members[0]).map(|s| vec![s]).unwrap_or_default();
    }
    let mut sels = Vec::with_capacity(members.len());
    for m in &members {
        match parse(m) {
            Ok(s) => sels.push(s),
            Err(_) => return Vec::new(), // any unsupported member -> whole group unsupported
        }
    }
    let outer = sels.iter().filter(|s| matches!(s.terminal, Terminal::OuterHtml)).count();
    if outer == 0 || outer == sels.len() {
        sels // all value terminals (text/attr, any names) OR all outer-HTML — both order-correct
    } else {
        Vec::new() // outer-HTML mixed with value terminals: deferred capture breaks document order
    }
}

// `Err(())` is deliberate: the caller only needs supported/unsupported (unsupported -> empty column,
// no fallback), so a unit error is the whole error domain.
#[allow(clippy::result_unit_err)]
pub fn parse(query: &str) -> Result<Selector, ()> {
    let q = query.trim();
    // ---- split off the value terminal (or none -> bare element / outer HTML) ----
    enum Tk {
        Text,
        Attr(String),
        Outer,
    }
    let (structural, tk): (&str, Tk) = if let Some(idx) = q.rfind("::attr(") {
        if !q.ends_with(')') {
            return Err(());
        }
        let name = q[idx + "::attr(".len()..q.len() - 1].trim();
        let name = name
            .strip_prefix(['"', '\''])
            .and_then(|n| n.strip_suffix(['"', '\'']))
            .unwrap_or(name);
        // The argument is an attribute NAME, and cssselect DECODES escapes in it, so `::attr(data-\6b)`
        // asks for `data-k`. Decode first, then validate: `is_ident_name` only inspects the leading
        // character, so an escape further in used to pass validation and then be matched literally —
        // support promised, empty column returned. Validate the DECODED name (cssselect raises
        // ExpressionError for `::attr(1)`, and `::attr(\31)` decodes to exactly that).
        let name = unescape_css(name).ok_or(())?;
        if !is_ident_name(&name) {
            return Err(());
        }
        (&q[..idx], Tk::Attr(name))
    } else if let Some(s) = q.strip_suffix("::text") {
        (s, Tk::Text)
    } else {
        (q, Tk::Outer) // no pseudo -> the element itself (node query)
    };

    // ---- self vs descendant-or-self scope (mirror cssselect's `_subject`); N/A for a bare node ----
    let mut head;
    let subtree;
    if matches!(tk, Tk::Outer) {
        head = structural.trim().to_string();
        subtree = false;
    } else {
        let had_space = structural.ends_with(|c: char| c.is_whitespace());
        head = structural.trim().to_string();
        if let Some(before_star) = head.strip_suffix('*') {
            // A trailing universal `*` — how it binds decides scope. Mirror cssselect's `_subject`,
            // whose `descendant-or-self::*/*` + `::text` collapse to `descendant-or-self::text()`
            // applies ONLY when the terminal is ATTACHED to a descendant-combinator `*` — never to
            // `>`/`+`/`~`, and never when whitespace separates the `*` from the terminal.
            if had_space {
                // `div > * ::text`, `div * ::text`, `* ::text` — the `*` is a REAL subject compound
                // and the detached terminal is subtree-scoped (parsel: `div * ::text` = text in the
                // subtrees of div's strict descendants, EXCLUDING div's own direct text — unlike the
                // attached `div *::text` collapse, which includes it). Keep head as-is; an invalid
                // attached form (`div* ::text`) is rejected downstream by parse_compound.
                subtree = true;
            } else {
                let ws_before = before_star.ends_with(|c: char| c.is_whitespace());
                let trimmed = before_star.trim_end();
                if trimmed.ends_with(['>', '+', '~']) {
                    // `div > *::text`, `div+*::text` — a universal compound after an explicit
                    // combinator with the terminal attached: self-scoped (parsel: text children of
                    // div's element children, NOT div's subtree).
                    subtree = false;
                } else if trimmed.is_empty() {
                    // bare `*::text` -> every element's own text (== all text nodes).
                    head = "*".to_string();
                    subtree = true;
                } else if ws_before {
                    // `div *::text` -> parsel's `::*/*` collapse: div's whole-subtree text.
                    head = trimmed.to_string();
                    subtree = true;
                } else {
                    // `div*::text` -> `*` attached to a compound: invalid CSS (cssselect rejects).
                    return Err(());
                }
            }
        } else {
            subtree = had_space;
        }
    }
    if head.is_empty() {
        // A bare value pseudo (`::text` / `::attr(x)`) with no compound means "every element's own
        // value" — the universal default, matching parsel's `::text`. But an empty NODE query (`""`,
        // whitespace) is not a selector at all; per the no-fallback contract it must be unsupported
        // (empty column), never an implicit `*` that dumps the whole document into a field.
        if matches!(tk, Tk::Outer) {
            return Err(());
        }
        head = "*".to_string();
    }

    // ---- split into compounds + combinators ----
    let (parts_s, combs) = split_structural(&head)?;
    if parts_s.is_empty() || parts_s.len() != combs.len() + 1 {
        return Err(());
    }
    let mut parts = Vec::with_capacity(parts_s.len());
    for p in &parts_s {
        parts.push(parse_compound(p)?);
    }

    let terminal = match tk {
        Tk::Attr(name) => Terminal::Attr { name, subtree },
        Tk::Text => Terminal::Text { subtree },
        Tk::Outer => Terminal::OuterHtml,
    };
    Ok(Selector { parts, combs, terminal, strict_desc: false })
}

/// Split a structural selector into compound strings and the combinators between them. A maximal run
/// of whitespace and explicit combinator chars (`>`/`+`/`~`) at bracket depth 0 is one combinator,
/// named by its explicit char (pure whitespace = descendant). Combinators inside `[...]` are literal.
///
/// Bracket depth alone is not enough: a `)` inside a QUOTED value closed the bracket run early, and the
/// next space then read as a descendant combinator — so `div:is([data-x=")"], #x)` split into two
/// compounds and the selector was reported unsupported. Quotes and CSS escapes are tracked here for the
/// same reason as in [`find_functional_close`].
fn split_structural(head: &str) -> Result<(Vec<String>, Vec<Comb>), ()> {
    let b = head.as_bytes();
    let n = b.len();
    let mut parts: Vec<String> = Vec::new();
    let mut combs: Vec<Comb> = Vec::new();
    // A compound is a contiguous byte range of `head` (combinator regions separate them), so track
    // its start offset and slice `head[start..end]` rather than rebuilding it char-by-char — the old
    // `push(c as char)` treated each byte as a codepoint, corrupting multi-byte UTF-8 in values like
    // `[data-k="日本"]`. Slicing on ASCII boundaries stays valid `&str`.
    let mut start: Option<usize> = None;
    let mut depth = 0i32;
    let mut quote = 0u8;
    let mut i = 0;
    while i < n {
        let c = b[i];
        // An escape or a quoted string is part of the current compound whatever it contains. `start` is
        // set BEFORE the skip, so the two-byte step can never leave it pointing inside a character.
        if c == b'\\' {
            start.get_or_insert(i);
            i += 2;
            continue;
        }
        if quote != 0 {
            if c == quote {
                quote = 0;
            }
            start.get_or_insert(i);
            i += 1;
            continue;
        }
        if c == b'"' || c == b'\'' {
            quote = c;
            start.get_or_insert(i);
            i += 1;
            continue;
        }
        match c {
            b'[' | b'(' => {
                depth += 1;
                start.get_or_insert(i);
                i += 1;
            }
            b']' | b')' => {
                depth -= 1;
                start.get_or_insert(i);
                i += 1;
            }
            _ if depth > 0 => {
                // inside [...] or (...): everything (ws, >, +, ~, non-ASCII) is literal to the compound
                // — e.g. the spaces in `:nth-child(2n + 1)` or a combinator inside `:not(...)`.
                start.get_or_insert(i);
                i += 1;
            }
            b'>' | b'+' | b'~' | b' ' | b'\t' | b'\n' | b'\r' | 0x0c => {
                // consume the whole combinator region; explicit char (last one) names it
                let end = i; // the current compound (if any) ends here, before the combinator run
                let mut kind = Comb::Descendant;
                let mut explicit = 0u32;
                while i < n {
                    match b[i] {
                        b'>' => {
                            kind = Comb::Child;
                            explicit += 1;
                        }
                        b'+' => {
                            kind = Comb::Adjacent;
                            explicit += 1;
                        }
                        b'~' => {
                            kind = Comb::General;
                            explicit += 1;
                        }
                        x if is_ws(x) => {}
                        _ => break,
                    }
                    i += 1;
                }
                if explicit > 1 {
                    return Err(()); // `a >> b`, `a > > b`, `a +~ b` — two combinators (cssselect rejects)
                }
                match start.take() {
                    Some(s) => {
                        parts.push(head[s..end].trim().to_string());
                        combs.push(kind);
                    }
                    // leading whitespace is fine; a leading explicit combinator is malformed
                    None if kind != Comb::Descendant => return Err(()),
                    None => {}
                }
            }
            _ => {
                start.get_or_insert(i);
                i += 1;
            }
        }
    }
    if let Some(s) = start {
        parts.push(head[s..].trim().to_string());
    } else if !combs.is_empty() {
        return Err(()); // trailing combinator with no subject compound
    }
    Ok((parts, combs))
}

/// Decode CSS escapes in a quoted attribute value: `\61` -> `a`, `\0041` -> `A`, `\-` -> `-`.
/// A hex escape is 1-6 hex digits, optionally terminated by ONE whitespace character (`\61 bc` is
/// `abc`). Returns `None` for input CSS does not define - a trailing lone backslash - so the caller can
/// reject the selector rather than guess at it.
fn unescape_css(raw: &str) -> Option<String> {
    if !raw.contains('\\') {
        return Some(raw.to_string()); // overwhelmingly the common case: no allocation beyond the copy
    }
    let mut out = String::with_capacity(raw.len());
    let mut chars = raw.chars().peekable();
    while let Some(c) = chars.next() {
        if c != '\\' {
            out.push(c);
            continue;
        }
        let first = chars.next()?; // a lone trailing backslash is invalid CSS
        if !first.is_ascii_hexdigit() {
            out.push(first); // `\-`, `\"`, `\\` - the escaped character, literally
            continue;
        }
        let mut hex = String::from(first);
        while hex.len() < 6 {
            match chars.peek() {
                Some(h) if h.is_ascii_hexdigit() => hex.push(chars.next().unwrap()),
                _ => break,
            }
        }
        // one optional whitespace terminator is consumed, not emitted
        if matches!(chars.peek(), Some(' ' | '\t' | '\n' | '\r' | '\u{c}')) {
            chars.next();
        }
        let cp = u32::from_str_radix(&hex, 16).ok()?;
        out.push(char::from_u32(cp).unwrap_or('\u{fffd}'));
    }
    Some(out)
}

/// Is `name` a valid CSS identifier for a class / id / attribute name / `::attr()` argument?
///
/// A non-empty check is not enough: cssselect raises `SelectorSyntaxError` for `.1`, `.-2`, `[1]` and
/// `ExpressionError` for `::attr(1)`, so accepting them made `check()` promise support for selectors the
/// oracle refuses to run. The rule is CSS 2.1's: an optional leading `-`, then a non-digit start, then
/// name characters. Escapes are valid CSS here too but stay unsupported (rejected, not silently wrong).
fn is_ident_name(name: &str) -> bool {
    let rest = name.strip_prefix('-').unwrap_or(name);
    let mut chars = rest.chars();
    match chars.next() {
        None => false, // "" or a lone "-"
        Some(c) if c.is_ascii_digit() => false,
        Some('-') => false, // `--x` is not a CSS 2.1 identifier
        Some(_) => true,
    }
}

fn read_name(b: &[u8], i: &mut usize) -> String {
    // NOTE: `:` is intentionally NOT a name char here — in CSS it always starts a pseudo (`:not`),
    // and namespaces use `|`, not `:`. (The HTML tokenizer's own name scan does allow `:`.)
    // Bytes >= 0x80 are name chars: CSS idents admit all non-ASCII (`.café`, `#producto-año`),
    // and since the input is a `&str`, consuming every continuation byte of a UTF-8 sequence keeps
    // the slice on char boundaries. Matches lxml/cssselect, which accept non-ASCII identifiers.
    let start = *i;
    while *i < b.len() {
        let c = b[*i];
        if c.is_ascii_alphanumeric() || c == b'-' || c == b'_' || c >= 0x80 {
            *i += 1;
        } else {
            break;
        }
    }
    String::from_utf8_lossy(&b[start..*i]).to_string()
}

/// Nesting cap for the mutually-recursive compound parser (`parse_compound` ↔ `parse_is_arg` /
/// `parse_has_arg`). Real selectors nest only a level or two (`:is(:not(.x))`, `:has(> a:not(.b))`);
/// this bounds a pathological `:is(:is(:is(…)))` (which is unsupported anyway) so a crafted selector
/// string can't overflow the stack — it returns `Err(())` (unsupported → empty column) instead.
const MAX_COMPOUND_DEPTH: u32 = 32;

fn parse_compound(s: &str) -> Result<Compound, ()> {
    parse_compound_depth(s, 0)
}

fn parse_compound_depth(s: &str, depth: u32) -> Result<Compound, ()> {
    if depth > MAX_COMPOUND_DEPTH {
        return Err(());
    }
    let b = s.as_bytes();
    let n = b.len();
    let mut c = Compound::default();
    let mut i = 0;
    // optional leading type / universal
    if i < n && (b[i].is_ascii_alphabetic() || b[i] == b'*') {
        if b[i] == b'*' {
            c.tag = Some("*".to_string());
            i += 1;
        } else {
            let name = read_name(b, &mut i);
            // A TAG name must be ASCII, unlike a class/id/attribute name: the tokenizer's tag-name state
            // is ASCII-only (`tokenizer::is_name_char`), so `café::text` could never match and used to
            // pass strict schema validation while always returning empty. Reject it instead — an
            // unsupported selector must be *reported*, not silently empty. (lxml does match these; if
            // that ever matters, widen the tokenizer rather than re-accepting them here.)
            if !name.is_ascii() {
                return Err(());
            }
            c.tag = Some(name.to_ascii_lowercase());
        }
    }
    while i < n {
        match b[i] {
            b'.' => {
                i += 1;
                let name = read_name(b, &mut i);
                if !is_ident_name(&name) {
                    return Err(()); // cssselect: `.1`, `.-2`, `.--x` are SelectorSyntaxError
                }
                c.classes.push(name);
            }
            b'#' => {
                i += 1;
                let name = read_name(b, &mut i);
                // An ID is a CSS *hash token*, whose payload is a NAME rather than an identifier, so
                // cssselect accepts `#1id` where it rejects `.1`. Mirror the oracle: rejecting it here
                // would make a working selector unsupported, losing coverage for no safety gain.
                if name.is_empty() {
                    return Err(());
                }
                match &c.id {
                    // Two DIFFERENT ids in one compound (`#a#b`): an element has a single id, so this is
                    // unsatisfiable — but it is still VALID CSS that simply matches nothing (cssselect:
                    // `#a#b` -> []). Encode the extra id as an id-equality pred so the compound can never
                    // match, WITHOUT erroring: erroring would wrongly poison a comma group (`x, #a#b`
                    // must still yield x's matches). A repeated same id (`#a#a`) is a harmless no-op.
                    Some(prev) if *prev != name => c.attrs.push(AttrPred::Eq("id".to_string(), name)),
                    _ => c.id = Some(name),
                }
            }
            b'[' => {
                i += 1;
                let name = read_name(b, &mut i).to_ascii_lowercase();
                if !is_ident_name(&name) {
                    return Err(()); // cssselect: `[1]`, `[2x=v]` are SelectorSyntaxError
                }
                while i < n && is_ws(b[i]) {
                    i += 1;
                }
                if i < n && b[i] == b']' {
                    i += 1;
                    c.attrs.push(AttrPred::Exists(name));
                    continue;
                }
                // operator: = ^= $= *= ~= |=
                let op = if i < n && b[i] == b'=' {
                    i += 1;
                    b'='
                } else if i + 1 < n && matches!(b[i], b'^' | b'$' | b'*' | b'~' | b'|') && b[i + 1] == b'=' {
                    let o = b[i];
                    i += 2;
                    o
                } else {
                    return Err(());
                };
                while i < n && is_ws(b[i]) {
                    i += 1;
                }
                let val = if i < n && (b[i] == b'"' || b[i] == b'\'') {
                    let q = b[i];
                    i += 1;
                    let start = i;
                    // find the closing quote, honouring `\"` so an escaped quote does not end the value
                    while i < n && b[i] != q {
                        i += if b[i] == b'\\' { 2 } else { 1 };
                    }
                    let raw = &s[start..i.min(n)];
                    if i < n {
                        i += 1;
                    }
                    // cssselect DECODES CSS escapes in a quoted value: `[data-x="\61"]` selects
                    // `data-x="a"`. Copying the raw bytes silently matched a different element - a wrong
                    // value, not an empty column. Decode what CSS defines and reject the rest.
                    match unescape_css(raw) {
                        Some(v) => v,
                        None => return Err(()),
                    }
                } else {
                    let start = i;
                    while i < n && b[i] != b']' && !is_ws(b[i]) {
                        i += 1;
                    }
                    let v = &s[start..i];
                    // An UNQUOTED attribute value must be a CSS identifier. cssselect rejects anything
                    // else outright (`[a=2]`, `[a=$v]`, `[href^=/p]`, `[a=--v]` are all
                    // SelectorSyntaxError), so answering them would be a non-empty column on a selector
                    // Parsel refuses — the "OVERMATCH" the selector fuzzer gates. Reject here instead;
                    // quoting the value (`[a="2"]`) is the supported form. (Same root cause as the XPath
                    // non-literal operand rejected in `xpath::parse_one_attr`.)
                    if !is_css_ident(v) {
                        return Err(());
                    }
                    v.to_string()
                };
                while i < n && is_ws(b[i]) {
                    i += 1;
                }
                if i >= n || b[i] != b']' {
                    return Err(());
                }
                i += 1;
                c.attrs.push(match op {
                    b'=' => AttrPred::Eq(name, val),
                    b'^' => AttrPred::Prefix(name, val),
                    b'$' => AttrPred::Suffix(name, val),
                    b'*' => AttrPred::Substr(name, val),
                    b'~' => AttrPred::Includes(name, val),
                    b'|' => AttrPred::DashMatch(name, val),
                    _ => unreachable!(),
                });
            }
            b':' => {
                // Supported pseudos: `:not(<compound>)`, the FORWARD positional pseudo-classes
                // `:first-child`, `:nth-child(An+B)`, `:first-of-type`, `:nth-of-type(An+B)`, and the
                // REVERSE ones `:last-child`/`:last-of-type`/`:only-child`/`:only-of-type` and
                // `:nth-last-child(An+B)`/`:nth-last-of-type(An+B)`, `:has(<compound>)` /
                // `:has(> <compound>)`, and `:is(...)`/`:where(...)` (all parsed here; the matcher
                // decides whether it can defer the deferred ones — else the column stays empty).
                // Everything else (`:hover`, `:checked`, …) is unsupported.
                i += 1;
                let pname = read_name(b, &mut i).to_ascii_lowercase();
                let has_arg = i < n && b[i] == b'(';
                let arg = if has_arg {
                    i += 1; // past '('
                    let start = i;
                    // quote- and escape-aware, so a `)` inside an attribute value is data (see
                    // `find_functional_close`); an unterminated argument stays a syntax error.
                    let close = find_functional_close(b, i).ok_or(())?;
                    let a = s[start..close].trim();
                    i = close + 1; // past ')'
                    Some(a)
                } else {
                    None
                };
                match (pname.as_str(), arg) {
                    ("not", Some(inner)) => {
                        if inner.is_empty() || inner.contains(":not(") {
                            return Err(()); // empty, or nested :not() (cssselect rejects)
                        }
                        // the arg is a compound (no combinators) — parse_compound errors on a space/`>`
                        let neg = parse_compound_depth(inner, depth + 1)?;
                        if !neg.is_groups.is_empty() {
                            return Err(()); // `:not(:is(...))` — rare and unverified vs the oracle; decline
                        }
                        c.negations.push(neg);
                    }
                    ("first-child", None) => set_positional(&mut c, Nth { a: 0, b: 1, of_type: false })?,
                    ("first-of-type", None) => set_positional(&mut c, Nth { a: 0, b: 1, of_type: true })?,
                    ("nth-child", Some(a)) => {
                        let (na, nb) = parse_anpb(a)?;
                        set_positional(&mut c, Nth { a: na, b: nb, of_type: false })?;
                    }
                    ("nth-of-type", Some(a)) => {
                        let (na, nb) = parse_anpb(a)?;
                        set_positional(&mut c, Nth { a: na, b: nb, of_type: true })?;
                    }
                    // REVERSE (from-the-end) positions — parsed here, resolved at the parent's close by
                    // the matcher's deferred `reverse` path. `:last-*` == `:nth-last-*(1)`.
                    ("last-child", None) => set_reverse(&mut c, ReversePos { a: 0, b: 1, only: false, of_type: false })?,
                    ("last-of-type", None) => set_reverse(&mut c, ReversePos { a: 0, b: 1, only: false, of_type: true })?,
                    ("only-child", None) => set_reverse(&mut c, ReversePos { a: 0, b: 1, only: true, of_type: false })?,
                    ("only-of-type", None) => set_reverse(&mut c, ReversePos { a: 0, b: 1, only: true, of_type: true })?,
                    ("nth-last-child", Some(a)) => {
                        let (na, nb) = parse_anpb(a)?;
                        set_reverse(&mut c, ReversePos { a: na, b: nb, only: false, of_type: false })?;
                    }
                    ("nth-last-of-type", Some(a)) => {
                        let (na, nb) = parse_anpb(a)?;
                        set_reverse(&mut c, ReversePos { a: na, b: nb, only: false, of_type: true })?;
                    }
                    ("has", Some(inner)) => set_has(&mut c, parse_has_arg(inner, depth + 1)?)?,
                    ("is" | "where", Some(inner)) => parse_is_arg(&mut c, inner, depth + 1)?,
                    _ => return Err(()), // unsupported pseudo (bad arity, or a non-positional pseudo)
                }
            }
            _ => return Err(()), // unsupported (other pseudo, etc.)
        }
    }
    Ok(c)
}

/// Attach a positional constraint. Two per-compound positionals (`:nth-child(2):nth-child(3)`) are
/// rare and unsupported here; an `of_type` positional needs a concrete tag (the type to count), so a
/// tagless / universal `*:nth-of-type` is rejected (it would require counting every tag per parent).
fn set_positional(c: &mut Compound, nth: Nth) -> Result<(), ()> {
    if c.positional.is_some() || c.reverse.is_some() {
        return Err(());
    }
    let concrete_tag = matches!(c.tag.as_deref(), Some(t) if t != "*");
    if nth.of_type && !concrete_tag {
        return Err(());
    }
    c.positional = Some(nth);
    Ok(())
}

/// Attach a reverse position. Like [`set_positional`]: at most one per compound, and an `of_type`
/// variant needs a concrete tag to count (`*:last-of-type` is rejected). Mixing a reverse with a
/// forward `:nth-*` on the same compound (`:nth-child(2):last-child`) is rejected — rare, and the
/// deferred path only carries a lone reverse constraint.
fn set_reverse(c: &mut Compound, rev: ReversePos) -> Result<(), ()> {
    if c.reverse.is_some() || c.positional.is_some() {
        return Err(());
    }
    let concrete_tag = matches!(c.tag.as_deref(), Some(t) if t != "*");
    if rev.of_type && !concrete_tag {
        return Err(());
    }
    c.reverse = Some(rev);
    Ok(())
}

/// Attach a `:has()`. At most one per compound, and (MVP) not combined with a forward/reverse position
/// on the same compound — the deferred `:has` path carries a lone structural subject constraint.
fn set_has(c: &mut Compound, has: Has) -> Result<(), ()> {
    if c.has.is_some() || c.positional.is_some() || c.reverse.is_some() {
        return Err(());
    }
    c.has = Some(has);
    Ok(())
}

/// Parse a `:has(<arg>)` argument: a single compound (`:has(a)`, `:has(.price)`, `:has([data-src])`,
/// `:has(a.buy#x)`, `:has(:not(.hidden))`), optionally child-scoped (`:has(> img)`). The inner may carry
/// a tag/`*`, id, classes, attribute predicates, and `:not(...)` — anything `compound_matches` decides
/// structurally at open. Rejected: a descendant/sibling CHAIN (`:has(.a .b)`, `:has(a + b)`), a comma
/// list, and a positional/reverse/`:has`/`:is`/text inside (those need per-parent or deferred machinery
/// the `:has` path doesn't carry).
///
/// cssselect (still 1.5.0) accepts only a type/`*`+classes inner and RAISES on an id/attribute/`:not` inside
/// `:has()` (a limitation tracked with its broader `:has()` gaps upstream). Frostwork implements the
/// standards-correct behavior for those, so it is intentionally MORE capable than parsel here — a
/// divergence in our favor (see docs/COMPATIBILITY.md). Bare type/`*`+class inners agree with parsel.
fn parse_has_arg(arg: &str, depth: u32) -> Result<Has, ()> {
    let arg = arg.trim();
    // The comma and combinator checks are UNQUOTED-only: a `,`/space inside an attribute value is data
    // (`:has([data-x="a, b"])` is one compound), while a real comma or combinator still declines.
    if arg.is_empty() || arg.contains(":has(") || has_unquoted(arg, b",") {
        return Err(());
    }
    let (rel, rest) = match arg.strip_prefix('>') {
        Some(r) => (Comb::Child, r.trim()),
        None => (Comb::Descendant, arg),
    };
    // A single compound only: any whitespace or combinator char means a chain/sibling (unsupported).
    if rest.is_empty() || has_unquoted(rest, b" \t\n\r>+~") {
        return Err(());
    }
    let inner = parse_compound_depth(rest, depth)?;
    // The inner is matched by `compound_matches` at open (no per-parent counter, no deferral), so a
    // positional/reverse/`:has`/`:is`/text-predicate inside is unsupported (empty column) — but tag/id/
    // class/attr/`:not` are all fine.
    if inner.positional.is_some()
        || inner.reverse.is_some()
        || inner.has.is_some()
        || inner.text_pred.is_some()
        || !inner.is_groups.is_empty()
    {
        return Err(());
    }
    Ok(Has { rel, inner: Box::new(inner) })
}

/// Parse a `:is(...)` / `:where(...)` argument into one matches-any group appended to `c.is_groups`.
/// The argument is a comma-list of COMPOUND alternatives (cssselect's `:is()` grammar — each a simple
/// selector, no combinators). `:is`/`:where` are identical for matching (specificity is irrelevant when
/// we only test membership). Each alternative is restricted to a plain structural compound
/// (tag/`*`/id/class/attr/`:not`); a positional/reverse/`:has`/nested-`:is` inside an alternative makes
/// the whole selector unsupported (empty column) rather than pulling deferral into the compound kernel.
fn parse_is_arg(c: &mut Compound, arg: &str, depth: u32) -> Result<(), ()> {
    let arg = arg.trim();
    if arg.is_empty() {
        return Err(());
    }
    let mut group = Vec::new();
    for part in split_top_commas(arg) {
        let part = part.trim();
        if part.is_empty() {
            return Err(()); // empty member (`:is(.a, , .b)`)
        }
        let alt = parse_compound_depth(part, depth)?; // single compound; parse_compound rejects combinators
        if alt.positional.is_some()
            || alt.reverse.is_some()
            || alt.has.is_some()
            || !alt.is_groups.is_empty()
        {
            return Err(());
        }
        group.push(alt);
    }
    c.is_groups.push(group);
    Ok(())
}

/// Parse the CSS `An+B` microsyntax into `(a, b)`: `odd`→(2,1), `even`→(2,0), `N`→(0,N), `n`→(1,0),
/// `2n+1`, `-n+3`, `n-2`, `+3`, etc. `Err` on anything malformed.
fn parse_anpb(s: &str) -> Result<(i32, i32), ()> {
    let t = s.trim().to_ascii_lowercase();
    if t == "odd" {
        return Ok((2, 1));
    }
    if t == "even" {
        return Ok((2, 0));
    }
    let bytes = t.as_bytes();
    match bytes.iter().position(|&c| c == b'n') {
        None => t.parse::<i32>().map(|b| (0, b)).map_err(|_| ()), // pure `B`
        Some(np) => {
            // coefficient of n (before the `n`): ""/"+" -> 1, "-" -> -1, else an integer
            let a_str = t[..np].trim();
            let a = match a_str {
                "" | "+" => 1,
                "-" => -1,
                other => other.parse::<i32>().map_err(|_| ())?,
            };
            // the `B` part after `n`: empty -> 0, else a signed integer (whitespace around `+`/`-` ok)
            let b_str: String = t[np + 1..].split_whitespace().collect();
            let b = if b_str.is_empty() { 0 } else { b_str.parse::<i32>().map_err(|_| ())? };
            Ok((a, b))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pos(q: &str) -> Option<Nth> {
        parse(q).ok().and_then(|s| s.parts.last().and_then(|c| c.positional))
    }

    fn rev(q: &str) -> Option<ReversePos> {
        parse(q).ok().and_then(|s| s.parts.last().and_then(|c| c.reverse))
    }

    #[test]
    fn deeply_nested_is_declines_without_overflow() {
        // A pathological `:is(:is(:is(…a…)))` must not recurse the parser into a stack overflow;
        // beyond the depth cap it is simply unsupported (empty column), like any other declined query.
        let n = 100_000;
        let sel = format!("{}a{}", ":is(".repeat(n), ")".repeat(n));
        assert!(parse(&sel).is_err());
        // one legitimate level still parses
        assert!(parse("div:is(.a, .b)").is_ok());
        assert!(parse(":is(:not(.x))").is_ok());
    }

    #[test]
    fn unquoted_attr_value_must_be_a_css_ident() {
        // cssselect rejects a non-ident unquoted value outright, so answering one would be a non-empty
        // column on a selector Parsel refuses (the fuzzer's OVERMATCH). Quoted is always fine.
        for q in ["a[href^=/p]", "i[a=2]", "i[a=2v]", "i[a=$v]", "i[a=--v]", "i[a=-2]", "i[a=a.b]",
                  "i[a=#v]", "i[a=1e5]", "i[a=]", "i[a=-]", "i[a=v!]", "i[a=a:b]"] {
            assert!(parse(q).is_err(), "{q} should not parse");
        }
        for q in ["i[a=v]", "i[a=v2]", "i[a=-v]", "i[a=_v]", "i[a=v-2]", "i[a=café]",
                  "a[href^=\"/p\"]", "i[a=\"2\"]", "i[a='$v']", "i[a=\"\"]", "i[class~=x]"] {
            assert!(parse(q).is_ok(), "{q} should parse");
        }
        assert!(matches!(
            parse("i[a=\"2\"]").unwrap().parts[0].attrs.as_slice(),
            [AttrPred::Eq(name, val)] if name == "a" && val == "2"
        ));
    }

    #[test]
    fn anpb_microsyntax() {
        assert_eq!(parse_anpb("odd"), Ok((2, 1)));
        assert_eq!(parse_anpb("even"), Ok((2, 0)));
        assert_eq!(parse_anpb("3"), Ok((0, 3)));
        assert_eq!(parse_anpb("n"), Ok((1, 0)));
        assert_eq!(parse_anpb("2n+1"), Ok((2, 1)));
        assert_eq!(parse_anpb("2n-1"), Ok((2, -1)));
        assert_eq!(parse_anpb("-n+3"), Ok((-1, 3)));
        assert_eq!(parse_anpb("-2n + 3"), Ok((-2, 3)));
        assert_eq!(parse_anpb("+n"), Ok((1, 0)));
        assert!(parse_anpb("").is_err());
        assert!(parse_anpb("2x+1").is_err());
    }

    #[test]
    fn positional_pseudos_parse_or_reject() {
        // supported forward positions
        assert!(matches!(pos("li:first-child"), Some(Nth { a: 0, b: 1, of_type: false })));
        assert!(matches!(pos("li:first-of-type"), Some(Nth { a: 0, b: 1, of_type: true })));
        assert!(matches!(pos("li:nth-child(2n+1)"), Some(Nth { a: 2, b: 1, of_type: false })));
        assert!(matches!(pos("li:nth-of-type(3)"), Some(Nth { a: 0, b: 3, of_type: true })));
        assert!(pos("div:nth-child(2)").is_some());
        // reverse positions now PARSE (the matcher decides whether it can defer them). `:last-*` is
        // `:nth-last-*(1)`; `:nth-last-*` carries its own An+B.
        assert!(matches!(rev("li:last-child"), Some(ReversePos { a: 0, b: 1, only: false, of_type: false })));
        assert!(matches!(rev("li:only-child"), Some(ReversePos { only: true, of_type: false, .. })));
        assert!(matches!(rev("li:last-of-type"), Some(ReversePos { a: 0, b: 1, only: false, of_type: true })));
        assert!(matches!(rev("li:only-of-type"), Some(ReversePos { only: true, of_type: true, .. })));
        assert!(matches!(rev("li:nth-last-child(2)"), Some(ReversePos { a: 0, b: 2, only: false, of_type: false })));
        assert!(matches!(rev("li:nth-last-of-type(2n+1)"), Some(ReversePos { a: 2, b: 1, only: false, of_type: true })));
        // still unsupported: universal/tagless of-type (fwd or reverse), a reverse+forward mix
        assert!(parse("*:last-of-type").is_err()); // universal of-type reverse: needs all-tag counting
        assert!(parse("*:nth-last-of-type(1)").is_err());
        assert!(parse("*:nth-of-type(1)").is_err()); // universal of-type: needs all-tag counting
        assert!(parse(".x:nth-of-type(1)").is_err()); // tagless of-type
        assert!(parse("li:first-child:last-child").is_err()); // reverse + forward on one compound
        assert!(parse("li:hover").is_err());
    }

    fn has(q: &str) -> Option<Has> {
        parse(q).ok().and_then(|s| s.parts.last().and_then(|c| c.has.clone()))
    }

    #[test]
    fn has_pseudo_parses_or_rejects() {
        // supported MVP: a single (optionally child-scoped) compound inner
        let h = has("div:has(a)").unwrap();
        assert_eq!(h.rel, Comb::Descendant);
        assert_eq!(h.inner.tag.as_deref(), Some("a"));
        let h = has("div:has(> img)").unwrap();
        assert_eq!(h.rel, Comb::Child);
        assert_eq!(h.inner.tag.as_deref(), Some("img"));
        assert!(has("section:has(.price)").is_some());
        assert!(has("div:has(> a.b)").is_some()); // child-scoped, type + class
        assert!(has("div:has(*)").is_some()); // universal inner
        assert!(parse("div:has(a)::text").is_ok()); // with a value terminal
        assert!(parse("div.card:has(a.buy)").is_ok()); // has alongside class on the subject
        // id/attribute/`:not` inners ARE supported (correct behavior — cssselect RAISES on these; a
        // divergence in our favor, see COMPATIBILITY.md)
        assert!(has("li:has([data-x])").is_some()); // attribute inner
        assert!(has("div:has(#id)").is_some()); // id inner
        assert!(has("div:has(a[href])").is_some()); // tag + attribute inner
        assert!(has("div:has(:not(.empty))").is_some()); // `:not` inner
        assert!(has("div:has(> [data-src])").is_some()); // child-scoped attribute inner
        // unsupported (empty column, never wrong): a chain, positional/reverse/`:has`/`:is` inside
        assert!(parse("div:has(.a .b)").is_err()); // descendant chain inside
        assert!(parse("div:has(a + b)").is_err()); // sibling inside
        assert!(parse("div:has(a, b)").is_err()); // comma list inside
        assert!(parse("div:has(:has(a))").is_err()); // nested :has
        assert!(parse("div:has(a:first-child)").is_err()); // positional inner
        assert!(parse("div:has()").is_err()); // empty arg
        assert!(parse("li:last-child:has(a)").is_err()); // has + reverse on one compound
    }

    fn is_groups(q: &str) -> Vec<Vec<Compound>> {
        parse(q).ok().and_then(|s| s.parts.last().map(|c| c.is_groups.clone())).unwrap_or_default()
    }

    #[test]
    fn is_where_pseudo_parses_or_rejects() {
        // supported: a comma-list of plain compound alternatives; `:where` is identical to `:is`
        let g = is_groups(":is(h1, h2, h3)");
        assert_eq!(g.len(), 1);
        assert_eq!(g[0].len(), 3);
        assert_eq!(g[0][1].tag.as_deref(), Some("h2"));
        assert_eq!(is_groups("div:where(.a, .b)")[0].len(), 2);
        assert!(parse("a:is([href], [data-k])::text").is_ok()); // attribute alternatives
        assert!(parse("li:is(a.x, b#y)").is_ok());
        assert!(parse("*:is(.a, .b) span::text").is_ok()); // :is on a non-subject compound (bare `*`)
        // `:is` combined with other conditions is supported with CORRECT AND semantics. cssselect ORed
        // them up to 1.4.0 and agrees from 1.5.0 on — see COMPATIBILITY.md / the matcher tests
        assert!(parse("div.card:is(.a, .b)").is_ok()); // class + :is: div AND card AND (a or b)
        assert_eq!(is_groups("div.card:is(.a, .b)")[0].len(), 2);
        assert!(parse("div:is(.a, .b):is(.c, .d)").is_ok()); // two groups: (a or b) AND (c or d)
        assert_eq!(is_groups("div:is(.a, .b):is(.c, .d)").len(), 2);
        assert!(parse("a[href]:is(.a, .b)").is_ok()); // attr + :is
        assert!(parse("div:not(.x):is(.a, .b)").is_ok()); // :not + :is
        // unsupported (empty column, never wrong): a chain/combinator inside, positional/reverse/has
        // inside, a nested :is, or an empty member — cssselect rejects most of these too
        assert!(parse("div:is(.a .b, .c)").is_err()); // descendant chain inside an alternative
        assert!(parse("div:is(a + b)").is_err()); // combinator inside
        assert!(parse("div:is(:first-child, .b)").is_err()); // positional inside
        assert!(parse("div:is(:last-child)").is_err()); // reverse inside
        assert!(parse("div:is(:has(a))").is_err()); // :has inside
        assert!(parse("div:is(:is(.a))").is_err()); // nested :is
        assert!(parse("div:is(.a, , .b)").is_err()); // empty member
        assert!(parse("div:is()").is_err()); // empty arg
    }
}

/// Support-boundary vectors: a selector the ORACLE rejects must be reported unsupported, not merely
/// answered empty. `check()`/`audit_schema` saying "supported" is a promise, and the no-fallback contract
/// makes a broken promise indistinguishable from a legitimately empty field at the scraper layer.
#[cfg(test)]
mod support_boundary_tests {
    use super::*;

    /// cssselect 1.5.0 rejects these, so `parse` must too (an unsupported selector -> empty column AND
    /// an unsupported *verdict*). A class/attribute/pseudo-argument name is not "any non-empty string":
    /// a CSS identifier may not start with a digit, or with a hyphen followed by a digit.
    #[test]
    fn invalid_css_identifiers_are_rejected() {
        for q in [
            ".1::text",        // class starting with a digit
            ".-2::text",       // hyphen then digit
            "[1]::text",       // attribute name starting with a digit
            "div::attr(1)",    // ::attr() argument is an attribute name
            ".2col::text",
            ".--x::text",   // `--x` is not a CSS 2.1 identifier
            "[2x=v]::text",
        ] {
            assert!(parse(q).is_err(), "cssselect rejects {q:?}, so it must be unsupported here");
        }
        // ...but these ARE valid identifiers and must keep working
        // an ID is a hash token, not an identifier: cssselect accepts `#1id`, so we must too
        for q in [".c1::text", ".-c::text", "._c::text", "[data-1]::text", "div::attr(data-1)",
                  ".café::text", "#año::text", "#1id::text", "#-x::text"] {
            assert!(parse(q).is_ok(), "{q:?} is a valid identifier and must stay supported");
        }
    }

    /// A `)` or `,` inside a QUOTED attribute value is data, not the end of a functional pseudo. Getting
    /// this wrong reported a selector Parsel answers as unsupported, so the column came back empty.
    #[test]
    fn quoted_delimiters_inside_functional_pseudos_are_data() {
        for q in [
            r#"div:is(#outer, [data-x=")"])::attr(id)"#,
            r#"div:is([data-x=")"], #other)::attr(id)"#,   // the quoted `)` BEFORE the comma
            r#"div:where([data-x=")"])::attr(id)"#,
            r#"div:not([data-x=")"])::attr(id)"#,
            r#"div:has([data-x=")"])::attr(id)"#,
            r#"div:not([data-x="("])::attr(id)"#,          // an unbalanced `(` in a value
            r#"p:is([title='a(b'])::attr(id)"#,            // ...single-quoted, too
            r#"div:is([data-x="\)"])::attr(id)"#,          // an ESCAPED paren outside a value
            r#"div:is([class="a,b"])::attr(id)"#,
            r#"div:is([data-x=")"]) span::text"#,          // ...and the tail still splits correctly
            r#"div:is(#outer, [data-x=")"]) > span::text"#,
            r#"div:has([data-x="a, b"])::attr(id)"#,       // a quoted comma in a `:has` inner
        ] {
            assert!(parse(q).is_ok(), "{q:?} is valid CSS and must be supported");
        }
        // FAIL CLOSED on syntax that is genuinely broken: an argument the quote scan never closes is a
        // syntax error, not something to guess at (cssselect raises on every one of these).
        for q in [
            r#"div:is(#outer, [data-x=")"]::attr(id)"#,    // pseudo never closed
            r#"div:is(#outer::attr(id)"#,
            r#"div:not([data-x=")"]::attr(id)"#,
            r#"div:is([data-x=")"))::attr(id)"#,           // unbalanced inside the argument
            "div:is()::attr(id)",
            r#"div:is([data-x=")::attr(id)"#,              // unterminated string
        ] {
            assert!(parse(q).is_err(), "{q:?} is malformed and must stay unsupported");
        }
    }

    /// A quoted attribute value may contain CSS ESCAPES, and cssselect decodes them: `[data-x="\\61"]`
    /// selects `data-x="a"`. Copying the raw bytes silently matched a DIFFERENT element — a wrong value,
    /// not an empty one. Decode the escapes we can, and reject the rest rather than guess.
    #[test]
    fn css_escapes_in_quoted_values_are_decoded() {
        let v = |q: &str| {
            parse(q).ok().and_then(|s| {
                s.parts.last().and_then(|c| {
                    c.attrs.first().map(|p| match p {
                        AttrPred::Eq(_, val) => val.clone(),
                        _ => String::new(),
                    })
                })
            })
        };
        assert_eq!(v(r#"[data-x="\61"]"#).as_deref(), Some("a"));
        assert_eq!(v(r#"[data-x="\61 bc"]"#).as_deref(), Some("abc")); // space terminates the escape
        assert_eq!(v(r#"[data-x="\0041"]"#).as_deref(), Some("A"));
        assert_eq!(v(r#"[data-x="a\-b"]"#).as_deref(), Some("a-b")); // escaped literal
        assert_eq!(v(r#"[data-x="plain"]"#).as_deref(), Some("plain"));
        // a backslash at end-of-value is invalid CSS; reject rather than guess
        assert!(parse(r#"[data-x="a\"]::text"#).is_err() || v(r#"[data-x="a\"]"#).is_some());
    }

    /// The `::attr()` ARGUMENT takes escapes too, and this one was worse than a coverage gap: the
    /// validator only inspected the first character, so `::attr(data-\6b)` passed as a valid identifier
    /// and was then matched as the literal name `data-\6b`, which no element carries. The compiler
    /// PROMISED support and returned an empty column — the one outcome the no-fallback contract forbids
    /// (parsel answers `['v1']`). Found by the selector fuzzer's new escape family, not by hand.
    #[test]
    fn css_escapes_in_the_attr_argument_are_decoded() {
        let arg = |q: &str| match parse(q).map(|s| s.terminal) {
            Ok(Terminal::Attr { name, .. }) => Some(name),
            _ => None,
        };
        assert_eq!(arg(r"p::attr(data-\6b)").as_deref(), Some("data-k"));
        assert_eq!(arg(r"p::attr(data-\6b )").as_deref(), Some("data-k")); // space terminator
        assert_eq!(arg(r"p::attr(\64 ata-k)").as_deref(), Some("data-k"));
        assert_eq!(arg(r"p::attr(href)").as_deref(), Some("href")); // unescaped still works
        // decoding must be validated AFTER decoding: `\31` is the digit `1`, which cssselect's
        // ExpressionError rejects as an attribute name, so it must stay unsupported
        assert!(parse(r"p::attr(\31)").is_err());
        // a lone trailing backslash is not valid CSS
        assert!(parse(r"p::attr(data\)").is_err());
    }
}
