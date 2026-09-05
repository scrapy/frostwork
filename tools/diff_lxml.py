"""
Frostwork differential harness (L2/L3 of docs/TESTING.md): drive the bespoke engine over many
permutations of source HTML and grade every (page x selector) pair against lxml/Parsel.

Two generators feed the engine's `differ` binary (one long-running process, hex-framed protocol):
  * FAMILIES  — the tagged optional-end-tag constructs from tools/families.py. Each is bucketed
                SHOULD / SKIP / CONTROL, so a divergence auto-classifies: SHOULD/CONTROL diverging =
                a BUG; SKIP diverging = expected (adoption-agency / foster-parenting, not ported).
  * GRAMMAR   — random *well-formed* trees (from diff_parity.generate): tests the safety invariant —
                on closed-tag input the engine must be byte-identical to lxml (0 DIVERGE).

Verdict per pair:  AGREE | WS (equal after strip) | SKIP-EXPECTED | DIVERGE (bug) | CRASH.
Headline gate:  DIVERGE + CRASH == 0, with SKIP-EXPECTED reported as the measured distance to lxml.

Usage:  .venv/bin/python tools/diff_lxml.py [--pages N] [--grammar G] [--seed S] [--show K]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
from collections import defaultdict

from parsel import Selector as PS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from families import FAMILIES, build_page  # tagged construct generators
import conformant  # content-model-aware generator (the clean invariant gate)
import encpages  # legacy-encoding pages x non-ASCII selector literals (the byte/UTF-8 boundary)
import foreign  # svg/math/template foreign-content generator (also a clean parity gate)
import oracle  # oracle-toolchain guard: the verdicts only mean anything against libxml2 >= 2.14

# unconstrained grammar generator (adversarial: measures SKIP-set distance, not a gate)
try:  # optional: unconstrained-grammar generator for the adversarial (SKIP-distance) mode
    from diff_parity import generate as gen_grammar, GENERIC  # noqa: E402
except Exception:
    gen_grammar, GENERIC = None, []

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "target", "release", "differ")


def _parse_lines(out):
    """Parse the engine's line-per-case JSON, stopping at the first un-parseable line (a truncated
    final line is the tell-tale of a mid-write panic; the missing tail is caught by the count check)."""
    results = []
    for line in out.decode(errors="replace").splitlines():
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            break
    return results


# Basket selectors the ORACLE answers nowhere in the generated corpus. Such a pair can only ever catch the
# engine INVENTING values (an over-match); it can never catch one losing them, because there is nothing to
# lose. That is not automatically a defect — for some of these the emptiness IS the assertion:
# `dl > dt + dt::text` is empty precisely because libxml2 NESTS a repeated `<dt>` instead of closing it, so
# a value there would mean the tree rule broke. Others are accidental, and those are what this guards:
# `h1, h2` and `h1::text, h2::text, h3::text` are empty because the generator emits no headings AT ALL, so
# two comma-group spellings sit in the basket exercising nothing.
#
# So the gate is on the SET, not a count: a selector may leave (the generator grew) but a new one may not
# arrive unnoticed. That is how the first `:contains()` case here was caught — `div:contains(alpha)::attr(id)`
# reads like coverage and the generator puts no `id` on a `div`, so it would have graded AGREE against a
# build with no `:contains()` at all.
ORACLE_EMPTY_BASKET = frozenset({
    # ACCIDENTAL — the markup the selector needs is absent from the generator, so the spelling tests
    # nothing. Emitting an `h1`/`h2`/`h3`, or an `id` on a `colgroup`, would recover four pairs.
    "h1 ~ p::text", "h1, h2", "h1::text, h2::text, h3::text", "table > colgroup::attr(id)",
    # INTENDED — empty because the tree rules say so, which makes the emptiness the assertion. A repeated
    # `<dt>`/`<dd>` NESTS rather than closing, so no sibling pair exists to match; if the engine
    # auto-closed them instead, these would start returning values.
    "dl > dt + dt::text", "dl > dd + dd::text", "thead + tbody::text", "caption + thead::text",
    "optgroup + optgroup::text",
    # INTENDED — `::text` is the element's OWN text nodes, and these elements have none: a table section,
    # a row and an optgroup hold their text in descendants, and `<input>` is void so it has no text at all.
    "table > thead::text", "table > tbody::text", "table > tfoot::text", "table > tr::text",
    "thead > tr::text", "tbody > tr::text", "select > optgroup::text", 'input[value|="v"]::text',
})


def oracle_empty_basket(basket, oracle_counts):
    """Basket selectors the oracle answered ZERO times, in basket order.

    Separated out so `tests/test_gates.py` can seed it: this decides a gate, and the property it decides
    ("could this pair have gone red at all?") is invisible in a run that prints only AGREE totals.
    """
    return [sel for sel in basket if not oracle_counts.get(sel, 0)]


def _crash_check(cases, results, returncode, err):
    """(crashed_cases, diag): nonzero when `differ` died mid-batch or exited nonzero. Without this a
    panic truncates stdout, `zip` silently drops every later case, and the run can still print PASS —
    the "0 CRASH" half of the gate would be vacuous."""
    shortfall = len(cases) - len(results)
    if shortfall <= 0 and returncode in (0, None):
        return 0, None
    crashed = shortfall if shortfall > 0 else 1  # nonzero exit having delivered everything still counts
    bad = cases[len(results)][0].hex() if len(results) < len(cases) else "(all cases delivered)"
    diag = (f"differ exited {returncode}; delivered {len(results)}/{len(cases)} cases "
            f"({crashed} counted CRASH).\n      first undelivered HTML (hex): {bad}\n"
            f"      stderr: {err.decode(errors='replace').strip()[:400]}")
    return crashed, diag


def run_engine(cases, strict_budget=True, enc=""):
    """cases: list of (html_bytes, [selectors]) -> (results, crashed_cases, diag).

    `enc` is the charset label sent in the protocol's ENC field for every case in the batch; empty
    (the default) means sniff, which is what every family except the encoding one wants.

    results[i] holds the value-columns for cases[i] (truncated if the engine crashed mid-batch).

    `strict_budget` makes an over-budget schema a loud harness error instead of empty columns — right
    for PARITY callers (empty columns would read as divergence in every one of them), wrong for
    `sel_fuzz.py`, whose budget bombs are the thing under test."""
    env = dict(os.environ)
    if strict_budget:
        env["FROSTWORK_DIFFER_BUDGET_STRICT"] = "1"
    else:
        env.pop("FROSTWORK_DIFFER_BUDGET_STRICT", None)
    proc = subprocess.Popen([BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env)
    # protocol: ENC \t HEXHTML \t sels...   (empty ENC = sniff)
    payload = "".join(
        enc + "\t" + html.hex() + ("\t" if sels else "") + "\t".join(sels) + "\n"
        for html, sels in cases
    ).encode()
    out, err = proc.communicate(payload)
    results = _parse_lines(out)
    return (results, *_crash_check(cases, results, proc.returncode, err))


def run_support(selectors):
    """Ask the real Rust compiler whether each selector is supported, without a PyO3 build.

    One audit probe per line keeps arbitrary selector text isolated and returns a bool per selector.
    A bridge crash is treated as unsupported here and will be caught separately by engine runs.
    """
    proc = subprocess.Popen([BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    payload = "".join("A\t" + sel + "\n" for sel in selectors).encode()
    out, _err = proc.communicate(payload)
    rows = _parse_lines(out)
    return [bool(rows[i][0]) if i < len(rows) and rows[i] else False for i in range(len(selectors))]


def is_xpath(sel):
    # Mirror the engine's routing (lib.rs `is_xpath`): absolute / `.`-rooted paths, or a
    # `normalize-space(...)` wrapper — so the Parsel oracle uses `.xpath()`, not `.css()`, for them.
    s = sel.strip()
    return s.startswith("/") or s.startswith("./") or s.startswith("normalize-space(")


def parsel_vals(html, sel, enc="utf-8"):
    try:
        s = PS(body=html, encoding=enc)
        return (s.xpath(sel) if is_xpath(sel) else s.css(sel)).getall()
    except Exception:
        return None


def is_node_query(sel):
    """A bare-element / outer-HTML query (no value terminal) returns element markup, not a scalar.
    Value terminals are the CSS pseudos `::text` / `::attr(...)` AND their XPath forms `/text()`,
    `//text()`, `/@name`. Misclassifying the latter as node queries routes XPath
    scalar results through the outer-HTML reparse path where `parsel.Selector(text="1")` guesses JSON
    (numeric text) and raises, spuriously reporting DIVERGE on equal values."""
    s = sel.strip()
    if "::text" in s or "::attr" in s:
        return False
    if s.startswith("normalize-space("):  # scalar string value, not element markup
        return False
    if s.endswith("text()"):  # xpath /text() and //text()
        return False
    if re.search(r"/@[A-Za-z_][\w:.-]*$", s):  # xpath /@name terminal
        return False
    return True


# The engine tracks per-selector membership in fixed-width bitsets (u128), so ONE pass answers at most
# `MAX_MEMBERS` selectors. Sibling combinators (`+`/`~`) also draw on a separate trigger-bit budget, so
# batch well under the member cap to leave headroom for a basket that is combinator-heavy.
MAX_SELECTORS_PER_PASS = 96


def _batches(selectors, n=MAX_SELECTORS_PER_PASS):
    return [list(selectors[i:i + n]) for i in range(0, len(selectors), n)] or [[]]


def verdict(mine, theirs, bucket, sel):
    if theirs is None:
        return "AGREE"  # parsel itself errored; nothing to compare
    if is_node_query(sel):
        # outer-HTML is RAW SOURCE (documented divergence from lxml's reflow), so byte-equality is
        # wrong. Validate re-parse equivalence on NON-WHITESPACE text (same node set + same content).
        # (Whitespace differs because re-parsing an unclosed raw fragment drops trailing ws at EOF —
        # an artifact of the fragment, not the capture; and non-ws parity is the project's bar.)
        if len(mine) != len(theirs):
            return "SKIP-EXPECTED" if bucket == "SKIP" else "DIVERGE"

        def nonws(f):
            return [t.strip() for t in PS(text=f).xpath("//text()").getall() if t.strip()]

        try:
            if [nonws(f) for f in mine] == [nonws(f) for f in theirs]:
                return "AGREE"
        except Exception:
            return "DIVERGE"
        return "SKIP-EXPECTED" if bucket == "SKIP" else "DIVERGE"
    if mine == theirs:
        return "AGREE"
    if [x.strip() for x in mine] == [x.strip() for x in theirs]:
        return "WS"
    return "SKIP-EXPECTED" if bucket == "SKIP" else "DIVERGE"


# --------------------------------------------------------------- grouped (Many/One) differential
# Sub-selector pools, widened past the single-compound MVP: multi-part CSS subs (`h3 a::text`,
# `.meta > span::text`) and relative XPath subs now get coverage, alongside value + outer-HTML.
_SUBS_CSS = ["h3::text", "a::attr(href)", ".price::text", "span::text", "a::text", "b", ".price",
             "img::attr(src)", "h3 a::text", ".meta > span::text", ".tag::text", "a::attr(class)"]
# Child/context anchors must be exercised alongside nested descendants: `.//x` is not `./x`.
_SUBS_XPATH = [".//a/@href", ".//h3//text()", ".//span/text()", ".//a/text()", ".//b/text()",
               "./h3/a/text()", "./span/text()", "./@class", "./text()", ".//text()"]
_SUBS_IMG = ["img::attr(src)", "img::attr(alt)", "img"]


def _card(rng):
    p = [f"card {rng.randint(1, 99)} "]
    if rng.random() < 0.85:
        p.append(f"<h3><a href='/a{rng.randint(1, 99)}' class='lnk'>It {rng.randint(1, 99)}</a></h3>")
    if rng.random() < 0.70:
        p.append(f'<span class="price">${rng.randint(1, 999)}.{rng.randint(0, 99):02d}</span>')
    if rng.random() < 0.50:
        p.append(f'<div class="meta"><span>m{rng.randint(0, 9)}</span><h3><a>nested</a></h3></div>')
    if rng.random() < 0.40:
        p.append(f'<b class="tag">t{rng.randint(0, 9)}</b>')
    if rng.random() < 0.35:
        p.append(f'<img src="/i{rng.randint(1, 9)}.png" alt="x{rng.randint(0, 9)}">')
    if rng.random() < 0.25:  # nested same-class container -> all-open-instances routing
        p.append(f'<div class="card"><span>n{rng.randint(0, 9)}</span></div>')
    return f'<div class="card">{"".join(p)}</div>'


def _li(rng, close):
    end = "</li>" if close else ""  # sometimes omit </li> -> implied-close instance boundary
    a = f"<a href='/l{rng.randint(1, 99)}'>go</a>" if rng.random() < 0.8 else ""
    tag = f'<b class="tag">t{rng.randint(0, 9)}</b>' if rng.random() < 0.5 else ""
    return f'<li class="row">{a}{tag}{end}'


def gen_grouped(rng):
    """A page carrying several container species + 1-3 grouped queries over them (multiple groups in
    one pass exercises cross-group routing in text()/emit_attrs, which single-group never touched).
    Returns (html_bytes, groups) where groups = [(container, subs), ...]. Weighted toward the
    divergence-prone cases: nested same-class + empty containers, implied-close (`<li>`), void (`img`),
    and XPath containers/subs."""
    cards = "".join(_card(rng) for _ in range(rng.randint(0, 4)))
    close_li = rng.random() < 0.5
    lis = "".join(_li(rng, close_li) for _ in range(rng.randint(0, 4)))
    imgs = "".join(f'<img src="/g{i}.png" alt="a{i}">' for i in range(rng.randint(0, 3)))
    body = (f'<html><body><div class="grid">{cards}</div>'
            f'<ul>{lis}</ul><div class="gallery">{imgs}</div></body></html>')
    # candidate (container, sub-pool); each is descendant-or-self-scoped exactly like Parsel's per-node
    species = [
        (".card", _SUBS_CSS),                     # well-formed CSS-class container (baseline)
        ('//div[@class="card"]', _SUBS_XPATH),     # XPath container + relative XPath subs
        ("ul > li", _SUBS_CSS),                    # implied-close boundary container
        ("li", _SUBS_CSS),
        ("img", _SUBS_IMG),                        # void/self-closing container: only self-matches
        (".gallery img", ["img::attr(src)", "img::attr(alt)"]),
    ]
    chosen = rng.sample(species, rng.randint(1, 3))
    groups = [(c, rng.sample(pool, rng.randint(1, min(3, len(pool))))) for c, pool in chosen]
    return body.encode(), groups


def run_grouped(cases):
    """cases: list of (html, groups) where groups=[(container, subs), ...] -> (out, crashed, diag).

    out[i] is [group][row][sub][values] for cases[i] (truncated on a mid-batch engine crash)."""
    proc = subprocess.Popen([BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    lines = []
    for html, groups in cases:
        parts = ["G2", "", html.hex(), str(len(groups))]
        for container, subs in groups:
            parts += [container, str(len(subs)), *subs]
        lines.append("\t".join(parts) + "\n")
    out, err = proc.communicate("".join(lines).encode())
    results = _parse_lines(out)
    return (results, *_crash_check(cases, results, proc.returncode, err))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=300, help="pages per construct family")
    ap.add_argument("--conformant", type=int, default=4000, help="content-model-conformant docs (gate)")
    ap.add_argument("--foreign", type=int, default=1500, help="svg/math/template foreign-content docs (gate)")
    ap.add_argument("--grouped", type=int, default=3000, help="single-pass Many/One grouped cases (gate)")
    ap.add_argument("--encoding", type=int, default=250,
                    help="legacy-encoding pages PER LABEL, with non-ASCII selector literals (gate)")
    ap.add_argument("--adversarial", type=int, default=0,
                    help="unconstrained grammar docs (measures SKIP-set distance; not a gate)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--show", type=int, default=6)
    oracle.add_argument(ap)
    args = ap.parse_args()
    # Before spending any time: the gate is only meaningful against the pinned oracle's *libxml2*, which
    # the lxml pin does not fix (see tools/oracle.py). Wrong oracle -> exit 2, not a bogus DIVERGE count.
    oracle.require(args.allow_old_libxml2)
    rng = random.Random(args.seed)

    stat = defaultdict(int)
    fam_div = defaultdict(lambda: defaultdict(int))
    examples = []
    crash_diags = []

    # ---- FAMILIES: tagged optional-end-tag constructs ----
    fam_cases = []  # (html, sels, bucket, family)
    for name, bucket, builder in FAMILIES:
        for _ in range(args.pages):
            body, selectors = build_page(rng, builder)
            sels = [s for s, _c in selectors]
            fam_cases.append((body, sels, bucket, name))
    engine_out, crashed, diag = run_engine([(b, s) for b, s, _bk, _nm in fam_cases])
    stat["CRASH"] += crashed
    if diag:
        crash_diags.append(("families", diag))
    for (body, sels, bucket, name), mine_cols in zip(fam_cases, engine_out):
        for si, sel in enumerate(sels):
            mine = mine_cols[si] if si < len(mine_cols) else []
            theirs = parsel_vals(body, sel)
            v = verdict(mine, theirs, bucket, sel)
            stat[v] += 1
            stat["pairs"] += 1
            fam_div[name][v] += 1
            if v in ("DIVERGE", "CRASH") and len(examples) < args.show:
                examples.append((name, sel, mine[:4], (theirs or [])[:4], body.decode()[:160]))

    # ---- CONFORMANT: content-model-valid trees; invariant = byte-identical to lxml (THE GATE) ----
    # The basket is BATCHED to stay inside the engine's fixed-width member budget. Over the budget the
    # surplus columns come back deterministically empty, which a differential reads as divergence in
    # every one of them — so growing the basket past the limit would look like a catastrophic parity
    # regression rather than a harness error. `differ` panics on an over-budget batch to make that loud.
    basket_batches = _batches(conformant.BASKET)  # invariant: slice once, not once per page
    conf_cases = [
        (page, batch)
        for page in (conformant.generate(rng) for _ in range(args.conformant))
        for batch in basket_batches
    ]
    engine_out, crashed, diag = run_engine(conf_cases)
    stat["CRASH"] += crashed
    if diag:
        crash_diags.append(("conformant", diag))
    # Per-selector ORACLE value counts, so the basket's own discriminating power is measured rather than
    # assumed — see `vacuous_basket`.
    oracle_vals = defaultdict(int)
    for (body, sels), mine_cols in zip(conf_cases, engine_out):
        for si, sel in enumerate(sels):
            mine = mine_cols[si] if si < len(mine_cols) else []
            theirs = parsel_vals(body, sel)
            oracle_vals[sel] += len(theirs or [])
            v = verdict(mine, theirs, "CONTROL", sel)
            stat[v] += 1
            stat["pairs"] += 1
            fam_div["(conformant)"][v] += 1
            if v in ("DIVERGE", "CRASH") and len(examples) < args.show:
                examples.append(("conformant", sel, mine[:5], (theirs or [])[:5], body.decode()[:240]))

    # ---- ENCODING: the same document in a legacy charset, asked with non-ASCII selector literals ----
    # The one axis this harness did not have. Both selector-construction sites hardcoded utf-8, so no
    # generated pair had ever crossed the byte/UTF-8 boundary a selector literal sits on — which is
    # exactly where a silent value loss shipped (`[data-año]` matched a UTF-8 page and returned nothing
    # for the same document in windows-1252). The label is sent on BOTH sides: the protocol's ENC field
    # to the engine, `PS(body=…, encoding=…)` to the oracle.
    #
    # `expect` is not decoration. A selector the oracle answers nowhere can only catch an over-match, so
    # a family of always-empty columns would grade AGREE against an engine with the feature removed —
    # the positives assert lxml returned something, and a violation is a HARNESS error (loud), not a
    # divergence.
    enc_oracle_empty = []
    for label in encpages.LABELS:
        enc_cases = [encpages.generate(rng, label) for _ in range(args.encoding)]
        if not enc_cases:
            break
        engine_out, crashed, diag = run_engine(
            [(b, [s for s, _e in sels]) for b, _l, sels in enc_cases], enc=label
        )
        stat["CRASH"] += crashed
        if diag:
            crash_diags.append((f"encoding:{label}", diag))
        for (body, _l, sels), mine_cols in zip(enc_cases, engine_out):
            for si, (sel, expect_nonempty) in enumerate(sels):
                mine = mine_cols[si] if si < len(mine_cols) else []
                theirs = parsel_vals(body, sel, enc=label)
                if expect_nonempty and not theirs:
                    enc_oracle_empty.append((label, sel))
                v = verdict(mine, theirs, "CONTROL", sel)
                stat[v] += 1
                stat["pairs"] += 1
                fam_div[f"(encoding:{label})"][v] += 1
                if v in ("DIVERGE", "CRASH") and len(examples) < args.show:
                    examples.append((f"encoding:{label}", sel, mine[:5], (theirs or [])[:5],
                                     body.decode(label)[:240]))
    if enc_oracle_empty:
        uniq = sorted(set(enc_oracle_empty))
        print(f"HARNESS ERROR: {len(uniq)} encoding selectors the ORACLE answered nowhere, which can "
              f"only catch an over-match:", file=sys.stderr)
        for label, sel in uniq[:8]:
            print(f"  {label}  {sel}", file=sys.stderr)
        sys.exit(2)

    # ---- FOREIGN: svg/math/template subtrees; libxml2 treats them as ordinary elements, so this is a
    # clean parity gate over a species conformant.py never emits (self-closing SVG leaves, camelCase
    # names, rawtext inside foreign content, table fragments inside <template>). ----
    fgn_cases = [(foreign.generate(rng), foreign.SELECTORS) for _ in range(args.foreign)]
    if fgn_cases:
        engine_out, crashed, diag = run_engine(fgn_cases)
        stat["CRASH"] += crashed
        if diag:
            crash_diags.append(("foreign", diag))
        for (body, sels), mine_cols in zip(fgn_cases, engine_out):
            for si, sel in enumerate(sels):
                mine = mine_cols[si] if si < len(mine_cols) else []
                v = verdict(mine, parsel_vals(body, sel), "CONTROL", sel)
                stat[v] += 1
                stat["pairs"] += 1
                fam_div["(foreign)"][v] += 1
                if v in ("DIVERGE", "CRASH") and len(examples) < args.show:
                    examples.append(("foreign", sel, mine[:5], (parsel_vals(body, sel) or [])[:5],
                                     body.decode()[:240]))

    # ---- GROUPED: single-pass Many/One parity vs Parsel per-container (THE GATE, grouped path) ----
    # Oracle = `for c in doc.css(container): [c.css(sub).getall() for sub in subs]` — Parsel's own
    # descendant-or-self scoping, which the engine must match row-for-row, cell-for-cell.
    g_cases = [gen_grouped(rng) for _ in range(args.grouped)]
    if g_cases:
        grouped_out, crashed, diag = run_grouped(g_cases)
        stat["CRASH"] += crashed
        if diag:
            crash_diags.append(("grouped", diag))
        for (body, groups), mine_groups in zip(g_cases, grouped_out):
            sel = PS(body=body, encoding="utf-8")
            for gi, (container, subs) in enumerate(groups):
                stat["pairs"] += 1
                rows = mine_groups[gi] if gi < len(mine_groups) else []
                containers = sel.xpath(container) if is_xpath(container) else sel.css(container)
                theirs_rows = [[(c.xpath(s) if is_xpath(s) else c.css(s)).getall() for s in subs]
                               for c in containers]
                if len(rows) != len(theirs_rows):
                    stat["DIVERGE"] += 1
                    fam_div["(grouped)"]["DIVERGE"] += 1
                    if len(examples) < args.show:
                        examples.append(("grouped:rows", container, [f"{len(rows)} rows"],
                                         [f"{len(theirs_rows)} rows"], body.decode()[:220]))
                    continue
                cell_bad = None
                for r_mine, r_theirs in zip(rows, theirs_rows):
                    for si, sub in enumerate(subs):
                        if verdict(r_mine[si], r_theirs[si], "CONTROL", sub) in ("DIVERGE", "CRASH"):
                            cell_bad = (sub, r_mine[si], r_theirs[si])
                if cell_bad is not None:
                    stat["DIVERGE"] += 1
                    fam_div["(grouped)"]["DIVERGE"] += 1
                    if len(examples) < args.show:
                        sub, mine_c, theirs_c = cell_bad
                        examples.append((f"grouped[{container}]", sub, mine_c[:4], theirs_c[:4],
                                         body.decode()[:220]))
                else:
                    stat["AGREE"] += 1
                    fam_div["(grouped)"]["AGREE"] += 1

    # ---- GROUPED UNSUPPORTED CONTRACT: reject whole, never execute a partial selector. ----
    # These shapes have useful lxml results but are deliberately outside grouped extraction. Their
    # contract is an empty group/cell, not "run the first comma member" or "audit green, matcher dead".
    neg_html = b'<html><body><div class="root"><p>P<a>x</a></p></div><span><p>S</p></span></body></html>'
    neg_groups = [
        ("div, span", ["p::text"]),
        ("div", ["p::text, a::text"]),
        ("div:has(a)", ["div::text"]),
        (".root", ["p:has(a)::text", './/p[contains(.,"x")]/text()']),
    ]
    neg_expected = [[], [[[]]], [], [[[], []]]]
    neg_out, crashed, diag = run_grouped([(neg_html, neg_groups)])
    stat["CRASH"] += crashed
    if diag:
        crash_diags.append(("grouped-unsupported", diag))
    got_groups = neg_out[0] if neg_out else []
    for gi, expected in enumerate(neg_expected):
        stat["pairs"] += 1
        got = got_groups[gi] if gi < len(got_groups) else None
        verdict_key = "AGREE" if got == expected else "DIVERGE"
        stat[verdict_key] += 1
        fam_div["(grouped-unsupported)"][verdict_key] += 1
        if verdict_key == "DIVERGE" and len(examples) < args.show:
            examples.append(("grouped-unsupported", neg_groups[gi][0], got, expected, neg_html.decode()))

    # ---- ADVERSARIAL: unconstrained grammar; measures SKIP-set distance (reported, NOT a gate) ----
    adv = defaultdict(int)
    if args.adversarial and gen_grammar is not None:
        basket = [s for s in GENERIC if "::text" in s or "::attr" in s]
        adv_cases = [(gen_grammar(rng).encode("utf-8"), basket) for _ in range(args.adversarial)]
        adv_out, adv_crashed, adv_diag = run_engine(adv_cases)
        adv["CRASH"] += adv_crashed
        for (body, sels), mine_cols in zip(adv_cases, adv_out):
            for si, sel in enumerate(sels):
                mine = mine_cols[si] if si < len(mine_cols) else []
                theirs = parsel_vals(body, sel)
                adv[verdict(mine, theirs, "CONTROL", sel)] += 1
                adv["pairs"] += 1

    pairs = stat["pairs"] or 1
    print(f"PATH-2 DIFFERENTIAL vs lxml   seed={args.seed}  pairs={stat['pairs']}")
    print(oracle.banner() + "\n")
    print(f"  AGREE          {stat['AGREE']:>8}  ({100.0*stat['AGREE']/pairs:.2f}%)")
    print(f"  WS-only        {stat['WS']:>8}")
    print(f"  SKIP-EXPECTED  {stat['SKIP-EXPECTED']:>8}   (documented tree-construction, allowed)")
    print(f"  DIVERGE (bug)  {stat['DIVERGE']:>8}   <-- gate: must be 0")
    print(f"  CRASH          {stat['CRASH']:>8}   <-- gate: must be 0\n")

    print("  by family:   family                 pairs   AGREE   WS  SKIP-EXP  DIVERGE")
    # Preferred order first, then EVERY remaining family that actually recorded pairs. A hand-written,
    # closed list lets a family count toward the gate while being INVISIBLE in the report; deriving the
    # tail means a new generator cannot be silently unlisted.
    preferred = [n for n, _, _ in FAMILIES] + [
        "(conformant)", "(foreign)", "(grouped)", "(grouped-unsupported)"
    ]
    order = preferred + sorted(k for k in fam_div if k not in set(preferred))
    for name in order:
        d = fam_div[name]
        p = sum(d.values())
        print(f"    {name:<26}{p:>6}{d['AGREE']:>8}{d['WS']:>5}{d['SKIP-EXPECTED']:>9}{d['DIVERGE']:>9}")

    if adv:
        ap_ = adv["pairs"] or 1
        print(f"\n  adversarial grammar (SKIP-set distance, NOT gated): pairs={adv['pairs']}  "
              f"AGREE={adv['AGREE']}  DIVERGE(skip-leak)={adv['DIVERGE']} ({100.0*adv['DIVERGE']/ap_:.2f}%)")

    if examples:
        print("\n  unexpected divergences (first few):")
        for name, sel, mine, theirs, snip in examples:
            print(f"    [{name}] {sel!r}\n        mine ={mine}\n        lxml ={theirs}\n        html: {snip!r}")

    if crash_diags:
        print("\n  ENGINE CRASHES (undelivered cases counted as CRASH -> gate FAIL):")
        for where, diag in crash_diags:
            print(f"    [{where}] {diag}")

    empty = oracle_empty_basket(conformant.BASKET, oracle_vals)
    unlisted = [sel for sel in empty if sel not in ORACLE_EMPTY_BASKET]
    print(f"\n  basket selectors the ORACLE answered nowhere: {len(empty)}/{len(conformant.BASKET)} "
          f"— over-match coverage only (see ORACLE_EMPTY_BASKET)")
    if unlisted:
        print("    UNLISTED — added since the set was recorded, and it cannot catch a LOST value:")
        for sel in unlisted:
            print(f"      {sel!r}")

    gate = stat["DIVERGE"] + stat["CRASH"]
    print(f"\n  GATE: DIVERGE+CRASH = {gate}  ->  {'PASS' if gate == 0 else 'FAIL'}")
    sys.exit(1 if gate or unlisted else 0)


if __name__ == "__main__":
    main()
