"""
tools/audit_tree_rules.py — enumerate EVERY tree-construction rule cell against the oracle.

Why this exists. The differential gate proves parity on the pages it *generates*, so a rule the
generators never exercise is asserted, not tested. That is not a hypothetical gap: the `dd`/`dt` and
`rt`/`rp` same-tag closes shipped wrong, and a follow-up audit found 19 more wrong cells plus a missing
table-scope rule — every one of them in a region no generated page reached. Coverage of *pages* is not
coverage of *rules*.

So this walks the rule tables directly and asks lxml about each cell:

  * the START-CLOSE relation over the WHOLE element universe — for every (open A, incoming B), does B
    close A? Not a representative subset: `tools/gen_tree_rules.ELEMENTS`, which `--check-universe`
    proves is a superset of both the engine's own names and an outside element index
  * the VOID set over that same universe, plus the four tags the contract claims libxml2 keeps OPEN
    (embed/source/track/wbr)
  * the per-element DATA MODE over that same universe (raw text / RCDATA / PLAINTEXT / normal)
  * the `<p>`-closing set, block-by-block and item-by-item, plus the tags that must NOT close it
  * TABLE SCOPE — which end tags libxml2 refuses to unwind a table for
  * HTML4 minimized boolean-attribute values

**Every universe here is one list, shared with the generator and the mutation sweep.** The reason is the
bug this audit shipped twice: its universes were hand-written lists of names someone remembered, so
`head`, `listing`, `xmp` and `plaintext` were simply absent and their cells could not fail. A "complete"
claim about a rule table is only as good as the set of names the table was asked about.

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

import oracle  # noqa: E402  — same libxml2 >= 2.14 requirement as the differential gate
# the harness's own oracle driver: same css/xpath dispatch and same parsel-error handling as the gate
from diff_lxml import parsel_vals  # noqa: E402
# ONE element universe and ONE oracle derivation, shared with the table generator and the mutation
# sweep. Importing it is what stops this file from growing a fourth hand-written list of tag names.
from gen_tree_rules import ELEMENTS, Oracle, check_universe  # noqa: E402

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
VOID = "area base basefont br col frame hr img input isindex link meta param".split()
NONVOID_CLAIMED = "embed source track wbr".split()      # contract: libxml2 keeps these OPEN
# ...and the mirror image: HTML4 names libxml2 treats as EMPTY that HTML5 does not have at all. The
# engine let all three hold children, so `<div><basefont><span>x</span></div>` put the span inside the
# basefont. Named here so the contract sentence is a test, not a comment.
VOID_HTML4_ONLY = "basefont frame isindex".split()
# The data mode of each name the contract names. `listing` and `noscript` are in here BECAUSE they look
# like raw-text elements and are not: libxml2 parses their content as markup (it has scripting off).
DATA_MODE_CLAIM = {
    "script": "rawtext", "style": "rawtext", "iframe": "rawtext", "noembed": "rawtext",
    "noframes": "rawtext", "xmp": "rawtext",
    "textarea": "rcdata", "title": "rcdata",
    "plaintext": "plaintext",
    "listing": "normal", "noscript": "normal", "template": "normal", "svg": "normal",
}
BOOLEAN_ATTRS = ("checked compact declare defer disabled ismap multiple nohref noresize noshade "
                 "nowrap readonly selected").split()
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

# ---------------------------------------------------------------- libxml2's start-close PAIR table
# The section below walks a surface the id-based cross product structurally cannot: libxml2 decides
# "does this start tag close that open element?" from a hardcoded NAME-pair list (`htmlStartClose`),
# and it distinguishes names the engine's tag ids lump together — `<td>` closes an open `<b>` but not an
# open `<em>`; `<table>` closes an open `<h1>` but not an open `<div>`. The mutation sweep
# (tools/mutate_rules.py) is what exposed this: 40 id-space cells survived every gate because no gate
# asked about them, and widening the audit to ask turned up 85 real disagreements.
#
# The universe is now `ELEMENTS` — every element name a browser-era parser special-cases — and NOT a
# list of representatives. Representatives are how this went wrong twice: the first version held 53
# names picked for "one per behaviour class we know of", which silently excluded `head`, `listing`,
# `xmp` and `plaintext`, and their whole rows/columns were missing from the engine while this gate read
# 0 disagreements. A subset is only safe if you already know the partition, which is exactly the thing
# under test.
#
# incoming tag -> open elements it closes in libxml2 2.14 but not (yet) here
KNOWN_START_CLOSE_GAP: dict[str, list[str]] = {
    # EMPTY, and kept that way deliberately. This held 87 pairs where libxml2 closed an open element and
    # the engine nested it; `implied_close::start_closes` is now GENERATED from the oracle over the full
    # universe by tools/gen_tree_rules.py and every cell agrees. The mechanism stays because it is what
    # keeps the list honest: an unlisted divergence fails the gate, and a LISTED pair that starts
    # agreeing also fails, so a future gap has to be added here in a diff someone reads.
}


def _engine():
    """The built extension, imported ON USE rather than at module load.

    This module is also the single Python home for the engine's tag tables, and `diff_fuzz` imports it
    for those — plain data that needs no compiled extension. Importing `frostwork` at module level made
    that import fail wherever the extension is not built yet, which in CI is every step before
    `maturin develop`: the fuzz gates run first, so they died on `ModuleNotFoundError` instead of
    fuzzing. It passed locally only because a developer venv already has the extension installed.
    """
    import frostwork

    return frostwork


def both(html: bytes, sel: str):
    """(lxml values, engine values) for one (page, selector)."""
    return parsel_vals(html, sel), _engine().extract(html, [sel], strict=False)[0]


def both_multi(html: bytes, sels: list[str]):
    """(lxml values, engine values) for several selectors over ONE page — the full-universe sweeps run
    ~60k cells, and one engine call per page instead of per selector is what keeps them a gate."""
    return [parsel_vals(html, s) for s in sels], _engine().extract(html, sels, strict=False)


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

    def check_many(self, group: str, label: str, html: bytes, sels: list[str]):
        """`check` over several selectors on one page. Each selector is its own cell."""
        lxs, frs = both_multi(html, sels)
        for sel, lx, fr in zip(sels, lxs, frs):
            self.checked += 1
            if lx != fr:
                self.fails.append((group, label, sel, lx, fr, html))
            elif self.verbose:
                print(f"    ok  {label:44} {sel:26} {lx}")
        return lxs


def audit_universe(a: Audit):
    """The universe every sweep below draws on must be a proven superset, or the sweeps prove nothing.

    This is the check that would have caught `head`/`listing`/`xmp`/`plaintext` being absent: it fails if
    libxml2 special-cases any name that an INDEPENDENT element index knows and `ELEMENTS` omits.
    """
    with a.section(f"element universe ({len(ELEMENTS)} names)"):
        a.checked += 1
        missing = check_universe()
        if missing:
            a.fails.append(("universe", "libxml2 special-cases names ELEMENTS omits",
                            "-", [], missing, b""))


def audit_void(a: Audit):
    """`<xwrap><X id=Z>A</xwrap>` over the WHOLE universe: if X is void, `A` is the wrapper's own text.

    The wrapper is an unknown element, not `<div>`: with `<div>` the probe selector also matches the
    element under test whenever that element IS a div, so `<div>` itself read as void.
    """
    with a.section("void set (whole universe)"):
        for t in ELEMENTS:
            html = f'<html><body><xwrap><{t} id="Z">A</xwrap></body></html>'.encode()
            a.check_many("void", f"<xwrap><{t}>A", html,
                         [f"xwrap > {t}::attr(id)", "xwrap::text", f"xwrap > {t}::text"])
        # The four HTML5-era names libxml2 keeps OPEN, asserted by NAME so the contract sentence in
        # COMPATIBILITY.md is testable rather than decorative. Same for the three HTML4 names it treats
        # as void, which the engine used to let hold children.
        for t in NONVOID_CLAIMED:
            html = f"<html><body><xwrap><{t}>A</xwrap></body></html>".encode()
            lx, _ = a.check("void-claim", f"{t} must be NON-void", html, f"{t}::text")
            if lx != ["A"]:
                a.fails.append(("void-claim", f"contract says {t} is NON-void, libxml2 disagrees",
                                f"{t}::text", ["A"], lx, html))
        for t in VOID_HTML4_ONLY:
            html = f"<html><body><xwrap><{t}>A</xwrap></body></html>".encode()
            lx, _ = a.check("void-claim", f"{t} must be void", html, "xwrap::text")
            if lx != ["A"]:
                a.fails.append(("void-claim", f"contract says {t} is void, libxml2 disagrees",
                                "xwrap::text", ["A"], lx, html))


def audit_data_modes(a: Audit):
    """Per-element DATA MODE over the WHOLE universe. A missing mode does not lose a value, it INVENTS
    elements: `<iframe><div>x</div></iframe>` matched `div::text` and every offset after it desynced."""
    modes = Oracle().modes()
    with a.section("data modes: raw text / RCDATA / PLAINTEXT (whole universe)"):
        # The mode of each name the contract NAMES, asserted as a claim rather than only swept: a mode is
        # a statement about a name, and `listing`/`noscript` look like raw text and are not.
        for t, want in DATA_MODE_CLAIM.items():
            a.checked += 1
            if modes[t] != want:
                a.fails.append(("data-mode-claim", f"contract says {t} is {want}",
                                "-", [want], [modes[t]], b""))
        for t in ELEMENTS:
            html = (f"<html><body><xwrap><{t}>a&amp;b<zzz>c</zzz></{t}>tail</xwrap>"
                    f"</body></html>").encode()
            a.check_many("data-mode", f"<{t}>a&amp;b<zzz>c</zzz>", html,
                         [f"xwrap > {t}::text", "zzz::text", "xwrap::text"])
        # `script` needs the escaped/double-escaped states on top of "stop at the end tag": taking the
        # FIRST `</script>` fabricates a `<div>` here and desyncs the rest of the document.
        a.check_many("data-mode", "script double-escaped",
                     b"<html><body><script><!--<script></script><div>fake</div></script>"
                     b"<p>real</p></body></html>",
                     ["script::text", "div::text", "p::text"])
        a.check_many("data-mode", "script escaped, single </script> still closes",
                     b"<html><body><script><!-- </script><div>real</div></body></html>",
                     ["script::text", "div::text"])
        a.check_many("data-mode", "plaintext runs to EOF",
                     b"<html><body><div><plaintext>a</plaintext><p>after</p></div></body></html>",
                     ["plaintext::text", "p::text", "div::text"])


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
    """libxml2's NAME-pair start-close relation, over EVERY (open x incoming) pair in the universe.

    The wrapper is an UNKNOWN element on purpose: libxml2 closes it for nothing, so it stays the parent
    boundary and the probe measures only the element under test. A known wrapper (`<div>`) confounds every
    cell where the open element IS a div — which is how a first pass read 72 spurious closes.

    Three probes per cell, because no single one settles every pair:
      * `xwrap > Y::attr(id)` — parentage. An ATTRIBUTE is never foster-parented, so this works even when
        the open element is a `<table>`, where a `::text` probe reads empty whichever way the rule goes.
      * `xwrap > Y::text` — the incoming element's own text.
      * `xwrap > X::text` — the OPEN element's text, which is the only probe that can see a pair where
        the incoming tag inserts no element at all (`<html>`/`<head>`/`<body>`): `aaa` alone means it was
        closed, `aaabbb` means the tag was invisible and the two runs are one text node.
    """
    with a.section(f"start-close relation, all {len(ELEMENTS)}x{len(ELEMENTS)} pairs "
                   f"(libxml2 htmlStartClose)"):
        stale = []
        # NO `x == y` skip: the diagonal (does <X> close an open <X>?) is a rule cell like any other, and
        # skipping it hid the nested-<a> and nested-<form> closes. The probe stays unambiguous because
        # only the SECOND element carries the id / trailing text.
        for x, y in itertools.product(ELEMENTS, ELEMENTS):
            html = f'<html><body><xwrap>xx<{x}>aaa<{y} id="Z">bbb</xwrap></body></html>'.encode()
            label = f"<{x}>aaa<{y} id=Z>bbb"
            probes = [f"xwrap > {y}::attr(id)", f"xwrap > {y}::text", f"xwrap > {x}::text"]
            if x in KNOWN_START_CLOSE_GAP.get(y, ()):
                lx, fr = a.check_gap("start-close", label, html, probes[0], True)
                if lx == fr:
                    stale.append((y, x))
            else:
                a.check_many("start-close", label, html, probes)
        if stale:
            a.fails.append(("start-close", f"{len(stale)} KNOWN_START_CLOSE_GAP entries now AGREE "
                                           f"(remove them): {stale[:6]}", "-", "-", "-", b""))
        print(f"   known gap: {sum(len(v) for v in KNOWN_START_CLOSE_GAP.values())} pairs where libxml2 "
              f"closes and the engine nests (enumerated, not tolerated — see COMPATIBILITY.md)")


def audit_document_frame(a: Audit):
    """`<html>`/`<head>`/`<body>`: accepted as the document frame, IGNORED anywhere else.

    Both halves are rules with observable consequences and neither had a cell anywhere. Without the
    frame closes, `<html><head><title>T</title><body><p>X</p>` left `<body>` nested inside `<head>`, so
    `html > body p::text` was empty. Without the ignore, a stray `<body>` deep in the document created a
    second one and split the text node around it.
    """
    with a.section("document frame (html/head/body)"):
        a.check_many("frame", "body closes head", b"<html><head><title>T</title><body><p>X</p>",
                     ["html > body p::text", "head > body p::text", "html > head > title::text",
                      "body p::text"])
        a.check_many("frame", "a block start closes head",
                     b"<html><head><title>T</title><div id=D>X</head><body>y</body></html>",
                     ["head > div::attr(id)", "div::text"])
        a.check_many("frame", "head-only tags stay in head",
                     b"<html><head><title>T</title><meta id=M><link id=L><style>s</style></head>"
                     b"<body>y</body></html>",
                     ["head > meta::attr(id)", "head > link::attr(id)", "head > style::text"])
        for stray in ("html", "head", "body"):
            a.check_many("frame", f"a redundant <{stray}> inserts nothing and does not split text",
                         f'<html><body><div>d<{stray} id="Z">y</div></body></html>'.encode(),
                         [f"div > {stray}::attr(id)", "div::text", f"{stray}::attr(id)"])
        a.check_many("frame", "a redundant <body> still runs its implied closes",
                     b"<html><body><p>x<body>y</body></html>", ["p::text", "body > p::text"])
        # A redundant frame tag INSIDE the part it names, asked unscoped. Every probe above is scoped to
        # a frame part, and a second `<head>` arriving while one is open is invisible to all of them: if
        # the open head were popped, the duplicate would be inserted as a SIBLING, which no
        # `head > …`/`body > …` selector can see. `close:head,head` was the one cell of the head column
        # that survived the mutation sweep for exactly that reason — an unscoped probe closes it.
        for stray in ("html", "head", "body"):
            a.check_many("frame", f"a duplicate <{stray}> inside itself inserts nothing",
                         f'<html><head><title>T</title><{stray} id="Z"><meta id=M></head>'
                         f"<body>b</body></html>".encode(),
                         [f"{stray}::attr(id)", f"html > {stray}::attr(id)",
                          "head > meta::attr(id)", "head > title::text"])


def audit_frame_synthesis(a: Audit):
    """A document that writes no `<html>`/`<head>`/`<body>` still HAS them — over the whole universe.

    `<html>`, `<head>` and `<body>` all have optional start AND end tags, so a conformant page may
    contain none of them, and libxml2 builds the frame anyway. The engine used to build nothing, so
    `body h1`, `h1 + p` (top-level siblings need a shared parent) and root-level text were all empty
    while lxml answered — the largest single entry the divergence list used to carry.

    Every name is asked where a BARE `<X>` document puts it, because the answer is not the relation it
    resembles: only six names open a `<head>`, while `input`/`noscript`/`template`/`basefont`/`bgsound`/
    `object` survive inside a head that is already open and open none. `frameset`/`frame`/`noframes`
    open neither — a frameset document has no body at all.
    """
    with a.section("document-frame synthesis (whole universe: bare <X> -> which frame part?)"):
        for t in ELEMENTS:
            if t in ("html", "head", "body"):
                continue
            html = f'<{t} id="Z">A</{t}>'.encode()
            a.check_many("frame-synth", f"bare <{t}>", html,
                         [f"head > {t}::attr(id)", f"body > {t}::attr(id)",
                          f"html > {t}::attr(id)", f"html {t}::attr(id)"])
        a.check_many("frame-synth", "the frameless conformant document",
                     b"<!DOCTYPE html><title>T</title><h1>a</h1><p>b</p>",
                     ["head > title::text", "body h1::text", "h1 + p::text",
                      "html > body > p::text", "body > :first-child::text"])
        a.check_many("frame-synth", "root-level text is body text",
                     b"abc<div>d</div>", ["body::text", "body > div::text", "head::text"])
        a.check_many("frame-synth", "whitespace before the frame starts nothing",
                     b"   \n <div>d</div>", ["body::text", "body > :first-child::text"])
        a.check_many("frame-synth", "head content after body content is body content",
                     b"<div>d</div><meta id=M>",
                     ["head > meta::attr(id)", "body > meta::attr(id)"])
        a.check_many("frame-synth", "head content nested in body content stays put",
                     b"<div><meta id=M></div>", ["body div > meta::attr(id)", "head meta::attr(id)"])
        a.check_many("frame-synth", "an explicit frame is used as written",
                     b"<html>  <meta id=M>", ["html > head > meta::attr(id)", "body meta::attr(id)"])


def audit_implied_body(a: Audit):
    """Whatever ENDS the head also STARTS the body — over the whole element universe.

    The engine closed the head correctly (that relation is audited pair-by-pair above) and then had
    nowhere to put what followed, so the content sat under `<html>`: `head + body`, `html > body` and
    `:first-child` all disagreed, in both directions, on four pages of a 1000-page crawl sample.

    Asked for EVERY name rather than the handful anyone would think to list, because the two relations
    are not the same one: `input`, `noscript`, `template`, `basefont`, `bgsound` and `object` do NOT
    close an open head, yet they do open a body after an explicit `</head>`. A hand-written list would
    have encoded whichever of those the author happened to try. Only the implicit end is asserted here —
    after an explicit `</head>` libxml2 and html5lib disagree, and the engine keeps libxml2's shape.
    """
    with a.section(f"implied <body> (whole universe: does what ends the head start it?)"):
        for t in ELEMENTS:
            if t in ("html", "head", "body", "frameset", "frame"):
                continue  # the frame itself, and frameset documents, which have no body at all
            html = (f"<html><head><title>T</title><{t} id=D>X</{t}>"
                    f"<link id=L></head><body>y</body></html>").encode()
            # where the element landed, whether the head kept its own content, and — the half that was
            # actually broken — whether a LATER head-only tag followed it into the body
            a.check_many("implied-body", f"<head><title><{t}>", html,
                         [f"body > {t}::attr(id)", f"head > {t}::attr(id)",
                          "head > title::text", "body > link::attr(id)", "head > link::attr(id)"])
        # character data ends it too, and splits the run at the first non-space character
        a.check_many("implied-body", "non-whitespace text ends the head",
                     b"<html><head>\n\t  TXT<meta id=M></head><body><p>P</p></body></html>",
                     ["body::text", "head::text", "body > meta::attr(id)", "head > meta::attr(id)"])
        a.check_many("implied-body", "whitespace alone does not",
                     b"<html><head>\n  <title>T</title></head><body><p>P</p></body></html>",
                     ["head > title::text", "body > title::text", "body > p::text"])
        a.check_many("implied-body", "text inside a head element is that element's",
                     b"<html><head><title>T</title><style>s</style></head><body><p>P</p></body></html>",
                     ["head > title::text", "head > style::text", "body::text"])


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
        # A NESTED table is a fresh scope boundary even for a table-family closer. This real-page
        # shape was absent from the original same-table probes, which therefore over-generalized
        # "`</tr>` always unwinds" and missed an outer row being closed through an inner table.
        a.check("table-scope-nested", "nested <table> blocks outer </tr>",
                b"<html><body><tr><table>AAA</tr>BBB</body></html>", "table::text")


def audit_div_end_scope(a: Audit):
    """An open div blocks an end tag aimed at one of its ancestors; peer block tags do not."""
    with a.section("div end-tag scope"):
        for outer in ("nav", "form", "ul", "span", "a"):
            html = f"<html><body><{outer}><div>AAA</{outer}>BBB</body></html>".encode()
            a.check("div-end-scope", f"<{outer}><div>AAA</{outer}>BBB", html, "div::text")
        a.check("div-end-scope-control", "blockquote is not a div boundary",
                b"<html><body><nav><blockquote>AAA</nav>BBB</body></html>",
                "blockquote::text")


def audit_rawtext(a: Audit):
    """Entity handling per mode, in the position each element really occurs in."""
    with a.section("rawtext / RCDATA entity handling"):
        for t in ("script", "style", "textarea", "noframes", "iframe", "noembed", "xmp"):
            html = f"<html><body><{t}>a&amp;b &lt; c</{t}></body></html>".encode()
            a.check("rawtext", t, html, f"{t}::text")
        a.check("rawtext", "title", b"<html><head><title>a&amp;b &lt; c</title></head><body>x</body>"
                b"</html>", "title::text")
        a.check("rawtext", "script double-escaped legacy wrapper",
                b"<html><head><script><!--<script></script>--><h4>x</head></html>",
                "h4::text")


def audit_boolean_attrs(a: Audit):
    with a.section("HTML4 minimized boolean attributes"):
        for attr in BOOLEAN_ATTRS:
            html = f"<html><body><div {attr}></div></body></html>".encode()
            a.check("boolean-attr", attr, html, f"div::attr({attr})")
        # Preserve the distinction between a minimized attribute and an explicitly empty value.
        a.check("boolean-attr", "disabled explicitly empty",
                b'<html><body><input disabled=""></body></html>', "input::attr(disabled)")
        a.check("boolean-attr", "unknown minimized attr stays empty",
                b"<html><body><div hidden></div></body></html>", "div::attr(hidden)")



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
    audit_universe(a)
    audit_void(a)
    audit_data_modes(a)
    audit_p_closing(a)
    audit_implied_close(a)
    audit_start_close_pairs(a)
    audit_document_frame(a)
    audit_frame_synthesis(a)
    audit_implied_body(a)
    audit_table_scope(a)
    audit_div_end_scope(a)
    audit_rawtext(a)
    audit_boolean_attrs(a)

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
