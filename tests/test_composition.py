"""Value parity across selector context, cardinality and response encodings."""
import pytest
from parsel import Selector

from frostwork import Page, check, extract_grouped

HTML = '''<html><body><article class="card" id="outer">café<h3>A<b>bold</b></h3><div><h3>NEST</h3></div><article class="card" id="inner">inner<h3>B</h3></article><h3>C</h3>tail</article><article class="card" id="empty"></article><img class="card" src="x"></body></html>'''
SUBS = ['@id', './@id', 'text()', './text()', './/text()', './h3/text()', './h3[1]/text()',
        './h3/b/text()', './div/h3/text()', './/h3/text()', './/@id', './*/@id']


@pytest.mark.parametrize('encoding', ['utf-8', 'windows-1252', 'utf-16'])
@pytest.mark.parametrize('container', ['.card', 'article', '//article', 'img'])
def test_group_context_anchors_match_parsel(encoding, container):
    body = HTML.encode(encoding)
    assert check([], [(container, [(str(i), q) for i, q in enumerate(SUBS)])]).ok
    _, groups = extract_grouped(body, [], [(container, [(str(i), q) for i, q in enumerate(SUBS)])], encoding=encoding)
    # Isolate selector semantics; the encoding gate separately checks byte decoding.
    sel = Selector(text=body.decode(encoding))
    nodes = sel.xpath(container) if container.startswith('/') else sel.css(container)
    expected = [[node.xpath(query).getall() for query in SUBS] for node in nodes]
    assert groups[0] == expected
    # Child and descendant paths must remain observably different.
    if container == 'article':
        assert expected[0][5] == ['A', 'C']
        assert expected[0][9] == ['A', 'NEST', 'B', 'C']


def test_context_paths_compose_with_page_group_cardinality():
    page = (Page().field('first', 'h3::text')
            .many('cards', 'article', {'id': '@id', 'title': './h3/text()',
                                     'titles': ('./h3/text()', 'all'), 'text': ('text()', 'join', '|')})
            .one('first_card', 'article', {'id': './@id', 'title': './h3/text()'}))
    item = page.extract(HTML.encode()).to_dict()
    assert item['cards'][0] == {'id': 'outer', 'title': 'A', 'titles': ['A', 'C'], 'text': 'café|tail'}
    assert item['cards'][1]['title'] == 'B'
    assert item['cards'][2]['title'] is None
    assert item['first_card'] == {'id': 'outer', 'title': 'A'}


def test_group_context_node_outer_html_and_empty_attributes():
    html = b'<div id=""><b>one</b><div id="nested">two</div></div>'
    _, (rows,) = extract_grouped(html, [], [('div', [('self', '.'), ('id', '@id'), ('child', './b')])])
    assert rows[0] == [[html.decode()], [''], ['<b>one</b>']]
    assert rows[1] == [['<div id="nested">two</div>'], ['nested'], []]


def test_context_paths_on_webpoet_groups():
    from web_poet import HttpResponse
    from frostwork.webpoet import FrostPage, Many, One, field
    class Cards(FrostPage):
        cards = Many('article', id=field('@id'), titles=field('./h3/text()', all=True))
        first = One('article', id=field('./@id'))
    page = Cards(HttpResponse('https://example.test', body=HTML.encode(), encoding='utf-8'))
    assert page.cards[0] == {'id': 'outer', 'titles': ['A', 'C']}
    assert page.first == {'id': 'outer'}


@pytest.mark.parametrize('name', ['sm:text-lg', 'w-1/2', 'item.foo', 'a,b', 'a>b', 'x*', '(box)',
                                  '1-start', 'café', 'x+y', 'x~y'])
def test_escaped_identifiers_match_in_flat_and_grouped_contexts(name):
    from html import escape
    from frostwork import extract
    escaped = ''.join(f'\\{ord(char):x} ' for char in name)
    body = (f'<section><p class="{escape(name)}" id="{escape(name)}">direct<b>nested</b>tail</p>'
            '<p class="other" id="other">other</p></section>').encode()
    queries = [f'.{escaped}::text', f'#{escaped}::text', f'p:is(.missing, .{escaped})::text',
               f'p:not(.{escaped})::text', f'#{escaped}::attr(id)']
    oracle = Selector(body=body)
    expected = [oracle.css(query).getall() for query in queries]
    assert expected[0] == ['direct', 'tail']
    assert extract(body, queries) == expected
    _, (rows,) = extract_grouped(body, [], [('section', [(str(i), q) for i, q in enumerate(queries)])])
    assert rows == [expected]


@pytest.mark.parametrize('query,name', [(r'.sm\:text-lg::text', 'sm:text-lg'),
                                      (r'.a\,b::text', 'a,b'), (r'.x\*::text', 'x*'),
                                      (r'.x\>::text', 'x>'), (r'#a\ b::text', 'a b')])
