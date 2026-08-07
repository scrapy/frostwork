"""
Competitive benchmark: Frostwork against the other fast HTML parsers used in scraping, over a real
production-selector corpus (the Zyte corpus layout `bench_corpus.py` documents).

`bench_corpus.py` answers "how much faster than what you run today". This answers the harder
question — "how much faster than the fastest thing available" — and it is a different question,
because Parsel is not the fast end of the field. selectolax/lexbor is.

Engines (each parses once per page, then answers one query per field — every one's real reuse
pattern):

  frostwork            `frostwork.extract`: ONE streaming pass, no DOM. Re-parses the selector
                       strings on every call, which is the pessimistic case; it is what
                       `bench_corpus.py` measures, so it stays the headline row.
  frostwork-plan       the same engine through a `Plan` compiled once per page object — what
                       `frostwork.Page`/`FrostPage` actually do.
  parsel               the incumbent, and this file's PARITY ORACLE.
  lxml+cssselect       parsel's tree and translation without parsel's Python wrapper. Without this
                       row, "N× faster than Parsel" could be N× faster than a wrapper rather than
                       than libxml2.
  selectolax-lexbor    lexbor: the fastest CSS-selector HTML parser in Python scraping.
  bs4+lxml             BeautifulSoup over the lxml tree with soupsieve's CSS — what most scrapers
                       actually run.

WHAT MAKES THIS A BENCHMARK RATHER THAN A BROCHURE

1. Nothing is timed before its values are checked. Every engine's every column is compared against
   Parsel with `diff_lxml.verdict` — the differential gate's own comparator, imported, not a second
   copy of it (a second comparator is a second standard, and this repo has paid for that twice).
   lexbor is an HTML5 parser and libxml2 is not, so on real malformed pages they disagree; how often
   they disagree ON PRODUCTION SELECTORS is a number this file exists to print.

2. An engine that cannot express a selector does not get a cheaper workload, and no engine is timed on
   a column it gets wrong. Two timed scopes, identical rules, differing only in who is in them:

     W-all     the columns the three full-coverage engines (frostwork, parsel, lxml) all express and
               all agree on — the widest realistic workload.
     W-common  the same for EVERY engine, so all six rows are timed on literally the same columns.

   Filtering W-all on parity too is not belt-and-braces: the first full run had the raw-lxml row
   returning an empty column for every `normalize-space(…)` selector, and without the filter that row
   would have been timed doing less work than the ones beside it.

   `workload_columns` is the one decision function behind both, and `assert_same_work` re-checks the
   invariant at runtime. Coverage — the fraction of the corpus's real selectors each engine can even
   express — is reported on its own, because it is half of what "can I use this?" means.

3. A missing library is reported by name with the reason. "0 engines skipped" must not be how an
   absent competitor looks.

SELECTOR TRANSLATION is the fairness crux. Parsel's `::text` / `::attr(name)` are not CSS; selectolax
and bs4 need them split off and re-implemented, and getting that wrong is how a competitor gets timed
doing less work. Note that `X::text` is the CHILD text nodes of X (`/text()`) while `X ::text` and
`X *::text` are the descendant ones — the mode is read off Parsel's own translator rather than guessed
from the string, and the parity check in (1) is what proves each adapter got it right.

The CSS-only engines are then driven the way their users drive them: match elements, read text or an
attribute off each match, in match order. That is NOT lxml's XPath node-set semantics, which dedupe
and re-sort into document order — so where matches nest, or a comma list interleaves two branches, the
answers differ. Those columns diverge, drop out of the shared workload and are counted. Reproducing
node-set order on top of a CSS engine would be a fairer *parser* comparison and a dishonest *library*
one, because no scraper writes that loop.

IMPORTANT: build the extension in RELEASE first — `.venv/bin/maturin develop --release` — or the
frostwork rows measure a debug build (~10× slower, and unfair vs optimized libxml2). `make
bench-engines` does it for you.

Usage:
  .venv/bin/python tools/bench_engines.py <corpus_dir> [--limit N] [--repeats R] [--engines a,b]
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
sys.path.insert(0, HERE)

from lxml import etree  # noqa: E402
from parsel.csstranslator import HTMLTranslator  # noqa: E402

import frostwork  # noqa: E402
from frostwork._frostwork import Plan  # noqa: E402

# The differential gate's comparator and the corpus bench's severity/timing helpers — imported, so
# "AGREE" here means exactly what it means there, including the raw-source allowance for bare-element
# columns (frostwork returns source, lxml reflows, lexbor serializes its own way; all three are
# compared by re-parse equivalence on non-whitespace text).
from diff_lxml import MAX_SELECTORS_PER_PASS, _batches, is_xpath, verdict  # noqa: E402
from bench_corpus import best_of, divergence_kind, parsel_extract  # noqa: E402

_TR = HTMLTranslator()

# ------------------------------------------------------------------ selector terminals
# A parsel selector is `<css><terminal>` where the terminal is one of `::text`, `::attr(name)`, or
# nothing (a bare element -> outer HTML). The CSS half is what selectolax/bs4 can run; the terminal
# half is what they have to re-implement.
CHILD_TEXT, DESC_TEXT, ATTR, NODE = "child-text", "desc-text", "attr", "node"

_ATTR_TAIL = re.compile(r"::attr\(\s*([^)]*?)\s*\)\s*$")
_TEXT_TAIL = re.compile(r"::text\s*$")
_XP_ATTR_TAIL = re.compile(r"/@[\w:.-]+$")
_XP_DESC_ATTR = re.compile(r"/descendant-or-self::\*/@[\w:.-]+$")
_TRAILING_UNIVERSAL = re.compile(r"^(.*\S)\s+\*$")


def split_branches(sel):
    """Split a selector list on its TOP-LEVEL commas (not the ones inside `[...]` / `(...)`)."""
    out, depth, cur = [], 0, ""
    for ch in sel:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [b.strip() for b in out if b.strip()]


def terminal(branch):
    """-> (css, kind, attr) for one comma-free selector, or None if it isn't translatable CSS.

    The KIND comes from Parsel's own translator, not from the string: `p::text` and `p ::text` differ
    only by a space and mean different things (`/text()` vs `/descendant-or-self::text()`). Guessing
    that from the source text is how an adapter silently returns a subset.
    """
    m = _ATTR_TAIL.search(branch)
    if m:
        # `::attr("href")` is as valid as `::attr(href)`; the quotes belong to the pseudo-element
        # syntax, not to the attribute name, and carrying them through looks up an attribute no
        # document has.
        css, attr = branch[: m.start()].rstrip(), m.group(1).strip("\"'")
    else:
        attr = None
        css = _TEXT_TAIL.sub("", branch).rstrip() if _TEXT_TAIL.search(branch) else branch
    try:
        xp = _TR.css_to_xpath(branch)
    except Exception:
        return None
    if xp.endswith("/descendant-or-self::text()"):
        kind = DESC_TEXT
    elif xp.endswith("/text()"):
        kind = CHILD_TEXT
    elif _XP_ATTR_TAIL.search(xp):
        kind = ATTR
    else:
        kind = NODE
    # A trailing universal is descendant-or-SELF in the translated XPath but descendants-ONLY in CSS,
    # so `X *::text` and `X ::attr(a)` both include X itself and the naive CSS half silently drops it.
    # Both are re-expressed as node sets CSS can name: `X` in descendant-text mode, and `X, X *`.
    tail_universal = _TRAILING_UNIVERSAL.match(css)
    if kind in (CHILD_TEXT, DESC_TEXT):
        if tail_universal:
            css, kind = tail_universal.group(1), DESC_TEXT
    elif kind == ATTR and _XP_DESC_ATTR.search(xp):
        base = tail_universal.group(1) if tail_universal else css
        css = f"{base}, {base} *"
    return css, kind, attr


def css_translate(sel):
    """-> ((css, kind, attr), None) or (None, reason).

    Every refusal carries a reason, because "selectolax expresses 73% of production selectors" is only
    useful next to WHY the other 27% are out of reach:
      * XPath — selectolax and bs4 have no XPath at all.
      * a comma list whose branches disagree on terminal or attribute name — the branches would have
        to be run separately and merged back into document order, which is the engine's job.
      * an empty or bare-universal CSS half (`::text`) — the same document-order problem, since every
        text node would be reachable from more than one match.
    """
    if is_xpath(sel):
        return None, "xpath (no XPath engine)"
    branches = split_branches(sel)
    if not branches:
        return None, "empty selector"
    parts = [terminal(b) for b in branches]
    if any(p is None for p in parts):
        return None, "untranslatable CSS"
    if len({p[1] for p in parts}) != 1 or len({p[2] for p in parts}) != 1:
        return None, "comma list with mixed terminals (needs document-order merge)"
    csses = [p[0] for p in parts]
    if any(not c or c == "*" for c in csses):
        return None, "universal/empty element part (needs document-order merge)"
    return (", ".join(csses), parts[0][1], parts[0][2]), None


def css_plan(sel):
    """The translation only — `run` has already been told the selector is expressible."""
    return css_translate(sel)[0]


# ------------------------------------------------------------------ engines
class Engine:
    """One adapter. `run` is the whole timed unit: parse this page once, answer every query."""

    key = label = note = ""
    full_coverage = False   # can it express (nearly) any production selector? -> in W-all

    def unavailable(self):
        """None if usable, else a one-line reason printed in the report."""
        return None

    def refusal(self, sel):
        """None if this engine can express the selector, else a one-line reason. The reasons are
        tallied per engine, because a coverage percentage without them is a number nobody can act on."""
        raise NotImplementedError

    def expressible(self, sel):
        return self.refusal(sel) is None

    def run(self, body, queries):
        raise NotImplementedError


class Frostwork(Engine):
    key, label = "frostwork", "frostwork"
    note = "one streaming pass, no DOM; selectors re-parsed per call"
    full_coverage = True

    def refusal(self, sel):
        return _frostwork_refusal(sel)

    def run(self, body, queries):
        # `strict=False`: the caller already filtered to supported selectors via `expressible`, and a
        # cached validation lookup inside the timed loop would measure this harness, not the engine.
        cols = []
        for batch in _batches(list(queries)):
            if batch:
                cols.extend(frostwork.extract(body, batch, "utf-8", strict=False))
        return cols


class FrostworkPlan(Engine):
    key, label = "frostwork-plan", "frostwork (Plan)"
    note = "same engine, schema compiled once per page object (what Page/FrostPage do)"
    full_coverage = True

    def __init__(self):
        self._cache = {}

    def refusal(self, sel):
        return _frostwork_refusal(sel)

    def _plans(self, queries):
        key = tuple(queries)
        got = self._cache.get(key)
        if got is None:
            got = [Plan(b, []) for b in _batches(list(queries)) if b]
            self._cache[key] = got
        return got

    def run(self, body, queries):
        cols = []
        for plan in self._plans(queries):
            cols.extend(plan.extract(body, "utf-8"))
        return cols


class Parsel(Engine):
    key, label = "parsel", "parsel"
    note = "the incumbent, and this file's parity oracle"
    full_coverage = True

    def refusal(self, sel):
        return None if _parsel_compiles(sel) else "parsel/cssselect rejects it"

    def run(self, body, queries):
        return parsel_extract(body, queries)


class Lxml(Engine):
    key, label = "lxml", "lxml + cssselect"
    note = "parsel's tree and translation without parsel's Python wrapper"
    full_coverage = True

    def refusal(self, sel):
        return None if _lxml_compiles(sel) else "cssselect/lxml rejects it"

    def run(self, body, queries):
        root = _lxml_root(body)
        cols = []
        for q in queries:
            out = []
            got = _lxml_xpath(q)(root)
            # A string/number XPath (`normalize-space(…)`) returns a scalar, not a node set. Iterating
            # it yields CHARACTERS, and an empty one yields nothing at all — which read as an empty
            # column rather than parsel's `['']`.
            for r in got if isinstance(got, list) else [got]:
                # parsel's Selector.get(): elements serialize as HTML, everything else is its string
                out.append(
                    etree.tostring(r, method="html", encoding="unicode", with_tail=False)
                    if isinstance(r, etree._Element)
                    else str(r)
                )
            cols.append(out)
        return cols


class Selectolax(Engine):
    key, label = "selectolax", "selectolax (lexbor)"
    note = "lexbor DOM; ::text/::attr re-implemented over its nodes"

    def unavailable(self):
        try:
            import selectolax.lexbor  # noqa: F401
        except Exception as e:
            return f"selectolax not installed ({e}) — pip install -r requirements-bench.txt"
        return None

    def refusal(self, sel):
        plan, why = css_translate(sel)
        if why:
            return why
        from selectolax.lexbor import LexborHTMLParser

        try:
            LexborHTMLParser(b"<p></p>").css(plan[0])
        except Exception:
            return "lexbor's CSS engine rejects it"
        return None

    def run(self, body, queries):
        from selectolax.lexbor import LexborHTMLParser

        tree = LexborHTMLParser(body)
        cols = []
        for q in queries:
            css, kind, attr = css_plan(q)
            out = []
            for n in tree.css(css):
                if kind == NODE:
                    out.append(n.html)
                elif kind == ATTR:
                    if attr in n.attributes:
                        # lexbor gives None for a valueless attribute; libxml2 gives ""
                        out.append(n.attributes[attr] or "")
                else:
                    walk = n.iter(include_text=True) if kind == CHILD_TEXT else n.traverse(include_text=True)
                    out.extend(x.text_content for x in walk if x.tag == "-text")
            cols.append(out)
        return cols


class BeautifulSoup4(Engine):
    key, label = "bs4", "bs4 + lxml"
    note = "lxml's tree, soupsieve's CSS, Python objects for every node"

    def unavailable(self):
        try:
            import bs4  # noqa: F401
            import soupsieve  # noqa: F401
        except Exception as e:
            return f"beautifulsoup4/soupsieve not installed ({e}) — pip install -r requirements-bench.txt"
        return None

    def refusal(self, sel):
        plan, why = css_translate(sel)
        if why:
            return why
        import soupsieve

        try:
            soupsieve.compile(plan[0])
        except Exception:
            return "soupsieve rejects it"
        return None

    def run(self, body, queries):
        import bs4
        from bs4.element import PreformattedString

        # Two arguments that make this the same measurement as the others rather than a different one.
        # `from_encoding`: every engine here is told utf-8; left to itself bs4 runs UnicodeDammit and
        # picked windows-1251 for a utf-8 page, which is an encoding-sniffing difference wearing a
        # parser-divergence costume. `multi_valued_attributes=None`: bs4's default splits `class` into
        # a list, which is a different value, not a different engine.
        soup = bs4.BeautifulSoup(body, "lxml", from_encoding="utf-8", multi_valued_attributes=None)
        cols = []
        for q in queries:
            css, kind, attr = css_plan(q)
            out = []
            for el in soup.select(css):
                if kind == NODE:
                    out.append(str(el))
                elif kind == ATTR:
                    if el.has_attr(attr):
                        out.append(el[attr])
                else:
                    out.extend(
                        str(x)
                        for x in el.find_all(string=True, recursive=(kind == DESC_TEXT))
                        if not isinstance(x, PreformattedString)  # comments/doctype aren't text()
                    )
            cols.append(out)
        return cols


ENGINES = [Frostwork(), FrostworkPlan(), Parsel(), Lxml(), Selectolax(), BeautifulSoup4()]
ORACLE = "parsel"


# ------------------------------------------------------------------ per-selector caches
# Every `expressible` answer and every compiled selector is cached across pages: each engine keeps
# whatever its own API would cache in a real crawl, so no row pays a compile it would not pay.
_supports_cache, _compiles_cache, _xpath_cache = {}, {}, {}


def _frostwork_refusal(sel):
    """None, or the engine's OWN advisory reason for declining the selector (`frostwork.check`) — the
    one coverage row in this report that does not have to be inferred from the outside."""
    if sel not in _supports_cache:
        try:
            bad = frostwork.check([sel]).unsupported
            _supports_cache[sel] = bad[0].reason.split(";")[0] if bad else None
        except Exception as e:
            _supports_cache[sel] = f"{type(e).__name__}"
    return _supports_cache[sel]


def _parsel_compiles(sel):
    key = ("parsel", sel)
    got = _compiles_cache.get(key)
    if got is None:
        got = parsel_extract(b"<html></html>", [sel])[0] is not None
        _compiles_cache[key] = got
    return got


def _lxml_compiles(sel):
    key = ("lxml", sel)
    got = _compiles_cache.get(key)
    if got is None:
        try:
            _lxml_xpath(sel)
            got = True
        except Exception:
            got = False
        _compiles_cache[key] = got
    return got


def _lxml_xpath(sel):
    got = _xpath_cache.get(sel)
    if got is None:
        xp = sel if is_xpath(sel) else _TR.css_to_xpath(sel)
        got = etree.XPath(xp)
        _xpath_cache[sel] = got
    return got


def _lxml_root(body):
    """parsel's `create_root_node`, inlined: same NUL strip, same strip(), same parser flags. The
    lxml row has to be parsel's tree — otherwise it is a different measurement, not a leaner one."""
    body = body.replace(b"\x00", b"").strip()
    parser = etree.HTMLParser(recover=True, encoding="utf-8", huge_tree=True)
    root = etree.fromstring(body, parser=parser)
    if root is None:
        root = etree.fromstring(b"<html/>", parser=parser)
    return root


