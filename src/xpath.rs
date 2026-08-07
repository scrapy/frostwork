//! Downward-XPath → `Selector` compiler for the common subset real spiders use. It maps onto the
//! exact same matcher as CSS:
//!   `//`  -> descendant · `/` -> child · step `tag`/`*` -> compound · `[@a]`/`[@a="v"]` -> attr pred
//!   `[contains(@a,"v")]` -> `*=` · `[starts-with(@a,"v")]` -> `^=` · `... and ...` -> ANDed preds
//!   trailing `/text()` -> `::text` (self) · `//text()` -> descendant text · `/@name` -> `::attr(name)`
//!   `//@name` -> descendant `::attr` · no value terminal -> the element itself (outer HTML).
//!
//! A query can expand to **several** `Selector` members that share one output column (document-ordered,
//! node-deduped — same as a CSS comma group): a union `//a | //b` splits on `|`, and a predicate `or`
//! (`//a[@x or @y]`) is distributed into alternatives (`//a[@x] | //a[@y]`), so `or` needs no matcher
//! change. A sole `[N]` (`//li[2]`, `//ul/*[3]`) compiles to a forward nth position and a sole
//! `[last()]`/`[last()-k]` (also the `position()=` forms) to a REVERSE position (`tag[last()]` of-type,
//! `*[last()]` nth-last-child), both resolved against the parent's counts; a top-level
//! `normalize-space(path)` maps to a normalized-text terminal. [`compile_members`] returns all members;
//! [`compile`] is the single-member form (containers / sub-fields, which can't hold alternatives).
//! Three non-downward axes are supported by lowering onto existing machinery: `following-sibling::`
//! (after a single `/`) is the CSS general-sibling relation, so `//a/following-sibling::b` lowers to
//! `a ~ b`; and the upward `ancestor::`/`parent::` reframe as `:has`, so `//INNER/ancestor::E` →
//! `E:has(INNER)` and `//INNER/parent::E` → `E:has(> INNER)` (absolute two-step paths). All three ride
//! gate-proven code. A sole text-content predicate on the subject step — `[.="v"]`, `[contains(.,"v")]`,
//! `[text()="v"]`, `[contains(text(),"v")]` — is also supported, deferred to the element's own close.
//! Anything else outside the downward subset — other axes (`preceding[-sibling]::`, `following::`,
//! `ancestor-or-self::`, downward synonyms like `child::`), a range/combined positional predicate
//! (`[position()<n]`, `[N]`/`[last()]` plus a second predicate), a positional predicate on the sibling
//! axis (`following-sibling::td[1]`), a text-content predicate in any other shape, other functions
//! (`count()`, `string()`), a variable reference (`$var`), a comparison against anything but a quoted
//! string literal (`[@a=2]`, `[@a=b]`) — yields no members (the query is then unsupported: empty
//! column, no fallback).

use crate::selector::{
    AttrPred, Comb, Compound, Has, Nth, ReversePos, Selector, Terminal, TextAxis, TextOp, TextPred,
};

