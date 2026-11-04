"""
tools/gen_html5_rules.py - derive the HTML5 element categories from a real browser and generate
`src/implied_close/generated_html5.rs` from them.

HTML5's tree builder is a set of algorithms over a handful of element categories: special, formatting,
five scope lists, the implied-end-tag sets, void, and the content state of each element. The algorithms
are written from the spec. The category MEMBERSHIP is measured here, over the same name universe as
`gen_tree_rules.ELEMENTS`: Chrome builds a tree for a small document per name and each category is read
off one observable difference in it (`PROBES`). Firefox runs the same documents and every disagreement is
reported, with Chrome the answer that is generated. `--report` also compares each derived set with the
WHATWG list and prints every difference, which is information, not an error.

Some cells cannot be read from any tree, and the tool says so instead of guessing. A void element is never
on the stack of open elements, so no end tag or start tag can step over it. A table part cannot be placed
without its `<table>`, which is itself special and a scope boundary. A raw-text element has no end tag
token to step over. Such a cell is generated from the WHATWG list, and the generated file's header names
every one per category.

Offline `--check` needs no browser: it compares the Rust file with `browser_oracle/html5_derivation.json`,
the stored per-cell answers, and checks that the probe documents are the ones the answers were measured
with. `--check --live` re-derives from the browser and compares with the stored answers, so a browser
update or an edited probe fails it. `--write` re-derives from the browsers and rewrites both files.

Usage:
  .venv/bin/python tools/gen_html5_rules.py --check            # offline: Rust file == stored derivation
  .venv/bin/python tools/gen_html5_rules.py --check --live     # also re-derive from Chrome
  .venv/bin/python tools/gen_html5_rules.py --write            # re-derive (Chrome + Firefox), rewrite both
  .venv/bin/python tools/gen_html5_rules.py --report [--live]  # derived sets vs the spec, Chrome vs Firefox
  .venv/bin/python tools/gen_html5_rules.py --print            # the rendered Rust
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import gen_tree_rules as G  # noqa: E402  (the one element universe, and the Rust line wrapping)

RUST_SRC = os.path.join(ROOT, "src", "implied_close", "generated_html5.rs")
SNAPSHOT = os.path.join(HERE, "browser_oracle", "html5_derivation.json")
SCHEMA = 1

ELEMENTS: list[str] = G.ELEMENTS
# A name no browser special-cases. Its answers become the catch-all arm of `html5_cat`.
UNKNOWN = "xunknown"
PROBE_NAMES = ELEMENTS + [UNKNOWN]

# The flags in the order they are numbered in the generated file. Each is the FULL set: the scope flags
# extend the default scope, so `applet` carries SCOPE, LIST_ITEM_SCOPE and BUTTON_SCOPE.
CATEGORIES = ("special", "formatting", "scope", "list_item_scope", "button_scope", "table_scope",
              "select_scope", "implied_end", "implied_end_thorough", "void")
FLAG = {c: c.upper() for c in CATEGORIES}

# A second probe, over a different algorithm, for the categories that have one. It is stored beside the
# category and never generated from: the two must agree wherever both can read a name.
WITNESS = {"special": "special_aaa", "scope": "scope_start", "button_scope": "button_scope_start"}

# ------------------------------------------------------------------------------------ the WHATWG lists
# Written out to compare a derivation with them, and to fill the cells no probe can read. HTML namespace
# only: the MathML and SVG elements that are special or scope boundaries (mi, mo, mn, ms, mtext,
# annotation-xml, foreignObject, desc, SVG title) are not HTML names, and the API is keyed on those.
_SCOPE = "applet caption html table td th marquee object select template"
_IMPLIED = "dd dt li optgroup option p rb rp rt rtc"
SPEC: dict[str, set[str]] = {
    "special": set("""address applet area article aside base basefont bgsound blockquote body br button
        caption center col colgroup dd details dir div dl dt embed fieldset figcaption figure footer form
        frame frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html iframe img input keygen li link listing
        main marquee menu meta nav noembed noframes noscript object ol p param plaintext pre script search
        section select source style summary table tbody td template textarea tfoot th thead title tr track
        ul wbr xmp""".split()),
    "formatting": set("a b big code em font i nobr s small strike strong tt u".split()),
    "scope": set(_SCOPE.split()),
    "list_item_scope": set((_SCOPE + " ol ul").split()),
    "button_scope": set((_SCOPE + " button").split()),
    "table_scope": set("html table template".split()),
    # No select scope in the current text: `</select>` is checked against the default scope list.
    "select_scope": set(_SCOPE.split()),
    "implied_end": set(_IMPLIED.split()),
    "implied_end_thorough": set((_IMPLIED + " caption colgroup tbody td tfoot th thead tr").split()),
    "void": set("""area base basefont bgsound br col embed frame hr img input keygen link meta param
        source track wbr""".split()),
}
# The select scope of the earlier spec, kept for `--report`: every element but these two.
OLD_SELECT_SCOPE = set(PROBE_NAMES) - {"optgroup", "option"}
SPEC_MODES: dict[str, str] = {
    **{n: "rcdata" for n in ("textarea", "title")},
    # `noscript` is raw text because the oracle runs with scripting on
    **{n: "rawtext" for n in ("iframe", "noembed", "noframes", "noscript", "style", "xmp")},
    "script": "script",
    "plaintext": "plaintext",
}

# One line per category: what the probe reads. The documents are built by `*_docs` below.
PROBES = {
    "void": "whitespace after `<N>` is its next sibling and N has no child",
    "mode": "markup inside `<N>` is text; `&amp;` decodes or not; `</N>` ends it; `<!--<script>` hides `</N>`",
    "special": "`</xwrap>` is ignored with N open inside the xwrap: the marker's ancestry is unchanged",
    "formatting": "`<div><N id=x>t</div>u` reopens a clone of N around `u`",
    "scope": "`</div>` is ignored with N open inside the div",
    "list_item_scope": "`</li>` is ignored with N open inside the li",
    "button_scope": "`</p>` is ignored with N open inside the p",
    "table_scope": "`</td>` is ignored with N open inside the cell",
    "select_scope": "`</select>` is ignored with N open inside the select",
    "implied_end": "`<rb>` closes N inside a ruby",
    "implied_end_thorough": "not observable: `</template>` pops through the extra names either way",
    "special_aaa": "`<b id=y><N id=x>t</b>u` moves N out of the b (N is the furthest block)",
    "scope_start": "`<button id=d><N>t<button id=y>` leaves y inside d",
    "button_scope_start": "`<p id=d><N>t<div id=y>` leaves y inside d",
}

MARK = "Z"  # the id of the marker element the end-tag probes compare


# ---------------------------------------------------------------------------------- the browser's tree
class El:
    """An element (or a comment, named `#c`) of the browser's tree, with the parent link the probes read."""
    __slots__ = ("name", "ns", "attrs", "kids", "parent")

    def __init__(self, name: str, ns: Any, attrs: dict[str, str], parent: Optional["El"]):
        self.name, self.ns, self.attrs, self.kids, self.parent = name, ns, attrs, [], parent

    @property
    def id(self) -> Optional[str]:
        return self.attrs.get("id")

    def text(self) -> str:
        return "".join(k for k in self.kids if isinstance(k, str))

    def ancestors(self) -> list["El"]:
        out, p = [], self.parent
        while p is not None and p.name != "#d":
            out.append(p)
            p = p.parent
        return out

    def next_sibling(self) -> Any:
        sibs = self.parent.kids if self.parent else []
        i = next(i for i, k in enumerate(sibs) if k is self)
        return sibs[i + 1] if i + 1 < len(sibs) else None


