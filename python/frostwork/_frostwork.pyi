"""Type stubs for the native Frostwork core (`frostwork._frostwork`).

The ergonomic, fully-typed surface is `frostwork.page` / `frostwork.webpoet`; these are the raw
primitives crossing the PyO3 boundary. `html` must be `bytes` (or a `bytes` subclass such
as web-poet's `HttpResponseBody`); the pure-Python wrappers accept other bytes-likes and `str`.
"""

from typing import List, Literal, Optional, Sequence, Tuple, Union, overload

Query = Union[str, Tuple[str, Optional[str]]]
"""A selector string, routed as CSS or XPath by its prefix, or ``(selector, syntax)`` with ``syntax``
one of ``"css"``, ``"xpath"`` or ``None`` (auto)."""

Parser = Literal["browser", "lxml"]
"""The parse mode: ``"browser"`` (the default) builds the tree a browser builds, ``"lxml"`` lxml's. Any other
string raises ``ValueError``."""

EncodingSource = Literal[
    "bom", "utf16-prefix", "encoding", "content-type", "xml-declaration", "meta", "utf8", "default",
    "autodetect", "str",
]
"""The rank that decided an encoding (``detect_encoding(..., explain=True)``); ``"str"`` for decoded text."""

def extract(
    html: Union[bytes, str],
    queries: Sequence[Query],
    encoding: Optional[str] = ...,
    *,
    content_type: Optional[str] = ...,
    url: Optional[str] = ...,
    parser: Parser = ...,
) -> List[List[str]]:
    """One run over ``html``: one value-column per query, in query order."""

def extract_grouped(
    html: Union[bytes, str],
    flat_queries: Sequence[Query],
    groups: Sequence[Tuple[Query, Sequence[Tuple[str, Query]]]],
    encoding: Optional[str] = ...,
    *,
    content_type: Optional[str] = ...,
    url: Optional[str] = ...,
    parser: Parser = ...,
) -> Tuple[List[List[str]], List[List[List[List[str]]]]]:
    """One run returning ``(flat_columns, grouped)`` — ``grouped[g][row][subfield][value]``."""

def audit_schema(
    flat_queries: Sequence[Query],
    groups: Sequence[Tuple[Query, Sequence[Tuple[str, Query]]]],
    *,
    parser: Parser = ...,
) -> Tuple[
    List[Tuple[bool, Optional[str]]],
    List[Tuple[Tuple[bool, Optional[str]], List[Tuple[bool, Optional[str]]]]],
    Tuple[int, int, int, int],
]:
    """Support/reason per selector in the ``parser`` mode, plus ``(members, max_members, sib_bits,
    max_sib_bits)`` — no HTML parsed."""

def selector_terminals(queries: Sequence[Query]) -> List[Optional[str]]:
    """The value terminal each query produces: ``"text"``, ``"attr"``, ``"outer"``, ``"normalize-space"``,
    or ``None`` if it does not compile. ``"outer"`` means the column holds the matched element's raw
    source — a NODE reference, not a scalar — which is what ``frostwork.webpoet`` re-parses before handing
    a field to a processor. Derived from the compiler, so it cannot drift from how a query is routed."""

def selector_node_identity(queries: Sequence[Query]) -> List[Tuple[Optional[str], bool]]:
    """Per query, ``(pinned_tag, can_match_a_synthesized_frame)`` — the matched-node identity an
    outer-HTML value cannot always carry. ``pinned_tag`` is the tag name every match must have (the
    subject's name test, when every comma/union member agrees) or ``None``; the flag is ``True`` when a
    match could be an ``<html>``/``<head>``/``<body>`` the page never wrote, whose value therefore begins
    with its CONTENT and names nothing. ``frostwork.webpoet``'s ``.as_node()`` reads both."""

def resolve_label(label: str) -> Optional[str]:
    """Canonical WHATWG encoding name for ``label`` (e.g. ``"UTF-8"``), or ``None`` if neither WHATWG
    nor Python's codec set names a WHATWG encoding by it."""

