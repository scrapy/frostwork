"""Source audits must respect grouped compilation and expose unverified input."""
import json

import pytest

import frostwork
from frostwork.audit import main
from frostwork.scan import judge, scan_source


@pytest.mark.parametrize('source,query,context', [
    ('Page().many("cards", ".card", {"title": "h2::text, h3::text"})', 'h2::text, h3::text', 'group-subfield'),
    ('Page().one("card", "article, section", {"title": "h2::text"})', 'article, section', 'group-container'),
    ('cards = Many(".card", title=field("h2::text, h3::text").map(str))', 'h2::text, h3::text', 'group-subfield'),
    ('cards = One(".card", title=field("normalize-space(.//h2)"))', 'normalize-space(.//h2)', 'group-subfield'),
    ('Page().many("cards", ".card", {"title": ("h2 + h3::text", "all")})', 'h2 + h3::text', 'group-subfield'),
    ('Page().many("cards", ".card", {"title": ["h2 + h3::text", "all"]})', 'h2 + h3::text', 'group-subfield'),
])
def test_scan_uses_the_group_compiler(source, query, context):
    verdict = next(v for v in judge(scan_source(source, 'spider.py')) if v.site.selector == query)
    assert verdict.site.context == context
    assert not verdict.supported
    assert frostwork.check([query]).ok
    assert 'group' in verdict.reason


@pytest.mark.parametrize('query,fragment', [
    ('h2::text, h3::text', 'single selector'),
    ('normalize-space(.//h2)', 'flat fields'),
    ('h2 + h3::text', 'sibling combinator'),
])
def test_group_rejection_names_the_context(query, fragment):
    report = frostwork.check([], [('article', [('title', query)])])
    assert fragment in report.groups[0].subfields[0].reason


@pytest.mark.parametrize('source', [
    'response.css(query)',
    'Page().many("cards", ".card", subfields)',
    'cards = Many(".card", **fields)',
    'cards = Many(".card", title=TITLE_FIELD)',
    '# no selector sites',
])
def test_complete_scan_cannot_pass_without_auditing_every_site(tmp_path, capsys, source):
    target = tmp_path / 'spider.py'
    target.write_text(source)
    assert main(['--scan', str(target), '--json', '--require-complete']) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report['ok']
    assert not report['summary']['complete']
    if source in ('response.css(query)', '# no selector sites'):
        assert report['summary']['coverage'] is None


def test_dynamic_only_text_does_not_claim_full_coverage(tmp_path, capsys):
    target = tmp_path / 'spider.py'
    target.write_text('response.css(query)')
    assert main(['--scan', str(target)]) == 0  # the stronger CI policy is opt-in
    output = capsys.readouterr().out
    assert 'coverage unknown' in output
    assert '100%' not in output


def test_complete_scan_accepts_audited_literals(tmp_path, capsys):
    target = tmp_path / 'spider.py'
    target.write_text('response.css("h1::text")')
    assert main(['--scan', str(target), '--require-complete', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['summary']['complete']


def test_group_context_follows_the_marker_not_its_callback():
    source = 'Many("article", title=field("./h2/text()").map(lambda v: field("h1::text, h2::text")))'
    verdicts = {v.site.selector: v for v in judge(scan_source(source, 'spider.py'))}
    assert verdicts['./h2/text()'].site.context == 'group-subfield'
    assert verdicts['./h2/text()'].supported
    assert verdicts['h1::text, h2::text'].site.context == 'flat'
    assert verdicts['h1::text, h2::text'].supported


@pytest.mark.parametrize('value', [
    'factory(field("h2::text"))',
    'field("h2::text") if condition else EXISTING_FIELD',
    'EXISTING_FIELD.map(lambda value: field("h2::text"))',
])
def test_a_nested_field_call_does_not_prove_a_group_schema_is_complete(tmp_path, capsys, value):
    target = tmp_path / 'spider.py'
    target.write_text(f'Many("article", title={value})')
    assert main(['--scan', str(target), '--require-complete', '--json']) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report['summary']['complete']
    assert any(s['context'] == 'group-schema' and s['supported'] is None for s in report['sites'])


def test_scan_covers_every_public_field_modifier():
    from frostwork.scan import FIELD_MODIFIERS
    from frostwork.webpoet import _FrostField

    public = {name for name, value in vars(_FrostField).items()
              if not name.startswith('_') and callable(value)}
    assert FIELD_MODIFIERS == public
    chain = ''.join(f'.{name}(argument)' for name in sorted(public))
    sites = scan_source(f'Many("article", title=field("./h2/text()"){chain})', 'spider.py')
    assert next(s for s in sites if s.selector == './h2/text()').context == 'group-subfield'