fn valid_name(s: &str) -> bool {
    // ASCII-only AND all-lowercase. The ASCII half is right for a TAG name — those come from the
    // tokenizer's ASCII name scan, so a non-ASCII one could never match — and over-broad for an
    // ATTRIBUTE name, which the matcher decodes before comparing (`Matcher::interesting_name`), so the
    // CSS spelling `[data-año]` matches and this refuses the same question. Refused, not silently
    // empty; widening it means lowering the `@` name scan below too, and proving the parity.
    // Lowercase is the subtler rule: libxml2 LOWERCASES every HTML
    // name in the tree, while XPath name-tests compare CASE-SENSITIVELY — so an XPath literal carrying
    // any uppercase letter (`//DIV`, `//rect/@ID`, `//svg/@viewBox`) matches nothing in lxml. Reject it
    // here so the query is unsupported (empty column, matching the oracle) instead of case-insensitively
    // over-matching via the matcher's `eq_ignore_ascii_case`. CSS is unaffected (cssselect lowercases,
    // so uppercase CSS names are meant to match). Non-ASCII attribute *values* are handled downstream.
    // A NAMESPACE PREFIX (`svg:rect`, `@x:y`) needs a prefix->URI binding, and there is no API to supply
    // one, so lxml raises "undefined namespace prefix" - `//div/@x:y` used to RETURN the attribute value
    // for an expression the oracle refuses to run. Reject the colon until bindings exist.
    // A leading digit / hyphen / dot is not a valid XML name start either (lxml: invalid expression).
    let ok_chars = |s: &str| {
        s.bytes().all(|b| matches!(b, b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.'))
    };
    let starts_ok = matches!(s.as_bytes().first(), Some(b'a'..=b'z' | b'_'));
    !s.is_empty() && starts_ok && ok_chars(s)
}

/// Cap on how many members one query may expand to (union parts × per-step `or` alternatives). Real
/// queries expand to a handful; this bounds a pathological `[@a or @b][@c or @d]…` cross-product. Over
/// the cap, the query is unsupported (empty) rather than allocating a blow-up.
const MAX_EXPAND: usize = 32;

/// Compile a downward-XPath query to the `Selector` **members** that share its output column, or an
/// empty vec if outside the supported subset. Members arise from unions (`//a | //b`) and from `or`
/// distributed into alternatives (`//a[@x or @y]` -> `//a[@x] | //a[@y]`); a plain query is one member.
/// The engine emits a multi-member column in document order, node-deduped — identical to a CSS comma
/// group (see `selector::parse_list`).
pub fn compile_members(q: &str) -> Vec<Selector> {
    compile_members_depth(q, 0)
}

/// `normalize-space(inner)` self-recurses on `inner`; nested `normalize-space` is unsupported, so
/// legitimate depth is 1. This cap stops a crafted `normalize-space(normalize-space(…))` from
/// overflowing the stack — it returns an empty (unsupported) column instead.
const MAX_XPATH_DEPTH: u32 = 32;

fn compile_members_depth(q: &str, depth: u32) -> Vec<Selector> {
    if depth > MAX_XPATH_DEPTH {
        return Vec::new();
    }
    let q = q.trim();
    if has_variable_ref(q) {
        return Vec::new(); // `$var` has no binding here — see `has_variable_ref`
    }
    // `normalize-space(inner)` is the whole query (XPath errors on unioning a string with a node-set),
    // so handle it before the union split. It wraps the inner path's terminal; the inner must be a
    // single node-set member (no union / `or` expansion). `normalize-space()` / `normalize-space(.)`
    // (context-node string value) is unsupported (rare).
    if let Some(inner) = q.strip_prefix("normalize-space(").and_then(|r| r.strip_suffix(')')) {
        let inner = inner.trim();
        if inner.is_empty() || inner == "." {
            return Vec::new();
        }
        let mut members = compile_members_depth(inner, depth + 1);
        if members.len() != 1 {
            return Vec::new(); // only a single node-set path can be wrapped
        }
        let sel = members.pop().unwrap();
        if matches!(sel.terminal, Terminal::NormalizeSpace(_)) {
            return Vec::new(); // nested normalize-space unsupported
        }
        return vec![Selector { terminal: Terminal::NormalizeSpace(Box::new(sel.terminal)), ..sel }];
    }
    let mut out = Vec::new();
    for part in split_top(q, "|") {
        // any unsupported union part makes the whole query unsupported (empty), matching lxml, which
        // would error/return nothing for a malformed union rather than a partial result.
        match compile_part(part.trim()) {
            Some(members) => out.extend(members),
            None => return Vec::new(),
        }
    }
    // Mirror `parse_list`: outer-HTML captures are deferred/reordered at finish, so mixing an
    // element (outer-HTML) member with value (`text()`/`@attr`) members would break document order.
    // All-outer or all-value is fine; mixed is unsupported.
    let outer = out.iter().filter(|s| matches!(s.terminal, Terminal::OuterHtml)).count();
    if outer != 0 && outer != out.len() {
        return Vec::new();
    }
    out
}

/// The single-member form for callers that can't hold alternatives (group containers / sub-fields):
/// `Some` only when the query compiles to exactly one member (no union, no `or` expansion).
pub fn compile(q: &str) -> Option<Selector> {
    let mut m = compile_members(q);
    if m.len() == 1 {
        m.pop()
    } else {
        None
    }
}

/// Split off the trailing value terminal (`/text()`, `//text()`, `/@name`, `//@name`) or, with none,
/// the element itself (outer HTML). `//@name` is the descendant-or-self attribute harvest.
fn split_terminal(q: &str) -> Option<(&str, Terminal)> {
    if let Some(p) = q.strip_suffix("//text()") {
        Some((p, Terminal::Text { subtree: true }))
    } else if let Some(p) = q.strip_suffix("/text()") {
        Some((p, Terminal::Text { subtree: false }))
    } else if let Some(idx) = q.rfind("//@") {
        let name = &q[idx + 3..];
        valid_name(name).then(|| (&q[..idx], Terminal::Attr { name: name.to_string(), subtree: true }))
    } else if let Some(idx) = q.rfind("/@") {
        let name = &q[idx + 2..];
        valid_name(name).then(|| (&q[..idx], Terminal::Attr { name: name.to_string(), subtree: false }))
    } else {
        Some((q, Terminal::OuterHtml))
    }
}

/// Compile ONE union part (no top-level `|`) to its members. A step's predicate `or` makes that step
/// yield several alternative compounds; the members are the cross-product across steps.
fn compile_part(q: &str) -> Option<Vec<Selector>> {
    let (path, terminal) = split_terminal(q)?;
    let steps = tokenize_path(path)?;
    if steps.is_empty() {
        return None;
    }
    // Upward axes (`ancestor::`/`parent::`) reframe the axis node-test as the subject and the step
    // before it as a `:has` predicate — see `build_upward`. Absolute paths only (a relative `.`-context
    // could reach ABOVE the scope floor). Anything with such an axis that doesn't fit -> unsupported.
    if steps.iter().any(|(_, s)| s.contains("ancestor::") || s.contains("parent::")) {
        if path.trim_start().starts_with('.') {
            return None;
        }
        return build_upward(&steps, terminal);
    }
    let mut combs = Vec::new();
    let mut step_alts: Vec<Vec<Compound>> = Vec::with_capacity(steps.len());
    for (i, (comb, step)) in steps.iter().enumerate() {
        let (axis_comb, alts) = parse_step(step)?; // `or` -> several compounds; node-test tag is shared
        if let Some(axis) = axis_comb {
            // An axis step (`following-sibling::` -> `~`) needs a preceding step to be a sibling of, so
            // it can't be the anchor; and it must follow a single-slash `/` (child separator). A `//`
            // before it (`//a//following-sibling::b`) means descendant-or-self THEN following sibling —
            // far broader than `a ~ b` — so reject it rather than mis-lower (empty column, never wrong).
            if i == 0 || *comb != Comb::Child {
                return None;
            }
            combs.push(axis);
        } else if i > 0 {
            combs.push(*comb);
        }
        step_alts.push(alts);
    }
    // The first path step's combinator is the ANCHOR, and the matcher is depth-agnostic on a segment's
    // first compound (it enforces only `depth >= floor`) — so a single-slash *child* anchor can't be
    // enforced and would silently over-match like its `//` descendant form. Two cases, both unsupported
    // (empty column, never wrong) unless the anchor is provably safe:
    //
    //   * Absolute `/step` — the first step is a child of the document node, i.e. the ROOT element.
    //     libxml2 always synthesizes `<html>` as the root, so `/X` matches nothing unless X is `html`;
    //     otherwise it would behave like `//X` (`/tr` ~ `//tr`, found by tools/sel_fuzz.py). Only the
    //     `/html…` form is safe (the matcher matching `html` at any depth is fine — there is one).
    //   * Relative `./step` — a direct child of the CONTEXT node (`.`). There is no fixed root tag to
    //     lean on, and the context differs between flat (the document node, whose only child is `html`)
    //     and grouped (the container element) use, so it cannot be anchored here. It would over-match
    //     like `.//step` — e.g. flat `./h3` returns descendants instead of `[]`, grouped `./h3` pulls in
    //     nested `h3` (both found in review). Reject until the first-step anchor is modeled in the
    //     selector. `.//step` (descendant of context) is anchor-free and stays supported and correct.
    // The node-test tag is identical across a step's `or` alternatives, so alt `[0]` is representative.
    let relative = path.trim_start().starts_with('.');
    if steps[0].0 == Comb::Child {
        if relative {
            return None;
        }
        if step_alts[0][0].tag.as_deref() != Some("html") {
            return None;
        }
    }
    // cross-product the per-step alternatives into full parts-lists (bounded by MAX_EXPAND)
    let mut lists: Vec<Vec<Compound>> = vec![Vec::new()];
    for alts in &step_alts {
        let mut next = Vec::with_capacity(lists.len() * alts.len());
        for prefix in &lists {
            for alt in alts {
                let mut p = prefix.clone();
                p.push(alt.clone());
                next.push(p);
            }
        }
        lists = next;
        if lists.len() > MAX_EXPAND {
            return None;
        }
    }
    // A `.`-relative path (`.//x`) is anchored at the context node and its descendant axis EXCLUDES
    // that node; absolute paths are descendant-or-self of the document root (which includes `<html>`).
    // `strict_desc` carries this to the matcher (see `Selector::strict_desc`); `.//` is the only
    // relative form that reaches here (relative `./` child anchors are rejected above).
    Some(
        lists
            .into_iter()
            .map(|parts| Selector { parts, combs: combs.clone(), terminal: terminal.clone(), strict_desc: relative })
            .collect(),
    )
}

/// `//INNER/ancestor::E` → `E:has(INNER)` and `//INNER/parent::E` → `E:has(> INNER)`. An upward axis
/// makes the axis node-test `E` the subject and the step before it a `:has` predicate: lxml's node set
/// (every `E` that is an ancestor / the parent of some `INNER`, document-ordered and deduped) is exactly
/// what `E:has(INNER)` / `E:has(> INNER)` matches, so it rides the (gate-proven) `:has` machinery.
/// MVP: an ABSOLUTE two-step path, `INNER` descendant-anchored (`//`) and `E` after a single `/`; each a
/// lone compound (tag/`*` + attribute predicates) with no position / `or` / nested axis. Anything else
/// → `None` (unsupported: empty column, never a wrong value).
fn build_upward(steps: &[(Comb, String)], terminal: Terminal) -> Option<Vec<Selector>> {
    if steps.len() != 2 || steps[0].0 != Comb::Descendant || steps[1].0 != Comb::Child {
        return None; // only `//INNER/axis::E` (INNER `//`-anchored, single `/` before the axis)
    }
    let s = &steps[1].1;
    let p = s.find("::")?;
    let rel = match &s[..p] {
        "ancestor" => Comb::Descendant, // E is an ANCESTOR of INNER -> E has INNER as a descendant
        "parent" => Comb::Child,        // E is the PARENT of INNER   -> E has INNER as a direct child
        _ => return None,               // some other axis on the second step
    };
    // A lone plain compound on each side: no axis, no `or` (single alt), no position/reverse/has.
    let one_plain = |raw: &str| -> Option<Compound> {
        let (axis, alts) = parse_step(raw)?;
        if axis.is_some() || alts.len() != 1 {
            return None;
        }
        let c = alts.into_iter().next().unwrap();
        (c.positional.is_none() && c.reverse.is_none() && c.has.is_none() && c.text_pred.is_none())
            .then_some(c)
    };
    let inner = one_plain(&steps[0].1)?;
    let mut subject = one_plain(&s[p + 2..])?;
    subject.has = Some(Has { rel, inner: Box::new(inner) });
    Some(vec![Selector { parts: vec![subject], combs: Vec::new(), terminal, strict_desc: false }])
}

/// Split an absolute/relative path into `(combinator, step)` pairs. The first pair's combinator is
/// the anchor (`//` or `/` from root / context) and is ignored by `compile`.
fn tokenize_path(path: &str) -> Option<Vec<(Comb, String)>> {
    let b = path.as_bytes();
    let n = b.len();
    let mut i = 0;
    if i < n && b[i] == b'.' {
        i += 1; // leading context node (`.//`, `./`)
    }
    if i >= n || b[i] != b'/' {
        return None; // must be an absolute or `.`-rooted path (relative bare steps unsupported)
    }
    let mut out = Vec::new();
    while i < n {
        let comb = if b[i] == b'/' {
            if i + 1 < n && b[i + 1] == b'/' {
                i += 2;
                Comb::Descendant
            } else {
                i += 1;
                Comb::Child
            }
        } else {
            return None;
        };
        let start = i;
        let mut depth = 0i32;
        while i < n {
            match b[i] {
                b'[' | b'(' => depth += 1,
                b']' | b')' => depth -= 1,
                b'/' if depth == 0 => break,
                _ => {}
            }
            i += 1;
        }
        let step = path[start..i].trim();
        if step.is_empty() {
            return None;
        }
        out.push((comb, step.to_string()));
    }
    Some(out)
}

/// Parse one path step into `(axis-combinator override, alternative compounds)`. The node-test
/// (`tag`/`*`) is fixed; a predicate `or` (`[@x or @y]`) splits the step into several compounds (one
/// per disjunct), cross-multiplied across predicates; `and` within a disjunct stays one ANDed compound.
///
/// An explicit axis (`name::`) is supported ONLY for `following-sibling::`, which lowers to the CSS
/// general-sibling combinator `~` (the two are the same tree relation) and overrides the step's
/// separator-derived combinator — [`compile_part`] applies the override and enforces that it follows a
/// single `/` (a child separator, not `//`) and is never the anchor step. Every other axis
/// (`ancestor::`, `parent::`, `preceding[-sibling]::`, `following::`, downward synonyms like `child::`)
/// stays unsupported: `None`, so the query yields an empty column. A positional/reverse predicate on a
/// sibling-axis step (`following-sibling::td[1]`) is also rejected — "first following sibling of type"
/// has no faithful `~` lowering — so it stays unsupported rather than over-matching.
fn parse_step(step: &str) -> Option<(Option<Comb>, Vec<Compound>)> {
    let (axis_comb, step) = match step.find("::") {
        Some(p) => match &step[..p] {
            "following-sibling" => (Some(Comb::General), &step[p + 2..]),
            _ => return None, // any other axis specifier -> unsupported
        },
        None => (None, step),
    };
    let b = step.as_bytes();
    let n = b.len();
    let mut base = Compound::default();
    let mut i = 0;
    // node test: a tag name or `*`
    if i < n && b[i] == b'*' {
        base.tag = Some("*".to_string());
        i += 1;
    } else if i < n && b[i].is_ascii_alphabetic() {
        let s = i;
        while i < n && (b[i].is_ascii_alphanumeric() || matches!(b[i], b'-' | b'_' | b':')) {
            i += 1;
        }
        let name = &step[s..i];
        if !valid_name(name) {
            return None; // uppercase tag name-test never matches libxml2's lowercased tree
        }
        base.tag = Some(name.to_string());
    } else {
        return None; // node() / comment() / other node tests unsupported
    }
    // collect the `[...]` predicate bodies
    let mut preds: Vec<&str> = Vec::new();
    while i < n {
        if b[i] != b'[' {
            return None;
        }
        let start = i + 1;
        let mut depth = 1i32;
        i += 1;
        while i < n && depth > 0 {
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
        if i >= n || b[i] != b']' {
            return None;
        }
        preds.push(step[start..i].trim());
        i += 1;
    }
    // A positional/reverse predicate on a SIBLING-axis step (`following-sibling::td[1]`,
    // `following-sibling::*[last()]`) counts among the *following siblings*, not among all element
    // children — the `~` lowering can't express that, so it stays unsupported (the attribute path below
    // rejects `[1]`/`[last()]`, giving an empty column). Skip the position fast-paths for axis steps.
    if axis_comb.is_none() {
        // A SOLE integer predicate `[N]` is a forward position: `tag[N]` counts among same-tag siblings
        // (of-type), `*[N]` among all element children (nth-child). Position COMBINED with any other
        // predicate (`p[@x][1]`) is position among the *filtered* set — not supported (reject, never wrong).
        if preds.len() == 1 && !preds[0].is_empty() && preds[0].bytes().all(|c| c.is_ascii_digit()) {
            let nn: i32 = preds[0].parse().ok()?;
            base.positional = Some(Nth { a: 0, b: nn, of_type: base.tag.as_deref() != Some("*") });
            return Some((axis_comb, vec![base]));
        }
        // A sole REVERSE position `[last()]` / `[last()-k]` (or the `position()=` forms): `tag[last()]`
        // is of-type from the end, `*[last()]` is nth-last-child. Resolved at the parent's close, like
        // the CSS `:last-*`. Combined with another predicate falls through to the attribute path (reject).
        if preds.len() == 1 {
            if let Some(b) = last_from_end(preds[0]) {
                base.reverse =
                    Some(ReversePos { a: 0, b, only: false, of_type: base.tag.as_deref() != Some("*") });
                return Some((axis_comb, vec![base]));
            }
        }
        // A sole text-content predicate — `[.="v"]`, `[contains(., "v")]`, `[text()="v"]`,
        // `[contains(text(),"v")]`. Resolved at the element's own close (see `matcher::text_pred`);
        // combined with another predicate (`p[@x][text()="y"]`) falls through and is rejected below.
        if preds.len() == 1 {
            if let Some(tp) = text_predicate(preds[0]) {
                base.text_pred = Some(tp);
                return Some((axis_comb, vec![base]));
            }
        }
    }
    // otherwise: attribute-test predicates; each is a disjunction (OR of AND-groups) -> cross-multiply
    let mut variants = vec![base];
    for pred in preds {
        let disjuncts = parse_predicate_alts(pred)?;
        let mut next = Vec::with_capacity(variants.len() * disjuncts.len());
        for v in &variants {
            for group in &disjuncts {
                let mut c = v.clone();
                c.attrs.extend(group.iter().cloned());
                next.push(c);
            }
        }
        variants = next;
        if variants.len() > MAX_EXPAND {
            return None;
        }
    }
    Some((axis_comb, variants))
}

/// Split `s` on top-level occurrences of `needle` (a whole-word separator like ` and `/` or `, or the
/// union `|`), ignoring occurrences inside `()`/`[]` or a quoted string.
fn split_top<'a>(s: &'a str, needle: &str) -> Vec<&'a str> {
    let b = s.as_bytes();
    let head = needle.as_bytes()[0];
    let mut parts = Vec::new();
    let mut start = 0;
    let mut depth = 0i32;
    let mut quote = 0u8;
    let mut i = 0;
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
            b'(' | b'[' => depth += 1,
            b')' | b']' => depth -= 1,
            _ => {}
        }
        if depth == 0 && c == head && s[i..].starts_with(needle) {
            parts.push(&s[start..i]);
            i += needle.len();
            start = i;
            continue;
        }
        i += 1;
    }
    parts.push(&s[start..]);
    parts
}

