"""
tools/browser_values.py: the documented reasons a value column may differ from the browser's.

A value column that differs on a document whose fingerprint agrees is not explained by the tree, so the
section 3 constructs cannot explain it either. VALUE_CONSTRUCTS are the value-level ones, each a documented
decision or divergence rather than a tree rule. They read the reduced, still-decorated document and the
oracle's own tree, never the page it came from, and `attribute_value` names the first that holds.
"""
from __future__ import annotations

import functools
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Optional

import frostwork

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seq_sweep  # noqa: E402  (which elements the fingerprint names)
from browser_answers import is_node_query  # noqa: E402
from browser_docs import Doc, grouped_spec, render  # noqa: E402
from browser_oracle import css as C  # noqa: E402
from browser_oracle import tree as T  # noqa: E402


@dataclass
class ValueRepro:
    doc: Doc                  # the reduced document, decorated
    sel: str
    engine: list[str]
    oracle: list[str]
    tree: Any                 # the oracle's tree of `doc`

    def quirks(self) -> bool:
        return T.is_quirks(self.tree)

    def unseen_elements(self) -> list[str]:
        """Names of the oracle's elements the fingerprint cannot see, and that this value column is about.

        Every generated start tag carries an id, so an element with none was built by the tree builder.
        The fingerprint sees one only between two id'd elements, or by name when it holds id'd children or
        text (`seq_sweep.SYNTHESISED`). An EMPTY one, such as the `<p>` a stray `</p>` inserts, answers
        every fingerprint column identically, and only a value column (its outer HTML, a `:has(> p)`) can
        show it. It counts here only when the column names it: the selector says the name, or an oracle
        value holds its tag."""
        names = set()
        for el in T.elements(self.tree):
            attrs = el[2][0::2]
            if "id" in attrs or el[0] in ("html", "head", "body"):
                continue
            sees = el[0] in seq_sweep.SYNTHESISED and any(
                isinstance(k, str) or (T.is_element(k) and "id" in k[2][0::2]) for k in el[3])
            if not sees:
                names.add(el[0])
        return sorted(n for n in names
                      if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", self.sel)
                      or any(isinstance(v, str) and re.search(rf"<{re.escape(n)}[\s/>]", v) for v in self.oracle))


_ASCII_WS = re.compile(r"[\t\n\x0c\r ]+")
_CLASS_OR_ID = re.compile(r"([.#])((?:[\w-]|\\[0-9a-fA-F]{1,6}[ \t\n\r\f]?|\\.)+)")


def _quirks_class_or_id(r: ValueRepro) -> bool:
    """A class or id selector that a quirks-mode document's element passes only case-folded.

    Deferred by decision 2 in docs/BROWSER_MODE.md section 8: browser mode builds the quirks-mode TREE,
    but matches class and id selectors case-sensitively in every mode."""
    if not C.is_css(r.sel) or grouped_spec(r.sel) or not r.quirks():
        return False
    for kind, raw in _CLASS_OR_ID.findall(C.top_level(r.sel)):
        want = C.unescape(raw)
        for el in T.elements(r.tree):
            attrs = dict(zip(el[2][0::2], el[2][1::2]))
            have = _ASCII_WS.split(attrs.get("class", "")) if kind == "." else [attrs.get("id", "")]
            if want not in have and want.lower() in (h.lower() for h in have):
                return True
    return False


@dataclass(frozen=True)
class ValueConstruct:
    name: str
    test: Callable[[ValueRepro], bool]


class _FirstStartTag(HTMLParser):
    """The first start tag of a value, as `(tag, sorted attribute names, id)`. `<image>` is the `<img>` a
    browser makes of it: raw source keeps the name the page wrote."""

    root: Optional[tuple] = None

    def handle_starttag(self, tag, attrs):
        if self.root is not None:
            return
        names: list[str] = []
        ident = None
        for k, v in attrs:
            if k not in names:
                names.append(k)
                ident = v if k == "id" else ident
        self.root = ("img" if tag == "image" else tag, tuple(sorted(names)), ident)


_BR_END_TAG = re.compile(r"</br[\t\n\x0c\r /][^>]*>|</br>", re.I)