def load(tree: Any) -> El:
    """A tree in `BrowserOracle`'s format as linked `El`s. An HTML `<template>`'s content is read as its
    children, which is where the parser put what was written inside it."""
    def add(parent: El, kid: Any) -> None:
        if isinstance(kid, str):
            parent.kids.append(kid)
        elif kid[0] == "#d":  # template content
            for k in kid[1]:
                add(parent, k)
        elif kid[0] == "#c":
            parent.kids.append(El("#c", 0, {}, parent))
        elif isinstance(kid[0], str) and not kid[0].startswith("#"):
            flat = kid[2]
            el = El(kid[0], kid[1], dict(zip(flat[0::2], flat[1::2])), parent)
            parent.kids.append(el)
            for k in kid[3]:
                add(el, k)

    root = El("#d", 0, {}, None)
    for k in tree[1]:
        add(root, k)
    return root


def walk(root: El) -> Iterator[El]:
    for k in root.kids:
        if isinstance(k, El):
            yield k
            yield from walk(k)


def html_elements(root: El, name: str, ident: str) -> list[El]:
    """The HTML elements called `name` with `id=ident`, in document order."""
    return [e for e in walk(root) if e.name == name and e.ns == 0 and e.id == ident]


def first(root: El, name: str, ident: str = "x") -> Optional[El]:
    found = html_elements(root, name, ident)
    return found[0] if found else None


def by_id(root: El, ident: str) -> Optional[El]:
    return next((e for e in walk(root) if e.id == ident), None)


def chain(el: El) -> tuple:
    return tuple((a.name, a.id) for a in el.ancestors())


# ------------------------------------------------------------------------------------- probe documents
DOCTYPE = "<!DOCTYPE html>"
# Where `<N id=x>` can be written for the element to exist, in the order tried; the first hit wins. The
# last is the bare document, which is what lets `html`, `head` and `body` be placed at all.
CONTEXTS = ("<body>", "<body><table>", "<frameset>", "")
# An end-tag probe runs in no-quirks mode, and again in quirks mode: there `<table>` does not close an
# open `<p>`, which makes `table` readable for button scope.
PREFIXES = (DOCTYPE, "")


def place_doc(n: str, ctx: str) -> str:
    return f"{DOCTYPE}{ctx}<{n} id=x> "


