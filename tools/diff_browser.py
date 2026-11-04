"""
tools/diff_browser.py: the browser differential, Frostwork against the tree a real browser builds.

The oracle for browser mode is the browser (docs/BROWSER_MODE.md, section 5), driven through
`tools/browser_oracle/client.py`. Each document is graded on `seq_sweep`'s whole-tree fingerprint: the
engine answers the fingerprint's XPath columns, and `tools/browser_oracle/tree.py` computes the same
columns natively from the browser's own JSON tree, so nothing is rebuilt in lxml and nothing the
browser keeps is lost.

`browser_docs.py` generates the documents, each element token carrying a unique id so the fingerprint can
see it: every `seq_sweep` sequence up to `--seq-depth`, `--random` seeded sequences over an HTML5-heavy
alphabet, and `--values` decorated sequences for the value columns, plus the deferred-tail and structured
families. The run refuses to grade unless every token was emitted.

Every differing column is REDUCED to the smallest token subsequence that still differs on that column (ids
are kept, so the column still names the same element), and the reduced document, not the page it came from,
is attributed to a row of CONSTRUCTS: a section 3 construct browser mode does not build as the browser does.
Page-scoped attribution over-credits (AGENTS.md, the truncated-tag lesson); a two-token repro has nowhere to
hide. Anything no construct explains is NOVEL, and today that is every tree divergence.

VALUE COLUMNS grade what a selector returns: conformant.py's basket, RULE_SELECTORS (case folding, class
lists, NUL, entities, control characters, deferred winners whose value is inside them) and per-id
`::text`/`::attr`/descendant-text columns. CSS is answered natively in the browser
(`browser_oracle/css.py`), XPath through the lossless lxml bridge (`browser_oracle/bridge.py`), both on
the tree the fingerprint was read from. A column that differs where
the fingerprint already differs is filed under the TREE, which that gate holds. Where the fingerprint
agrees, it is reduced with the fingerprint still agreeing at every step and attributed to VALUE_CONSTRUCTS;
if its column names an id-less element the fingerprint has no column for, to the section 3 construct the
repro exhibits (a tree difference only the value column could see); anything else is value NOVEL.

`--gate` fails on NOVEL > 0 and value NOVEL > 0, and on coverage: no document graded, a token never
emitted, an oracle error (a failed page load), a self-check where `DOMParser` and a page load built
different trees or CSS answers for a document the fast path was trusted with, a bridge that changed a
tree on its round trip or answers a fingerprint column differently from the native evaluator, native CSS
that answers differently from parsel's `.css()` over the bridged tree where their semantics coincide, and
a value-rule selector answered nowhere or refused by the engine.

With `--browsers chrome,firefox`, Firefox is run too. Where the two browsers agree that is the target;
where they disagree the case goes in a Chrome-versus-Firefox table of its own (also reduced), and the
engine is graded against Chrome, which browser mode follows.

Usage:
  .venv/bin/python tools/diff_browser.py                          # seq depth 2, 2000 random, 1000 value
  .venv/bin/python tools/diff_browser.py --browsers chrome,firefox --seq-depth 3 --random 20000
  .venv/bin/python tools/diff_browser.py --parser lxml --gate
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import conformant  # noqa: E402  (its selector basket)
import diff_lxml  # noqa: E402  (the engine's member budget, and what a bare-element selector is)
import seq_sweep  # noqa: E402  (the fingerprint and the short-sequence alphabet)
import webpoet_structure  # noqa: E402  (the one structural comparison for outer HTML)
from browser_docs import (  # noqa: E402
    ALPHABET, DOCTYPE, RULE_SELECTORS, VALUE_TOKENS, Doc, grouped_spec, ids_of, random_docs, redecorate, render,
    seq_docs, structured_docs, tail_docs, undecorate, value_docs, value_selectors)
from browser_oracle import bridge as Br  # noqa: E402
from browser_oracle import css as C  # noqa: E402
from browser_oracle import tree as T  # noqa: E402
from browser_oracle.client import BrowserOracle, Refused  # noqa: E402

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "browserdiff.json")
# A tree at Chrome's depth cap nests two JSON arrays per element, past Python's default recursion limit.
sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))


# ---------------------------------------------------------------- which oracle answers a column
def is_native(col: str) -> bool:
    """Is the column answered by the browser's own selector engine (CSS and grouped), not the bridge?"""
    return grouped_spec(col) is not None or C.is_css(col)


def oracle_form(col: str) -> Any:
    """The column as `BrowserOracle.query` takes it."""
    spec = grouped_spec(col)
    return (spec[0], spec[1]) if spec else col


# ---------------------------------------------------------------- the engine
MAX_SELECTORS_PER_PASS = diff_lxml.MAX_SELECTORS_PER_PASS


def engine_runner(parser: str) -> tuple[Callable[[bytes, list[str]], list[list[str]]], str]:
    import frostwork

    mode = seq_sweep.parser_kwargs(parser)

    def run(doc: bytes, sels: list[str], strict: bool = True) -> list[list[str]]:
        return frostwork.extract(doc, sels, "utf-8", strict=strict, **mode)

    def grouped(doc: bytes, cols: list[str]) -> list[Any]:
        """Each grouped column's rows: per container match, one value list per sub."""
        groups = []
        for c in cols:
            container, subs = grouped_spec(c)  # type: ignore[misc]
            groups.append((container, [(f"s{k}", s) for k, s in enumerate(subs)]))
        return frostwork.extract_grouped(doc, [], groups, "utf-8", **mode)[1]

    run.grouped = grouped  # type: ignore[attr-defined]
    if mode:
        return run, f"frostwork.extract(..., parser={parser!r})"
    note = "" if parser == "lxml" else f"; --parser {parser} was NOT applied"
    return run, f"frostwork.extract without parser= (the installed extension has no such keyword, so lxml mode{note})"