# ------------------------------------------------------------------ parse-only
# The document build alone, with no queries. It answers the one question the main comparison cannot:
# how much of an engine's row is its PARSE and how much is its query side. "selectolax is faster" is
# usually said about the parse, and on real pages that turns out not to be where its win comes from —
# a claim worth being able to re-run rather than quote. Frostwork has no row here on purpose: there is
# no tree to build, so it has no parse to separate out.
def parse_only_engines():
    rows = [("lxml (libxml2)", _lxml_root)]
    try:
        from selectolax.lexbor import LexborHTMLParser
        from selectolax.parser import HTMLParser as ModestHTMLParser

        rows.append(("selectolax (lexbor)", LexborHTMLParser))
        rows.append(("selectolax (modest)", ModestHTMLParser))
    except Exception:
        pass
    try:
        import bs4

        rows.append(("bs4 + lxml", lambda b: bs4.BeautifulSoup(
            b, "lxml", from_encoding="utf-8", multi_valued_attributes=None)))
    except Exception:
        pass
    return rows


def run_parse_only(pages, repeats):
    rows = parse_only_engines()
    print("PARSE ONLY — build the document, run no queries (µs)\n")
    print(f"  {'page':>9}  " + "  ".join(f"{n[:19]:>19}" for n, _ in rows))
    out = []
    for p in pages:
        with open(p, "rb") as f:
            body = f.read()
        if not body.strip():
            continue
        got = {}
        for name, fn in rows:
            try:
                got[name] = best_of(lambda fn=fn: fn(body), repeats) * 1e6
            except Exception:
                got[name] = float("nan")
        out.append({"page": os.path.basename(p), "bytes": len(body), "us": got})
        print(f"  {len(body)/1024:>7.0f}KB  " + "  ".join(f"{got[n]:>19.0f}" for n, _ in rows))
    if out:
        print(f"  {'median':>9}  " + "  ".join(
            f"{statistics.median([r['us'][n] for r in out]):>19.0f}" for n, _ in rows))
    return out


