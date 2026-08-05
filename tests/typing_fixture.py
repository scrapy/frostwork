"""Type-checker fixture — every `FrostPage` declaration form, with its expected STATIC type.

Not run as a test; `tests/test_typing.py` feeds this file to mypy and requires zero errors. `assert_type`
fails at type-check time (never at runtime), so each line below is an assertion about what a user's own
type checker sees. That is the thing being protected: the package ships `py.typed`, so these annotations
are a promise, and `field()` previously annotated as `_FrostField` — making correct code an error in the
user's CI while every test here passed.
"""

from typing import Any, Dict, List, Optional, assert_type

import attrs

from frostwork.webpoet import FrostBrowserPage, FrostFields, FrostPage, Many, One, field


@attrs.define
class Card:
    title: str
    href: Optional[str]


def _to_float(s: Optional[str]) -> float:
    return float((s or "0").lstrip("$"))


class ProductPage(FrostPage):
    # cardinality drives the value type
    name = field("h1::text")
    images = field("img::attr(src)", all=True)
    specs = field(".spec ::text", join=" ")
    # transforms follow the callable's return type
    price = field(".price::text").map(_to_float)
    symbol = field(".price::text").re_first(r"^\D+")
    chained = field(".price::text").map(_to_float).map(int)
    # web-poet's own keywords are accepted and do not disturb the value type
    cached = field(".sku::text", cached=True)
    tagged = field(".sku::text", meta={"expensive": True})
    processed = field(".crumbs", out=[lambda v, page: v])
    # groups: `item=` gives the item type, without it a row dict
    cards = Many(".card", item=Card, title=field("h3::text"), href=field("a::attr(href)"))
    rows = Many(".card", title=field("h3::text"))
    lead = One(".card", item=Card, title=field("h3::text"), href=field("a::attr(href)"))
    lead_row = One(".card", title=field("h3::text"))


def check_instance_types(p: ProductPage) -> None:
    assert_type(p.name, Optional[str])
    assert_type(p.images, List[str])
    assert_type(p.specs, str)
    assert_type(p.price, float)
    assert_type(p.symbol, Optional[str])
    assert_type(p.chained, int)
    assert_type(p.cached, Optional[str])
    assert_type(p.tagged, Optional[str])
    assert_type(p.processed, Optional[str])
    assert_type(p.cards, List[Card])
    assert_type(p.rows, List[Dict[str, Any]])
    assert_type(p.lead, Optional[Card])
    assert_type(p.lead_row, Optional[Dict[str, Any]])


def check_assignments_are_legal(p: ProductPage) -> None:
    """The other direction: correct runtime code must not be a type ERROR. This is the shape that used to
    fail — `x: str = p.name` against an annotation of `_FrostField`."""
    a: Optional[str] = p.name
    b: List[str] = p.images
    c: str = p.specs
    d: float = p.price
    e: List[Card] = p.cards
    f: Optional[Card] = p.lead
    g: int = p.chained
    del a, b, c, d, e, f, g


class BrowserProductPage(FrostBrowserPage):
    """The browser base carries the same field typing."""

    name = field("h1::text")


def check_browser_types(p: BrowserProductPage) -> None:
    assert_type(p.name, Optional[str])


@attrs.define
class BytesPage(FrostFields):
    """A custom input via the public hook still gets typed fields."""

    raw: bytes

    def frostwork_input(self) -> "tuple[bytes, Optional[str]]":
        return self.raw, None


class CustomPage(BytesPage):
    title = field("h1::text")


def check_custom_types(p: CustomPage) -> None:
    assert_type(p.title, Optional[str])
