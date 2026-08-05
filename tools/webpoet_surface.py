"""Derive the web-poet / zyte-common-items surface from the LIBRARIES, and go red when it moves.

Every one of the five web-poet integration defects was a hand-written list that omitted something:

  * the page base classes it could be fed (``BrowserResponse`` missing -> raised; web-poet's own
    ``BrowserPage`` missing -> silently returned ``{}``),
  * ``field()``'s keyword surface (``cached``/``meta``/``out`` missing -> a processor forced you out of
    the shared scan),
  * the value types a field processor accepts (``Selector``/``SelectorList``/``HtmlElement`` missing ->
    a raw-HTML string in a field typed ``List[Breadcrumb]``, silently).

That is the `colgroup` mistake AGENTS.md records four times in the engine's own rule tables — "a rule with
no name to probe cannot fail a gate" — and the answer there was to DERIVE the universe from the source of
truth rather than transcribe it. Here the source of truth is *importable*: web-poet and zyte-common-items
are Python objects, so the universe can be read instead of guessed at.

So this enumerates, by introspection:

  1. **web-poet's page/extractor base classes** — every ``Injectable``/``Extractor`` subclass it exports.
     Each must be either covered by a Frostwork base or explicitly DECLINED with a reason.
  2. **``web_poet.field``'s keyword-only parameters** — each must be forwarded by ``frostwork.webpoet.field``
     or explicitly declined.
  3. **``zyte_common_items.processors``' public processors** — each must be exercised by
     ``tools/diff_webpoet.py`` or explicitly declined.
  4. **the value types those processors branch on** — the ``isinstance`` gates that decide whether a
     processor transforms its input or hands it back unchanged, which is the mechanism defect 5 rode in on.

A name that appears upstream and in none of the accept/decline lists is a FAILURE, not a warning. That is
the whole point: the next `colgroup` should arrive as a red gate rather than as a silent wrong value.

Run:   .venv/bin/python tools/webpoet_surface.py            # print the snapshot
Gate:  .venv/bin/python tools/webpoet_surface.py --check    # fail if it drifted from the checked-in copy
"""

from __future__ import annotations

import argparse
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import web_poet
import webpoet_cases
from parsel import Selector, SelectorList
from web_poet import field as wp_field

from frostwork.webpoet import _WP_FIELD_KWARGS, FrostBrowserPage, FrostFields, FrostPage

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "WEBPOET_SURFACE.md"


# ------------------------------------------------------------------ 1. page / extractor base classes
# name -> (Frostwork counterpart, or None with a reason for declining)
BASES = {
    "ItemPage": ("FrostFields", None),
    "Extractor": (
        None,
        "field support WITHOUT `Injectable`, for a bundle composed into a page (as `SelectorExtractor` is). "
        "`FrostFields` is the `ItemPage` form — the same support plus injectability, which scrapy-poet "
        "requires: andi silently drops a callback argument whose class `is_injectable()` rejects.",
    ),
    "WebPage": ("FrostPage", None),
    "BrowserPage": ("FrostBrowserPage", None),
    "SelectorExtractor": (
        None,
        "input is a parsel.Selector — a tree lxml already built. Scanning it would mean serializing "
        "that tree back to markup and re-parsing it, which can disagree with both the original bytes "
        "and the Selector; and with a tree already built there is nothing left to save.",
    ),
    "Injectable": (None, "marker base for dependency injection, not an extraction surface"),
    "Returns": (None, "generic item-class mixin, composed with any base"),
}

FROST_BASES = {"FrostFields": FrostFields, "FrostPage": FrostPage, "FrostBrowserPage": FrostBrowserPage}


def upstream_bases() -> list:
    """Every class web-poet exports that participates in the page-object hierarchy."""
    from web_poet.pages import Extractor, Injectable, Returns

    out = []
    for name in dir(web_poet):
        if name.startswith("_"):
            continue
        obj = getattr(web_poet, name)
        if not inspect.isclass(obj):
            continue
        if issubclass(obj, (Injectable, Extractor, Returns)):
            out.append(name)
    return sorted(out)


