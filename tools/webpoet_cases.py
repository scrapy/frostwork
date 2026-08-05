"""One registry of field-processor CASES, shared by the web-poet differential and the surface gate.

Both tools need the same three answers about every processor `zyte_common_items` ships — is it covered,
what does a page object declare to reach it, and which gate proves it — and they used to answer separately.
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
    gates: Tuple[str, ...]  # which of GATES exercises it
    card: Tuple[str, Optional[str]] = ("first", None)

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
    ProcessorCase("images_processor", "images", "img.hero::attr(src)", "strings", GATES, ("all", None)),
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
    """`{field name: [processor names]}` as zyte's own `ProductPage` declares it — the by-NAME wiring a
    page object inherits without writing any `out=`."""
    from zyte_common_items.pages import ProductPage

    return {
        name: [getattr(f, "__name__", str(f)) for f in procs]
        for name, procs in vars(ProductPage.Processors).items()
        if not name.startswith("_")
    }


def cases_for(gate: str) -> tuple:
    return tuple(c for c in CASES if gate in c.gates)


def coverage_gaps() -> list:
    """Names upstream that are in neither `CASES` nor `DECLINED`, and cases whose processor is gone."""
    known = {c.processor for c in CASES} | set(DECLINED)
    upstream = set(upstream_processors())
    return (
        [f"upstream and unclassified: {n}" for n in sorted(upstream - known)]
        + [f"classified here but gone upstream: {n}" for n in sorted(known - upstream)]
    )
