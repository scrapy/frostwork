"""
tools/browser_docs.py: the documents and value columns the browser differential grades.

A document is a tuple of `(id, token)` pairs. Every element token has an id slot (`{}`) so the whole-tree
fingerprint can name each element, and the id is the token's position in the ORIGINAL document, so a
reduced document's columns still name the same elements. Four families:

  * `seq_docs`: every sequence of `seq_sweep.ALPHABET` up to a depth;
  * `random_docs`: seeded sequences over `ALPHABET`, every fourth one long, every other one with a doctype;
  * value documents: `value_docs` sequences whose tokens carry attributes and text from VALUE_ATTRS and
    VALUE_TEXTS (a decoration), `tail_docs` (a deferred winner inside every context) and
    `structured_docs` (the shapes a random run of a few tokens almost never assembles);
  * `generate` assembles them for a run and counts how often each token was emitted, so a run can refuse
    to grade when one never was.

`value_selectors` is the value columns asked of a document.
"""
from __future__ import annotations

import functools
import itertools
import os
import random
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seq_sweep  # noqa: E402  (the short-sequence alphabet)

DOCTYPE = "<!DOCTYPE html>"
# `probe.cjs`'s TOKENS with an id slot on every start tag, then the tokens it lacks.
ALPHABET: list[str] = [
    "<p{}>", "</p>", "<div{}>", "</div>", "<table{}>", "</table>", "<tr{}>", "<td{}>", "</td>", "<b{}>",
    "</b>", "<i{}>", "</i>", "<a{} href=x>", "</a>", "<li{}>", "<ul{}>", "<select{}>", "<option{}>",
    "</select>", "<template{}>", "</template>", "t", "<br{}>", "</br>", "<form{}>", "</form>", "<body{}>",
    "</body>", "<html{}>", "</html>", "<head{}>", "<title{}>T</title>", "<svg{}>", "</svg>", "<math{}>",
    "<!--c-->", "<button{}>", "<h1{}>", "<dd{}>", "<dt{}>", "<caption{}>", "<colgroup{}>", "<frameset{}>",
    "<pre{}>", "<textarea{}>", "<script{}>s</script>", "<style{}>s</style>", "<image{}>", "<nobr{}>",
    "<marquee{}>", "<object{}>", "<input{}>", "<hr{}>", "<![CDATA[x]]>", "<?pi?>", "<p{}/>", "<div{}/>",
    "<frame{}>", "<dl{}>", "<tbody{}>", "<th{}>", "<title{}>", "<foreignObject{}>", "<desc{}>", "<mi{}>",
    '<annotation-xml{} encoding="text/html">', "<template{} shadowrootmode=open>",
    "<template{} shadowrootmode=closed>", "<selectedcontent{}>",
    "<noscript{}>", "<section{}>", "\n", "\x0b", "\x00", "<col{}>",
    # Inputs the tree-construction rules read: the frame and text modes, headings and lists, ruby,
    # <select>, table parts and their end tags, the adoption agency's triggers and markers, Noah's Ark
    # (three alike), the foreign-content breakouts and integration points, declarative shadow hosts,
    # the leading newline in all three spellings, and the doctypes that set quirks and limited quirks.
    "</head>", "</li>", "</dd>", "</h1>", "<h2{}>", "</h2>", "</button>", "</option>", "</ul>",
    "</pre>", "</tr>", "</tbody>", "</th>", "</caption>", "</colgroup>", "</frameset>", "</nobr>",
    "</object>", "</marquee>", "</math>", "</foreignObject>", "</title>", "</span>", "</dl>",
    "<listing{}>", "<plaintext{}>", "<xmp{}>", "<iframe{}>", "<noembed{}>", "<noframes{}>",
    "<ruby{}>", "<rt{}>", "<rp{}>", "<rb{}>", "<rtc{}>", "<optgroup{}>", "<ol{}>", "<span{}>",
    "<thead{}>", "<tfoot{}>", "<applet{}>", "<em{}>", "<s{}>", "<font{}>", '<font{} color=red>',
    '<b{} class=k>', '<input{} type=hidden>', '<input{} type=HIDDEN>', "<img{}>", "<wbr{}>", "<keygen{}>",
    "<meta{}>", "<link{}>", "<base{}>", "<address{}>", "<center{}>", "<details{}>", "<dialog{}>",
    "<search{}>", "<summary{}>", "<fieldset{}>", "<menu{}>", "<x-y{}>", "<clipPath{}>", "<mtext{}>",
    "<mo{}>", "<mglyph{}>", "<malignmark{}>", "<annotation-xml{}>",
    '<annotation-xml{} encoding="application/xhtml+xml">', "<svg{}/>", "<math{}/>", "<script{}/>",
    "<template{} shadowrootmode=OPEN>", " ", "\r\n", "&#10;",
    '<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">',
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "x">',
    # The replay's inputs: the selectedness `<selectedcontent>` copies (a `selected` option, a disabled
    # one, a `multiple` select, which has none), and U+FFFD, which leaves frameset-ok set in Chrome.
    "<option{} selected>", "<option{} disabled>", "<select{} multiple>", "</selectedcontent>", "&#0;", "\ufffd",
    # Attribute names as HTML5 adjusts them on SVG and MathML (camel case, `definitionURL`, the
    # namespaced `xlink:`/`xml:`/`xmlns`), each inside its foreign element, so the value columns see them.
    '<svg{} VIEWBOX="0 0 1 1"><text textlength=5>t</text><a xlink:href=u xmlns=q fill=red>y</a></svg>',
    "<math{} definitionurl=d><mi definitionURL=k xml:lang=en>x</mi></math>",
    # Tag names in any letter case (HTML lowercases them, SVG camel-cases them by its own table), the
    # formatting elements past b/i/a/nobr/em/s/font, and the end tags of elements nothing above closes.
    "<DIV{}>", "</DIV>", "<B{}>", "</B>", "<TABLE{}>", "<SVG{}>", "</SVG>", "<MATH{}>", "<FOREIGNOBJECT{}>",
    "<u{}>", "<code{}>", "<strong{}>", "<small{}>", "<big{}>", "<tt{}>", "<strike{}>",
    "</em>", "</s>", "</font>", "</u>", "</code>", "</strong>", "</small>", "</big>", "</tt>", "</strike>", "</applet>", "</optgroup>", "</ruby>", "</rtc>", "</rt>",
    "</mi>", "</xmp>", "</iframe>", "</noembed>", "</textarea>", "</noscript>",
    # attributes besides the id, for the attribute sets the adoption agency and reconstruction copy
    "<a{} href=y>", "<a{} href=x title=t>", "<b{} CLASS=k>", '<b{} class="k">', "<b{} class=k class=j>",
    "<font{} size=1>",
    # text the tree builder reads as characters, and the markup declarations the tokenizer special-cases
    "&amp;", "x\x00y", "<!x>", "<!-->", "<!--a--!>", "--!>", "<![CDATA[x",
    # raw-text elements the input ends inside
    "<script{}>", "<style{}>",
]
# Formatting start tags that all carry the SAME id, so Noah's Ark can find three alike among them: every
# `{}` slot is a unique id, and no two such elements ever have the same attributes. Spellings of one
# attribute set (order, quoting, a reference, a repeated name, letter case), and sets that differ.
ARK_ID = "k"
ARK: list[str] = [
    "<b id=k>", "<b id=k class=c>", "<b class=c id=k>", '<b id=k class="c">', "<b id=k class=&#99;>",
    "<b id=k class=c class=d>", "<b ID=k CLASS=c>", "<b id=k class=d>", "<a id=k href=x>", "<a id=k href=y>",
    "<font id=k color=red>", "<font id=k size=1>", "<i id=k>",
]
ALPHABET += ARK
# Runs of nested `<div>`s that put what follows at Chrome's depth cap (512 open elements, `<html>` and
# `<body>` included): one token each, so a reduction keeps or drops the run whole. Their elements carry no
# id, which keeps the fingerprint to the tokens after them.
DEEP: dict[str, int] = {"<div>" * n: n for n in (508, 509, 510, 511)}
MAX_TOKENS = 7
# Every fourth random document is longer, for what only a sequence of many tokens reaches: long lists of
# active formatting elements, the adoption agency's inner-loop limit, nesting several scopes deep.
LONG_TOKENS = 20

