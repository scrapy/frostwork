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
  * every sequence of length <= `--depth`, plus a random sample at greater lengths for the shapes only
    depth reaches (the count is exponential in the alphabet, so the run prints it rather than this text);
  * a comparison of the ENTIRE TREE, not of a handful of selector values.

The tree comparison is the other half of the idea. Everything else here grades a few `::text` columns,
which sees a wrong tree only when a value happens to move; a page can be reshaped completely and still
answer `p::text` identically. Each generated element carries a unique id, and the fingerprint is
(document order, per-element descendant set, child set and own text, and the children and text of every
element the tree builder can synthesise without a tag). A difference names the element it is at, which
makes the reduction step almost free.

An element with no id is invisible to every per-id column, and the tree builder creates them: the frame
(`html`, `head`, `body`) and, in HTML5, an implied `tbody`/`tr`/`colgroup`. So the child-axis column sees
an id-less element BETWEEN two id'd ones (`<table id=0><tr id=1>` and the same with a `<tbody>` answer
every descendant and text column identically), the per-name columns see one that holds id'd children,
and the frame's children by POSITION see an empty one (HTML5 always builds a `<head>`). The document-order
column is a list, not a set: a reconstructed formatting element copies its original's id, so a clone is a
repeated id. `tools/diff_browser.py` grades the same columns against a browser.

Both sides are asked through the SAME selectors, so the fingerprint cannot encode one library's
convention (parsel's `#a *` includes the subject; that is fine as long as both sides are asked).

Usage:
  .venv/bin/python tools/seq_sweep.py --depth 3            # seconds
  .venv/bin/python tools/seq_sweep.py --depth 4 --gate     # the CI form
  .venv/bin/python tools/seq_sweep.py --depth 4 --random 20000 --length 7
"""
from __future__ import annotations

import argparse
import functools
import itertools
import os
import random
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import oracle  # noqa: E402  — libxml2 >= 2.15, same requirement as every other gate
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


# Element names a tree builder creates without a tag in the source, so they carry no id: libxml2's and
# HTML5's frame, and HTML5's table fix-ups.
SYNTHESISED: tuple[str, ...] = ("html", "head", "body", "tbody", "tr", "colgroup")
# How many of the root's children are read by position: head and body, or head and frameset, plus one.
FRAME_POSITIONS = 3


def fingerprint_selectors(ids: list[str]) -> list[str]:
    """Selectors whose combined answers determine the tree, id-less elements included.

    XPath, not CSS, and that is not a style choice: parsel's `.css()` evaluates from the FIRST root
    element, so on a document where libxml2 builds two roots (anything with content after `</html>`) it
    cannot see the second one and reports the engine as inventing elements. `//` sees both. `extract`
    validates strictly, so a form the engine does not support raises rather than reading as a tree
    difference. `tools/browser_oracle/tree.py` evaluates exactly these shapes and refuses any other.
    """
    sels = ["//*/@id"]                                       # document order, across all roots, duplicates kept
    for i in ids:
        sels.append(f'//*[@id="{i}"]//*/@id')                # descendant set
        sels.append(f'//*[@id="{i}"]/*/@id')                 # child set: sees an id-less element in between
        sels.append(f'//*[@id="{i}"]/text()')                # own text, so splits/joins show up
    # placement relative to the frame
    sels += ["//html//*/@id", "//head//*/@id", "//body//*/@id"]
    # every id-less element the builder can create: its id'd children and its own text
    for name in SYNTHESISED:
        sels += [f"//{name}/*/@id", f"//{name}/text()"]
    # the root's children by position, which is what shows an EMPTY one
    for k in range(1, FRAME_POSITIONS + 1):
        sels += [f"//html/*[{k}]/*/@id", f"//html/*[{k}]/text()"]
    return sels


@functools.lru_cache(maxsize=None)
def _takes_parser(parser: str) -> bool:
    import frostwork

    try:
        frostwork.extract(b"<p>x</p>", ["//p/text()"], "utf-8", parser=parser)  # type: ignore[call-arg]
    except TypeError:
        return False
    return True


def parser_kwargs(parser: str) -> dict[str, Any]:
    """`{"parser": parser}` if the installed extension takes that keyword, else `{}`.

    An extension without the keyword has one mode, lxml. This gate grades lxml mode and passes
    `parser="lxml"` explicitly whenever it can, so it keeps grading libxml2 behaviour whatever the
    default mode is. Probed once, by calling the engine.
    """
    return {"parser": parser} if _takes_parser(parser) else {}


def compare(sels: list[str], mine: list[list[str]],
            theirs: list[list[str]]) -> tuple[str, list[str], list[str]] | None:
    """The first fingerprint column whose answers differ, or `None` if the two trees are the same one.

    The whole verdict of this gate, kept as a function so `tests/test_gates.py` can seed a reshaped tree
    into it and require a difference: a comparison that structurally cannot see one reads exactly like a
    clean run.
    """
    if not len(sels) == len(mine) == len(theirs):
        return "<column count>", [str(len(mine))], [str(len(theirs))]
    for s, m, t in zip(sels, mine, theirs):
        if m != t:
            return s, m, t
    return None


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

    mode = parser_kwargs("lxml")
    print("engine            : " + ("frostwork.extract(..., parser='lxml')" if mode
                                    else "frostwork.extract without parser= (the extension's one mode, lxml)"))

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
        mine = frostwork.extract(doc, sels, "utf-8", **mode)
        sel = parsel.Selector(text=doc.decode(), type="html")
        theirs = [sel.xpath(s).getall() for s in sels]
        checked += 1
        diff = compare(sels, mine, theirs)
        if diff and seq not in seen_shapes:
            # one report per SHAPE (the token sequence), not per document
            seen_shapes.add(seq)
            differences.append((doc.decode(), *diff))

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