/// Does the query carry an XPath **variable reference** (`$pid`) outside a string literal? Parsel binds
/// variables at call time (`sel.xpath('//*[@id=$pid]', pid=…)`); Frostwork's API takes no bindings, so
/// there is nothing to substitute. Without this check `[@id=$pid]` parsed as a comparison against the
/// literal text `"$pid"` — reporting the query SUPPORTED and then matching an element whose id really is
/// `$pid` (a wrong value), or silently nothing. Reject the whole query instead: unsupported, empty
/// column, and the audit says so. Any `$` outside a literal is a variable reference or a syntax error in
/// XPath, so a bare `$` is rejected too; `$` *inside* a literal (`[contains(@id,"$p")]`, prices) is fine.
pub(crate) fn has_variable_ref(q: &str) -> bool {
    let b = q.as_bytes();
    let mut quote = 0u8;
    for &c in b {
        if quote != 0 {
            if c == quote {
                quote = 0;
            }
            continue;
        }
        match c {
            b'"' | b'\'' => quote = c,
            b'$' => return true,
            _ => {}
        }
    }
    false
}

/// Parse a reverse-position predicate to its 1-based FROM-END position: `last()` -> 1, `last()-k` ->
/// k+1, also the `position()=last()[-k]` forms. `None` if it isn't a bare last-relative position (any
/// other function/comparison stays unsupported). Whitespace is ignored.
fn last_from_end(pred: &str) -> Option<i32> {
    let compact: String = pred.chars().filter(|c| !c.is_whitespace()).collect();
    let body = compact.strip_prefix("position()=").unwrap_or(&compact);
    if body == "last()" {
        Some(1)
    } else if let Some(k) = body.strip_prefix("last()-") {
        k.parse::<i32>().ok().filter(|&k| k >= 0).map(|k| k + 1)
    } else {
        None
    }
}

