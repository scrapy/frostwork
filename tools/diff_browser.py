"""
tools/diff_browser.py: the browser differential, Frostwork against the tree a real browser builds.

The oracle for browser mode is the browser (docs/BROWSER_MODE.md, section 5), driven through
`tools/browser_oracle/client.py`. Each document is graded on `seq_sweep`'s whole-tree fingerprint: the
engine answers the fingerprint's XPath columns, and `tools/browser_oracle/tree.py` computes the same
columns natively from the browser's own JSON tree, so nothing is rebuilt in lxml and nothing the
browser keeps is lost.

Documents come from two generators, and every element token carries a unique id so the fingerprint can
see it:

  * every sequence of `seq_sweep.ALPHABET` up to `--seq-depth`, the frame and table shapes lxml mode's
    sequence gate enumerates;
  * `--random` seeded sequences over `ALPHABET` below, the probe's HTML5-heavy alphabet plus the
    section 3 constructs it does not reach. Half carry a doctype. The run refuses to grade unless every
    token is emitted.

Until browser mode implements a construct, its divergences are expected. So every differing column is
REDUCED to the smallest token subsequence that still differs on that column (ids are kept, so the column
still names the same element), and the reduced document, not the page it came from, is attributed to a
construct from section 3's table. Page-scoped attribution over-credits (AGENTS.md, the truncated-tag
lesson); a two-token repro has nowhere to hide. Anything no construct explains is NOVEL.

`--gate` fails on NOVEL > 0, and on coverage: no document graded, a token never emitted, an oracle error
(a failed page load), or a self-check where `DOMParser` and a page load built different trees for a
document the fast path was trusted with.

With `--browsers chrome,firefox`, Firefox is run too. Where the two browsers agree that is the target;
where they disagree the case goes in a Chrome-versus-Firefox table of its own (also reduced), and the
engine is graded against Chrome, which browser mode follows.

Usage:
  .venv/bin/python tools/diff_browser.py                          # seq depth 2 + 2000 random, Chrome
  .venv/bin/python tools/diff_browser.py --browsers chrome,firefox --seq-depth 3 --random 20000
  .venv/bin/python tools/diff_browser.py --parser lxml --gate
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seq_sweep  # noqa: E402  (the fingerprint and the short-sequence alphabet)
from browser_oracle import tree as T  # noqa: E402
from browser_oracle.client import BrowserOracle  # noqa: E402

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "browserdiff.json")

DOCTYPE = "<!DOCTYPE html>"
# The probe's 70-token alphabet (tools/browser_oracle/probe.cjs, TOKENS) with an id slot on every start
# tag, plus the section 3 constructs it does not reach: `<noscript>`, a `<p>` closer HTML5 adds
# (`<section>`), a newline for the leading-newline rule, a VT for the strip, NUL, and `<col>`.
ALPHABET: list[str] = [
    "<p{}>", "</p>", "<div{}>", "</div>", "<table{}>", "</table>", "<tr{}>", "<td{}>", "</td>", "<b{}>",
    "</b>", "<i{}>", "</i>", "<a{} href=x>", "</a>", "<li{}>", "<ul{}>", "<select{}>", "<option{}>",
    "</select>", "<template{}>", "</template>", "t", "<br{}>", "</br>", "<form{}>", "</form>", "<body{}>",
    "</body>", "<html{}>", "</html>", "<head{}>", "<title{}>T</title>", "<svg{}>", "</svg>", "<math{}>",
    "<!--c-->", "<button{}>", "<h1{}>", "<dd{}>", "<dt{}>", "<caption{}>", "<colgroup{}>", "<frameset{}>",
    "<pre{}>", "<textarea{}>", "<script{}>s</script>", "<style{}>s</style>", "<image{}>", "<nobr{}>",
    "<marquee{}>", "<object{}>", "<input{}>", "<hr{}>", "<![CDATA[x]]>", "<?pi?>", "<p{}/>", "<div{}/>",
    "<frame{}>", "<dl{}>", "<tbody{}>", "<th{}>", "<title{}>", "<foreignObject{}>", "<desc{}>", "<mi{}>",
    '<annotation-xml{} encoding="text/html">', "<template{} shadowrootmode=open>",
    "<template{} shadowrootmode=closed>", "<selectedcontent{}>",
    "<noscript{}>", "<section{}>", "\n", "\x0b", "\x00", "<col{}>",
]
MAX_TOKENS = 7

# A document is a tuple of (id, token template). The id is the token's position in the ORIGINAL
# document and survives reduction, so a reduced document's columns still name the same elements.
Doc = tuple[tuple[int, str], ...]


def _start_tag(token: str) -> bool:
    return token.startswith("<") and len(token) > 1 and token[1].isalpha()


for _t in ALPHABET + seq_sweep.ALPHABET:
    assert not _start_tag(_t) or "{}" in _t, f"{_t!r} is a start tag with no id slot; the fingerprint cannot see it"


def render(doc: Doc) -> str:
    return "".join(tok.format(f' id="{i}"') if "{}" in tok else tok for i, tok in doc)


def ids_of(doc: Doc) -> list[str]:
    return [str(i) for i, tok in doc if "{}" in tok]


def seq_docs(depth: int) -> list[Doc]:
    return [tuple(enumerate(seq)) for n in range(1, depth + 1)
            for seq in itertools.product(seq_sweep.ALPHABET, repeat=n)]


def random_docs(n: int, seed: int) -> tuple[list[Doc], Counter]:
    """`n` documents of 1 to `MAX_TOKENS` tokens; every other one starts with a doctype."""
    rng, used, docs = random.Random(seed), Counter[str](), []
    for k in range(n):
        toks = [rng.choice(ALPHABET) for _ in range(1 + rng.randrange(MAX_TOKENS))]
        used.update(toks)
        docs.append(tuple(enumerate(([DOCTYPE] if k % 2 else []) + toks)))
    return docs, used


# ---------------------------------------------------------------- the engine
MAX_SELECTORS_PER_PASS = 96  # the engine's member budget, as in diff_lxml


def engine_runner(parser: str) -> tuple[Callable[[bytes, list[str]], list[list[str]]], str]:
    import frostwork

    mode = seq_sweep.parser_kwargs(parser)

    def run(doc: bytes, sels: list[str]) -> list[list[str]]:
        return frostwork.extract(doc, sels, "utf-8", **mode)

    if mode:
        return run, f"frostwork.extract(..., parser={parser!r})"
    note = "" if parser == "lxml" else f"; --parser {parser} was NOT applied"
    return run, f"frostwork.extract without parser= (the installed extension has no such keyword, so lxml mode{note})"


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


# ---------------------------------------------------------------- attribution
def tag_name(token: str) -> str:
    """`<a{} href=x>` -> `a`, `</p>` -> `p`; lowercased, as the tokenizer reads it."""
    body = token[2:] if token.startswith("</") else token[1:]
    return body.split("{", 1)[0].split(">", 1)[0].split("/", 1)[0].split(" ", 1)[0].lower()


FORMATTING = {"a", "b", "big", "code", "em", "font", "i", "nobr", "s", "small", "strike", "strong", "tt", "u"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Start tags that close an open <p> in HTML5 ("close a p element" in the in-body rules).
P_CLOSERS = {"address", "article", "aside", "blockquote", "center", "details", "dialog", "dir", "div", "dl",
             "fieldset", "figcaption", "figure", "footer", "header", "hgroup", "main", "menu", "nav", "ol",
             "p", "search", "section", "summary", "ul", "pre", "listing", "form", "plaintext", "table",
             "hr", "xmp", "li", "dd", "dt", *HEADINGS}
# What a table can hold without fostering it out ("in table" and its sub-modes).
TABLE_CONTENT = {"table", "caption", "colgroup", "col", "tbody", "thead", "tfoot", "tr", "td", "th",
                 "template", "script", "style", "form"}
# HTML integration points and MathML text integration points, by the foreign root they belong to.
INTEGRATION_POINTS = {"svg": {"title", "desc", "foreignobject"},
                      "math": {"mi", "mo", "mn", "ms", "mtext", "annotation-xml"}}
# Tokens that leave the frame where it is: they can come before an explicit <body> without opening one.
FRAME_NEUTRAL = {"<!--c-->", "\n", "</%>", "</>", "<?pi?>"}


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

    @property
    def tokens(self) -> list[str]:
        return [t for _, t in self.doc]

    def starts(self) -> list[str]:
        return [tag_name(t) for t in self.tokens if _start_tag(t)]

    def ends(self) -> list[str]:
        return [tag_name(t) for t in self.tokens if t.startswith("</") and len(t) > 3]

    def has_doctype(self) -> bool:
        return any(t.lower().startswith("<!doctype") for t in self.tokens)

    def with_doctype(self) -> Doc:
        return ((-1, DOCTYPE),) + self.doc

    def with_head(self) -> Doc:
        lead = 1 if self.doc and self.doc[0][1].lower().startswith("<!doctype") else 0
        return self.doc[:lead] + ((-1, "<head></head>"),) + self.doc[lead:]

    def deslashed(self) -> Doc:
        return tuple((i, t.replace("{}/>", "{}>")) for i, t in self.doc)

    def counts(self, names: set[str]) -> int:
        return sum(n in names for n in self.starts())


def _strip(r: Repro) -> bool:
    edges = {r.tokens[0], r.tokens[-1]}
    return bool(edges & {"\x0b", "\n"})


def _foreign_integration(r: Repro) -> bool:
    """An integration point as the very next token after its own foreign root, with content after it."""
    toks = r.tokens
    return any(_start_tag(t) and tag_name(t) in INTEGRATION_POINTS and _start_tag(toks[k + 1])
               and tag_name(toks[k + 1]) in INTEGRATION_POINTS[tag_name(t)] and k + 2 < len(toks)
               for k, t in enumerate(toks[:-1]))


def _cdata(r: Repro) -> bool:
    toks = r.tokens
    return any(t == "<![CDATA[x]]>" and any(tag_name(u) in ("svg", "math") for u in toks[:k] if _start_tag(u))
               for k, t in enumerate(toks))


def _leading_newline(r: Repro) -> bool:
    toks = r.tokens
    return any(tag_name(t) in ("pre", "textarea", "listing") and _start_tag(t) and toks[k + 1].startswith("\n")
               for k, t in enumerate(toks[:-1]))


def _form_pointer(r: Repro) -> bool:
    s = r.starts()
    if "form" not in s:
        return False
    return s.count("form") > 1 or "form" in r.ends() or ("table" in s and s.index("table") < s.index("form"))


def _after_frame_end(r: Repro) -> bool:
    toks = r.tokens
    return any(t in ("</body>", "</html>") for t in toks[:-1])


def _second_frame_tag(r: Repro) -> bool:
    """A `<body>` after body content, or an `<html>` after anything: the browser merges its attributes."""
    toks = r.tokens
    for k, t in enumerate(toks):
        if not _start_tag(t) or tag_name(t) not in ("html", "body"):
            continue
        before = [u for u in toks[:k] if u not in FRAME_NEUTRAL and not u.lower().startswith("<!doctype")]
        if tag_name(t) == "html" and before:
            return True
        if tag_name(t) == "body" and any(tag_name(u) not in ("html", "head") or not _start_tag(u) for u in before
                                         if u != "</head>"):
            return True
    return False


def _implied_tbody(r: Repro) -> bool:
    s = r.starts()
    for k, n in enumerate(s):
        if n == "table":
            rest = s[k + 1:]
            for m in rest:
                if m == "tbody":
                    break
                if m in ("tr", "td", "th"):
                    return True
    return False


def _clone(r: Repro) -> bool:
    return len(set(r.browser_ids)) < len(r.browser_ids)


def _misnested_end(r: Repro) -> bool:
    """A formatting element's end tag with a non-formatting element opened inside it, or a second `<a>`."""
    toks = r.tokens
    if r.starts().count("a") > 1:
        return True
    for k, t in enumerate(toks):
        if _start_tag(t) and tag_name(t) in FORMATTING:
            for j in range(k + 1, len(toks)):
                u = toks[j]
                if u == f"</{tag_name(t)}>":
                    if any(_start_tag(v) and tag_name(v) not in FORMATTING for v in toks[k + 1:j]):
                        return True
                    break
    return False


