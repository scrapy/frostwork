"""
Tagged optional-end-tag construct generators for the differential harness (tools/diff_lxml.py).
Each family builds a page exercising a tree-construction construct in a `.probe` region, with
selectors sensitive to whether the construct nests or auto-closes, bucketed:
  SHOULD  = an implied-end-tag reshape should make it match lxml
  SKIP    = accepted divergence (foster-parenting / adoption agency)
  CONTROL = well-formed; must always match

Pure generators — no engine/library dependency (the harness diffs the engine vs lxml itself).
"""

_WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india"]


def _w(rng, n=1):
    return " ".join(rng.choice(_WORDS) for _ in range(n))


def c_li(rng):  # <li> with omitted </li>
    tag = rng.choice(["ul", "ol"])
    items = [_w(rng) for _ in range(rng.randint(2, 4))]
    html = f"<{tag}>" + "".join(f"<li>{t}" for t in items) + f"</{tag}>"
    return html, [(f".probe {tag} > li::text", "child"),
                  (".probe li + li::text", "sibling"),
                  (f".probe {tag} li::text", "descendant")]


def c_p(rng):  # <p> auto-closed by a following block-level start tag
    block = rng.choice(["div", "h2", "ul", "section"])
    inner = f"<{block}>{_w(rng)}</{block}>" if block in ("div", "h2", "section") else f"<ul><li>{_w(rng)}</li></ul>"
    html = f"<p>{_w(rng, 2)}{inner}<p>{_w(rng)}"
    return html, [(".probe > p::text", "child"),
                  (".probe p + p::text", "sibling"),
                  (".probe p::text", "descendant")]


def c_td(rng):  # <td>/<tr> with omitted end tags
    rows = []
    for _ in range(rng.randint(2, 3)):
        cells = "".join(f"<td>{_w(rng)}" for _ in range(rng.randint(2, 3)))
        rows.append(f"<tr>{cells}")
    html = "<table>" + "".join(rows) + "</table>"
    return html, [(".probe tr > td::text", "child"),
                  (".probe td + td::text", "sibling"),
                  (".probe td::text", "descendant")]


def c_dl(rng):  # <dt>/<dd> with omitted end tags — incl. same-tag RUNS (libxml2 nests those)
    parts = []
    for _ in range(rng.randint(1, 3)):
        for tag in ("dt", "dd"):
            parts += [f"<{tag}>{_w(rng)}" for _ in range(rng.randint(1, 2))]
    html = f"<dl>{''.join(parts)}</dl>"
    return html, [(".probe dl > dd::text", "child"),
                  (".probe dt + dd::text", "sibling"),
                  (".probe dl > dt::text", "child-dt"),
                  (".probe dt + dt::text", "sibling-same"),
                  (".probe dd::text", "descendant")]


def c_ruby(rng):  # <rt>/<rp> with omitted end tags — libxml2 NESTS these (never auto-closes)
    parts = []
    for _ in range(rng.randint(1, 3)):
        parts.append(_w(rng))
        for tag in rng.choice([("rt",), ("rt", "rp"), ("rt", "rt"), ("rp", "rt")]):
            parts.append(f"<{tag}>{_w(rng)}")
    html = f"<ruby>{''.join(parts)}</ruby>"
    return html, [(".probe ruby > rt::text", "child"),
                  (".probe ruby > rp::text", "child-rp"),
                  (".probe rt + rt::text", "sibling-same"),
                  (".probe ruby ::text", "descendant")]


def c_stray_close(rng):
    """An end tag with NO open element to match. libxml2 drops it and keeps the character data either
    side as ONE text node; a streaming engine naturally emits two. Malformed, but *doc generators emit
    it* (Sphinx: `</p>\\n</p>`), so it belongs in the gate and not only in the fuzzer — a split here
    silently TRUNCATES a One-cardinality field rather than emptying it."""
    stray = rng.choice(["</p>", "</span>", "</b>", "</li>", "</td>", "</div>", "</bogus>"])
    a, b, c = _w(rng), _w(rng), _w(rng)
    shape = rng.randint(0, 2)
    if shape == 0:
        html = f"<div>{a}{stray}{b}</div>"
    elif shape == 1:  # chained, as Sphinx emits it
        html = f"<div><p>{a}</p>\n{stray}\n{stray}<span>{b}</span>{c}</div>"
    else:  # a real node in the gap must STILL split the run
        html = f"<div>{a}{stray}<!--c-->{b}{stray}{c}</div>"
    return html, [(".probe div::text", "self-text"),
                  (".probe div ::text", "descendant-text"),
                  (".probe > div::text", "child-text")]


