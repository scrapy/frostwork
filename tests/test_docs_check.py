"""The documentation gate must fail on missing files, heading drift and absent rendering support."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from check_docs import check_documents
from release_check import REPOSITORY


@pytest.mark.parametrize('href', ['guide.md#heading', f'{REPOSITORY}/blob/main/guide.md#heading'])
def test_doc_gate_checks_rendered_heading_ids_and_missing_targets(tmp_path, href):
    source = tmp_path / 'README.md'
    target = tmp_path / 'guide.md'
    # Inject already-rendered HTML to test the decision without optional packaging dependencies.
    source.write_text(f'<a href="{href}">Guide</a><a href="https://example.invalid/">External</a>')
    target.write_text('<h1 id="user-content-heading">Heading</h1>')
    assert check_documents([source], tmp_path, render=str) == []
    target.write_text('<h1 id="user-content-renamed">Renamed</h1>')
    assert 'missing heading' in check_documents([source], tmp_path, render=str)[0]
    target.unlink()
    assert 'missing target' in check_documents([source], tmp_path, render=str)[0]


def test_doc_gate_cannot_pass_when_the_renderer_returns_nothing(tmp_path):
    source = tmp_path / 'README.md'
    source.write_text('# Title')
    with pytest.raises(ValueError, match='did not render'):
        check_documents([source], tmp_path, render=lambda source: None)


def test_doc_gate_checks_local_anchors_and_image_files(tmp_path):
    source = tmp_path / 'README.md'
    source.write_text('<h1 id="user-content-heading">Title</h1>'
                      '<a href="#user-content-heading">Heading</a><img src="missing.png">')
    errors = check_documents([source], tmp_path, render=str)
    assert len(errors) == 1 and 'missing.png' in errors[0]