# ------------------------------------------------------------------ 2. field() keyword surface
FIELD_KWARGS = {name: None for name in _WP_FIELD_KWARGS}  # name -> decline reason (None = forwarded)


def upstream_field_kwargs() -> list:
    return sorted(
        n for n, p in inspect.signature(wp_field).parameters.items() if p.kind == p.KEYWORD_ONLY
    )


# ------------------------------------------------------------------ 3. zyte processors
# The registry in `tools/webpoet_cases.py` is the single source: it says which processors are covered, how a
# page object reaches each one, and which gate proves it — so this tool and `tools/diff_webpoet.py` cannot
# drift. They did: two processors were declined here with reasons that were simply wrong about upstream
# (`description_processor` "reads a side channel" — it WRITES one; `gtin_processor` "takes a GTIN argument" —
# it is a plain `(value, page)`), which excluded them from every gate by a sentence nobody re-read.
PROCESSORS = {
    **{c.processor: None for c in webpoet_cases.CASES},
    **webpoet_cases.DECLINED,
}


def upstream_processors() -> list:
    return webpoet_cases.upstream_processors()


# ------------------------------------------------------------------ 4. processor input value types
# The isinstance gates a processor gates on, and whether Frostwork can produce that type for a field.
# This is the table defect 5 lived in: `str` was the only thing produced, and every node-taking processor
# is documented to return anything else UNCHANGED.
VALUE_TYPES = [
    ("str", "yes", "a `::text` / `::attr()` terminal, or a bare element with no processor (raw source)"),
    ("list[str]", "yes", "`all=True` on a scalar terminal — what images_processor consumes"),
    ("parsel.Selector", "yes", "a bare-element field with a processor attached: raw source re-parsed"),
    ("parsel.SelectorList", "yes", "the same, `all=True`"),
    ("lxml.html.HtmlElement", "via Selector", "processors accept `.root`; handed over as a Selector"),
    ("dict", "no", "rating_processor's dict form composes sub-values; write it as a @web_poet.field"),
]