def debug_panics(differ: str, parser: str, docs: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    """The documents on which a DEBUG build of the `differ` bridge panics, with the panic.

    Its `debug_assert!`s are the builder's own invariants (an element the builder still has open is never
    closed, a live element is open or parked, a tail pass opens every recorded winner). A release build
    that breaks one returns a value that can still pass, such as a template's outer HTML cut short, which
    the raw-source allowance excuses. The bridge answers a batch at exit, so a panic shows as the line its
    output stops at; the run goes on after it in a new process."""
    import subprocess

    lines: list[tuple[int, str]] = []
    for k, (rendered, sels) in enumerate(docs):
        sels = [s for s in sels if "\t" not in s and "\n" not in s]
        for start in range(0, max(1, len(sels)), 60):   # inside the engine's member budget
            lines.append((k, "\t".join(["utf-8", rendered.encode("utf-8").hex()] + sels[start:start + 60])))
    out: list[tuple[str, str]] = []
    at = 0
    while at < len(lines):
        p = subprocess.run([differ, "--parser", parser], input="".join(ln + "\n" for _k, ln in lines[at:]),
                           capture_output=True, text=True, encoding="utf-8")
        done = p.stdout.count("\n")
        if p.returncode == 0 and at + done >= len(lines):
            break
        k = lines[at + done][0]
        err = [ln.strip() for ln in p.stderr.splitlines() if ln.strip()]
        first = next((i for i, ln in enumerate(err) if "panicked" in ln), 0)
        out.append((docs[k][0], " ".join(err[first:first + 2]) or f"exit {p.returncode}"))
        at += done
        while at < len(lines) and lines[at][0] == k:
            at += 1
    return out


# ---------------------------------------------------------------- answers, cached per document
class Answers:
    """Every source's answer to (document, column), computed on demand and cached.

    Sources are "engine" and the browser names. A browser tree that failed to load answers `None`, which
    never counts as a difference: it is an oracle error, counted separately.
    """

    def __init__(self, run_engine: Callable, oracles: dict[str, BrowserOracle]):
        self.run_engine = run_engine
        self.oracles = oracles
        self.trees: dict[str, dict[str, Any]] = {b: {} for b in oracles}
        self.indexed: dict[tuple[str, str], Any] = {}
        self.cache: dict[tuple[str, str, str], Optional[list[str]]] = {}
        self.values: dict[tuple[str, str, str], Any] = {}
        self.bridges: dict[tuple[str, str], Any] = {}
        self.oracle_seconds = 0.0
        self.oracle_docs = 0

    def fetch(self, rendered: set[str] | list[str]) -> None:
        for b, oracle in self.oracles.items():
            missing = sorted({r for r in rendered if r not in self.trees[b]})
            if not missing:
                continue
            t0 = time.perf_counter()
            for r, t in zip(missing, oracle.trees(missing)):
                self.trees[b][r] = t
            self.oracle_seconds += time.perf_counter() - t0
            self.oracle_docs += len(missing)

    def prefetch_engine(self, wanted: dict[str, set[str]]) -> None:
        """One engine pass per document for every column it is still missing."""
        for r, sels in wanted.items():
            need = sorted(s for s in sels if ("engine", r, s) not in self.cache)
            for start in range(0, len(need), MAX_SELECTORS_PER_PASS):
                chunk = need[start:start + MAX_SELECTORS_PER_PASS]
                for s, v in zip(chunk, self.run_engine(r.encode(), chunk)):
                    self.cache[("engine", r, s)] = v

    def get(self, source: str, rendered: str, sel: str) -> Optional[list[str]]:
        key = (source, rendered, sel)
        if key not in self.cache:
            if source == "engine":
                self.prefetch_engine({rendered: {sel}})
            else:
                if rendered not in self.trees[source]:
                    self.fetch([rendered])
                tree = self.trees[source][rendered]
                if tree is None:
                    self.cache[key] = None
                else:
                    ix = self.indexed.get((source, rendered))
                    if ix is None:
                        ix = self.indexed[(source, rendered)] = T.index(T.document_view(tree))
                    self.cache[key] = T.evaluate(ix, sel)
        return self.cache[key]

    def differs(self, pair: tuple[str, str], rendered: str, sel: str) -> bool:
        a, b = self.get(pair[0], rendered, sel), self.get(pair[1], rendered, sel)
        return a is not None and b is not None and a != b

    # ------------------------------------------------------------ value columns
    # A value column's oracle answer is a list of strings, or a `Refused` saying why there is none: css.py
    # could not plan it, the browser rejected it, the bridge cannot hold the document, or the tree failed
    # to load. A `Refused` is never graded and never reads as an empty column.
    def bridge(self, browser: str, rendered: str) -> Any:
        key = (browser, rendered)
        if key not in self.bridges:
            tree = self.trees[browser].get(rendered)
            try:
                self.bridges[key] = Refused("page load failed", "load") if tree is None else Br.Bridge(tree)
            except Br.Unrepresentable as e:
                self.bridges[key] = Refused(str(e), "bridge")
        return self.bridges[key]

    def fetch_values(self, wanted: dict[str, set[str]]) -> None:
        """Every engine and oracle value `wanted` names: CSS in one browser request, XPath via the bridge."""
        for b, oracle in self.oracles.items():
            need = {r: sorted(s for s in sels if is_native(s) and (b, r, s) not in self.values)
                    for r, sels in wanted.items()}
            need = {r: s for r, s in need.items() if s}
            if need:
                rs = sorted(need)
                t0 = time.perf_counter()
                trees, got = oracle.query(rs, [[oracle_form(s) for s in need[r]] for r in rs])
                self.oracle_seconds += time.perf_counter() - t0
                self.oracle_docs += len(rs)
                for r, t, row in zip(rs, trees, got):
                    self.trees[b].setdefault(r, t)
                    for k, s in enumerate(need[r]):
                        self.values[(b, r, s)] = Refused("page load failed", "load") if row is None else row[k]
            missing = sorted({r for r in wanted if r not in self.trees[b]})
            if missing:
                self.fetch(missing)
        for r, sels in wanted.items():
            need = sorted(s for s in sels if ("engine", r, s) not in self.values)
            grouped = [s for s in need if grouped_spec(s)]
            need = [s for s in need if not grouped_spec(s)]
            for start in range(0, len(need), MAX_SELECTORS_PER_PASS):
                chunk = need[start:start + MAX_SELECTORS_PER_PASS]
                for s, v in zip(chunk, self.run_engine(r.encode(), chunk, strict=False)):
                    self.values[("engine", r, s)] = v
            if grouped:
                for s, v in zip(grouped, self.run_engine.grouped(r.encode(), grouped)):
                    self.values[("engine", r, s)] = v

    def value(self, source: str, rendered: str, sel: str) -> Any:
        key = (source, rendered, sel)
        if key not in self.values:
            if source == "engine" or is_native(sel):
                self.fetch_values({rendered: {sel}})
            else:
                br = self.bridge(source, rendered)
                if isinstance(br, Refused):
                    self.values[key] = br
                else:
                    try:
                        self.values[key] = br.xpath(sel)
                    except Exception as e:  # noqa: BLE001 - lxml's refusal is the reason
                        self.values[key] = Refused(f"{type(e).__name__}: {e}", "xpath")
        return self.values[key]

    def value_differs(self, pair: tuple[str, str], rendered: str, sel: str) -> bool:
        a, b = self.value(pair[0], rendered, sel), self.value(pair[1], rendered, sel)
        return isinstance(a, list) and isinstance(b, list) and not same_values(a, b, sel)

    def fingerprint_differs(self, pair: tuple[str, str], rendered: str, ids: list[str]) -> bool:
        return any(self.differs(pair, rendered, s) for s in seq_sweep.fingerprint_selectors(ids))


def same_values(mine: list[str], theirs: list[str], sel: str) -> bool:
    """Do two value columns agree? Exactly, except outer HTML, which is compared by parsed STRUCTURE:
    the engine returns the element's raw source and the oracle a serialisation of its tree, and the
    one comparator every outer-HTML gate shares (`webpoet_structure`) says what that allowance means."""
    if mine == theirs:
        return True
    if not is_node_query(sel) or len(mine) != len(theirs):
        return False
    try:
        return all(webpoet_structure.structure_of(a) == webpoet_structure.structure_of(b)
                   for a, b in zip(mine, theirs))
    except Exception:  # noqa: BLE001 - a fragment that will not parse agrees with nothing
        return False


def is_node_query(sel: str) -> bool:
    """A bare-element selector: its value is outer HTML. `diff_lxml`'s rule, and the scalar forms this
    gate's columns add (`string()`, `count()`, `/@*`)."""
    s = sel.strip()
    return diff_lxml.is_node_query(s) and not (s.startswith(("string(", "count(")) or s.endswith("/@*"))


# ---------------------------------------------------------------- reduction to the smallest repro
@dataclass
class Divergence:
    doc: Doc                  # the generated document
    sel: str                  # the differing column
    pair: tuple[str, str]     # which two sources differ on it
    repro: Doc = ()           # the smallest token subsequence that still differs on `sel`


def reduce_all(items: list[Divergence], answers: Answers) -> None:
    """Shrink each item's document one token at a time while the column still differs (1-minimal).

    Runs in rounds over every item at once, so each round costs one oracle batch rather than one
    browser round trip per candidate.
    """
    for it in items:
        it.repro = it.doc
    active = [it for it in items if len(it.repro) > 1]
    while active:
        cands = {id(it): [it.repro[:k] + it.repro[k + 1:] for k in range(len(it.repro))] for it in active}
        rendered = {render(c) for cs in cands.values() for c in cs}
        answers.fetch(rendered)
        wanted: dict[str, set[str]] = defaultdict(set)
        for it in active:
            for c in cands[id(it)]:
                if "engine" in it.pair:
                    wanted[render(c)].add(it.sel)
        answers.prefetch_engine(wanted)
        nxt = []
        for it in active:
            for c in cands[id(it)]:
                if answers.differs(it.pair, render(c), it.sel):
                    it.repro = c
                    if len(c) > 1:
                        nxt.append(it)
                    break
        active = nxt


def reduce_values(items: list[Divergence], answers: Answers) -> None:
    """`reduce_all` for value columns, with one more condition on every step: the two sources' trees must
    still agree on the fingerprint. A value divergence is one the tree does not explain, so its repro must
    never turn into a tree divergence on the way down."""
    for it in items:
        it.repro = it.doc
    active = [it for it in items if len(it.repro) > 1]
    while active:
        cands = {id(it): [it.repro[:k] + it.repro[k + 1:] for k in range(len(it.repro))] for it in active}
        wanted: dict[str, set[str]] = defaultdict(set)
        prints: dict[str, set[str]] = defaultdict(set)
        for it in active:
            for c in cands[id(it)]:
                wanted[render(c)].add(it.sel)
                prints[render(c)].update(seq_sweep.fingerprint_selectors(ids_of(c)))
        answers.fetch_values(wanted)
        answers.prefetch_engine(prints)
        nxt = []
        for it in active:
            for c in cands[id(it)]:
                r = render(c)
                if answers.value_differs(it.pair, r, it.sel) and \
                        not answers.fingerprint_differs(it.pair, r, ids_of(c)):
                    it.repro = c
                    if len(c) > 1:
                        nxt.append(it)
                    break
        active = nxt


# ---------------------------------------------------------------- attribution
@dataclass
class Repro:
    """A reduced document as the attribution rules see it.

    `differs(doc)` asks whether another document (a counterfactual edit of this one) still differs on the
    same column, which is how the rules that are about a document-level property are decided: whether
    a doctype, an explicit `<head>` or dropping a `/>` makes the divergence go away. `browser_ids` is
    the browser's document-order id column, where a cloned formatting element shows as a repeated id.
    """

    doc: Doc
    sel: str
    differs: Callable[[Doc], bool]
    browser_ids: list[str]

    def has_doctype(self) -> bool:
        return any(t.lower().startswith("<!doctype") for _i, t in self.doc)

    def with_doctype(self) -> Doc:
        return ((-1, DOCTYPE),) + self.doc

    def with_head(self) -> Doc:
        lead = 1 if self.doc and self.doc[0][1].lower().startswith("<!doctype") else 0
        return self.doc[:lead] + ((-1, "<head></head>"),) + self.doc[lead:]

    def deslashed(self) -> Doc:
        return tuple((i, t.replace("{}/>", "{}>")) for i, t in self.doc)


@dataclass(frozen=True)
class Construct:
    name: str                       # the section 3 row it stands for
    test: Callable[[Repro], bool]   # does this smallest repro exhibit it?


# The section 3 constructs browser mode does not build as the browser does, each a rule its reduced repro
# satisfies. Empty: every section 3 row is implemented, the five retroactive rules by the replay (the header
# of `src/matcher/browser/mod.rs`), so every tree divergence is NOVEL and the gate holds each construct at
# zero. A construct that cannot be made exact gets a row here, with the reason, and a divergence entry in
# docs/COMPATIBILITY.md.
CONSTRUCTS: list[Construct] = []


def counterfactuals(r: Repro) -> list[Doc]:
    """The edited documents the counterfactual rules will ask about, so they can be fetched in one batch."""
    out = [r.with_head(), r.deslashed()]
    if not r.has_doctype():
        out.append(r.with_doctype())
    return out


def attribute(repro: Repro) -> Optional[str]:
    """The first construct whose rule the reduced document satisfies, or `None` (NOVEL)."""
    for c in CONSTRUCTS:
        if c.test(repro):
            return c.name
    return None


# ---------------------------------------------------------------- value attribution
# A value column that differs on a document whose fingerprint agrees is not explained by the tree, so the
# section 3 constructs cannot explain it either. These are the value-level ones, each a documented
# decision or divergence rather than a tree rule. They read the reduced, still-decorated document and the
# oracle's own tree, never the page it came from.
# The quirks half of HTML's "initial" insertion mode (13.2.6.4.1), for a well-formed doctype as the tree
# records it: a missing identifier is the empty string there.
QUIRKS_PUBLIC_PREFIXES = (
    "+//Silmaril//dtd html Pro v0r11 19970101//",
    "-//AS//DTD HTML 3.0 asWedit + extensions//",
    "-//AdvaSoft Ltd//DTD HTML 3.0 asWedit + extensions//",
    "-//IETF//DTD HTML 2.0 Level 1//",
    "-//IETF//DTD HTML 2.0 Level 2//",
    "-//IETF//DTD HTML 2.0 Strict Level 1//",
    "-//IETF//DTD HTML 2.0 Strict Level 2//",
    "-//IETF//DTD HTML 2.0 Strict//",
    "-//IETF//DTD HTML 2.0//",
    "-//IETF//DTD HTML 2.1E//",
    "-//IETF//DTD HTML 3.0//",
    "-//IETF//DTD HTML 3.2 Final//",
    "-//IETF//DTD HTML 3.2//",
    "-//IETF//DTD HTML 3//",
    "-//IETF//DTD HTML Level 0//",
    "-//IETF//DTD HTML Level 1//",
    "-//IETF//DTD HTML Level 2//",
    "-//IETF//DTD HTML Level 3//",
    "-//IETF//DTD HTML Strict Level 0//",
    "-//IETF//DTD HTML Strict Level 1//",
    "-//IETF//DTD HTML Strict Level 2//",
    "-//IETF//DTD HTML Strict Level 3//",
    "-//IETF//DTD HTML Strict//",
    "-//IETF//DTD HTML//",
    "-//Metrius//DTD Metrius Presentational//",
    "-//Microsoft//DTD Internet Explorer 2.0 HTML Strict//",
    "-//Microsoft//DTD Internet Explorer 2.0 HTML//",
    "-//Microsoft//DTD Internet Explorer 2.0 Tables//",
    "-//Microsoft//DTD Internet Explorer 3.0 HTML Strict//",
    "-//Microsoft//DTD Internet Explorer 3.0 HTML//",
    "-//Microsoft//DTD Internet Explorer 3.0 Tables//",
    "-//Netscape Comm. Corp.//DTD HTML//",
    "-//Netscape Comm. Corp.//DTD Strict HTML//",
    "-//O'Reilly and Associates//DTD HTML 2.0//",
    "-//O'Reilly and Associates//DTD HTML Extended 1.0//",
    "-//O'Reilly and Associates//DTD HTML Extended Relaxed 1.0//",
    "-//SQ//DTD HTML 2.0 HoTMetaL + extensions//",
    "-//SoftQuad Software//DTD HoTMetaL PRO 6.0::19990601::extensions to HTML 4.0//",
    "-//SoftQuad//DTD HoTMetaL PRO 4.0::19971010::extensions to HTML 4.0//",
    "-//Spyglass//DTD HTML 2.0 Extended//",
    "-//Sun Microsystems Corp.//DTD HotJava HTML//",
    "-//Sun Microsystems Corp.//DTD HotJava Strict HTML//",
    "-//W3C//DTD HTML 3 1995-03-24//",
    "-//W3C//DTD HTML 3.2 Draft//",
    "-//W3C//DTD HTML 3.2 Final//",
    "-//W3C//DTD HTML 3.2//",
    "-//W3C//DTD HTML 3.2S Draft//",
    "-//W3C//DTD HTML 4.0 Frameset//",
    "-//W3C//DTD HTML 4.0 Transitional//",
    "-//W3C//DTD HTML Experimental 19960712//",
    "-//W3C//DTD HTML Experimental 970421//",
    "-//W3C//DTD W3 HTML//",
    "-//W3O//DTD W3 HTML 3.0//",
    "-//WebTechs//DTD Mozilla HTML 2.0//",
    "-//WebTechs//DTD Mozilla HTML//",
)


def quirks_doctype(name: str, public: str, system: str) -> bool:
    pub, sys_ = public.lower(), system.lower()
    return (name.lower() != "html"
            or pub in ("-//w3o//dtd w3 html strict 3.0//en//", "-/w3c/dtd html 4.0 transitional/en", "html")
            or sys_ == "http://www.ibm.com/data/dtd/v11/ibmxhtml1-transitional.dtd"
            or any(pub.startswith(p.lower()) for p in QUIRKS_PUBLIC_PREFIXES)
            or (not sys_ and pub.startswith(("-//w3c//dtd html 4.01 frameset//", "-//w3c//dtd html 4.01 transitional//"))))


@dataclass
class ValueRepro:
    doc: Doc                  # the reduced document, decorated
    sel: str
    engine: list[str]
    oracle: list[str]
    tree: Any                 # the oracle's tree of `doc`

    def quirks(self) -> bool:
        """The document is in quirks mode: no doctype reached the initial insertion mode (it is the only
        place a doctype is kept, so the tree says), or the one that did names a quirks document. Every
        doctype this generator writes is well formed, so the tree's name and identifiers decide it."""
        doctype = next((n for n in self.tree[1] if isinstance(n, list) and n[0] == "#dt"), None)
        return doctype is None or quirks_doctype(doctype[1], doctype[2], doctype[3])

    def elements(self, html_only: bool = False) -> list[Any]:
        out, stack = [], [T.document_view(self.tree)]
        while stack:
            n = stack.pop()
            if isinstance(n, list) and n and n[0] == "#d":
                stack.extend(n[1])
            elif T.is_element(n):
                if not html_only or n[1] == T.XHTML:
                    out.append(n)
                stack.extend(n[3])
        return out

    def unseen_elements(self) -> list[str]:
        """Names of the oracle's elements the fingerprint cannot see, and that this value column is about.

        Every generated start tag carries an id, so an element with none was built by the tree builder.
        The fingerprint sees one only between two id'd elements, or by name when it holds id'd children or
        text (`seq_sweep.SYNTHESISED`). An EMPTY one, such as the `<p>` a stray `</p>` inserts, answers
        every fingerprint column identically, and only a value column (its outer HTML, a `:has(> p)`) can
        show it. It counts here only when the column names it: the selector says the name, or an oracle
        value holds its tag."""
        names = set()
        for el in self.elements():
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


# The attributes whose values HTML has CSS match ASCII case-insensitively on an HTML element (the HTML
# standard, "Case-sensitivity of selectors"). XPath matches every value exactly.
CASE_INSENSITIVE_ATTRS = frozenset("""accept accept-charset align alink axis bgcolor charset checked clear
codetype color compact declare defer dir direction disabled enctype face frame hreflang http-equiv lang
language link media method multiple nohref noresize noshade nowrap readonly rel rev rules scope scrolling
selected shape target text type valign valuetype vlink""".split())
_ATTR_TEST = re.compile(r"""\[\s*([^\s~|^$*!=\]]+)\s*([~|^$*]?=)\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\]\s]+)\s*\]""")
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
        for el in r.elements():
            attrs = dict(zip(el[2][0::2], el[2][1::2]))
            have = _ASCII_WS.split(attrs.get("class", "")) if kind == "." else [attrs.get("id", "")]
            if want not in have and want.lower() in (h.lower() for h in have):
                return True
    return False


@dataclass(frozen=True)
class ValueConstruct:
    name: str
    test: Callable[[ValueRepro], bool]


def _outer_root(html: str) -> Optional[tuple]:
    """The element an outer-HTML value's start tag names: its tag, its attribute names (the first of a
    repeated one, as a browser keeps) and its id. Read by a tokenizer rather than by parsing the value
    as a fragment, which loses an `<html>` (a fragment parse has nowhere to put it), and without the
    other values, whose raw spelling (`&noti=c`) Python's unescaping does not read as a browser does.
    `<image>` is the `<img>` a browser makes of it: raw source keeps the name the page wrote."""
    from html.parser import HTMLParser

    class First(HTMLParser):
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

    if not html.startswith("<"):
        return None  # a value that begins with content is not one element's source
    p = First(convert_charrefs=False)
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


# ---------------------------------------------------------------- the verdict
@dataclass
class Report:
    graded: int = 0
    missing_tokens: list[str] = field(default_factory=list)
    oracle_errors: list[dict[str, Any]] = field(default_factory=list)
    selfcheck_checked: int = 0
    selfcheck_differ: list[str] = field(default_factory=list)
    buckets: dict[str, list[Divergence]] = field(default_factory=dict)
    novel: list[Divergence] = field(default_factory=list)
    # the bridge (tools/browser_oracle/bridge.py), over every generated document's tree
    bridge_checked: int = 0
    bridge_failures: list[tuple[str, str]] = field(default_factory=list)        # (document, what changed)
    bridge_unrepresentable: list[tuple[str, str]] = field(default_factory=list)  # (document, why)
    bridge_xpath_differ: list[tuple[str, str]] = field(default_factory=list)     # (document, column)
    # value columns
    value_graded: int = 0
    missing_value_tokens: list[str] = field(default_factory=list)
    selfcheck_css_differ: list[str] = field(default_factory=list)
    rule_never_answered: list[str] = field(default_factory=list)
    rule_refused: list[str] = field(default_factory=list)   # value-rule selectors the engine refuses
    css_mirror_checked: int = 0                             # native CSS against parsel over the bridge
    css_mirror_differ: list[tuple[str, str]] = field(default_factory=list)
    value_on_tree: list[Divergence] = field(default_factory=list)
    # fingerprint agrees, but the column is about an element the fingerprint cannot see: by tree construct
    value_unseen_tree: dict[str, list[Divergence]] = field(default_factory=dict)
    value_buckets: dict[str, list[Divergence]] = field(default_factory=dict)
    value_novel: list[Divergence] = field(default_factory=list)
    # the debug build's invariants, when `--debug-differ` names one
    debug_requested: bool = False
    debug_checked: int = 0
    debug_panics: list[tuple[str, str]] = field(default_factory=list)   # (document, panic)


def gate_failures(report: Report) -> list[str]:
    """Every reason this run cannot pass. Empty means PASS.

    The whole verdict, kept as a function so `tests/test_gates.py` can seed each failure into it: a gate
    that graded nothing, or whose oracle quietly failed, reads exactly like a clean run.
    """
    fails = []
    if report.graded == 0:
        fails.append("no document was graded")
    if report.missing_tokens:
        fails.append(f"{len(report.missing_tokens)} token(s) never emitted: {report.missing_tokens}")
    if report.oracle_errors:
        fails.append(f"{len(report.oracle_errors)} oracle error(s), e.g. {report.oracle_errors[0]}")
    if report.selfcheck_checked == 0:
        fails.append("the DOMParser/page-load self-check checked no document")
    if report.selfcheck_differ:
        fails.append(f"{len(report.selfcheck_differ)} fast-path document(s) whose DOMParser tree differs "
                     f"from a page load, e.g. {report.selfcheck_differ[0]!r}")
    if report.novel:
        fails.append(f"NOVEL = {len(report.novel)}: divergences no section 3 construct explains")
    if report.bridge_checked == 0:
        fails.append("the XPath bridge round-tripped no document")
    if report.bridge_failures:
        fails.append(f"{len(report.bridge_failures)} tree(s) the bridge did not round-trip exactly, e.g. "
                     f"{report.bridge_failures[0]}")
    if report.bridge_xpath_differ:
        fails.append(f"{len(report.bridge_xpath_differ)} fingerprint column(s) where XPath through the bridge "
                     f"answers differently from the native evaluator, e.g. {report.bridge_xpath_differ[0]}")
    if report.value_graded == 0:
        fails.append("no value column was graded")
    if report.missing_value_tokens:
        fails.append(f"{len(report.missing_value_tokens)} value token(s) never emitted: "
                     f"{report.missing_value_tokens}")
    if report.selfcheck_css_differ:
        fails.append(f"{len(report.selfcheck_css_differ)} fast-path document(s) whose CSS answers differ "
                     f"from a page load's, e.g. {report.selfcheck_css_differ[0]!r}")
    if report.rule_never_answered:
        fails.append(f"{len(report.rule_never_answered)} value-rule selector(s) the oracle answered nowhere, "
                     f"so they cannot catch a lost value: {report.rule_never_answered}")
    if report.rule_refused:
        fails.append(f"{len(report.rule_refused)} value-rule selector(s) the engine refuses in this mode, so "
                     f"they grade nothing: {report.rule_refused}")
    if report.css_mirror_checked == 0:
        fails.append("native CSS was checked against parsel on no column")
    if report.css_mirror_differ:
        fails.append(f"{len(report.css_mirror_differ)} CSS column(s) where the native plan does not answer "
                     f"what parsel answers on the same tree, e.g. {report.css_mirror_differ[0]}")
    if report.value_novel:
        fails.append(f"value NOVEL = {len(report.value_novel)}: value columns that differ on a tree the "
                     f"fingerprint says is right, and that no value construct explains")
    if report.debug_requested and report.debug_checked == 0:
        fails.append("the debug build checked no document")
    if report.debug_panics:
        fails.append(f"{len(report.debug_panics)} document(s) on which the debug build panics, e.g. "
                     f"{report.debug_panics[0]}")
    return fails


def repro_of(d: Divergence, answers: Answers) -> tuple[Repro, dict[int, tuple[str, str]]]:
    """The attribution view of a reduced divergence: its ALPHABET tokens, with every counterfactual
    decorated again before the oracle is asked about it."""
    plain, decos = undecorate(d.repro)
    return (Repro(plain, d.sel, lambda doc: answers.differs(d.pair, render(redecorate(doc, decos)), d.sel),
                  answers.get(d.pair[1], render(d.repro), "//*/@id") or []), decos)


def attribute_all(items: list[Divergence], answers: Answers, report: Report) -> None:
    """File each reduced divergence under its construct, fetching every counterfactual in one batch."""
    repros = [repro_of(d, answers) for d in items]
    wanted: dict[str, set[str]] = defaultdict(set)
    for d, (r, decos) in zip(items, repros):
        for doc in counterfactuals(r):
            wanted[render(redecorate(doc, decos))].add(d.sel)
    answers.fetch(set(wanted))
    answers.prefetch_engine(wanted)
    for d, (r, _decos) in zip(items, repros):
        name = attribute(r)
        if name is None:
            report.novel.append(d)
        else:
            report.buckets.setdefault(name, []).append(d)


def attribute_values(items: list[Divergence], answers: Answers, report: Report) -> None:
    """File each reduced value divergence under a value construct; else, if the column is about an element
    the fingerprint cannot see, under the section 3 construct the reduced document exhibits (a TREE
    divergence the value column found); else NOVEL."""
    unseen: list[tuple[Divergence, Repro, dict[int, tuple[str, str]]]] = []
    for d in items:
        r = render(d.repro)
        tree = answers.trees[d.pair[1]].get(r)
        a, b = answers.value(d.pair[0], r, d.sel), answers.value(d.pair[1], r, d.sel)
        vr = ValueRepro(d.repro, d.sel, a, b, tree) if tree is not None else None
        name = attribute_value(vr) if vr is not None else None
        if name is not None:
            report.value_buckets.setdefault(name, []).append(d)
        elif vr is not None and vr.unseen_elements():
            plain, decos = undecorate(d.repro)
            rep = Repro(plain, d.sel, lambda doc, d=d, decos=decos: answers.value_differs(
                d.pair, render(redecorate(doc, decos)), d.sel), answers.get(d.pair[1], r, "//*/@id") or [])
            unseen.append((d, rep, decos))
        else:
            report.value_novel.append(d)
    wanted: dict[str, set[str]] = defaultdict(set)
    for d, rep, decos in unseen:
        for doc in counterfactuals(rep):
            wanted[render(redecorate(doc, decos))].add(d.sel)
    answers.fetch_values(wanted)
    for d, rep, _decos in unseen:
        name = attribute(rep)
        if name is None:
            report.value_novel.append(d)
        else:
            report.value_unseen_tree.setdefault(name, []).append(d)


def split_value(d: Divergence, tree_divergent: set[str], report: Report, value_items: list[Divergence]) -> None:
    """A differing value column on a document whose fingerprint already differs is a TREE divergence, which
    the fingerprint gate holds; anywhere else it is a VALUE divergence, to be reduced and attributed."""
    (report.value_on_tree if render(d.doc) in tree_divergent else value_items).append(d)


_ATTR_NAME = re.compile(r"\[\s*([^\s~|^$*!=\]]+)")


def mirrors_parsel(tree: Any, sel: str) -> bool:
    """Do browser CSS and cssselect's XPath mean the same thing for `sel` on `tree`? Not in quirks mode
    (class and id selectors fold case), not for an `::attr()` name with a capital (`getAttribute()`
    lowercases it on an HTML element, XPath's `@` does not), not for an attribute test HTML folds, not for a class test where
    a class value holds a form feed (ASCII whitespace for a browser, not for XPath's normalize-space), and
    not for an attribute test naming what an SVG or MathML element carries camel-cased or in a namespace
    (`viewBox`, `xmlns`): CSS compares an attribute name case-insensitively and names no namespaced one,
    XPath compares the name exactly."""
    vr = ValueRepro((), sel, [], [], tree)
    if vr.quirks():
        return False
    if any(c.isupper() for name in re.findall(r"::attr\(([^)]*)\)", sel) for c in name if c.isascii()):
        return False
    if any(name.lower() in CASE_INSENSITIVE_ATTRS for name, _op, _v in _ATTR_TEST.findall(sel)):
        return False
    tested = {n.lower() for n in _ATTR_NAME.findall(sel)}
    if tested and any(k.lower() in tested and (k != k.lower() or ":" in k or k == "xmlns")
                      for el in vr.elements() if el[1] != T.XHTML for k in el[2][0::2]):
        return False
    if "class" in sel or re.search(r"\.", C.top_level(sel)):
        return not any("\x0c" in v for el in vr.elements() for k, v in zip(el[2][0::2], el[2][1::2])
                       if k == "class")
    return True


def mirror_check(answers: Answers, rendered: str, sel: str, native: list[str], report: Report) -> None:
    """Native CSS against parsel's own `.css()` over the bridged tree, wherever the two semantics coincide.

    This is the planner's standing check (`tools/browser_oracle/css.py`): the browser answers the
    selector, parsel's translator answers it on the same tree through lxml, and the pseudo-elements, the
    comma-group union and `:contains()` must come out the same. A plan that mirrors parsel only on the
    selectors someone tried by hand reads exactly like one that is right. Exact equality, outer HTML
    included: the bridge serialises an element the way `outerHTML` does, so this checks that too."""
    tree = answers.trees["chrome"].get(rendered)
    br = answers.bridge("chrome", rendered)
    if tree is None or isinstance(br, Refused) or not mirrors_parsel(tree, sel):
        return
    try:
        theirs = br.css(sel)
    except Exception:  # noqa: BLE001 - a selector parsel cannot run has nothing to mirror
        return
    report.css_mirror_checked += 1
    if native != theirs:
        report.css_mirror_differ.append((rendered, sel))


def check_bridge(answers: Answers, browsers: list[str], graded: list[tuple[Doc, str]], report: Report) -> None:
    """The bridge's two checks over every generated document: tree -> lxml -> tree is the identity, and
    XPath through lxml answers every fingerprint column as the native evaluator does."""
    for b in browsers:
        trees = [answers.trees[b][r] for _d, r in graded]
        failures, unrepresentable = Br.round_trip_failures(trees)
        rendered_of = {id(t): r for (_d, r), t in zip(graded, trees)}
        report.bridge_checked += sum(t is not None for t in trees) - len(unrepresentable)
        report.bridge_failures += [(rendered_of[id(t)], why) for t, why in failures]
        report.bridge_unrepresentable += [(rendered_of[id(t)], why) for t, why in unrepresentable]
        for d, r in graded:
            br = answers.bridge(b, r)
            if isinstance(br, Refused):
                continue
            for s in seq_sweep.fingerprint_selectors(ids_of(d)):
                if br.xpath(s) != answers.get(b, r, s):
                    report.bridge_xpath_differ.append((r, s))


# ---------------------------------------------------------------- the run
def short(values: Optional[list[str]], width: int = 60) -> str:
    text = json.dumps(values)
    return text if len(text) <= width else text[:width - 3] + "..."


def example_line(d: Divergence, answers: Answers) -> str:
    r = render(d.repro)
    a, b = (answers.get(src, r, d.sel) for src in d.pair)
    return f"{json.dumps(r)}  {d.sel}  {d.pair[0]}={short(a)} {d.pair[1]}={short(b)}"


def value_line(d: Divergence, answers: Answers) -> str:
    r = render(d.repro)
    a, b = (answers.value(src, r, d.sel) for src in d.pair)
    return f"{json.dumps(r)}  {d.sel}  {d.pair[0]}={short(a)} {d.pair[1]}={short(b)}"


def by_shape(items: list[Divergence], plain: bool = False) -> dict[tuple[str, ...], list[Divergence]]:
    """Group reduced divergences by their token sequence; `plain` groups a tree divergence by its
    undecorated tokens, since a decoration is not what a tree construct is about."""
    groups: dict[tuple[str, ...], list[Divergence]] = defaultdict(list)
    for d in items:
        doc = undecorate(d.repro)[0] if plain else d.repro
        groups[tuple(t for _, t in doc)].append(d)
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), len(kv[0]), kv[0])))


