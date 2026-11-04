"""The lossless XPath bridge: a browser's JSON tree (`__ser`, see client.py) as an lxml tree, and back.

Browser-mode XPath is the browser's tree with lxml's XPath semantics (docs/BROWSER_MODE.md, decision 5):
`//svg` finds an SVG element and `//P` does not find a `<p>`. So XPath is never run in the browser. It
runs in lxml, over an lxml tree built node for node from the browser's tree, and its answers are mapped
back to the browser's strings.

lxml cannot hold everything a browser keeps, so the bridge carries a per-document REVERSIBLE MAP rather
than sanitising:

  * a code point XML forbids (C0 controls but tab, LF and CR; U+FFFE, U+FFFF; surrogates) in text, an
    attribute value or comment data, and a character lxml refuses in an element name (whitespace,
    `"&'/<>`, a leading `{`), an attribute name or a processing-instruction's data, becomes ONE private-use
    character, `chr(base + c)`. `base` is the start of supplementary private-use plane 15 or 16, whichever
    the document's strings never use, so decoding is the inverse character map over every answer;
  * a comment lxml refuses (`--`, or a trailing `-`) has its hyphens mapped the same way;
  * an XPath query passes through the value map too, so a literal holding a control character still
    compares equal to the value it names. One character for one character keeps `string-length()`,
    `substring()` and `translate()` exact;
  * the namespace of each element, the doctype, HTML template content and shadow roots are kept beside
    the lxml tree, not in it: XPath reaches none of them (`tree.document_view`), and `to_json` restores
    them, so `Bridge(tree).to_json() == tree` is checkable.

What no map can express raises `Unrepresentable`: two adjacent text nodes or an empty one (lxml has one
`.text`/`.tail` slot per position, so the two would merge and `text()` would return one string where
the browser has two), a document whose strings use both private-use planes, or a processing-instruction
target that is not an XML name. A caller counts and reports those documents; it never grades them
through lxml and never drops them silently.

One limit is not detectable from a query: a literal compared with an element NAME the map rewrote
(`name()="p<img"`) is value-mapped and cannot match. `Bridge.mapped_names` says whether a document has
such a name.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from lxml import etree

from .tree import is_element

PLANES = (0xF0000, 0x100000)

_VALUE_BAD = "\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff"
_VALUE_RE = re.compile(f"[{_VALUE_BAD}]")
# what lxml's HTML documents refuse in an element name, on top of the value set (measured, not assumed:
# `etree.SubElement` over every ASCII character)
_TAG_RE = re.compile(f"[{_VALUE_BAD}\t\n\r \"&'/<>]|^\\{{")
_ATTR_RE = re.compile(f"[{_VALUE_BAD}]|^\\{{")
_PI_DATA_RE = re.compile(f"[{_VALUE_BAD}]|(?<=\\?)>")


class Unrepresentable(ValueError):
    """The browser's tree has a shape lxml cannot hold, even through the reversible map."""


def _strings(tree: Any):
    """Every string a `__ser` tree carries: names, values, text, comment and doctype fields."""
    stack = [tree]
    while stack:
        n = stack.pop()
        if isinstance(n, str):
            yield n
        elif isinstance(n, list):
            for x in n:
                if isinstance(x, (str, list)):
                    stack.append(x)


def choose_plane(tree: Any) -> int:
    """The private-use plane no string of `tree` uses, so a placeholder can never be mistaken for data."""
    used = set()
    for s in _strings(tree):
        for ch in s:
            if ord(ch) >= PLANES[0]:
                used.add(0 if ord(ch) < PLANES[1] else 1)
    for k, base in enumerate(PLANES):
        if k not in used:
            return base
    raise Unrepresentable("the document uses both supplementary private-use planes")