/// Parse a text-content predicate: `.="v"`, `contains(., "v")`, `text()="v"`, `contains(text(),"v")`.
/// `None` if it isn't exactly one of those (any other function/operator/combination stays unsupported,
/// including `or`/`and` joins, `!=`, and `normalize-space(…)`). The left operand fixes the axis
/// (`.` → whole string-value, `text()` → direct child text nodes); the right must be a single quoted
/// literal (so an `or`-joined body — whose right side isn't a clean literal — is rejected).
fn text_predicate(pred: &str) -> Option<TextPred> {
    let p = pred.trim();
    // `contains(<axis>, "needle")`
    if let Some(inner) = p.strip_prefix("contains(").and_then(|r| r.strip_suffix(')')) {
        let comma = inner.find(',')?;
        let axis = text_axis(inner[..comma].trim())?;
        let needle = single_literal(inner[comma + 1..].trim())?;
        return Some(TextPred { axis, op: TextOp::Contains, needle });
    }
    // `<axis> = "needle"` — the first `=` that is not part of `!=`/`<=`/`>=`
    let bytes = p.as_bytes();
    let eq = (0..bytes.len())
        .find(|&i| bytes[i] == b'=' && (i == 0 || !matches!(bytes[i - 1], b'!' | b'<' | b'>')))?;
    let axis = text_axis(p[..eq].trim())?;
    let needle = single_literal(p[eq + 1..].trim())?;
    Some(TextPred { axis, op: TextOp::Eq, needle })
}