Doc = tuple[tuple[int, str], ...]
Decorations = dict[int, tuple[str, str]]   # a decorated document's attrs and text by token id: (kind, decoration)


def _start_tag(token: str) -> bool:
    return token.startswith("<") and len(token) > 1 and token[1].isalpha()


def render(doc: Doc) -> str:
    return "".join(tok.format(f' id="{i}"') if "{}" in tok else tok for i, tok in doc)


def ids_of(doc: Doc) -> list[str]:
    ids = [str(i) for i, tok in doc if "{}" in tok]
    return ids + [ARK_ID] if any(tok in ARK for _i, tok in doc) else ids


def seq_docs(depth: int) -> list[Doc]:
    return [tuple(enumerate(seq)) for n in range(1, depth + 1)
            for seq in itertools.product(seq_sweep.ALPHABET, repeat=n)]


def random_docs(n: int, seed: int) -> tuple[list[Doc], Counter]:
    """`n` documents of 1 to `MAX_TOKENS` tokens, every fourth one of up to `LONG_TOKENS`; every other one
    starts with a doctype."""
    rng, used, docs = random.Random(seed), Counter[str](), []
    for k in range(n):
        toks = [rng.choice(ALPHABET) for _ in range(1 + rng.randrange(LONG_TOKENS if k % 4 == 3 else MAX_TOKENS))]
        used.update(toks)
        docs.append(tuple(enumerate(([DOCTYPE] if k % 2 else []) + toks)))
    return docs, used


