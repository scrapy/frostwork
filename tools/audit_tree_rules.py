"""
tools/audit_tree_rules.py — enumerate EVERY tree-construction rule cell against the oracle.

Why this exists. The differential gate proves parity on the pages it *generates*, so a rule the
generators never exercise is asserted, not tested. That is not a hypothetical gap: the `dd`/`dt` and
`rt`/`rp` same-tag closes shipped wrong, and a follow-up audit found 19 more wrong cells plus a missing
table-scope rule — every one of them in a region no generated page reached. Coverage of *pages* is not
coverage of *rules*.

So this walks the rule tables directly and asks lxml about each cell:

  * the implied-close CROSS PRODUCT — for every (open A, incoming B), does B close A?
  * the VOID set, and the four tags the contract claims libxml2 keeps OPEN (embed/source/track/wbr)
  * the `<p>`-closing set, block-by-block and item-by-item, plus the tags that must NOT close it
  * TABLE SCOPE — which end tags libxml2 refuses to unwind a table for
  * RAWTEXT/RCDATA entity handling

It is fast (no page generation) and deterministic, so it is a gate, not a survey. `--gate` exits nonzero
on any disagreement. Add a row here whenever you add a rule; a rule with no row here is a rule on trust.

Usage:
  .venv/bin/python tools/audit_tree_rules.py [--gate] [--verbose]
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import frostwork  # noqa: E402
import oracle  # noqa: E402  — same libxml2 >= 2.14 requirement as the differential gate
# the harness's own oracle driver: same css/xpath dispatch and same parsel-error handling as the gate
from diff_lxml import parsel_vals  # noqa: E402

# An open element and the wrapper it needs to be in scope. This is the HTML **optional-end-tag** set —
# a fixed universe from the spec, NOT a mirror of the engine's own tag ids. Drawing it from the engine
# would make the audit self-referential: it could find a wrong cell but never a MISSING rule, and
# `colgroup` (which had no rule at all) is exactly what that blind spot hid.
WRAP = {"li": "ul", "dd": "dl", "dt": "dl", "option": "select", "optgroup": "select",
        "tr": "table", "thead": "table", "tbody": "table", "tfoot": "table", "caption": "table",
        "td": "table", "th": "table", "rt": "ruby", "rp": "ruby", "p": "div",
        "colgroup": "table"}
# Extra markup needed between the wrapper and the element (a cell needs a row).
EXTRA = {"td": "<tr>", "th": "<tr>"}
# Void, so never the OPEN element, but it must still appear as an INCOMING tag.
INCOMING_ONLY = ["col"]
VOID = "area base br col hr img input link meta param".split()
NONVOID_CLAIMED = "embed source track wbr".split()      # contract: libxml2 keeps these OPEN
BLOCK = ("address blockquote center dir div dl fieldset form h1 h2 h3 h4 h5 h6 hr menu ol pre table "
         "ul").split()
SECTIONING = "section article aside header footer nav main figure details hgroup".split()
INLINE = "span a b em strong small label".split()
TABLE_SCOPED = "table caption thead tbody tfoot tr td th".split()
# markup needed BEFORE an element for it to be in scope, derived from WRAP so adding a tag there is
# enough (`table` needs no wrapper; a cell needs a row as well)
SCOPE_PRE = {t: f"<{w}>{EXTRA.get(t, '')}" for t, w in WRAP.items()}
SCOPE_PRE.update({"table": "", "p": "", "span": ""})
# Every element whose scope contribution is a real question — the optional-end-tag set plus `table` and
# two controls. Drawing this from TABLE_SCOPED plus a hand-picked few made it self-referential AGAIN, in a
# subtler place than before: `dd`, `dt` and `rp` were absent, so flipping their scope entry changed the
# engine's behaviour and NOTHING went red (found by tools/mutate_rules.py, not by reading the code).
SCOPE_CANDIDATES = TABLE_SCOPED + ["colgroup", "li", "dd", "dt", "p", "span", "option", "optgroup",
                                   "rt", "rp"]

# The cross product below is over TAG NAMES, but the engine's table is over 19 tag IDS, and three of
# them are not names: OTHER (any unrecognized element), BLOCK (the <p>-closing block set) and TABLE.
# So `<div>` closing an open `<dd>`, or `<dd>` closing an open `<span>`, was never asked — 40 such cells
# survived every gate in the mutation sweep. `span` stands for OTHER and `div` for BLOCK; each needs a
# wrapper that is not itself the tag under test, or the probe selector cannot tell parent from ancestor.
XPROD_WRAP = dict(WRAP, span="div", div="section")
# `table` is asked only as the INCOMING tag: as the open element, any text placed to reveal the nesting
# is foster-parented, and foster-parenting is a documented divergence — so those cells cannot be settled
# by value parity. They stay asserted, and docs/TESTING.md says so.
XPROD_INCOMING_ONLY = ["table"]

# ---------------------------------------------------------------- libxml2's start-close PAIR table
# The section below walks a surface the id-based cross product structurally cannot: libxml2 decides
# "does this start tag close that open element?" from a hardcoded NAME-pair list (`htmlStartClose`),
# and it distinguishes names the engine's tag ids lump together — `<td>` closes an open `<b>` but not an
# open `<em>`; `<table>` closes an open `<h1>` but not an open `<div>`. The mutation sweep
# (tools/mutate_rules.py) is what exposed this: 40 id-space cells survived every gate because no gate
# asked about them, and widening the audit to ask turned up 85 real disagreements.
#
# They are enumerated rather than tolerated. A pair NOT listed here that disagrees FAILS the gate, and a
# pair listed here that starts AGREEING also fails — so the list cannot rot in either direction, the same
# discipline as diff_fuzz's DOCUMENTED set. Every one is the same shape: libxml2 closes, the engine nests
# (0 cases of the engine over-closing), so closing this gap is purely additive. See COMPATIBILITY.md.
STARTCLOSE_NAMES = ("p pre address dir menu ul ol dl dt dd li h1 h2 h3 h4 h5 h6 a b i u font span big "
                    "small tt em strong div section caption colgroup td th tr thead tbody tfoot option "
                    "optgroup table fieldset form center blockquote ruby rt rp "
                    # Every start-close CLASS needs a representative here or its cells are unreachable:
                    # `col`/`hr` are void (incoming-role only), `legend` is closed only by <fieldset>, and
                    # `s`/`strike` share a class with `big`/`small`/`tt` — omitting them left 93 cells
                    # unprobed, which the mutation sweep found and nothing else would have.
                    "col hr legend s strike").split()
# incoming tag -> open elements it closes in libxml2 2.14 but not (yet) here
KNOWN_START_CLOSE_GAP: dict[str, list[str]] = {
    # EMPTY, and kept that way deliberately. This held 87 pairs where libxml2 closed an open element and
    # the engine nested it; `implied_close::start_closes` now ports libxml2's htmlStartClose pair table
    # and all 11,543 (open x incoming) cells agree. The mechanism stays because it is what keeps the list
    # honest: an unlisted divergence fails the gate, and a LISTED pair that starts agreeing also fails, so
    # a future gap has to be added here in a diff someone reads rather than absorbed silently.
}


def both(html: bytes, sel: str):
    """(lxml values, engine values) for one (page, selector)."""
    return parsel_vals(html, sel), frostwork.extract(html, [sel], strict=False)[0]


class Audit:
    def __init__(self, verbose: bool):
        self.verbose, self.checked, self.fails = verbose, 0, []

    def section(self, title: str):
        """Print a section header and, on exit, the number of cells it actually checked — so the count
        can't drift from the loop (a hand-written total already over-reported `hr`, which is in both
        BLOCK and VOID and is skipped)."""
        audit = self

        class _Section:
            def __enter__(self):
                print(f"== {title}")
                self.before = audit.checked

            def __exit__(self, *_):
                print(f"   {audit.checked - self.before} cells checked")
                return False

        return _Section()

    def check_gap(self, group: str, label: str, html: bytes, sel: str, expected_gap: bool):
        """Like `check`, but this cell is a KNOWN divergence: a disagreement is recorded only when it is
        NOT the expected one. Counted as checked either way — the cell IS being measured."""
        lx, fr = both(html, sel)
        self.checked += 1
        if lx != fr and not expected_gap:
            self.fails.append((group, label, sel, lx, fr, html))
        return lx, fr

    def check(self, group: str, label: str, html: bytes, sel: str):
        lx, fr = both(html, sel)
        self.checked += 1
        if lx != fr:
            self.fails.append((group, label, sel, lx, fr, html))
        elif self.verbose:
            print(f"    ok  {label:44} {sel:26} {lx}")
        return lx, fr


def audit_void(a: Audit):
    """`<div><X>A</div>`: if X is void, A is the div's own text; if a container, it is X's."""
    with a.section("void set / claimed-NON-void set"):
        for t in VOID + NONVOID_CLAIMED + ["span"]:
            html = f"<html><body><div><{t}>A</div></body></html>".encode()
            lx, _ = a.check("void", f"<div><{t}>A", html, "div::text")
            is_void = lx == ["A"]
            expect = t in VOID
            if is_void != expect and t != "span":
                a.fails.append(("void-claim", f"contract says {t} is "
                                f"{'void' if expect else 'NON-void'}, libxml2 disagrees",
                                "div::text", [expect], [is_void], html))


def audit_p_closing(a: Audit):
    """`<div><p>A<X>B</X></div>`: if X closed the <p>, p and X are both children of the div."""
    items = [t for t in WRAP if t != "p"]
    with a.section("<p>-closing set"):
        for t in BLOCK + SECTIONING + INLINE + items:
            if t in VOID:
                continue  # a void X has no text of its own; covered by the void audit instead
            html = f"<html><body><div><p>A<{t}>B</{t}></div></body></html>".encode()
            a.check("p-closing", f"<p>A<{t}>B", html, "div > *::text")


def audit_implied_close(a: Audit):
    """Full cross product: does an incoming <B> auto-close an open <A>?"""
    tags = list(XPROD_WRAP)
    with a.section("implied-close cross product"):
        for x, y in itertools.product(tags, tags):
            w = XPROD_WRAP[x]
            pre = EXTRA.get(x, "")
            html = f"<html><body><{w}>{pre}<{x}>aaa<{y}>bbb</{w}></body></html>".encode()
            # `bbb` surfaces under the wrapper only if <y> closed <x> (else it is nested inside it).
            # `span` and `div` stand for the OTHER/BLOCK ids, so some of these cells land on the
            # enumerated name-pair gap below — same lookup, one source of truth for it.
            a.check_gap("implied-close", f"<{w}>{pre}<{x}>aaa<{y}>bbb", html, f"{w} > {y}::text",
                        x in KNOWN_START_CLOSE_GAP.get(y, ()))
        # incoming <table>: keep the probe text in a CELL so the answer is not confounded by
        # foster-parenting, and ask whether the table landed under the wrapper or inside <x>
        for x in tags:
            w = XPROD_WRAP[x]
            pre = EXTRA.get(x, "")
            html = (f"<html><body><{w}>{pre}<{x}>aaa<table><tbody><tr><td>bbb"
                    f"</{w}></body></html>").encode()
            a.check("implied-close", f"<{w}>{pre}<{x}>aaa<table>…<td>bbb", html,
                    f"{w} > table td::text")



def audit_start_close_pairs(a: Audit):
    """libxml2's NAME-pair start-close table, over a universe the id cross product cannot reach.

    The wrapper is an UNKNOWN element on purpose: libxml2 closes it for nothing, so it stays the parent
    boundary and the probe measures only the element under test. A known wrapper (`<div>`) confounds every
    cell where the open element IS a div — which is how a first pass read 72 spurious closes.
    """
    with a.section("start-close pair table (libxml2 htmlStartClose)"):
        stale = []
        # NO `x == y` skip: the diagonal (does <X> close an open <X>?) is a rule cell like any other, and
        # skipping it hid the nested-<a> and nested-<form> closes. The probe stays unambiguous because
        # only the SECOND element carries the id / trailing text.
        for x, y in itertools.product(STARTCLOSE_NAMES, STARTCLOSE_NAMES):
            html = f"<html><body><xwrap>xx<{x}>aaa<{y}>bbb</xwrap></body></html>".encode()
            expected_gap = x in KNOWN_START_CLOSE_GAP.get(y, ())
            lx, fr = a.check_gap("start-close", f"<{x}>aaa<{y}>bbb", html, f"xwrap > {y}::text",
                                 expected_gap)
            if expected_gap and lx == fr:
                stale.append((y, x))
        # SECOND PASS, attribute probe. Text inside an open `<table>` is foster-parented (a documented
        # divergence), so a `::text` probe reads empty whether or not the table was closed — which is
        # exactly why `table`-as-the-open-element cells survived the mutation sweep unnoticed. An
        # ATTRIBUTE is never foster-parented, so it reveals the parentage directly. Verified to return the
        # same verdict as the text probe on all 1980 cells (same 85 gap pairs, no new disagreement), so
        # this adds observability, not a second contract.
        for x, y in itertools.product(STARTCLOSE_NAMES, STARTCLOSE_NAMES):
            html = f'<html><body><xwrap>xx<{x}>aaa<{y} id="Z">bbb</xwrap></body></html>'.encode()
            a.check_gap("start-close-attr", f"<{x}>aaa<{y} id=Z>", html, f"xwrap > {y}::attr(id)",
                        x in KNOWN_START_CLOSE_GAP.get(y, ()))
        if stale:
            a.fails.append(("start-close", f"{len(stale)} KNOWN_START_CLOSE_GAP entries now AGREE "
                                           f"(remove them): {stale[:6]}", "-", "-", "-", b""))
        print(f"   known gap: {sum(len(v) for v in KNOWN_START_CLOSE_GAP.values())} pairs where libxml2 "
              f"closes and the engine nests (enumerated, not tolerated — see COMPATIBILITY.md)")


def audit_table_scope(a: Audit):
    """libxml2 will not unwind a table for an ordinary end tag; it discards it instead."""
    with a.section("table scope (end tag through an open table-scoped element)"):
        # BARE: no wrapper, so this measures the element's OWN scope contribution. Wrapping each in
        # `<table>` hid it (the table blocks regardless) — which is how a wrong `caption` entry survived.
        for inner in SCOPE_CANDIDATES:
            for outer in ("div", "ul", "span", "section"):
                html = f"<html><body><{outer}><{inner}>AAA</{outer}>BBB</body></html>".encode()
                a.check("table-scope-bare", f"<{outer}><{inner}>AAA</{outer}>BBB", html,
                        f"{inner}::text")
        for inner in SCOPE_CANDIDATES:
            pre = SCOPE_PRE.get(inner, "")
            for outer in ("div", "ul", "span", "section"):
                html = f"<html><body><{outer}>{pre}<{inner}>AAA</{outer}>BBB</body></html>".encode()
                a.check("table-scope", f"<{outer}>{pre}<{inner}>AAA</{outer}>BBB", html,
                        f"{inner}::text")
        # a table-scoped end tag must still unwind, and </body>/</html> must still close the document
        for closer, sel in [("table", "div::text"), ("tbody", "td::text"), ("tr", "td::text"),
                            ("body", "td::text"), ("html", "td::text")]:
            html = (f"<html><body><div><table><tbody><tr><td>AAA</{closer}>BBB"
                    f"</body></html>").encode()
            a.check("table-scope", f"</{closer}> must still unwind", html, sel)


def audit_rawtext(a: Audit):
    with a.section("rawtext / RCDATA"):
        for t in ("script", "style", "textarea"):
            html = f"<html><body><{t}>a&amp;b &lt; c</{t}></body></html>".encode()
            a.check("rawtext", t, html, f"{t}::text")
        a.check("rawtext", "title", b"<html><head><title>a&amp;b &lt; c</title></head><body>x</body>"
                b"</html>", "title::text")



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true", help="exit nonzero on any disagreement (for CI)")
    ap.add_argument("--verbose", action="store_true", help="print every passing cell too")
    oracle.add_argument(ap)
    args = ap.parse_args()
    oracle.require(args.allow_old_libxml2)

    print("TREE-RULE AUDIT vs lxml")
    print(oracle.banner() + "\n")
    a = Audit(args.verbose)
    audit_void(a)
    audit_p_closing(a)
    audit_implied_close(a)
    audit_start_close_pairs(a)
    audit_table_scope(a)
    audit_rawtext(a)

    print(f"\n  cells checked : {a.checked}")
    print(f"  DISAGREEMENTS : {len(a.fails)}")
    if a.fails:
        by_group: dict[str, int] = {}
        for g, *_ in a.fails:
            by_group[g] = by_group.get(g, 0) + 1
        print("   by group: " + "  ".join(f"{g}={n}" for g, n in sorted(by_group.items())))
        for g, label, sel, lx, fr, html in a.fails[:40]:
            print(f"\n    [{g}] {label}\n        {sel}\n        lxml ={lx}\n        engine={fr}"
                  f"\n        html={html.decode()}")
    if args.gate:
        print(f"\n  GATE: rule disagreements = {len(a.fails)}  ->  "
              f"{'PASS' if not a.fails else 'FAIL'}")
        sys.exit(1 if a.fails else 0)


if __name__ == "__main__":
    main()