/// The left operand of a text predicate: `.` (string-value) or `text()` (direct text nodes). Anything
/// else (`normalize-space(.)`, `@a`, `string(.)`, `.[1]`) is not a supported text axis.
fn text_axis(s: &str) -> Option<TextAxis> {
    match s {
        "." => Some(TextAxis::StringValue),
        "text()" => Some(TextAxis::DirectText),
        _ => None,
    }
}

/// The compared operand must be a SINGLE quoted string literal (`"v"` / `'v'`) — start and end with the
/// same quote, nothing after. Rejects an `or`-joined or otherwise compound right side (which would not
/// be a lone literal), so those predicates stay unsupported rather than parsing a bogus needle. Used for
/// both text predicates and attribute tests: an unquoted operand (a number, a bare name, a variable) has
/// non-byte-compare XPath semantics, so it must not be taken for a literal.
fn single_literal(s: &str) -> Option<String> {
    let b = s.as_bytes();
    if b.len() >= 2 && (b[0] == b'"' || b[0] == b'\'') && b[b.len() - 1] == b[0] {
        let inner = &s[1..s.len() - 1];
        if !inner.contains(b[0] as char) {
            return Some(inner.to_string());
        }
    }
    None
}

/// A predicate body as OR-of-AND: split on top-level ` or `, then each disjunct on ` and `, each conjunct
/// an attribute test. Returns one `Vec<AttrPred>` (ANDed) per disjunct; the step becomes one compound
/// alternative per disjunct.
fn parse_predicate_alts(pred: &str) -> Option<Vec<Vec<AttrPred>>> {
    let mut disjuncts = Vec::new();
    for disj in split_top(pred, " or ") {
        let mut preds = Vec::new();
        for conj in split_top(disj, " and ") {
            parse_one_attr(conj.trim(), &mut preds)?;
        }
        disjuncts.push(preds);
    }
    Some(disjuncts)
}

/// Parse one attribute test (`@a`, `@a="v"`, `contains(@a,"v")`, `starts-with(@a,"v")`) into an
/// `AttrPred`. Anything else (positional, `text()`, function) is unsupported. The compared value must be
/// a QUOTED string literal: XPath gives `[@a=2]` numeric semantics (`number(@a)=2`, so `a="02"` matches)
/// and `[@a=b]` node-set semantics (compare against child `<b>` elements' string-value), neither of which
/// a byte compare against the raw text `2`/`b` reproduces — both used to yield wrong values (see
/// `single_literal`), so an unquoted operand is now unsupported (empty column).
fn parse_one_attr(t: &str, out: &mut Vec<AttrPred>) -> Option<()> {
    if let Some(inner) = t.strip_prefix("contains(").and_then(|r| r.strip_suffix(")")) {
        let (name, val) = fn_args(inner)?;
        out.push(AttrPred::Substr(name, val));
    } else if let Some(inner) = t.strip_prefix("starts-with(").and_then(|r| r.strip_suffix(")")) {
        let (name, val) = fn_args(inner)?;
        out.push(AttrPred::Prefix(name, val));
    } else {
        // not `@name…` -> positional / text() / function predicate -> unsupported
        let rest = t.strip_prefix('@')?;
        if let Some(eq) = rest.find('=') {
            let name = rest[..eq].trim();
            if !valid_name(name) {
                return None;
            }
            let val = single_literal(rest[eq + 1..].trim())?;
            out.push(AttrPred::Eq(name.to_string(), val));
        } else {
            let name = rest.trim();
            if !valid_name(name) {
                return None;
            }
            out.push(AttrPred::Exists(name.to_string()));
        }
    }
    Some(())
}