def mode_docs(n: str, ctx: str) -> tuple[str, str]:
    return (f"{DOCTYPE}{ctx}<{n} id=x><zq id=m>t&amp;u</{n}><zr id=r>",
            f"{DOCTYPE}{ctx}<{n} id=x><!--<script></{n}><zr id=r>")


def formatting_doc(n: str) -> str:
    return f"{DOCTYPE}<body><div id=d><{n} id=x>t</div>u"


# The wrapper each end-tag probe opens, and the end tag it sends.
END_TAG_SHAPES = {
    "special": ("<xwrap id=d>", "</xwrap>"),
    "scope": ("<div id=d>", "</div>"),
    "list_item_scope": ("<li id=d>", "</li>"),
    "button_scope": ("<p id=d>", "</p>"),
    "table_scope": ("<table><tr><td id=d>", "</td>"),
    "select_scope": ("<select id=d>", "</select>"),
}


def end_tag_docs(cat: str, n: str, prefix: str) -> tuple[str, str]:
    """The same document with and without the end tag, ending in a marker element."""
    wrap, end = END_TAG_SHAPES[cat]
    head = f"{prefix}<body>{wrap}<{n} id=x>t"
    marker = f"<zmark id={MARK}>"
    return head + end + marker, head + marker


def implied_docs(n: str) -> tuple[str, str]:
    head = f"{DOCTYPE}<body><ruby id=d><{n} id=x>t"
    return head + "<rb id=y>", head + "<span id=y>"


def aaa_docs(n: str) -> tuple[str, str]:
    head = f"{DOCTYPE}<body><b id=y><{n} id=x>t"
    return head + "</b>u", head


def start_docs(kind: str, n: str) -> str:
    wrap, closer = {"scope_start": ("<button id=d>", "<button id=y>"),
                    "button_scope_start": ("<p id=d>", "<div id=y>")}[kind]
    return f"{DOCTYPE}<body>{wrap}<{n} id=x>t{closer}"


def probe_documents(n: str) -> list[str]:
    """Every document the probes send for one name, in a fixed order (the fingerprint hashes them)."""
    docs = [place_doc(n, c) for c in CONTEXTS]
    for c in CONTEXTS:
        docs += list(mode_docs(n, c))
    docs.append(formatting_doc(n))
    for cat in END_TAG_SHAPES:
        for prefix in PREFIXES:
            docs += list(end_tag_docs(cat, n, prefix))
    docs += list(implied_docs(n)) + list(aaa_docs(n))
    docs += [start_docs("scope_start", n), start_docs("button_scope_start", n)]
    return docs


def fingerprint() -> str:
    h = hashlib.sha256()
    for n in PROBE_NAMES:
        for d in probe_documents(n):
            h.update(d.encode() + b"\0")
    return h.hexdigest()


# ------------------------------------------------------------------------------------------ the probes
@dataclass
class Answer:
    value: Any   # True / False / a mode name, or None when no probe can read the cell
    why: str = ""  # for None: what could not be observed


@dataclass
class Derivation:
    """One browser's answers: `answers[category][name]` is True, False or None (unreadable)."""
    build: str
    answers: dict[str, dict[str, Optional[bool]]] = field(default_factory=dict)
    modes: dict[str, Optional[str]] = field(default_factory=dict)
    why: dict[str, dict[str, str]] = field(default_factory=dict)
    ctx: dict[str, Optional[str]] = field(default_factory=dict)
    selfcheck: tuple[int, int] = (0, 0)  # (documents page-loaded, documents whose tree differed)

    def put(self, cat: str, n: str, a: Answer) -> None:
        self.answers.setdefault(cat, {})[n] = a.value
        if a.value is None:
            self.why.setdefault(cat, {})[n] = a.why

    def to_json(self) -> dict:
        return {"build": self.build, "selfcheck": list(self.selfcheck), "answers": self.answers,
                "modes": self.modes, "why": self.why, "ctx": self.ctx}

    @staticmethod
    def from_json(d: dict) -> "Derivation":
        return Derivation(d["build"], d["answers"], d["modes"], d["why"], d["ctx"], tuple(d["selfcheck"]))


# Why a cell is unreadable. The code is what is stored; the text is what `--report` and the generated file's
# header print.
WHY = {
    "foreign": "no HTML element of that name is created (the parser renames it or makes a foreign element)",
    "void": "popped on insertion, so never on the stack of open elements",
    "text": "the tokenizer makes no end or start tag token inside a text-mode element",
    "container": "cannot sit directly in the probe's wrapper (it needs a container, or its start tag "
                 "closes or moves the wrapper)",
    "boundary": "a default-scope boundary, so the b or ruby around it is out of scope and the tag under "
                "test does nothing",
    "popped": "`</div>` does not pop it (a scope boundary), so nothing is left to reconstruct",
    "thorough": "`</template>` pops through it either way, so no tree shows it",
    "entities": "text mode that neither decodes nor keeps `&amp;`",
}
NOT_CREATED, NOT_DIRECT = "foreign", "container"


