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
the tree the fingerprint was read from. A column that differs where the fingerprint already differs is
filed under the TREE, which that gate holds. Where the fingerprint agrees, it is reduced with the
fingerprint still agreeing at every step and attributed to VALUE_CONSTRUCTS; if its column names an
id-less element the fingerprint has no column for, to the section 3 construct the repro exhibits (a tree
difference only the value column could see); anything else is value NOVEL.

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
import contextlib
import functools
import itertools
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import conformant  # noqa: E402  (its selector basket)
import diff_lxml  # noqa: E402  (the engine's member budget, and what a bare-element selector is)
import seq_sweep  # noqa: E402  (the fingerprint and the short-sequence alphabet)
import webpoet_structure  # noqa: E402  (the one structural comparison for outer HTML)
from browser_docs import (  # noqa: E402
    ALPHABET, DOCTYPE, PLAIN_HEAD, RULE_SELECTORS, VALUE_TOKENS, Decorations, Doc, Documents, generate, grouped_spec,
    ids_of, redecorate, render, undecorate, value_selectors)
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
class Engine:
    """`frostwork.extract` in one parse mode. `parser=` is passed only when the installed extension takes it."""

    def __init__(self, parser: str):
        self.parser = parser
        self.mode = seq_sweep.parser_kwargs(parser)

    def __call__(self, doc: bytes, sels: list[str], strict: bool = True) -> list[list[str]]:
        import frostwork

        return frostwork.extract(doc, sels, "utf-8", strict=strict, **self.mode)

    def grouped(self, doc: bytes, cols: list[str]) -> list[Any]:
        """Each grouped column's rows: per container match, one value list per sub."""
        import frostwork

        return frostwork.extract_grouped(doc, [], [engine_group(c) for c in cols], "utf-8", **self.mode)[1]

    def describe(self) -> str:
        if self.mode:
            return f"frostwork.extract(..., parser={self.parser!r})"
        note = "" if self.parser == "lxml" else f"; --parser {self.parser} was NOT applied"
        return f"frostwork.extract without parser= (the installed extension has no such keyword, so lxml mode{note})"


def engine_group(col: str) -> tuple[str, list[tuple[str, str]]]:
    """A grouped column as the engine takes a group: its container, and its subs named s0, s1, ..."""
    container, subs = grouped_spec(col)  # type: ignore[misc]
    return container, [(f"s{k}", s) for k, s in enumerate(subs)]


def debug_panics(differ: str, parser: str, docs: list[tuple[str, list[str]]]) -> list[tuple[str, str]]:
    """The documents on which a DEBUG build of the `differ` bridge panics, with the panic.

    Its `debug_assert!`s are the builder's own invariants (an element the builder still has open is never
    closed, a live element is open or parked, a tail pass opens every recorded winner). A release build
    that breaks one returns a value that can still pass, such as a template's outer HTML cut short, which
    the raw-source allowance excuses. The bridge answers a batch at exit, so a panic shows as the line its
    output stops at; the run goes on after it in a new process."""
    requests: list[tuple[int, str]] = []   # (document index, one line of the bridge's protocol)
    for k, (rendered, sels) in enumerate(docs):
        sels = [s for s in sels if "\t" not in s and "\n" not in s]
        for start in range(0, max(1, len(sels)), 60):   # inside the engine's member budget
            requests.append((k, "\t".join(["utf-8", rendered.encode("utf-8").hex()] + sels[start:start + 60])))
    out: list[tuple[str, str]] = []
    at = 0
    while at < len(requests):
        p = subprocess.run([differ, "--parser", parser], input="".join(line + "\n" for _k, line in requests[at:]),
                           capture_output=True, text=True, encoding="utf-8")
        done = p.stdout.count("\n")
        if p.returncode == 0 and at + done >= len(requests):
            break
        k = requests[at + done][0]
        err = [ln.strip() for ln in p.stderr.splitlines() if ln.strip()]
        first = next((i for i, ln in enumerate(err) if "panicked" in ln), 0)
        out.append((docs[k][0], " ".join(err[first:first + 2]) or f"exit {p.returncode}"))
        at += done
        while at < len(requests) and requests[at][0] == k:
            at += 1
    return out


def engine_refusals(sels: list[str], parser: str) -> dict[str, str]:
    """The value selectors the engine refuses in this mode, with its reason. A refusal is an empty column
    by contract (no fallback), so it is reported, not graded as a lost value.

    Each selector is asked ALONE: every selector of a run together is one schema past the engine's column
    ceiling, where a mixed comma group cannot be routed and would read as refused."""
    import frostwork

    kw = seq_sweep.parser_kwargs(parser)
    out = {}
    for s in sels:
        if grouped_spec(s):
            g = frostwork.check([], [engine_group(s)], **kw).groups[0]
            fields = [g.container] + list(g.subfields)
        else:
            fields = [frostwork.check([s], **kw).fields[0]]
        bad = [f for f in fields if not f.supported]
        if bad:
            out[s] = bad[0].reason or "unsupported"
    return out


