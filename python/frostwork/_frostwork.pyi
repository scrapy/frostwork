"""Type stubs for the native Frostwork core (`frostwork._frostwork`).

The ergonomic, fully-typed surface is `frostwork.page` / `frostwork.webpoet`; these are the raw
one-pass primitives crossing the PyO3 boundary. `html` must be `bytes` (or a `bytes` subclass such
as web-poet's `HttpResponseBody`); the pure-Python wrappers accept other bytes-likes and `str`.
"""

from typing import List, Optional, Tuple

def extract(
    html: bytes, queries: List[str], encoding: Optional[str] = ...
) -> List[List[str]]:
    """One streaming pass: one value-column per query, in query order."""

def extract_grouped(
    html: bytes,
    flat_queries: List[str],
    groups: List[Tuple[str, List[Tuple[str, str]]]],
    encoding: Optional[str] = ...,
) -> Tuple[List[List[str]], List[List[List[List[str]]]]]:
    """One streaming pass returning ``(flat_columns, grouped)`` — ``grouped[g][row][subfield][value]``."""

def audit_schema(
    flat_queries: List[str],
    groups: List[Tuple[str, List[Tuple[str, str]]]],
) -> Tuple[
    List[Tuple[bool, Optional[str]]],
    List[Tuple[Tuple[bool, Optional[str]], List[Tuple[bool, Optional[str]]]]],
    Tuple[int, int, int, int],
]:
    """Support/reason per selector plus ``(members, max_members, sib_bits, max_sib_bits)`` — no HTML parsed."""

def resolve_label(label: str) -> Optional[str]:
    """Canonical WHATWG encoding name for ``label`` (e.g. ``"UTF-8"``), or ``None`` if unrecognized."""

class Plan:
    """A schema compiled once (budget validated at construction) and reused across pages."""

    def __init__(
        self,
        flat_queries: List[str],
        groups: List[Tuple[str, List[Tuple[str, str]]]],
    ) -> None: ...
    def extract(self, html: bytes, encoding: Optional[str] = ...) -> List[List[str]]: ...
    def extract_grouped(
        self, html: bytes, encoding: Optional[str] = ...
    ) -> Tuple[List[List[str]], List[List[List[List[str]]]]]: ...
