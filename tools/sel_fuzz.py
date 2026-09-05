"""
tools/sel_fuzz.py — SELECTOR fuzzer: fuzz the query, not the HTML (complements diff_fuzz.py, which
fuzzes the HTML against a fixed selector basket).

The engine's contract has no fallback: an unsupported OR invalid query must return an EMPTY column —
never an error, never a wrong value (README / CLAUDE.md). So over a stream of random selectors — valid,
exotic-but-unsupported, malformed, and budget-bombs — the invariant is:

    Frostwork's column is EMPTY, or value-equal to lxml. It is NEVER non-empty-and-wrong, NEVER a crash.

Selectors are drawn from the vocabulary that actually appears in conformant.py / foreign.py pages (tags,
`c<N>`/`shared` classes, `i<N>` ids, href/src/data-k/title attrs) so supported queries genuinely match
content — otherwise every column is trivially empty and nothing is tested. Categories mixed per run:
valid CSS, valid XPath, deep `:not()`, exotic (likely-unsupported) forms, CSS ESCAPES (`.\63 1` is
`.c1` to the oracle — a surface no generated selector reached until a review found it), malformed
strings, and budget bombs (>128 comma members / >64 sibling chains) exercising the DEAD-clamp.

Verdicts per (page, selector):
  AGREE       mine == lxml (or empty and lxml empty)
  UNSUPPORTED mine empty, lxml non-empty — ALLOWED (no-fallback coverage gap, not a bug)
  BUDGET      over-budget selector returned non-empty (DEAD-clamp partial) — ALLOWED, reported
  OVERMATCH   parsel rejects the selector as invalid, yet Frostwork returned data — candidate bug
  WRONG       parsel has a value and Frostwork's non-empty column disagrees — candidate bug
  CRASH       engine panic — always a bug

--gate exits nonzero on WRONG + OVERMATCH + CRASH (the hard invariant).

Usage:  .venv/bin/python tools/sel_fuzz.py [--iters N] [--per K] [--seed S] [--gate] [--show M]
"""
from __future__ import annotations

import argparse
import os
import random
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import conformant
import foreign
from diff_lxml import run_engine, run_support, parsel_vals, verdict
import oracle  # same libxml2 >= 2.14 requirement as the gate (parsel is the oracle here too)

TAGS = ["div", "p", "span", "a", "li", "td", "th", "ul", "ol", "dl", "dt", "dd", "option", "select",
        "table", "tr", "b", "em", "strong", "i", "small", "label", "img", "input", "section",
        "article", "header", "footer", "h1", "h2", "h3", "svg", "g", "rect", "circle", "math", "template"]
CLASSES = [f"c{i}" for i in range(12)] + ["shared", "chart", "box", "lnk", "tpl", "row", "price"]
IDS = [f"i{i}" for i in range(12)] + ["tpl1", "s1"]
ATTRS = ["class", "id", "data-k", "href", "src", "value", "title", "alt"]
ATTR_VALS = ["v1", "c1", "shared", "/p1", "/a1", "s1.png", "x", "日本", "café", ""]
ATTR_OPS = ["=", "^=", "$=", "*=", "~=", "|="]
# unquoted attribute values: valid CSS identifiers (cssselect parses them) mixed with non-idents
# (cssselect raises SelectorSyntaxError, so the engine must stay empty — see `is_css_ident`).
UNQUOTED_VALS = ["v1", "c1", "shared", "-v", "_v", "café", "2", "2v", "1e5", "--v", "$v", "/p1",
                 "s1.png", "#v", "", "-"]
COMBS = [" ", " > ", " + ", " ~ "]
TERMS = ["", "::text", "::attr(href)", "::attr(class)", "::attr(data-k)", " ::text"]
# text-content-predicate needles: decoded forms of conformant.py TEXTBITS (full matches), substrings
# (for `contains`), and misses. `g&h`/`x<y` exercise entity-decoded comparison (page has g&amp;h/x&lt;y).
TEXT_NEEDLES = ["alpha", "café", " beta ", "1 2 3", "g&h", "x<y", "al", "et", "2", "zzz", ""]


def g_attr_pred(rng):
    name = rng.choice(ATTRS)
    if rng.random() < 0.35:
        return f"[{name}]"
    if rng.random() < 0.12:
        # UNQUOTED value. A CSS identifier (`v1`, `-v`, `café`) is valid and must match like Parsel;
        # anything else (`2`, `/p1`, `$v`, `--v`) is a cssselect SelectorSyntaxError, so the engine must
        # return EMPTY — a non-empty column there is the OVERMATCH the gate exists to catch.
        return f"[{name}{rng.choice(ATTR_OPS)}{rng.choice(UNQUOTED_VALS)}]"
    return f'[{name}{rng.choice(ATTR_OPS)}"{rng.choice(ATTR_VALS)}"]'