def _foster(r: Repro) -> bool:
    toks = r.tokens
    for k, t in enumerate(toks):
        if not (_start_tag(t) and tag_name(t) == "table"):
            continue
        for u in toks[k + 1:]:
            if u in ("</table>",) or (_start_tag(u) and tag_name(u) in ("td", "th", "caption")):
                break
            if u == "t" or u == "x" or (_start_tag(u) and tag_name(u) not in TABLE_CONTENT):
                return True
    return False


def _stray_end_p(r: Repro) -> bool:
    """A `</p>` with no `<p>` before it, in HTML content (in foreign content it breaks out instead)."""
    toks = r.tokens
    return any(t == "</p>" and not any(_start_tag(u) and tag_name(u) in ("p", "svg", "math") for u in toks[:k])
               for k, t in enumerate(toks))


def _deep_p(r: Repro) -> bool:
    s = r.starts()
    return any(n == "p" and any(m not in P_CLOSERS for m in s[k + 1:j]) and s[j] in P_CLOSERS
               for k, n in enumerate(s) for j in range(k + 2, len(s)))


def _p_closer(r: Repro) -> bool:
    s = r.starts()
    return any(n == "p" and s[k + 1] in P_CLOSERS for k, n in enumerate(s[:-1]))


@dataclass(frozen=True)
class Construct:
    name: str                       # the section 3 row it stands for
    test: Callable[[Repro], bool]   # does this smallest repro exhibit it?