# ---------------------------------------------------------------- answers, cached per document
class Answers:
    """Every source's answer to (document, column), computed on demand and cached.

    Sources are "engine" and the browser names. A fingerprint column's answer is a list of strings. A
    browser tree that failed to load answers `None`, which never counts as a difference: it is an oracle
    error, counted separately. A value column's answer is a list of strings, or a `Refused` saying why
    there is none (css.py could not plan it, the browser rejected it, the bridge cannot hold the document,
    or the tree failed to load), which is never graded and never reads as an empty column.
    """

    def __init__(self, engine: Engine, oracles: dict[str, BrowserOracle]):
        self.engine = engine
        self.oracles = oracles
        self.trees: dict[str, dict[str, Any]] = {b: {} for b in oracles}  # browser -> document -> its tree
        self.columns: dict[tuple[str, str, str], Optional[list[str]]] = {}  # (source, document, fingerprint column)
        self.values: dict[tuple[str, str, str], Any] = {}                   # (source, document, value column)
        self.oracle_seconds = 0.0
        self.oracle_docs = 0
        self._indexed: dict[tuple[str, str], Any] = {}
        self._bridges: dict[tuple[str, str], Any] = {}

    @contextlib.contextmanager
    def _asking(self, docs: int):
        """Count `docs` documents and the time the block takes toward the oracle's throughput."""
        t0 = time.perf_counter()
        yield
        self.oracle_seconds += time.perf_counter() - t0
        self.oracle_docs += docs

    def fetch(self, rendered: set[str] | list[str]) -> None:
        """Every browser's tree of each document it does not have yet."""
        for b, oracle in self.oracles.items():
            missing = sorted({r for r in rendered if r not in self.trees[b]})
            if missing:
                with self._asking(len(missing)):
                    self.trees[b].update(zip(missing, oracle.trees(missing)))

    def _run_engine(self, store: dict, rendered: str, sels: list[str] | set[str], strict: bool) -> None:
        """One engine pass per `MAX_SELECTORS_PER_PASS` of the columns `store` lacks for a document."""
        need = sorted(s for s in sels if ("engine", rendered, s) not in store)
        for start in range(0, len(need), diff_lxml.MAX_SELECTORS_PER_PASS):
            chunk = need[start:start + diff_lxml.MAX_SELECTORS_PER_PASS]
            for s, v in zip(chunk, self.engine(rendered.encode(), chunk, strict=strict)):
                store[("engine", rendered, s)] = v

    def prefetch_engine(self, wanted: dict[str, set[str]]) -> None:
        """The engine's answer to every fingerprint column `wanted` names, one pass per document."""
        for r, sels in wanted.items():
            self._run_engine(self.columns, r, sels, strict=True)

    def _tree(self, browser: str, rendered: str) -> Any:
        if rendered not in self.trees[browser]:
            self.fetch([rendered])
        return self.trees[browser][rendered]

    def column(self, source: str, rendered: str, sel: str) -> Optional[list[str]]:
        key = (source, rendered, sel)
        if key not in self.columns:
            if source == "engine":
                self.prefetch_engine({rendered: {sel}})
            else:
                tree = self._tree(source, rendered)
                if tree is None:
                    self.columns[key] = None
                else:
                    index = self._indexed.get((source, rendered))
                    if index is None:
                        index = self._indexed[(source, rendered)] = T.index(T.document_view(tree))
                    self.columns[key] = T.evaluate(index, sel)
        return self.columns[key]

    def differs(self, pair: tuple[str, str], rendered: str, sel: str) -> bool:
        a, b = self.column(pair[0], rendered, sel), self.column(pair[1], rendered, sel)
        return a is not None and b is not None and a != b

    def fingerprint_differs(self, pair: tuple[str, str], rendered: str, ids: list[str]) -> bool:
        return any(self.differs(pair, rendered, s) for s in seq_sweep.fingerprint_selectors(ids))

    # ------------------------------------------------------------ value columns
    def bridge(self, browser: str, rendered: str) -> Any:
        key = (browser, rendered)
        if key not in self._bridges:
            self._bridges[key] = Br.open_bridge(self.trees[browser].get(rendered))
        return self._bridges[key]

    def fetch_values(self, wanted: dict[str, set[str]]) -> None:
        """Every engine and oracle value `wanted` names: CSS in one browser request, XPath via the bridge."""
        for b, oracle in self.oracles.items():
            need = {r: sorted(s for s in sels if is_native(s) and (b, r, s) not in self.values)
                    for r, sels in wanted.items()}
            need = {r: s for r, s in need.items() if s}
            if need:
                rs = sorted(need)
                with self._asking(len(rs)):
                    trees, got = oracle.query(rs, [[oracle_form(s) for s in need[r]] for r in rs])
                for r, t, row in zip(rs, trees, got):
                    self.trees[b].setdefault(r, t)
                    for k, s in enumerate(need[r]):
                        self.values[(b, r, s)] = Refused("page load failed", "load") if row is None else row[k]
            missing = sorted({r for r in wanted if r not in self.trees[b]})
            if missing:
                self.fetch(missing)
        for r, sels in wanted.items():
            missing = [s for s in sels if ("engine", r, s) not in self.values]
            self._run_engine(self.values, r, [s for s in missing if not grouped_spec(s)], strict=False)
            grouped = sorted(s for s in missing if grouped_spec(s))
            if grouped:
                for s, v in zip(grouped, self.engine.grouped(r.encode(), grouped)):
                    self.values[("engine", r, s)] = v

    def value(self, source: str, rendered: str, sel: str) -> Any:
        key = (source, rendered, sel)
        if key not in self.values:
            if source == "engine" or is_native(sel):
                self.fetch_values({rendered: {sel}})
            else:
                self.values[key] = Br.xpath_answer(self.bridge(source, rendered), sel)
        return self.values[key]

    def value_differs(self, pair: tuple[str, str], rendered: str, sel: str) -> bool:
        a, b = self.value(pair[0], rendered, sel), self.value(pair[1], rendered, sel)
        return isinstance(a, list) and isinstance(b, list) and not same_values(a, b, sel)


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


