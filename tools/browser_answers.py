"""
tools/browser_answers.py: what the engine and a browser each answer for a column, and when two answers agree.

A column is a selector asked of one document. `Engine` is frostwork in one parse mode and `Answers` holds
every source's answer to every (document, column) a run asks, computed on demand: the engine's, and each
browser's, which is native for the fingerprint's XPath (`browser_oracle/tree.py`), native CSS for a value
column's CSS (`browser_oracle/css.py`) and the lossless lxml bridge for its XPath (`browser_oracle/bridge.py`).
`same_values` is the one meaning of AGREE for a value column, shared by `diff_browser.py` and
`corpus_browser.py`, and `engine_refusals` the one list of what the engine refuses in a mode.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import diff_lxml  # noqa: E402  (the engine's member budget, and what a bare-element selector is)
import seq_sweep  # noqa: E402  (the fingerprint and the parse-mode probe)
import webpoet_structure  # noqa: E402  (the one structural comparison for outer HTML)
from browser_docs import grouped_spec  # noqa: E402
from browser_oracle import bridge as Br  # noqa: E402
from browser_oracle import css as C  # noqa: E402
from browser_oracle import tree as T  # noqa: E402
from browser_oracle.client import BrowserOracle, Refused  # noqa: E402


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