# Section 3's table, in the order the rules are tried. Rules triggered by a token nothing else reads
# come first. The counterfactual rules come next, because they are decided by the oracle rather than by
# a shape: if an explicit `<head></head>` makes the column agree, the missing head is the cause whatever
# else the repro holds. The shape rules follow, and foster parenting and the <p> rules, whose shapes are
# the loosest, come last.
CONSTRUCTS: list[Construct] = [
    Construct("NUL", lambda r: "\x00" in r.tokens),
    Construct("leading strip", _strip),
    Construct("<noscript>", lambda r: "noscript" in r.starts()),
    Construct("declarative shadow root", lambda r: any("shadowrootmode" in t for t in r.tokens)),
    Construct("HTML <template>", lambda r: "template" in r.starts()),
    Construct("frameset replacing the body", lambda r: "frameset" in r.starts()),
    Construct("CDATA in foreign content", _cdata),
    Construct("SVG <title> (integration point)", _foreign_integration),
    Construct("quirks-mode tree", lambda r: not r.has_doctype() and not r.differs(r.with_doctype())),
    Construct("<head>", lambda r: "head" not in r.starts() and not r.differs(r.with_head())),
    Construct("/> on a non-void tag", lambda r: any("{}/>" in t for t in r.tokens) and not r.differs(r.deslashed())),
    Construct("renames", lambda r: "</br>" in r.tokens or "image" in r.starts()),
    Construct("leading newline", _leading_newline),
    Construct("form pointer", _form_pointer),
    Construct("dt/dd repeat", lambda r: r.counts({"dt", "dd"}) > 1),
    Construct("nested button/h1/nobr", lambda r: r.counts({"button"}) > 1 or r.counts(HEADINGS) > 1
              or r.counts({"nobr"}) > 1),
    Construct("after </html>", _after_frame_end),
    Construct("second <html>/<body> tag", _second_frame_tag),
    Construct("implied <tbody>", _implied_tbody),
    Construct("adoption agency", lambda r: _misnested_end(r)),
    Construct("formatting reconstruction", _clone),
    Construct("foster parenting", _foster),
    Construct("stray </p>", _stray_end_p),
    Construct("deep <p>", _deep_p),
    Construct("<p> closers", _p_closer),
]


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
    return fails