def read_place(n: str, trees: dict[str, El]) -> tuple[Optional[str], Answer]:
    """The first context in which `<N id=x>` exists as an HTML element, and whether N is void there."""
    for c in CONTEXTS:
        el = first(trees[place_doc(n, c)], n)
        if el is not None:
            return c, Answer(not el.kids and el.next_sibling() == " ")
    return None, Answer(None, NOT_CREATED)


def read_mode(n: str, ctx: Optional[str], trees: dict[str, El]) -> Answer:
    if ctx is None:
        return Answer(None, NOT_CREATED)
    d1, d2 = mode_docs(n, ctx)
    x = first(trees[d1], n)
    if x is None:
        return Answer(None, NOT_CREATED)
    t1 = x.text()
    if "<zq" not in t1:
        return Answer("normal")
    if f"</{n}>" in t1:
        return Answer("plaintext")
    x2 = first(trees[d2], n)
    if x2 is not None and f"</{n}>" in x2.text():
        return Answer("script")
    if "t&u" in t1:
        return Answer("rcdata")
    return Answer("rawtext") if "t&amp;u" in t1 else Answer(None, "entities")


def stack_reason(void: Answer, mode: Answer) -> Optional[str]:
    """Why a probe cannot read N, or None if it can: N has to be an element that sits on the stack of
    open elements, and one whose end and start tags are tokenized as tags."""
    if void.value is None:
        return NOT_CREATED
    if void.value:
        return "void"
    if mode.value != "normal":
        return "text"
    return None


def read_end_tag(cat: str, n: str, trees: dict[str, El], reason: Optional[str]) -> Answer:
    """Is the end tag ignored with N open inside the wrapper? Equal marker ancestry with and without it
    means the end tag changed nothing."""
    if reason:
        return Answer(None, reason)
    for prefix in PREFIXES:
        with_end, without = end_tag_docs(cat, n, prefix)
        x, wrapper = first(trees[without], n), by_id(trees[without], "d")
        if x is None or wrapper is None or x.parent is not wrapper:
            continue
        zw, zo = first(trees[with_end], "zmark", MARK), first(trees[without], "zmark", MARK)
        if zw is None or zo is None:
            continue
        return Answer(chain(zw) == chain(zo))
    return Answer(None, NOT_DIRECT)


def read_formatting(n: str, trees: dict[str, El], reason: Optional[str]) -> Answer:
    if reason:
        return Answer(None, reason)
    root = trees[formatting_doc(n)]
    found = html_elements(root, n, "x")
    d = by_id(root, "d")
    if not found or d is None or found[0].parent is not d:
        return Answer(None, NOT_DIRECT)
    if "u" in found[0].text():
        return Answer(None, "popped")
    return Answer(len(found) >= 2)


def read_implied(n: str, trees: dict[str, El], reason: Optional[str]) -> Answer:
    if reason:
        return Answer(None, reason)
    closer, control = implied_docs(n)
    cx, cy = first(trees[control], n), first(trees[control], "span", "y")
    if cx is None or cy is None or cx.parent is None or cx.parent.name != "ruby":
        return Answer(None, NOT_DIRECT)
    if cx not in cy.ancestors():
        return Answer(None, NOT_DIRECT)
    x, y = first(trees[closer], n), first(trees[closer], "rb", "y")
    if x is None or y is None:
        return Answer(None, "container")
    return Answer(x not in y.ancestors())


def read_aaa(n: str, trees: dict[str, El], reason: Optional[str]) -> Answer:
    """Special iff the adoption agency moves N out of the `<b>`: N is then the furthest block."""
    if reason:
        return Answer(None, reason)
    probe, control = aaa_docs(n)
    cx, cy = first(trees[control], n), first(trees[control], "b", "y")
    if cx is None or cy is None or cx.parent is not cy:
        return Answer(None, NOT_DIRECT)
    x, y = first(trees[probe], n), first(trees[probe], "b", "y")
    if x is None or y is None:
        return Answer(None, "container")
    return Answer(y not in x.ancestors())


def read_start(kind: str, n: str, trees: dict[str, El], reason: Optional[str]) -> Answer:
    """Boundary iff the start tag that would close the wrapper (a second `<button>`, or a block start tag
    for an open `<p>`) lands inside it instead."""
    if reason:
        return Answer(None, reason)
    root = trees[start_docs(kind, n)]
    x, y, d = first(root, n), by_id(root, "y"), by_id(root, "d")
    if x is None or y is None or d is None or x.parent is not d:
        return Answer(None, NOT_DIRECT)
    return Answer(d in y.ancestors())