def _shrink(items: list[Divergence], prepare: Callable[[list[Divergence], dict], None],
            still_differs: Callable[[Divergence, Doc], bool]) -> None:
    """Shrink each item's document one token at a time while `still_differs(item, candidate)` (1-minimal).

    Runs in rounds over every item at once, so each round costs one oracle batch rather than one browser
    round trip per candidate: `prepare(active, candidates)` fetches what a round's candidates need."""
    for it in items:
        it.repro = it.doc
    active = [it for it in items if len(it.repro) > 1]
    while active:
        cands = {id(it): [it.repro[:k] + it.repro[k + 1:] for k in range(len(it.repro))] for it in active}
        prepare(active, cands)
        nxt = []
        for it in active:
            for c in cands[id(it)]:
                if still_differs(it, c):
                    it.repro = c
                    if len(c) > 1:
                        nxt.append(it)
                    break
        active = nxt


def reduce_all(items: list[Divergence], answers: Answers) -> None:
    """Reduce tree columns: each document to the smallest subsequence on which its column still differs."""
    def prepare(active, cands):
        answers.fetch({render(c) for cs in cands.values() for c in cs})
        wanted: dict[str, set[str]] = defaultdict(set)
        for it in active:
            if "engine" in it.pair:
                for c in cands[id(it)]:
                    wanted[render(c)].add(it.sel)
        answers.prefetch_engine(wanted)

    _shrink(items, prepare, lambda it, c: answers.differs(it.pair, render(c), it.sel))


def reduce_values(items: list[Divergence], answers: Answers) -> None:
    """`reduce_all` for value columns, with one more condition on every step: the two sources' trees must
    still agree on the fingerprint. A value divergence is one the tree does not explain, so its repro must
    never turn into a tree divergence on the way down."""
    def prepare(active, cands):
        wanted: dict[str, set[str]] = defaultdict(set)
        prints: dict[str, set[str]] = defaultdict(set)
        for it in active:
            for c in cands[id(it)]:
                wanted[render(c)].add(it.sel)
                prints[render(c)].update(seq_sweep.fingerprint_selectors(ids_of(c)))
        answers.fetch_values(wanted)
        answers.prefetch_engine(prints)

    def still_differs(it, c):
        r = render(c)
        return answers.value_differs(it.pair, r, it.sel) and not answers.fingerprint_differs(it.pair, r, ids_of(c))

    _shrink(items, prepare, still_differs)


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
        return self.doc[:lead] + ((-1, PLAIN_HEAD),) + self.doc[lead:]

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


