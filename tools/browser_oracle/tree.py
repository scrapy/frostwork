"""Selector answers computed natively from a browser's JSON tree (the `__ser` format, see client.py).

The browser gate never rebuilds the browser's tree in lxml: lxml refuses control characters, `p<img`
tag names and other things browsers keep, so a rebuild would lose exactly the malformed cases the gate
exists for. Instead this module evaluates the gate's XPath columns over the JSON tree itself, with lxml's
XPath semantics (a name test matches the local name in any namespace, case-sensitively; `//` sees every
root; a node-set is deduplicated by node identity and returned in document order).

It evaluates a deliberately small subset, the one `seq_sweep.fingerprint_selectors` uses:

    path  := ('/' | '//') step (('/' | '//') step)*
    step  := ('*' | NAME | 'text()' | '@' NAME) pred*
    pred  := '[@' NAME '="' VALUE '"]' | '[' INT ']'

Anything else raises `ValueError`, so a new column shape cannot be evaluated by a rule written for a
different one. `from_lxml` converts lxml's own tree into the same format, which is how
`tests/test_gates.py` proves this evaluator gives lxml's answers on lxml's trees.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

XHTML = 0  # __ser writes the HTML namespace as 0


def is_element(node: Any) -> bool:
    return isinstance(node, list) and isinstance(node[0], str) and not node[0].startswith("#")


def document_view(tree: Any) -> Any:
    """The tree a selector reaches: HTML template content and shadow roots removed.

    `__ser` appends an HTML `<template>`'s content to its children as a `#d` fragment and an attached
    shadow root as `#shadow`; neither is in the document's child lists, so neither is selectable.
    """
    if isinstance(tree, str) or not isinstance(tree, list):
        return tree
    if tree[0] == "#d":
        return ["#d", [document_view(k) for k in tree[1]]]
    if not is_element(tree):
        return tree
    kids = [document_view(k) for k in tree[3]
            if not (isinstance(k, list) and k and k[0] in ("#d", "#shadow"))]
    return [tree[0], tree[1], tree[2], kids]


# ---------------------------------------------------------------- an indexed view of the tree
class _Node:
    __slots__ = ("kind", "name", "attrs", "children", "order", "data")

    def __init__(self, kind: str, order: int, name: str = "", attrs: Optional[list[tuple[str, str]]] = None,
                 data: str = ""):
        self.kind = kind            # "doc", "el", "text" or "other"
        self.name = name
        self.attrs = attrs or []
        self.children: list[_Node] = []
        self.order = order
        self.data = data


def index(tree: Any) -> _Node:
    """An indexed view of a JSON document for `evaluate`; index a tree once to ask it many columns."""
    counter = [0]

    def build(n: Any) -> _Node:
        counter[0] += 1
        if isinstance(n, str):
            return _Node("text", counter[0], data=n)
        if n[0] == "#d":
            node = _Node("doc", counter[0])
            node.children = [build(k) for k in n[1]]
            return node
        if is_element(n):
            flat = n[2]
            node = _Node("el", counter[0], name=n[0], attrs=list(zip(flat[0::2], flat[1::2])))
            node.children = [build(k) for k in n[3]]
            return node
        return _Node("other", counter[0])

    root = build(tree)
    if root.kind != "doc":
        raise ValueError("a tree must be a document (`#d`)")
    return root


# ---------------------------------------------------------------- the XPath subset
_STEP = re.compile(r"(//|/)(\*|text\(\)|@[A-Za-z_][\w.-]*|[A-Za-z_][\w.-]*)((?:\[[^\]]*\])*)")
_PRED = re.compile(r'\[(?:@([A-Za-z_][\w.-]*)="([^"]*)"|([1-9][0-9]*))\]')


def parse(xpath: str) -> list[tuple[bool, str, list[tuple]]]:
    """`(descendant, test, predicates)` per step, or `ValueError` for a shape outside the subset."""
    steps, pos = [], 0
    while pos < len(xpath):
        m = _STEP.match(xpath, pos)
        if not m:
            raise ValueError(f"outside the native evaluator's subset: {xpath!r} at {pos}")
        preds, ppos, ptext = [], 0, m.group(3)
        while ppos < len(ptext):
            pm = _PRED.match(ptext, ppos)
            if not pm:
                raise ValueError(f"unsupported predicate in {xpath!r}: {ptext[ppos:]!r}")
            preds.append(("pos", int(pm.group(3))) if pm.group(3) else ("attr", pm.group(1), pm.group(2)))
            ppos = pm.end()
        steps.append((m.group(1) == "//", m.group(2), preds))
        pos = m.end()
    if not steps:
        raise ValueError(f"empty path {xpath!r}")
    for _, test, _ in steps[:-1]:
        if test == "text()" or test.startswith("@"):
            raise ValueError(f"a value step must be the last one: {xpath!r}")
    return steps


def _descendants_or_self(node: _Node) -> Iterable[_Node]:
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed([k for k in n.children if k.kind in ("el", "doc")]))


def _step(context: list[_Node], descendant: bool, test: str, preds: list[tuple]) -> list[Any]:
    """One location step from every context node; returns nodes, or `(owner, value)` for attributes."""
    if descendant:
        seen: dict[int, _Node] = {}
        for c in context:
            for d in _descendants_or_self(c):
                seen.setdefault(id(d), d)
        context = sorted(seen.values(), key=lambda n: n.order)
    found: dict[Any, tuple] = {}
    for c in context:
        if test.startswith("@"):
            if preds:
                raise ValueError("a predicate on an attribute step is outside the subset")
            for n, (name, value) in enumerate(c.attrs):
                if name == test[1:]:
                    found.setdefault((id(c), n), ((c.order, 1, n), (c, value)))
            continue
        if test == "text()":
            cands = [k for k in c.children if k.kind == "text"]
        elif test == "*":
            cands = [k for k in c.children if k.kind == "el"]
        else:
            cands = [k for k in c.children if k.kind == "el" and k.name == test]
        for pred in preds:
            if pred[0] == "pos":
                cands = cands[pred[1] - 1:pred[1]]
            else:
                cands = [k for k in cands if any(a == (pred[1], pred[2]) for a in k.attrs)]
        for node in cands:
            found.setdefault(id(node), ((node.order, 0, 0), node))
    return [v for _, v in sorted(found.values(), key=lambda e: e[0])]


def evaluate(root: _Node, xpath: str) -> list[str]:
    steps = parse(xpath)
    context: list[Any] = [root]
    for descendant, test, preds in steps:
        context = _step(context, descendant, test, preds)
    last = steps[-1][1]
    if last.startswith("@"):
        return [value for _, value in context]
    if last == "text()":
        return [n.data for n in context]
    raise ValueError(f"the native evaluator answers value columns only (text() or @attr): {xpath!r}")


def fingerprint(tree: Any, selectors: list[str]) -> list[list[str]]:
    """Each selector's answer over `tree`'s document view, as `frostwork.extract` would return it."""
    root = index(document_view(tree))
    return [evaluate(root, s) for s in selectors]


# ---------------------------------------------------------------- lxml's tree in the same format
def from_lxml(element: Any) -> Any:
    """The document holding lxml `element` (for example `parsel.Selector(...).root`), in `__ser` format.

    Every top-level sibling of the root is kept, because libxml2 can build more than one root and `//`
    sees all of them. lxml keeps the doctype outside the tree, so there is no `#dt` node.
    """
    from lxml import etree

    def node(e: Any) -> list[Any]:
        if e.tag is etree.Comment:
            return ["#c", e.text or ""]
        if e.tag is etree.ProcessingInstruction:
            return ["#7", e.target, e.text or ""]
        if not isinstance(e.tag, str):
            return ["#?"]
        flat: list[str] = []
        for k, v in e.attrib.items():
            flat += [k, v]
        return [e.tag, XHTML, flat, children(e)]

    def children(e: Any) -> list[Any]:
        out: list[Any] = [e.text] if e.text else []
        for k in e:
            out.append(node(k))
            if k.tail:
                out.append(k.tail)
        return out

    top = list(reversed(list(element.itersiblings(preceding=True)))) + [element] + list(element.itersiblings())
    return ["#d", [node(e) for e in top]]