# ---------------------------------------------------------------- value documents
# The fingerprint grades the tree; the value columns grade what a selector RETURNS from it. A value
# document is a run of ALPHABET tokens, decorated: a start tag carries attributes from VALUE_ATTRS,
# inserted before its id slot so `{}/>` and the tag name stay where the attribution rules look, and a
# text token `t` becomes one of VALUE_TEXTS. Each decorated token is recorded, so tree attribution sees the
# plain ALPHABET token and a counterfactual edit is decorated again before the oracle is asked about it.
VALUE_ATTRS: list[str] = [
    # conformant.py's vocabulary, so its basket has something to answer
    ' class="shared"', ' class="c1 shared"', ' data-k="v1"', ' href="/p1"', ' src="s1.png"', ' value="v"',
    ' title="t"', ' title="a\r\nb\rc"',
    # entities, and the attribute-value exception for a semicolon-less reference
    ' title="x&amp;y"', ' title="&lt;&#x41;&#0;&#x80;"', ' title="a&notit;b&noti=c"', " data-u=a&amp;b",
    # NUL (U+FFFD in a browser) and control characters a browser keeps and lxml cannot hold
    ' title="a\x00b"', ' title="a\x01b\x0bc"',
    # class lists split on ASCII whitespace, form feed included, and on no other
    ' class="a b"', ' class="a\x0cb"', ' class="a\tb"', ' class="a\N{IDEOGRAPHIC SPACE}b"',
    ' class="a\N{NO-BREAK SPACE}b"', ' class="A"',
    # letter case: attributes whose values HTML has CSS match case-insensitively, and one it does not
    ' lang="EN"', ' lang="en"', ' dir="RTL"', ' align="Center"', ' rel="NoFollow"', ' type="SUBMIT"',
    ' target="_Blank"', ' data-case="Mixed"',
    # an upper-case name, a duplicate (the first wins) and an empty value
    ' TITLE="Up"', ' title="1" title="2"', ' data-e=""',
    # valueless attributes, among them libxml2's minimized booleans, which lxml mode gives their own name
    ' disabled', ' defer', ' checked', ' hidden',
    # a nonce, which a page load's header CSP hides from the DOM and the oracle restores
    ' nonce="n1"',
    # names HTML5 adjusts on an SVG or MathML element, and leaves lowercased on an HTML one
    ' VIEWBOX="0 0 1 1"', ' textlength="5"', ' definitionurl="d"', ' xlink:href="u"', ' xmlns="q"', ' FILL="red"',
]
VALUE_TEXTS: list[str] = ["alpha", " beta ", "g&amp;h", "x&lt;y", "café", "a&nbsp;b", "&notin;x", "&copy x",
                          "&#0;", "&#x80;", "x\x01y", "a\N{IDEOGRAPHIC SPACE}b", "a&#13;b"]
# The structure of half the value documents: ordinary containers, taken from ALPHABET by value, plus
# elements no tree rule treats specially, which conformant.py's basket asks about and ALPHABET does not hold.
# These documents open with an explicit `<head></head>`, so their value columns do not depend on how the
# builder synthesises a head.
PLAIN_STRUCTURE = {"<p{}>", "</p>", "<div{}>", "</div>", "<b{}>", "</b>", "<i{}>", "</i>", "<a{} href=x>",
                   "</a>", "<li{}>", "<ul{}>", "t", "<br{}>", "<input{}>", "<section{}>", "<dl{}>", "<dd{}>",
                   "<dt{}>", "<svg{}>", "</svg>"}
VALUE_STRUCTURE = ["<span{}>", "</span>", "<label{}>", "</label>", "<img{}>"]
PLAIN_HEAD = "<head></head>"
TEXT_TOKEN = "t"
VALUE_TOKENS = VALUE_ATTRS + VALUE_TEXTS + VALUE_STRUCTURE

# decorated token -> (ALPHABET token, "attrs" | "text", the decoration)
_DECORATED: dict[str, tuple[str, str, str]] = {}


def _decorate(token: str, kind: str, deco: str) -> str:
    out = token.replace("{}", deco + "{}", 1) if kind == "attrs" else deco
    _DECORATED[out] = (token, kind, deco)
    return out


def undecorate(doc: Doc) -> tuple[Doc, Decorations]:
    """`doc` with every decorated token back to its ALPHABET token, and the decorations by id."""
    plain, decos = [], {}
    for i, t in doc:
        base = _DECORATED.get(t)
        if base is None:
            plain.append((i, t))
        else:
            plain.append((i, base[0]))
            decos[i] = (base[1], base[2])
    return tuple(plain), decos


def redecorate(doc: Doc, decos: Decorations) -> Doc:
    """Put `decos` back on an (edited) undecorated document, by id; an edited token keeps its edit."""
    out = []
    for i, t in doc:
        kind, deco = decos.get(i, ("", ""))
        if kind == "attrs" and "{}" in t:
            t = t.replace("{}", deco + "{}", 1)
        elif kind == "text" and t == TEXT_TOKEN:
            t = deco
        out.append((i, t))
    return tuple(out)


def value_docs(n: int, seed: int) -> tuple[list[Doc], Counter]:
    """`n` decorated documents: plain or full-ALPHABET structure, with and without a doctype, in turn."""
    rng, used, docs = random.Random(seed ^ 0x5EED), Counter[str](), []
    plain = [t for t in ALPHABET if t in PLAIN_STRUCTURE] + VALUE_STRUCTURE
    for k in range(n):
        pool = plain if k % 4 < 2 else ALPHABET + VALUE_STRUCTURE
        toks = []
        for _ in range(2 + rng.randrange(MAX_TOKENS)):
            t = TEXT_TOKEN if rng.random() < 0.3 else rng.choice(pool)
            if t in VALUE_STRUCTURE:
                used[t] += 1
            if _start_tag(t) and "{}" in t:
                attrs = rng.sample(VALUE_ATTRS, rng.choices((0, 1, 2), (3, 5, 2))[0])
                used.update(attrs)
                if attrs:
                    t = _decorate(t, "attrs", "".join(attrs))
            elif t == TEXT_TOKEN and rng.random() < 0.8:
                text = rng.choice(VALUE_TEXTS)
                used[text] += 1
                t = _decorate(t, "text", text)
            toks.append(t)
        lead = ([DOCTYPE] if k % 2 else []) + ([PLAIN_HEAD] if k % 4 < 2 else [])
        docs.append(tuple(enumerate(lead + toks)))
    return docs, used