class Bridge:
    """One browser tree as an lxml tree. `xpath(q)` answers with lxml's semantics in the browser's strings.

    `root` is the lxml root element (parsel: `parsel.Selector(root=bridge.root)`); `encode`/`decode` are
    the map, for a caller that evaluates through lxml itself.
    """

    def __init__(self, tree: Any):
        if not (isinstance(tree, list) and tree and tree[0] == "#d"):
            raise Unrepresentable("a tree must be a document (`#d`)")
        self.base = choose_plane(tree)
        self.mapped_names = False
        self._node_of: dict[Any, Any] = {}  # lxml element -> its browser JSON node
        self._meta: list[tuple[Any, list[tuple[int, Any]]]] = []  # per element, in document order
        self._doctypes: list[tuple[int, Any]] = []
        self._selector = None
        top = tree[1]
        roots = [k for k, n in enumerate(top) if is_element(n)]
        if len(roots) != 1:
            raise Unrepresentable(f"a document with {len(roots)} root elements")
        parser = etree.HTMLParser()
        self.root = etree.fromstring("<html></html>", parser)
        r = roots[0]
        self._fill(self.root, top[r])
        for k, n in enumerate(top):
            if k == r:
                continue
            if isinstance(n, list) and n[0] == "#dt":
                self._doctypes.append((k, n))
            elif isinstance(n, str):
                raise Unrepresentable("text at the top level of a document")
        before = [n for k, n in enumerate(top[:r]) if not (isinstance(n, list) and n[0] == "#dt")]
        after = [n for n in top[r + 1:] if not (isinstance(n, list) and n[0] == "#dt")]
        for n in before:
            self.root.addprevious(self._leaf(n))
        for n in reversed(after):
            self.root.addnext(self._leaf(n))

    # ------------------------------------------------------------------ the map
    def _sub(self, pattern: re.Pattern, s: str) -> str:
        base = self.base
        return pattern.sub(lambda m: chr(base + ord(m.group()[-1])), s)

    def encode(self, s: str) -> str:
        """A value, or an XPath query, in the lxml tree's alphabet."""
        return self._sub(_VALUE_RE, s)

    def decode(self, s: str) -> str:
        """An answer from the lxml tree in the browser's alphabet: every placeholder back to its character."""
        lo, hi = self.base, self.base + 0x10000
        if not any(lo <= ord(ch) < hi for ch in s):
            return s
        return "".join(chr(ord(ch) - lo) if lo <= ord(ch) < hi else ch for ch in s)

    def _name(self, name: str, pattern: re.Pattern, what: str) -> str:
        """An element or attribute name in lxml's alphabet."""
        if not name:
            raise Unrepresentable(f"an empty {what} name")
        out = self._sub(pattern, name)
        self.mapped_names |= out != name
        return out

    def _comment(self, data: str) -> Any:
        s = self.encode(data)
        if "--" in s or s.endswith("-"):
            s = s.replace("-", chr(self.base + 0x2D))
        return etree.Comment(s)

    def _leaf(self, n: Any) -> Any:
        if isinstance(n, list) and n[0] == "#c":
            return self._comment(n[1])
        if isinstance(n, list) and n[0] == "#7":
            if len(n) != 3:
                raise Unrepresentable("a processing instruction serialised without its target and data")
            try:
                return etree.ProcessingInstruction(n[1], self._sub(_PI_DATA_RE, n[2]))
            except ValueError as e:
                raise Unrepresentable(f"processing instruction target {n[1]!r}: {e}") from None
        raise Unrepresentable(f"a {n[0] if isinstance(n, list) else type(n).__name__} node lxml cannot hold here")

    # ------------------------------------------------------------------ building
    def _fill(self, el: Any, node: Any) -> None:
        """Make lxml element `el` hold browser element `node`: name, attributes and children, depth first."""
        stack = [(el, node)]
        while stack:
            el, node = stack.pop()
            el.tag = self._name(node[0], _TAG_RE, "element")
            hidden: list[tuple[int, Any]] = []
            self._meta.append((node[1], hidden))
            self._node_of[el] = node  # keeps the proxy alive, so XPath hands back this same object
            flat = node[2]
            for k in range(0, len(flat), 2):
                el.set(self._name(flat[k], _ATTR_RE, "attribute"), self.encode(flat[k + 1]))
            last, prev_text = None, False
            pending: list[tuple[Any, Any]] = []
            for idx, kid in enumerate(node[3]):
                if isinstance(kid, str):
                    if not kid:
                        raise Unrepresentable("an empty text node")
                    if prev_text:
                        raise Unrepresentable("two adjacent text nodes, which lxml would merge into one")
                    if last is None:
                        el.text = self.encode(kid)
                    else:
                        last.tail = self.encode(kid)
                    prev_text = True
                    continue
                if isinstance(kid, list) and kid[0] in ("#d", "#shadow"):
                    hidden.append((idx, kid))  # outside the document view; restored by to_json
                    continue
                prev_text = False
                if is_element(kid):
                    child = etree.SubElement(el, "x")
                    pending.append((child, kid))
                else:
                    child = self._leaf(kid)
                    el.append(child)
                last = child
            # depth first in document order: the stack pops the first child next
            stack.extend(reversed(pending))
        # `_meta` is in the order elements were FILLED, which the stack makes pre-order document order

    # ------------------------------------------------------------------ answers
    def values(self, results: Any) -> list[str]:
        """parsel's `.getall()` for a `SelectorList` from this tree, in the browser's strings.

        An element is answered with the browser's own serialisation of its JSON node (`outer_html`), not
        lxml's: the lxml tree is the document view, so lxml would serialise a `<template>` empty where the
        browser's `outerHTML` holds its content."""
        out = []
        for s in results:
            root = s.root
            if not isinstance(root, etree._Element):
                out.append("1" if root is True else "0" if root is False else self.decode(str(root)))
                continue
            node = self._node_of.get(root)
            if node is not None:
                out.append(outer_html(node))
            elif root.tag is etree.Comment:
                out.append(outer_html(["#c", self.decode(root.text or "")]))
            elif root.tag is etree.ProcessingInstruction:
                out.append(outer_html(["#7", root.target, self.decode(root.text or "")]))
            else:
                raise AssertionError(f"an lxml node the bridge did not build: {root!r}")
        return out

    def xpath(self, query: str) -> list[str]:
        """parsel's `.xpath(query).getall()` over the bridged tree, in the browser's strings."""
        return self.values(self.selector().xpath(self.encode(query)))

    def css(self, query: str) -> list[str]:
        """parsel's `.css(query).getall()` over the bridged tree: lxml's CSS semantics, the browser's tree."""
        return self.values(self.selector().css(self.encode(query)))

    def selector(self) -> Any:
        """A `parsel.Selector` over the bridged tree. Its answers are in the map's alphabet: `decode` them."""
        import parsel

        if self._selector is None:
            self._selector = parsel.Selector(root=self.root, type="html")
        return self._selector

    # ------------------------------------------------------------------ and back
    def to_json(self) -> Any:
        """The browser's tree, rebuilt from the lxml tree and the side tables alone."""
        meta = iter(self._meta)

        def leaf(c: Any) -> Any:
            if c.tag is etree.Comment:
                return ["#c", self.decode(c.text or "")]
            if c.tag is etree.ProcessingInstruction:
                return ["#7", c.target, self.decode(c.text or "")]
            raise AssertionError(f"unexpected lxml node {c!r}")

        def element(e: Any) -> Any:
            ns, hidden = next(meta)
            flat: list[str] = []
            for k, v in e.attrib.items():
                flat += [self.decode(k), self.decode(v)]
            kids: list[Any] = [self.decode(e.text)] if e.text else []
            for c in e:
                kids.append(element(c) if isinstance(c.tag, str) else leaf(c))
                if c.tail:
                    kids.append(self.decode(c.tail))
            for idx, h in hidden:
                kids.insert(idx, h)
            return [self.decode(e.tag), ns, flat, kids]

        before = [leaf(c) for c in reversed(list(self.root.itersiblings(preceding=True)))]
        after = [leaf(c) for c in self.root.itersiblings()]
        top = before + [element(self.root)] + after
        for idx, dt in self._doctypes:
            top.insert(idx, dt)
        return ["#d", top]