def repro_of(d: Divergence, answers: Answers,
             differs: Callable[[tuple[str, str], str, str], bool]) -> tuple[Repro, Decorations]:
    """The attribution view of a reduced divergence: its ALPHABET tokens, with every counterfactual
    decorated again (by `decos`) before `differs(pair, document, column)` asks the oracle about it."""
    plain, decos = undecorate(d.repro)
    return (Repro(plain, d.sel, lambda doc: differs(d.pair, render(redecorate(doc, decos)), d.sel),
                  answers.column(d.pair[1], render(d.repro), "//*/@id") or []), decos)


def counterfactual_documents(entries: list[tuple[Divergence, Repro, Decorations]]) -> dict[str, set[str]]:
    """Every document the entries' counterfactual rules will ask about, with the columns, to fetch in one batch."""
    wanted: dict[str, set[str]] = defaultdict(set)
    for d, repro, decos in entries:
        for doc in counterfactuals(repro):
            wanted[render(redecorate(doc, decos))].add(d.sel)
    return wanted


def attribute_all(items: list[Divergence], answers: Answers, report: Report) -> None:
    """File each reduced divergence under its construct."""
    entries = [(d, *repro_of(d, answers, answers.differs)) for d in items]
    wanted = counterfactual_documents(entries)
    answers.fetch(set(wanted))
    answers.prefetch_engine(wanted)
    for d, repro, _decos in entries:
        name = attribute(repro)
        if name is None:
            report.novel.append(d)
        else:
            report.buckets.setdefault(name, []).append(d)


# ---------------------------------------------------------------- value attribution
# A value column that differs on a document whose fingerprint agrees is not explained by the tree, so the
# section 3 constructs cannot explain it either. These are the value-level ones, each a documented
# decision or divergence rather than a tree rule. They read the reduced, still-decorated document and the
# oracle's own tree, never the page it came from.
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


def attribute_values(items: list[Divergence], answers: Answers, report: Report) -> None:
    """File each reduced value divergence under a value construct; else, if the column is about an element
    the fingerprint cannot see, under the section 3 construct the reduced document exhibits (a TREE
    divergence the value column found); else NOVEL."""
    unseen = []
    for d in items:
        r = render(d.repro)
        tree = answers.trees[d.pair[1]].get(r)
        a, b = answers.value(d.pair[0], r, d.sel), answers.value(d.pair[1], r, d.sel)
        vr = ValueRepro(d.repro, d.sel, a, b, tree) if tree is not None else None
        name = attribute_value(vr) if vr is not None else None
        if name is not None:
            report.value_buckets.setdefault(name, []).append(d)
        elif vr is not None and vr.unseen_elements():
            unseen.append((d, *repro_of(d, answers, answers.value_differs)))
        else:
            report.value_novel.append(d)
    answers.fetch_values(counterfactual_documents(unseen))
    for d, repro, _decos in unseen:
        name = attribute(repro)
        if name is None:
            report.value_novel.append(d)
        else:
            report.value_unseen_tree.setdefault(name, []).append(d)


def split_value(d: Divergence, tree_divergent: set[str], report: Report, value_items: list[Divergence]) -> None:
    """A differing value column on a document whose fingerprint already differs is a TREE divergence, which
    the fingerprint gate holds; anywhere else it is a VALUE divergence, to be reduced and attributed."""
    (report.value_on_tree if render(d.doc) in tree_divergent else value_items).append(d)


# ---------------------------------------------------------------- checks the gate makes on its own tools
# The attributes whose values HTML has CSS match ASCII case-insensitively on an HTML element (the HTML
# standard, "Case-sensitivity of selectors"). XPath matches every value exactly.
CASE_INSENSITIVE_ATTRS = frozenset("""accept accept-charset align alink axis bgcolor charset checked clear
codetype color compact declare defer dir direction disabled enctype face frame hreflang http-equiv lang
language link media method multiple nohref noresize noshade nowrap readonly rel rev rules scope scrolling
selected shape target text type valign valuetype vlink""".split())
_ATTR_TEST = re.compile(r"""\[\s*([^\s~|^$*!=\]]+)\s*([~|^$*]?=)\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[^\]\s]+)\s*\]""")
_ATTR_NAME = re.compile(r"\[\s*([^\s~|^$*!=\]]+)")