# Deferred-tail documents. A deferred winner's value can come from inside it (`li:last-child b::text`),
# and what is inside it can take its shape from what is open OUTSIDE it: reconstruction copies a `<b>` into
# the last `<li>`, a table gives a row its `<tbody>` and makes `<tr>`/`<td>` elements at all, and an SVG
# `<title>` or `<desc>` is an integration point whose `<b>` is an element. A winner's source parsed on its
# own loses each of those, so these documents are where a tail read without context differs. Every
# context is crossed with every body, with and without a doctype (quirks mode keeps a `<p>` open across a
# `<table>`), and the text tokens cycle through VALUE_TEXTS so `:contains()` has something to find.
TAIL_CONTEXTS: list[list[str]] = [
    [], ["<table{}>"], ["<p{}>", "<b{}>", "t", "</p>"], ["<p{}>", "<i{}>", "<b{}>", "t", "</p>"],
    ["<svg{}>"], ["<svg{}>", "<desc{}>"], ["<math{}>", "<mi{}>"], ["<form{}>"], ["<a{} href=x>", "t"],
]
TAIL_BODIES: list[list[str]] = [
    ["<ul{}>", "<li{}>", "t", "<li{}>", "<b{}>", "t"],
    ["<tr{}>", "<td{}>", "t", "<td{}>", "<b{}>", "t", "<tr{}>", "<td{}>", "t"],
    ["<td{}>", "t", "<td{}>", "<i{}>", "t"],
    ["<div{}>", "t", "</div>", "<div{}>", "<b{}>", "t", "</div>"],
    ["<title{}>", "<b{}>", "t", "</title>", "<desc{}>", "<b{}>", "t"],
    ["<p{}>", "t", "<p{}>", "<b{}>", "t", "<span{}>", "t"],
    ["<dl{}>", "<dt{}>", "t", "<dd{}>", "<b{}>", "t", "<dt{}>", "t"],
]


def tail_docs() -> list[Doc]:
    """Every TAIL_CONTEXTS x TAIL_BODIES document, without and with a doctype."""
    docs, k = [], 0
    for lead in ([], [DOCTYPE]):
        for ctx in TAIL_CONTEXTS:
            for body in TAIL_BODIES:
                toks = []
                for t in ctx + body:
                    if t == TEXT_TOKEN:
                        t = _decorate(t, "text", VALUE_TEXTS[k % len(VALUE_TEXTS)])
                        k += 1
                    toks.append(t)
                docs.append(tuple(enumerate(lead + toks)))
    return docs


