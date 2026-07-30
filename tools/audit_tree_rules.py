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

# An open element and the wrapper it needs to be in scope.
WRAP = {"li": "ul", "dd": "dl", "dt": "dl", "option": "select", "optgroup": "select",
        "tr": "table", "thead": "table", "tbody": "table", "tfoot": "table", "caption": "table",
        "td": "table", "th": "table", "rt": "ruby", "rp": "ruby", "p": "div"}
VOID = "area base br col hr img input link meta param".split()
NONVOID_CLAIMED = "embed source track wbr".split()      # contract: libxml2 keeps these OPEN
BLOCK = ("address blockquote center dir div dl fieldset form h1 h2 h3 h4 h5 h6 hr menu ol pre table "
         "ul").split()
SECTIONING = "section article aside header footer nav main figure details hgroup".split()
INLINE = "span a b em strong small label".split()
TABLE_SCOPED = "table caption thead tbody tfoot tr td th".split()
# markup needed BEFORE an element for it to be in scope, derived from WRAP so adding a tag there is
# enough (`table` needs no wrapper; a cell needs a row as well)
SCOPE_PRE = {t: f"<{w}>" for t, w in WRAP.items()}
SCOPE_PRE.update({"td": "<table><tr>", "th": "<table><tr>", "table": "", "p": "", "span": ""})


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
    tags = list(WRAP)
    with a.section("implied-close cross product"):
        for x, y in itertools.product(tags, tags):
            w = WRAP[x]
            html = f"<html><body><{w}><{x}>aaa<{y}>bbb</{w}></body></html>".encode()
            # `bbb` surfaces under the wrapper only if <y> closed <x> (else it is nested inside it)
            a.check("implied-close", f"<{w}><{x}>aaa<{y}>bbb", html, f"{w} > {y}::text")



def audit_table_scope(a: Audit):
    """libxml2 will not unwind a table for an ordinary end tag; it discards it instead."""
    with a.section("table scope (end tag through an open table-scoped element)"):
        for inner in TABLE_SCOPED + ["li", "p", "span", "option", "optgroup", "rt"]:
            pre = SCOPE_PRE[inner]
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
