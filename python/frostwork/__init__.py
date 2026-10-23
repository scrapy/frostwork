"""Frostwork — treeless, one-pass HTML extraction.

`extract(html, queries, encoding=None)` is the primitive: one streaming scan answers every
CSS/XPath query, returning one value-column per query (no DOM, no fallback). Unsupported selectors
raise by default; pass ``strict=False`` for permissive empty columns. The declarative `Page`/`Item`
layer names those columns and fills a whole item in that single pass.

For Scrapy / web-poet page objects, see `frostwork.webpoet` (requires the ``web-poet`` package —
the ``frostwork[webpoet]`` extra).
"""

from importlib import import_module as _import_module
import typing as _typing

if _typing.TYPE_CHECKING:
    # `X as X` marks each name as re-exported, which type checkers run with `no_implicit_reexport`
    # (mypy --strict) require, since they cannot read `__all__` below.
    from ._navigation import MetaRefresh as MetaRefresh, Navigation as Navigation, navigation as navigation
    from .encoding import detect_encoding as detect_encoding, resolve_label as resolve_label
    from .page import (
        FieldReport as FieldReport,
        GroupReport as GroupReport,
        Item as Item,
        Page as Page,
        SchemaReport as SchemaReport,
        UnsupportedSelector as UnsupportedSelector,
        check as check,
        extract as extract,
        extract_grouped as extract_grouped,
    )
    from .validation import (
        FieldProcessingError as FieldProcessingError,
        ItemValidationError as ItemValidationError,
        ValidationIssue as ValidationIssue,
        ValidationReport as ValidationReport,
    )

    __version__: str

# Imported on first access, so that `import frostwork.encoding` does not pay for the page-object layer.
_EXPORTS = {
    "extract": "page",
    "extract_grouped": "page",
    "check": "page",
    "detect_encoding": "encoding",
    "resolve_label": "encoding",
    "navigation": "_navigation",
    "Navigation": "_navigation",
    "MetaRefresh": "_navigation",
    "Page": "page",
    "Item": "page",
    "SchemaReport": "page",
    "FieldReport": "page",
    "GroupReport": "page",
    "UnsupportedSelector": "page",
    "FieldProcessingError": "validation",
    "ItemValidationError": "validation",
    "ValidationIssue": "validation",
    "ValidationReport": "validation",
}

__all__ = [*_EXPORTS, "__version__"]


# Hidden from type checkers, which would otherwise read any misspelled name as `Any`.
if not _typing.TYPE_CHECKING:

    def __getattr__(name: str) -> _typing.Any:
        value: _typing.Any
        if name == "__version__":
            try:
                from importlib.metadata import version

                value = version("frostwork")
            except Exception:  # pragma: no cover - source tree without installed metadata
                value = "0.0.0+unknown"
        elif name in _EXPORTS:
            value = getattr(_import_module(f".{_EXPORTS[name]}", __name__), name)
        elif name in {"encoding", "page", "validation"}:
            value = _import_module(f".{name}", __name__)
        else:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        globals()[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted({*globals(), *__all__})
