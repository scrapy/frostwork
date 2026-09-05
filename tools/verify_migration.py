#!/usr/bin/env python3
"""Verify complete Page schemas on saved response bytes, then optionally time equal whole items.

Usage: python tools/verify_migration.py pages.py:REGISTRY responses/manifest.json --json report.json
The registry is a Page or a mapping of names to Pages. Import-safe modules only: importing executes
Python code, including the transforms used by both implementations. This is an offline comparison
against Parsel, never a runtime fallback. See docs/MIGRATION.md for the manifest and exact gate.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time

from parsel import Selector

from frostwork import Page, detect_encoding
from frostwork.audit import _load_module, _report_dict
from frostwork.page import Item, _shape
from diff_lxml import verdict


def differences(actual, expected, path='$'):
    """Exact whole-item differences; no whitespace, missing-key or serialization allowance."""
    if type(actual) is not type(expected):
        return [{'field': path, 'kind': 'changed', 'actual': actual, 'expected': expected}]
    if isinstance(actual, dict):
        out = []
        for key in expected.keys() - actual.keys():
            out.append({'field': f'{path}.{key}', 'kind': 'missing', 'expected': expected[key]})
        for key in actual.keys() - expected.keys():
            out.append({'field': f'{path}.{key}', 'kind': 'extra', 'actual': actual[key]})
        for key in expected.keys() & actual.keys():
            out.extend(differences(actual[key], expected[key], f'{path}.{key}'))
        return sorted(out, key=lambda row: row['field'])
    if isinstance(actual, list):
        out = []
        for i in range(max(len(actual), len(expected))):
            if i >= len(actual):
                out.append({'field': f'{path}[{i}]', 'kind': 'missing', 'expected': expected[i]})
            elif i >= len(expected):
                out.append({'field': f'{path}[{i}]', 'kind': 'extra', 'actual': actual[i]})
            else:
                out.extend(differences(actual[i], expected[i], f'{path}[{i}]'))
        if out and Counter(json.dumps(v, sort_keys=True) for v in actual) == Counter(
                json.dumps(v, sort_keys=True) for v in expected):
            return [{'field': path, 'kind': 'reordered', 'actual': actual, 'expected': expected}]
        return out
    return [] if actual == expected else [
        {'field': path, 'kind': 'changed', 'actual': actual, 'expected': expected}]


def values(node, query, *, first=False):
    query = query.strip()
    xpath = query.startswith(('/', '.', '@', 'text()', 'normalize-space(', 'string('))
    # A CSS .class is not a relative XPath.
    xpath = xpath and not (query.startswith('.') and not query.startswith(('./', './/')) and query != '.')
    selected = node.xpath(query) if xpath else node.css(query)
    if first:
        value = selected.get()
        return [] if value is None else [value]
    return selected.getall()


def oracle(page, body, encoding):
    root = Selector(body=body, encoding=encoding or 'utf-8')
    columns = [values(root, f.selector, first=f.card[0] == 'first') for f in page._fields.values()]
    groups = {}
    for name, group in page._groups.items():
        rows = []
        selector = group.container
        nodes = root.xpath(selector) if selector.startswith(('/', './')) else root.css(selector)
        if group.one:
            nodes = nodes[:1]
        for node in nodes:
            rows.append({sn: _shape(values(node, f.selector, first=f.card[0] == 'first'), f.card)
                         for sn, f in group.subfields.items()})
        groups[name] = (rows[0] if rows else None) if group.one else rows
    return Item._from_columns(page._fields, columns, groups, {})


def timed_pair(frost, parsel, rounds, iterations):
    """Interleave both complete extraction+shaping calls, reporting samples and within-run spread."""
    samples = {'frostwork': [], 'parsel': []}
    calls = {'frostwork': frost, 'parsel': parsel}
    for call in calls.values():
        for _ in range(3): call()
    for rep in range(rounds):
        for name in (['frostwork', 'parsel'] if rep % 2 == 0 else ['parsel', 'frostwork']):
            call = calls[name]
            start = time.perf_counter_ns()
            for _ in range(iterations): call()
            samples[name].append((time.perf_counter_ns() - start) / iterations / 1000)
    return {name: {'samples_us': data, 'median_us': statistics.median(data), 'min_us': min(data),
                   'spread_percent': (max(data) - min(data)) / min(data) * 100}
            for name, data in samples.items()}


def verify(registry, manifest_path, *, benchmark=False, rounds=7, iterations=100):
    manifest_path = Path(manifest_path).resolve()
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get('version') != 1 or not manifest.get('responses'):
        raise ValueError('manifest needs version: 1 and a nonempty responses list')
    audits = {name: _report_dict(name, page.check()) for name, page in registry.items()}
    result = {'manifest_sha256': hashlib.sha256(manifest_bytes).hexdigest(), 'schemas': audits,
              'responses': [], 'environment': {
                  'python': platform.python_version(), 'platform': platform.platform(),
                  'machine': platform.machine(),
                  'packages': {name: version(name) for name in ('frostwork', 'parsel', 'lxml', 'cssselect')},
              }}
    from lxml import etree
    from frostwork import _frostwork
    result['environment']['libxml2'] = list(etree.LIBXML_VERSION)
    result['environment']['extension_sha256'] = hashlib.sha256(Path(_frostwork.__file__).read_bytes()).hexdigest()
    git = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)
    result['environment']['git_revision'] = git.stdout.strip() if git.returncode == 0 else None
    status = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True)
    result['environment']['git_dirty'] = bool(status.stdout.strip()) if status.returncode == 0 else None
    result['environment']['processor'] = platform.processor()
    pending = []
    for response in manifest['responses']:
        name = response['schema']
        if name not in registry:
            raise ValueError(f'unknown response schema: {name!r}')
        path = (manifest_path.parent / response['file']).resolve()
        body = path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != response['sha256']:
            raise ValueError(f'body hash mismatch: {response["file"]}')
        encoding = response.get('encoding')
        page = registry[name]
        row = {**response, 'bytes': len(body), 'frostwork_encoding': detect_encoding(body, encoding),
               'parsel_encoding': encoding or 'utf-8', 'ok': False}
        result['responses'].append(row)
        if not audits[name]['ok'] or not len(page):
            row['error'] = 'schema unsupported, over budget or empty'
            continue
        try:
            actual = page.extract(body, encoding=encoding)
            expected = oracle(page, body, encoding)
            actual_values, expected_values = actual.to_dict(), expected.to_dict()
            json.dumps([actual_values, expected_values], allow_nan=False)
            row['differences'] = differences(actual_values, expected_values)
            row['empty_fields'] = actual.empty_fields()
            # Publish the existing engine verdict for context. It does NOT waive migration differences:
            # raw-source HTML and whitespace can be accepted by the engine gate yet change a user's item.
            row['columns'] = [{'field': n, 'selector': f.selector,
                               'gate_verdict': verdict(actual.get_all(n), expected.get_all(n), '', f.selector)}
                              for n, f in page._fields.items()]
            row['raw_differences'] = differences(
                {n: actual.get_all(n) for n in page._fields},
                {n: expected.get_all(n) for n in page._fields}, '$.raw_fields')
            row['ok'] = not row['differences'] and not row['raw_differences']
            row['has_matches'] = any(actual.get_all(n) for n in page.field_names)
            # Exact JSON is the artifact contract; fail explicitly on arbitrary transformed objects.
            json.dumps(row)
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
            row['ok'] = False
        pending.append((row, page, body, encoding))
    result['complete_schemas'] = sum(
        bool(rows := [r for r in result['responses'] if r['schema'] == name]) and all(r['ok'] for r in rows)
        for name in registry)
    result['ok'] = result['complete_schemas'] == len(registry) and bool(registry)
    if benchmark and result['ok']:
        for row, page, body, encoding in pending:
            if not row['has_matches']:
                row['benchmark_skipped'] = 'no matches; cannot represent an extraction workload'
                continue
            page._get_plan()  # force compilation outside every timed call
            row['timing'] = timed_pair(
                lambda: page.extract(body, encoding=encoding).to_dict(),
                lambda: oracle(page, body, encoding).to_dict(), rounds, iterations)
    elif benchmark:
        result['benchmark_skipped'] = 'whole-schema parity failed; nothing was timed'
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', help='import-safe pages.py:PAGE or module:REGISTRY (mapping of Pages)')
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--json', required=True, type=Path, dest='output')
    parser.add_argument('--benchmark', action='store_true')
    parser.add_argument('--rounds', type=int, default=7)
    parser.add_argument('--iterations', type=int, default=100)
    args = parser.parse_args(argv)
    try:
        if args.rounds < 2 or args.iterations < 1:
            raise ValueError('rounds must be >= 2 and iterations >= 1')
        module, attr = args.target.rsplit(':', 1)
        module_obj = _load_module(module)
        registry = getattr(module_obj, attr)
        if isinstance(registry, Page): registry = {attr: registry}
        if not isinstance(registry, dict) or not all(isinstance(p, Page) for p in registry.values()):
            raise ValueError('target must be a Page or a dict of named Pages')
        result = verify(registry, args.manifest, benchmark=args.benchmark,
                        rounds=args.rounds, iterations=args.iterations)
        result['target'] = args.target
        if getattr(module_obj, '__file__', None):
            result['schema_source_sha256'] = hashlib.sha256(Path(module_obj.__file__).read_bytes()).hexdigest()
        args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    except Exception as exc:
        print(f'verify-migration: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    print(f'{"PASS" if result["ok"] else "FAIL"}: {result["complete_schemas"]}/{len(registry)} complete '
          f'schemas match on saved responses; report: {args.output}')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
