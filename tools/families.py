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


def c_dl(rng):  # <dt>/<dd> with omitted end tags
    parts = "".join(f"<dt>{_w(rng)}<dd>{_w(rng)}" for _ in range(rng.randint(1, 3)))
    html = f"<dl>{parts}</dl>"
    return html, [(".probe dl > dd::text", "child"),
                  (".probe dt + dd::text", "sibling"),
                  (".probe dd::text", "descendant")]


def c_option(rng):  # <option> with omitted </option>
    opts = "".join(f"<option>{_w(rng)}" for _ in range(rng.randint(2, 4)))
    html = f"<select>{opts}</select>"
    return html, [(".probe select > option::text", "child"),
                  (".probe option + option::text", "sibling"),
                  (".probe option::text", "descendant")]


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
    ("option", "SHOULD", c_option),
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