def engine_refusals(sels: list[str], parser: str) -> dict[str, str]:
    """The value selectors the engine refuses in this mode, with its reason. A refusal is an empty column
    by contract (no fallback), so it is reported, not graded as a lost value.

    Each selector is asked ALONE. One `check` over every selector of a run is one schema far past the
    engine's column ceiling, where a mixed comma group cannot be routed, so it reported conformant's six
    `p::text, p:contains("alpha")::text` members refused in both modes and the gate never graded them."""
    import frostwork

    kw = seq_sweep.parser_kwargs(parser)
    out = {}
    for s in sels:
        spec = grouped_spec(s)
        if spec:
            g = frostwork.check([], [(spec[0], [(f"s{k}", sub) for k, sub in enumerate(spec[1])])], **kw).groups[0]
            fields = [g.container] + list(g.subfields)
        else:
            fields = [frostwork.check([s], **kw).fields[0]]
        bad = [f for f in fields if not f.supported]
        if bad:
            out[s] = bad[0].reason or "unsupported"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browsers", default="chrome", help="comma-separated: chrome, firefox (chrome first)")
    ap.add_argument("--parser", default="browser", help="the engine's parse mode, if the extension accepts parser=")
    ap.add_argument("--seq-depth", type=int, default=2, help="every seq_sweep sequence up to this length")
    ap.add_argument("--random", type=int, default=2000, help="random documents over ALPHABET")
    ap.add_argument("--values", type=int, default=1000,
                    help="decorated documents over ALPHABET, VALUE_ATTRS and VALUE_TEXTS for the value columns")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--selfcheck", type=int, default=300,
                    help="documents sent for the self-check: each fast-path one is also page-loaded, "
                         "and its two trees (and CSS answers) must be the same")
    ap.add_argument("--tabs", type=int, default=8, help="page-load tabs per browser")
    ap.add_argument("--show", type=int, default=25, help="NOVEL shapes to print")
    ap.add_argument("--gate", action="store_true", help="exit nonzero on NOVEL or a coverage failure")
    ap.add_argument("--debug-differ", default=None,
                    help="a DEBUG build of the differ bridge (target/debug/differ): every graded document is "
                         "run through it too, and a panic (a broken builder invariant) fails the gate")
    args = ap.parse_args()

    browsers = [b.strip() for b in args.browsers.split(",") if b.strip()]
    if not browsers or browsers[0] != "chrome":
        ap.error("--browsers must start with chrome: browser mode follows Chrome where the two disagree")
    run_engine, engine_desc = engine_runner(args.parser)

    docs = seq_docs(args.seq_depth)
    rdocs, used = random_docs(args.random, args.seed)
    vdocs, vused = value_docs(args.values, args.seed)
    tdocs = tail_docs() if args.values else []
    sdocs = structured_docs() if args.values else []
    vdocs += tdocs + sdocs
    docs += rdocs + vdocs
    decorated = {render(d) for d in vdocs}
    report = Report(missing_tokens=[t for t in ALPHABET if args.random and not used[t]],
                    missing_value_tokens=[t for t in VALUE_TOKENS if args.values and not vused[t]],
                    debug_requested=bool(args.debug_differ))

    # The value basket: conformant.py's, plus the value-rule selectors, minus what the engine refuses here.
    basket = list(dict.fromkeys(conformant.BASKET))
    every_value_sel = sorted({s for d in docs for s in value_selectors(d, render(d) in decorated, basket)})
    refused = engine_refusals(every_value_sel, args.parser)
    basket = [s for s in basket if s not in refused]

    oracles: dict[str, BrowserOracle] = {}
    t_start = time.perf_counter()
    try:
        for b in browsers:
            oracles[b] = BrowserOracle(b, tabs=args.tabs).__enter__()
        answers = Answers(run_engine, oracles)
        rendered_all = [render(d) for d in docs]
        answers.fetch(rendered_all)

        # The fast path is trusted only as far as this run re-measures it. probe.cjs decides which of the
        # sample is eligible, so the rule has one copy. The value documents' sample carries their CSS, so
        # the self-check covers the native answers as well as the trees.
        step = max(1, len(rendered_all) // max(1, args.selfcheck))
        sample = rendered_all[::step][:args.selfcheck]
        vsample = [d for d in vdocs[::max(1, len(vdocs) // max(1, args.selfcheck // 3))]][:args.selfcheck // 3]
        for oracle in oracles.values():
            if sample:
                oracle.trees(sample, selfcheck=True)
            if vsample:
                oracle.query([render(d) for d in vsample],
                             [[oracle_form(s) for s in value_selectors(d, True, basket) if is_native(s) and s not in refused]
                              for d in vsample], selfcheck=True)

        graded_docs: list[tuple[Doc, str]] = []
        engine_items: list[Divergence] = []
        browser_items: list[Divergence] = []
        vs_firefox = 0  # columns where the engine differs from Firefox, whatever Chrome says
        for d, r in zip(docs, rendered_all):
            sels = seq_sweep.fingerprint_selectors(ids_of(d))
            if any(answers.trees[b][r] is None for b in browsers):
                continue
            answers.prefetch_engine({r: set(sels)})
            report.graded += 1
            graded_docs.append((d, r))
            for s in sels:
                if answers.differs(("engine", "chrome"), r, s):
                    engine_items.append(Divergence(d, s, ("engine", "chrome")))
                if "firefox" in oracles:
                    if answers.differs(("chrome", "firefox"), r, s):
                        browser_items.append(Divergence(d, s, ("chrome", "firefox")))
                    vs_firefox += answers.differs(("engine", "firefox"), r, s)
        tree_divergent = {render(it.doc) for it in engine_items}
        check_bridge(answers, browsers, graded_docs, report)
        t_graded = time.perf_counter()

        # ---- value columns: CSS natively, XPath through the bridge, on the same trees
        wanted = {r: {s for s in value_selectors(d, r in decorated, basket) if s not in refused}
                  for d, r in graded_docs}
        answers.fetch_values(wanted)
        value_items: list[Divergence] = []
        browser_value_items: list[Divergence] = []
        oracle_refused: Counter = Counter()
        oracle_answered: Counter = Counter()
        for d, r in graded_docs:
            for s in sorted(wanted[r]):
                want = answers.value("chrome", r, s)
                if isinstance(want, Refused):
                    oracle_refused[(want.by, want.reason[:90])] += 1
                    continue
                report.value_graded += 1
                oracle_answered[s] += bool(want)
                if answers.value_differs(("engine", "chrome"), r, s):
                    split_value(Divergence(d, s, ("engine", "chrome")), tree_divergent, report, value_items)
                if "firefox" in oracles and answers.value_differs(("chrome", "firefox"), r, s):
                    browser_value_items.append(Divergence(d, s, ("chrome", "firefox")))
                if C.is_css(s) and not grouped_spec(s):
                    mirror_check(answers, r, s, want, report)
        report.rule_never_answered = [s for s in RULE_SELECTORS if s not in refused and not oracle_answered[s]]
        report.rule_refused = [s for s in RULE_SELECTORS if s in refused]
        t_values = time.perf_counter()

        t_debug = time.perf_counter()
        if args.debug_differ:
            checked = [(r, seq_sweep.fingerprint_selectors(ids_of(d)) + RULE_SELECTORS) for d, r in graded_docs]
            report.debug_panics = debug_panics(args.debug_differ, args.parser, checked)
            report.debug_checked = len(checked)
        t_debug = time.perf_counter() - t_debug
        reduce_all(engine_items + browser_items, answers)
        reduce_values(value_items + browser_value_items, answers)
        t_reduced = time.perf_counter()
        attribute_all(engine_items, answers, report)
        attribute_values(value_items, answers, report)
    finally:
        for oracle in oracles.values():
            oracle.close()

    for o in oracles.values():
        report.oracle_errors += [{"browser": o.browser, **e} for e in o.errors]
        report.selfcheck_checked += o.selfcheck_checked
        report.selfcheck_differ += o.selfcheck_differ
        report.selfcheck_css_differ += o.selfcheck_css_differ

    # ------------------------------------------------------------ the report
    print(f"engine            : {engine_desc}")
    for o in oracles.values():
        print(f"oracle            : {o.build} ({dict(o.paths)})")
    print(f"documents         : {len(docs)} ({len(docs) - len(rdocs) - len(vdocs)} seq_sweep sequences up to "
          f"depth {args.seq_depth}, {len(rdocs)} random, {len(vdocs)} value of which {len(tdocs)} deferred-tail and "
          f"{len(sdocs)} structured, "
          f"seed {args.seed}); "
          f"graded {report.graded}")
    if args.random:
        print(f"random alphabet   : {len(ALPHABET)} tokens, each emitted {min(used[t] for t in ALPHABET)} "
              f"to {max(used[t] for t in ALPHABET)} times")
    if args.values:
        print(f"value tokens      : {len(VALUE_TOKENS)}, each emitted {min(vused[t] for t in VALUE_TOKENS)} "
              f"to {max(vused[t] for t in VALUE_TOKENS)} times")
    rate = answers.oracle_docs / answers.oracle_seconds if answers.oracle_seconds else 0.0
    print(f"oracle throughput : {answers.oracle_docs} trees in {answers.oracle_seconds:.1f}s = {rate:.0f} docs/s "
          f"(all browsers, reduction candidates included)")
    print(f"wall time         : grading {t_graded - t_start:.1f}s, values {t_values - t_graded:.1f}s, "
          f"debug build {t_debug:.1f}s, reduction {t_reduced - t_values - t_debug:.1f}s")
    if args.debug_differ:
        print(f"debug build       : {report.debug_checked} documents through {args.debug_differ}, "
              f"{len(report.debug_panics)} panic")
        for r, why in report.debug_panics[:args.show]:
            print(f"    {json.dumps(r)[:160]}  {why[:200]}")
    print(f"self-check        : {report.selfcheck_checked} fast-path documents page-loaded, "
          f"{len(report.selfcheck_differ)} trees and {len(report.selfcheck_css_differ)} CSS answers differ")
    print(f"native CSS        : {report.css_mirror_checked} columns checked against parsel's .css() over the "
          f"bridged tree, {len(report.css_mirror_differ)} differ"
          + (f", e.g. {report.css_mirror_differ[:3]}" if report.css_mirror_differ else ""))
    unrep = Counter(why for _r, why in report.bridge_unrepresentable)
    print(f"XPath bridge      : {report.bridge_checked} trees round-tripped, {len(report.bridge_failures)} changed, "
          f"{len(report.bridge_xpath_differ)} fingerprint columns answered differently from the native "
          f"evaluator; {len(report.bridge_unrepresentable)} unrepresentable {dict(unrep) if unrep else ''}")
    print(f"divergent columns : {len(engine_items)} against Chrome, in "
          f"{len({render(d.doc) for d in engine_items})} documents")
    if "firefox" in oracles:
        print(f"                    {vs_firefox} against Firefox; {len(browser_items)} columns where the "
              f"browsers disagree (the engine is graded against Chrome there)")

    print(f"\n  {'construct (section 3)':<34} {'columns':>8} {'docs':>6} {'shapes':>7}  smallest example")
    for name, items in sorted(report.buckets.items(), key=lambda kv: -len(kv[1])):
        ex = min(items, key=lambda d: (len(d.repro), render(d.repro)))
        print(f"  {name:<34} {len(items):>8} {len({render(d.doc) for d in items}):>6} "
              f"{len(by_shape(items, plain=True)):>7}  {example_line(ex, answers)}")
    shapes = by_shape(report.novel, plain=True)
    print(f"  {'NOVEL':<34} {len(report.novel):>8} {len({render(d.doc) for d in report.novel}):>6} "
          f"{len(shapes):>7}")
    if shapes:
        print(f"\n  NOVEL, by reduced shape (no section 3 construct explains these; first {args.show}):")
        for shape, items in itertools.islice(shapes.items(), args.show):
            print(f"    {len(items):>6}  {example_line(items[0], answers)}")

    if browser_items:
        bshapes = by_shape(browser_items, plain=True)
        print(f"\n  Chrome versus Firefox: {len(browser_items)} columns in "
              f"{len({render(d.doc) for d in browser_items})} documents, {len(bshapes)} reduced shapes")
        for shape, items in itertools.islice(bshapes.items(), args.show):
            print(f"    {len(items):>6}  {example_line(items[0], answers)}")

    # ---- the value verdict, split by whether the tree already explains it
    print(f"\nVALUE COLUMNS     : {report.value_graded} graded against Chrome ({len(basket)} basket selectors, "
          f"{len(RULE_SELECTORS)} value-rule selectors, per-id columns on every document)")
    if refused:
        print(f"  refused by the engine in {args.parser} mode (an empty column by contract, not graded): "
              f"{len(refused)}, e.g. {sorted(refused)[:4]}")
    if oracle_refused:
        print(f"  not answerable by the oracle (never graded): {sum(oracle_refused.values())} columns")
        for (by, why), n in oracle_refused.most_common(6):
            print(f"    {n:>6}  [{by}] {why}")
    empty = [s for s in basket if not oracle_answered[s]]
    if empty:
        print(f"  basket selectors the oracle answered nowhere (they can only catch an over-match): {len(empty)}")
    on_tree: Counter = Counter()
    doc_buckets: dict[str, set[str]] = defaultdict(set)
    for name, items in report.buckets.items():
        for it in items:
            doc_buckets[render(it.doc)].add(name)
    for it in report.novel:
        doc_buckets[render(it.doc)].add("NOVEL")
    for it in report.value_on_tree:
        for name in doc_buckets.get(render(it.doc), {"?"}):
            on_tree[name] += 1
    print(f"  TREE  (the document's fingerprint already differs, so the tree gate holds these): "
          f"{len(report.value_on_tree)} columns in {len({render(d.doc) for d in report.value_on_tree})} documents")
    for name, n in on_tree.most_common():
        print(f"    {n:>8}  under {name}")
    unseen = sum(len(v) for v in report.value_unseen_tree.values())
    print(f"  TREE the fingerprint cannot see (it agrees, but the column is about an id-less element it has no "
          f"column for): {unseen}")
    for name, items in sorted(report.value_unseen_tree.items(), key=lambda kv: -len(kv[1])):
        ex = min(items, key=lambda d: (len(d.repro), render(d.repro)))
        print(f"    {len(items):>8}  under {name:<30} {value_line(ex, answers)}")
    print("  VALUE (the fingerprint agrees):")
    print(f"  {'value construct':<46} {'columns':>8} {'docs':>6} {'shapes':>7}  smallest example")
    for name, items in sorted(report.value_buckets.items(), key=lambda kv: -len(kv[1])):
        ex = min(items, key=lambda d: (len(d.repro), render(d.repro)))
        print(f"  {name:<46} {len(items):>8} {len({render(d.doc) for d in items}):>6} "
              f"{len(by_shape(items)):>7}  {value_line(ex, answers)}")
    vshapes = by_shape(report.value_novel)
    print(f"  {'value NOVEL':<46} {len(report.value_novel):>8} "
          f"{len({render(d.doc) for d in report.value_novel}):>6} {len(vshapes):>7}")
    if vshapes:
        print(f"\n  value NOVEL, by reduced shape (first {args.show}):")
        for shape, items in itertools.islice(vshapes.items(), args.show):
            print(f"    {len(items):>6}  {value_line(items[0], answers)}")
    if browser_value_items:
        bvshapes = by_shape(browser_value_items)
        print(f"\n  Chrome versus Firefox, value columns: {len(browser_value_items)} in {len(bvshapes)} reduced shapes")
        for shape, items in itertools.islice(bvshapes.items(), args.show):
            print(f"    {len(items):>6}  {value_line(items[0], answers)}")

    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    with open(RESULTS, "w", encoding="utf-8") as f:
        json.dump({
            "engine": engine_desc, "browsers": {o.browser: o.build for o in oracles.values()},
            "graded": report.graded, "seed": args.seed,
            "buckets": {k: len(v) for k, v in report.buckets.items()},
            "novel": [{"repro": render(items[0].repro), "column": items[0].sel, "count": len(items)}
                      for items in shapes.values()],
            "chrome_vs_firefox": [{"repro": render(items[0].repro), "column": items[0].sel, "count": len(items)}
                                  for items in by_shape(browser_items, plain=True).values()],
            "oracle_errors": report.oracle_errors, "selfcheck_differ": report.selfcheck_differ,
            "values": {
                "graded": report.value_graded, "on_tree": len(report.value_on_tree),
                "unseen_tree": {k: len(v) for k, v in report.value_unseen_tree.items()},
                "buckets": {k: len(v) for k, v in report.value_buckets.items()},
                "novel": [{"repro": render(items[0].repro), "column": items[0].sel, "count": len(items)}
                          for items in vshapes.values()],
                "engine_refused": sorted(refused), "basket_oracle_empty": empty,
            },
            "debug": {"checked": report.debug_checked, "panics": report.debug_panics[:20]},
            "bridge": {"checked": report.bridge_checked, "failures": report.bridge_failures[:20],
                       "unrepresentable": report.bridge_unrepresentable[:20],
                       "xpath_differ": report.bridge_xpath_differ[:20]},
        }, f, indent=1)
    print(f"\n  wrote {os.path.relpath(RESULTS)}")

    if args.gate:
        fails = gate_failures(report)
        for why in fails:
            print(f"  GATE: {why}")
        print(f"  GATE: {'FAIL' if fails else 'PASS'}")
        return 1 if fails else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