def repro_of(d: Divergence, answers: Answers) -> Repro:
    return Repro(d.repro, d.sel, lambda doc: answers.differs(d.pair, render(doc), d.sel),
                 answers.get(d.pair[1], render(d.repro), "//*/@id") or [])


def attribute_all(items: list[Divergence], answers: Answers, report: Report) -> None:
    """File each reduced divergence under its construct, fetching every counterfactual in one batch."""
    repros = [repro_of(d, answers) for d in items]
    wanted: dict[str, set[str]] = defaultdict(set)
    for d, r in zip(items, repros):
        for doc in counterfactuals(r):
            wanted[render(doc)].add(d.sel)
    answers.fetch(set(wanted))
    answers.prefetch_engine(wanted)
    for d, r in zip(items, repros):
        name = attribute(r)
        if name is None:
            report.novel.append(d)
        else:
            report.buckets.setdefault(name, []).append(d)


# ---------------------------------------------------------------- the run
def short(values: Optional[list[str]], width: int = 60) -> str:
    text = json.dumps(values)
    return text if len(text) <= width else text[:width - 3] + "..."


def example_line(d: Divergence, answers: Answers) -> str:
    r = render(d.repro)
    a, b = (answers.get(src, r, d.sel) for src in d.pair)
    return f"{json.dumps(r)}  {d.sel}  {d.pair[0]}={short(a)} {d.pair[1]}={short(b)}"


