"""Whole-schema migration parity must fail on item loss, ordering, oracle errors and missing fixtures."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from frostwork import Page

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import verify_migration as vm

FIXTURES = ROOT / 'tests' / 'migration'


def registry():
    return vm._load_module(str(FIXTURES / 'pages.py')).REGISTRY


def test_saved_bytes_and_complete_schemas_agree():
    report = vm.verify(registry(), FIXTURES / 'manifest.json', benchmark=True, rounds=2, iterations=1)
    assert report['ok']
    assert report['complete_schemas'] == len(registry())
    assert all(row['ok'] and 'timing' in row for row in report['responses'])
    assert len(report['environment']['extension_sha256']) == 64
    assert report['responses'][-1]['frostwork_encoding'] == 'windows-1252'


@pytest.mark.parametrize('actual,expected,kind', [
    ({}, {'empty': ''}, 'missing'), ({'empty': ''}, {}, 'extra'),
    (['a', 'b'], ['b', 'a'], 'reordered'), ([''], [], 'extra'),
    (['a', 'a'], ['a'], 'extra'), ({'n': 0}, {'n': False}, 'changed'),
    ('a  b', 'a b', 'changed'), (['x'], ['y'], 'changed'),
    ([{'n': 0}], [{'n': False}], 'changed'), ([1], [1.0], 'changed'),
])
def test_exact_migration_differences_preserve_keys_values_and_order(actual, expected, kind):
    assert vm.differences(actual, expected)[0]['kind'] == kind


@pytest.mark.parametrize('defect', ['missing_key', 'wrong_value', 'oracle_error'])
def test_migration_gate_goes_red_and_refuses_timing(monkeypatch, defect):
    original = vm.oracle
    class Broken:
        def __init__(self, item): self.item = item
        def to_dict(self):
            value = self.item.to_dict()
            name = next(iter(value))
            if defect == 'missing_key': value.pop(name)
            else: value[name] = 'regression'
            return value
        def get_all(self, name): return self.item.get_all(name)
    def broken(*args):
        if defect == 'oracle_error': raise ValueError('oracle refused')
        return Broken(original(*args))
    monkeypatch.setattr(vm, 'oracle', broken)
    monkeypatch.setattr(vm, 'timed_pair', lambda *args: pytest.fail('timed a failing schema'))
    report = vm.verify(registry(), FIXTURES / 'manifest.json', benchmark=True)
    assert not report['ok'] and report['complete_schemas'] == 0
    assert 'benchmark_skipped' in report


def test_untested_schema_is_not_counted_as_migratable():
    pages = registry()
    pages['untested'] = Page().field('name', 'h1::text')
    report = vm.verify(pages, FIXTURES / 'manifest.json')
    assert not report['ok']
    assert report['complete_schemas'] == len(pages) - 1


def test_manifest_requires_original_bytes(tmp_path):
    doc = json.loads((FIXTURES / 'manifest.json').read_text())
    for row in doc['responses']: row['file'] = str(FIXTURES / row['file'])
    doc['responses'][0]['sha256'] = '0' * 64
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match='hash mismatch'):
        vm.verify(registry(), path)


def test_cli_writes_reviewable_report_and_returns_failure(tmp_path):
    target = tmp_path / 'pages.py'
    target.write_text('from frostwork import Page\nREGISTRY = {name: Page().field("x", "//a/following::b") '
                      'for name in ["product", "listing", "article"]}\n')
    output = tmp_path / 'report.json'
    run = subprocess.run([sys.executable, str(ROOT / 'tools' / 'verify_migration.py'), f'{target}:REGISTRY',
                          str(FIXTURES / 'manifest.json'), '--json', str(output), '--benchmark'],
                         capture_output=True, text=True)
    assert run.returncode == 1, run.stderr
    report = json.loads(output.read_text())
    assert not report['ok'] and report['complete_schemas'] == 0
    assert all('timing' not in row for row in report['responses'])


def test_the_cli_imports_the_schema_module_only_once(tmp_path):
    marker = tmp_path / 'imports.txt'
    target = tmp_path / 'pages.py'
    target.write_text('from pathlib import Path\nfrom frostwork import Page\n'
                      f'p = Path({str(marker)!r})\np.write_text(p.read_text() + "x" if p.exists() else "x")\n'
                      'REGISTRY = {name: Page().field("title", "h1::text") '
                      'for name in ["product", "listing", "article"]}\n')
    assert vm.main([f'{target}:REGISTRY', str(FIXTURES / 'manifest.json'), '--json',
                    str(tmp_path / 'result.json')]) == 0
    assert marker.read_text() == 'x'


def test_a_transform_cannot_hide_a_lost_raw_value(monkeypatch):
    original = vm.oracle
    class Broken:
        def __init__(self, item): self.item = item
        def to_dict(self): return self.item.to_dict()
        def get_all(self, name): return self.item.get_all(name) + ['lost raw value']
    monkeypatch.setattr(vm, 'oracle', lambda *args: Broken(original(*args)))
    report = vm.verify(registry(), FIXTURES / 'manifest.json', benchmark=True)
    assert not report['ok'] and 'benchmark_skipped' in report
    assert all(not row['differences'] and row['raw_differences'] for row in report['responses'])


def test_oracle_first_fields_do_not_serialize_discarded_matches():
    class Matches:
        def get(self): return ''  # an empty match must survive
        def getall(self): pytest.fail('first-only field serialized every match')
    class Node:
        def css(self, query): return Matches()
    assert vm.values(Node(), 'a::attr(href)', first=True) == ['']
