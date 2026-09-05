#!/usr/bin/env python3
"""Render the public Markdown and check repository links against the rendered heading IDs.

No network access: external links are left to their owners. Requires requirements-release.txt.
"""
from __future__ import annotations

from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from release_check import REPOSITORY

ROOT = Path(__file__).resolve().parents[1]


class Links(HTMLParser):
    def __init__(self, rendered: str):
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []
        self.feed(rendered)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id'):
            self.ids.add(attrs['id'])
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if tag == 'img' and attrs.get('src'):
            self.links.append(attrs['src'])


def check_documents(paths, root=ROOT, render=None):
    if render is None:
        from readme_renderer.markdown import render

    @lru_cache(maxsize=None)
    def document(path):
        rendered = render(path.read_text(encoding='utf-8'))
        if not rendered:
            raise ValueError(f'{path}: Markdown did not render; install requirements-release.txt')
        return Links(rendered)

    errors = []
    for source in paths:
        for href in document(source).links:
            link = urlsplit(href)
            prefix = f'{REPOSITORY}/blob/main/'
            if href.startswith(prefix):
                target = root / unquote(link.path.split('/blob/main/', 1)[1])
            elif link.scheme or link.netloc:
                continue
            else:
                target = (source.parent / unquote(link.path)).resolve() if link.path else source
            if not target.exists():
                errors.append(f'{source.name}: missing target {href}')
            elif link.fragment and target.suffix == '.md':
                fragment = unquote(link.fragment)
                ids = document(target).ids
                if fragment not in ids and f'user-content-{fragment}' not in ids:
                    errors.append(f'{source.name}: missing heading {href}')
    return errors


def main():
    try:
        errors = check_documents([ROOT / 'README.md', ROOT / 'CHANGELOG.md', *sorted((ROOT / 'docs').glob('*.md'))])
    except (ImportError, OSError, ValueError) as exc:
        errors = [str(exc)]
    for error in errors:
        print(error)
    if not errors:
        print('docs check: Markdown rendered and repository links resolved')
    return bool(errors)


if __name__ == '__main__':
    raise SystemExit(main())
