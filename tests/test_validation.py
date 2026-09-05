"""Runtime checks must catch missing values without confusing them with false or zero."""
from collections import Counter
from types import SimpleNamespace

import pytest

from frostwork import FieldProcessingError, ItemValidationError, Page, check


def test_response_adapter_preserves_bytes_and_encoding():
    class Response:
        body = '<h1>café</h1>'.encode('windows-1252')
        encoding = 'windows-1252'
        @property
        def text(self):
            raise AssertionError('must not decode or construct a selector')
    page = Page().field('title', 'h1::text')
    assert page.extract_response(Response()).value('title') == 'café'
    assert page.extract_response(SimpleNamespace(body=b'<h1>x</h1>', encoding=None)).value('title') == 'x'
    bad = Page().field('x', '//a/following::b')
    assert bad.extract_response(Response(), strict=False).value('x') is None
    assert check(**{'queries': page.frost_schema()['fields'], 'groups': page.frost_schema()['groups']}).ok


def test_validation_distinguishes_raw_and_processed_values():
    calls = []
    def fail(value):
        calls.append(value)
        raise RuntimeError('conversion failed')
    page = (Page().field('missing', 'h2::text').field('empty', 'a::attr(href)')
            .field('stripped', 'p::text', map=str.strip)
            .field('failed', 'p::text', map=fail)
            .field('zero', 'i::text', map=int)
            .field('false', 'i::text', map=lambda value: False)
            .field('default', 'h2::text', map=lambda value: 'default'))
    item = page.extract(b'<a href=""></a><p> </p><i>0</i>')
    report = item.validate(required=['missing', 'empty', 'stripped', 'zero', 'false', 'default'])
    assert report.states == {'missing': 'no_match', 'empty': 'matched_empty',
                             'stripped': 'processed_empty', 'failed': 'processing_failed',
                             'zero': 'filled', 'false': 'filled', 'default': 'no_match'}
    assert {issue.field for issue in report.issues} == {'missing', 'empty', 'stripped', 'failed'}
    assert report.item['zero'] == 0 and report.item['false'] is False
    assert 'failed' not in report.item
    assert len(calls) == 1
    with pytest.raises(ItemValidationError) as caught:
        report.raise_for_status()
    assert caught.value.report is report
    with pytest.raises(FieldProcessingError) as caught:
        item.to_dict()
    assert caught.value.field == 'failed' and caught.value.selector == 'p::text'
    assert isinstance(caught.value.__cause__, RuntimeError)


@pytest.mark.parametrize('method', ['field_all', 'field_join'])
def test_count_checks_use_raw_matches_even_after_a_transform(method):
    page = getattr(Page(), method)('images', 'img::attr(src)', map=lambda value: 'one string')
    item = page.extract(b'<img src=a><img src=""><img src=b>')
    assert item.validate(counts={'images': (3, 3)}).ok
    assert not item.validate(counts={'images': (1, 2)}).ok
    assert item.validate(required=['images']).ok


def test_group_validation_names_the_row_and_bounds_stats_keys():
    page = (Page().many('offers', 'article', {'price': './b/text()', 'tags': ('./i/text()', 'all')})
            .one('first', 'article', {'price': './b/text()'}))
    item = page.extract(b'<article><b>9</b><i>a</i></article><article></article>')
    report = item.validate(required=['offers'], counts={'offers': (1, 5)},
                           group_required={'offers': ['price', 'tags'], 'first': ['price']})
    assert [i.field for i in report.issues] == ['offers[1].price', 'offers[1].tags']
    class Stats:
        def __init__(self): self.values = Counter()
        def inc_value(self, key, count=1): self.values[key] += count
    stats = Stats()
    report.record_stats(stats)
    assert stats.values['frostwork/invalid'] == 1
    assert stats.values['frostwork/issues/required'] == 2
    assert not any('[1]' in key for key in stats.values)
    empty = page.extract(b'')
    assert empty.validate(group_required={'offers': ['price']}).ok
    assert not empty.validate(required=['offers']).ok
    with pytest.raises(ValueError, match='unknown subfields'):
        empty.validate(group_required={'offers': ['typo']})


@pytest.mark.parametrize('kind', ['field', 'one'])
def test_count_checks_reject_first_only_declarations(kind):
    page = (Page().field('x', 'p::text') if kind == 'field' else
            Page().one('x', 'p', {'text': 'text()'}))
    item = page.extract(b'<p>a</p><p>b</p>')
    with pytest.raises(ValueError, match='first-only'):
        item.validate(counts={'x': (1, 1)})


@pytest.mark.parametrize('kwargs', [dict(required=['typo']), dict(counts={'x': (-1, 2)}),
                                    dict(counts={'x': (2, 1)}), dict(counts={'x': (True, 2)}),
                                    dict(group_required={'x': ['text']})])
def test_invalid_validation_rules_fail_before_processing(kwargs):
    def unexpected(value): raise AssertionError('invalid rules must not execute transforms')
    item = Page().field_all('x', 'p::text', map=unexpected).extract(b'<p>x</p>')
    with pytest.raises(ValueError):
        item.validate(**kwargs)


def test_an_empty_row_is_still_a_matched_container():
    item = Page().one('one', 'div', {}).many('many', 'div', {}).extract(b'<div></div>')
    assert item.empty_fields() == []
    report = item.validate(required=['one', 'many'])
    assert report.ok and report.states == {'one': 'filled', 'many': 'filled'}
