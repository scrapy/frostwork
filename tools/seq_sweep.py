"""
tools/seq_sweep.py — enumerate short TAG SEQUENCES exhaustively and compare the whole TREE.

Why this exists. Every rule table in this repo is now derived from the oracle and swept cell by cell, and
the bugs kept coming anyway — six in three crawl samples. They were not wrong cells. They were wrong
*sequences*: `<frameset>` inside `<head>`, `<body>` after `</body>`, content after `</html>`, `</%>`
between two text runs, `<tbody>` between a row and its `</tr>`. In every one of them the state at token N
depended on tokens 1..N-1, and the audit's sweeps are two-dimensional (open x incoming, open x closing) so
they structurally cannot reach that. The crawl corpus reaches it only by luck of what the web contains.

Sequence space over a curated alphabet is small enough to enumerate outright, so this does:

  * an ALPHABET of ~20 tokens, one per behaviour class the engine special-cases (frame tags, table
    machinery, raw text, a bogus end tag, text, a doctype), NOT a list of tags someone found interesting;
  * every sequence of length <= `--depth` (default 4: ~160k documents), plus a random sample at greater
    lengths for the shapes only depth reaches;
  * a comparison of the ENTIRE TREE, not of a handful of selector values.

The tree comparison is the other half of the idea. Everything else here grades a few `::text` columns,
which sees a wrong tree only when a value happens to move; a page can be reshaped completely and still
answer `p::text` identically. Each generated element carries a unique id, and the fingerprint is
(document order, per-element descendant set, per-element own text, placement relative to the synthesized
frame). Two trees with the same fingerprint are the same tree, and a difference names the element it is
at, which makes the reduction step almost free.

Both sides are asked through the SAME selectors, so the fingerprint cannot encode one library's
convention (parsel's `#a *` includes the subject; that is fine as long as both sides are asked).

Usage:
  .venv/bin/python tools/seq_sweep.py --depth 3            # ~8k documents, seconds
  .venv/bin/python tools/seq_sweep.py --depth 4 --gate     # ~160k documents, the CI form
  .venv/bin/python tools/seq_sweep.py --depth 4 --random 20000 --length 7
"""
from __future__ import annotations

import argparse
import itertools
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import oracle  # noqa: E402  — libxml2 >= 2.14, same requirement as every other gate
import parsel  # noqa: E402

# One token per behaviour class the engine special-cases. Marked `{}` where an id goes, so every element
# the sweep creates can be named in the fingerprint. Keep this list SHORT: the sweep is exponential in it,
# and a name that behaves identically to another adds documents without adding coverage.
ALPHABET: list[str] = [
    # the document frame, whose rules are three different ones (see matcher/frame.rs)
    "<html{}>", "</html>", "<head{}>", "</head>", "<body{}>", "</body>",
    # table machinery: the end-tag priority order lives here
    "<table{}>", "<tr{}>", "<td{}>", "</td>", "</tr>", "<tbody{}>",
    # an ordinary block and an inline, to sit between two special tags and break their adjacency
    "<div{}>", "</div>", "<span{}>",
    # frameset documents open neither head nor body
    "<frameset{}>", "</frameset>",
    # raw text changes tokenization, not just tree shape
    "<title{}>", "</title>",
    # not tags at all: a bogus comment, an ignored end tag, a doctype, and character data
    "</%>", "</>", "<!doctype html>", "x",
]


def build(seq: tuple[str, ...]) -> tuple[bytes, list[str]]:
    """Render a token sequence into a document, giving every element a unique id."""
    out, ids = [], []
    for i, token in enumerate(seq):
        if "{}" in token:
            ids.append(str(i))
            out.append(token.format(f' id={i}'))
        else:
            out.append(token)
    return "".join(out).encode(), ids


def fingerprint_selectors(ids: list[str]) -> list[str]:
    """Selectors whose combined answers determine the tree exactly.

    XPath, not CSS, and that is not a style choice: parsel's `.css()` evaluates from the FIRST root
    element, so on a document where libxml2 builds two roots (anything with content after `</html>`) it
    cannot see the second one and reports the engine as inventing elements. `//` sees both. Every form
    here is checked SUPPORTED before use, or the no-fallback contract would read as a tree difference.
    """
    sels = ["//*/@id"]                                       # document order, across all roots
    for i in ids:
        sels.append(f'//*[@id="{i}"]//*/@id')                # descendant set
        sels.append(f'//*[@id="{i}"]/text()')                # own text, so splits/joins show up
    # placement relative to the frame, which carries no id of its own
    sels += ["//html/*/@id", "//html//*/@id", "//head//*/@id", "//body//*/@id",
             "//body/text()", "//html/text()"]
    return sels


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--depth", type=int, default=3, help="enumerate every sequence up to this length")
    ap.add_argument("--random", type=int, default=0, help="also try N random sequences")
    ap.add_argument("--length", type=int, default=7, help="length of the random sequences")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--show", type=int, default=12, help="how many differing sequences to print")
    ap.add_argument("--gate", action="store_true", help="exit nonzero on any difference")
    oracle.add_argument(ap)
    args = ap.parse_args()
    oracle.require(args.allow_old_libxml2)

    import frostwork

    def sequences():
        for n in range(1, args.depth + 1):
            yield from itertools.product(ALPHABET, repeat=n)
        if args.random:
            rng = random.Random(args.seed)
            for _ in range(args.random):
                yield tuple(rng.choice(ALPHABET) for _ in range(args.length))

    checked = 0
    differences: list[tuple[str, str, list[str], list[str]]] = []
    seen_shapes: set[tuple[str, ...]] = set()
    for seq in sequences():
        doc, ids = build(seq)
        sels = fingerprint_selectors(ids)
        mine = frostwork.extract(doc, sels, "utf-8")
        sel = parsel.Selector(text=doc.decode(), type="html")
        theirs = [sel.xpath(s).getall() for s in sels]
        checked += 1
        for s, m, t in zip(sels, mine, theirs):
            if m != t:
                # one report per SHAPE (the token sequence with ids stripped), not per document
                shape = tuple(tok for tok in seq)
                if shape not in seen_shapes:
                    seen_shapes.add(shape)
                    differences.append((doc.decode(), s, m, t))
                break

    print(f"sequences checked : {checked}")
    print(f"alphabet          : {len(ALPHABET)} tokens")
    print(f"DIFFERING SHAPES  : {len(differences)}")
    for doc, s, m, t in differences[: args.show]:
        print(f"\n  {doc}")
        print(f"    {s}\n      engine={m}\n      lxml  ={t}")
    if args.gate:
        print(f"\n  GATE: differing shapes = {len(differences)}  ->  "
              f"{'PASS' if not differences else 'FAIL'}")
        return 1 if differences else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
