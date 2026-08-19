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
build serves every mutant (a rebuild per mutant would take hours):

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
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

PY = os.path.join(ROOT, ".venv", "bin", "python")


# The name universe for close-decision cells, DERIVED from the oracle rather than remembered.
#
# It has to be independent of the engine's own tables: a name the engine wrongly treats as ordinary
# (`em`, `section`, and until now `listing`) has no cell to flip, but flipping it TO closing is exactly
# the check that a gate would notice such a mistake. Two hand-written attempts at this list both failed
# the same way — the first omitted the tag name `s` (93 unprobed cells), the second omitted `head`,
# `listing`, `xmp` and `plaintext` (whole missing rows in the engine).
#
# So the names come from `tools/gen_tree_rules`, which partitions the WHOLE element universe by measured
# behaviour: one representative per class. That is a real compression rather than a hopeful one — two
# names in a class are indistinguishable to every rule table by construction — and the per-name residual
# risk is covered because the rule AUDIT (this sweep's strongest detector) probes every name, not the
# representatives. A full name-pair sweep would be 142² mutants; at ~2.3s each that is about 13 hours.
def _derived_universe() -> tuple[list[str], list[str], list[str], list[str]]:
    from gen_tree_rules import ELEMENTS, Oracle, classify
    o = Oracle()
    _, by_class, _ = classify(o)
    void, modes = o.void(), o.modes()
    # Names that can never be the OPEN element when a start tag arrives, so `close:<any>,<name>` is
    # unobservable BY CONSTRUCTION rather than untested: a void element has no content, and a raw-text /
    # RCDATA / PLAINTEXT element's content is character data, so no start tag is tokenized inside it.
    # Restricting this to the VOID half left 39 `close:<X>,title` mutants in a full sweep's survivor list,
    # every one of them a false alarm — `title` is RCDATA. `html`/`body` are deliberately NOT here:
    # nothing closes them, but they ARE on the stack, so a mutation that makes something close one is
    # perfectly observable and must stay in the sweep.
    unobservable = sorted(t for t in ELEMENTS if void[t] or modes[t] != "normal")
    # Prefer an OBSERVABLE-as-open representative: a class holding both (e.g. `blockquote` with the void
    # `hr` and the raw-text `xmp`) would otherwise be skipped wholesale if the alphabetically-first name
    # happened to be the unobservable one, silently dropping every cell of a column that IS reachable.
    reps = []
    for names in by_class.values():
        obs = [n for n in sorted(names) if n not in unobservable]
        reps.append(obs[0] if obs else sorted(names)[0])
    return (sorted(reps), sorted(t for t in ELEMENTS if void[t]),
            sorted(t for t in ELEMENTS if not void[t]), unobservable)


CLOSE_NAMES, VOID_SET, NONVOID_SET, UNOBSERVABLE_AS_OPEN = _derived_universe()
# The data-mode table gets the WHOLE universe rather than representatives: it is one mutant per name
# (cheap), and "the raw-text universe was the four names we already knew about" is precisely the bug
# that let `iframe`/`noembed`/`xmp`/`plaintext` fabricate elements out of their own text content.
DATA_MODE_NAMES = sorted(VOID_SET + NONVOID_SET)
# Same reasoning for end-tag priority: one mutant per name, over every name that can be ON the stack when
# a stray end tag arrives (a void or raw-text element never is, so its priority cannot matter).
#
# `<html>` and `<head>` are excluded on top of that, and for a different reason than the close dimension
# keeps them: priority only matters for an element open STRICTLY ABOVE the match, and neither can be. An
# `<html>` has nothing below it but the match `</html>`, which `end_tag_discardable` exempts outright, and
# a misplaced `<head>` is ignored rather than inserted, so it is never on the stack at all.
#
# `<body>` was in this list and should not have been. The same reasoning was applied to it and it is
# WRONG: after a `</body>` libxml2 starts a second body wherever the next one is written, so a `<body>`
# can sit above a `</td>` — and it out-ranks every end tag there. That is a real cell on a real crawled
# page, which is why "unobservable" has to be measured rather than argued.
PRIORITY_UNOBSERVABLE = {"html", "head"}
PRIORITY_NAMES = sorted(set(DATA_MODE_NAMES) - set(UNOBSERVABLE_AS_OPEN) - PRIORITY_UNOBSERVABLE)
# `void:` mutants cover the whole void set plus the HTML5-era names libxml2 deliberately keeps OPEN,
# since flipping one of THOSE to void is the mistake the contract exists to prevent.
VOID_NAMES = VOID_SET + ["embed", "source", "track", "wbr"]