@overload
def detect_encoding(
    html: Union[bytes, str],
    encoding: Optional[str] = ...,
    *,
    content_type: Optional[str] = ...,
    url: Optional[str] = ...,
    explain: Literal[False] = ...,
) -> str:
    """The encoding ``extract`` would scan this document with, as a WHATWG name: BOM → BOM-less UTF-16
    prefix → ``encoding`` label → ``content_type`` charset → XML declaration or ``<meta>`` →
    autodetection: valid UTF-8 is windows-1252 when ``url`` is http(s), as browsers read it, and UTF-8
    otherwise; anything else is guessed, informed by the top-level domain of ``url``. A label that names
    no encoding is ignored. A ``str`` is already-decoded text, so the answer is ``"UTF-8"`` without sniffing."""
@overload
def detect_encoding(
    html: Union[bytes, str],
    encoding: Optional[str] = ...,
    *,
    content_type: Optional[str] = ...,
    url: Optional[str] = ...,
    explain: Literal[True],
) -> Tuple[str, EncodingSource]:
    """``(name, source)``: the encoding and the rank that decided it."""
@overload
def detect_encoding(
    html: Union[bytes, str],
    encoding: Optional[str] = ...,
    *,
    content_type: Optional[str] = ...,
    url: Optional[str] = ...,
    explain: bool = ...,
) -> Union[str, Tuple[str, EncodingSource]]: ...

def bom_encoding(body: bytes) -> Optional[Tuple[str, int]]:
    """``(encoding, bom_length)`` from the start of ``body``, or ``None``."""
def declared_encoding(body: bytes) -> Optional[str]:
    """The encoding the ``<meta>``/XML-declaration prescan finds, or ``None``."""
def autodetect_encoding(body: bytes, *, url: Optional[str] = ...) -> str:
    """For valid UTF-8, ``"windows-1252"`` when ``url`` is http(s), else ``"UTF-8"``; for anything else,
    a guess informed by the top-level domain of ``url``."""
def navigation(
    html: Union[bytes, str],
    url: str,
    encoding: Optional[str] = ...,
    *,
    content_type: Optional[str] = ...,
    base_url: bool = ...,
    meta_refresh: bool = ...,
    scripting: bool = ...,
) -> Tuple[Optional[str], Optional[Tuple[float, str]]]:
    """``(base_url, meta_refresh)`` as a browser reads them."""
def decode(body: bytes, label: str) -> str:
    """``body`` decoded with the WHATWG decoder, BOM included; ``LookupError`` unless ``label`` is a
    WHATWG label."""

class Plan:
    """A schema compiled once (budget validated at construction) and reused across pages."""

    def __init__(
        self,
        flat_queries: Sequence[Query],
        groups: Sequence[Tuple[Query, Sequence[Tuple[str, Query]]]],
        first_only: Optional[List[bool]] = ...,
        *,
        parser: Parser = ...,
    ) -> None:
        """``first_only[c]`` declares that flat column ``c``'s consumer keeps only the FIRST value. When
        every column says so and the schema is eligible, the scan stops as soon as each has one instead
        of running to EOF; the values a single-valued consumer sees are unchanged. ``parser`` fixes the
        parse mode, once."""
    @property
    def parser(self) -> Parser:
        """The parse mode this plan was compiled for."""
    def extract(
        self,
        html: Union[bytes, str],
        encoding: Optional[str] = ...,
        *,
        content_type: Optional[str] = ...,
        url: Optional[str] = ...,
    ) -> List[List[str]]: ...
    def extract_grouped(
        self,
        html: Union[bytes, str],
        encoding: Optional[str] = ...,
        *,
        content_type: Optional[str] = ...,
        url: Optional[str] = ...,
    ) -> Tuple[List[List[str]], List[List[List[List[str]]]]]: ...
    def extract_with_navigation(
        self,
        html: Union[bytes, str],
        url: str,
        encoding: Optional[str] = ...,
        *,
        content_type: Optional[str] = ...,
        base_url: bool = ...,
        meta_refresh: bool = ...,
        scripting: bool = ...,
    ) -> Tuple[
        List[List[str]], List[List[List[List[str]]]], Tuple[Optional[str], Optional[Tuple[float, str]]]
    ]: ...