def mirrors_parsel(tree: Any, sel: str) -> bool:
    """Do browser CSS and cssselect's XPath mean the same thing for `sel` on `tree`? Not in quirks mode
    (class and id selectors fold case), not for an `::attr()` name with a capital (`getAttribute()`
    lowercases it on an HTML element, XPath's `@` does not), not for an attribute test HTML folds, not for
    a class test where a class value holds a form feed (ASCII whitespace for a browser, not for XPath's
    normalize-space), and not for an attribute test naming what an SVG or MathML element carries
    camel-cased or in a namespace (`viewBox`, `xmlns`): CSS compares an attribute name case-insensitively
    and names no namespaced one, XPath compares the name exactly."""
    if T.is_quirks(tree):
        return False
    if any(c.isupper() for name in re.findall(r"::attr\(([^)]*)\)", sel) for c in name if c.isascii()):
        return False
    if any(name.lower() in CASE_INSENSITIVE_ATTRS for name, _op, _v in _ATTR_TEST.findall(sel)):
        return False
    tested = {n.lower() for n in _ATTR_NAME.findall(sel)}
    if tested and any(k.lower() in tested and (k != k.lower() or ":" in k or k == "xmlns")
                      for el in T.elements(tree) if el[1] != T.XHTML for k in el[2][0::2]):
        return False
    if "class" in sel or re.search(r"\.", C.top_level(sel)):
        return not any("\x0c" in v for el in T.elements(tree) for k, v in zip(el[2][0::2], el[2][1::2])
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
                if br.xpath(s) != answers.column(b, r, s):
                    report.bridge_xpath_differ.append((r, s))


# ---------------------------------------------------------------- the run
class Phases(dict):
    """Seconds spent in each named phase of a run, marked as it ends."""

    def __init__(self) -> None:
        super().__init__()
        self._last = time.perf_counter()

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self[name] = now - self._last
        self._last = now


@dataclass
class Run:
    """What a run measured besides its verdict: the printed report and the results file read it."""

    args: argparse.Namespace
    engine: Engine
    docs: Documents
    oracles: dict[str, BrowserOracle]
    answers: Answers
    basket: list[str]                 # conformant.py's selectors, minus what the engine refuses in this mode
    refused: dict[str, str]           # every value selector the engine refuses, with its reason
    phases: Phases
    engine_items: list[Divergence] = field(default_factory=list)         # tree columns, engine against Chrome
    browser_items: list[Divergence] = field(default_factory=list)        # ... Chrome against Firefox
    value_items: list[Divergence] = field(default_factory=list)          # value columns the tree does not explain
    browser_value_items: list[Divergence] = field(default_factory=list)  # ... Chrome against Firefox
    vs_firefox: int = 0               # tree columns where the engine differs from Firefox, whatever Chrome says
    oracle_refused: Counter = field(default_factory=Counter)             # (who, why) -> columns it cannot answer
    oracle_answered: Counter = field(default_factory=Counter)            # selector -> columns answered non-empty

    @functools.cached_property
    def rendered(self) -> list[str]:
        return [render(d) for d in self.docs.all]

    @property
    def basket_oracle_empty(self) -> list[str]:
        return [s for s in self.basket if not self.oracle_answered[s]]


def evenly(items: list, count: int) -> list:
    """About `count` of `items`, evenly spaced."""
    return items[::max(1, len(items) // max(1, count))][:count]


def self_check(run: Run) -> None:
    """The fast path is trusted only as far as the run re-measures it: a sample of the documents is
    page-loaded too, and the oracle records each one whose tree differs. `probe.cjs` decides which of the
    sample is eligible, so the rule has one copy. The value documents' sample carries their CSS, so the
    check covers the native answers as well as the trees."""
    sample = evenly(run.rendered, run.args.selfcheck)
    vsample = evenly(run.docs.value, run.args.selfcheck // 3)
    for oracle in run.oracles.values():
        if sample:
            oracle.trees(sample, selfcheck=True)
        if vsample:
            oracle.query([render(d) for d in vsample],
                         [[oracle_form(s) for s in value_selectors(d, True, run.basket)
                           if is_native(s) and s not in run.refused] for d in vsample],
                         selfcheck=True)


def grade_trees(run: Run, report: Report) -> list[tuple[Doc, str]]:
    """The fingerprint of every document, engine against Chrome and Chrome against Firefox. Returns the
    graded documents: those every browser loaded."""
    answers, firefox = run.answers, "firefox" in run.oracles
    graded = []
    for d, r in zip(run.docs.all, run.rendered):
        sels = seq_sweep.fingerprint_selectors(ids_of(d))
        if any(answers.trees[b][r] is None for b in run.oracles):
            continue
        answers.prefetch_engine({r: set(sels)})
        report.graded += 1
        graded.append((d, r))
        for s in sels:
            if answers.differs(("engine", "chrome"), r, s):
                run.engine_items.append(Divergence(d, s, ("engine", "chrome")))
            if firefox:
                if answers.differs(("chrome", "firefox"), r, s):
                    run.browser_items.append(Divergence(d, s, ("chrome", "firefox")))
                run.vs_firefox += answers.differs(("engine", "firefox"), r, s)
    return graded


def grade_values(run: Run, report: Report, graded: list[tuple[Doc, str]]) -> None:
    """The value columns of every graded document: CSS natively, XPath through the bridge, on the trees
    the fingerprint was read from."""
    answers, firefox = run.answers, "firefox" in run.oracles
    tree_divergent = {render(it.doc) for it in run.engine_items}
    decorated = run.docs.decorated
    wanted = {r: {s for s in value_selectors(d, r in decorated, run.basket) if s not in run.refused}
              for d, r in graded}
    answers.fetch_values(wanted)
    for d, r in graded:
        for s in sorted(wanted[r]):
            want = answers.value("chrome", r, s)
            if isinstance(want, Refused):
                run.oracle_refused[(want.by, want.reason[:90])] += 1
                continue
            report.value_graded += 1
            run.oracle_answered[s] += bool(want)
            if answers.value_differs(("engine", "chrome"), r, s):
                split_value(Divergence(d, s, ("engine", "chrome")), tree_divergent, report, run.value_items)
            if firefox and answers.value_differs(("chrome", "firefox"), r, s):
                run.browser_value_items.append(Divergence(d, s, ("chrome", "firefox")))
            if C.is_css(s) and not grouped_spec(s):
                mirror_check(answers, r, s, want, report)
    report.rule_never_answered = [s for s in RULE_SELECTORS if s not in run.refused and not run.oracle_answered[s]]
    report.rule_refused = [s for s in RULE_SELECTORS if s in run.refused]


def check_debug_build(run: Run, report: Report, graded: list[tuple[Doc, str]]) -> None:
    """Every graded document through a DEBUG build of the `differ` bridge, whose invariants are the builder's."""
    checked = [(r, seq_sweep.fingerprint_selectors(ids_of(d)) + RULE_SELECTORS) for d, r in graded]
    report.debug_panics = debug_panics(run.args.debug_differ, run.args.parser, checked)
    report.debug_checked = len(checked)


# ---------------------------------------------------------------- the report
def short(values: Optional[list[str]], width: int = 60) -> str:
    text = json.dumps(values)
    return text if len(text) <= width else text[:width - 3] + "..."


def example(d: Divergence, answers: Answers, *, value: bool = False) -> str:
    """`document  column  source=answer source=answer`, from the reduced document."""
    r = render(d.repro)
    a, b = (answers.value(src, r, d.sel) if value else answers.column(src, r, d.sel) for src in d.pair)
    return f"{json.dumps(r)}  {d.sel}  {d.pair[0]}={short(a)} {d.pair[1]}={short(b)}"


def by_shape(items: list[Divergence], plain: bool = False) -> dict[tuple[str, ...], list[Divergence]]:
    """Group reduced divergences by their token sequence; `plain` groups a tree divergence by its
    undecorated tokens, since a decoration is not what a tree construct is about."""
    groups: dict[tuple[str, ...], list[Divergence]] = defaultdict(list)
    for d in items:
        doc = undecorate(d.repro)[0] if plain else d.repro
        groups[tuple(t for _, t in doc)].append(d)
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), len(kv[0]), kv[0])))