# ------------------------------------------------------------------ the workload decision
def workload_columns(n_cols, expressible, keys, parity=None):
    """Indices every engine in `keys` may be timed on.

    `expressible[key][i]` is whether that engine can run column i at all; `parity[key][i]` (when
    given) is its `diff_lxml.verdict` against the oracle. A column survives only if EVERY listed
    engine expresses it, and — when parity is supplied — every one of them also gets it right.

    This is the whole fairness argument in one function, so it is tested directly in
    `tests/test_gates.py`: an engine that returns a wrong column must lose the column, not keep it
    and be timed on work the others are doing correctly.
    """
    out = []
    for i in range(n_cols):
        if not all(expressible[k][i] for k in keys):
            continue
        if parity is not None and not all(parity[k][i] in ("AGREE", "WS") for k in keys):
            continue
        out.append(i)
    return out


def coverage_gap(refusals, oracle_key):
    """Split each engine's refusals into the ACTIONABLE gap and the ones the oracle shares.

    `refusals[key][i]` is that engine's reason for declining column i, or `None` if it expresses it.
    A column the oracle also refuses is not a coverage gap — no port would ask for a selector parsel
    itself rejects — so it is counted separately instead of inflating the engine's own refusal total.

    Returns `{key: (Counter(reason -> n), shared_count)}`, oracle excluded.

    A named function rather than a loop inline in `main` for the same reason `workload_columns` is one:
    it decides a number that gets published, so `tests/test_gates.py` can seed it. Two independent
    coverage percentages only BOUND the set difference (between `|refused| - |oracle refused|` and
    `|refused|`), and quoting the difference of the two totals asserts the optimistic end of that range.
    """
    out = {}
    n = len(refusals[oracle_key])
    for k, rs in refusals.items():
        if k == oracle_key:
            continue
        kinds, shared = collections.Counter(), 0
        for j in range(n):
            if rs[j] is None:
                continue
            if refusals[oracle_key][j] is None:
                kinds[rs[j]] += 1
            else:
                shared += 1
        out[k] = (kinds, shared)
    return out