# ---------------------------------------------------------------- the browser's serialisation
# The HTML standard's fragment serialisation algorithm, as `outerHTML` runs it, over a `__ser` node. Every
# document the oracle page-loads has scripting on, and DOMParser's (scripting off) never hold a <noscript>,
# so a <noscript>'s text is raw, except inside template content, whose inert document has no scripting.
# `<` and `>` in an attribute value are escaped, as both browsers now do.
VOID = frozenset("area base basefont bgsound br col embed frame hr img input keygen link meta param source "
                 "track wbr".split())
RAW_TEXT_PARENTS = frozenset("style script xmp iframe noembed noframes plaintext noscript".split())


def _escape(s: str, attribute: bool) -> str:
    s = s.replace("&", "&amp;").replace("\xa0", "&nbsp;").replace("<", "&lt;").replace(">", "&gt;")
    return s.replace('"', "&quot;") if attribute else s


def outer_html(node: Any) -> str:
    """`node.outerHTML` for a `__ser` element, comment or text, computed from the JSON tree alone."""
    out: list[str] = []

    def kids(n: Any) -> list[Any]:
        if n[0] == "template" and n[1] == 0:
            return next((k[1] for k in n[3] if isinstance(k, list) and k and k[0] == "#d"), [])
        return [k for k in n[3] if not (isinstance(k, list) and k and k[0] in ("#d", "#shadow"))]

    def walk(n: Any, raw: bool, scripting: bool) -> None:
        if isinstance(n, str):
            out.append(n if raw else _escape(n, False))
        elif n[0] == "#c":
            out.append(f"<!--{n[1]}-->")
        elif n[0] == "#7":
            out.append(f"<?{n[1]} {n[2]}?>")  # Chrome 156, which parses processing instructions
        elif n[0] == "#dt":
            out.append(f"<!DOCTYPE {n[1]}>")
        elif is_element(n):
            flat = n[2]
            attrs = "".join(f' {flat[k]}="{_escape(flat[k + 1], True)}"' for k in range(0, len(flat), 2))
            out.append(f"<{n[0]}{attrs}>")
            if n[1] == 0 and n[0] in VOID:
                return
            html = n[1] == 0
            raw_kids = html and (n[0] in RAW_TEXT_PARENTS - {"noscript"} or (n[0] == "noscript" and scripting))
            # template content is in the template's inert document, which has no scripting
            inner = scripting and not (html and n[0] == "template")
            for k in kids(n):
                walk(k, raw_kids, inner)
            out.append(f"</{n[0]}>")

    walk(node, False, True)
    return "".join(out)