def documents_in(items: list[Divergence]) -> int:
    return len({render(d.doc) for d in items})


def smallest(items: list[Divergence]) -> Divergence:
    return min(items, key=lambda d: (len(d.repro), render(d.repro)))


def table_row(name: str, items: list[Divergence], width: int, *, plain: bool, example: str = "") -> str:
    """One row of a construct table: columns, documents, reduced shapes, and the smallest example if given."""
    row = f"  {name:<{width}} {len(items):>8} {documents_in(items):>6} {len(by_shape(items, plain=plain)):>7}"
    return f"{row}  {example}" if example else row


def print_shapes(shapes: dict, answers: Answers, show: int, *, value: bool = False) -> None:
    """The first `show` reduced shapes, each with how many columns it explains and one example."""
    for _shape, items in itertools.islice(shapes.items(), show):
        print(f"    {len(items):>6}  {example(items[0], answers, value=value)}")


def print_report(run: Run, report: Report) -> None:
    args, answers, docs, phases = run.args, run.answers, run.docs, run.phases
    print(f"engine            : {run.engine.describe()}")
    for o in run.oracles.values():
        print(f"oracle            : {o.build} ({dict(o.paths)})")
    print(f"documents         : {len(docs.all)} ({len(docs.seq)} seq_sweep sequences up to depth {args.seq_depth}, "
          f"{len(docs.random)} random, {len(docs.value)} value of which {docs.tail} deferred-tail and "
          f"{docs.structured} structured, seed {args.seed}); graded {report.graded}")
    if args.random:
        print(f"random alphabet   : {len(ALPHABET)} tokens, each emitted {min(docs.used[t] for t in ALPHABET)} "
              f"to {max(docs.used[t] for t in ALPHABET)} times")
    if args.values:
        print(f"value tokens      : {len(VALUE_TOKENS)}, each emitted {min(docs.value_used[t] for t in VALUE_TOKENS)} "
              f"to {max(docs.value_used[t] for t in VALUE_TOKENS)} times")
    rate = answers.oracle_docs / answers.oracle_seconds if answers.oracle_seconds else 0.0
    print(f"oracle throughput : {answers.oracle_docs} trees in {answers.oracle_seconds:.1f}s = {rate:.0f} docs/s "
          f"(all browsers, reduction candidates included)")
    print(f"wall time         : grading {phases['grading']:.1f}s, values {phases['values']:.1f}s, "
          f"debug build {phases['debug build']:.1f}s, reduction {phases['reduction']:.1f}s")
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
    print_tree_report(run, report)
    print_value_report(run, report)