# ---------------------------------------------------------------- structured families
# Shapes a random run of a few tokens almost never assembles, each family built for one kind of rule. A
# family member is a value document (every value column is graded on it), without and with a doctype
# unless it writes its own.
#
# A document that ends before the body: at end of input the insertion mode's "anything else" still runs,
# so the frame is complete (an empty `<body>` after a lone `<title>`, all three after nothing at all).
EOF_FRAMES: list[list[str]] = [
    [], ["<title{}>T</title>"], ["<title{}>"], ["<title{}>", "t"], ["<textarea{}>"], ["<textarea{}>", "t"],
    ["<head{}>"], ["<head{}>", "</head>"], ["<head{}>", "</head>", " "], ["<head{}>", "</head>", "<!--c-->"],
    ["<html{}>"], ["<html{}>", "<head{}>"], ["<html{}>", "<head{}>", "</head>"], ["<html{}>", " "],
    ["<html{}>", "<title{}>T</title>"], ["<html{}>", "<head{}>", "</head>", "<title{}>T</title>"],
    ["<!--c-->"], [" "], ["<meta{}>"], ["<script{}>s</script>"], ["<script{}>"], ["<style{}>"], ["<noscript{}>"],
    ["<template{}>"], ["<template{}>", "t"], ["<head{}>", "</head>", "<template{}>", "t"], ["</head>"], ["</html>"],
    ["<head{}>", "</head>", "</html>"], ["<frameset{}>"], ["<head{}>", "<title{}>"], ["<plaintext{}>"],
]
# A raw-text, RCDATA or script element whose end tag the input ends inside: right after the name the
# `</name` is text, and past it the tag is dropped.
RAW_ENDS: list[list[str]] = [
    [f"<{name}{{}}>", f"t{end}"]
    for name in ("title", "textarea", "style", "xmp", "iframe", "noembed", "noframes", "noscript", "script")
    for end in (f"</{name}", f"</{name.upper()}", f"</{name} ", f"</{name}/", f"</{name} x")
] + [["<script{}>", "<!--t</script"]]
# Comments in every position: each skeleton once per place a comment can go, so one lands in the
# document, the head, "after head" (where `<html>`'s whitespace is held around head content), template
# content written after `</head>`, a table, a select, foreign content, a frameset and after the body.
COMMENT = "<!--c-->"
COMMENT_SKELETONS: list[list[str]] = [
    [DOCTYPE, "<html{}>", "<head{}>", "<title{}>T</title>", "</head>", " ", "<template{}>", "<p{}>", "t", "</template>",
     " ", "<script{}>s</script>", " ", "<style{}>s</style>", " ", "<body{}>", "<p{}>", "t", "</p>", "</body>", " ",
     "</html>", " "],
    ["<title{}>T</title>", " ", "<p{}>", "t", "<table{}>", "<tr{}>", "<td{}>", "t", "</table>", "<select{}>",
     "<option{}>", "t", "</select>", "<svg{}>", "<desc{}>", "t", "</svg>"],
    [DOCTYPE, "<head{}>", "</head>", " ", "<frameset{}>", " ", "<frame{}>", "</frameset>", " ", "</html>", " "],
]
# Formatting elements that reconstruction copies at one offset: the copies and the text that made them
# begin at the same byte, so two matches of one column share an offset; the active formatting list at its
# longest, past the adoption agency's inner-loop limit of three; and the adoption agency's outer loop.
FORMATTING: list[list[str]] = [
    ["<p{}>", "<b{}>", "t", "<i{}>", "t", "</p>", "t"],
    ["<p{}>", "<b{}>", "t", "<i{}>", "t", "</p>", "t", "<b{}>"],
    ["<p{}>", "<a{} href=x>", "<b{}>", "<i{}>", "<u{}>", "t", "</p>", "<p{}>", "t"],
    ["<p{}>", "<b{} class=k>", "<em{}>", "t", "</p>", "<div{}>", "t", "</div>", "t"],
    ["<div{}>", "<b{}>", "<i{}>", "t", "</div>", "<br{}>", "</br>", "t"],
    ["<p{}>", "<b{}>", "<i{}>", "<u{}>", "<s{}>", "<em{}>", "<font{}>", "<code{}>", "<strong{}>", "<small{}>",
     "<big{}>", "<tt{}>", "<strike{}>", "t", "</p>", "t"],
    ["<p{}>", "<b{}>", "<i{}>", "<u{}>", "<s{}>", "<em{}>", "<font{}>", "t", "<div{}>", "t", "</b>", "t", "</p>", "t"],
    ["<b{}>", "<i{}>", "<u{}>", "<s{}>", "<em{}>", "<div{}>", "t", "</b>", "t", "</div>", "t"],
    ["<b{}>", "<div{}>", "<div{}>", "<div{}>", "<div{}>", "<div{}>", "<div{}>", "<div{}>", "<div{}>", "<div{}>",
     "<div{}>", "t", "</b>", "t"],
    ["<a{} href=x>", "<div{}>", "t", "<a{} href=y>", "t", "</a>", "t"],
    ["<table{}>", "<tr{}>", "<td{}>", "<b{}>", "<i{}>", "t", "</td>", "<td{}>", "t", "</table>", "t"],
    ["<p{}>", "<b{}>", "<i{}>", "t", "<applet{}>", "<b{}>", "t", "</applet>", "t", "</p>", "t"],
]
# Noah's Ark: three alike stay on the list and a fourth evicts the earliest, so the copies a later
# paragraph gets show how many were kept. Every spelling of one attribute set is alike; `ARK[7:]` differ.
ARK_FAMILY: list[list[str]] = (
    [["<p{}>"] + [a] * 4 + ["t", "</p>", "t"] for a in ARK]
    + [["<p{}>"] + ["<b id=k class=c>"] * 3 + [a, "t", "</p>", "t"] for a in ARK[1:8]]
    + [["<p{}>", "<b id=k>", "<b id=k>", "<applet{}>", "<b id=k>", "<b id=k>", "t", "</applet>", "<b id=k>", "t",
        "</p>", "t"],
       ["<p{}>", "<a id=k href=x>", "<b id=k>", "<a id=k href=x>", "<b id=k>", "<a id=k href=x>", "<b id=k>",
        "<a id=k href=x>", "t", "</p>", "t"]]
)
# Past Chrome's depth cap, an element opens beside the current node instead of in it. Each run of `DEEP`
# divs is followed by what the cap reorders: an end tag for an element opened beside its parent, the
# adoption agency, reconstruction, a table, a list, a select, foreign content.
DEEP_TAILS: list[list[str]] = [
    ["<div{}>", "<b{}>", "t", "<div{}>", "t", "</b>", "t", "</div>", "</div>", "<p{}>", "t", "</p>"],
    ["<p{}>", "t", "<p{}>", "t", "</div>", "t"],
    ["<b{}>", "<i{}>", "t", "</b>", "t", "<p{}>", "t"],
    ["<table{}>", "<tr{}>", "<td{}>", "t", "</table>", "t"],
    ["<ul{}>", "<li{}>", "t", "<li{}>", "t"],
    ["<select{}>", "<option{}>", "t", "<option{}>", "t"],
    ["<svg{}>", "<desc{}>", "t", "</svg>", "t"],
    ["<a{} href=x>", "<div{}>", "t", "</a>", "t"],
]
# Successive `<html>` and `<body>` tags: each adds the attributes the element lacks, the first tag to name
# one winning, and the rule selectors below match only the attributes of two tags together.
FRAME_MERGES: list[list[str]] = [
    ["<p{}>", "t", "<body class=a>", "<body title=b>"],
    ["<p{}>", "t", "<html class=a>", "<html title=b>"],
    ["<p{}>", "t", "<body class=a>", "<body class=x title=b>"],
    ["<p{}>", "t", "<body class=a>", "<body title=c>"],
    ["<body class=a>", "<p{}>", "t", "<body title=b>"],
    ["<html class=a>", "<p{}>", "t", "<html title=b>"],
    ["<p{}>", "t", "<body class=a>", "<body title=b>", "<body lang=c>"],
]
# Documents a page load could leave: a meta refresh (to another page, to itself, and a frame's
# navigation) while a subresource the probe serves slowly holds the load open. The oracle must grade the
# document it was sent, so each is asked of DOMParser and, under selfcheck, of a page load.
PAGE_LOADS: list[list[str]] = [
    [f'<meta http-equiv=refresh content="{c}">', "<img src=/slow>", "<p{}>", "t"]
    for c in ("0; url=/elsewhere", "0")
] + [["<iframe src=/slow></iframe>", "<p{}>", "t"], ["<noscript{}>", "</noscript>", '<meta http-equiv=refresh content=0>',
                                                      "<img src=/slow>", "<p{}>", "t"]]
