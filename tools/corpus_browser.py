"""
tools/corpus_browser.py: browser mode against Chrome page loads over a page corpus, per selector. REPORT-ONLY.

The corpus layout is `bench_corpus.py`'s (`<dir>/<page-object>/{selectors.json,pages/*.html}`). Each page
is served to Chrome as its exact bytes with a Content-Type header and page-loaded (never DOMParser, which
takes a string), so the browser resolves the encoding itself. The header is the one-line sidecar
`<page>.content-type` when there is one, else `text/html` with no charset. The engine gets the same
bytes and the same header value (`content_type=`), in browser mode.

Every selector of the page object is a column. CSS is answered natively in the browser
(`browser_oracle/css.py`), XPath through the lossless lxml bridge (`browser_oracle/bridge.py`), both on the
tree the load built. A column is AGREE, REFUSED (the engine refuses it in browser mode, an empty column by
contract), ORACLE (the oracle cannot answer it), or it differs. A differing column is split by the page's
whole-tree fingerprint (`seq_sweep.fingerprint_selectors` over the page's own ids, at most `--ids`), the
split `diff_browser.py` makes: TREE if the fingerprint differs, VALUE if it agrees. A real page carries
few ids, so where the fingerprint has none the split is weak, and the report says so per page.

Nothing here is reduced or attributed to a construct: a corpus page is not a token sequence, and
page-scoped attribution over-credits (AGENTS.md). The output is a worklist, and the run exits 0 unless the
oracle failed. `make gate-corpus-browser [CORPUS=<dir>]`.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, NamedTuple, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import diff_lxml  # noqa: E402  (the engine's member budget)
import seq_sweep  # noqa: E402
from browser_answers import engine_refusals, same_values  # noqa: E402
from browser_oracle import bridge as Br  # noqa: E402
from browser_oracle import css as C  # noqa: E402
from browser_oracle import tree as T  # noqa: E402
from browser_oracle.client import BrowserOracle, Refused  # noqa: E402

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "corpus_browser.json")
DEFAULT_CTYPE = "text/html"


class Page(NamedTuple):
    obj: str                        # its page object
    path: str
    fields: list[tuple[str, str]]   # (field, selector)
    body: bytes
    ctype: str                      # the Content-Type it is served with


def load(corpus: str, limit: int) -> list[Page]:
    out = []
    for p in sorted(glob.glob(os.path.join(corpus, "*", "pages", "*.html"))):
        obj = os.path.dirname(os.path.dirname(p))
        sel_json = os.path.join(obj, "selectors.json")
        if not os.path.exists(sel_json):
            continue
        with open(sel_json, encoding="utf-8") as f:
            fields = list(json.load(f).items())
        with open(p, "rb") as f:
            body = f.read()
        sidecar = p[:-len(".html")] + ".content-type"
        ctype = DEFAULT_CTYPE
        if os.path.exists(sidecar):
            with open(sidecar, encoding="ascii") as f:
                ctype = f.read().strip()
        if fields and body.strip():
            out.append(Page(os.path.basename(obj), p, fields, body, ctype))
    return out[:limit] if limit else out


def page_ids(tree, cap: int) -> list[str]:
    """The page's own unique ids, in document order, that an XPath literal can hold: the fingerprint's ids."""
    root = T.index(T.document_view(tree))
    ids = T.evaluate(root, "//*/@id")
    counts = Counter(ids)
    return [i for i in dict.fromkeys(ids) if counts[i] == 1 and '"' not in i and i][:cap]


def column_verdict(sel: str, refused: bool, mine: Optional[list[str]], want: Any, tree_differs: bool,
                   ids: int) -> str:
    """One corpus column's verdict. The report's whole decision, so `tests/test_gates.py` can seed it."""
    if refused:
        return "REFUSED"
    if not isinstance(want, list) or mine is None:
        return "ORACLE"
    if same_values(mine, want, sel):
        return "AGREE"
    if tree_differs:
        return "TREE"
    return "VALUE" if ids else "UNSPLIT"


def engine(body: bytes, sels: list[str], ctype: str, encoding: Optional[str]) -> list[list[str]]:
    import frostwork

    out: list[list[str]] = []
    for start in range(0, len(sels), diff_lxml.MAX_SELECTORS_PER_PASS):
        chunk = sels[start:start + diff_lxml.MAX_SELECTORS_PER_PASS]
        out += frostwork.extract(body, chunk, encoding, content_type=ctype, strict=False,
                                 **seq_sweep.parser_kwargs("browser"))
    return out


def grade_page(chrome: BrowserOracle, page: Page, refused: dict[str, str], cap: int, corpus: str
               ) -> Optional[tuple[dict, list[tuple]]]:
    """A page's row, and its columns as `(field, selector, verdict, engine's values, Chrome's)`; `None` when
    Chrome did not load it."""
    import frostwork

    css_sels = [s for _f, s in page.fields if C.is_css(s) and s not in refused]
    trees, answers = chrome.query([page.body], [css_sels], content_types=[page.ctype])
    tree = trees[0]
    if tree is None or answers[0] is None:
        return None
    native = dict(zip(css_sels, answers[0]))
    charsets = (chrome.last_charsets[0], frostwork.detect_encoding(page.body, content_type=page.ctype))
    bridge = Br.open_bridge(tree)
    ids = page_ids(tree, cap)
    prints = seq_sweep.fingerprint_selectors(ids)
    graded = [s for _f, s in page.fields if s not in refused]
    # Encoding is its own axis (enc_check, and decision E2 keeps unlabelled valid UTF-8 as UTF-8). A page
    # the two decoded differently is reported as such, and graded on Chrome's decoding, so its tree and
    # value columns are not all lost to the encoding.
    forced = charsets[0] if charsets[0] != charsets[1] else None
    got = engine(page.body, prints + graded, page.ctype, forced)
    mine_prints, mine = got[:len(prints)], dict(zip(graded, got[len(prints):]))
    if isinstance(bridge, Refused):
        theirs_prints = T.fingerprint(tree, prints)
    else:
        theirs_prints = [bridge.xpath(s) for s in prints]
    print_diff = [s for s, a, b in zip(prints, mine_prints, theirs_prints) if a != b]
    columns = []
    for field, sel in page.fields:
        want = None
        if sel not in refused:
            want = native.get(sel) if C.is_css(sel) else Br.xpath_answer(bridge, sel)
        verdict = column_verdict(sel, sel in refused, mine.get(sel), want, bool(print_diff), len(ids))
        columns.append((field, sel, verdict, mine.get(sel), want))
    row = {"page": os.path.relpath(page.path, corpus), "content_type": page.ctype, "ids": len(ids),
           "fingerprint_columns": len(prints), "fingerprint_differs": print_diff[:10],
           "charsets": {"chrome": charsets[0], "engine": charsets[1]}, "graded_as": forced,
           "verdicts": Counter(verdict for _f, _s, verdict, _m, _w in columns)}
    return row, columns


@dataclass
class Result:
    corpus: str
    build: str
    seconds: float
    rows: list[dict]                             # one per graded page
    verdicts: Counter                            # over every column
    by_selector: dict[tuple[str, str], Counter]  # (page object, selector) -> verdicts
    examples: list[dict]                         # the first differing columns, with both sides' values
    refused: dict[str, str]
    oracle_errors: list


def print_report(result: Result, show: int) -> None:
    rows, verdicts = result.rows, result.verdicts
    print("engine            : frostwork.extract(..., parser='browser', content_type=<the served header>)")
    print(f"oracle            : {result.build}, CSP page loads of the exact bytes")
    print(f"corpus            : {result.corpus}, {len(rows)} pages graded in {result.seconds:.1f}s")
    print(f"columns           : {sum(verdicts.values())}  " + "  ".join(
        f"{k}={verdicts[k]}" for k in ("AGREE", "TREE", "VALUE", "UNSPLIT", "REFUSED", "ORACLE")))
    print("  TREE: the page's fingerprint differs, so the column may only show that; VALUE: it agrees; "
          "UNSPLIT: the page has no ids, so its fingerprint cannot tell")
    decoded = [r for r in rows if r["graded_as"]]
    print(f"encoding          : {len(decoded)} of {len(rows)} pages decoded differently by Chrome and the "
          f"engine, graded on Chrome's decoding (the encoding gates own the difference)")
    print(f"\n  {'page':<44} {'ids':>5} {'fp diff':>8}  {'decoded (chrome / engine)':<28} verdicts")
    for row in rows:
        weak = "  (no ids: differing columns are UNSPLIT)" if not row["ids"] and not row["fingerprint_differs"] else ""
        enc = f"{row['charsets']['chrome']} / {row['charsets']['engine']}"
        print(f"  {row['page']:<44} {row['ids']:>5} {len(row['fingerprint_differs']):>8}  {enc:<28} "
              f"{dict(row['verdicts'])}{weak}")
    differing = [(k, c) for k, c in result.by_selector.items() if c["TREE"] or c["VALUE"] or c["UNSPLIT"]]
    if differing:
        print(f"\n  selectors that differ ({len(differing)}; page object, selector, verdicts):")
        for (obj, sel), c in sorted(differing, key=lambda kv: -(kv[1]["VALUE"] * 1000 + kv[1]["TREE"]))[:show]:
            print(f"    {obj:<16} {sel:<48} {dict(c)}")
        print("\n  examples:")
        for ex in result.examples[:show]:
            print(f"    [{ex['verdict']}] {ex['page']} {ex['selector']}\n"
                  f"        engine={json.dumps(ex['engine'])[:160]}\n        chrome={json.dumps(ex['chrome'])[:160]}")
    if result.refused:
        print(f"\n  refused by the engine in browser mode: {len(result.refused)}, e.g. {sorted(result.refused)[:3]}")
    if result.oracle_errors:
        print(f"\n  ORACLE ERRORS: {len(result.oracle_errors)}, e.g. {result.oracle_errors[0]}")


def _jsonable(o):
    """Values the report holds that JSON has no type for: raw bytes and refusal records."""
    return o.decode("utf-8", "replace") if isinstance(o, bytes) else repr(o)


def write_results(result: Result) -> None:
    os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
    with open(RESULTS, "w", encoding="utf-8") as f:
        json.dump({"browser": result.build, "corpus": result.corpus, "verdicts": result.verdicts,
                   "pages": [{**r, "verdicts": dict(r["verdicts"])} for r in result.rows],
                   "examples": result.examples, "refused": result.refused, "oracle_errors": result.oracle_errors},
                  f, indent=1, default=_jsonable)
    print(f"\n  wrote {os.path.relpath(RESULTS)}  (report-only: no gate yet)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpus", help="a corpus in bench_corpus.py's layout")
    ap.add_argument("--limit", type=int, default=0, help="at most this many pages")
    ap.add_argument("--ids", type=int, default=200, help="at most this many of a page's ids in the fingerprint")
    ap.add_argument("--show", type=int, default=20)
    args = ap.parse_args()

    pages = load(args.corpus, args.limit)
    refused = engine_refusals(sorted({s for page in pages for _f, s in page.fields}), "browser")

    verdicts: Counter = Counter()
    by_selector: dict[tuple[str, str], Counter] = defaultdict(Counter)
    examples: list[dict] = []
    rows: list[dict] = []
    oracle_errors: list = []
    t0 = time.perf_counter()
    with BrowserOracle("chrome") as chrome:
        for page in pages:
            graded = grade_page(chrome, page, refused, args.ids, args.corpus)
            if graded is None:
                oracle_errors.append({"page": page.path, "error": chrome.errors[-1:] or "no tree"})
                continue
            row, columns = graded
            for field, sel, verdict, mine, want in columns:
                if verdict in ("TREE", "VALUE", "UNSPLIT") and len(examples) < 200:
                    examples.append({"page": row["page"], "field": field, "selector": sel, "verdict": verdict,
                                     "engine": mine[:5], "chrome": want[:5]})
                verdicts[verdict] += 1
                by_selector[(page.obj, sel)][verdict] += 1
            rows.append(row)
        oracle_errors += chrome.errors
        build = chrome.build
    result = Result(args.corpus, build, time.perf_counter() - t0, rows, verdicts, by_selector, examples, refused,
                    oracle_errors)
    print_report(result, args.show)
    write_results(result)
    return 1 if oracle_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