def print_tree_report(run: Run, report: Report) -> None:
    answers, show = run.answers, run.args.show
    print(f"divergent columns : {len(run.engine_items)} against Chrome, in {documents_in(run.engine_items)} documents")
    if "firefox" in run.oracles:
        print(f"                    {run.vs_firefox} against Firefox; {len(run.browser_items)} columns where the "
              f"browsers disagree (the engine is graded against Chrome there)")

    print(f"\n  {'construct (section 3)':<34} {'columns':>8} {'docs':>6} {'shapes':>7}  smallest example")
    for name, items in sorted(report.buckets.items(), key=lambda kv: -len(kv[1])):
        print(table_row(name, items, 34, plain=True, example=example(smallest(items), answers)))
    print(table_row("NOVEL", report.novel, 34, plain=True))
    shapes = by_shape(report.novel, plain=True)
    if shapes:
        print(f"\n  NOVEL, by reduced shape (no section 3 construct explains these; first {show}):")
        print_shapes(shapes, answers, show)
    if run.browser_items:
        shapes = by_shape(run.browser_items, plain=True)
        print(f"\n  Chrome versus Firefox: {len(run.browser_items)} columns in {documents_in(run.browser_items)} "
              f"documents, {len(shapes)} reduced shapes")
        print_shapes(shapes, answers, show)


def print_value_report(run: Run, report: Report) -> None:
    """The value verdict, split by whether the tree already explains each difference."""
    answers, show = run.answers, run.args.show
    print(f"\nVALUE COLUMNS     : {report.value_graded} graded against Chrome ({len(run.basket)} basket selectors, "
          f"{len(RULE_SELECTORS)} value-rule selectors, per-id columns on every document)")
    if run.refused:
        print(f"  refused by the engine in {run.args.parser} mode (an empty column by contract, not graded): "
              f"{len(run.refused)}, e.g. {sorted(run.refused)[:4]}")
    if run.oracle_refused:
        print(f"  not answerable by the oracle (never graded): {sum(run.oracle_refused.values())} columns")
        for (by, why), n in run.oracle_refused.most_common(6):
            print(f"    {n:>6}  [{by}] {why}")
    if run.basket_oracle_empty:
        print(f"  basket selectors the oracle answered nowhere (they can only catch an over-match): "
              f"{len(run.basket_oracle_empty)}")
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
          f"{len(report.value_on_tree)} columns in {documents_in(report.value_on_tree)} documents")
    for name, n in on_tree.most_common():
        print(f"    {n:>8}  under {name}")
    unseen = sum(len(v) for v in report.value_unseen_tree.values())
    print(f"  TREE the fingerprint cannot see (it agrees, but the column is about an id-less element it has no "
          f"column for): {unseen}")
    for name, items in sorted(report.value_unseen_tree.items(), key=lambda kv: -len(kv[1])):
        print(f"    {len(items):>8}  under {name:<30} {example(smallest(items), answers, value=True)}")
    print("  VALUE (the fingerprint agrees):")
    print(f"  {'value construct':<46} {'columns':>8} {'docs':>6} {'shapes':>7}  smallest example")
    for name, items in sorted(report.value_buckets.items(), key=lambda kv: -len(kv[1])):
        print(table_row(name, items, 46, plain=False, example=example(smallest(items), answers, value=True)))
    print(table_row("value NOVEL", report.value_novel, 46, plain=False))
    shapes = by_shape(report.value_novel)
    if shapes:
        print(f"\n  value NOVEL, by reduced shape (first {show}):")
        print_shapes(shapes, answers, show, value=True)
    if run.browser_value_items:
        shapes = by_shape(run.browser_value_items)
        print(f"\n  Chrome versus Firefox, value columns: {len(run.browser_value_items)} in {len(shapes)} "
              f"reduced shapes")
        print_shapes(shapes, answers, show, value=True)