# The attribute values a tree-construction rule reads, each spelled literally and with a character
# reference: a rule reads the decoded value, so the two spellings build one tree. The other spellings
# are the edges of each test (case, a trailing space, the parse of `size`).
RULE_VALUES: list[list[str]] = (
    [["<table{}>", f'<input{{}} type="{v}">', "</table>", "t"]
     for v in ("hidden", "hidd&#101;n", "HIDD&#x45;N", "hidden ")]
    + [[f'<input{{}} type="{v}">', "<frameset{}>", "</frameset>"] for v in ("hidden", "hidd&#101;n", "text")]
    + [["<div{}>", f'<template{{}} shadowrootmode="{v}">', "<p{}>", "t", "</template>", "</div>"]
       for v in ("open", "o&#112;en", "CLOSED", "&#99;losed", "none")]
    + [["<math{}>", f'<annotation-xml{{}} encoding="{v}">', "<style{}>s</style>", "<b{}>", "t", "</annotation-xml>",
        "</math>"]
       for v in ("text/html", "text&#47;html", "TEXT/HTML", "application&#47;xhtml+xml", "text/plain")]
    + [[f'<select{{}} size="{v}">', "<button{}>", "<selectedcontent{}>", "</selectedcontent>", "</button>",
        "<option{}>", "t", "</select>"]
       for v in ("2", "&#50;", "2px", "&#32;2", "+2", "-0", "0x2", "4294967295", "4294967296", "")]
)

# The option a `<select>` selects by default, which `<selectedcontent>` copies: the first one that is not
# disabled, where an option is disabled by its own attribute or by the nearest `<optgroup>` around it.
_SELECT = ["<select{}>", "<button{}>", "<selectedcontent{}>", "</selectedcontent>", "</button>"]
SELECT_OPTIONS: list[list[str]] = [
    _SELECT + body + ["<option{}>", "u", "</select>"]
    for body in (
        ["<optgroup{} disabled>", "<option{}>", "t", "</optgroup>"],
        ["<optgroup{} disabled>", "<div{}>", "<option{}>", "t", "</div>", "</optgroup>"],
        ["<optgroup{} disabled>", "<optgroup{}>", "<option{}>", "t", "</optgroup>", "</optgroup>"],
        ["<optgroup{} disabled>", "<option{} selected>", "t", "</optgroup>"],
        ["<optgroup{}>", "<option{} disabled>", "t", "</optgroup>"],
    )
]


def structured_docs() -> list[Doc]:
    """Every structured family member, as a document."""
    seqs = [lead + f for f in EOF_FRAMES + FORMATTING + RAW_ENDS + FRAME_MERGES + RULE_VALUES + SELECT_OPTIONS + PAGE_LOADS
            for lead in ([], [DOCTYPE])]
    seqs += [s[:k] + [COMMENT] + s[k:] for s in COMMENT_SKELETONS for k in range(len(s) + 1)]
    seqs += [[DOCTYPE] + f for f in ARK_FAMILY]
    seqs += [[DOCTYPE, deep] + tail for deep in DEEP for tail in DEEP_TAILS]
    return [tuple(enumerate(s)) for s in seqs]



def id_value_selectors(ids: list[str], decorated: bool) -> list[str]:
    """Per-id value columns: own and subtree text, outer HTML, and (on value documents) attributes."""
    out = []
    for i in ids:
        out += [f'[id="{i}"]::text', f'[id="{i}"] ::text', f'//*[@id="{i}"]//text()']
        if decorated:
            out += [f'[id="{i}"]', f'[id="{i}"]::attr(title)', f'[id="{i}"]::attr(class)',
                    f'//*[@id="{i}"]/@title', f'//*[@id="{i}"]/@class']
    return out