def round_trip(tree: Any) -> Optional[str]:
    """`None` when `tree` survives tree -> lxml -> tree exactly, else what differed. `Unrepresentable` raises."""
    back = Bridge(tree).to_json()
    if back == tree:
        return None
    return first_difference(tree, back)


def first_difference(a: Any, b: Any, path: str = "") -> str:
    """A short description of where two JSON trees first differ."""
    if type(a) is not type(b):
        return f"{path or '/'}: {a!r} != {b!r}"
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path or '/'}: {len(a)} items != {len(b)}: {a!r} != {b!r}"
        for k, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return first_difference(x, y, f"{path}/{k}")
    return f"{path or '/'}: {a!r} != {b!r}"


def round_trip_failures(trees: list[Any]) -> tuple[list[tuple[Any, str]], list[tuple[Any, str]]]:
    """`(failures, unrepresentable)` over `trees`: a tree the bridge changed, and one it cannot hold.

    The round-trip gate's whole verdict, kept as a function so `tests/test_gates.py` can seed a lossy map
    into it: a bridge that quietly drops a control character reads exactly like a clean one.
    """
    failures, unrepresentable = [], []
    for t in trees:
        if t is None:
            continue
        try:
            diff = round_trip(t)
        except Unrepresentable as e:
            unrepresentable.append((t, str(e)))
            continue
        except Exception as e:  # noqa: BLE001 - lxml refusing what the map should have mapped is a failure
            diff = f"{type(e).__name__}: {e}"
        if diff is not None:
            failures.append((t, diff))
    return failures, unrepresentable
