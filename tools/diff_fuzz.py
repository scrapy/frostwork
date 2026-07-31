"""
tools/diff_fuzz.py — mutation-based DIFFERENTIAL fuzzer (bridges L2 + L4 of docs/TESTING.md).

The two existing robustness layers have an inverted coverage hole:
  * the cargo-fuzz target (fuzz/) feeds ARBITRARY bytes but only asserts *panic-freedom* — never parity;
  * the differential (diff_lxml.py) proves *value-parity* but only on WELL-FORMED input (conformant.py).
So malformed HTML — precisely where a treeless, tree-construction-emulating engine is most likely to
DIVERGE silently (implied-close boundaries, misnesting, foster-parenting, unterminated rawtext) rather
than crash — is checked for crashes but never for correctness.

This closes that hole: take well-formed conformant pages (and, with --corpus, the checked-in fuzz
seeds), apply structural + byte-level mutations to break them, then compare Frostwork to lxml/Parsel on
that adversarial distribution using the SAME verdict function as the gate.

Unlike diff_lxml.py this is a DISCOVERY tool, not a clean gate. Malformed input legitimately triggers
the documented SKIP set (foster-parenting / adoption-agency, not ported), so DIVERGE > 0 is EXPECTED —
its job is to SURFACE, CLUSTER (by mutation signature), and MINIMIZE candidate divergences for triage.
The one hard invariant is CRASH == 0: any panic/hang on any input is a real bug regardless of parity.
Use --gate to make CRASH a nonzero exit (suitable for a nightly CI job).

Usage:
  .venv/bin/python tools/diff_fuzz.py [--iters N] [--per K] [--seed S]
                                      [--corpus] [--minimize M] [--only NAME] [--gate] [--show K]
"""
from __future__ import annotations

import argparse
import glob
import os
import random
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conformant  # well-formed base-page generator
import foreign  # svg/math/template base pages (mutating foreign content is a rich desync surface)
from diff_lxml import run_engine, parsel_vals, verdict, is_node_query, _batches  # engine driver + oracle + grader
import audit_tree_rules  # the single Python home for the engine's tag tables (checked against lxml there)
import oracle  # same libxml2 >= 2.14 requirement (an older oracle invents divergences of its own)

CORPUS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fuzz", "corpus", "extract")

# Stray fragments that libxml2 reshapes (foster-parenting / adoption-agency bait).
_FOSTER = [
    "<td>stray-cell</td>", "<tr><td>x</td></tr>", "<table>orphan", "<title>t</title>",
    "<b>1<p>2</b>3</p>", "<a>1<div>2<a>3</a></div></a>", "<i>a<b>b</i>c</b>",
    "</div></p></span>", "<form><form>x</form></form>",
]
_STRAY_CLOSE = ["</p>", "</div>", "</table>", "</li>", "</a>", "</b>", "</tr>", "</td>", "</ul>"]
_INSERT_BYTES = [b"<", b">", b"</", b"</x>", b'"', b"'", b"=", b"&", b"<!--", b"-->", b"<![CDATA[",
                 b"<svg>", b"<math>", b"<template>", b"\x00", b"</script>"]


# --------------------------------------------------------------------------- structural mutations (str)
def m_drop_end_tag(rng, s):
    ends = list(re.finditer(r"</[a-zA-Z][^>]*>", s))
    if not ends:
        return s
    m = rng.choice(ends)
    return s[: m.start()] + s[m.end():]


def m_unterminate_rawtext(rng, s):
    ends = list(re.finditer(r"</(?:script|style|textarea)\s*>", s, re.I))
    if not ends:
        return s
    m = rng.choice(ends)
    return s[: m.start()] + s[m.end():]


def m_swap_adjacent_close(rng, s):
    pairs = list(re.finditer(r"(</[^>]+>)(</[^>]+>)", s))
    if not pairs:
        return s
    m = rng.choice(pairs)
    return s[: m.start()] + m.group(2) + m.group(1) + s[m.end():]


