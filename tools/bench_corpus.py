"""
Corpus analysis: parse+extract THROUGHPUT and value-PARITY for Frostwork vs Parsel over a directory
of real page objects laid out as the Zyte corpus is:

    <corpus_dir>/<page-object>/selectors.json      # {field: css_or_xpath, ...}  (the real selectors)
    <corpus_dir>/<page-object>/pages/test-*.html    # real page snapshots

For each page, Frostwork does ONE streaming pass over all of that page object's selectors; Parsel
parses once into an lxml tree, then runs one query per selector (its real Selector-reuse pattern).
Both run the SAME selectors on the SAME bytes with encoding forced to utf-8 (matching diff_lxml.py),
so each page is a fair "extract this page object's fields from this page" comparison.

Reports: per-page µs and MB/s, the speedup distribution (Parsel µs / Frostwork µs), and value-parity
— the fraction of (page, selector) columns that are byte-identical between the two engines, which is
the project's correctness bar. Whitespace-only differences are reported separately.

IMPORTANT: build the extension in RELEASE first — `.venv/bin/maturin develop --release` — or you are
timing a debug build (~10x slower than release, and unfair vs optimized libxml2).

Usage:
  .venv/bin/python tools/bench_corpus.py <corpus_dir> [--limit N] [--repeats R]
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import sys
import time

from parsel import Selector as PS

import frostwork
from frostwork._frostwork import Plan

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
sys.path.insert(0, HERE)
# Reuse the differential gate's own parity semantics so DIVERGE here means exactly what it means
# there — including the documented "outer-HTML = raw source" handling: a bare-element (node) query
# is compared by re-parse equivalence on non-whitespace text, NOT byte-equality against lxml reflow.
from diff_lxml import verdict, is_xpath, is_node_query  # noqa: E402


def gap_reason(sel):
    """Classify a selector by the frostwork coverage-gap that explains an empty/partial column.
    'unexplained' means it uses only supported features, so a divergence there is a real mismatch.

    NOTE: this is a *shape* heuristic for grouping — it explains WHY a column may be empty. It does
    NOT by itself mean the divergence is safe; `divergence_kind` (empty/subset/wrong) is the actual
    severity signal. Historically this only knew CSS gaps, so every XPath gap (relative context,
    text() node-tests, predicates) fell through to 'unexplained' and looked like a bug — it wasn't."""
    s = sel.strip()
    # --- XPath coverage gaps (the downward-CSS-shaped subset is all that's supported) ---
    if is_xpath(s):
        if s.startswith(".//") or s.startswith("./"):
            return "relative/context XPath (.//, ./)"   # meant to run per-item; run flat -> empty
        low = s.lower()
        if "text()" in low or "comment()" in low or "node()" in low:
            return "xpath node-test (text()/node())"    # //text() etc. -> unsupported, empty
        if "(" in s and (")" in s):
            return "xpath function predicate (contains/not/…)"
        if "[" in s:
            return "xpath predicate ([n], [child], [@x=…])"
        if "|" in s:
            return "xpath union (a|b)"
        return "xpath (other unsupported axis/step)"
    # top-level (outside []) scan for list-comma and sibling combinators
    depth = 0
    for ch in sel:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif depth == 0 and ch == ",":
            return "selector-list (a, b)"
        elif depth == 0 and ch in "+~":
            return "sibling-combinator (+/~)"
    low = sel.lower()
    if ":contains(" in low:
        return ":contains()"
    if ":scope" in low:
        return ":scope (relative CSS)"
    for p in ("last-child", "first-child", "nth-", "only-child", "last-of-type", "first-of-type",
              "nth-of-type", ":not(", ":empty", ":checked"):
        if p in low:
            return "structural-pseudo"
    # bare/universal text collector (`::text`, ` ::text`, `*::text`) over the whole document: diverges
    # only in text-node segmentation / root-level stray text, not in extracted values.
    if low.endswith("::text") and s[: -len("::text")].strip() in ("", "*"):
        return "universal ::text collector (segmentation)"
    return "unexplained (supported features — investigate)"


