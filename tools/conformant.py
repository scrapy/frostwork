"""
Content-model-aware HTML generator: emits only *tree-conformant* HTML — elements nest where the
HTML content model allows, table cells only inside tables, options only inside selects, `<a>` holds
only phrasing, no head-only elements in the body — with optional end tags omitted **only where HTML
permits** (`li`/`dt`/`dd`/`option`/`td`/`tr`/`p`).

The point: on such input the engine and lxml apply the *same* rules (Frostwork ported lxml's implied
close), so they must agree byte-for-byte. That makes the differential a clean gate — any DIVERGE on
this generator is a true engine bug, not tree-construction leakage (unlike the unconstrained grammar
in diff_parity, which emits stray `<td>`/`<title>`/misnested formatting that libxml2 reshapes).

Deterministic given an rng. `generate(rng) -> bytes`; `BASKET` is a supported-selector basket.
"""
from __future__ import annotations

TEXTBITS = ["alpha", " beta ", "g&amp;h", "x&lt;y", "café", "  ", "\n ", "1 2 3", "a&nbsp;b", ""]
PHRASING = ["span", "b", "em", "strong", "i", "small", "label"]
VOID = ["br", "img", "input"]  # libxml2-void only; wbr/embed/source/track are non-void in libxml2


def _attr(rng, n):
    r = rng.random()
    if r < 0.40:
        return ""
    if r < 0.55:
        return f' id="i{n}"'
    if r < 0.70:
        return f' class="c{n}"'
    if r < 0.80:
        return f' class="c{n} shared"'
    if r < 0.90:
        return f' data-k="v{n}"'
    if r < 0.95:
        # CR / CRLF in a value: HTML normalizes to \n before entity decode (T4 gate coverage)
        return f' title="a{n}\r\nb\rc"'
    return ' title="t"'


def _next(ctr):
    ctr[0] += 1
    return ctr[0]


def _text(rng):
    return rng.choice(TEXTBITS)


def _void(rng, ctr):
    n = _next(ctr)
    t = rng.choice(VOID)
    extra = f' src="s{n}.png"' if t == "img" else (' value="v"' if t == "input" else "")
    return f"<{t}{_attr(rng, n)}{extra}>"


def gen_phrasing(rng, depth, in_a, ctr):
    """Phrasing content: text, void phrasing, nested inline. `<a>` never nests inside `<a>`, and
    phrasing never contains block — so no adoption-agency reshaping."""
    out = []
    for _ in range(rng.randint(0, 3)):
        r = rng.random()
        if depth <= 0 or r < 0.45:
            out.append(_text(rng))
        elif r < 0.60:
            out.append(_void(rng, ctr))
        elif r < 0.72 and not in_a:
            n = _next(ctr)
            out.append(f'<a href="/p{n}"{_attr(rng, n)}>{gen_phrasing(rng, depth - 1, True, ctr)}</a>')
        else:
            t = rng.choice(PHRASING)
            n = _next(ctr)
            out.append(f"<{t}{_attr(rng, n)}>{gen_phrasing(rng, depth - 1, in_a, ctr)}</{t}>")
    return "".join(out)


def _p(rng, depth, ctr):
    n = _next(ctr)
    body = gen_phrasing(rng, depth - 1, False, ctr)
    # <p> end tag is optional; omitting it is conformant (a following block/p or parent close ends it)
    return f"<p{_attr(rng, n)}>{body}" + ("" if rng.random() < 0.5 else "</p>")


def _list(rng, depth, ctr):
    tag = rng.choice(["ul", "ol"])
    omit = rng.random() < 0.5
    items = []
    for _ in range(rng.randint(1, 4)):
        n = _next(ctr)
        inner = gen_flow(rng, depth - 1, ctr) if rng.random() < 0.4 else gen_phrasing(rng, depth - 1, False, ctr)
        items.append(f"<li{_attr(rng, n)}>{inner}" + ("" if omit else "</li>"))
    return f"<{tag}>" + "".join(items) + f"</{tag}>"


def _dl(rng, depth, ctr):
    omit = rng.random() < 0.5
    parts = []
    for _ in range(rng.randint(1, 3)):
        # Emit RUNS of the same term/definition tag, not just a strict dt->dd alternation. libxml2
        # auto-closes only the CROSS pair and nests a same-tag repeat, so `<dt>a<dt>b` is the shape that
        # actually discriminates the rule — with alternation alone the whole family looks correct.
        for tag in ("dt", "dd"):
            for _ in range(rng.randint(1, 2) if omit else 1):
                n = _next(ctr)
                body = gen_phrasing(rng, depth - 1, False, ctr)
                parts.append(f"<{tag}{_attr(rng, n)}>{body}" + ("" if omit else f"</{tag}>"))
    return "<dl>" + "".join(parts) + "</dl>"