def _dist_version(dist: str) -> str:
    """Installed version of a distribution. Neither library exposes `__version__`, and hand-parsing the
    requirements file would report the PIN rather than what is actually imported here."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(dist)
    except PackageNotFoundError:  # pragma: no cover - both are pinned test deps
        return "(not installed)"


def _gate(kind: str, upstream: list, known: dict) -> list:
    """Every upstream name must be in `known` (covered or declined). Returns the rows to render."""
    missing = [n for n in upstream if n not in known]
    if missing:
        raise SystemExit(
            f"webpoet-surface: {kind} appeared upstream and is in neither the covered nor the declined "
            f"list: {missing}\n"
            f"  This is the gate working. Add it to tools/webpoet_surface.py — either wire it up in "
            f"frostwork.webpoet (and exercise it in tools/diff_webpoet.py) or decline it with a REASON.\n"
            f"  Do not delete the name from the check to make this pass."
        )
    stale = [n for n in known if n not in upstream]
    if stale:
        raise SystemExit(
            f"webpoet-surface: {kind} is listed here but no longer exists upstream: {stale}\n"
            f"  Upstream removed or renamed it; drop the entry (and any code that targets it)."
        )
    return [(n, known[n]) for n in upstream]


def render() -> str:
    base_rows = _gate("a page base class", upstream_bases(), BASES)
    kwarg_rows = _gate("a field() keyword", upstream_field_kwargs(), FIELD_KWARGS)
    proc_rows = _gate("a zyte processor", upstream_processors(), PROCESSORS)

    # the Frostwork bases named in the table must actually exist and be usable
    for _name, (counterpart, _reason) in BASES.items():
        if counterpart is not None and counterpart not in FROST_BASES:
            raise SystemExit(f"webpoet-surface: BASES names {counterpart!r}, which frostwork.webpoet lacks")

    # ...and "usable" includes INJECTABLE, which is the half that was missing. scrapy-poet builds a
    # callback argument only if `web_poet.pages.is_injectable` accepts its class; for anything it rejects,
    # andi leaves the argument out of the plan and the page object never arrives — no exception, no log.
    # Asked here rather than in a test because it is a property of an UPSTREAM predicate: if web-poet
    # changes what counts as injectable, this is the gate that should go red.
    from web_poet.pages import is_injectable

    for name, base in FROST_BASES.items():
        if not is_injectable(base):
            raise SystemExit(
                f"webpoet-surface: {name} is not is_injectable(), so scrapy-poet would silently omit a "
                f"callback argument annotated with it. Every shipped base must be an ItemPage."
            )

    # and the node types claimed producible must really be what the handoff produces
    from frostwork.webpoet import _as_node, _as_nodes

    if not isinstance(_as_node("<p>x</p>"), Selector):
        raise SystemExit("webpoet-surface: the node handoff no longer produces a parsel.Selector")
    if not isinstance(_as_nodes(["<p>x</p>"], ("all", None)), SelectorList):
        raise SystemExit("webpoet-surface: the all= node handoff no longer produces a SelectorList")

    lines = [
        "# web-poet integration surface",
        "",
        "Generated/verified by `tools/webpoet_surface.py` **from the installed libraries** — not written by",
        "hand. Every name below was read out of `web_poet` / `zyte_common_items.processors` by",
        "introspection, and a name that appears upstream and in neither list fails the gate. Five defects",
        "came from hand-written versions of these four tables; see `docs/PYTHON.md` for how to use the",
        "supported entries and `AGENTS.md` for why the universe is derived rather than transcribed.",
        "",
        f"Read from: web-poet {_dist_version('web-poet')}, "
        f"zyte-common-items {_dist_version('zyte-common-items')} "
        "(both pinned in `requirements-test.txt`, so this snapshot moves only when a pin does).",
        "",
        "## Page / extractor base classes",
        "",
        "| web-poet class | Frostwork counterpart | if declined, why |",
        "|---|---|---|",
    ]
    for name, (counterpart, reason) in base_rows:
        lines.append(f"| `{name}` | {f'`{counterpart}`' if counterpart else '—'} | {reason or ''} |")

    lines += [
        "",
        "## `web_poet.field` keyword surface",
        "",
        "Forwarded verbatim by `frostwork.webpoet.field`, so a declaration built by Frostwork is not a",
        "second-class `web_poet.field`.",
        "",
        "| keyword | forwarded | if declined, why |",
        "|---|---|---|",
    ]
    for name, reason in kwarg_rows:
        lines.append(f"| `{name}` | {'no' if reason else 'yes'} | {reason or ''} |")

    lines += [
        "",
        "## zyte-common-items field processors",
        "",
        "Each supported processor is exercised against parsel by `make gate-webpoet`, on generated markup",
        "the processor can actually parse (the run prints how many pairs carried a non-empty expected",
        "value, because a processor returning `None` on both sides proves nothing).",
        "",
        "| processor | covered | if declined, why |",
        "|---|---|---|",
    ]
    for name, reason in proc_rows:
        lines.append(f"| `{name}` | {'no' if reason else 'yes'} | {reason or ''} |")

    lines += [
        "",
        "## Value types a processor can be handed",
        "",
        "The `isinstance` gates that decide whether a processor transforms its input or returns it",
        "unchanged. `str` used to be the only type Frostwork produced, which is exactly why a node-taking",
        "processor silently passed raw HTML through into a typed field.",
        "",
        "| type | Frostwork can produce | from |",
        "|---|---|---|",
    ]
    for name, can, how in VALUE_TYPES:
        lines.append(f"| `{name}` | {can} | {how} |")

    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the checked-in snapshot differs")
    args = ap.parse_args()
    content = render()
    if args.check:
        if not TARGET.exists():
            raise SystemExit(f"{TARGET.relative_to(ROOT)} is missing; generate it with tools/webpoet_surface.py")
        if TARGET.read_text() != content:
            raise SystemExit(
                f"{TARGET.relative_to(ROOT)} is stale; regenerate with "
                f"`python tools/webpoet_surface.py > {TARGET.relative_to(ROOT)}`"
            )
        print(f"webpoet-surface: {TARGET.relative_to(ROOT)} matches the installed libraries -> PASS")
    else:
        print(content, end="")


if __name__ == "__main__":
    main()