def divergence_kind(fc, pc, query):
    """The ACTUAL severity of a divergence, per the no-fallback contract ('never a wrong value'):
      EMPTY   — frostwork returns nothing (safe: an unsupported query yields an empty column)
      SUBSET  — frostwork's non-ws content is a strict subset of Parsel's (missing values, not wrong)
      SEGMENT — same total text, different text-node BOUNDARIES. Benign ONLY under join cardinality:
                a `One` field takes col[0], so an extra split silently TRUNCATES it ('HELLO', not
                'HELLOWORLD'). Treated as a bug by --gate; do not wave it through as cosmetic.
      WRONG   — frostwork emits non-ws content Parsel does NOT (an outright wrong value)
    Node (outer-HTML) queries are compared on re-parsed non-ws text (raw-source vs lxml reflow)."""
    if is_node_query(query):
        def texts(frag):
            try:
                return [t.strip() for t in PS(text=frag).xpath("//text()").getall() if t.strip()]
            except Exception:
                return []
        fset = {t for frag in fc for t in texts(frag)}
        pset = {t for frag in (pc or []) for t in texts(frag)}
    else:
        fset = {v.strip() for v in fc if v.strip()}
        pset = {v.strip() for v in (pc or []) if v.strip()}
    if not fset:
        return "EMPTY"
    if fset <= pset:
        return "SUBSET"
    # WS-collapsed concatenation identical -> same content, only the text-node split differs
    if " ".join("".join(fc).split()) == " ".join("".join(pc or []).split()):
        return "SEGMENT"  # NOT benign for a One-cardinality field — see the docstring
    return "WRONG"


# Every selector this tool measures is one the engine claims to SUPPORT — `frostwork.extract` runs with
# strict validation, so an unsupported one raises rather than reaching the comparison. That makes EMPTY
# and SUBSET regressions, not coverage gaps: a supported column going from ["a","b"] to ["a"] is exactly
# the failure a rule change causes, and grading it as a gap let `make gate-corpus` stay green through it.
VALUE_BUG_KINDS = ("EMPTY", "SUBSET", "SEGMENT", "WRONG")


def is_value_bug(kind: str) -> bool:
    """Does this divergence kind fail the gate? Every non-whitespace divergence does."""
    return kind in VALUE_BUG_KINDS


def _plan_for(queries):
    """One page object's schema, compiled once — audited FIRST, exactly as a page object is.

    The audit is what the severity classification above rests on: EMPTY and SUBSET are graded as
    REGRESSIONS rather than coverage gaps precisely because an unsupported selector raises before it can
    reach the comparison. A bare `Plan` only checks the bitset budget, so it must not be built without
    this — an unsupported selector would become a silently empty column the gate reads as a value bug, or
    as a legitimate empty answer.
    """
    frostwork.check(list(queries)).raise_for_status()
    return Plan(list(queries), [])


def nonws_equal(a, b):
    """True if two value columns carry the same non-whitespace content (drop empty/ws-only items).
    This is the project's actual bar; the gate's WS check is length-sensitive to empty text nodes."""
    return [v.strip() for v in a if v.strip()] == [v.strip() for v in b if v.strip()]


def parsel_extract(body, queries):
    """Parsel's real model: parse once, then one query per field. -> list of columns."""
    s = PS(body=body, encoding="utf-8")
    cols = []
    for q in queries:
        try:
            cols.append((s.xpath(q) if is_xpath(q) else s.css(q)).getall())
        except Exception:
            cols.append(None)  # selector Parsel can't compile — excluded from parity
    return cols


