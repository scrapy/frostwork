"""Frostwork — treeless, one-pass HTML extraction.

`extract(html, queries, encoding=None)` is the primitive: one streaming scan answers every
CSS/XPath query, returning one value-column per query (no DOM, no fallback). Unsupported selectors
raise by default; pass ``strict=False`` for permissive empty columns. The declarative `Page`/`Item`
layer names those columns and fills a whole item in that single pass.

For Scrapy / web-poet page objects, see `frostwork.webpoet` (requires the ``web-poet`` package —
the ``frostwork[webpoet]`` extra).
"""

from .page import (
    FieldReport,
    GroupReport,
    Item,
    Page,
    SchemaReport,
    UnsupportedSelector,
    check,
    extract,
    extract_grouped,
)

try:
    from importlib.metadata import version as _version

    __version__ = _version("frostwork")
except Exception:  # pragma: no cover - source tree without installed metadata
    __version__ = "0.0.0+unknown"

__all__ = [
    "extract",
    "extract_grouped",
    "check",
    "Page",
    "Item",
    "SchemaReport",
    "FieldReport",
    "GroupReport",
    "UnsupportedSelector",
    "__version__",
]
