"""Auditing and extraction must normalize the same schema without interpreting names as selectors."""
import pytest

from frostwork import Item, Page, check, extract_grouped


@pytest.mark.parametrize('group', [
    'ab', ('div', ['ab']), ('div', [None]), ('div', [("text", 1)]),
    (None, []), ('div', {'text': 1}),
])
@pytest.mark.parametrize('strict', [False, True])
def test_bad_group_shapes_fail_before_auditing_or_extracting(group, strict):
    # A two-character string used to unpack as (name, selector), auditing/extracting unrelated data.
    with pytest.raises(TypeError, match='each group must be'):
        check([], [group])
    with pytest.raises(TypeError, match='each group must be'):
        extract_grouped(b'<div><b>x</b></div>', [], [group], strict=strict)


@pytest.mark.parametrize('subfields', [
    lambda: {'title': './b/text()', 'id': '@id'},
    lambda: [['title', './b/text()'], ['id', '@id']],
    lambda: ((name, sel) for name, sel in [('title', './b/text()'), ('id', '@id')]),
])
def test_group_subfields_keep_names_selectors_and_order(subfields):
    report = check([], [('div', subfields())])
    assert report.ok
    assert [(s.name, s.selector) for s in report.groups[0].subfields] == [
        ('title', './b/text()'), ('id', '@id')]
    assert extract_grouped(b'<div id=one><b>x</b></div>', [], [('div', subfields())]) == (
        [], [[[["x"], ["one"]]]])


def test_page_audit_and_export_name_the_same_mixed_schema():
    page = (Page(strict=False).field('title', 'h1::text').field_all('missing', '//a/following::b')
            .many('cards', 'article', {'title': './h2/text()', 'bad': 'a + b::text'})
            .one('first', 'aside', {'title': 'text()'}))
    schema = page.frost_schema()
    assert page.check() == check(schema['fields'], schema['groups'])
    assert [field.name for field in page.check().unsupported] == ['missing', 'bad']


@pytest.mark.parametrize('method', ['many', 'one'])
@pytest.mark.parametrize('spec', [(), 0, None, {'p::text': 'all'}, (0,),
                                 ('p::text', 'all', 'ignored'), ('p::text', 'first', 'ignored'),
                                 ('p::text', 'join', False), ('p::text', 'join', '|', 'ignored')])
def test_malformed_group_cardinality_is_rejected_before_extraction(method, spec):
    page = Page()
    with pytest.raises(TypeError, match='many/one sub-spec'):
        getattr(page, method)('rows', 'div', {'name': spec})
    assert len(page) == 0  # a rejected declaration cannot leave a partial schema


@pytest.mark.parametrize('spec,expected', [
    ('p::text', 'a'), (('p::text',), 'a'), (['p::text', 'first'], 'a'),
    (('p::text', 'all'), ['a', 'b']), (('p::text', 'join'), 'ab'),
    (('p::text', 'join', '|'), 'a|b'),
])
def test_valid_group_cardinality_forms_preserve_their_values(spec, expected):
    assert Page().one('row', 'div', {'name': spec}).extract(b'<div><p>a</p><p>b</p></div>').to_dict() == {
        'row': {'name': expected},
    }


def test_items_keep_their_schema_when_the_page_is_extended():
    page = Page().field('title', 'h1::text', map=str.upper)
    first = page.extract(b'<h1>first</h1>')
    page.field_all('tags', 'i::text').one('card', 'article', {'title': './h2/text()'})
    second = page.extract(b'<h1>second</h1><i>a</i><i>b</i><article><h2>card</h2></article>')
    assert first.to_dict() == {'title': 'FIRST'}
    assert first.get('tags') is None and first.get_all('tags') == []
    assert len(first) == 1 and first.validate(required=['title']).ok
    with pytest.raises(ValueError, match='unknown validation fields'):
        first.validate(required=['tags'])
    assert second.to_dict() == {'title': 'SECOND', 'tags': ['a', 'b'], 'card': {'title': 'card'}}
    assert second.validate(counts={'tags': (2, 2)}, group_required={'card': ['title']}).ok
    # Editing an exported audit snapshot or a returned column cannot mutate the schema or raw item.
    page.frost_schema()['fields']['title'] = 'unsupported'
    second.get_all('tags').clear()
    assert second.get_all('tags') == ['a', 'b']
    assert page.extract(b'<h1>third</h1>').value('title') == 'THIRD'


def test_direct_item_construction_keeps_cardinality_and_transforms():
    item = Item(['title', 'tags', 'joined'], [('first', None), ('all', None), ('join', '|')],
                [['x', 'y'], ['a', 'b'], ['a', 'b']], [(str.upper,), (), ()], {'row': {}},
                selectors=['h1::text', 'i::text', 'i::text'])
    assert item.to_dict() == {'title': 'X', 'tags': ['a', 'b'], 'joined': 'a|b', 'row': {}}
    assert item.get_all('title') == ['x'] and item.get('title') == 'x'
    assert item.get('row') == {} and item.get_all('row') == [{}]
    assert item.empty_fields() == []
    assert item.value('missing') is None


@pytest.mark.parametrize('overrides', [
    {'names': ['a', 'a']}, {'cards': [('first', None)]}, {'cols': [['x']]},
    {'transforms': [()]}, {'selectors': ['p::text']},
])
def test_inconsistent_item_metadata_cannot_silently_drop_columns(overrides):
    args = dict(names=['a', 'b'], cards=[('first', None)] * 2, cols=[['x'], ['y']])
    with pytest.raises(ValueError, match='Item'):
        Item(**{**args, **overrides})