def shape_rows(shapes: dict) -> list[dict[str, Any]]:
    """One row per reduced shape for the results file: its first repro, column and how many columns it explains."""
    return [{"repro": render(items[0].repro), "column": items[0].sel, "count": len(items)}
            for items in shapes.values()]


def write_results(run: Run, report: Report) -> None:
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    with open(RESULTS, "w", encoding="utf-8") as f:
        json.dump({
            "engine": run.engine.describe(), "browsers": {o.browser: o.build for o in run.oracles.values()},
            "graded": report.graded, "seed": run.args.seed,
            "buckets": {k: len(v) for k, v in report.buckets.items()},
            "novel": shape_rows(by_shape(report.novel, plain=True)),
            "chrome_vs_firefox": shape_rows(by_shape(run.browser_items, plain=True)),
            "oracle_errors": report.oracle_errors, "selfcheck_differ": report.selfcheck_differ,
            "values": {
                "graded": report.value_graded, "on_tree": len(report.value_on_tree),
                "unseen_tree": {k: len(v) for k, v in report.value_unseen_tree.items()},
                "buckets": {k: len(v) for k, v in report.value_buckets.items()},
                "novel": shape_rows(by_shape(report.value_novel)),
                "engine_refused": sorted(run.refused), "basket_oracle_empty": run.basket_oracle_empty,
            },
            "debug": {"checked": report.debug_checked, "panics": report.debug_panics[:20]},
            "bridge": {"checked": report.bridge_checked, "failures": report.bridge_failures[:20],
                       "unrepresentable": report.bridge_unrepresentable[:20],
                       "xpath_differ": report.bridge_xpath_differ[:20]},
        }, f, indent=1)
    print(f"\n  wrote {os.path.relpath(RESULTS)}")


# ---------------------------------------------------------------- the entry point
def parse_args() -> argparse.Namespace:
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
    args.browsers = [b.strip() for b in args.browsers.split(",") if b.strip()]
    if not args.browsers or args.browsers[0] != "chrome":
        ap.error("--browsers must start with chrome: browser mode follows Chrome where the two disagree")
    return args


def value_basket(docs: Documents, parser: str) -> tuple[list[str], dict[str, str]]:
    """conformant.py's selectors minus what the engine refuses in this mode, and the refusals over every
    value selector the run will ask."""
    basket = list(dict.fromkeys(conformant.BASKET))
    decorated = docs.decorated
    every = sorted({s for d in docs.all for s in value_selectors(d, render(d) in decorated, basket)})
    refused = engine_refusals(every, parser)
    return [s for s in basket if s not in refused], refused


def main() -> int:
    args = parse_args()
    engine = Engine(args.parser)
    docs = generate(args.seq_depth, args.random, args.values, args.seed)
    report = Report(missing_tokens=docs.missing_tokens, missing_value_tokens=docs.missing_value_tokens,
                    debug_requested=bool(args.debug_differ))
    basket, refused = value_basket(docs, args.parser)

    phases = Phases()
    with contextlib.ExitStack() as stack:
        oracles = {b: stack.enter_context(BrowserOracle(b, tabs=args.tabs)) for b in args.browsers}
        run = Run(args, engine, docs, oracles, Answers(engine, oracles), basket, refused, phases)
        run.answers.fetch(run.rendered)
        self_check(run)
        graded = grade_trees(run, report)
        check_bridge(run.answers, args.browsers, graded, report)
        phases.mark("grading")
        grade_values(run, report, graded)
        phases.mark("values")
        if args.debug_differ:
            check_debug_build(run, report, graded)
        phases.mark("debug build")
        reduce_all(run.engine_items + run.browser_items, run.answers)
        reduce_values(run.value_items + run.browser_value_items, run.answers)
        phases.mark("reduction")
        attribute_all(run.engine_items, run.answers, report)
        attribute_values(run.value_items, run.answers, report)

    for o in oracles.values():
        report.oracle_errors += [{"browser": o.browser, **e} for e in o.errors]
        report.selfcheck_checked += o.selfcheck_checked
        report.selfcheck_differ += o.selfcheck_differ
        report.selfcheck_css_differ += o.selfcheck_css_differ

    print_report(run, report)
    write_results(run, report)
    if args.gate:
        fails = gate_failures(report)
        for why in fails:
            print(f"  GATE: {why}")
        print(f"  GATE: {'FAIL' if fails else 'PASS'}")
        return 1 if fails else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
