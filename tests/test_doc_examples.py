"""Run the documented examples. A doc example is untested code, and these two were wrong while reading fine.

`tests/doc_examples.py` holds the page objects that `README.md` and `docs/PYTHON.md` show; this file feeds
them a response and asserts the item. What it protects is narrow and worth having: that the examples build
(a `field()` on the wrong base raises at class definition) and that `to_item()` returns something the
declared item type can hold (a field the item has no room for raises `TypeError`).
"""

import asyncio

import pytest

pytest.importorskip("web_poet")

from web_poet import HttpResponse  # noqa: E402

from tests.doc_examples import ListingPage, Offer, Product, ProductPage  # noqa: E402

PRODUCT_HTML = (
    b"<html><head><meta itemprop='brand' content='Acme'></head><body>"
    b"<h1>Roomy Bag</h1><span class=price>$24.50</span>"
    b"<img src=/i/1.jpg><img src=/i/2.jpg>"
    b"<ul class=spec><li>Leather</li><li>2 kg</li></ul>"
    b"</body></html>"
)

LISTING_HTML = (
    b"<html><body><h1>Bags</h1>"
    b"<div class=offer><span class=p>$9</span><span class=s>Alice</span></div>"
    b"<div class=offer><span class=p>$11</span><span class=s>Bob</span></div>"
    b"</body></html>"
)


def _resp(body):
    return HttpResponse(url="http://example.com/p/1", body=body)


def test_the_documented_product_page_returns_its_item():
    item = asyncio.run(ProductPage(response=_resp(PRODUCT_HTML)).to_item())
    assert item == Product(
        name="Roomy Bag",
        price="$24.50",
        images=["/i/1.jpg", "/i/2.jpg"],
        specs="Leather 2 kg",
        brand="Acme",
    )


def test_the_documented_grouped_page_returns_its_rows():
    item = asyncio.run(ListingPage(response=_resp(LISTING_HTML)).to_item())
    assert item == {
        "title": "Bags",
        "offers": [Offer(price="$9", seller="Alice"), Offer(price="$11", seller="Bob")],
    }


def test_the_documented_zyte_composition_builds_and_runs():
    """The processors recipe, with the real library. `docs/PYTHON.md` showed a composition that raises."""
    pytest.importorskip("zyte_common_items")
    from zyte_common_items import Product as ZyteProduct

    from tests.doc_examples import zyte_product_page

    html = (
        b"<html><body><h1>Roomy Bag</h1>"
        b"<nav class=crumbs><a href='/c1'>Cat 1</a></nav>"
        b"<div class=desc><p>A roomy bag.</p><script>track()</script></div>"
        b"<span class=rating>3.8 out of 5 stars</span>"
        b"<span class=sku>SKU-1</span><img class=hero src=/i/1.jpg></body></html>"
    )
    item = asyncio.run(zyte_product_page()(response=_resp(html)).to_item())
    assert isinstance(item, ZyteProduct)
    assert item.name == "Roomy Bag"
    assert [b.name for b in item.breadcrumbs] == ["Cat 1"]
    assert "<script>" not in item.descriptionHtml
    assert item.aggregateRating.ratingValue == 3.8


def test_the_documented_out_empty_recipe_declines_an_inherited_processor():
    """The other half of that section: `out=[]` cancels one of the processors the base attaches by name."""
    pytest.importorskip("zyte_common_items")

    from tests.doc_examples import zyte_page_declining_a_processor

    html = b"<html><body><nav class=crumbs><a href='/c1'>Cat 1</a></nav></body></html>"
    item = asyncio.run(zyte_page_declining_a_processor()(response=_resp(html)).to_item())
    # raw HTML, not a List[Breadcrumb] — which is what web-poet itself returns for a declined field
    assert item.breadcrumbs == "<nav class=crumbs><a href='/c1'>Cat 1</a></nav>"