def best_of(fn, repeats):
    best = float("inf")
    for _ in range(repeats):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def main():
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    corpus_dir = os.path.abspath(args[0])
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    gate = "--gate" in args  # exit nonzero on any VALUE bug, so a real corpus can guard a release
    # a parity verdict needs ONE run of each engine; timing repeats would just re-parse with lxml
    default_repeats = 1 if gate else 3
    repeats = int(args[args.index("--repeats") + 1]) if "--repeats" in args else default_repeats

    pages = sorted(glob.glob(os.path.join(corpus_dir, "*", "pages", "*.html")), key=os.path.getsize)
    if not pages:
        raise SystemExit(f"no <dir>/*/pages/*.html under {corpus_dir}")
    if limit:
        # keep a size-spread sample, not just the smallest
        step = max(1, len(pages) // limit)
        pages = pages[::step][:limit]

    n_pages = 0
    n_pageobjs = set()
    total_bytes = 0
    n_cols = 0
    agree = 0             # value-identical (node queries: re-parse-equivalent) — gate's AGREE
    ws_only = 0           # differ only in whitespace / empty text-node count (content identical)
    node_cols = 0         # bare-element (outer-HTML) columns, compared as raw source
    diverge = []          # (page, sel_index, query) true content divergences (non-ws differs)
    parsel_uncompilable = 0
    frost_us, parsel_us, sizes, speedups, frost_mbps = [], [], [], [], []

    print(f"corpus: {corpus_dir}")
    print(f"pages: {len(pages)}  (repeats={repeats})  — timing + parity vs Parsel\n")

    for i, p in enumerate(pages):
        sel_json = os.path.join(os.path.dirname(os.path.dirname(p)), "selectors.json")
        if not os.path.exists(sel_json):
            continue
        with open(sel_json) as f:
            queries = list(json.load(f).values())
        if not queries:
            continue
        with open(p, "rb") as f:
            body = f.read()
        if not body.strip():
            continue  # empty snapshot — Parsel can't build a Selector from it
        # The schema is compiled ONCE, outside the timed loop, because that is how a page object runs:
        # its selectors are declared on a class and `FrostPage` compiles them to a `Plan` at
        # class-definition time. Re-parsing selector strings per response measures a program nobody
        # writes. Parsel gets the same treatment on its side — one `Selector` per page, then a query per
        # field (`parsel_extract`), which is its own real reuse pattern.
        plan = _plan_for(queries)

        # warmup (also validates neither side crashes)
        fcols = plan.extract(body, "utf-8")
        pcols = parsel_extract(body, queries)

        ft = best_of(lambda: plan.extract(body, "utf-8"), repeats)
        pt = best_of(lambda: parsel_extract(body, queries), repeats)

        n_pages += 1
        n_pageobjs.add(os.path.dirname(os.path.dirname(p)))
        total_bytes += len(body)
        sizes.append(len(body))
        frost_us.append(ft * 1e6)
        parsel_us.append(pt * 1e6)
        speedups.append(pt / ft if ft else 0)
        frost_mbps.append(len(body) / ft / 1e6 if ft else 0)

        # parity: compare column-by-column using the gate's own verdict (CONTROL bucket = a real
        # divergence is a bug, never SKIP-EXPECTED)
        for j, (fc, pc) in enumerate(zip(fcols, pcols)):
            if pc is None:
                parsel_uncompilable += 1
                continue
            n_cols += 1
            if is_node_query(queries[j]):
                node_cols += 1
            v = verdict(fc, pc, "CONTROL", queries[j])
            if v == "AGREE":
                agree += 1
            elif v == "WS" or (not is_node_query(queries[j]) and nonws_equal(fc, pc)):
                # only whitespace differs, incl. extra empty text-nodes on universal ::text collectors
                ws_only += 1
            else:  # real content divergence
                kind = divergence_kind(fc, pc, queries[j])
                diverge.append((os.path.relpath(p, corpus_dir), j, queries[j], kind))

        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(pages)} pages")

    # ---- report ----
    def pct(a, b):
        return 100.0 * a / b if b else 0.0

    med_speed = statistics.median(speedups) if speedups else 0
    mean_speed = statistics.mean(speedups) if speedups else 0
    print("\n" + "=" * 60)
    print("CORPUS PERFORMANCE — Frostwork (1 pass, no DOM) vs Parsel")
    print("=" * 60)
    print(f"pages measured   : {n_pages}  across {len(n_pageobjs)} page objects")
    print(f"total bytes      : {total_bytes/1048576:.1f} MB   "
          f"(page size: min {min(sizes)/1024:.0f} KB, median {statistics.median(sizes)/1024:.0f} KB, "
          f"max {max(sizes)/1048576:.1f} MB)")
    print(f"selectors run    : {n_cols} (page,selector) columns"
          + (f"  (+{parsel_uncompilable} Parsel-uncompilable, excluded)" if parsel_uncompilable else ""))
    print()
    print(f"frostwork        : median {statistics.median(frost_us):.0f} µs/page   "
          f"median {statistics.median(frost_mbps):.0f} MB/s   "
          f"total {sum(frost_us)/1e6:.2f} s")
    print(f"parsel           : median {statistics.median(parsel_us):.0f} µs/page   "
          f"total {sum(parsel_us)/1e6:.2f} s")
    print(f"speedup          : median {med_speed:.1f}x   mean {mean_speed:.1f}x   "
          f"aggregate {sum(parsel_us)/sum(frost_us):.1f}x")
    print()
    print("PARITY vs Parsel/lxml — same semantics as the differential gate (diff_lxml.py)")
    print(f"  columns compared : {n_cols}   ({node_cols} bare-element/outer-HTML, {n_cols-node_cols} ::text/::attr)"
          + (f"   +{parsel_uncompilable} Parsel-uncompilable, excluded" if parsel_uncompilable else ""))
    content_ok = agree + ws_only
    print(f"  content-identical: {content_ok}/{n_cols}  ({pct(content_ok, n_cols):.2f}%)"
          f"   [{agree} exact + {ws_only} whitespace-only]")
    print(f"  DIVERGE          : {len(diverge)}  ({pct(len(diverge), n_cols):.2f}%)   "
          f"(non-whitespace content differs)")
    # SEVERITY (the real signal, per the no-fallback contract): only WRONG is a 'wrong value' bug.
    # EMPTY/SUBSET are coverage gaps (frostwork returns nothing / a subset — never a wrong value).
    import collections
    by_kind = collections.Counter(k for _p, _j, _q, k in diverge)
    print(f"    by severity: "
          + "  ".join(f"{by_kind.get(k, 0)} {k}" for k in ("EMPTY", "SUBSET", "SEGMENT", "WRONG")))
    # split divergences by the shape that explains the gap (for grouping only — severity is above)
    by_reason = collections.Counter(gap_reason(q) for _p, _j, q, _k in diverge)
    for reason, cnt in by_reason.most_common():
        flag = "  <-- REAL MISMATCH" if reason.startswith("unexplained") else ""
        print(f"      {cnt:>4}  {reason}{flag}")
    # EVERY non-whitespace divergence fails: these are all supported selectors (strict validation), so
    # EMPTY/SUBSET are lost values, not coverage gaps. Grading them as gaps meant a regression from
    # ["a","b"] to ["a"] left this gate green — the single most likely way a rule change breaks a scrape.
    wrong = [(p, j, q, k) for p, j, q, k in diverge if is_value_bug(k)]
    if wrong:
        print("    VALUE bugs (EMPTY/SUBSET = lost values; SEGMENT = extra text-node split -> a One"
              " field truncates; WRONG = content Parsel lacks):")
        for page, j, q, k in wrong[:20]:
            print(f"      - [{k}] {page}  sel[{j}]  {q!r}")
    else:
        print("    VALUE bugs: 0")

    os.makedirs(RESULTS, exist_ok=True)
    out = {
        "corpus": corpus_dir, "pages": n_pages, "page_objects": len(n_pageobjs),
        "total_bytes": total_bytes, "columns": n_cols,
        "parsel_uncompilable": parsel_uncompilable,
        "frost_us_median": statistics.median(frost_us), "frost_mbps_median": statistics.median(frost_mbps),
        "parsel_us_median": statistics.median(parsel_us),
        "speedup_median": med_speed, "speedup_mean": mean_speed,
        "speedup_aggregate": sum(parsel_us) / sum(frost_us) if sum(frost_us) else 0,
        "parity_agree": agree, "parity_ws": ws_only, "node_columns": node_cols,
        "diverge": len(diverge), "diverge_by_kind": dict(by_kind),
        "diverge_by_reason": dict(by_reason),
        "diverge_examples": [{"page": p, "sel": j, "query": q, "kind": k, "reason": gap_reason(q)}
                             for p, j, q, k in diverge],
        "platform": sys.platform,
    }
    with open(os.path.join(RESULTS, "corpusbench.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote tools/results/corpusbench.json")

    if gate:
        print(f"\nCORPUS GATE: value bugs (any non-whitespace divergence) = {len(wrong)}  ->  "
              f"{'PASS' if not wrong else 'FAIL'}")
        if wrong:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