def derive(oracle: Any, *, selfcheck: bool = False) -> Derivation:
    """Ask the browser every probe over the whole universe and read the answers."""
    docs = sorted({d for n in PROBE_NAMES for d in probe_documents(n)})
    trees = dict(zip(docs, map(load, oracle.trees(docs, selfcheck=selfcheck))))
    if oracle.errors:
        raise SystemExit(f"{oracle.browser}: {len(oracle.errors)} documents had no tree, e.g. {oracle.errors[0]}")
    out = Derivation(oracle.build or oracle.browser)
    if selfcheck:
        out.selfcheck = (oracle.selfcheck_checked, len(oracle.selfcheck_differ))

    void: dict[str, Answer] = {}
    mode: dict[str, Answer] = {}
    for n in PROBE_NAMES:
        out.ctx[n], void[n] = read_place(n, trees)
        mode[n] = read_mode(n, out.ctx[n], trees)
        out.put("void", n, void[n])
        out.modes[n] = mode[n].value
        if mode[n].value is None:
            out.why.setdefault("mode", {})[n] = mode[n].why
    for n in PROBE_NAMES:
        r = stack_reason(void[n], mode[n])
        for cat in END_TAG_SHAPES:
            out.put(cat, n, read_end_tag(cat, n, trees, r))
        out.put("formatting", n, read_formatting(n, trees, r))
        out.put("scope_start", n, read_start("scope_start", n, trees, r))
        out.put("button_scope_start", n, read_start("button_scope_start", n, trees, r))
    # `</b>` and `<rb>` reach an element only through a scope check (`b in scope`, `ruby in scope`), so a
    # default-scope boundary hides whether it is special or an implied-end element. The scope answers are
    # therefore read first.
    boundary = resolve(out, "scope")
    for n in PROBE_NAMES:
        r = stack_reason(void[n], mode[n])
        hidden = "boundary"
        out.put("special_aaa", n, read_aaa(n, trees, r or (hidden if n in boundary else None)))
        out.put("implied_end", n, read_implied(n, trees, r or (hidden if n in boundary else None)))
    # No tree separates "thorough" from the plain set, since `</template>` pops through either way: the
    # plain set's answers carry over and the spec's eight extra names are generated from the spec.
    extras = SPEC["implied_end_thorough"] - SPEC["implied_end"]
    for n in PROBE_NAMES:
        out.put("implied_end_thorough", n,
                Answer(None, "thorough") if n in extras
                else Answer(out.answers["implied_end"][n], out.why.get("implied_end", {}).get(n, "")))
    return out


def resolve(d: Derivation, cat: str) -> set[str]:
    """The generated member set: the probe's answer, or the spec's where the probe cannot read the name."""
    return {n for n in PROBE_NAMES
            if (d.answers[cat][n] if d.answers[cat][n] is not None else n in SPEC[cat])}


def resolve_modes(d: Derivation) -> dict[str, str]:
    return {n: (m if m is not None else SPEC_MODES.get(n, "normal")) for n, m in d.modes.items()}


# ------------------------------------------------------------------------------------------- controls
# Names whose answer is known independently of the derivation. Every category must read both ways, and
# read these as expected, or its probe is not discriminating: a probe that answers the same for the whole
# universe measures nothing. A control that reads None fails too. `select_scope` has no independent source
# (the spec dropped the list), so its controls are the default scope's members and non-members, which is
# what `</select>` is checked against.
CONTROLS: dict[str, tuple[set[str], set[str]]] = {
    "void": ({"br", "img", "input", "hr", "meta", "col", "frame"}, {"div", "span", "p", "a", "table", "script"}),
    "special": ({"div", "p", "li", "table", "ul", "h1", "button", "details"}, {"span", "a", "b", "i", "font", "em"}),
    "formatting": ({"a", "b", "i", "em", "strong", "u", "code"}, {"span", "div", "p", "button", "li"}),
    "scope": ({"table", "object", "marquee", "applet", "template"}, {"div", "span", "p", "b", "ul", "li", "button"}),
    "list_item_scope": ({"ul", "ol", "table", "object"}, {"div", "span", "p", "b", "button"}),
    "button_scope": ({"button", "object", "marquee", "applet", "template"}, {"span", "b", "a", "em", "code"}),
    "table_scope": ({"table", "template"}, {"div", "span", "b", "p", "object", "button"}),
    "select_scope": ({"table", "object"}, {"option", "optgroup", "div", "span", "b"}),
    "implied_end": ({"p", "li", "dd", "dt", "option", "rb", "rt"}, {"div", "span", "b", "a", "ul"}),
}
MODE_CONTROLS = {"title": "rcdata", "textarea": "rcdata", "style": "rawtext", "xmp": "rawtext",
                 "iframe": "rawtext", "noembed": "rawtext", "noscript": "rawtext", "script": "script",
                 "plaintext": "plaintext", "div": "normal", "span": "normal", "select": "normal"}


def control_failures(d: Derivation) -> list[str]:
    bad = []
    for cat, (pos, neg) in CONTROLS.items():
        for n in sorted(pos):
            if d.answers[cat][n] is not True:
                bad.append(f"{cat}: control {n} should read True, read {d.answers[cat][n]}")
        for n in sorted(neg):
            if d.answers[cat][n] is not False:
                bad.append(f"{cat}: control {n} should read False, read {d.answers[cat][n]}")
        seen = {v for v in d.answers[cat].values() if v is not None}
        if seen != {True, False}:
            bad.append(f"{cat}: the probe answered only {sorted(map(str, seen))} over the whole universe")
    for n, want in MODE_CONTROLS.items():
        if d.modes[n] != want:
            bad.append(f"mode: control {n} should read {want}, read {d.modes[n]}")
    # the lists extend one another by definition, so a probe that breaks the nesting is reading something else
    for inner, outer in (("scope", "list_item_scope"), ("scope", "button_scope"),
                         ("implied_end", "implied_end_thorough")):
        lost = resolve(d, inner) - resolve(d, outer)
        if lost:
            bad.append(f"{outer} does not contain {inner}: missing {sorted(lost)}")
    return bad