def c_option(rng):  # <option> with omitted </option>
    opts = "".join(f"<option>{_w(rng)}" for _ in range(rng.randint(2, 4)))
    html = f"<select>{opts}</select>"
    return html, [(".probe select > option::text", "child"),
                  (".probe option + option::text", "sibling"),
                  (".probe option::text", "descendant")]


def c_optgroup(rng):
    """`<optgroup>` runs with omitted end tags — a grouped `<select>` as real pages write it. libxml2
    NESTS a same-tag repeat here (it does not auto-close), and an `<option>` does not close a group."""
    parts = []
    for i in range(rng.randint(2, 3)):
        # direct text inside the group, so `> optgroup::text` can tell nesting from siblings at all
        # (with only <option> children every column is empty either way and the family proves nothing)
        parts.append(f'<optgroup label="g{i}">{_w(rng)}')
        parts += [f"<option>{_w(rng)}" for _ in range(rng.randint(1, 2))]
    html = f"<select>{''.join(parts)}</select>"
    return html, [(".probe select > optgroup::text", "child"),
                  (".probe optgroup + optgroup::text", "sibling-same"),
                  # the structural discriminator: nested groups make the 2nd group's options
                  # grandchildren, so they drop out of `select > optgroup > option`
                  (".probe select > optgroup > option::text", "group-child-option"),
                  (".probe optgroup optgroup::text", "nested-group"),
                  (".probe select > option::text", "child-option"),
                  (".probe option::text", "descendant")]


def c_table_sections(rng):
    """`<thead>`/`<tbody>`/`<tfoot>`/`<caption>` runs with omitted end tags. The three sections are NOT
    interchangeable in libxml2: `<tbody>`/`<tfoot>` close an open row/cell, `<thead>` nests instead."""
    head = f"<thead><tr><th>{_w(rng)}"
    body = "".join(f"<tr><td>{_w(rng)}" for _ in range(rng.randint(1, 2)))
    foot = f"<tfoot><tr><td>{_w(rng)}" if rng.random() < 0.6 else ""
    cap = f"<caption>{_w(rng)}" if rng.random() < 0.5 else ""
    html = f"<table>{cap}{head}<tbody>{body}{foot}</table>"
    return html, [(".probe table > thead::text", "child-thead"),
                  (".probe table > tbody::text", "child-tbody"),
                  (".probe table > tfoot::text", "child-tfoot"),
                  (".probe table > caption::text", "child-caption"),
                  (".probe thead th::text", "head-cell"),
                  (".probe tbody td::text", "body-cell"),
                  (".probe td::text", "descendant")]


def c_p_nonclosers(rng):
    """An unclosed `<p>` followed by a tag that does NOT close it. `option`/`optgroup`/`thead`/`rt`/`rp`
    nest inside the `<p>` in libxml2; a blanket 'any recognized tag closes <p>' rule over-closes here."""
    t = rng.choice(["option", "optgroup", "thead", "rt", "rp"])
    html = f"<div><p>{_w(rng)}<{t}>{_w(rng)}</div>"
    return html, [(".probe div > p::text", "p-self-text"),
                  (".probe div > *::text", "div-children"),
                  (f".probe div > {t}::text", "would-be-sibling"),
                  (".probe p ::text", "p-subtree")]