def test_literal_css_escapes_are_data_not_combinators(query, name):
    from frostwork import extract
    html = f'<p class="{name}" id="{name}">direct<b>nested</b></p>'.encode()
    assert extract(html, [query]) == [Selector(body=html).css(query).getall()] == [['direct']]


@pytest.mark.parametrize('query', ['.x\\', '.x\\\n::text', r'.x\0::text', r'.a\5c b::text'])
def test_invalid_identifier_escapes_stay_unsupported(query):
    assert not check([query]).ok


@pytest.mark.parametrize('suffix', ['', '<h4>end</h4>'])
@pytest.mark.parametrize('groups', [[], [('article', [('id', '@id'), ('text', './/text()')])]])
def test_first_value_retention_with_full_scan_consumers(suffix, groups):
    from frostwork._frostwork import Plan
    body = ('<article id=""><p id="p" class="x">first<b>nested</b>tail</p>'
            '<p id="second" class="y">second</p></article>' + suffix).encode()
    queries = ['p::text', 'p::attr(id)', 'article::attr(id)', 'p::attr(class), p::attr(id)',
               'p::text, p::attr(id)', 'article ::text', 'h4::text', 'p::text']
    flags = [True] * (len(queries) - 1) + [False]
    plan = Plan(queries, groups, flags)
    cols, rows = plan.extract_grouped(body)
    oracle = Selector(body=body)
    expected = [oracle.css(q).getall()[:1] if first else oracle.css(q).getall()
                for q, first in zip(queries, flags)]
    assert cols == expected
    assert cols[2] == ['']  # an empty first match satisfies the field
    if groups:
        assert rows == [[[[""], ['first', 'nested', 'tail', 'second']]]]


@pytest.mark.parametrize('deferred', ['p:last-child::text', 'p:has(b) ::text',
                                      'p:contains("first")::text', 'normalize-space(//article)',
                                      'p::text, p:contains("first")::text'])
def test_first_value_declaration_does_not_discard_deferred_or_reordered_values(deferred):
    from frostwork._frostwork import Plan
    body = b'<article><p>first<b>nested</b>tail</p><p>last</p></article>'
    queries = [deferred, 'p::text', 'p::attr(id)']
    cols = Plan(queries, [], [True, True, False]).extract(body)
    root = Selector(body=body)
    expected = [root.xpath(deferred).getall() if deferred.startswith('normalize-space') else
                root.css(deferred).getall(), root.css('p::text').getall(), []]
    assert [c[:1] for c in cols] == [c[:1] for c in expected]


def test_first_outer_html_still_uses_start_order_with_nested_matches():
    from frostwork._frostwork import Plan
    body = b'<div id="outer"><div id="inner">x</div>tail</div><p>end</p>'
    cols = Plan(['div', 'p::text'], [], [True, False]).extract(body)
    assert cols[0][0] == '<div id="outer"><div id="inner">x</div>tail</div>'
    assert cols[1] == ['end']


def test_mixed_first_benchmark_extracts_equal_nonempty_values():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
    import ab_bench
    from frostwork._frostwork import Plan
    (_name, body, _counts, pool), = ab_bench.tables(['first-mixed'])
    first = [q.startswith('F ') for q in pool]
    selectors = [q[2:] if flag else q for q, flag in zip(pool, first)]
    actual = Plan(selectors, [], first).extract(body)
    root = Selector(body=body)
    expected = [root.css(q).getall()[:1] if flag else root.css(q).getall()
                for q, flag in zip(selectors, first)]
    assert all(expected) and actual == expected


@pytest.mark.parametrize('query', [r'.\d800::text', r'#\dfff::text', r'[id="\d800"]::text',
                                   r'[id="\0"]::text', r'[class*="\d800"]::text'])
def test_non_executable_css_escapes_cannot_match_replacement_characters(query):
    from frostwork import extract
    body = '<p class="�" id="�">hit</p>'.encode()
    with pytest.raises((UnicodeError, ValueError)):
        Selector(body=body).css(query).getall()
    assert not check([query]).ok
    assert extract(body, [query], strict=False) == [[]]


@pytest.mark.parametrize('name,query', [('a\\b', r'[data-k="a\5c b"]::text'),
                                      ('a\\b', r'[data-k="a\\b"]::text'),
                                      ('\v', r'[data-k="\b"]::text'),
                                      ('\ufffe', r'[data-k="\fffe"]::text')])
def test_quoted_escape_refusals_share_the_identifier_safety_boundary(name, query):
    from html import escape
    from frostwork import extract
    body = f'<p data-k="{escape(name)}">hit</p>'.encode()
    try:
        reference = Selector(body=body).css(query).getall()
    except (UnicodeError, ValueError):
        reference = []
    assert reference == []
    assert not check([query]).ok
    assert extract(body, [query], strict=False) == [[]]