# positional pseudo-classes: supported forward forms + unsupported (last/only/nth-last) for negative
# coverage. `nth-*` args span the An+B microsyntax. Parsel is the oracle for all of them.
_NTH_ARGS = ["odd", "even", "1", "2", "3", "2n", "2n+1", "2n-1", "-n+3", "n", "n+2"]


def g_positional(rng):
    kind = rng.choice([
        "first-child", "first-of-type", "last-child", "only-child",
        "last-of-type", "only-of-type",
        f"nth-child({rng.choice(_NTH_ARGS)})", f"nth-of-type({rng.choice(_NTH_ARGS)})",
        f"nth-last-child({rng.choice(_NTH_ARGS)})", f"nth-last-of-type({rng.choice(_NTH_ARGS)})",
    ])
    return ":" + kind


def g_compound(rng, depth=1):
    parts = []
    if rng.random() < 0.7:
        parts.append(rng.choice(TAGS) if rng.random() < 0.85 else "*")
    for _ in range(rng.randint(0, 3)):
        r = rng.random()
        if r < 0.4:
            parts.append("." + rng.choice(CLASSES))
        elif r < 0.55:
            parts.append("#" + rng.choice(IDS))
        elif r < 0.72:
            parts.append(g_attr_pred(rng))
        elif r < 0.85:
            parts.append(g_positional(rng))
        elif depth > 0:
            parts.append(f":not({g_compound(rng, depth - 1)})")
    return "".join(parts) or rng.choice(TAGS)


def g_has_inner(rng):
    """A single inner compound for `:has(...)`, restricted to a TYPE/`*` + class — the only inner
    cssselect translates (it RAISES on id/attribute/`:not` inners, still at 1.5.0), so this keeps parsel a valid
    oracle for the random fuzzer. The id/attribute/`:not` inners (which Frostwork supports as a
    documented divergence-in-our-favor) are covered by a dedicated parity test in tests/test_python.py."""
    p = rng.choice(TAGS) if rng.random() < 0.7 else ""
    if rng.random() < 0.5:
        p += "." + rng.choice(CLASSES)
    return p or rng.choice(TAGS)


def g_has(rng):
    # Mostly the supported MVP shape (a single, optionally child-scoped, compound); ~15% a descendant
    # chain inside — unsupported, so the whole selector must yield EMPTY (never WRONG). Parsel is oracle.
    inner = g_has_inner(rng)
    if rng.random() < 0.15:
        inner += " " + g_has_inner(rng)  # chain: unsupported (negative coverage)
    rel = "> " if rng.random() < 0.25 else ""
    return f":has({rel}{inner})"


def g_is_alt(rng):
    """One simple compound alternative for `:is(...)` (no combinators)."""
    p = rng.choice(TAGS) if rng.random() < 0.5 else ""
    r = rng.random()
    if r < 0.5:
        p += "." + rng.choice(CLASSES)
    elif r < 0.7:
        p += g_attr_pred(rng)
    elif r < 0.85:
        p += "#" + rng.choice(IDS)
    return p or rng.choice(TAGS)


def g_contains(rng):
    """`:contains(...)` — cssselect's extension, which lowers to `contains(., "needle")`, the same
    deferred text predicate as the XPath `[contains(.,"v")]` spelling. Both argument tokens are emitted,
    since cssselect accepts a STRING or an IDENT and raises on anything else: a bare needle is only used
    when it really is a CSS identifier (`2` is a NUMBER to cssselect, so `:contains(2)` must stay empty).
    """
    needle = rng.choice(TEXT_NEEDLES)
    if needle and needle[0].isalpha() and needle.isalnum() and rng.random() < 0.25:
        return f":contains({needle})"
    return f':contains("{needle}")'


def g_is(rng):
    # SUPPORTED shape only: `[tag|*]:is(alt, ...)` — a lone `:is`/`:where` on a bare tag/universal
    # (cssselect translates only this shape correctly). Parsel is the oracle.
    tag = rng.choice(TAGS) if rng.random() < 0.6 else "*"
    kw = ":is" if rng.random() < 0.7 else ":where"
    alts = ", ".join(g_is_alt(rng) for _ in range(rng.randint(1, 3)))
    return f"{tag}{kw}({alts})"