def c_colgroup(rng):
    """`<colgroup><col>` with an omitted `</colgroup>`, followed by the sections. libxml2 closes the
    colgroup on `<thead>`/`<tbody>`/`<tfoot>`/`<tr>`/`<colgroup>`; nesting them instead loses every
    child-anchored selector past the colgroup. `<col>` is void, so it is never the open element."""
    cols = "".join("<col>" for _ in range(rng.randint(1, 3)))
    body = "".join(f"<tr><td>{_w(rng)}" for _ in range(rng.randint(1, 2)))
    head = f"<thead><tr><th>{_w(rng)}" if rng.random() < 0.7 else ""
    cap = f"<caption>{_w(rng)}" if rng.random() < 0.4 else ""
    html = f"<table>{cap}<colgroup>{cols}{head}<tbody>{body}</table>"
    return html, [(".probe table > thead th::text", "child-head-cell"),
                  (".probe table > tbody td::text", "child-body-cell"),
                  (".probe table > colgroup::attr(id)", "child-colgroup"),
                  (".probe colgroup thead th::text", "must-not-nest"),
                  (".probe th::text", "descendant-head"),
                  (".probe td::text", "descendant-body")]


def c_table_scope(rng):
    """A stray end tag that would have to unwind a table. libxml2 DISCARDS it (table scope), keeping the
    cell open and its text whole; popping through instead closes the cell early and splits the text.
    Unbalanced `<div>`s wrapped around tables are one of the commonest real-world malformations."""
    outer = rng.choice(["div", "ul", "span", "section"])
    a, b = _w(rng), _w(rng)
    if rng.random() < 0.5:
        html = f"<{outer}><table><tr><td>{a}</{outer}>{b}</td></tr></table>"
    else:  # table-scoped end tags must STILL unwind normally
        html = f"<{outer}><table><tr><td>{a}</table>{b}</{outer}>"
    return html, [(".probe td::text", "cell-text"),
                  (".probe table td::text", "cell-descendant"),
                  (f".probe {outer}::text", "outer-text"),
                  (".probe tr > td::text", "child-cell")]


def c_misnest(rng):  # SKIP: misnested formatting -> adoption agency
    html = f"<div><b>{_w(rng)}<i>{_w(rng)}</b>{_w(rng)}</i></div>"
    return html, [(".probe b::text", "descendant"),
                  (".probe i::text", "descendant"),
                  (".probe div > b::text", "child")]


def c_foster(rng):  # SKIP: stray content inside <table> -> foster parenting
    html = f"<table>{_w(rng)}<tr><td>{_w(rng)}</table>"
    return html, [(".probe table::text", "descendant"),
                  (".probe table > tr::text", "child")]


def c_wellformed(rng):  # CONTROL: everything closed -> must always match
    items = "".join(f"<li>{_w(rng)}</li>" for _ in range(rng.randint(2, 4)))
    html = f"<ul>{items}</ul>"
    return html, [(".probe ul > li::text", "child"),
                  (".probe li + li::text", "sibling"),
                  (".probe ul li::text", "descendant")]


FAMILIES = [
    ("li", "SHOULD", c_li),
    ("p", "SHOULD", c_p),
    ("td/tr", "SHOULD", c_td),
    ("dt/dd", "SHOULD", c_dl),
    ("ruby", "SHOULD", c_ruby),
    ("stray-close", "SHOULD", c_stray_close),
    ("option", "SHOULD", c_option),
    ("optgroup", "SHOULD", c_optgroup),
    ("table-sections", "SHOULD", c_table_sections),
    ("p-nonclosers", "SHOULD", c_p_nonclosers),
    ("table-scope", "SHOULD", c_table_scope),
    ("colgroup", "SHOULD", c_colgroup),
    ("misnest-fmt", "SKIP", c_misnest),
    ("table-foster", "SKIP", c_foster),
    ("well-formed", "CONTROL", c_wellformed),
]


def _filler(rng):
    return (f"<header><h1>{_w(rng, 3)}</h1><nav><a href='/'>{_w(rng)}</a></nav></header>"
            f"<p>{_w(rng, 6)}</p>")


def build_page(rng, builder):
    probe, selectors = builder(rng)
    body = (f"<!DOCTYPE html><html><head><title>{_w(rng, 2)}</title></head><body>"
            f"{_filler(rng)}<main><div class=\"probe\">{probe}</div>"
            f"<div>{_w(rng, 8)}</div></main></body></html>")
    return body.encode("utf-8"), selectors