def _ruby(rng, depth, ctr):
    """`ruby`/`rt`/`rp` — libxml2 never auto-closes annotations (they nest), unlike every other
    optional-end-tag family. Previously ungenerated, so the rule had zero differential coverage."""
    omit = rng.random() < 0.5
    parts = []
    for _ in range(rng.randint(1, 3)):
        n = _next(ctr)
        parts.append(_text(rng))
        for tag in rng.choice([("rt",), ("rp",), ("rt", "rp"), ("rt", "rt"), ("rp", "rt")]):
            parts.append(f"<{tag}{_attr(rng, n)}>{_text(rng)}" + ("" if omit else f"</{tag}>"))
    return "<ruby>" + "".join(parts) + "</ruby>"


def _table(rng, depth, ctr):
    omit = rng.random() < 0.5
    use_tbody = rng.random() < 0.5
    rows = []
    for _ in range(rng.randint(1, 3)):
        cells = []
        for _ in range(rng.randint(1, 3)):
            n = _next(ctr)
            tag = rng.choice(["td", "th"])
            inner = gen_phrasing(rng, depth - 1, False, ctr)
            cells.append(f"<{tag}{_attr(rng, n)}>{inner}" + ("" if omit else f"</{tag}>"))
        rows.append("<tr>" + "".join(cells) + ("" if omit else "</tr>"))
    inner = "".join(rows)
    if use_tbody:
        inner = "<tbody>" + inner + ("" if omit else "</tbody>")
    # Section/caption RUNS with omitted end tags. `<thead>…<tbody>` after an unclosed row or cell is
    # everyday generated markup, and the three sections are NOT interchangeable in libxml2 (`<tbody>`/
    # `<tfoot>` close an open row/cell, `<thead>` nests). None of this was generated before.
    if rng.random() < 0.4:
        head_cells = "".join(f"<th>{_text(rng)}" + ("" if omit else "</th>")
                             for _ in range(rng.randint(1, 2)))
        head = "<thead><tr>" + head_cells + ("" if omit else "</tr></thead>")
        inner = head + inner
    if rng.random() < 0.3:
        inner += "<tfoot><tr><td>" + _text(rng) + ("" if omit else "</td></tr></tfoot>")
    if rng.random() < 0.3:
        inner = f"<caption>{_text(rng)}" + ("" if omit else "</caption>") + inner
    # `<colgroup><col>…` with an omitted `</colgroup>` is ordinary table markup and had NO rule: the
    # sections ended up nested inside the colgroup, so a child-anchored selector lost the cells.
    if rng.random() < 0.35:
        cols = "".join(f"<col{_attr(rng, _next(ctr))}>" for _ in range(rng.randint(1, 3)))
        inner = f"<colgroup>{cols}" + ("" if omit else "</colgroup>") + inner
    return "<table>" + inner + "</table>"


def _select(rng, depth, ctr):
    omit = rng.random() < 0.5
    opts = []
    for _ in range(rng.randint(1, 4)):
        n = _next(ctr)
        # `<optgroup>` RUNS with an omitted end tag are ordinary markup on real pages, and libxml2
        # NESTS them (it does not auto-close a same-tag repeat) — previously ungenerated entirely.
        # Keep the "did I open one?" answer in a local: sniffing the output for `"optgroup"` also matches
        # `"</optgroup>"`, which appended stray closers into a CONFORMANT page.
        opened = rng.random() < 0.35
        if opened:
            opts.append(f'<optgroup label="g{n}"{_attr(rng, n)}>')
        opts.append(f"<option{_attr(rng, n)}>{_text(rng)}" + ("" if omit else "</option>"))
        if opened and not omit:
            opts.append("</optgroup>")
    return "<select>" + "".join(opts) + "</select>"


def _rawtext(rng, ctr):
    n = _next(ctr)
    t = rng.choice(["script", "style", "textarea"])
    # content chosen so a bare "<" or "&" can't create a global desync
    content = rng.choice(["a<b && c>d", "x=1;", "café &amp; more", ".c{color:red}", "hi"])
    return f"<{t}{_attr(rng, n)}>{content}</{t}>"


