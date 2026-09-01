"""Type-checker fixture — every `FrostPage` declaration form, with its expected STATIC type.

Not run as a test; `tests/test_typing.py` feeds this file to mypy and requires zero errors. `assert_type`
fails at type-check time (never at runtime), so each line below is an assertion about what a user's own
type checker sees. That is the thing being protected: the package ships `py.typed`, so these annotations
are a promise, and only a type checker can hold them: annotating `field()` as the internal `_FrostField`
makes correct code an error in the user's CI while every runtime test here passes.
"""

from typing import Any, Dict, List, Optional

import attrs
from typing_extensions import assert_type

from frostwork.webpoet import FrostBrowserPage, FrostFields, FrostPage, Many, One, field


@attrs.define
class Card:
    title: str
    href: Optional[str]


def _to_float(s: Optional[str]) -> float:
    return float((s or "0").lstrip("$"))


DYNAMIC_ALL: bool = True
DYNAMIC_SEP: Optional[str] = None


class ProductPage(FrostPage):
    # cardinality drives the value type
    name = field("h1::text")
    images = field("img::attr(src)", all=True)
    specs = field(".spec ::text", join=" ")
    # ...and the default has to be SPELLABLE: `all=False`/`join=None` are runtime-valid calls, so a checker
    # that rejects them is the py.typed promise being broken for correct code
    explicit_first = field("h1::text", all=False)
    explicit_nojoin = field("h1::text", join=None)
    # ...including the redundant-but-legal pairings of the two keywords
    all_no_join = field("img::attr(src)", all=True, join=None)
    join_not_all = field(".spec ::text", all=False, join=" ")
    # cardinality decided at runtime cannot have a static value type; `Any` is the honest answer
    dynamic = field("h1::text", all=DYNAMIC_ALL)
    dynamic_join = field("h1::text", join=DYNAMIC_SEP)
    # `.as_node()`/`.as_value()` do not change the field's value type for a checker: what a processor returns
    # is opaque, and pretending otherwise (`Any`) would make a node-taking `.map()` type-check even though the
    # runtime refuses it. Narrow the processor's output with `typed_as` where it matters.
    noded = field(".crumbs", out=[lambda v, page: [Card(title="x", href=None)]]).as_node()
    valued = field(".crumbs", out=[lambda v, page: v]).as_value()
    noded_typed = field(".crumbs", out=[lambda v, page: []]).as_node().typed_as(List[Card])
    # transforms follow the callable's return type
    price = field(".price::text").map(_to_float)
    symbol = field(".price::text").re_first(r"^\D+")
    chained = field(".price::text").map(_to_float).map(int)
    # web-poet's own keywords are accepted and do not disturb the value type
    cached = field(".sku::text", cached=True)
    tagged = field(".sku::text", meta={"expensive": True})
    # A processor is an opaque callable attached at runtime (often by NAME from a base page's `Processors`),
    # so the overloads describe the value BEFORE it runs. `typed_as` is how a declaration says what the
    # processor actually produces — without it, a field yielding `List[Card]` types as `str | None`.
    processed = field(".crumbs", out=[lambda v, page: v])
    typed_processed = field(".crumbs", out=[lambda v, page: [Card(title="x", href=None)]]).typed_as(
        List[Card]
    )
    # ...and a processor's output is very often a UNION, in either spelling. `typed_as` was annotated
    # `Type[U]` — the type of a CLASS OBJECT — so both of these were an error in the user's CI while every
    # test here passed.
    typed_optional = field(".price::text", out=[lambda v, page: v]).typed_as(Optional[str])
    typed_union = field(".price::text", out=[lambda v, page: v]).typed_as(str | None)
    typed_scalar = field(".price::text", out=[lambda v, page: 1.0]).typed_as(float)
    # groups: `item=` gives the item type, without it a row dict
    cards = Many(".card", item=Card, title=field("h3::text"), href=field("a::attr(href)"))
    rows = Many(".card", title=field("h3::text"))
    lead = One(".card", item=Card, title=field("h3::text"), href=field("a::attr(href)"))
    lead_row = One(".card", title=field("h3::text"))


def check_instance_types(p: ProductPage) -> None:
    assert_type(p.name, Optional[str])
    assert_type(p.images, List[str])
    assert_type(p.specs, str)
    assert_type(p.explicit_first, Optional[str])
    assert_type(p.explicit_nojoin, Optional[str])
    assert_type(p.all_no_join, List[str])
    assert_type(p.join_not_all, str)
    assert_type(p.dynamic, Any)
    assert_type(p.dynamic_join, Any)
    assert_type(p.noded, Optional[str])
    assert_type(p.valued, Optional[str])
    assert_type(p.noded_typed, List[Card])
    assert_type(p.price, float)
    assert_type(p.symbol, Optional[str])
    assert_type(p.chained, int)
    assert_type(p.cached, Optional[str])
    assert_type(p.tagged, Optional[str])
    # the pre-processor type, which is what the overloads can know...
    assert_type(p.processed, Optional[str])
    # ...and the declared one, which is what the field really produces
    assert_type(p.typed_processed, List[Card])
    assert_type(p.typed_optional, Optional[str])
    assert_type(p.typed_union, Optional[str])
    assert_type(p.typed_scalar, float)
    assert_type(p.cards, List[Card])
    assert_type(p.rows, List[Dict[str, Any]])
    assert_type(p.lead, Optional[Card])
    assert_type(p.lead_row, Optional[Dict[str, Any]])


def check_assignments_are_legal(p: ProductPage) -> None:
    """The other direction: correct runtime code must not be a type ERROR — `x: str = p.name` against an
    annotation of `_FrostField` is the shape that fails."""
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