def mutants() -> list[tuple[str, str]]:
    """(spec, human label) for every rule cell that can be flipped."""
    out: list[tuple[str, str]] = []
    for top in CLOSE_NAMES:
        # Not a carve-out: `UNOBSERVABLE_AS_OPEN` is derived from the oracle, and such a name is never on
        # the stack when a start tag arrives, so the mutant cannot change any output. The first sweep
        # spent 70 mutants proving that for the void half the slow way; the raw-text half then produced
        # 39 `close:<X>,title` false survivors for exactly the same reason.
        if top in UNOBSERVABLE_AS_OPEN:
            continue
        for inc in CLOSE_NAMES:
            out.append((f"close:{inc},{top}", f"<{inc}> closes an open <{top}>"))
    for name in DATA_MODE_NAMES:
        out.append((f"mode:{name}", f"data_mode({name})"))
    # End-tag scope gets one mutant per NAME over the whole universe, not per pair: one table
    # (`end_priority`) feeds the answer, so there is no second rule to mask the flip the way a second,
    # overlapping table would. Per NAME rather than per tag-id, because an id enumeration can only reach
    # the ids the engine already has — leaving the ORDER inside the table machinery (`</tr>` may not
    # unwind a `<tbody>`) unprobed while the sweep reports full protection.
    for name in PRIORITY_NAMES:
        out.append((f"prio:{name}", f"end_priority({name})"))
    for n in VOID_NAMES:
        out.append((f"void:{n}", f"void({n})"))
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


# A mutation that MUST be noticed: `img` is void, so flipping it makes `<div><img>A</div>` put `A` inside
# the img, which the rule audit checks by name. If this is not detected then the build the detectors run
# against does not have the `mutate` feature live, and NOTHING in the run means anything.
CANARY_SPEC = "void:img"
CANARY_EVERY = 100


def check_canary(dets: list[Detector], clean_env: dict[str, str], where: str) -> None:
    """Abort unless a known-detectable mutation is still detected.

    "Survived" is only information if the mutation was applied at all, and the baseline check in `main`
    does not establish that — it only shows the detectors are green when NOTHING is mutated. A build that
    has lost the feature passes the baseline and then reports every remaining mutant as a survivor.

    This is not hypothetical. The `mutate` artifacts are shared state (a release binary plus whatever
    `maturin develop` last installed into the venv), so anything else that builds — another session in a
    worktree, a stray `make py` — silently swaps them mid-run. That happened during a full sweep and 451 of
    1621 mutants came back as a contiguous TAIL of false survivors: a result that reads as a catastrophic
    coverage collapse and means nothing. So the canary runs before the sweep and every `CANARY_EVERY`
    mutants, and a failure is fatal rather than a warning — a partially-inert run is worse than no run,
    because the survivor list is the output people act on.
    """
    env = dict(clean_env, FROSTWORK_MUTATE=CANARY_SPEC)
    for d in dets:
        p = subprocess.run(d.argv, cwd=ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if p.returncode not in (0, 2):
            return  # someone noticed — the hook is live
    raise SystemExit(
        f"\nCANARY FAILED {where}: the mutation `{CANARY_SPEC}` was noticed by NO detector, so the build "
        f"they run against does not have the `mutate` feature. Every result from here would be a false "
        f"survivor. Rebuild both artifacts and start again:\n"
        f"    cargo build --release --features mutate\n"
        f"    .venv/bin/maturin develop --release --features python,mutate\n"
        f"(and check nothing else is rebuilding them while the sweep runs — that is how this happened.)")


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
    ap.add_argument("--only", help="SEMICOLON-separated specs to test (e.g. 'close:dd,dt;prio:tbody' — "
                                   "separator is ';' because a cell spec already contains a comma). Use "
                                   "this to re-test the survivors after widening a gate.")
    ap.add_argument("--detectors", help="comma-separated subset to run (default: all). The `unit` "
                                       "detector costs ~2.7s per mutant and catches no start-close cell, "
                                       "so a full sc sweep is much faster without it.")
    ap.add_argument("--gate", action="store_true", help="exit nonzero if any mutant SURVIVES")
    ap.add_argument("--json", help="write the full matrix here")
    args = ap.parse_args()

    all_m = mutants()
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
    if args.detectors:
        want = {x.strip() for x in args.detectors.split(",") if x.strip()}
        unknown = want - {d.name for d in dets}
        if unknown:
            raise SystemExit(f"unknown detector(s): {sorted(unknown)}; have "
                             f"{sorted(d.name for d in dets)}")
        dets = [d for d in dets if d.name in want]

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
    print("  baseline  : all detectors green on the unmutated build")
    check_canary(dets, clean_env, "before the sweep")
    print(f"  canary    : `{CANARY_SPEC}` IS noticed, so the mutate build is live for the detectors\n")

    survivors: list[tuple[str, str]] = []
    matrix: dict[str, list[str]] = {}
    t0 = time.perf_counter()
    for n, (spec, label) in enumerate(chosen, 1):
        if n % CANARY_EVERY == 0:
            check_canary(dets, clean_env, f"at mutant {n}/{len(chosen)}")
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