def assert_same_work(scope, per_engine_columns):
    """Runtime invariant: every engine timed in a scope saw the SAME columns. A table whose rows were
    measured on different work is not a comparison, and the way that happens is an adapter quietly
    dropping a selector it cannot run."""
    distinct = {tuple(v) for v in per_engine_columns.values()}
    if len(distinct) > 1:
        raise AssertionError(
            f"{scope}: engines were timed on different columns "
            f"({ {k: len(v) for k, v in per_engine_columns.items()} })"
        )
    return True


def unavailable_report(engines):
    """[(label, reason)] for every engine that cannot run. Printed even when empty, so an absent
    competitor is never indistinguishable from a competitor that lost."""
    return [(e.label, r) for e in engines if (r := e.unavailable())]


# ------------------------------------------------------------------ run
def _pct(a, b):
    return 100.0 * a / b if b else 0.0


def _quantile(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpus")
    ap.add_argument("--limit", type=int, default=None, help="size-spread sample of N pages")
    ap.add_argument("--repeats", type=int, default=3, help="best-of-N per (page, scope, engine)")
    ap.add_argument("--engines", default=None, help="comma-separated subset of engine keys")
    ap.add_argument("--parse-only", action="store_true",
                    help="time each DOM engine's document build alone and exit")
    args = ap.parse_args()

    corpus_dir = os.path.abspath(os.path.expanduser(args.corpus))
    engines = [e for e in ENGINES if not args.engines or e.key in args.engines.split(",")]

    missing = unavailable_report(engines)
    for label, reason in missing:
        print(f"SKIPPED ENGINE: {label} — {reason}")
    engines = [e for e in engines if not e.unavailable()]
    if not any(e.key == ORACLE for e in engines):
        raise SystemExit(f"the {ORACLE} oracle is required — parity cannot be judged without it")
    keys = [e.key for e in engines]
    full_keys = [e.key for e in engines if e.full_coverage]
    by_key = {e.key: e for e in engines}

    pages = sorted(glob.glob(os.path.join(corpus_dir, "*", "pages", "*.html")), key=os.path.getsize)
    if not pages:
        raise SystemExit(f"no <dir>/*/pages/*.html under {corpus_dir}")
    if args.limit:
        step = max(1, len(pages) // args.limit)
        pages = pages[::step][: args.limit]

    print(f"corpus: {corpus_dir}")
    if args.parse_only:
        rows = run_parse_only(pages, args.repeats)
        os.makedirs(RESULTS, exist_ok=True)
        with open(os.path.join(RESULTS, "enginebench_parse.json"), "w") as f:
            json.dump({"corpus": corpus_dir, "rows": rows, "platform": sys.platform}, f, indent=2)
        print("\nwrote tools/results/enginebench_parse.json")
        return
    print(f"pages: {len(pages)}  engines: {', '.join(keys)}  (best of {args.repeats})\n")

    # --- accumulators -------------------------------------------------------------------------
    times = {s: {k: [] for k in keys} for s in ("W-all", "W-common")}   # seconds per page
    bytes_seen = {s: [] for s in ("W-all", "W-common")}
    speedup = {s: {k: [] for k in keys} for s in ("W-all", "W-common")}  # vs the oracle, per page
    cols_in_scope = {s: 0 for s in ("W-all", "W-common")}
    expressed = collections.Counter()      # engine -> columns it can run
    refusal_kinds = {k: collections.Counter() for k in keys}
    # The ACTIONABLE coverage gap: columns the ORACLE expresses and this engine does not. Two
    # independent percentages against the whole corpus (93.2% vs 97.5%) only bound it — the difference
    # assumes every refusal this engine makes is one parsel also makes, and a refusal the two SHARE is
    # not a gap at all. So the set difference is measured rather than inferred from the two totals.
    gap_kinds = {k: collections.Counter() for k in keys}
    gap_shared = collections.Counter()      # engine -> columns BOTH refuse (not a gap)
    parity_tally = {k: collections.Counter() for k in keys}
    diverge_kinds = {k: collections.Counter() for k in keys}
    diverge_examples = {k: collections.defaultdict(list) for k in keys}
    n_pages = n_objs = total_bytes = total_cols = 0
    seen_objs = set()
    empty_common = 0

    for i, p in enumerate(pages):
        objdir = os.path.dirname(os.path.dirname(p))
        sel_json = os.path.join(objdir, "selectors.json")
        if not os.path.exists(sel_json):
            continue
        with open(sel_json) as f:
            queries = [q for q in json.load(f).values() if isinstance(q, str) and q.strip()]
        if not queries:
            continue
        with open(p, "rb") as f:
            body = f.read()
        if not body.strip():
            continue

        n = len(queries)
        refusals = {k: [by_key[k].refusal(q) for q in queries] for k in keys}
        expressible = {k: [r is None for r in refusals[k]] for k in keys}
        for k in keys:
            expressed[k] += sum(expressible[k])
            for r in refusals[k]:
                if r is not None:
                    refusal_kinds[k][r] += 1
        # Read from `refusals`, which the CRASH handler below does not touch (it zeroes `expressible`):
        # the gap is a coverage fact about the selector, not a runtime one about the page.
        for k, (kinds, shared) in coverage_gap(refusals, ORACLE).items():
            gap_kinds[k].update(kinds)
            gap_shared[k] += shared
        total_cols += n

        # --- values first, timing second: one untimed run per engine over what it can express ---
        vals = {}
        for k in keys:
            idx = [j for j in range(n) if expressible[k][j]]
            try:
                got = by_key[k].run(body, [queries[j] for j in idx])
            except Exception as e:  # an engine that crashes on a real page is a result, not a stop
                parity_tally[k]["CRASH"] += 1
                if len(diverge_examples[k]["CRASH"]) < 8:
                    diverge_examples[k]["CRASH"].append(
                        {"page": os.path.relpath(p, corpus_dir), "query": "<page>",
                         "detail": f"{type(e).__name__}: {e}"}
                    )
                expressible[k] = [False] * n
                vals[k] = [None] * n
                continue
            col = [None] * n
            for slot, j in enumerate(idx):
                col[j] = got[slot] if slot < len(got) else None
            vals[k] = col

        oracle = vals[ORACLE]
        parity = {k: ["N/A"] * n for k in keys}
        for k in keys:
            for j in range(n):
                if not expressible[k][j] or oracle[j] is None:
                    continue
                # Byte-identical is AGREE without asking the comparator: `verdict` re-parses a
                # bare-element column before comparing it, and a fragment parsel cannot re-parse reads
                # as DIVERGE even when the two sides are the same string. That artifact showed up as
                # the oracle diverging from itself.
                if vals[k][j] == oracle[j]:
                    v = "AGREE"
                else:
                    v = verdict(vals[k][j], oracle[j], "CONTROL", queries[j])
                parity[k][j] = v
                parity_tally[k][v] += 1
                if v == "DIVERGE":
                    kind = divergence_kind(vals[k][j], oracle[j], queries[j])
                    diverge_kinds[k][kind] += 1
                    # Capped PER KIND, not overall: a flat cap fills up with whatever diverges first,
                    # and the first run's list was 20 SUBSET/EMPTY rows with none of the 9 WRONG ones —
                    # the severest bucket, invisible in the only place you could go look at it.
                    if len(diverge_examples[k][kind]) < 8:
                        diverge_examples[k][kind].append(
                            {"page": os.path.relpath(p, corpus_dir), "query": queries[j]}
                        )

        # --- scopes -------------------------------------------------------------------------
        scopes = {
            "W-all": (full_keys, workload_columns(n, expressible, full_keys, parity)),
            "W-common": (keys, workload_columns(n, expressible, keys, parity)),
        }
        if not scopes["W-common"][1]:
            empty_common += 1

        for scope, (scope_keys, idx) in scopes.items():
            if not idx:
                continue
            cols_in_scope[scope] += len(idx)
            bytes_seen[scope].append(len(body))
            qs = [queries[j] for j in idx]
            assert_same_work(scope, {k: idx for k in scope_keys})
            for k in scope_keys:
                times[scope][k].append(best_of(lambda k=k, qs=qs: by_key[k].run(body, qs), args.repeats))
            base = times[scope][ORACLE][-1]
            for k in scope_keys:
                t = times[scope][k][-1]
                speedup[scope][k].append(base / t if t else 0.0)

        n_pages += 1
        seen_objs.add(objdir)
        total_bytes += len(body)
        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(pages)} pages")

    n_objs = len(seen_objs)

    # --- report -------------------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("COMPETITIVE BENCHMARK — Frostwork vs the other fast scraping parsers")
    print("=" * 78)
    print(f"pages measured : {n_pages} across {n_objs} page objects, {total_bytes/1048576:.0f} MB")
    print(f"columns        : {total_cols} (page, selector) pairs from the corpus's own selectors")
    print(f"repeats        : best of {args.repeats}, warm, one machine — indicative, not controlled")

    print("\nCOVERAGE — can the engine express the corpus's real selectors at all?")
    for k in keys:
        print(f"  {by_key[k].label:22} {expressed[k]:>9}/{total_cols} "
              f"({_pct(expressed[k], total_cols):5.1f}%)")
        for reason, cnt in refusal_kinds[k].most_common(4):
            print(f"      {cnt:>5}  {reason[:96]}")

    print(f"\nCOVERAGE GAP vs {by_key[ORACLE].label} — columns the ORACLE expresses and this engine does"
          f" not.\n  This is the number to act on: an engine's own refusal total also counts selectors"
          f" {by_key[ORACLE].label} rejects too, which no port would ever ask for.")
    for k in keys:
        if k == ORACLE:
            continue
        gap = sum(gap_kinds[k].values())
        print(f"  {by_key[k].label:22} {gap:>9} gap  ({_pct(gap, total_cols):4.1f}% of all columns)"
              f"  + {gap_shared[k]} shared refusals")
        for reason, cnt in gap_kinds[k].most_common(6):
            print(f"      {cnt:>5}  {reason[:96]}")

    print("\nVALUE PARITY vs parsel/lxml — over each engine's own expressible columns")
    print(f"  {'engine':22} {'identical':>12} {'ws-only':>9} {'DIVERGE':>9}  divergence kinds")
    for k in keys:
        t = parity_tally[k]
        tot = t["AGREE"] + t["WS"] + t["DIVERGE"]
        kinds = "  ".join(f"{v} {kk}" for kk, v in diverge_kinds[k].most_common())
        crash = f"  [{t['CRASH']} page CRASH]" if t["CRASH"] else ""
        print(f"  {by_key[k].label:22} {t['AGREE']:>7} ({_pct(t['AGREE'], tot):4.1f}%) "
              f"{t['WS']:>9} {t['DIVERGE']:>9}  {kinds}{crash}")

    out_scopes = {}
    for scope, blurb in (
        ("W-all", "the columns frostwork/parsel/lxml all express AND agree on"),
        ("W-common", "the same, for EVERY engine — the widest workload all six share"),
    ):
        rows = [k for k in keys if times[scope][k]]
        if not rows:
            continue
        base = statistics.median(times[scope][ORACLE])
        fw = statistics.median(times[scope]["frostwork"]) if times[scope].get("frostwork") else None
        mb = bytes_seen[scope]
        print(f"\n{scope} — {blurb}")
        print(f"  {cols_in_scope[scope]} columns over {len(mb)} pages "
              f"({sum(mb)/1048576:.0f} MB, median {statistics.median(mb)/1024:.0f} KB)")
        print(f"  {'engine':22} {'median µs':>10} {'MB/s':>8} {'×parsel':>9} {'×frostwork':>11}"
              f" {'p10':>7} {'p90':>7}")
        srows = []
        for k in rows:
            med = statistics.median(times[scope][k])
            mbps = statistics.median(
                [b / t / 1e6 for b, t in zip(mb, times[scope][k]) if t]
            )
            row = {
                "engine": by_key[k].label,
                "median_us": med * 1e6,
                "mbps": mbps,
                "x_parsel": base / med if med else 0.0,
                "x_frostwork": (fw / med if med else 0.0) if fw else None,
                "speedup_p10": _quantile(speedup[scope][k], 0.10),
                "speedup_median": statistics.median(speedup[scope][k]) if speedup[scope][k] else 0.0,
                "speedup_p90": _quantile(speedup[scope][k], 0.90),
                "aggregate_x_parsel": (sum(times[scope][ORACLE]) / sum(times[scope][k])
                                       if sum(times[scope][k]) else 0.0),
            }
            srows.append(row)
            print(f"  {row['engine']:22} {row['median_us']:>10.0f} {row['mbps']:>8.0f} "
                  f"{row['x_parsel']:>8.2f}× {row['x_frostwork'] or 0:>10.2f}× "
                  f"{row['speedup_p10']:>6.1f}× {row['speedup_p90']:>6.1f}×")
        print("  (p10/p90 are the per-page speedup-vs-parsel distribution: page shape moves this "
              "number more than anything else)")
        out_scopes[scope] = {"columns": cols_in_scope[scope], "pages": len(mb), "rows": srows}

    if empty_common:
        print(f"\nNOTE: {empty_common} page(s) had an empty W-common and were excluded from it — "
              f"every selector was either inexpressible somewhere or divergent somewhere.")
    print(f"\nengines skipped: {len(missing)}" + (f"  ({', '.join(l for l, _ in missing)})" if missing else ""))

    os.makedirs(RESULTS, exist_ok=True)
    out = {
        "corpus": corpus_dir, "pages": n_pages, "page_objects": n_objs, "total_bytes": total_bytes,
        "columns": total_cols, "repeats": args.repeats, "platform": sys.platform,
        "max_selectors_per_pass": MAX_SELECTORS_PER_PASS,
        "engines": [{"key": k, "label": by_key[k].label, "note": by_key[k].note} for k in keys],
        "skipped_engines": [{"engine": l, "reason": r} for l, r in missing],
        "coverage": {k: {"expressible": expressed[k], "of": total_cols,
                         "refusals": dict(refusal_kinds[k]),
                         "gap_vs_oracle": sum(gap_kinds[k].values()),
                         "gap_reasons": dict(gap_kinds[k]),
                         "shared_refusals": gap_shared[k]} for k in keys},
        "parity": {k: dict(parity_tally[k]) for k in keys},
        "parity_kinds": {k: dict(diverge_kinds[k]) for k in keys},
        "parity_examples": {k: {kind: v for kind, v in ex.items()} for k, ex in diverge_examples.items()},
        "scopes": out_scopes,
    }
    with open(os.path.join(RESULTS, "enginebench.json"), "w") as f:
        json.dump(out, f, indent=2)
    print("\nwrote tools/results/enginebench.json")


if __name__ == "__main__":
    main()