def g_css(rng):
    n = rng.randint(1, 4)
    s = g_compound(rng)
    # The combinator that introduces the SUBJECT compound, and the selector text up to and including it.
    # Tracked while building rather than recovered by scanning backwards: an explicit combinator carries
    # its own spaces (` + `), so a backward scan stops on that space before it ever reaches the `+` — which
    # is exactly the bug that makes such a generator emit nothing at all.
    subject_comb = None
    for _ in range(n - 1):
        comb = rng.choice(COMBS)
        subject_comb = s + comb
        s += comb + g_compound(rng)
    if rng.random() < 0.18:
        s += g_has(rng)  # attach `:has(...)` to the SUBJECT compound (before the value terminal)
    if rng.random() < 0.15:
        # `:contains()` on the SUBJECT compound. Landing it on the same compound as a `:has()` is
        # deliberate: two deferred kinds is out of tier, so that pair must grade UNSUPPORTED (empty) and
        # never WRONG — the negative half of the coverage this adds.
        s += g_contains(rng)
    if subject_comb and subject_comb.rstrip()[-1] in "+>~" and rng.random() < 0.15:
        # Drop the SUBJECT compound and let the value terminal stand on the implicit universal
        # (`dt + ::text`, `div > ::attr(id)`). parsel strips the pseudo-element before cssselect sees the
        # selector, so it answers this exactly as the `*` spelling — and the `*` spelling is emitted here
        # too, so the pair is compared rather than one side assumed. Only an EXPLICIT combinator: after a
        # descendant one, `E ::text` is parsel's or-self collapse, a different rule already covered.
        star = "*" if rng.random() < 0.5 else ""
        term = rng.choice([t for t in TERMS if t])  # a bare element needs a real subject compound
        return subject_comb + (" " if rng.random() < 0.5 else "") + star + term
    if rng.random() < 0.12:
        # a clean `[tag|*]:is(...)` subject — the shape cssselect translates CORRECTLY, so parsel is a
        # valid oracle. Combined shapes (`div.a:is(...)`, chained `:is`) diverge from cssselect's bug and
        # are covered by a dedicated parity test (tests/test_python.py), not this random oracle.
        s = g_is(rng)
    elif rng.random() < 0.1:
        # Case B (CSS): `C:has(<type+class>) ~/+ S` — a deferred `:has` on a PRECEDING sibling, value from
        # the later sibling. cssselect handles `:has(type+class)` and `~`/`+` correctly, so parsel is a
        # valid oracle. (id/attr/:not `:has` inners are cssselect-invalid, so keep the inner type+class.)
        c = rng.choice(TAGS) + (("." + rng.choice(CLASSES)) if rng.random() < 0.4 else "")
        comb = rng.choice([" ~ ", " + "])
        # The same Case-B shape with a text predicate instead of a `:has` — the label→value pattern
        # (`dt:contains("Price") + dd::text`), which is the reason a scraper reaches for `:contains()`.
        pred = f":has({g_has_inner(rng)})" if rng.random() < 0.5 else g_contains(rng)
        return f"{c}{pred}{comb}{g_compound(rng)}" + rng.choice(TERMS)
    return s + rng.choice(TERMS)


def g_comma(rng):
    return ", ".join(g_css(rng) for _ in range(rng.randint(2, 5)))


def _g_xpath_one(rng):
    steps = rng.randint(1, 3)
    # Sometimes a `.`-relative (context) path: `.//step` (descendant of context) or `./step` (child of
    # context). At the flat top level the context is the document node, so `./step` matches nothing and
    # `.//step` matches descendants — Parsel is the oracle. This exercises the leading-anchor handling
    # that once let `./step` over-match like `.//step` (see xpath.rs relative-anchor rejection).
    s = "." if rng.random() < 0.25 else ""
    for i in range(steps):
        # ~25% of non-anchor steps use the `following-sibling::` axis — same tree relation as CSS `~`,
        # lowered by xpath.rs to a general-sibling combinator. It must follow a single `/` (a `//` before
        # it means descendant-or-self THEN sibling, which is unsupported), and takes no positional
        # predicate (`following-sibling::td[1]` is unsupported). Parsel is the oracle either way.
        sib = i > 0 and rng.random() < 0.25
        if sib:
            s += "/following-sibling::" + (rng.choice(TAGS) if rng.random() < 0.85 else "*")
        else:
            s += rng.choice(["//", "/"]) + (rng.choice(TAGS) if rng.random() < 0.85 else "*")
        if rng.random() < 0.4:
            # a SOLE `[N]` position: `tag[N]` (of-type) / `*[N]` (nth-child). Parsel is the oracle.
            # Skipped on a sibling-axis step (position among following siblings has no `~` lowering).
            if not sib and rng.random() < 0.2:
                s += f"[{rng.randint(1, 4)}]"
                continue
            # a SOLE reverse position `[last()]` / `[last()-k]` (of-type for a tag, nth-last-child for `*`)
            if not sib and rng.random() < 0.15:
                s += rng.choice(["[last()]", "[last()-1]", "[last()-2]", "[position()=last()]"])
                continue
            # a SOLE text-content predicate: `[.="v"]` / `[contains(.,"v")]` / `[text()="v"]` /
            # `[contains(text(),"v")]`. Needles are drawn from the pages' decoded text (TEXTBITS) plus
            # misses/substrings, so both match and no-match paths get coverage. On a non-subject step it's
            # unsupported (empty) — negative coverage. Parsel is the oracle.
            if not sib and rng.random() < 0.22:
                axis = rng.choice([".", "text()"])
                needle = rng.choice(TEXT_NEEDLES)
                op = f'{axis}="{needle}"' if rng.random() < 0.5 else f'contains({axis},"{needle}")'
                s += f"[{op}]"
                continue
            a = rng.choice(ATTRS)
            r = rng.random()
            if r < 0.3:
                s += f"[@{a}]"
            elif r < 0.55:
                s += f'[@{a}="{rng.choice(ATTR_VALS)}"]'
            elif r < 0.7:
                # non-empty operand only: `contains(@a,"")` is a DOCUMENTED divergence (empty needle =
                # match-nothing here vs always-true in XPath-proper), separately covered — not a bug.
                s += f'[contains(@{a},"{rng.choice([x for x in ATTR_VALS if x])}")]'
            else:
                # predicate `or`/`and` (distributed into union members by the compiler)
                b = rng.choice(ATTRS)
                op = rng.choice([" or ", " and "])
                s += f'[@{a}{op}@{b}]'
    # terminal: value, self/descendant text, child `/@a`, or descendant-or-self `//@a` attribute harvest
    tail = rng.choice(["", "/text()", "//text()", f"/@{rng.choice(ATTRS)}", f"//@{rng.choice(ATTRS)}"])
    return s + tail


