"""Parsel's CSS, answered natively by a browser: the plans `probe.cjs` executes with `querySelectorAll`.

Browser-mode CSS semantics come from the browser itself (docs/BROWSER_MODE.md, section 5): which elements
a selector matches, attribute-value case folding and quirks-mode class matching included, is whatever
`querySelectorAll` answers on the document the load built. What a browser has no syntax for is parsel's,
and is mirrored from parsel's own translator rather than re-derived:

  * `::text` is the matched element's text-node children, and `::attr(name)` the attribute
    `getAttribute(name)` returns: the name ASCII-lowercased on an HTML element, compared with the DOM name
    exactly on an SVG or MathML one. Parsel's XPath `@name` is case-sensitive everywhere, so the two differ
    only on a name with a capital, which `diff_browser.mirrors_parsel` leaves out;
  * a pseudo-element after a DESCENDANT combinator and a bare universal (`p ::text`, `div *::text`) is
    the SUBTREE form: parsel rewrites the XPath step `descendant-or-self::*/*` to
    `descendant-or-self::text()` (or `descendant-or-self::*/@name`), so the values come from the left
    part's matches and everything below them, the matches themselves included;
  * a comma group is an XPath union: one node is one value, in document order;
  * `:contains(s)` is cssselect's `contains(., s)`, i.e. the element's `textContent` contains `s`. The
    browser has no such pseudo-class, so each one is replaced by a marker attribute set on the matching
    elements for the duration of the query;
  * no pseudo-element is the element itself, answered as `outerHTML`.

Every plan is checked against parsel before it is sent: the CSS the browser gets, with the pseudo-element
put back, must translate to exactly the XPath parsel translates the original to. A selector for which that
fails raises `Unplannable`, so a form this module does not understand is refused rather than answered.

`within` plans answer `node.css(query)` relative to each element of a container query, as parsel's nested
selectors do: their XPath starts `descendant-or-self::`, so the first compound may match the container
itself or anything inside it, and later compounds may leave it (a sibling of the container). The
container is marked with an attribute and the first compound is pinned to `:is([mark], [mark] *)`.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import cssselect
from cssselect.parser import FunctionalPseudoElement
from parsel.csstranslator import HTMLTranslator

_T = HTMLTranslator()
CONTAINS_MARK = "data-fw-contains-"
SCOPE_MARK = "data-fw-scope"


class Unplannable(ValueError):
    """A selector this module cannot hand to `querySelectorAll` with parsel's meaning intact."""


def _scan(css: str):
    """Yield `(index, char, depth)` for each character outside strings, with escapes skipped as units."""
    depth, quote, k = 0, None, 0
    while k < len(css):
        ch = css[k]
        if ch == "\\":
            k += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        else:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            yield k, ch, depth
        k += 1


def top_level(css: str) -> str:
    """`css` with everything inside strings, brackets and parentheses blanked to spaces, escapes kept."""
    out = [" "] * len(css)
    depth, quote, k = 0, None, 0
    while k < len(css):
        ch = css[k]
        if ch == "\\":
            if depth == 0 and not quote:
                out[k:k + 2] = css[k:k + 2]
            k += 2
            continue
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0:
            out[k] = ch
        k += 1
    return "".join(out[:len(css)])


def split_members(css: str) -> list[str]:
    """A comma group's members, split at top-level commas only (`:is(p, span)` stays whole)."""
    cuts = [k for k, ch, depth in _scan(css) if ch == "," and depth == 0]
    bounds = [-1] + cuts + [len(css)]
    return [css[a + 1:b].strip() for a, b in zip(bounds, bounds[1:])]


_PSEUDO = re.compile(r"::\s*(text|attr\s*\([^)]*\))\s*$", re.I)


def unescape(s: str) -> str:
    """CSS escapes in an identifier or string: `\\` + 1-6 hex digits (+ one whitespace), or `\\` + a char."""
    def one(m: re.Match) -> str:
        h = m.group(1)
        if h:
            cp = int(h, 16)
            return "\ufffd" if cp == 0 or cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF else chr(cp)
        return m.group(2)
    return re.sub(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))", one, s, flags=re.S)


def _replace_contains(css: str, marks: list[tuple[str, str]]) -> str:
    """Every `:contains(arg)` as `[marker]`, recording `(marker, needle)`; strings and brackets respected."""
    out, k = [], 0
    low = css.lower()
    positions = [i for i, ch, _depth in _scan(css) if ch == ":" and low.startswith(":contains(", i)]
    for start in positions:
        j = start + len(":contains(")
        while j < len(css) and css[j] in " \t\n\r\f":
            j += 1
        if j < len(css) and css[j] in "\"'":
            q, e = css[j], j + 1
            while e < len(css) and css[e] != q:
                e += 2 if css[e] == "\\" else 1
            needle, j = unescape(css[j + 1:e]), e + 1
        else:
            e = j
            while e < len(css) and css[e] not in " \t\n\r\f)":
                e += 2 if css[e] == "\\" else 1
            needle, j = unescape(css[j:e]), e
        while j < len(css) and css[j] in " \t\n\r\f":
            j += 1
        if j >= len(css) or css[j] != ")":
            raise Unplannable(f"cannot read the :contains() argument in {css!r}")
        name = f"{CONTAINS_MARK}{len(marks)}"
        marks.append((name, needle))
        out.append(css[k:start] + f"[{name}]")
        k = j + 1
    out.append(css[k:])
    return "".join(out)


