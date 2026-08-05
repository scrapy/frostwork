"""The page objects the docs show, as real code — imported and run by `tests/test_doc_examples.py`.

A documented example is untested code unless something executes it, and this one had shipped broken twice:
the README/`docs/PYTHON.md` product page declared a field its `Returns[...]` item had no room for (so
`to_item()` raised `TypeError`), and the processor recipe named a base composition that raises at class
definition. Both were correct-looking prose.

So the examples live here and the docs point at this file. If you change one, change it here first and let
the suite tell you whether it works.
"""

from __future__ import annotations

from typing import List, Optional

import attrs
from web_poet import Returns

from frostwork.webpoet import FrostPage, Many, field


# --------------------------------------------------------------------------- README / PYTHON.md §3
@attrs.define
class Product:
    name: Optional[str]
    price: Optional[str]
    images: List[str]
    specs: str
    brand: Optional[str]


class ProductPage(FrostPage, Returns[Product]):
    """Every selector below is answered by ONE streaming pass over the response body."""

    name = field("h1::text")
    price = field(".price::text")
    images = field("img::attr(src)", all=True)  # -> list
    specs = field(".spec ::text", join=" ")  # -> joined str
    brand = field("//meta[@itemprop='brand']/@content")  # XPath works too


# --------------------------------------------------------------------------- PYTHON.md, Many/One
@attrs.define
class Offer:
    price: Optional[str]
    seller: Optional[str]


class ListingPage(FrostPage):
    title = field("h1::text")
    offers = Many(".offer", item=Offer, price=field(".p::text"), seller=field(".s::text"))


# --------------------------------------------------------------------------- PYTHON.md, processors
def zyte_product_page():
    """The zyte-common-items composition, built lazily so the module imports without zyte installed.

    `FrostPage` must come FIRST: `field()` leaves a marker that `FrostFields.__init_subclass__` converts, so
    `class MyProductPage(ProductPage)` alone raises at class definition."""
    from zyte_common_items.pages import ProductPage as ZyteProductPage

    class MyProductPage(FrostPage, ZyteProductPage):
        name = field("h1::text")
        breadcrumbs = field(".crumbs")  # -> breadcrumbs_processor gets the <nav> node
        descriptionHtml = field(".desc")  # -> description_html_processor, clear-html runs
        aggregateRating = field(".rating")  # -> rating_processor gets the <span>
        images = field("img.hero::attr(src)", all=True)  # a SCALAR terminal: stays a list of str

    return MyProductPage


def zyte_page_declining_a_processor():
    """The `out=[]` recipe: decline one of the nine processors a zyte base attaches BY FIELD NAME, and the
    field yields the element's raw HTML exactly as it would with no `Processors` entry anywhere."""
    from zyte_common_items.pages import ProductPage as ZyteProductPage

    class RawCrumbsPage(FrostPage, ZyteProductPage):
        breadcrumbs = field(".crumbs", out=[])

    return RawCrumbsPage