def _g_upward(rng):
    # `//INNER/ancestor::E` / `//INNER/parent::E` -> `E:has(INNER)` / `E:has(> INNER)`. INNER and E each
    # a compound with an optional attribute predicate. Parsel is the oracle for the reframed node set.
    inner = rng.choice(TAGS)
    if rng.random() < 0.4:
        inner += f'[@{rng.choice(ATTRS)}="{rng.choice([x for x in ATTR_VALS if x])}"]'
    e = rng.choice(TAGS)
    if rng.random() < 0.3:
        e += f"[@{rng.choice(ATTRS)}]"
    axis = rng.choice(["ancestor", "parent"])
    tail = rng.choice(["", "/text()", f"/@{rng.choice(ATTRS)}"])
    return f"//{inner}/{axis}::{e}{tail}"


def _g_caseb_xpath(rng):
    # Case B: `//C[textpred]/following-sibling::S` — a deferred text predicate on a PRECEDING sibling,
    # value from the later sibling (the label->value + text-filter pattern). lxml XPath evaluates this
    # correctly, so parsel is a valid oracle.
    c = rng.choice(TAGS)
    axis = rng.choice([".", "text()"])
    needle = rng.choice(TEXT_NEEDLES)
    pred = f'{axis}="{needle}"' if rng.random() < 0.5 else f'contains({axis},"{needle}")'
    sub = rng.choice(TAGS) if rng.random() < 0.85 else "*"
    tail = rng.choice(["", "/text()", f"/@{rng.choice(ATTRS)}"])
    return f'//{c}[{pred}]/following-sibling::{sub}{tail}'


def _g_nonliteral_operand(rng):
    # Comparisons against something that is NOT a quoted literal, which must stay UNSUPPORTED (empty):
    #   * a variable reference (`$v`) — parsel binds those at call time; Frostwork takes no bindings, so
    #     lxml REJECTS the query here and any non-empty column is an OVERMATCH — the near miss is
    #     auditing it as supported and matching an element whose id really is `$v`);
    #   * a numeric operand (`[@a=2]`, XPath compares numerically: `a="02"` matches) or a bare-name one
    #     (`[@a=b]`, a node-set compare against child `<b>` elements) — valid for lxml, so the engine's
    #     empty column grades as UNSUPPORTED, but a non-empty one grades WRONG.
    a = rng.choice(ATTRS)
    tag = rng.choice(TAGS)
    tail = rng.choice(["", "/text()", f"/@{a}"])
    pred = rng.choice([
        f"[@{a}=$v]",
        f"[contains(@{a},$v)]",
        f"[starts-with(@{a},$v)]",
        f'[.=$v]',
        f"[@{a}={rng.randint(0, 20)}]",
        f"[@{a}={rng.choice(TAGS)}]",
        f"[contains(@{a},{rng.randint(0, 20)})]",
    ])
    return f"//{tag}{pred}{tail}"


