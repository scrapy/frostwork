"""Run after `make bootstrap && make py`: `.venv/bin/python examples/basic.py`."""

from frostwork import Page

html = b"""
<main>
  <h1>Widget</h1>
  <span class="price">$9</span>
  <img src="/a.png"><img src="/b.png">
</main>
"""

page = (
    Page()
    .field("title", "h1::text")
    .field("price", ".price::text")
    .field_all("images", "img::attr(src)")
)
page.check().raise_for_status()
print(page.extract(html).to_json(indent=2))
