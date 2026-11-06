"""Import-safe complete schemas for the redistributable migration fixtures."""
from frostwork import Page

# lxml mode: tools/verify_migration.py grades against Parsel.
REGISTRY = {
    'product': (Page(parser="lxml").field('name', 'h1::text', map=str.strip)
                .field('price', '.price::text')
                .field_all('images', 'img::attr(src)')
                .field_join('description', '.description ::text', separator='')
                .one('seller', '.seller', {'name': './b/text()', 'url': './a/@href'})),
    'listing': (Page(parser="lxml").field('title', 'h1::text')
                .many('products', 'article', {'id': '@id', 'name': './h2/text()',
                                              'tags': ('./ul/li/text()', 'all')})
                .field_all('links', r'.sm\:link::attr(href)')),
    'article': (Page(parser="lxml").field('title', 'h1::text')
                .field_join('body', '.body ::text', separator='')
                .field_all('terms', 'dt::text').field_all('definitions', 'dd::text')),
}