def g_xpath(rng):
    # ~6% a non-literal comparison operand (variable / number / bare name): must be empty, never wrong.
    if rng.random() < 0.06:
        return _g_nonliteral_operand(rng)
    # ~10% Case B: text-predicate on a preceding sibling (label->value). Parsel is the oracle.
    if rng.random() < 0.1:
        return _g_caseb_xpath(rng)
    # ~12% an upward-axis path (ancestor::/parent:: reframed as :has). Parsel is the oracle.
    if rng.random() < 0.12:
        return _g_upward(rng)
    # ~12% normalize-space(<path>): scalar (first node's string-value, ws-collapsed). Parsel is the
    # oracle; an `or`-expanding inner is an allowed UNSUPPORTED gap (empty), never WRONG.
    if rng.random() < 0.12:
        return f"normalize-space({_g_xpath_one(rng)})"
    # ~20% of the time, a union of 2-3 independent paths (`//a | //b`) — one document-ordered,
    # node-deduped column, same as a CSS comma group. Parsel is the oracle for the merged result.
    if rng.random() < 0.2:
        return " | ".join(_g_xpath_one(rng) for _ in range(rng.randint(2, 3)))
    return _g_xpath_one(rng)


def g_exotic(rng):
    # valid CSS syntax that is probably OUTSIDE the supported subset (must still be empty, never wrong)
    return rng.choice([
        f"{rng.choice(TAGS)}:nth-child({rng.randint(1,3)})::text",
        f"{rng.choice(TAGS)}:first-child::text",
        f"{rng.choice(TAGS)}:hover::text",
        f"{rng.choice(TAGS)}::before",
        f"{rng.choice(TAGS)} {rng.choice(TAGS)}:not({g_compound(rng)}):not({g_compound(rng)})::text",
        f'{rng.choice(TAGS)}[{rng.choice(ATTRS)}="{rng.choice(ATTR_VALS)}" i]::text',
        f"{rng.choice(TAGS)} >> {rng.choice(TAGS)}::text",
        f":is({rng.choice(TAGS)}, {rng.choice(TAGS)})::text",
    ])


def g_malformed(rng):
    base = g_css(rng)
    op = rng.choice(["trunc", "brack", "paren", "trailcomb", "emptymember", "badpseudo", "stray",
                     "dblcolon", "unicode"])
    if op == "trunc" and len(base) > 3:
        return base[: rng.randint(1, len(base) - 1)]
    if op == "brack":
        return base + "["
    if op == "paren":
        return base.replace("::text", ":not(") or base + ":not("
    if op == "trailcomb":
        return base + rng.choice([" >", " +", " ~", " "])
    if op == "emptymember":
        return base + ", , " + g_compound(rng) + "::text"
    if op == "badpseudo":
        return base + "::" + rng.choice(["bogus", "attr(", "text(", "attr()"])
    if op == "stray":
        return rng.choice(["!@#", "()", "[]", "><", base + "@#$", "{}" + base])
    if op == "dblcolon":
        return base.replace("::", ":::")
    return base + "·λ→"  # non-ASCII junk


def g_escaped(rng):
    """CSS ESCAPES — a surface this fuzzer could not reach, so hand vectors were the only net.

    cssselect DECODES escapes before matching: `.\\63 1` is `.c1`, `[data-k="caf\\e9"]` is
    `[data-k="café"]`. An engine that keeps the backslash literally answers a *different* selector than
    the oracle — and because escapes never appeared in any generated selector, that went unnoticed until a
    review read the parser. The generated forms decode onto names the pages really carry, so parsel has
    values and a literal-matching engine grades WRONG rather than harmlessly-empty-on-both-sides.

    Class and ID escapes are decoded, while attribute-name escapes remain unsupported. These
    probes exercise both the supported surface and the refusal boundary. The trailing-lone-backslash forms are ones cssselect REJECTS, where any non-empty column is
    an OVERMATCH.

    The escaped values deliberately use SUBSTRING/PREFIX operators over `class`/`data-k`/`href`, which
    every generated page carries. An exact-match probe (`[data-k="\76 1"]` for `data-k="v1"`) only
    discriminates on pages that happen to hold that exact value: measured pre-fix it caught the bug on
    3 seeds out of 4 and missed on the fourth, i.e. a net that passes a broken engine 25% of the time.
    Retargeted it fails every seed. It then found a SECOND bug the hand vectors missed —
    `::attr(data-\6b)` was reported supported and matched literally, so it answered a selector parsel
    answers with an empty column.
    """
    return rng.choice([
        # DISCRIMINATING: escapes where the engine decodes, matching content every page has
        r'[class*="\63"]::text',            # -> [class*="c"]
        r'[class^="\63 "]::text',           # -> [class^="c"]  (space terminates the escape)
        r'[data-k^="\76"]::text',           # -> [data-k^="v"]
        r'[data-k^="\000076"]::text',       # -> same, 6-digit form (max escape length, no terminator)
        r'[href*="\2f "]::attr(href)',      # -> [href*="/"]   (escaped non-identifier char)
        r'[title*="\74"]::text',            # -> [title*="t"]
        r'[class*="c\31"]::text',           # -> [class*="c1"] (escaped digit)
        r'[data-k]::attr(data-\6b)',        # ::attr() argument -> data-k
        # decodes fine but matches nothing on these pages (title is `t` / `a<n>`): a decode-path probe
        r'[title*="caf\e9"]::text',         # -> [title*="café"], non-ASCII via escape
        # Identifier escapes: class/ID supported; attribute names still refused
        r".\63 1::text",                    # class name
        r"#\69 1::text",                    # id
        r'[data-\6b ="v1"]::text',          # attribute name
        r".\-x::text",                      # `\-` is a literal dash, not a hex escape
        # REJECTED BY cssselect: any non-empty column here is an OVERMATCH
        ".c1\\",                            # lone trailing backslash
        '[data-k="v1\\"]::text',            # unterminated string via escaped quote
    ])