def m_foster(rng, s):
    frag = rng.choice(_FOSTER)
    i = s.find("<body>")
    at = i + len("<body>") if i >= 0 else 0
    return s[:at] + frag + s[at:]


def m_stray_close(rng, s):
    gts = [m.end() for m in re.finditer(r">", s)]
    if not gts:
        return s
    at = rng.choice(gts)
    return s[:at] + rng.choice(_STRAY_CLOSE) + s[at:]


def m_dup_attr(rng, s):
    # duplicate an existing class="..." on some start tag (libxml2 keeps first, drops later duplicates)
    starts = list(re.finditer(r'(<[a-zA-Z]+[^>]*?)(\sclass="[^"]*")([^>]*>)', s))
    if not starts:
        return s
    m = rng.choice(starts)
    return s[: m.start()] + m.group(1) + m.group(2) + " " + m.group(2).strip() + m.group(3) + s[m.end():]


def m_lt_in_attr(rng, s):
    vals = list(re.finditer(r'="[^"<>]*"', s))
    if not vals:
        return s
    m = rng.choice(vals)
    return s[: m.end() - 1] + "<x>" + s[m.end() - 1:]


def m_self_close_nonvoid(rng, s):
    starts = list(re.finditer(r"<(?:div|p|span|section|li|td|a)\b[^>/]*>", s))
    if not starts:
        return s
    m = rng.choice(starts)
    return s[: m.end() - 1] + "/>" + s[m.end():]


def m_comment_bait(rng, s):
    at = rng.choice([m.start() for m in re.finditer(r"<", s)] or [0])
    frag = rng.choice(["<!-->", "<!-- -- -->", "<!--unterminated", "<!--x--!>", "<?pi?>", "<!bogus>"])
    return s[:at] + frag + s[at:]


def m_deep_nest(rng, s):
    n = rng.randint(50, 400)  # probes matcher stack depth without ballooning the input
    i, j = s.find("<body>"), s.rfind("</body>")
    if i < 0 or j < 0:
        return ("<div>" * n) + s + ("</div>" * n)
    a = i + len("<body>")
    return s[:a] + "<div>" * n + s[a:j] + "</div>" * n + s[j:]


# --------------------------------------------------------------------------- byte-level mutations (bytes)
def m_truncate(rng, b):
    if len(b) < 2:
        return b
    return b[: rng.randint(1, len(b) - 1)]


def m_drop_bytes(rng, b):
    if len(b) < 4:
        return b
    i = rng.randint(0, len(b) - 2)
    return b[:i] + b[i + rng.randint(1, min(8, len(b) - i)):]


def m_insert_bytes(rng, b):
    i = rng.randint(0, len(b))
    return b[:i] + rng.choice(_INSERT_BYTES) + b[i:]


def m_dup_slice(rng, b):
    if len(b) < 4:
        return b
    i = rng.randint(0, len(b) - 2)
    j = rng.randint(i + 1, min(len(b), i + 40))
    return b[:j] + b[i:j] + b[j:]


def m_corrupt_angle(rng, b):
    pos = [i for i, c in enumerate(b) if c in (0x3C, 0x3E)]  # '<' or '>'
    if not pos:
        return b
    i = rng.choice(pos)
    return b[:i] + b" " + b[i + 1:]


STR_MUTS = [
    ("drop_end_tag", m_drop_end_tag), ("unterminate_rawtext", m_unterminate_rawtext),
    ("swap_adjacent_close", m_swap_adjacent_close), ("foster", m_foster),
    ("stray_close", m_stray_close), ("dup_attr", m_dup_attr), ("lt_in_attr", m_lt_in_attr),
    ("self_close_nonvoid", m_self_close_nonvoid), ("comment_bait", m_comment_bait),
    ("deep_nest", m_deep_nest),
]
BYTE_MUTS = [
    ("truncate", m_truncate), ("drop_bytes", m_drop_bytes), ("insert_bytes", m_insert_bytes),
    ("dup_slice", m_dup_slice), ("corrupt_angle", m_corrupt_angle),
]
ALL_MUTS = {n: (kind, fn) for kind, lst in (("str", STR_MUTS), ("bytes", BYTE_MUTS)) for n, fn in lst}


