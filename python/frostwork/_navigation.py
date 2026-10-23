from __future__ import annotations

from typing import NamedTuple, Optional, Tuple, Union

from ._frostwork import navigation as _navigation

__all__ = ["MetaRefresh", "Navigation", "navigation"]


class MetaRefresh(NamedTuple):
    """A meta refresh, as :attr:`Navigation.meta_refresh` reports it."""

    delay: float
    """The delay before the refresh, in whole seconds."""

    url: str
    """The absolute URL to load, which is the document URL itself for a refresh that names none."""


class Navigation(NamedTuple):
    """The base URL and meta refresh of a document, as :func:`navigation` and
    :meth:`Page.extract_with_navigation <frostwork.Page.extract_with_navigation>` return them. A
    value that was not asked for is ``None``."""

    base_url: Optional[str]
    """The URL that relative URLs of the document resolve against: its first ``<base>`` with an
    ``href``, resolved, or the document URL when it has none or its ``href`` is unusable. URLs are
    serialized as the URL standard does, except a document URL that is not absolute, which is kept as
    given."""

    meta_refresh: Optional[MetaRefresh]
    """The meta refresh a browser would follow, or ``None`` if the document declares none."""


def navigation(
    html: Union[bytes, bytearray, memoryview, str],
    url: str,
    encoding: Optional[str] = None,
    *,
    base_url: bool = True,
    meta_refresh: bool = True,
    scripting: bool = True,
) -> Navigation:
    """Read the base URL and meta refresh of *html*, the document at *url*, in one pass, the way a
    browser does: the whole document counts, the shortest refresh delay wins, and a tie goes to the
    later ``<meta>``. Where Chromium and Firefox disagree, the answer is Firefox's.

    *base_url* and *meta_refresh* choose what to read. *scripting* reads the document as a browser
    with JavaScript enabled does, which ignores the content of ``<noscript>``; pass ``False`` to read
    it as one with JavaScript disabled.

    *encoding* is a charset label, as Scrapy passes from ``Content-Type``; ``None``, or a label that
    names no encoding, detects it from the document. When *url* is not absolute, only absolute URLs
    in the document resolve, and a relative refresh URL is ignored.
    """
    if not isinstance(html, (bytes, str)):
        html = bytes(html)
    return _from_native(
        _navigation(html, url, encoding, base_url=base_url, meta_refresh=meta_refresh, scripting=scripting)
    )


def _from_native(nav: Tuple[Optional[str], Optional[Tuple[float, str]]]) -> Navigation:
    base_url, refresh = nav
    return Navigation(base_url, None if refresh is None else MetaRefresh(*refresh))