def g_quoted_delim(rng):
    r"""QUOTED DELIMITERS inside a functional pseudo — the same blind spot as the escapes above, one
    level up: no generated selector ever put a `)` or a `,` inside a quoted attribute value, so the
    entire question "does the `:is()`/`:not()` argument scanner know what a string is?" rode on hand
    vectors. It did not: a bare paren-depth counter ended the pseudo at the quoted `)`, the leftover
    `"])` failed to parse, and `div:is(#outer, [data-x=")"])` — valid CSS that parsel answers — reported
    UNSUPPORTED and returned an empty column.

    Discrimination is the whole design here. The generated pages contain no `)` in any attribute value,
    so an exact-match probe would be empty on BOTH sides and grade AGREE against a broken parser. These
    put the quoted delimiter inside a `:not()` (or as the losing alternative of an `:is()`) so the
    predicate is universally TRUE and the column equals the plain selector's.

    Be precise about what that measures, because it is NOT a red gate. A parser gap makes these selectors
    report UNSUPPORTED, and empty-when-unsupported is exactly what the no-fallback contract permits — so
    against the pre-fix build this family moves from 0 UNSUPPORTED pairs to ~20-30% of the family (303,
    221 and 216 pairs over seeds 0/1/2) and the gate still passes. What it buys is the WRONG/OVERMATCH
    invariant across many generated pages, plus a visible number in the per-category table. The assertion
    that these shapes must be SUPPORTED (the half that goes red) is a contract sweep:
    `tests/test_python.py::test_quoted_delimiters_in_functional_pseudos_are_supported`.

    COMBINED and CHAINED `:is()`/`:where()` forms are generated here, which needs cssselect >= 1.5.0:
    at <= 1.4.0 it mis-translates an `:is()` whose compound carries any other condition, and the fuzzer's
    oracle is parsel. `tools/oracle.py` enforces that floor. One of them is a red-gate discriminator for
    the engine's own semantics rather
    than a coverage probe: `[class*="c"]:is([class*=")"])` is base-AND-fail, so the correct column is
    EMPTY, and an engine that ever regressed to OR semantics would return the whole `[class*="c"]` set
    and grade OVERMATCH.

    `:has()` is still left out, and so is every other BEYOND-lxml form — a widened `:has()` inner, a
    `:has()`/`:not()` selector LIST, and the `[a=v i]` case flag. cssselect rejects all of them even at
    1.5.0, so `theirs is None` and the CORRECT answer grades OVERMATCH: this fuzzer's oracle is parsel,
    and parsel cannot express what those selectors mean. They are covered instead by the contract sweep
    in `tests/test_python.py::test_selector_grammar_surface_obeys_the_contract`, which declares each one
    with its expected values, and by a per-feature test that oracles against the equivalent spelling
    parsel CAN run (`div:has(a), div:has(img)` for `div:has(a, img)`; `:not(a):not(b)` for `:not(a, b)`;
    an lxml `translate()` for the case flag). Generating them here would need that per-form oracle inside
    the fuzzer, which is the same assertion twice — so the rule is: a new beyond-lxml capability is added
    to the contract sweep, NOT to these generators.
    """
    return rng.choice([
        # DISCRIMINATING: universally-true `:not()`, so the column matches the plain selector's
        r'[class*="c"]:not([class*=")"])::text',
        r'[class*="c"]:not([class*="("])::text',
        r'[class*="c"]:not([class*=","])::text',
        r"[class*='c']:not([class*=')'])::text",
        r'[class*="c"]:not([title*="a(b"])::text',
        r'[class*="c"]:not([class*="\29"])::text',   # the same `)`, written as a CSS escape
        r'[class*="c"]:not([class*="\2c"])::text',   # ...and the same `,`
        # ...and the `:is()`/`:where()` form: the second alternative is the one that matches
        r':is([class*=")"], [class*="c"])::text',
        r':where([class*="("], [class*="c"])::text',
        r':is([data-k*=","], [class*="c"])::text',
        # COMBINED base + `:is()` — unlocked by the cssselect 1.5.0 AND fix (see the docstring). Base is
        # universally true and one alternative matches, so the column equals the plain selector's.
        r'[class*="c"]:is([class*=")"], [class*="c"])::text',
        r'[class*="c"]:where([class*="("], [class*="c"])::text',
        r'[class*="c"]:not([title*="a(b"]):is([data-k*=","], [class*="c"])::text',
        # CHAINED groups: every group must match, so this is still the plain selector's column
        r'[class*="c"]:is([class*=")"], [class*="c"]):is([class*="("], [class*="c"])::text',
        # AND-vs-OR DISCRIMINATOR: base AND (fails) => correct column is EMPTY. An engine that ORed the
        # alternatives onto the base (the old cssselect bug) would return every `[class*="c"]` element,
        # which grades OVERMATCH -> RED. This is the one form here that can fail the gate.
        r'[class*="c"]:is([class*=")"])::text',
        r'[class*="c"]:where([data-k*=","])::text',
        # PARSES but matches nothing on either side — a parse-path probe, not a discriminator
        r'[data-k*=")"]::text',
        r'[class$=")"]::text',
        r':is([class*=")"])::text',
        # REJECTED BY cssselect: any non-empty column here is an OVERMATCH
        r'[class*="c"]:not([class*=")"]::text',      # argument never closed
        r':is([class*=")"], [class*="c"]::text',
        r':is([class*=")::text',                     # unterminated string
    ])


