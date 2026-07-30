"""
tools/mutate_rules.py — MUTATION-test the tree-construction rule tables.

Two different questions have to be asked about a rule table, and this repo could only answer one of them:

    is every cell RIGHT?          -> tools/audit_tree_rules.py  (asks lxml about each cell)
    if a cell were WRONG,
    would anything go red?        -> this tool

The second is the one that finds blind spots without a human first guessing where they are. Every gap
closed here so far was noticed by a person reading code and thinking "hold on, nothing covers that": the
`dd`/`dt` arm, the missing `colgroup` rule, CSS escapes in selectors. That does not scale and it already
missed things three review rounds running. This enumerates instead: flip one cell, run the gates, record
which ones noticed. A cell no gate notices is a cell where a future edit is unprotected.

It needs the `mutate` cargo feature, which turns FROSTWORK_MUTATE into a one-cell override so a single
build serves every mutant (~400 rebuilds would take hours):

    cargo build --release --features mutate
    .venv/bin/maturin develop --release --features python,mutate
    .venv/bin/python tools/mutate_rules.py --sample 40      # a few minutes
    .venv/bin/python tools/mutate_rules.py --all            # the full sweep

    # then put the normal build back — a mutate build must never be shipped or benchmarked:
    cargo build --release && .venv/bin/maturin develop --release

`--all` is the nightly form. Output is a per-detector matrix plus the list of SURVIVING mutants (no
detector went red), which is the actionable part.

Reading the results: a surviving mutant is not automatically a bug in the suite. Some cells are
unreachable in any realistic markup, and some are genuinely equivalent under mutation. But every survivor
is a cell where the tables are asserted rather than tested, so each one needs a verdict — a new
differential family, an audit row, or a written note saying why it cannot matter.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

PY = os.path.join(ROOT, ".venv", "bin", "python")


def tag_ids() -> dict[str, int]:
    """Read `tag::*` straight out of the Rust source — no second copy of the table to drift."""
    src = open(os.path.join(ROOT, "src", "implied_close.rs")).read()
    block = re.search(r"pub mod tag \{(.*?)\n\}", src, re.S)
    if not block:
        raise SystemExit("could not find `pub mod tag` in src/implied_close.rs")
    ids = {m[1].lower(): int(m[2]) for m in
           re.finditer(r"pub const (\w+): u8 = (\d+);", block.group(1))}
    if not ids:
        raise SystemExit("no tag ids parsed from src/implied_close.rs")
    return ids


# names whose <p>-closing membership is a real question: libxml2's HTML4 block list, plus the HTML5-era
# sectioning elements it does NOT close <p> on (the arm that was wrong before).
PCLOSE_NAMES = [
    "address", "blockquote", "center", "dir", "div", "dl", "fieldset", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "menu", "ol", "pre", "ul",
    "section", "article", "aside", "header", "footer", "nav", "main", "figure",
    "details", "hgroup", "span", "b",
]
# the void set, plus the HTML5-era names libxml2 deliberately does NOT treat as void
VOID_NAMES = ["area", "base", "br", "col", "hr", "img", "input", "link", "meta", "param",
              "embed", "source", "track", "wbr"]


def mutants(ids: dict[str, int]) -> list[tuple[str, str]]:
    """(spec, human label) for every rule cell that can be flipped."""
    out: list[tuple[str, str]] = []
    by_id = {v: k for k, v in ids.items()}
    for top in sorted(by_id):
        for start in sorted(by_id):
            out.append((f"cell:{start},{top}", f"implies_close({by_id[start]} closes {by_id[top]})"))
    for tid in sorted(by_id):
        out.append((f"scope:{tid}", f"table_scope({by_id[tid]})"))
    for n in VOID_NAMES:
        out.append((f"void:{n}", f"void({n})"))
    for n in PCLOSE_NAMES:
        out.append((f"pclose:{n}", f"p_closed_by({n})"))
    return out


class Detector:
    def __init__(self, name: str, argv: list[str], why: str):
        self.name, self.argv, self.why = name, argv, why
        self.caught: list[str] = []
        self.secs = 0.0

    def run(self, env: dict[str, str]) -> bool:
        """True == went RED (noticed the mutation)."""
        t = time.perf_counter()
        p = subprocess.run(self.argv, cwd=ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.secs += time.perf_counter() - t
        if p.returncode == 2:
            # reserved by the harness for "oracle unusable" — not a verdict about the mutation
            raise SystemExit(f"{self.name}: exit 2 (oracle/toolchain guard). Run it directly to see why.")
        return p.returncode != 0


def build_detectors(with_differential: bool, corpus: str | None) -> list[Detector]:
    ds = [
        Detector("unit", ["cargo", "test", "--features", "mutate", "-q"],
                 "the hand-written vectors in src/"),
        Detector("audit", [PY, "tools/audit_tree_rules.py", "--gate"],
                 "asks lxml about every rule cell"),
        Detector("corpus-fixtures", [PY, "tools/bench_corpus.py", "tests/corpus", "--gate"],
                 "self-authored pages shaped like what broke us"),
    ]
    if corpus:
        ds.append(Detector("corpus-real", [PY, "tools/bench_corpus.py", corpus, "--gate", "--limit", "12"],
                           "real fetched pages (tools/corpus_fetch.py)"))
    if with_differential:
        # a reduced workload: the question is whether the family SHAPE reaches this cell at all, and a
        # rule error that only a rare page shows is exactly the "asserted, not tested" case anyway
        ds.append(Detector("differential", [PY, "tools/diff_lxml.py", "--conformant", "400",
                                            "--foreign", "150", "--grouped", "300", "--pages", "40"],
                           "generated-page differential vs lxml (reduced)"))
    return ds


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sample", type=int, default=40, help="mutants to test (stratified); 0 = all")
    ap.add_argument("--all", action="store_true", help="test every mutant (the nightly form)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--corpus", default="fixtures/realweb",
                    help="real-page corpus dir for the corpus-real detector ('' to skip)")
    ap.add_argument("--with-differential", action="store_true",
                    help="also run a reduced diff_lxml per mutant (slower, more informative)")
    ap.add_argument("--only", help="SEMICOLON-separated specs to test (e.g. 'cell:2,0;scope:3' — the "
                                   "separator is ';' because a cell spec already contains a comma). Use "
                                   "this to re-test the survivors after widening a gate.")
    ap.add_argument("--gate", action="store_true", help="exit nonzero if any mutant SURVIVES")
    ap.add_argument("--json", help="write the full matrix here")
    args = ap.parse_args()

    ids = tag_ids()
    all_m = mutants(ids)
    rng = random.Random(args.seed)
    if args.only:
        want = [x.strip() for x in args.only.split(";") if x.strip()]
        known = dict(all_m)
        missing = [w for w in want if w not in known]
        if missing:
            raise SystemExit(f"unknown mutant spec(s): {missing}")
        chosen = [(w, known[w]) for w in want]
    elif args.all or args.sample == 0:
        chosen = all_m
    else:
        # stratify by kind so a small sample still covers all four tables
        by_kind: dict[str, list[tuple[str, str]]] = {}
        for spec, label in all_m:
            by_kind.setdefault(spec.split(":")[0], []).append((spec, label))
        chosen = []
        per = max(1, args.sample // len(by_kind))
        for kind, items in sorted(by_kind.items()):
            chosen += rng.sample(items, min(per, len(items)))
        chosen = chosen[: args.sample]

    corpus = args.corpus if args.corpus and os.path.isdir(os.path.join(ROOT, args.corpus)) else None
    if args.corpus and not corpus:
        print(f"note: --corpus {args.corpus} not found; skipping the real-page detector "
              f"(build it with tools/corpus_fetch.py)\n")
    dets = build_detectors(args.with_differential, corpus)

    print("RULE-TABLE MUTATION SWEEP")
    print(f"  mutants   : {len(chosen)} of {len(all_m)}"
          f"{' (FULL)' if len(chosen) == len(all_m) else f' (sample, seed={args.seed})'}")
    print(f"  detectors : {', '.join(d.name for d in dets)}\n")

    # sanity: with no mutation every detector must be GREEN, or a "caught" result means nothing
    clean_env = {k: v for k, v in os.environ.items() if k != "FROSTWORK_MUTATE"}
    for d in dets:
        if d.run(clean_env):
            raise SystemExit(f"{d.name} is RED on the UNMUTATED build — fix that first, "
                             f"or every mutant looks caught")
    print("  baseline  : all detectors green on the unmutated build\n")

    survivors: list[tuple[str, str]] = []
    matrix: dict[str, list[str]] = {}
    t0 = time.perf_counter()
    for n, (spec, label) in enumerate(chosen, 1):
        env = dict(clean_env, FROSTWORK_MUTATE=spec)
        caught_by = []
        for d in dets:
            if d.run(env):
                d.caught.append(spec)
                caught_by.append(d.name)
        matrix[spec] = caught_by
        if not caught_by:
            survivors.append((spec, label))
        mark = ",".join(caught_by) if caught_by else "*** SURVIVED ***"
        rate = (time.perf_counter() - t0) / n
        eta = rate * (len(chosen) - n)
        print(f"  [{n:>3}/{len(chosen)}] {label:<46} {mark}"
              f"{'' if n % 10 else f'   (eta {eta / 60:.0f}m)'}")

    print(f"\n  elapsed: {(time.perf_counter() - t0) / 60:.1f} min")
    print("\nDETECTION BY GATE (how many of the tested mutants each one noticed)")
    for d in dets:
        pct = 100.0 * len(d.caught) / max(1, len(chosen))
        print(f"  {d.name:<17} {len(d.caught):>4}/{len(chosen)}  ({pct:5.1f}%)  {d.secs / 60:5.1f} min"
              f"   — {d.why}")

    only_audit = [s for s, c in matrix.items() if c == ["audit"]]
    print(f"\n  cells ONLY the rule audit catches: {len(only_audit)}")
    print("    (i.e. no page-based gate reaches them — the audit is the single point of failure there,")
    print("     which is why a new rule needs an audit ROW and not just a passing differential)")

    print(f"\nSURVIVORS (no detector noticed): {len(survivors)}")
    for spec, label in survivors:
        print(f"    {spec:<22} {label}")
    if not survivors:
        print("    none — every tested cell is protected by at least one gate")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"matrix": matrix, "survivors": [s for s, _ in survivors],
                       "detectors": {d.name: d.caught for d in dets}}, fh, indent=2)
        print(f"\nwrote {args.json}")

    print("\nREMEMBER: put the normal build back — "
          "cargo build --release && .venv/bin/maturin develop --release")
    if args.gate:
        print(f"\n  GATE: surviving mutants = {len(survivors)}  ->  "
              f"{'PASS' if not survivors else 'FAIL'}")
        return 1 if survivors else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