/// Parse `@name , "value"` (a contains/starts-with argument list). The value must be a quoted literal —
/// `contains(@a,2)` / `contains(@a,$v)` is a numeric/variable operand, not a byte compare.
fn fn_args(inner: &str) -> Option<(String, String)> {
    let comma = inner.find(',')?;
    let name = inner[..comma].trim().strip_prefix('@')?.trim();
    if !valid_name(name) {
        return None;
    }
    let val = single_literal(inner[comma + 1..].trim())?;
    Some((name.to_string(), val))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn ok(q: &str) -> bool {
        compile(q).is_some() // single member
    }
    fn members(q: &str) -> usize {
        compile_members(q).len()
    }
    #[test]
    fn supported_forms_compile() {
        assert!(ok("//div"));
        assert!(ok("//a/@href"));
        assert!(ok("//div/text()"));
        assert!(ok("//div//text()"));
        assert!(ok("/html/body/div"));
        assert!(ok("//div[@class=\"x\"]//a/@href"));
        assert!(ok("//a[contains(@class,\"btn\")]"));
        assert!(ok("//a[starts-with(@href,\"/p\")]/@href"));
        assert!(ok("//div[@id=\"x\" and @data-k=\"v\"]/text()"));
        assert!(ok(".//span/text()"));
    }
    #[test]
    fn unions_or_and_descendant_attr_supported() {
        // union -> one member per part; `or` -> one member per disjunct (distributed); both share the
        // output column. `//X//@a` is a descendant-or-self attribute terminal (subtree).
        assert_eq!(members("//a | //b"), 2);
        assert_eq!(members("//a/text() | //b/text() | //c/text()"), 3);
        assert_eq!(members("//a[@x or @y]/text()"), 2);
        assert_eq!(members("//a[@x or @y or @z]"), 3);
        assert_eq!(members("//a[@x]//b[@p or @q]"), 2); // per-step or, cross-product
        assert_eq!(members("//div[@a=\"1\" and @b=\"2\" or @c=\"3\"]"), 2); // (a and b) or c
        assert!(ok("//div//@href")); // descendant attribute (single member)
        assert!(matches!(compile("//div//@href").unwrap().terminal, Terminal::Attr { subtree: true, .. }));
        // a bad member makes the whole union unsupported (empty), matching lxml
        assert_eq!(members("//a | //b[position()<2]"), 0);
    }
    #[test]
    fn positional_predicate_compiles() {
        // sole `[N]`: `tag[N]` -> of-type, `*[N]` -> nth-child. Combined with any other predicate, or a
        // range/function like `[position()<n]`, is unsupported (empty).
        assert!(matches!(compile("//li[2]").unwrap().parts[0].positional,
            Some(Nth { a: 0, b: 2, of_type: true })));
        assert!(matches!(compile("//*[1]").unwrap().parts[0].positional,
            Some(Nth { a: 0, b: 1, of_type: false })));
        assert!(ok("//ul/li[3]/text()"));
        assert!(!ok("//p[@class=\"x\"][1]")); // filtered position (position among the filtered set)
        assert!(!ok("//li[position()<2]"));
        // reverse `[last()]` / `[last()-k]` (and `position()=` forms) -> ReversePos: `tag[last()]` is
        // of-type, `*[last()]` is nth-last-child.
        assert!(matches!(compile("//li[last()]").unwrap().parts[0].reverse,
            Some(ReversePos { a: 0, b: 1, of_type: true, only: false })));
        assert!(matches!(compile("//ul/*[last()]").unwrap().parts.last().unwrap().reverse,
            Some(ReversePos { a: 0, b: 1, of_type: false, only: false })));
        assert!(matches!(compile("//li[last()-1]").unwrap().parts[0].reverse,
            Some(ReversePos { a: 0, b: 2, of_type: true, only: false })));
        assert!(matches!(compile("//li[position()=last()]").unwrap().parts[0].reverse,
            Some(ReversePos { a: 0, b: 1, of_type: true, only: false })));
        assert!(!ok("//li[@x][last()]")); // combined with another predicate: unsupported
    }
    #[test]
    fn normalize_space_wraps_inner_terminal() {
        // `normalize-space(inner)` -> one member whose terminal wraps the inner's (element string-value,
        // first text node, or first attr value).
        assert!(matches!(compile("normalize-space(//h1)").unwrap().terminal,
            Terminal::NormalizeSpace(b) if matches!(*b, Terminal::OuterHtml)));
        assert!(matches!(compile("normalize-space(//h1/text())").unwrap().terminal,
            Terminal::NormalizeSpace(b) if matches!(*b, Terminal::Text { subtree: false })));
        assert!(matches!(compile("normalize-space(.//p//text())").unwrap().terminal,
            Terminal::NormalizeSpace(b) if matches!(*b, Terminal::Text { subtree: true })));
        assert!(matches!(compile("normalize-space(//a/@href)").unwrap().terminal,
            Terminal::NormalizeSpace(b) if matches!(*b, Terminal::Attr { subtree: false, .. })));
        // context-node / empty / nested / non-single-member inner -> unsupported
        assert_eq!(members("normalize-space(.)"), 0);
        assert_eq!(members("normalize-space()"), 0);
        assert_eq!(members("normalize-space(//a | //b)"), 0);
        assert_eq!(members("normalize-space(normalize-space(//a))"), 0);
        // a pathological `normalize-space(normalize-space(…))` tower declines without recursing the
        // compiler into a stack overflow
        let n = 100_000;
        let deep = format!("{}//a{}", "normalize-space(".repeat(n), ")".repeat(n));
        assert_eq!(members(&deep), 0);
        // pathological cross-product past the cap is unsupported, not a blow-up
        assert_eq!(members("//a[@a or @b][@c or @d][@e or @f][@g or @h][@i or @j][@k or @l]"), 0);
    }
    #[test]
    fn unsupported_forms_reject() {
        assert!(!ok("//a[position()<2]")); // range position (only sole `[N]`/`[last()]` are positions)
        assert_eq!(members("//a | //b[position()<2]"), 0); // union with an unsupported part
        assert!(!ok("//ancestor::div")); // axis (has `::`, no node test after `/`)
        assert!(!ok("count(//a)")); // function
        assert_eq!(members("//@href"), 0); // bare descendant attr: no subject element
    }

    #[test]
    fn variable_refs_and_unquoted_operands_reject() {
        // A variable reference has no binding in this API (parsel passes them at call time), and an
        // unquoted operand is a numeric / node-set comparison in XPath — neither is a byte compare
        // against the raw text, so both are unsupported (empty column) rather than wrong values.
        assert_eq!(members("//*[@id=$pid]"), 0); // reported: audited as supported, matched `id="$pid"`
        assert_eq!(members("//div[@id=$pid]/text()"), 0);
        assert_eq!(members("//span[contains(@x,$v)]/text()"), 0);
        assert_eq!(members("//a[starts-with(@href,$p)]/@href"), 0);
        assert_eq!(members("//div[.=$v]"), 0);
        assert_eq!(members("//li[$n]"), 0);
        assert_eq!(members("//a | //b[@id=$pid]"), 0); // one bad union member sinks the query
        assert_eq!(members("normalize-space(//a[@id=$pid])"), 0);
        assert_eq!(members("//div[@id=foo]/text()"), 0); // node-set compare (child <foo>), not "foo"
        assert_eq!(members("//span[@x=2]/text()"), 0); // numeric: `x="02"` matches in lxml
        assert_eq!(members("//span[contains(@x,2)]"), 0);
        // `$` inside a string literal is just data (prices, `$`-prefixed ids) — still supported.
        assert!(ok("//div[contains(@id,\"$p\")]/text()"));
        assert!(ok("//div[@id=\"$pid\"]"));
        assert!(ok("//span[.=\"$4.99\"]"));
        assert!(ok("//div[@id='$x']//a/@href"));
    }

    #[test]
    fn following_sibling_lowers_to_general_combinator() {
        // `following-sibling::` is the same tree relation as CSS `~`, so `//a/following-sibling::b`
        // lowers to `a ~ b` (a General combinator between the two compounds) and rides the existing
        // sibling machinery. The value terminal rides along (`/text()`, `/@href`).
        let s = compile("//a/following-sibling::b").unwrap();
        assert_eq!(s.combs, vec![Comb::General]);
        assert_eq!(s.parts.len(), 2);
        assert_eq!(s.parts[0].tag.as_deref(), Some("a"));
        assert_eq!(s.parts[1].tag.as_deref(), Some("b"));
        assert!(ok("//a/following-sibling::b/text()"));
        assert!(ok("//dt/following-sibling::dd/text()")); // the label -> value pattern
        assert!(ok("//a/following-sibling::*")); // universal following sibling
        assert!(ok("//a/following-sibling::b/@href")); // attr terminal
        assert!(ok("//a[@class=\"x\"]/following-sibling::b[@id=\"y\"]")); // predicates both sides
        // a following-sibling step chains further down (`a ~ b > span` / `a ~ b span`)
        let chained = compile("//a/following-sibling::b/span").unwrap();
        assert_eq!(chained.combs, vec![Comb::General, Comb::Child]);
        assert!(ok(".//a/following-sibling::b/text()")); // relative anchor, then sibling
    }

    #[test]
    fn text_content_predicates_parse_or_reject() {
        use crate::selector::{TextAxis, TextOp};
        let tp = |q: &str| compile(q).and_then(|s| s.parts.last().and_then(|c| c.text_pred.clone()));
        // the four supported forms
        let p = tp("//h2[.=\"Price\"]").unwrap();
        assert_eq!((p.axis, p.op, p.needle.as_str()), (TextAxis::StringValue, TextOp::Eq, "Price"));
        let p = tp("//h2[contains(., \"Pri\")]").unwrap();
        assert_eq!((p.axis, p.op, p.needle.as_str()), (TextAxis::StringValue, TextOp::Contains, "Pri"));
        let p = tp("//h2[text()=\"Price\"]").unwrap();
        assert_eq!((p.axis, p.op), (TextAxis::DirectText, TextOp::Eq));
        let p = tp("//h2[contains(text(),\"Pri\")]").unwrap();
        assert_eq!((p.axis, p.op), (TextAxis::DirectText, TextOp::Contains));
        assert!(ok("//h2[contains(text(),\"P\")]/text()")); // value terminals ride along
        assert!(ok("//a[.=\"buy\"]/@href"));
        assert!(ok("//div[@class=\"x\"]//h2[contains(.,\"P\")]")); // descendant chain then subject pred
        // unsupported: combined with another predicate, non-literal RHS, `or`-joined, other funcs/ops
        assert!(!ok("//p[@x][text()=\"y\"]")); // text-pred combined with another predicate
        assert!(!ok("//p[text()!=\"y\"]")); // != operator
        assert!(!ok("//p[text()=@x]")); // non-literal RHS
        assert!(!ok("//p[contains(text(),\"a\") or contains(text(),\"b\")]")); // or-joined
        assert!(!ok("//p[normalize-space(.)=\"y\"]")); // normalize-space inside predicate
        assert!(!ok("//p[string(.)=\"y\"]")); // other function
        // `//div[text()="x"]//a` PARSES (the text-pred sits on the non-subject `div`); it's the matcher
        // that routes a non-subject text-pred to an empty column (deferrable_text_pred is subject-only).
        assert!(compile("//div[text()=\"x\"]//a").is_some());
    }

    #[test]
    fn upward_axes_reframe_as_has() {
        // `//INNER/ancestor::E` -> `E:has(INNER)`; `//INNER/parent::E` -> `E:has(> INNER)`.
        let s = compile("//span/ancestor::div").unwrap();
        assert_eq!(s.parts.len(), 1);
        assert_eq!(s.parts[0].tag.as_deref(), Some("div"));
        let h = s.parts[0].has.as_ref().unwrap();
        assert_eq!(h.rel, Comb::Descendant);
        assert_eq!(h.inner.tag.as_deref(), Some("span"));
        let s = compile("//a/parent::li").unwrap();
        assert_eq!(s.parts[0].tag.as_deref(), Some("li"));
        assert_eq!(s.parts[0].has.as_ref().unwrap().rel, Comb::Child);
        assert!(ok("//span/ancestor::div/@id")); // attr terminal on the subject
        assert!(ok("//span/ancestor::div/text()"));
        assert!(ok("//a[@class=\"buy\"]/ancestor::div")); // attribute predicate on INNER
        assert!(ok("//span/ancestor::div[@id=\"main\"]")); // attribute predicate on E (subject)
        // unsupported upward shapes -> empty column (never wrong)
        assert!(!ok("//ancestor::div")); // one step: no INNER
        assert!(!ok("//div//span/ancestor::section")); // >2 steps (chain INNER)
        assert!(!ok("//span//ancestor::div")); // `//` before the axis (not a single `/`)
        assert!(!ok(".//span/ancestor::div")); // relative context could reach above the scope
        assert!(!ok("//a[1]/ancestor::div")); // positional INNER
        assert!(!ok("//span/ancestor-or-self::div")); // ancestor-or-self unsupported
        assert!(!ok("//span/preceding::div")); // preceding axis unsupported
    }

    #[test]
    fn following_sibling_unsupported_shapes_reject() {
        assert!(!ok("//following-sibling::b")); // anchor step: no preceding sibling to root on
        assert!(!ok("//a//following-sibling::b")); // `//` before it: descendant-or-self then sibling, not `~`
        assert!(!ok("//a/following-sibling::b[1]")); // positional on the sibling axis (not expressible as `~`)
        assert!(!ok("//a/following-sibling::td[last()]")); // reverse position on the sibling axis
        assert!(!ok("//a/preceding-sibling::b")); // preceding axis: forward-unknowable in one pass
        assert!(!ok("//a/following::b")); // following (non-sibling) axis
        assert!(!ok("//a/child::b")); // downward synonym: unsupported (use `//a/b`)
    }
    #[test]
    fn uppercase_names_reject_matching_libxml2_lowercasing() {
        // libxml2 lowercases HTML names in the tree; XPath name-tests are case-sensitive, so any
        // uppercase literal matches nothing in lxml. Compile such queries as unsupported (empty), not
        // case-insensitive over-matches. All-lowercase equivalents stay supported.
        assert!(!ok("//DIV")); // uppercase tag
        assert!(!ok("//foreignObject//div")); // camelCase foreign tag
        assert!(!ok("//rect/@ID")); // uppercase attr (terminal)
        assert!(!ok("//svg/@viewBox")); // camelCase attr (terminal)
        assert!(!ok("//div[@ID]")); // uppercase attr in predicate
        assert!(!ok("//a[contains(@Href,\"x\")]")); // uppercase attr in contains()
        assert!(ok("//div/@id")); // lowercase equivalents unaffected
        assert!(ok("//svg//rect"));
        assert!(ok("//a[@data-k=\"V\"]")); // uppercase VALUE is fine (values are case-sensitive in both)
    }
    #[test]
    fn absolute_single_slash_anchors_to_html_root() {
        // libxml2 always roots the tree at synthesized <html>; a single-slash `/X` selects the root
        // element, so it matches nothing unless X is html. `//X` (descendant) is unaffected.
        assert!(!ok("/tr")); // would otherwise over-match like //tr
        assert!(!ok("/p/@id"));
        assert!(!ok("/div//text()"));
        assert!(!ok("/*[@id]")); // `/ *` = root only; matcher can't anchor -> unsupported
        assert!(ok("/html")); // the one matching form
        assert!(ok("/html/body/div"));
        assert!(ok("/html/body//a/@href"));
        assert!(ok("//tr")); // descendant anchor still matches anywhere
        assert!(ok("//div/text()"));
        assert!(ok(".//span/text()")); // relative (context) path unaffected
    }
    #[test]
    fn relative_child_anchor_rejected_descendant_kept() {
        // `./step` is a direct child of the context node, which the depth-agnostic matcher can't
        // anchor — it would over-match like `.//step` (wrong values). Reject as unsupported.
        assert!(!ok("./h3")); // would over-match like .//h3
        assert!(!ok("./h3/text()"));
        assert!(!ok("./body/h3/text()"));
        assert!(!ok("./div/@class"));
        assert!(!ok("./@class")); // bare `.` path (also rejected by tokenize_path)
        // `.//…` (descendant of context) is anchor-free and stays supported and correct.
        assert!(ok(".//h3"));
        assert!(ok(".//span/text()"));
        assert!(ok(".//div//text()"));
        assert!(ok(".//a/@href"));
        assert!(ok(".//div/h3/text()")); // only the LEADING anchor is the issue; inner `/` is enforced
    }
}

#[cfg(test)]
mod support_boundary_tests {
    use super::*;

    /// An expression lxml REJECTS must be unsupported here, not answered. `//div/@x:y` was the worst
    /// case: lxml raises "undefined namespace prefix" and we returned the attribute's value — a wrong
    /// value from an expression the oracle refuses to run.
    #[test]
    fn expressions_lxml_rejects_are_unsupported() {
        for q in [
            "//div/@1",            // attribute name starting with a digit -> lxml: invalid expression
            "//1div/text()",       // element name starting with a digit
            "//svg:rect/text()",   // namespace prefix, no bindings -> lxml: undefined prefix
            "//div/@x:y",          // prefixed attribute -> lxml: undefined prefix
            "//a:b/@c:d",
            "//div/@-1",
        ] {
            assert!(compile(q).is_none(), "lxml rejects {q:?}, so it must be unsupported here");
        }
        // ...and the valid neighbours must keep working
        for q in ["//div/@data-x", "//div/text()", "//div/@x", "//x-y/text()", "//div/@_x"] {
            assert!(compile(q).is_some(), "{q:?} is valid and must stay supported");
        }
    }
}
