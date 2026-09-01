"""Regression tests for the metadata gate that protects the published package page."""

from pathlib import Path
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import release_check  # noqa: E402


def test_current_source_is_release_ready():
    assert release_check.source_errors() == []


def test_description_gate_rejects_the_pypi_relative_link_regression():
    readme = (ROOT / "README.md").read_text()
    broken = readme.replace(
        "https://github.com/scrapy/frostwork/blob/main/docs/COMPATIBILITY.md",
        "docs/COMPATIBILITY.md",
        1,
    )
    errors = release_check.description_errors(broken)
    assert any("relative or non-HTTPS" in error for error in errors), errors


def test_description_gate_rejects_stale_prepublication_copy():
    readme = (ROOT / "README.md").read_text() + "\nFrostwork is not yet published to PyPI.\n"
    errors = release_check.description_errors(readme)
    assert any("stale pre-publication" in error for error in errors), errors


def test_built_metadata_is_checked_not_just_the_source(tmp_path):
    project = release_check._toml(ROOT / "pyproject.toml")["project"]
    headers = [
        "Metadata-Version: 2.4",
        "Name: frostwork",
        f"Version: {project['version']}",
        f"Requires-Python: {project['requires-python']}",
        "Description-Content-Type: text/markdown",
    ]
    for name, target in project["urls"].items():
        headers.append(f"Project-URL: {name}, {target}")
    description = (ROOT / "README.md").read_text().replace(
        "https://github.com/scrapy/frostwork/blob/main/docs/COMPATIBILITY.md",
        "docs/COMPATIBILITY.md",
        1,
    )

    pkg_info = tmp_path / "PKG-INFO"
    pkg_info.write_bytes(("\n".join(headers) + "\n\n").encode() + description.encode())
    sdist = tmp_path / "frostwork-test.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(pkg_info, arcname="frostwork-test/PKG-INFO")

    errors = release_check.distribution_errors(sdist)
    assert any("relative or non-HTTPS" in error for error in errors), errors


def test_release_notes_come_from_the_versioned_changelog_section():
    notes = release_check.changelog_notes(
        "# Changelog\n\n## 1.2.3 (2026-09-01)\n\n### Fixed\n\n- Links.\n\n"
        "## 1.2.2 (2026-08-01)\n\n- Older.\n",
        "1.2.3",
    )
    assert notes == "### Fixed\n\n- Links."


def test_release_gate_is_wired_into_local_ci_and_tag_publishing():
    """A correct checker that no workflow calls is not a release gate."""
    makefile = (ROOT / "Makefile").read_text()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    publish = (ROOT / ".github" / "workflows" / "publish.yml").read_text()

    assert "ci: test gate gate-corpus gate-seq fuzz-smoke py gate-webpoet " \
        "gate-webpoet-mutate release-check" in makefile
    assert "- run: make release-check" in ci
    assert "uses: ./.github/workflows/ci.yml" in publish
    assert 'python tools/verify_pypi.py "$GITHUB_REF_NAME"' in publish
    assert "needs: verify" in publish