def gen_flow(rng, depth, ctr):
    """Flow content: blocks + phrasing, all validly nested."""
    out = []
    for _ in range(rng.randint(1, 4)):
        r = rng.random()
        if depth <= 0 or r < 0.25:
            out.append(gen_phrasing(rng, depth - 1, False, ctr))
        elif r < 0.42:
            out.append(_p(rng, depth, ctr))
        elif r < 0.57:
            t = rng.choice(["div", "section", "article", "header", "footer"])
            n = _next(ctr)
            out.append(f"<{t}{_attr(rng, n)}>{gen_flow(rng, depth - 1, ctr)}</{t}>")
        elif r < 0.68:
            out.append(_list(rng, depth, ctr))
        elif r < 0.77:
            out.append(_table(rng, depth, ctr))
        elif r < 0.85:
            out.append(_dl(rng, depth, ctr))
        elif r < 0.91:
            out.append(_select(rng, depth, ctr))
        elif r < 0.95:
            out.append(_ruby(rng, depth, ctr))
        else:
            out.append(_rawtext(rng, ctr))
    return "".join(out)


def generate(rng, depth=5):
    ctr = [0]
    title = _text(rng).replace("<", "").replace("&", "") or "t"
    body = gen_flow(rng, depth, ctr)
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title></head><body>{body}</body></html>"
    ).encode("utf-8")