def witness_disagreements(d: Derivation) -> list[str]:
    """Names where a category's probe and its witness both read a value and differ."""
    out = []
    for cat, wit in WITNESS.items():
        for n in PROBE_NAMES:
            a, b = d.answers[cat][n], d.answers[wit][n]
            if a is not None and b is not None and a != b:
                out.append(f"{cat}: {n} reads {a} by `{PROBES[cat]}` but {b} by `{PROBES[wit]}`")
    return out


# ------------------------------------------------------------------------------------------ rendering
def _arm(names: list[str], body: str) -> list[str]:
    return G._wrap_arm(" | ".join(f'b"{n}"' for n in names), body)


def _comment(text: str, prefix: str = "//!     ") -> list[str]:
    return [prefix + line for line in G._wrap_words(text, 100 - len(prefix))]


def render_rust(d: Derivation) -> str:
    members = {c: resolve(d, c) for c in CATEGORIES}
    mask = {n: sum(1 << i for i, c in enumerate(CATEGORIES) if n in members[c]) for n in PROBE_NAMES}
    default = mask[UNKNOWN]
    modes = resolve_modes(d)

    def flags(m: int) -> str:
        return " | ".join(FLAG[c] for i, c in enumerate(CATEGORIES) if m >> i & 1) or "0"

    out = [
        "//! GENERATED by `tools/gen_html5_rules.py` from Chrome - do not edit by hand.",
        "//!",
        f"//! Measured with {d.build} over the element universe in `gen_tree_rules.ELEMENTS`. The stored",
        "//! answers are `tools/browser_oracle/html5_derivation.json`. `--check` fails if this file is not what",
        "//! they render to, `--check --live` if the browser now answers differently, and `--write`",
        "//! regenerates both. To change a category, change the probe, never this file.",
        "//!",
        "//! Each category is the FULL set: the scope flags extend the default scope, so `applet` carries",
        "//! SCOPE, LIST_ITEM_SCOPE and BUTTON_SCOPE. HTML namespace only. SELECT_SCOPE is the boundary set",
        "//! of the lookup `</select>` makes in this browser: the current WHATWG text has no select scope.",
        "//! An element outside the universe is an ordinary element and gets the catch-all arm.",
        "//!",
        "//! A cell no probe can read is generated from the WHATWG list instead. Reasons:",
    ]
    groups: dict[tuple[str, tuple[str, ...]], list[str]] = {}
    for c in CATEGORIES + ("mode",):
        by_code: dict[str, list[str]] = {}
        for n in PROBE_NAMES:
            if (d.modes[n] if c == "mode" else d.answers[c][n]) is None:
                by_code.setdefault(d.why[c][n], []).append(n)
        for code, names in by_code.items():
            groups.setdefault((code, tuple(names)), []).append("data mode" if c == "mode" else c)
    for code in WHY:
        if any(k[0] == code for k in groups):
            out += _comment(f"{code}: {WHY[code]}", "//!   ")
    out += ["//!", "//! Names per reason, then the categories in which they could not be read:"]
    for (code, names), cats in sorted(groups.items()):
        out += _comment(f"{code}: {' '.join(names)}", "//!   ")
        out += _comment(f"in {', '.join(cats)}", "//!       ")
    out += ["", "use crate::tokenizer::DataMode;", ""]
    out += [f"pub(crate) const {FLAG[c]}: u16 = 1 << {i};" for i, c in enumerate(CATEGORIES)]
    out += ["", "/// The category flags of an HTML-namespace element name (ASCII lowercase), OR-ed together.",
            "pub(crate) fn html5_cat(name: &[u8]) -> u16 {", "    match name {"]
    by_mask: dict[int, list[str]] = {}
    for n in ELEMENTS:
        if mask[n] != default:
            by_mask.setdefault(mask[n], []).append(n)
    for m in sorted(by_mask, key=lambda m: (bin(m).count("1"), m)):
        out += _arm(sorted(by_mask[m]), f"{flags(m)},")
    out += [f"        _ => {flags(default)},", "    }", "}", "",
            "/// How the tokenizer reads the content of an HTML-namespace element, with scripting on.",
            "/// `script` is raw text here; its escape states are the tokenizer's, keyed on the name.",
            "pub(crate) fn html5_data_mode(name: &[u8]) -> DataMode {", "    match name {"]
    for arm, kinds in (("Rawtext", ("rawtext", "script")), ("Rcdata", ("rcdata",)), ("Plaintext", ("plaintext",))):
        names = sorted(n for n in ELEMENTS if modes[n] in kinds)
        if names:
            out += _arm(names, f"DataMode::{arm},")
    out += ["        _ => DataMode::Normal,", "    }", "}"]
    return "\n".join(out) + "\n"


