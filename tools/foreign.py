"""
Foreign-content generator: pages carrying `<svg>`, `<math>`, and `<template>` subtrees interleaved with
ordinary HTML. libxml2's HTML parser (Frostwork's oracle) is pre-HTML5 — it does NOT implement HTML5
foreign-content rules, so it treats svg/math/template and their children as ORDINARY elements. The one
real risk for a treeless engine is XML-style self-closing (`<rect/>`, `<circle/>`, `<path/>`) on
otherwise-unknown, non-void elements, plus rawtext elements (`<title>`/`<style>`/`<script>`) appearing
inside foreign subtrees. This generator exercises exactly those, so the differential proves parity on a
species neither conformant.py nor families.py emit.

Deterministic given an rng. `generate(rng) -> bytes`; `SELECTORS` is a foreign-aware selector basket.
"""
from __future__ import annotations

# SVG/MathML elements that are commonly written self-closing in the wild (XML habit).
SVG_LEAF = ["rect", "circle", "path", "line", "ellipse", "polygon", "use", "stop"]
SVG_CONTAINER = ["g", "defs", "symbol", "marker", "foreignObject", "svg"]
MATH_LEAF = ["mi", "mo", "mn"]


def _selfclose(rng, tag, n):
    attr = rng.choice(["", f' id="s{n}"', f' class="fc{n}"', f' x="{n}"', f' data-k="v{n}"'])
    slash = "/" if rng.random() < 0.7 else ""  # mix `<rect/>` and `<rect>` (non-void, stays open in HTML)
    return f"<{tag}{attr}{slash}>"


def _svg(rng, depth, ctr):
    ctr[0] += 1
    n = ctr[0]
    kids = []
    for _ in range(rng.randint(0, 3)):
        r = rng.random()
        if r < 0.45 or depth <= 0:
            kids.append(_selfclose(rng, rng.choice(SVG_LEAF), _bump(ctr)))
        elif r < 0.6:
            # rawtext/rcdata inside SVG: libxml2 still treats these as (r)rawtext regardless of context
            kids.append(f"<title>t{n}</title>")
        elif r < 0.72:
            kids.append(f'<text class="lbl">x{n}</text>')
        elif r < 0.85 and depth > 0:
            inner = _svg(rng, depth - 1, ctr)
            kids.append(inner)
        else:  # foreignObject drops back into ordinary HTML
            kids.append(f'<foreignObject><div class="fo">d{n}</div></foreignObject>')
    xmlns = ' xmlns="http://www.w3.org/2000/svg"' if rng.random() < 0.5 else ""
    return f'<svg{xmlns} class="chart c{n}">' + "".join(kids) + "</svg>"


def _math(rng, ctr):
    ctr[0] += 1
    n = ctr[0]
    def _cell():
        t = rng.choice(MATH_LEAF)
        return f"<{t}>{rng.choice('xyz0123')}</{t}>"
    body = "".join(_cell() for _ in range(rng.randint(1, 3)))
    return f'<math class="m{n}"><mrow>{body}</mrow></math>'


def _template(rng, depth, ctr):
    ctr[0] += 1
    n = ctr[0]
    # <template> is an ordinary element to libxml2; its content is visible in the tree (unlike HTML5).
    inner = rng.choice([
        f'<p class="tpl">p{n}</p>',
        f'<tr><td class="cell">c{n}</td></tr>',   # table content w/o table wrapper
        f'<li class="row">r{n}</li>',
        f'<a href="/t{n}">link{n}</a>',
    ])
    return f'<template id="tpl{n}">{inner}</template>'


def _bump(ctr):
    ctr[0] += 1
    return ctr[0]


def _ordinary(rng, ctr):
    ctr[0] += 1
    n = ctr[0]
    return rng.choice([
        f'<p class="c{n}">p{n}</p>',
        f'<div class="box"><span>s{n}</span></div>',
        f'<a href="/a{n}" class="lnk">a{n}</a>',
        f'<ul><li>li{n}</li></ul>',
    ])


def generate(rng, depth=3):
    ctr = [0]
    body = []
    for _ in range(rng.randint(3, 8)):
        r = rng.random()
        if r < 0.40:
            body.append(_svg(rng, depth, ctr))
        elif r < 0.55:
            body.append(_math(rng, ctr))
        elif r < 0.70:
            body.append(_template(rng, depth, ctr))
        else:
            body.append(_ordinary(rng, ctr))
    return (f"<!DOCTYPE html><html><head><title>f</title></head><body>{''.join(body)}</body></html>"
            ).encode("utf-8")


# Foreign-aware selectors: reach into svg/math/template subtrees and mix with ordinary queries.
SELECTORS = [
    "svg", "svg rect", "svg > g", "circle", "path", "rect", "g rect", "use",
    "svg .lbl::text", "text::text", "svg title::text", "foreignObject .fo::text",
    "math mi::text", "math .m1::text", "mrow mo::text",
    "template .tpl::text", "template td::text", "template .row::text", "template a::attr(href)",
    ".chart", ".box span::text", "a.lnk::attr(href)", "p.c1::text",
    "svg[class]::attr(class)", "[data-k]::attr(data-k)", "circle::attr(id)",
    # XPath into foreign subtrees
    "//svg//rect", "//svg/@class", "//math//mi/text()", "//template//a/@href",
    "//foreignObject//div/text()", "//g/rect/@x",
]