def apply_mut(rng, data, name):
    kind, fn = ALL_MUTS[name]
    try:
        if kind == "str":
            return fn(rng, data.decode("utf-8", "replace")).encode("utf-8", "replace")
        return fn(rng, data)
    except Exception:
        return data  # a mutation that can't apply is a no-op, never a harness failure


def mutate(rng, base, per, only):
    names = [only] * per if only else [rng.choice(list(ALL_MUTS)) for _ in range(per)]
    data = base
    for n in names:
        data = apply_mut(rng, data, n)
    return data, tuple(sorted(set(names)))


# ------------------------------------------------------------------------------------------ single-case
def diverges(html, sel):
    """True iff this single (html, sel) diverges from lxml (and does not crash) — used by the minimizer."""
    results, crashed, _ = run_engine([(html, [sel])])
    if crashed or not results:
        return False  # a crash is handled separately; the minimizer only shrinks value divergences
    mine = results[0][0] if results[0] else []
    return verdict(mine, parsel_vals(html, sel), "CONTROL", sel) in ("DIVERGE", "CRASH")


def minimize(html, sel, budget):
    """ddmin-lite: greedily delete byte spans while the divergence survives. Bounded by `budget` runs."""
    cur, runs = html, [0]

    def check(h):
        runs[0] += 1
        return runs[0] <= budget and h and diverges(h, sel)

    gran = max(1, len(cur) // 2)
    while gran >= 1 and runs[0] < budget:
        i, shrunk = 0, False
        while i < len(cur) and runs[0] < budget:
            cand = cur[:i] + cur[i + gran:]
            if check(cand):
                cur, shrunk = cand, True
            else:
                i += gran
        if not shrunk:
            gran //= 2
    return cur


# --------------------------------------------------------------- documented-construct attribution
# A bulk "DIVERGE > 0 is expected" bucket is how a real bug hides: the unmatched-end-tag text split
# (Sphinx emits it) sat in this tool's output for a long time, clustered under a `foster` mutation
# signature, indistinguishable from the accepted adoption-agency cases. So attribute every divergence
# to the documented construct that explains it, and treat anything left over as NOVEL — a candidate bug.
_FORMATTING = {"a", "b", "i", "em", "strong", "small", "label", "font", "u", "s", "big", "tt", "nobr"}
# The tag tables are the ENGINE's rules, so they live in one Python place — `audit_tree_rules`, which
# checks them cell-by-cell against lxml. Copying them here is how they drift: an earlier copy still
# listed `option`/`optgroup`/`thead` as `<p>`-closers after that was proved wrong, which silently
# attributed every `<p>`-nesting divergence to `deep-p` and kept it out of the NOVEL bucket this gate
# is built around.
_TABLE_SCOPED = set(audit_tree_rules.TABLE_SCOPED)
_HEAD_ONLY = {"title", "base", "meta", "link", "basefont"}
_VOID = set(audit_tree_rules.VOID)
# closes an open `<p>`: the block set plus list/table ITEMS — NOT option/optgroup/thead/rt/rp
_P_KEEPERS = set(audit_tree_rules.BLOCK) | {
    "li", "dd", "dt", "td", "th", "tr", "tbody", "tfoot", "caption"}
_TAG_RE = re.compile(rb"<(/?)([a-zA-Z][^\s/>]*)")

# Constructs docs/COMPATIBILITY.md lists as accepted divergences. Anything NOT in here that explains a
# divergence is a bug: `unmatched-end` is deliberately absent — it is now ported, so if it ever shows
# up as the sole explanation again, that is the regression we want the gate to catch.
# `nested-form` is deliberately ABSENT: `<form>` closing an open `<form>` is implemented now (libxml2's
# start-close pair table, `implied_close::start_closes`), so if a nested form ever explains a divergence
# again that is a REGRESSION and belongs in NOVEL — the same reasoning that keeps `unmatched-end` out.
DOCUMENTED = {"foster", "misnest", "deep-p", "head-in-body", "fragment", "outer-html",
              "truncated-tag", "after-html"}


def constructs(html: bytes) -> set:
    """Which documented-divergence constructs this (page, selector) contains. Approximate but
    conservative: a cheap stack walk over tag tokens, no tree build (we cannot use lxml's own tree
    here — the point is to classify WITHOUT trusting either implementation)."""
    found = set()
    low = html.lower()
    if b"<body" not in low:
        found.add("fragment")
    # A tag left unterminated by EOF (a response cut off mid-tag). libxml2 discards the incomplete tag
    # outright; the engine, which has already seen the attributes, keeps them.
    last = html.rfind(b"<")
    if last != -1 and _TAG_RE.match(html, last) and b">" not in html[last:]:
        found.add("truncated-tag")
    # Content after `</html>`: libxml2 discards it, the engine KEEPS it (browser behavior) — a
    # deliberate divergence, so it explains a disagreement rather than being a candidate bug.
    close = low.rfind(b"</html>")
    if close != -1 and html[close + 7:].strip():
        found.add("after-html")
    stack = []
    for m in _TAG_RE.finditer(html):
        closing, name = m.group(1), m.group(2).decode("latin-1").lower()
        if closing:
            if name in stack:
                k = len(stack) - 1 - stack[::-1].index(name)
                # elements implicitly closed above the match: a formatting element crossing a block
                # boundary is the adoption-agency signature
                above = stack[k + 1:]
                if above and (name in _FORMATTING or any(a in _FORMATTING for a in above)):
                    found.add("misnest")
                del stack[k:]
            else:
                found.add("unmatched-end")
            continue
        if name in _VOID:
            continue
        if name in _TABLE_SCOPED and "table" not in stack:
            found.add("foster")
        if stack and stack[-1] == "table" and name not in _TABLE_SCOPED:
            found.add("foster")
        if name in _HEAD_ONLY and "body" in stack:
            found.add("head-in-body")
        if name == "form" and "form" in stack:
            # Kept as an attribution LABEL (it still says something useful about a page) but no longer
            # documented: libxml2 closes the outer form and opens a sibling, and so does the engine.
            found.add("nested-form")
        if name in _P_KEEPERS and "p" in stack and stack[-1] != "p":
            found.add("deep-p")
        stack.append(name)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=5000, help="mutated pages to test")
    ap.add_argument("--per", type=int, default=2, help="mutations applied per page (1-3 is the sweet spot)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--corpus", action="store_true", help="also mutate the checked-in fuzz corpus seeds")
    ap.add_argument("--minimize", type=int, default=0, help="minimize up to N divergent repros (0=off)")
    ap.add_argument("--only", help="apply only this mutation (triage a single class)")
    ap.add_argument("--gate", action="store_true", help="exit nonzero on CRASH, or NOVEL over budget")
    # The NOVEL tail is a known, triaged residue (malformed-input framing: a corrupt `<html>` root, no
    # implied `</head>` before `<body>`, a truncated attribute swallowing the rest of the document) — not
    # zero, so the gate is a RATE not an absolute. Tightening this number is the way to work that tail
    # down; raising it needs a reason.
    #
    # 0.10% -> 0.05%: "NUL inside a tag name" left this list when raw NUL started being deleted before
    # tokenization (the engine used to drop it only from emitted values), and the data modes
    # (`iframe`/`noembed`/`xmp`/`plaintext`) took the fabricated-element cases with them. Measured over
    # seeds 0-3 at 6000 iters: 0.0097%, 0.0143%, 0.0133%, 0.0128% of ~1.1M pairs — so 0.05% keeps ~3.5x
    # headroom over the worst seed while halving the room a future regression can hide in.
    ap.add_argument("--novel-budget", type=float, default=0.05,
                    help="max %% of pairs allowed in the NOVEL bucket under --gate (default 0.05%%)")
    ap.add_argument("--show", type=int, default=8)
    oracle.add_argument(ap)
    args = ap.parse_args()
    oracle.require(args.allow_old_libxml2)
    if args.only and args.only not in ALL_MUTS:
        sys.exit(f"unknown --only {args.only!r}; choices: {', '.join(ALL_MUTS)}")
    rng = random.Random(args.seed)

    nbase = max(200, args.iters // 8)
    bases = [conformant.generate(rng) for _ in range(nbase)]
    bases += [foreign.generate(rng) for _ in range(nbase // 3)]  # mix in foreign-content base pages
    if args.corpus:
        seeds = []
        for p in sorted(glob.glob(os.path.join(CORPUS_DIR, "*")))[:2000]:
            try:  # a running `cargo fuzz` mutates this dir live — skip files that vanish mid-read
                with open(p, "rb") as f:
                    seeds.append(f.read())
            except OSError:
                continue
        bases += [s for s in seeds if s]
    if not bases:
        sys.exit("no base pages")

    # batched to stay inside the engine's fixed-width member budget (`differ` panics past it, since
    # over-budget columns come back empty and would read as divergence in every one of them)
    batches = _batches(conformant.BASKET)
    cases, sigs = [], []
    for _ in range(args.iters):
        html, sig = mutate(rng, rng.choice(bases), args.per, args.only)
        for batch in batches:
            cases.append((html, batch))
            sigs.append(sig)

    stat = defaultdict(int)
    cluster = defaultdict(lambda: defaultdict(int))  # mutation-sig -> verdict -> count
    examples, seen_ex, crash_diags = [], set(), []
    attributed = defaultdict(int)          # documented construct -> divergences it explains
    novel, seen_novel = [], set()          # divergences NO documented construct explains

    CHUNK = 500  # localize a panic to its chunk so one crash doesn't blind the rest of the run
    for base_i in range(0, len(cases), CHUNK):
        chunk = cases[base_i: base_i + CHUNK]
        results, crashed, diag = run_engine(chunk)
        stat["CRASH"] += crashed
        if diag:
            crash_diags.append((base_i, diag))
        for k, (html, case_sels) in enumerate(chunk):
            sig = sigs[base_i + k]
            mine_cols = results[k] if k < len(results) else None
            if mine_cols is None:
                continue  # undelivered: already counted as CRASH via _crash_check
            page_constructs = None
            for si, sel in enumerate(case_sels):
                mine = mine_cols[si] if si < len(mine_cols) else []
                theirs = parsel_vals(html, sel)
                v = verdict(mine, theirs, "CONTROL", sel)
                stat[v] += 1
                stat["pairs"] += 1
                cluster[sig][v] += 1
                if v == "DIVERGE":
                    if page_constructs is None:
                        page_constructs = constructs(html)  # page-scoped, so safe to memoize
                    why = (page_constructs | ({"outer-html"} if is_node_query(sel) else set())) & DOCUMENTED
                    if why:
                        for c in why:
                            attributed[c] += 1
                    else:
                        stat["NOVEL"] += 1
                        nkey = (sig, sel)
                        if nkey not in seen_novel and len(novel) < args.show:
                            seen_novel.add(nkey)
                            novel.append([sig, sel, mine[:4], (theirs or [])[:4], html])
                    key = (sig, sel)
                    if key not in seen_ex and len(examples) < args.show:
                        seen_ex.add(key)
                        examples.append([sig, sel, mine[:4], (theirs or [])[:4], html])

    pairs = stat["pairs"] or 1
    print(f"DIFFERENTIAL FUZZ vs lxml   seed={args.seed}  iters={args.iters}  per={args.per}"
          f"{'  only=' + args.only if args.only else ''}  pairs={stat['pairs']}\n")
    print(f"  AGREE          {stat['AGREE']:>9}  ({100.0 * stat['AGREE'] / pairs:.2f}%)")
    print(f"  WS-only        {stat['WS']:>9}")
    print(f"  DIVERGE        {stat['DIVERGE']:>9}  (documented SKIP constructs + NOVEL, split below)")
    print(f"  NOVEL          {stat['NOVEL']:>9}  <-- gate: no documented construct explains these")
    print(f"  CRASH          {stat['CRASH']:>9}  <-- HARD gate: must be 0\n")

    print("  DIVERGE attributed to a documented construct (a divergence can have >1 explanation):")
    for c, n in sorted(attributed.items(), key=lambda kv: -kv[1]):
        print(f"    {c:<40} {n:>6}")
    if not attributed:
        print("    (none)")
    print()

    print("  DIVERGE by mutation signature (which breakage the disagreement clusters under):")
    ranked = sorted(cluster.items(), key=lambda kv: -kv[1]["DIVERGE"])
    for sig, d in ranked:
        tot = sum(d.values()) or 1
        if d["DIVERGE"]:
            print(f"    {'+'.join(sig):<40} DIVERGE {d['DIVERGE']:>6} / {tot:<6} "
                  f"({100.0 * d['DIVERGE'] / tot:.1f}%)")

    if args.minimize and examples:
        print(f"\n  minimizing up to {args.minimize} repros (ddmin, byte-span deletion)...")
        for ex in examples[: args.minimize]:
            ex[4] = minimize(ex[4], ex[1], budget=400)

    if examples:
        print("\n  candidate divergences (triage: is this a real bug or a documented SKIP-set case?):")
        for sig, sel, mine, theirs, html in examples:
            snip = html.decode("utf-8", "replace")
            snip = snip if len(snip) <= 200 else snip[:200] + "…"
            print(f"    [{'+'.join(sig)}] {sel!r}\n        mine ={mine}\n        lxml ={theirs}"
                  f"\n        html ({len(html)}B): {snip!r}\n        hex : {html.hex()}")

    if novel:
        print("\n  NOVEL divergences — NO documented construct on the page explains these. This is the"
              "\n  bucket a real bug hides in; triage each one before dismissing it:")
        for sig, sel, mine, theirs, html in novel:
            snip = html.decode("utf-8", "replace")
            snip = snip if len(snip) <= 200 else snip[:200] + "…"
            print(f"    [{'+'.join(sig)}] {sel!r}\n        mine ={mine}\n        lxml ={theirs}"
                  f"\n        html ({len(html)}B): {snip!r}\n        hex : {html.hex()}")

    if crash_diags:
        print("\n  ENGINE CRASHES (each hex below is a panic repro — feed to `differ` or the fuzz target):")
        for where, diag in crash_diags:
            print(f"    [chunk@{where}] {diag}")

    if args.gate:
        novel_pct = 100.0 * stat["NOVEL"] / pairs
        over = novel_pct > args.novel_budget
        print(f"\n  GATE: CRASH = {stat['CRASH']} (must be 0); NOVEL = {stat['NOVEL']}"
              f" = {novel_pct:.3f}% of pairs (budget {args.novel_budget:.2f}%)"
              f"  ->  {'FAIL' if (stat['CRASH'] or over) else 'PASS'}")
        if over:
            print("  NOVEL over budget: a divergence class no documented construct explains has grown."
                  "\n  Triage the examples above — do NOT raise the budget to make this pass.")
        sys.exit(1 if (stat["CRASH"] or over) else 0)


if __name__ == "__main__":
    main()