def _outer_root(html: str) -> Optional[tuple]:
    """The element an outer-HTML value's start tag names: its tag, its attribute names (the first of a
    repeated one, as a browser keeps) and its id. Read by a tokenizer rather than by parsing the value
    as a fragment, which loses an `<html>` (a fragment parse has nowhere to put it), and without the
    other values, whose raw spelling (`&noti=c`) Python's unescaping does not read as a browser does.
    A `</br>` is the source of the `<br>` a browser makes of it, which has no attributes."""
    if _BR_END_TAG.fullmatch(html):
        return ("br", (), None)
    if not html.startswith("<"):
        return None  # a value that begins with content is not one element's source
    p = _FirstStartTag(convert_charrefs=False)
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - a value the tokenizer cannot read has no root to compare
        return None
    return p.root


_TRAILING_END_TAGS = re.compile(r"(?:</[^<>]*>)+$")


def _text_of(html: str, serialised: bool = False, context: Optional[str] = None) -> str:
    """The text an outer-HTML value holds: the value parsed as a UTF-8 document in browser mode, which
    reads raw text, RCDATA, `<noscript>` and `<plaintext>` as Chrome does. Both sides of a pair are read
    this way, and the text itself is graded against Chrome by the per-id text columns. A serialisation's
    trailing end tags hold no text and are dropped first, since a `</plaintext>` would parse back as text.
    `context` is the foreign element (`svg`, `math`) the value was parsed inside, without which its
    `<style>` or `<script>` would be read as raw text. UTF-8 is pinned: a value may hold the page's own
    `<meta charset>`, which the prescan would otherwise obey."""
    if serialised:
        html = _TRAILING_END_TAGS.sub("", html)
    if context:
        html = f"<{context}>{html}"
    return "".join(frostwork.extract(html.encode("utf-8", "surrogatepass"), ["html ::text"], parser="browser",
                                     encoding="utf-8")[0])


_FOREIGN = {"http://www.w3.org/2000/svg": "svg", "http://www.w3.org/1998/Math/MathML": "math"}


def _context(r: ValueRepro, name: str) -> Optional[str]:
    """The foreign element a value whose root is `name` lies inside: `svg` or `math` when every element
    of the oracle's tree so named is in that namespace, else None (an HTML or a mixed name)."""
    if r.tree is None:
        return None
    spaces = {el[1] for el in T.elements(r.tree) if el[0].lower() == name}
    return _FOREIGN.get(spaces.pop()) if len(spaces) == 1 else None


def _lines(text: str) -> str:
    """Text with each CR read as a line feed. A browser serialises a CR from `&#13;` as a literal CR, which
    re-parsing turns into a line feed, while the raw source keeps the reference. A CR and an LF are not
    collapsed into one: the input stream already did that to every literal CRLF, so a pair here is a
    reference's CR beside the LF of the next text node."""
    return text.replace("\r", "\n")


def _in_order(want: str, have: str) -> bool:
    """Is every character of `want` in `have`, in the same order?"""
    rest = iter(have)
    return all(c in rest for c in want)


_START_TAG = re.compile(r"<[A-Za-z][^>]*>")


def _from_page(r: ValueRepro) -> bool:
    """Every engine value is assembled from the page's own bytes, so no start tag occurs in it more often
    than in the page. A raw source, a copy's prefix and a frame's later tag all keep to that; a tag the
    engine wrote twice does not."""
    # the page as the engine's raw source reads it: line breaks normalised, NUL as U+FFFD
    page = render(r.doc).replace("\r\n", "\n").replace("\r", "\n").replace("\0", "\ufffd")
    page = Counter(_START_TAG.findall(page))
    return all(not Counter(_START_TAG.findall(v)) - page for v in r.engine)


def _raw_source(r: ValueRepro) -> bool:
    """Outer HTML of the SAME elements, whose content differs: the engine returns raw source (documented
    in docs/COMPATIBILITY.md), and where the browser built an element's content differently from its source the two
    parse differently. Same count, each pair starts with the same tag and attributes, and the browser's
    text is in the raw source in order. The source may hold more, which the browser moved elsewhere,
    but a value that lost content the browser has is not raw source."""
    if not is_node_query(r.sel) or len(r.engine) != len(r.oracle) or not _from_page(r):
        return False

    def same(a: str, b: str) -> bool:
        root = _outer_root(a)
        if root is None or root != _outer_root(b):
            return False
        context = _context(r, root[0])
        return _in_order(_lines(_text_of(b, serialised=True, context=context)), _lines(_text_of(a, context=context)))
    return all(same(a, b) for a, b in zip(r.engine, r.oracle))