def _first_compound_end(css: str) -> int:
    """Where the first compound ends: the first top-level combinator, or the end."""
    for k, ch, depth in _scan(css):
        if depth == 0 and ch in " \t\n\r\f>+~":
            return k
    return len(css)


def _pseudo_parts(text: str, pseudo: Any) -> tuple[str, Optional[str], str, str]:
    """What a member answers and where its selector ends: `(value, attr, the selector before the
    pseudo-element, the pseudo-element)`, with `value` one of `outer`, `text` and `attr`."""
    m = _PSEUDO.search(text)
    if pseudo is None:
        return "outer", None, text, ""
    if pseudo == "text" and m:
        return "text", None, text[:m.start()], "::text"
    if isinstance(pseudo, FunctionalPseudoElement) and pseudo.name == "attr" and m and len(pseudo.arguments) == 1:
        argument = text[m.start():].split("(", 1)[1].rsplit(")", 1)[0].strip()
        return "attr", pseudo.arguments[0].value, text[:m.start()], f"::attr({argument})"
    raise Unplannable(f"pseudo-element {pseudo!r} in {text!r}")


def _native_form(text: str, want: str, value: str, attr: Optional[str], pre: str, suffix: str
                 ) -> tuple[str, str, str]:
    """`(css for the browser, scope, the XPath that css translates to)`. `want` is parsel's XPath for the
    member, and the subtree form is the one it ends in `descendant-or-self::`."""
    if value != "outer" and (want.endswith("/descendant-or-self::text()")
                             or want.endswith("/descendant-or-self::*/@" + str(attr))):
        if pre[-1:].isspace():
            css = pre.rstrip()
        elif pre.rstrip().endswith("*") and pre.rstrip()[:-1][-1:].isspace():
            css = pre.rstrip()[:-1].rstrip()
        else:
            raise Unplannable(f"cannot find the subtree form's left part in {text!r}")
        tail = "/descendant-or-self::text()" if value == "text" else f"/descendant-or-self::*/@{attr}"
        return css, "subtree", _T.css_to_xpath(css) + tail
    css = pre.rstrip() or "*"
    if css[-1] in ">+~":
        css += " *"
    return css, "self", _T.css_to_xpath(css + suffix)


def _member(text: str) -> dict[str, Any]:
    try:
        parsed = cssselect.parse(text)
        want = _T.css_to_xpath(text)
    except Exception as e:  # noqa: BLE001 - cssselect's own errors are the refusal reason
        raise Unplannable(f"{type(e).__name__}: {e}") from None
    if len(parsed) != 1:
        raise Unplannable(f"{text!r} is not one comma-group member")
    value, attr, pre, suffix = _pseudo_parts(text, parsed[0].pseudo_element)
    css, scope, check = _native_form(text, want, value, attr, pre, suffix)
    if check != want:
        raise Unplannable(f"the native form of {text!r} would translate to {check!r}, not parsel's {want!r}")
    marks: list[tuple[str, str]] = []
    native = _replace_contains(css, marks)
    cut = _first_compound_end(native)
    anchored = native[:cut] + f":is([{SCOPE_MARK}],[{SCOPE_MARK}] *)" + native[cut:]
    return {"css": native, "anchored": anchored, "value": value, "attr": attr, "scope": scope,
            "contains": marks}


def plan(selector: str) -> dict[str, Any]:
    """The native plan for one parsel CSS selector (a comma group's members are a union)."""
    members = split_members(selector)
    if not all(members):
        raise Unplannable(f"{selector!r} has an empty comma-group member")
    return {"members": [_member(m) for m in members]}


def grouped(container: str, subs: list[str]) -> dict[str, Any]:
    """A container plan and sub-plans answered relative to each container match (parsel's `node.css`)."""
    c = plan(container)
    if any(m["value"] != "outer" for m in c["members"]):
        raise Unplannable(f"a container must select elements, not {container!r}")
    return {"container": c, "subs": [plan(s) for s in subs]}


def is_css(selector: str) -> bool:
    """parsel's routing: XPath starts with `/`, `(` or `./`; this harness sends everything else to CSS."""
    s = selector.lstrip()
    return not (s.startswith(("/", "(", "./")) or s.startswith(("normalize-space(", "string(", "count(")))


def try_plan(selector: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """`(plan, None)` for a selector (a `str`) or a grouped column (`(container, [sub, ...])`), else
    `(None, why not)`."""
    try:
        return (grouped(selector[0], list(selector[1])) if isinstance(selector, tuple) else plan(selector)), None
    except Unplannable as e:
        return None, str(e)