# ----------------------------------------------------------------------------------- stored derivation
def snapshot_json(browsers: dict[str, Derivation]) -> str:
    """One line per section, so a diff of the stored answers is readable."""
    head = {"schema": SCHEMA, "fingerprint": fingerprint(), "universe": ELEMENTS}
    lines = ["{"]
    for k, v in head.items():
        lines.append(f' "{k}": {json.dumps(v, separators=(",", ":"))},')
    lines.append(' "browsers": {')
    items = list(browsers.items())
    for i, (name, d) in enumerate(items):
        lines.append(f'  "{name}": {{')
        body = d.to_json()
        keys = list(body)
        for j, k in enumerate(keys):
            lines.append(f'   "{k}": {json.dumps(body[k], separators=(",", ":"))}{"," if j < len(keys) - 1 else ""}')
        lines.append("  }" + ("," if i < len(items) - 1 else ""))
    lines += [" }", "}"]
    return "\n".join(lines) + "\n"


def load_snapshot(path: str = SNAPSHOT) -> dict:
    with open(path) as f:
        return json.load(f)


def check_generated(snapshot: dict, rust: str) -> list[str]:
    """Why the Rust file cannot be trusted, offline: stale probes, a failed control, or a file that is not
    what the stored answers render to."""
    bad = []
    if snapshot.get("schema") != SCHEMA:
        return [f"stored derivation has schema {snapshot.get('schema')}, the tool writes {SCHEMA}"]
    if snapshot["universe"] != ELEMENTS:
        bad.append("the element universe changed since the derivation was measured: run --write")
    if snapshot["fingerprint"] != fingerprint():
        bad.append("the probe documents changed since the derivation was measured: run --write")
    chrome = Derivation.from_json(snapshot["browsers"]["chrome"])
    bad += control_failures(chrome)
    bad += witness_disagreements(chrome)
    want = render_rust(chrome)
    if rust.strip() != want.strip():
        diff = difflib.unified_diff(rust.splitlines(), want.splitlines(), "generated_html5.rs",
                                    "rendered from the stored answers", lineterm="", n=1)
        bad.append("the Rust file is not what the stored answers render to:\n  " + "\n  ".join(list(diff)[:40]))
    return bad


def compare_live(stored: Derivation, live: Derivation) -> list[str]:
    """Every cell where the browser's answer now differs from the stored one."""
    bad = []
    for cat in sorted(set(stored.answers) | set(live.answers)):
        for n in PROBE_NAMES:
            a, b = stored.answers.get(cat, {}).get(n), live.answers.get(cat, {}).get(n)
            if a != b:
                bad.append(f"{cat}: {n} was {a}, now reads {b}")
    for n in PROBE_NAMES:
        if stored.modes.get(n) != live.modes.get(n):
            bad.append(f"mode: {n} was {stored.modes.get(n)}, now reads {live.modes.get(n)}")
    return bad


# ----------------------------------------------------------------------------------------------- report
def report(chrome: Derivation, firefox: Optional[Derivation]) -> None:
    ff = f"; {firefox.build}" if firefox else "; Firefox not measured"
    print(f"derived from {chrome.build}{ff}; {len(PROBE_NAMES)} names ({len(ELEMENTS)} + 1 unrecognized)")
    for label, d in (("chrome", chrome), ("firefox", firefox)):
        if d and d.selfcheck[0]:
            print(f"{label} self-check: {d.selfcheck[0]} documents page-loaded too, {d.selfcheck[1]} differed")
    for cat in CATEGORIES:
        got, spec = resolve(chrome, cat), SPEC[cat]
        read = sum(1 for v in chrome.answers[cat].values() if v is not None)
        print(f"\n== {cat}: {len(got)} members ({read} of {len(PROBE_NAMES)} names read by the probe)")
        print(f"   probe: {PROBES[cat]}")
        print(f"   vs spec: +{sorted(got - spec)} -{sorted(spec - got)}")
        if cat == "select_scope":
            print(f"   vs the earlier spec's select scope (all but option and optgroup): "
                  f"+{sorted(got - OLD_SELECT_SCOPE)} -{len(OLD_SELECT_SCOPE - got)} names")
        why: dict[str, list[str]] = {}
        for n, v in chrome.answers[cat].items():
            if v is None:
                why.setdefault(chrome.why[cat][n], []).append(n)
        for reason, names in why.items():
            print(f"   unread, from the spec - {WHY[reason]}: {' '.join(names)}")
        if cat in WITNESS:
            wit = WITNESS[cat]
            both = sum(1 for n in PROBE_NAMES if chrome.answers[cat][n] is not None
                       and chrome.answers[wit][n] is not None)
            print(f"   witness {PROBES[wit]}: read {both} of the same names, "
                  f"{len([w for w in witness_disagreements(chrome) if w.startswith(cat + ':')])} disagree")
        if firefox:
            diff = [(n, chrome.answers[cat][n], firefox.answers[cat][n]) for n in PROBE_NAMES
                    if chrome.answers[cat][n] != firefox.answers[cat][n]]
            print(f"   firefox differs: {diff if diff else 'nowhere'}")
    print("\n== data mode")
    modes, spec = resolve_modes(chrome), SPEC_MODES
    print(f"   probe: {PROBES['mode']}")
    for kind in ("rcdata", "rawtext", "script", "plaintext"):
        print(f"   {kind:<9}: {' '.join(n for n in PROBE_NAMES if modes[n] == kind)}")
    wrong = {n: (modes[n], spec.get(n, "normal")) for n in PROBE_NAMES if modes[n] != spec.get(n, "normal")}
    print(f"   vs spec: {wrong if wrong else 'no difference'}")
    unread = [n for n in PROBE_NAMES if chrome.modes[n] is None]
    print(f"   unread, from the spec - {WHY[NOT_CREATED]}: {' '.join(unread)}")
    if firefox:
        diff = [(n, chrome.modes[n], firefox.modes[n]) for n in PROBE_NAMES if chrome.modes[n] != firefox.modes[n]]
        print(f"   firefox differs: {diff if diff else 'nowhere'}")
    print("\ncontrols:", control_failures(chrome) or "every control reads as expected")
    if firefox:
        print("firefox controls:", control_failures(firefox) or "every control reads as expected")