def gen_selector(rng):
    r = rng.random()
    if r < 0.30:
        return g_css(rng), False
    if r < 0.45:
        return g_xpath(rng), False
    if r < 0.57:
        return g_comma(rng), False
    if r < 0.68:
        return g_exotic(rng), False
    if r < 0.76:
        return g_escaped(rng), False
    if r < 0.84:
        return g_quoted_delim(rng), False
    if r < 0.92:
        return g_malformed(rng), False
    # budget bombs: clearly over the DEAD-clamp thresholds; must be crash-safe (empty or partial)
    if rng.random() < 0.5:
        return ", ".join(f".c{i % 12}::text" for i in range(rng.randint(130, 200))), True
    base = "li" + " ~ li" * rng.randint(66, 90) + "::text"
    return base, True


def over_budget(sel):
    """Loose upper bound on the two DEAD-clamp thresholds (matcher.rs MAX_MEMBERS/MAX_SIB_BITS): a
    comma group past 128 members, or a chain past 64 sibling combinators, may legitimately return a
    DEAD-clamped partial. Over-count is safe here (only suppresses false WRONG flags)."""
    members = sel.count(",") + 1
    sib = sel.count("+") + (sel.count("~") - sel.count("~="))
    return members > 128 or sib > 64


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iters", type=int, default=6000, help="random selectors to test")
    ap.add_argument("--per", type=int, default=4, help="random pages each selector is tested against")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--gate", action="store_true", help="exit nonzero on WRONG/OVERMATCH/CRASH")
    ap.add_argument("--show", type=int, default=12)
    oracle.add_argument(ap)
    args = ap.parse_args()
    oracle.require(args.allow_old_libxml2)
    rng = random.Random(args.seed)

    pool = [conformant.generate(rng) for _ in range(60)] + [foreign.generate(rng) for _ in range(20)]
    trials = [gen_selector(rng) for _ in range(args.iters)]
    supported = run_support([sel for sel, _bomb in trials])
    cases = []  # (html, [sel], sel, is_bomb)
    for (sel, bomb), is_supported in zip(trials, supported):
        for _ in range(args.per):
            cases.append((rng.choice(pool), [sel], sel, bomb, is_supported))

    stat = defaultdict(int)
    cat = defaultdict(lambda: defaultdict(int))
    examples, seen = [], set()
    crash_diags = []

    CHUNK = 500
    for base in range(0, len(cases), CHUNK):
        chunk = cases[base: base + CHUNK]
        # budget bombs are deliberate here (see the BUDGET verdict), so do NOT ask the bridge to treat
        # an over-budget schema as a harness error — surviving it with empty/partial columns is the test
        results, crashed, diag = run_engine([(h, s) for h, s, _sel, _b, _sup in chunk],
                                            strict_budget=False)
        stat["CRASH"] += crashed
        if diag:
            crash_diags.append((base, diag))
        for k, (html, _sels, sel, bomb, is_supported) in enumerate(chunk):
            cols = results[k] if k < len(results) else None
            if cols is None:
                continue
            mine = cols[0] if cols else []
            theirs = parsel_vals(html, sel)  # None == parsel rejected the selector
            if bomb or over_budget(sel):
                v = "BUDGET" if mine else ("AGREE" if not theirs else "UNSUPPORTED")
            elif not mine:
                # The old gate called every empty-vs-nonempty result UNSUPPORTED, even when the compiler
                # promised support. That allowed a supported feature to regress to always-empty. Support
                # is now authoritative: a promised selector dropping oracle values is WRONG.
                v = "AGREE" if not theirs else ("WRONG" if is_supported else "UNSUPPORTED")
            elif not is_supported:
                # Unsupported must be empty. Non-empty output is a no-fallback contract violation even
                # when it happens to equal lxml for this page.
                v = "OVERMATCH"
            elif theirs is None:
                v = "OVERMATCH"
            else:
                v = "AGREE" if verdict(mine, theirs, "CONTROL", sel) in ("AGREE", "WS") else "WRONG"
            stat[v] += 1
            stat["pairs"] += 1
            cat[_category(sel)][v] += 1
            if v in ("WRONG", "OVERMATCH") and sel not in seen and len(examples) < args.show:
                seen.add(sel)
                examples.append((v, sel, mine[:4], (theirs or [])[:4], html.decode("utf-8", "replace")[:120]))

    pairs = stat["pairs"] or 1
    print(f"SELECTOR FUZZ vs lxml   seed={args.seed}  iters={args.iters}  per={args.per}  pairs={stat['pairs']}\n")
    for k in ("AGREE", "UNSUPPORTED", "BUDGET"):
        print(f"  {k:<12} {stat[k]:>8}  ({100.0 * stat[k] / pairs:.2f}%)")
    print(f"  {'OVERMATCH':<12} {stat['OVERMATCH']:>8}  <-- gate: non-empty on a parsel-invalid selector")
    print(f"  {'WRONG':<12} {stat['WRONG']:>8}  <-- gate: non-empty AND disagrees with lxml")
    print(f"  {'CRASH':<12} {stat['CRASH']:>8}  <-- gate: engine panic\n")

    print("  by category:  category      pairs  AGREE  UNSUP  BUDGET  OVERMATCH  WRONG")
    for c in ("css", "xpath", "comma", "exotic", "escaped", "quoted", "malformed", "bomb"):
        d = cat[c]
        p = sum(d.values())
        print(f"    {c:<12}{p:>8}{d['AGREE']:>7}{d['UNSUPPORTED']:>7}{d['BUDGET']:>8}"
              f"{d['OVERMATCH']:>10}{d['WRONG']:>7}")

    if examples:
        print("\n  candidate bugs (non-empty where lxml has a different value or rejects the selector):")
        for v, sel, mine, theirs, snip in examples:
            print(f"    [{v}] {sel!r}\n        mine ={mine}\n        lxml ={theirs}\n        html: {snip!r}")

    if crash_diags:
        print("\n  ENGINE CRASHES:")
        for where, diag in crash_diags:
            print(f"    [chunk@{where}] {diag}")

    gate = stat["WRONG"] + stat["OVERMATCH"] + stat["CRASH"]
    if args.gate:
        print(f"\n  GATE: WRONG+OVERMATCH+CRASH = {gate}  ->  {'PASS' if gate == 0 else 'FAIL'}")
        sys.exit(1 if gate else 0)


def _category(sel):
    s = sel.strip()
    if s.count(",") > 3:
        return "bomb" if over_budget(s) else "comma"
    if "~ li ~ li ~ li" in s:
        return "bomb"
    if s.startswith(("/", ".//")):
        return "xpath"
    # a delimiter INSIDE a quoted value is the quoted-delimiter family, whether or not it also carries an
    # escape — checked before "escaped" so the two categories do not collide in the report
    if any(d in s for d in ('=")', "=')", '="(', "='(", '=",', "=',", '*="\\29', '*="\\2c',
                            '="a(b', '$=")')):
        return "quoted"
    if "\\" in s:
        return "escaped"
    if any(x in s for x in (":nth", ":first", ":hover", "::before", ">>", ":is(", " i]", ":::")):
        return "exotic"
    if any(x in s for x in ("[", "]", "(", ")")) and not _balanced(s):
        return "malformed"
    if "," in s:
        return "comma"
    if not s or any(ord(c) > 127 for c in s) or "@#" in s or "{}" in s:
        return "malformed"
    return "css"


def _balanced(s):
    return s.count("[") == s.count("]") and s.count("(") == s.count(")")


if __name__ == "__main__":
    main()