# The basket selectors Chrome answers nowhere on these documents, as recorded. Each can only catch an
# over-match: most need markup the alphabet does not emit (a `<thead>` with a `<th>`, two `<li>` siblings,
# the `alpha`/`beta` words), and the table-section and optgroup `::text` rows are empty by
# the tree rules. `diff_browser --gate` fails on a selector that joins them, which is a column that has
# stopped grading anything.
BASKET_ORACLE_EMPTY: frozenset[str] = frozenset({
    '#\\69 1::attr(id)', 'th::text', 'ol > li::text', 'ruby > rt::text', 'ruby > rp::text',
    'dl > dt + dt::text', 'dl > dd + dd::text', 'table > thead::text', 'table > tbody::text',
    'table > tfoot::text', 'table > caption::text', 'thead > tr::text', 'tbody > tr::text', 'thead th::text',
    'table > colgroup::attr(id)', 'table > thead th::text', 'table > tfoot td::text',
    'colgroup + thead th::text', 'table > colgroup > col::attr(class)', 'select > optgroup::text',
    'optgroup + optgroup::text', 'thead + tbody::text', 'caption + thead::text', 'table > tr::text',
    'tr > th::text', 'dt + dd::text', 'option + option::text', 'h1 ~ p::text', 'ul li ~ li::text',
    'span + span::text', 'section ~ *::text', 'li + *::attr(class)', 'td > *::attr(data-k)',
    'section ~ * ::text', 'input[value|="v"]::text', 'li:not([id])::text', '//div[last()]//span/text()',
    'div:has(a) p::text', 'div:has(p) span::text', 'li:has(a) span::text',
    "//div[contains(.,'alpha')]//a/@href", "//li[contains(.,'beta')]//span/text()",
    'div:contains("alpha") a::attr(href)', 'li:contains("beta") span::text',
    'dt:contains("alpha") + dd::text', 'dt:contains("alpha") ~ dd::text', 'div:contains(alpha)::attr(class)',
    'div.shared:contains("alpha")::attr(class)', 'dt:contains("alpha") + ::text', 'div:has(> p)::attr(class)',
    '//td[@class]/text()', '//dt/following-sibling::dd/text()',
    '//dt[contains(.,"alpha")]/following-sibling::dd/text()', 'p + p::text',
})


# Selectors aimed at the value rules, graded on every value document. Each must be answered non-empty
# by the oracle somewhere in a run (`Report.rule_never_answered`), or it cannot catch a lost value.
RULE_SELECTORS: list[str] = [
    # case-insensitive attribute values (CSS only), and an attribute HTML does not list. The same tests
    # reach SVG elements, which are matched case-sensitively, so folding there would be an over-match.
    '[lang="en"]::attr(id)', '[lang="EN"]::attr(id)', '[lang^="e"]::attr(id)', '[lang|="en"]::attr(id)',
    '[dir="rtl"]::attr(id)', '[align="center"]::attr(id)', '[rel~="nofollow"]::attr(id)',
    '[type="submit"]::attr(id)', '[target="_blank"]::attr(id)', '[data-case="Mixed"]::attr(id)',
    'svg[lang]::attr(lang)', '//*[@lang="en"]/@id', '//*[@dir="RTL"]/@id',
    # class lists, and class/id matching that quirks mode folds
    '.a::attr(id)', '.b::attr(id)', '.A::attr(id)', '[class~="b"]::attr(id)', '[class="a b"]::attr(id)',
    '.shared::attr(id)', '//*[contains(@class, "b")]/@id',
    # decoded values, NUL and control characters, in CSS literals and through the bridge's literal map
    '*::attr(disabled)', '//*/@defer', '[checked]::attr(checked)', '*::attr(hidden)', '*::attr(nonce)',
    '[nonce="n1"]::attr(id)',
    '[title]::attr(title)', '[title="x&y"]::attr(id)', '[title="a\\FFFD b"]::attr(id)',
    '[title="Up"]::attr(id)', '[title="1"]::attr(id)', '[data-u]::attr(data-u)', '[data-e=""]::attr(id)',
    '//*/@title', '//*/@class', '//*[@title="a\x01b\x0bc"]/@id', '//*[@data-u]/@data-u',
    # text
    '*::text', '//body//text()',
    # deferred winners whose value is inside them (TAIL_CONTEXTS is where their context matters)
    'li:last-child ::text', 'li:last-child b::text', 'li:nth-last-child(2) ::text', 'li:only-child ::text',
    'tr:last-child td::text', 'tr:last-child ::attr(id)', 'td:last-child ::text', 'td:last-of-type b::text',
    'tbody:last-child td::attr(id)', 'p:last-of-type ::text', 'dt:last-of-type ::text', ':last-child ::attr(id)',
    'tr:has(td) ::text', 'tr:has(b) td::attr(id)', 'li:has(b) ::text', 'p:has(b) b::text',
    'div:has(b) ::attr(id)', 'title:has(b) ::text', 'desc:has(b) b::text', 'dd:has(> b) ::text',
    'div:contains("a") b::text', 'li:contains("a") ::text', 'td:contains("a") ::attr(id)',
    '//li[last()]//text()', '//tr[last()]//td/text()', '//*[contains(., "a")]//b/text()',
    # attribute names: `::attr()` reads as `getAttribute()` does (the name lowercased on an HTML element,
    # the DOM name exactly on an SVG or MathML one: `viewBox`, `definitionURL`), XPath's `@` compares the
    # DOM name exactly, a CSS attribute selector case-insensitively and never a namespaced one, and an
    # XPath name-test never a camel-cased SVG element
    '[viewbox]::attr(id)', '*::attr(viewBox)', '*::attr(viewbox)', '*::attr(textLength)', '[textlength]::attr(id)',
    '*::attr(definitionURL)', '[definitionurl]::attr(id)', '[xmlns]::attr(id)', '*::attr(xmlns)',
    '*::attr(ID)', '*::attr(Title)', '*::attr(VIEWBOX)', '*::attr(DEFINITIONURL)',
    '[fill]::attr(id)', '//*/@viewbox', '//*[@textlength]/@id', '//*[@fill]/@id', '//clippath/@id',
    # the positions the frame gives what is in it, which an empty `<body>` the parser adds at end of input
    # changes, and the parents it gives
    '*:last-child::attr(id)', '*:only-child::attr(id)', 'html > :last-child::attr(id)', ':has(> body)::attr(id)',
    ':has(> head)::attr(id)',
    # the attributes of two `<html>` or `<body>` tags together (FRAME_MERGES)
    'body.a[title="b"] p::text', 'html.a[title="b"] p::text', 'body.a[lang]::attr(title)', 'body.a', 'html.a',
    # two matches of one column at one offset: a formatting element's copy, and the copy inside it
    'b, i', 'b:has(i)', '*:has(> i)::attr(id)', 'b:last-child::attr(id)', 'b i::attr(id)',
]
# Outer HTML of the frame, which browser mode refuses (a synthesised element has no source of its own) and
# lxml mode answers; listed so that an engine that starts answering them is graded on them.
FRAME_COLUMNS: list[str] = ["body", "head", "html", "head + body", "html > *:last-child"]

