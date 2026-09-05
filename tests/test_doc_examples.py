"""Execute the complete examples marked with ``<!-- doc-test: NAME -->`` in the public docs."""

import asyncio
import re
from pathlib import Path

import pytest

pytest.importorskip("web_poet")

from web_poet import HttpResponse  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "docs" / "PYTHON.md"]

EXPECTED_EXAMPLES = {"frost-page", "product-page", "zyte-product-page", "runtime-validation"}

PRODUCT_HTML = (
    b"<html><head><meta itemprop='brand' content='Acme'></head><body>"
    b"<h1>Roomy Bag</h1><span class=price>$24.50</span>"
    b"<img src=/i/1.jpg><img src=/i/2.jpg>"
    b"<ul class=spec><li>Leather</li><li>2 kg</li></ul>"
    b"<nav class=crumbs><a href='/c1'>Cat 1</a></nav>"
    b"<div class=desc><p>A roomy bag.</p><script>track()</script></div>"
    b"<span class=rating>3.8 out of 5 stars</span>"
    b"<img class=hero src=/i/hero.jpg>"
    b"</body></html>"
)


def _resp(body=PRODUCT_HTML):
    return HttpResponse(url="http://example.com/p/1", body=body)


def marked_blocks():
    """``[(id, source, code)]`` for each explicitly marked complete example."""
    out = []
    for doc in DOCS:
        pattern = r"<!--\s*doc-test:\s*([a-z0-9-]+)\s*-->\s*```python\n(.*?)```"
        for example_id, block in re.findall(pattern, doc.read_text(), re.S):
            out.append((example_id, f"{doc.name}:{example_id}", block))
    return out


def _run(block: str, source: str) -> dict:
    """Execute one block in a namespace of its own, under a MODULE NAME.

    The name is what lets :func:`_page_objects` tell the classes a block DEFINES from the ones it imports:
    a class takes `__module__` from the namespace's `__name__`, and without one they all read `builtins`."""
    namespace: dict = {"__name__": f"doc_example_{re.sub(r'[^A-Za-z0-9]', '_', source)}"}
    try:
        exec(compile(block, f"<{source}>", "exec"), namespace)  # noqa: S102 - executing the docs is the point
    except Exception as exc:  # noqa: BLE001 - report which doc broke, not just that one did
        pytest.fail(f"{source}: a marked block does not execute: {type(exc).__name__}: {exc}")
    return namespace


def _page_objects(namespace: dict) -> list:
    """The page-object classes a block DEFINED — not the bases it imported to define them."""
    from frostwork.webpoet import FrostFields

    return [
        obj
        for obj in namespace.values()
        if isinstance(obj, type)
        and issubclass(obj, FrostFields)
        and obj.__module__ == namespace["__name__"]
    ]


def test_every_marked_documentation_block_builds_and_produces_an_item():
    """Each marked block runs as written, and every page object it defines produces an item.

    That is the whole guarantee, and it is the one the earlier comparison lacked: imports resolve, the bases
    compose, the class-definition checks pass, and the declared fields fit whatever `Returns[...]` says."""
    blocks = marked_blocks()
    ids = [example_id for example_id, _source, _block in blocks]
    assert len(ids) == len(set(ids)), f"duplicate documentation example ids: {ids}"
    assert set(ids) == EXPECTED_EXAMPLES, f"documentation example ids changed: {ids}"

    for _example_id, source, block in blocks:
        namespace = _run(block, source)
        if _example_id == "runtime-validation":
            assert namespace["report"].ok
            assert namespace["report"].item["offers"] == [{"price": "9"}]
            continue
        pages = _page_objects(namespace)
        assert pages, f"{source}: a marked block defines no page object"
        for page_cls in pages:
            item = asyncio.run(page_cls(response=_resp()).to_item())
            assert item is not None, f"{source}: {page_cls.__name__}.to_item() returned nothing"


def test_the_documented_product_pages_return_the_documented_values():
    """Pin values for both the compact README page and the typed guide page."""
    blocks = {example_id: (source, block) for example_id, source, block in marked_blocks()}
    source, block = blocks["frost-page"]
    ns = _run(block, source)
    assert asyncio.run(ns["ProductPage"](response=_resp()).to_item()) == {
        "name": "Roomy Bag",
        "price": "$24.50",
        "images": ["/i/1.jpg", "/i/2.jpg", "/i/hero.jpg"],
        "specs": "Leather 2 kg",
        "brand": "Acme",
    }

    source, block = blocks["product-page"]
    ns = _run(block, source)
    assert asyncio.run(ns["ProductPage"](response=_resp()).to_item()) == ns["Product"](
        name="Roomy Bag",
        price="$24.50",
        images=["/i/1.jpg", "/i/2.jpg", "/i/hero.jpg"],
        specs="Leather 2 kg",
        brand="Acme",
    )


def test_the_documented_zyte_composition_runs_the_real_processors():
    """The processors recipe against the real library, which is where `.as_node()` earns its keep: every one of
    these processors returns its input UNCHANGED if handed a string."""
    pytest.importorskip("zyte_common_items")
    from zyte_common_items import Image, Product

    blocks = {example_id: (source, block) for example_id, source, block in marked_blocks()}
    source, block = blocks["zyte-product-page"]
    ns = _run(block, source)

    item = asyncio.run(ns["MyProductPage"](response=_resp()).to_item())
    assert isinstance(item, Product)
    assert item.name == "Roomy Bag"
    assert [b.name for b in item.breadcrumbs] == ["Cat 1"]   # breadcrumbs_processor ran on the node
    assert "<script>" not in item.descriptionHtml            # clear-html ran
    assert item.aggregateRating.ratingValue == 3.8           # rating_processor ran
    assert item.images == [Image(url="/i/hero.jpg")]         # a scalar terminal: URL strings, not a node