# Supported-subset selector basket, spanning compounds x {descendant, child} x {text, attr}.
BASKET = [
    "div ::text", "p::text", "p ::text", "span::text", "li::text", "td::text", "th::text",
    "a::text", "a ::text", "label::text", "option::text", "dd::text", "dt::text",
    "ul > li::text", "ol > li::text", "div > p::text", "dl > dt::text", "dl > dd::text",
    # ruby: `> rt` / `> rp` are what catch an over-eager annotation auto-close (they nest in libxml2)
    "ruby > rt::text", "ruby > rp::text", "ruby ::text", "rt::text", "rp::text", "rt ::text",
    "dl > dt + dt::text", "dl > dd + dd::text", "dt dt::text", "dl dd ::text",
    # table sections + optgroup: arms that had NO differential coverage until these were added
    "table > thead::text", "table > tbody::text", "table > tfoot::text", "table > caption::text",
    "thead > tr::text", "tbody > tr::text", "thead th::text", "tbody td::text",
    # child-anchored past a colgroup: these are what a nested-colgroup bug drops
    "table > colgroup::attr(id)", "table > thead th::text", "table > tbody td::text",
    "table > tfoot td::text", "colgroup + thead th::text", "table > colgroup > col::attr(class)",
    "select > optgroup::text", "optgroup > option::text", "optgroup + optgroup::text",
    "thead + tbody::text", "caption + thead::text",
    "table tr > td::text", "select > option::text", "table > tr::text", "tr > th::text",
    "a::attr(href)", "img::attr(src)", "input::attr(value)", "[data-k]::attr(data-k)",
    "[title]::attr(title)",  # exercises CR/CRLF-in-attribute normalization (T4)
    ".shared::text", ".shared ::text", "section p::text", "div div ::text",
    # sibling combinators (+ / ~), incl. chains and a descendant base
    "li + li::text", "li ~ li::text", "dt + dd::text", "td + td::text", "option + option::text",
    "p + p::text", "h1 ~ p::text", "ul li ~ li::text", "tr > td + td::text",
    "span + span::text", "a + a::attr(href)",
    # universal `*` after an explicit combinator: self-scoped, NOT collapsed to subtree (T1 regression)
    "div > *::text", "li + *::text", "section ~ *::text", "ul > *::text", "p + *::text",
    "div > *::attr(id)", "li + *::attr(class)", "td > *::attr(data-k)",
    # detached terminal (`* ::text` — whitespace before the pseudo): `*` is a real subject compound,
    # terminal subtree-scoped; `div * ::text` excludes div's own direct text (T1 follow-up regression)
    "div > * ::text", "li + * ::text", "div * ::text", "p * ::text", "section ~ * ::text",
    "div > * ::attr(id)", "li * ::attr(class)",
    # attribute operators
    'a[href^="/p"]::attr(href)', 'img[src$=".png"]::attr(src)', '[class~="shared"]::text',
    '[data-k^="v"]::text', 'a[href*="p"]::attr(href)', '[class*="c"]::text', 'input[value|="v"]::text',
    # :not()
    "a:not([data-k])::attr(href)", "span:not(.shared)::text", "li:not([id])::text",
    "[class]:not(.shared)::text", "p:not(.shared):not([data-k])::text", "td:not(a.x)::text",
    # forward/reverse positions (subject-attached terminals)
    "li:first-child::text", "li:nth-child(2)::text", "li:nth-of-type(2)::text",
    "li:last-child::text", "li:last-of-type::text", "li:nth-last-of-type(2)::text",
    # reverse with a SUBTREE terminal — values recovered by re-scanning the winner's span, so these
    # exercise the re-scan path (incl. nested winners, which must de-duplicate)
    "li:last-child ::text", "li:nth-last-child(2) ::text", "li:only-child ::text",
    "div:last-child ::text", "p:last-of-type ::text", "td:last-child ::text",
    "li:last-child ::attr(href)", "div:last-child ::attr(id)",
    "//li[last()]//text()", "//li[last()-1]//text()", "//div[last()]//text()",
    # deferred predicate on an ANCESTOR compound — value from a descendant, recovered by re-scanning the
    # winner's span with the selector tail (reverse, `:has()` and text-predicate all share that path)
    "li:last-child a::attr(href)", "li:last-child span::text", "div:last-child p::text",
    "li:only-child a::text", "//li[last()]//a/@href", "//div[last()]//span/text()",
    "div:has(a) ::text", "div:has(a) ::attr(href)", "div:has(span) ::text",
    "div:has(a) p::text", "div:has(a) a::attr(href)", "div:has(p) span::text",
    "li:has(a) span::text", "//div[contains(.,'alpha')]//text()",
    "//div[contains(.,'alpha')]//a/@href", "//li[contains(.,'beta')]//span/text()",
    # CSS `:contains()` — cssselect lowers it to `contains(., "v")`, so it shares every value location the
    # XPath text predicate above has, and each one is listed rather than assumed: own, subtree, descendant,
    # and the following-sibling (label->value) shape. Both argument tokens cssselect accepts are here too.
    'p:contains("alpha")::text', 'div:contains("alpha") ::text',
    'div:contains("alpha") a::attr(href)', 'li:contains("beta") span::text',
    'dt:contains("alpha") + dd::text', 'dt:contains("alpha") ~ dd::text',
    'div:contains(alpha)::attr(class)', "span:contains('beta')::text",
    'div.shared:contains("alpha")::attr(class)', 'td:contains("beta")::text',
    # a value terminal with no subject compound after an EXPLICIT combinator — the implicit universal.
    # parsel answers these identically to the `*` spelling, and the corpus writes the label->value pattern
    # this way (`:contains('Price')+::text`), so both spellings are gated side by side.
    "dt + ::text", "dt ~ ::text", "dl > ::text", "li + ::text", "td + ::attr(id)",
    "dt + *::text", "dt ~ *::text", "dl > *::text",
    ':contains("alpha") + ::text', 'dt:contains("alpha") + ::text',
    # deferred :has() and matches-any pseudos in cssselect-oracle-compatible shapes
    "div:has(span)::attr(id)", "div:has(> p)::attr(class)",
    "*:is(p, span)::text", "*:where(dt, dd)::text",
    # comma groups (same-terminal): members merge in document order, per-column dedup
    "h1::text, h2::text, h3::text", "dt::text, dd::text", ".shared::text, .c1::text",
    "td::text, th::text", "li::text, option::text", "ul > li::text, ol > li::text",
    # bare-element / outer-HTML (raw-source; validated by re-parse equivalence, not byte-equality)
    "div", "span", "a", "li", "td", "p", "ul > li", ".shared", "table", "option", "dt",
    "h1, h2", "div a", "section",
    # downward XPath (compiled to the same matcher)
    "//a/@href", "//img/@src", "//li/text()", "//td/text()", "//div//text()", "//p/text()",
    "//dt/text()", "//option/text()", '//a[contains(@href,"/p")]/@href', "/html/body//a/@href",
    '//*[@data-k]/@data-k', "//ul/li", "//span", "//li[@id]/text()", "//select/option/text()",
    '//td[@class]/text()',
    # widened XPath: positions, union/or, scalar normalize-space, non-downward lowering, text predicates
    "//li[2]/text()", "//li[last()]/text()", "//dt/text() | //dd/text()",
    "//a[@href or @title]/@href", "normalize-space(//p)",
    "//dt/following-sibling::dd/text()", "//a/ancestor::div/@id", "//a/parent::span/text()",
    '//p[.="alpha"]/text()', '//p[contains(.,"alpha")]/text()',
    '//dt[contains(.,"alpha")]/following-sibling::dd/text()',
]