# Grouped columns, which see what no flat column can: an element with no attribute and no text still makes
# a row. A column is written `grouped:<container> -> <sub> | <sub>`, so it travels with the flat ones.
GROUPED_PREFIX = "grouped:"


def grouped_column(container: str, subs: list[str]) -> str:
    return f"{GROUPED_PREFIX}{container} -> {' | '.join(subs)}"


def grouped_spec(col: str) -> Optional[tuple[str, list[str]]]:
    """`(container, subs)` for a grouped column, else `None`."""
    if not col.startswith(GROUPED_PREFIX):
        return None
    container, subs = col[len(GROUPED_PREFIX):].split(" -> ", 1)
    return container, subs.split(" | ")


GROUPED_COLUMNS: list[str] = [grouped_column(c, s) for c, s in [
    ("html", ["::attr(id)"]), ("head", ["::text"]), ("body", ["::text"]), ("head > *", ["::attr(id)"]),
    ("b", ["::attr(id)", "::text"]),
]]


def value_selectors(d: Doc, decorated: bool, basket: list[str]) -> list[str]:
    """A document's value columns. One at the depth cap leaves out the basket and the frame's outer HTML:
    their bare-element columns return hundreds of nested `<div>`s, which no structural comparison parses
    past libxml2's nesting limit, and the per-id and rule columns are what see the elements after them."""
    sels = id_value_selectors(ids_of(d), decorated)
    if not decorated:
        return sels
    if any(t in DEEP for _i, t in d):
        return sels + RULE_SELECTORS + GROUPED_COLUMNS
    return sels + RULE_SELECTORS + FRAME_COLUMNS + GROUPED_COLUMNS + basket


@dataclass
class Documents:
    """Every document of one run, and how often each of its tokens was emitted."""

    seq: list[Doc]
    random: list[Doc]
    value: list[Doc]               # the decorated documents: value, deferred-tail and structured
    tail: int                      # how many of `value` are deferred-tail documents
    structured: int                # ... and how many are structured
    used: Counter                  # `random`'s ALPHABET tokens
    value_used: Counter            # `value`'s decorations and structure tokens
    missing_tokens: list[str]
    missing_value_tokens: list[str]

    @functools.cached_property
    def all(self) -> list[Doc]:
        return self.seq + self.random + self.value

    @functools.cached_property
    def decorated(self) -> set[str]:
        return {render(d) for d in self.value}


def generate(seq_depth: int, random_n: int, values_n: int, seed: int) -> Documents:
    """The run's documents. A family that was not asked for is empty, and so is its missing-token list."""
    rdocs, used = random_docs(random_n, seed)
    vdocs, vused = value_docs(values_n, seed)
    tdocs = tail_docs() if values_n else []
    sdocs = structured_docs() if values_n else []
    return Documents(seq_docs(seq_depth), rdocs, vdocs + tdocs + sdocs, len(tdocs), len(sdocs), used, vused,
                     [t for t in ALPHABET if random_n and not used[t]],
                     [t for t in VALUE_TOKENS if values_n and not vused[t]])


# Import-time checks. A start tag with no id slot is an element the fingerprint cannot name, and a token
# outside ALPHABET is one no attribution rule reads.
def _check_tokens() -> None:
    for t in ALPHABET + seq_sweep.ALPHABET + VALUE_STRUCTURE:
        assert not _start_tag(t) or "{}" in t or t in ARK, f"{t!r} is a start tag with no id slot"
    families = TAIL_CONTEXTS + TAIL_BODIES + EOF_FRAMES + COMMENT_SKELETONS + FORMATTING + ARK_FAMILY + DEEP_TAILS
    for t in {t for seq in families for t in seq}:
        assert t in ALPHABET or t in (TEXT_TOKEN, DOCTYPE), f"{t!r} is not an ALPHABET token"


_check_tokens()
