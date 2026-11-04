"""
tools/browser_values.py: the documented reasons a value column may differ from the browser's.

A value column that differs on a document whose fingerprint agrees is not explained by the tree, so the
section 3 constructs cannot explain it either. VALUE_CONSTRUCTS are the value-level ones, each a documented
decision or divergence rather than a tree rule. They read the reduced, still-decorated document and the
oracle's own tree, never the page it came from, and `attribute_value` names the first that holds.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seq_sweep  # noqa: E402  (which elements the fingerprint names)
from browser_answers import is_node_query  # noqa: E402
from browser_docs import Doc, grouped_spec  # noqa: E402
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


def _outer_root(html: str) -> Optional[tuple]:
    """The element an outer-HTML value's start tag names: its tag, its attribute names (the first of a
    repeated one, as a browser keeps) and its id. Read by a tokenizer rather than by parsing the value
    as a fragment, which loses an `<html>` (a fragment parse has nowhere to put it), and without the
    other values, whose raw spelling (`&noti=c`) Python's unescaping does not read as a browser does."""
    if not html.startswith("<"):
        return None  # a value that begins with content is not one element's source
    p = _FirstStartTag(convert_charrefs=False)
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - a value the tokenizer cannot read has no root to compare
        return None
    return p.root


def _raw_source(r: ValueRepro) -> bool:
    """Outer HTML of the SAME elements, whose content differs: the engine returns raw source (documented
    in AGENTS.md), and where the browser built an element's content differently from its source the two
    parse differently. Same count, and each pair starts with the same tag and attributes."""
    if not is_node_query(r.sel) or len(r.engine) != len(r.oracle):
        return False
    return all(_outer_root(a) is not None and _outer_root(a) == _outer_root(b) for a, b in zip(r.engine, r.oracle))


def _merged_frame_source(r: ValueRepro) -> bool:
    """Outer HTML of an `<html>` or `<body>` that a later start tag merged attributes into: the raw source
    holds the element's own start tag, so the browser's has every name it has and more."""
    if not is_node_query(r.sel) or len(r.engine) != len(r.oracle):
        return False
    pairs = [(_outer_root(a), _outer_root(b)) for a, b in zip(r.engine, r.oracle)]
    if any(a is None or b is None for a, b in pairs) or all(a == b for a, b in pairs):
        return False
    return all(a == b or (a[0] == b[0] in ("html", "body") and set(a[1]) < set(b[1])) for a, b in pairs)


VALUE_CONSTRUCTS: list[ValueConstruct] = [
    ValueConstruct("quirks-mode class and id matching (deferred)", _quirks_class_or_id),
    ValueConstruct("raw-source outer HTML", _raw_source),
    ValueConstruct("raw-source outer HTML of a merged <html>/<body>", _merged_frame_source),
]


def attribute_value(r: ValueRepro) -> Optional[str]:
    for c in VALUE_CONSTRUCTS:
        if c.test(r):
            return c.name
    return None
