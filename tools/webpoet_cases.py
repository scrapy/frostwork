"""One registry of field-processor CASES, shared by the web-poet differential and the surface gate.

Both tools need the same three answers about every processor `zyte_common_items` ships — is it covered,
what does a page object declare to reach it, and which gate proves it — answered in one place, not three.
That drifted twice in the same commit: `description_processor` was declined as reading a side channel
(it does not; it processes its input and WRITES one) and `gtin_processor` as needing a non-standard
signature (it is a plain `(value, page)`), so two real processors were excluded from every gate by a
sentence nobody re-read. A coverage check built from "the buckets that showed up" cannot notice that: an
entire family can vanish and every number stays green.

So the universe is DERIVED — `upstream_processors()` reads the module, `PRODUCT_PAGE_PROCESSORS` reads
`ProductPage.Processors` — and this file only says, per name, how to exercise it. A name upstream that is
in neither `CASES` nor `DECLINED` fails the surface gate; a case that no run graded fails the differential.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Optional, Tuple

import zyte_common_items.processors as zproc

# Which gate proves a case:
#   "generated"   — tools/diff_webpoet.py's generated schemas, against a parsel oracle with the same
#                   nested `Processors` (the column that varies cardinality, out=[] and .map())
#   "productpage" — the same processor reached through zyte's REAL `ProductPage` MRO, by field name only
GATES = ("generated", "productpage")


@dataclasses.dataclass(frozen=True)
class ProcessorCase:
    """One processor, and everything a gate needs to exercise it."""

    processor: str  # attribute name in `zyte_common_items.processors`
    field_name: str  # the FIELD NAME web-poet resolves it under (zyte's own name where there is one)
    selector: str  # a selector against `tools/diff_webpoet.gen_page()`'s markup
    input_kind: str  # "node" (isinstance-gated on Selector/HtmlElement) | "strings" (URL strings)
    gates: Tuple[str, ...]  # which of GATES exercises it — validated below, never empty

    def __post_init__(self):
        # An empty or misspelled `gates` would remove the case from every differential while every coverage
        # check stayed green: the required-column set is DERIVED from these tuples, so a case that claims no
        # gate is a case nobody notices is missing.
        unknown = set(self.gates) - set(GATES)
        if unknown or not self.gates:
            raise ValueError(
                f"ProcessorCase({self.processor}): gates={self.gates!r} must be a non-empty subset of "
                f"{GATES}; a case exercised by no gate is invisible to the coverage check. Decline it in "
                f"DECLINED with a reason instead."
            )
        if self.input_kind not in ("node", "strings"):
            raise ValueError(f"ProcessorCase({self.processor}): unknown input_kind {self.input_kind!r}")

    @property
    def callable(self):
        return getattr(zproc, self.processor)

    @property
    def takes_node(self) -> bool:
        return self.input_kind == "node"


CASES = (
    ProcessorCase("breadcrumbs_processor", "breadcrumbs", ".crumbs", "node", GATES),
    ProcessorCase("description_html_processor", "descriptionHtml", ".desc", "node", GATES),
    # Not a side-channel reader (the old decline reason): it processes its input and WRITES
    # `page._description_str`/`_description_node`, which zyte's own `descriptionHtml` field reads when the
    # page object does not declare one. Here both are declared, so each processor sees its own value.
    ProcessorCase("description_processor", "description", ".desc", "node", GATES),
    ProcessorCase("rating_processor", "aggregateRating", ".rating", "node", GATES),
    ProcessorCase("price_processor", "price", ".price", "node", GATES),
    # zyte attaches this one to `regularPrice`; the differential also drives it under an invented field
    # name, which is what `simplePrice` is below.
    ProcessorCase("simple_price_processor", "regularPrice", ".regular-price", "node", GATES),
    ProcessorCase("brand_processor", "brand", ".brand", "node", GATES),
    # A plain `(value, page)` processor over a node, contrary to the old decline reason ("takes a GTIN type
    # argument"). zyte-parsers reads the GTIN out of the element's text.
    ProcessorCase("gtin_processor", "gtin", ".gtin", "node", GATES),
    # The one processor whose input is URL STRINGS, with no Selector branch at all — so its faithful
    # declaration is a scalar terminal with all=True, and handing it a node would break a working field.
    ProcessorCase("images_processor", "images", "img.hero::attr(src)", "strings", GATES),
)

# Upstream names that are NOT field processors, with the reason. Anything here is excluded from coverage on
# purpose; anything upstream in neither list fails the surface gate.
DECLINED = {
    "metadata_processor": "operates on an item's metadata object, not on a selector's value",
    "probability_request_list_processor": "takes a Request list (one argument), not a field value",
    "only_handle_nodes": "a DECORATOR used to build processors, not a processor",
}


def upstream_processors() -> list:
    """Public callables in `zyte_common_items.processors` that look like field processors — read from the
    module rather than listed, so a processor added upstream arrives as a gate failure."""
    out = []
    for name in dir(zproc):
        if name.startswith("_"):
            continue
        obj = getattr(zproc, name)
        if not callable(obj) or inspect.isclass(obj):
            continue
        if getattr(obj, "__module__", "") != zproc.__name__:
            continue  # re-exported helper (extract_price, clean_node, ...)
        out.append(name)
    return sorted(out)


def product_page_processors() -> dict:
    """`{field name: [processor names, in order]}` — the EFFECTIVE wiring a page object inherits from zyte's
    `ProductPage`, resolved across the whole `Processors` MRO.

    `vars(ProductPage.Processors)` is not that: the class inherits from two more `Processors` classes, and
    reading only its own namespace missed `metadata` entirely while every check stayed green. Ordered lists,
    not sets, because a processor APPENDED to a field that is already covered changes what runs."""
    from zyte_common_items.pages import ProductPage

    effective: dict = {}
    for klass in reversed(ProductPage.Processors.__mro__):
        for name, procs in vars(klass).items():
            if name.startswith("_") or not isinstance(procs, (list, tuple)):
                continue
            effective[name] = [getattr(f, "__name__", str(f)) for f in procs]
    return effective


def cases_for(gate: str) -> tuple:
    return tuple(c for c in CASES if gate in c.gates)


# Field names in `ProductPage.Processors` that this registry deliberately does NOT drive through the
# `productpage` gate: `{field: (the processors expected there, the reason)}`. Checked against the real class,
# so a name zyte adds or removes shows up as a gap rather than as a quietly smaller sweep.
#
# The expected LIST is half the entry: a decline is about one WIRING, and storing only a reason degraded the
# check to "the name is wired to something" — re-pointing `metadata`, appending to it or emptying it passed.
DECLINED_PRODUCT_PAGE_FIELDS = {
    "metadata": (
        ["metadata_processor"],
        "metadata_processor operates on the item's metadata object, not on a selector's value, so no page "
        "object declares a `metadata` selector for it to process (see DECLINED above)",
    ),
}


def coverage_gaps() -> list:
    """Everything the registry and the installed libraries disagree about, in BOTH directions.

    Registry -> upstream catches a stale entry; upstream -> registry catches the dangerous one: a processor
    (or a `ProductPage.Processors` mapping) that upstream ADDS and nobody here notices, which is a shrinking
    sweep that reports the same green."""
    gaps = []
    known = {c.processor for c in CASES} | set(DECLINED)
    upstream = set(upstream_processors())
    gaps += [f"upstream processor in neither CASES nor DECLINED: {n}" for n in sorted(upstream - known)]
    gaps += [f"classified here but gone upstream: {n}" for n in sorted(known - upstream)]

    wired = product_page_processors()
    covered = {c.field_name for c in cases_for("productpage")}
    for name, procs in sorted(wired.items()):
        if name in covered or name in DECLINED_PRODUCT_PAGE_FIELDS:
            continue
        gaps.append(
            f"ProductPage.Processors wires {name!r} -> {procs} and no case drives it through the "
            f"productpage gate (add one, or decline it in DECLINED_PRODUCT_PAGE_FIELDS with a reason)"
        )
    for name in sorted(covered - set(wired)):
        gaps.append(f"a case claims ProductPage wires {name!r}, but the class no longer does")
    for name, (expected, why) in sorted(DECLINED_PRODUCT_PAGE_FIELDS.items()):
        if name not in wired:
            gaps.append(f"{name!r} is declined for the productpage gate but ProductPage no longer wires it")
        elif wired[name] != expected:
            gaps.append(
                f"{name!r} is declined because {why} — but ProductPage now wires {wired[name]} there, not "
                f"{expected}. The decline was about that wiring; re-read it before widening this entry."
            )
    for case in cases_for("productpage"):
        # EXACTLY this processor, and only it: membership would accept a second processor appended upstream to
        # a field this registry already covers, which changes what runs and what the gate should compare
        if wired.get(case.field_name) != [case.processor]:
            gaps.append(
                f"{case.processor} is claimed to be all that arrives as {case.field_name!r}, but ProductPage "
                f"wires {wired.get(case.field_name)} there"
            )
    return gaps