def by_shape(items: list[Divergence]) -> dict[tuple[str, ...], list[Divergence]]:
    groups: dict[tuple[str, ...], list[Divergence]] = defaultdict(list)
    for d in items:
        groups[tuple(t for _, t in d.repro)].append(d)
    return dict(sorted(groups.items(), key=lambda kv: (-len(kv[1]), len(kv[0]), kv[0])))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--browsers", default="chrome", help="comma-separated: chrome, firefox (chrome first)")
    ap.add_argument("--parser", default="browser", help="the engine's parse mode, if the extension accepts parser=")
    ap.add_argument("--seq-depth", type=int, default=2, help="every seq_sweep sequence up to this length")
    ap.add_argument("--random", type=int, default=2000, help="random documents over ALPHABET")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--selfcheck", type=int, default=300,
                    help="documents sent for the self-check: each fast-path one is also page-loaded, "
                         "and its two trees must be the same")
    ap.add_argument("--tabs", type=int, default=8, help="page-load tabs per browser")
    ap.add_argument("--show", type=int, default=25, help="NOVEL shapes to print")
    ap.add_argument("--gate", action="store_true", help="exit nonzero on NOVEL or a coverage failure")
    args = ap.parse_args()

    browsers = [b.strip() for b in args.browsers.split(",") if b.strip()]
    if not browsers or browsers[0] != "chrome":
        ap.error("--browsers must start with chrome: browser mode follows Chrome where the two disagree")
    run_engine, engine_desc = engine_runner(args.parser)

    docs = seq_docs(args.seq_depth)
    rdocs, used = random_docs(args.random, args.seed)
    docs += rdocs
    report = Report(missing_tokens=[t for t in ALPHABET if args.random and not used[t]])

    oracles: dict[str, BrowserOracle] = {}
    t_start = time.perf_counter()
    try:
        for b in browsers:
            oracles[b] = BrowserOracle(b, tabs=args.tabs).__enter__()
        answers = Answers(run_engine, oracles)
        rendered_all = [render(d) for d in docs]
        answers.fetch(rendered_all)

        # The fast path is trusted only as far as this run re-measures it. probe.cjs decides which of the
        # sample is eligible, so the rule has one copy.
        step = max(1, len(rendered_all) // max(1, args.selfcheck))
        sample = rendered_all[::step][:args.selfcheck]
        for oracle in oracles.values():
            if sample:
                oracle.trees(sample, selfcheck=True)

        engine_items: list[Divergence] = []
        browser_items: list[Divergence] = []
        vs_firefox = 0  # columns where the engine differs from Firefox, whatever Chrome says
        for d, r in zip(docs, rendered_all):
            sels = seq_sweep.fingerprint_selectors(ids_of(d))
            if any(answers.trees[b][r] is None for b in browsers):
                continue
            answers.prefetch_engine({r: set(sels)})
            report.graded += 1
            for s in sels:
                if answers.differs(("engine", "chrome"), r, s):
                    engine_items.append(Divergence(d, s, ("engine", "chrome")))
                if "firefox" in oracles:
                    if answers.differs(("chrome", "firefox"), r, s):
                        browser_items.append(Divergence(d, s, ("chrome", "firefox")))
                    vs_firefox += answers.differs(("engine", "firefox"), r, s)
        t_graded = time.perf_counter()
        reduce_all(engine_items + browser_items, answers)
        t_reduced = time.perf_counter()
        attribute_all(engine_items, answers, report)
    finally:
        for oracle in oracles.values():
            oracle.close()

    for o in oracles.values():
        report.oracle_errors += [{"browser": o.browser, **e} for e in o.errors]
        report.selfcheck_checked += o.selfcheck_checked
        report.selfcheck_differ += o.selfcheck_differ

    # ------------------------------------------------------------ the report
    print(f"engine            : {engine_desc}")
    for o in oracles.values():
        print(f"oracle            : {o.build} ({dict(o.paths)})")
    print(f"documents         : {len(docs)} ({len(docs) - len(rdocs)} seq_sweep sequences up to depth "
          f"{args.seq_depth}, {len(rdocs)} random, seed {args.seed}); graded {report.graded}")
    if args.random:
        print(f"random alphabet   : {len(ALPHABET)} tokens, each emitted {min(used[t] for t in ALPHABET)} "
              f"to {max(used[t] for t in ALPHABET)} times")
    rate = answers.oracle_docs / answers.oracle_seconds if answers.oracle_seconds else 0.0
    print(f"oracle throughput : {answers.oracle_docs} trees in {answers.oracle_seconds:.1f}s = {rate:.0f} docs/s "
          f"(all browsers, reduction candidates included)")
    print(f"wall time         : grading {t_graded - t_start:.1f}s, reduction {t_reduced - t_graded:.1f}s")
    print(f"self-check        : {report.selfcheck_checked} fast-path documents page-loaded, "
          f"{len(report.selfcheck_differ)} differ")
    print(f"divergent columns : {len(engine_items)} against Chrome, in "
          f"{len({render(d.doc) for d in engine_items})} documents")
    if "firefox" in oracles:
        print(f"                    {vs_firefox} against Firefox; {len(browser_items)} columns where the "
              f"browsers disagree (the engine is graded against Chrome there)")

    print(f"\n  {'construct (section 3)':<34} {'columns':>8} {'docs':>6} {'shapes':>7}  smallest example")
    for name, items in sorted(report.buckets.items(), key=lambda kv: -len(kv[1])):
        ex = min(items, key=lambda d: (len(d.repro), render(d.repro)))
        print(f"  {name:<34} {len(items):>8} {len({render(d.doc) for d in items}):>6} "
              f"{len(by_shape(items)):>7}  {example_line(ex, answers)}")
    shapes = by_shape(report.novel)
    print(f"  {'NOVEL':<34} {len(report.novel):>8} {len({render(d.doc) for d in report.novel}):>6} "
          f"{len(shapes):>7}")
    if shapes:
        print(f"\n  NOVEL, by reduced shape (no section 3 construct explains these; first {args.show}):")
        for shape, items in itertools.islice(shapes.items(), args.show):
            print(f"    {len(items):>6}  {example_line(items[0], answers)}")

    if browser_items:
        bshapes = by_shape(browser_items)
        print(f"\n  Chrome versus Firefox: {len(browser_items)} columns in "
              f"{len({render(d.doc) for d in browser_items})} documents, {len(bshapes)} reduced shapes")
        for shape, items in itertools.islice(bshapes.items(), args.show):
            print(f"    {len(items):>6}  {example_line(items[0], answers)}")

    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    with open(RESULTS, "w", encoding="utf-8") as f:
        json.dump({
            "engine": engine_desc, "browsers": {o.browser: o.build for o in oracles.values()},
            "graded": report.graded, "seed": args.seed,
            "buckets": {k: len(v) for k, v in report.buckets.items()},
            "novel": [{"repro": render(items[0].repro), "column": items[0].sel, "count": len(items)}
                      for items in shapes.values()],
            "chrome_vs_firefox": [{"repro": render(items[0].repro), "column": items[0].sel, "count": len(items)}
                                  for items in by_shape(browser_items).values()],
            "oracle_errors": report.oracle_errors, "selfcheck_differ": report.selfcheck_differ,
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