# ------------------------------------------------------------------------------------------------ main
def measure(browsers: list[str], *, selfcheck: bool) -> dict[str, Derivation]:
    from browser_oracle.client import BrowserOracle

    out = {}
    for name in browsers:
        with BrowserOracle(name) as oracle:
            out[name] = derive(oracle, selfcheck=selfcheck)
    return out


def write(browsers: list[str]) -> int:
    """Re-derive from `browsers` and rewrite the stored answers and the Rust file, unless the derivation is
    not trustworthy."""
    got = measure(browsers, selfcheck=True)
    if "chrome" not in got:
        raise SystemExit("--write needs chrome: it is the oracle the file is generated from")
    failures = control_failures(got["chrome"]) + witness_disagreements(got["chrome"])
    if got["chrome"].selfcheck[1]:
        failures.append(f"{got['chrome'].selfcheck[1]} documents build a different tree by page load than by DOMParser")
    if failures:
        print("REFUSING to write: the derivation is not trustworthy\n  " + "\n  ".join(failures))
        return 1
    with open(SNAPSHOT, "w") as f:
        f.write(snapshot_json(got))
    with open(RUST_SRC, "w") as f:
        f.write(render_rust(got["chrome"]))
    print(f"wrote {os.path.relpath(SNAPSHOT, ROOT)} and {os.path.relpath(RUST_SRC, ROOT)} from {got['chrome'].build}")
    return 0


def check(snap: dict, stored: dict[str, Derivation], live: bool) -> int:
    """The gate: the Rust file is what the stored answers render to, and with `live` the browser still gives them."""
    bad = check_generated(snap, open(RUST_SRC).read())
    if live:
        now = measure(["chrome"], selfcheck=False)["chrome"]
        bad += compare_live(stored["chrome"], now)
        note = f" (live {now.build}, stored {stored['chrome'].build})"
    else:
        note = f" (stored answers from {stored['chrome'].build}; --live re-measures)"
    if bad:
        print("GATE: generated_html5.rs is NOT what the browser says -> FAIL\n  " + "\n  ".join(bad))
        return 1
    print(f"GATE: {os.path.relpath(RUST_SRC, ROOT)} matches the derivation over {len(PROBE_NAMES)} names -> PASS{note}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="exit nonzero if the Rust file has drifted")
    ap.add_argument("--write", action="store_true", help="re-derive from the browsers, rewrite both files")
    ap.add_argument("--print", dest="show", action="store_true", help="print the rendered Rust")
    ap.add_argument("--render", action="store_true",
                    help="rewrite the Rust file from the stored answers, with no browser (after editing the renderer)")
    ap.add_argument("--report", action="store_true", help="derived sets vs the spec, Chrome vs Firefox")
    ap.add_argument("--live", action="store_true", help="measure in the browser instead of reading the stored answers")
    ap.add_argument("--browsers", default="chrome,firefox", help="for --write and --report --live")
    args = ap.parse_args()

    if args.write:
        return write(args.browsers.split(","))
    snap = load_snapshot()
    stored = {k: Derivation.from_json(v) for k, v in snap["browsers"].items()}
    if args.report:
        live = measure(args.browsers.split(","), selfcheck=False) if args.live else stored
        report(live["chrome"], live.get("firefox"))
    elif args.show:
        print(render_rust(stored["chrome"]), end="")
    elif args.render:
        with open(RUST_SRC, "w") as f:
            f.write(render_rust(stored["chrome"]))
        print(f"rendered {os.path.relpath(RUST_SRC, ROOT)} from the stored answers ({stored['chrome'].build})")
    elif args.check:
        return check(snap, stored, args.live)
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