def _merged_frame_source(r: ValueRepro) -> bool:
    """Outer HTML of an `<html>` or `<body>` that a later start tag merged attributes into: the raw source
    holds the element's own start tag, so the browser's has every name it has and more."""
    if not is_node_query(r.sel) or len(r.engine) != len(r.oracle) or not _from_page(r):
        return False
    pairs = [(_outer_root(a), _outer_root(b)) for a, b in zip(r.engine, r.oracle)]
    if any(a is None or b is None for a, b in pairs) or all(a == b for a, b in pairs):
        return False
    return all(a == b or (a[0] == b[0] in ("html", "body") and set(a[1]) < set(b[1])) for a, b in pairs)


_SELECTEDCONTENT = re.compile(r"(<selectedcontent\b[^>]*>).*?(</selectedcontent>)", re.S)


def _selectedcontent_copy(r: ValueRepro) -> bool:
    """Outer HTML holding a `<selectedcontent>` its select's selected option was copied into: the copy is
    no part of the page's source, so the raw source has the element as the page wrote it. Raw source once
    the browser's copies are emptied too, and only then."""
    if not is_node_query(r.sel):
        return False
    emptied = [_SELECTEDCONTENT.sub(r"\1\2", b) for b in r.oracle]
    return emptied != r.oracle and _raw_source(ValueRepro(r.doc, r.sel, r.engine, emptied, r.tree))


@functools.cache
def _chrome_big5_fixes() -> dict[str, str]:
    """Chrome's line for each Big5 pointer in `browser_decoders.BROWSER_BUGS` -> WHATWG's characters."""
    import browser_decoders as D
    return {line: "".join(frostwork.extract(bytes.fromhex(s), ["html ::text"], content_type="text/html; charset=Big5")[0])
            for (b, enc, s), line in D.BROWSER_BUGS.items() if b == "chrome" and enc == "Big5"}


def _chrome_big5(r: ValueRepro) -> bool:
    """A value holding Chrome's reading of one of the four Big5 pointers WHATWG maps to a letter and a
    combining mark: the engine's value, or one another construct explains, once those are WHATWG's."""
    fixed = list(r.oracle)
    for chrome, whatwg in _chrome_big5_fixes().items():
        fixed = [v.replace(chrome, whatwg) for v in fixed]
    if fixed == r.oracle:
        return False
    rest = ValueRepro(r.doc, r.sel, r.engine, fixed, r.tree)
    return fixed == r.engine or any(c.test(rest) for c in VALUE_CONSTRUCTS if c.test is not _chrome_big5)


def _as_windows_1252(text: str) -> str:
    """`text`'s UTF-8 bytes read as windows-1252, as WHATWG decodes it: the five bytes Python's codec
    leaves unassigned are their C1 controls."""
    out = []
    for byte in text.encode("utf-8", "surrogatepass"):
        try:
            out.append(bytes([byte]).decode("cp1252"))
        except UnicodeDecodeError:
            out.append(chr(byte))
    return "".join(out)


def _unlabelled_utf8(r: ValueRepro) -> bool:
    """Chrome's windows-1252 reading of text Frostwork reads as UTF-8: every differing value is the
    engine's UTF-8 read as windows-1252. On a page with no label, Chrome decodes UTF-8 as windows-1252, or
    switches to UTF-8 partway, while Frostwork reads unlabelled valid UTF-8 as UTF-8 (docs/COMPATIBILITY.md,
    "Deliberate differences from browsers")."""
    pairs = [(a, b) for a, b in zip(r.engine, r.oracle) if a != b]
    return (len(r.engine) == len(r.oracle) and bool(pairs)
            and all(b == _as_windows_1252(a) for a, b in pairs))


VALUE_CONSTRUCTS: list[ValueConstruct] = [
    ValueConstruct("quirks-mode class and id matching (deferred)", _quirks_class_or_id),
    ValueConstruct("raw-source outer HTML", _raw_source),
    ValueConstruct("raw-source outer HTML of a merged <html>/<body>", _merged_frame_source),
    ValueConstruct("raw-source outer HTML of a <selectedcontent> copy", _selectedcontent_copy),
    ValueConstruct("Chrome's Big5 decoder bug (not followed)", _chrome_big5),
    ValueConstruct("unlabelled UTF-8 Chrome reads as windows-1252 (documented)", _unlabelled_utf8),
]


def attribute_value(r: ValueRepro) -> Optional[str]:
    for c in VALUE_CONSTRUCTS:
        if c.test(r):
            return c.name
    return None
