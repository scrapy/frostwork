"""
tools/retro_census.py: how often each of browser mode's retroactive rules fires on real pages.

A retroactive rule changes a decision the one-pass matcher has already made, so a document where one
fires is answered by the replay (the header of `src/matcher/browser/mod.rs`) at the cost of a second
pass. This counts, per rule, the fraction of pages where it fires, over corpora in the
`bench_corpus.py` layout (`<dir>/<page-object>/selectors.json` and `pages/*.html`). It reads the pages
where they are and writes nothing next to them.

A second `<html>` or `<body>` tag is counted twice: with any attribute at all, and when its attributes
change an answer to the page object's own selectors, which is the only case that costs a replay.

Usage:
  .venv/bin/python tools/retro_census.py fixtures/realweb ~/src/frostwork-crawl-lab/corpora/<sample> ...
  .venv/bin/python tools/retro_census.py --limit 2000 <dir>     # the first 2000 pages of each corpus
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DIFFER = os.path.join(HERE, "..", "target", "release", "differ")

# The bits of `Plan::retro_census`, in `src/matcher/browser/mod.rs`'s order.
RULES = [
    ("second <html>/<body> changes an answer", 1 << 0),
    ("foster parenting", 1 << 1),
    ("frameset replacing the body", 1 << 2),
    ("adoption agency with a furthest block", 1 << 3),
    ("<selectedcontent>", 1 << 4),
    ("second <html>/<body> with any attribute", 1 << 5),
    ("past the depth cap", 1 << 6),
]
ANY_REPLAY = sum(bit for name, bit in RULES if name != "second <html>/<body> with any attribute")
BITS = [bit for _name, bit in RULES] + [ANY_REPLAY]


def pages_of(corpus: str, limit: int) -> list[tuple[str, list[str]]]:
    """`(page path, its page object's selectors)` for up to `limit` pages of one corpus."""
    out: list[tuple[str, list[str]]] = []
    for po in sorted(glob.glob(os.path.join(corpus, "*", "selectors.json"))):
        try:
            with open(po, encoding="utf-8") as f:
                sels = [s for s in json.load(f).values() if isinstance(s, str) and "\t" not in s and "\n" not in s]
        except (OSError, ValueError):
            sels = []
        for page in sorted(glob.glob(os.path.join(os.path.dirname(po), "pages", "*.html"))):
            out.append((page, sels))
            if limit and len(out) >= limit:
                return out
    return out


def census(pages: list[tuple[str, list[str]]]) -> tuple[int, Counter]:
    """`(pages read, pages per rule bit)`. The differ answers a whole batch at once (it does not flush
    per line), so the batch is written before anything is read."""
    batch = "".join("\t".join(["R", path, *sels]) + "\n" for path, sels in pages)
    out = subprocess.run([DIFFER, "--parser", "browser"], input=batch, capture_output=True, text=True,
                         check=True).stdout.splitlines()
    assert len(out) == len(pages), "the differ answered a different number of pages"
    counts: Counter = Counter()
    read = 0
    for line in out:
        answer = json.loads(line)
        if answer is None:
            continue
        read += 1
        counts.update(bit for bit in BITS if answer & bit)
    return read, counts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpora", nargs="+")
    ap.add_argument("--limit", type=int, default=0, help="pages per corpus (0: all)")
    args = ap.parse_args()
    if not os.path.exists(DIFFER):
        print("build the differ first: cargo build --release", file=sys.stderr)
        return 2
    total_pages, total = 0, Counter()
    for corpus in args.corpora:
        n, counts = census(pages_of(corpus, args.limit))
        total_pages += n
        total.update(counts)
        print(f"{corpus}: {n} pages, replayed {counts[ANY_REPLAY]} ({100 * counts[ANY_REPLAY] / max(n, 1):.2f}%)")
    print(f"\n{total_pages} pages")
    for name, bit in RULES:
        print(f"  {name:50} {total[bit]:7}  {100 * total[bit] / max(total_pages, 1):6.2f}%")
    print(f"  {'any rule (the pages a replay answers)':50} {total[ANY_REPLAY]:7}  "
          f"{100 * total[ANY_REPLAY] / max(total_pages, 1):6.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
