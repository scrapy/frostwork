#!/usr/bin/env python3
"""Validate the source and built metadata users will see on PyPI.

This is intentionally stdlib-only so a tag can run the source checks before installing anything.  The
distribution check reads the actual PKG-INFO/METADATA rather than assuming the build backend copied the
source metadata faithfully.
"""

from __future__ import annotations

import argparse
from email import policy
from email.parser import BytesParser
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tomllib
from urllib.parse import unquote, urlsplit
import zipfile


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/scrapy/frostwork"
REQUIRED_PROJECT_URLS = {
    "Homepage": REPOSITORY,
    "Repository": REPOSITORY,
    "Documentation": f"{REPOSITORY}/blob/main/docs/PYTHON.md",
    "Issues": f"{REPOSITORY}/issues",
    "Changelog": f"{REPOSITORY}/blob/main/CHANGELOG.md",
    "Security": f"{REPOSITORY}/security/policy",
}
REQUIRED_DESCRIPTION_TEXT = (
    "Frostwork requires Python ≥ 3.10",
    "pip install frostwork",
    'pip install "frostwork[webpoet]"',
)
STALE_DESCRIPTION_TEXT = (
    "until packages are published",
    "not yet published to pypi",
)
MARKDOWN_LINK = re.compile(r"!?(?:\[[^]]*\])\(\s*([^)\s]+)")
RELEASE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def _toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def markdown_targets(text: str) -> list[str]:
    """Return ordinary Markdown link/image targets (autolinks need no base URL)."""
    return [match.group(1).strip("<>") for match in MARKDOWN_LINK.finditer(text)]


def _repo_target_error(target: str) -> str | None:
    """Resolve absolute links back into this checkout when they name a repository path."""
    parsed = urlsplit(target)
    if parsed.netloc != "github.com":
        return None
    prefix = "/scrapy/frostwork/blob/main/"
    tree_prefix = "/scrapy/frostwork/tree/main/"
    if parsed.path.startswith(prefix):
        local = ROOT / unquote(parsed.path[len(prefix):])
    elif parsed.path.startswith(tree_prefix):
        local = ROOT / unquote(parsed.path[len(tree_prefix):])
    else:
        return None
    if not local.exists():
        return f"repository URL names no local path: {target}"
    return None


def description_errors(text: str, *, source: str = "description") -> list[str]:
    errors: list[str] = []
    for target in markdown_targets(text):
        if target.startswith("#"):
            continue
        parsed = urlsplit(target)
        if parsed.scheme not in {"https", "mailto"}:
            errors.append(f"{source}: relative or non-HTTPS Markdown target: {target}")
            continue
        repo_error = _repo_target_error(target)
        if repo_error:
            errors.append(f"{source}: {repo_error}")

    folded = text.casefold()
    for stale in STALE_DESCRIPTION_TEXT:
        if stale in folded:
            errors.append(f"{source}: stale pre-publication text: {stale!r}")
    for required in REQUIRED_DESCRIPTION_TEXT:
        if required not in text:
            errors.append(f"{source}: missing release text: {required!r}")
    return errors


def _top_changelog_heading(text: str) -> tuple[str, str] | None:
    match = re.search(r"^## ([0-9]+\.[0-9]+\.[0-9]+) \(([^)]+)\)$", text, re.MULTILINE)
    return match.groups() if match else None


def changelog_notes(text: str, version: str) -> str:
    pattern = re.compile(
        rf"^## {re.escape(version)} \([^)]+\)\n+(.*?)(?=^## [0-9]+\.[0-9]+\.[0-9]+ |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    return match.group(1).strip()


def source_errors(*, tag: str | None = None) -> list[str]:
    pyproject = _toml(ROOT / "pyproject.toml")
    cargo = _toml(ROOT / "Cargo.toml")
    project = pyproject["project"]
    version = project["version"]
    errors: list[str] = []

    if version != cargo["package"]["version"]:
        errors.append(
            f"version mismatch: pyproject.toml={version}, Cargo.toml={cargo['package']['version']}"
        )
    if project.get("requires-python") != ">=3.10":
        errors.append(f"requires-python must be >=3.10, got {project.get('requires-python')!r}")
    if project.get("readme") != "README.md":
        errors.append(f"project.readme must be README.md, got {project.get('readme')!r}")

    urls = project.get("urls", {})
    for name, expected in REQUIRED_PROJECT_URLS.items():
        if urls.get(name) != expected:
            errors.append(f"project URL {name!r}: expected {expected!r}, got {urls.get(name)!r}")
    for name, target in urls.items():
        if not target.startswith("https://"):
            errors.append(f"project URL {name!r} is not HTTPS: {target!r}")

    errors.extend(description_errors((ROOT / "README.md").read_text(), source="README.md"))

    changelog = (ROOT / "CHANGELOG.md").read_text()
    top = _top_changelog_heading(changelog)
    if top is None:
        errors.append("CHANGELOG.md has no version heading")
    elif tag is None and top[1] != "unreleased":
        errors.append(f"top changelog section must be unreleased between releases, got {top!r}")
    elif tag is not None:
        if not RELEASE.fullmatch(tag):
            errors.append(f"release tag must be X.Y.Z without a prefix, got {tag!r}")
        if version != tag:
            errors.append(f"release tag {tag} does not match package version {version}")
        if top[0] != tag or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", top[1]):
            errors.append(f"top changelog section must be {tag} with a release date, got {top!r}")
    return errors


def _distribution_metadata(path: Path):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError(f"{path}: expected one .dist-info/METADATA, found {names}")
            raw = archive.read(names[0])
    else:
        with tarfile.open(path, "r:*") as archive:
            members = [
                member for member in archive.getmembers()
                if PurePosixPath(member.name).name == "PKG-INFO" and member.isfile()
            ]
            if len(members) != 1:
                raise ValueError(f"{path}: expected one PKG-INFO, found {[m.name for m in members]}")
            extracted = archive.extractfile(members[0])
            if extracted is None:
                raise ValueError(f"{path}: cannot read {members[0].name}")
            raw = extracted.read()
    separator = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
    headers, found, description = raw.partition(separator)
    if not found:
        raise ValueError(f"{path}: metadata has no header/description separator")
    message = BytesParser(policy=policy.default).parsebytes(headers + separator)
    return message, description.decode("utf-8")


def distribution_errors(path: Path, *, expected_version: str | None = None) -> list[str]:
    message, description = _distribution_metadata(path)
    pyproject = _toml(ROOT / "pyproject.toml")["project"]
    expected_version = expected_version or pyproject["version"]
    errors: list[str] = []
    if message["Version"] != expected_version:
        errors.append(f"{path}: metadata Version={message['Version']!r}, expected {expected_version!r}")
    if message["Requires-Python"] != pyproject["requires-python"]:
        errors.append(
            f"{path}: metadata Requires-Python={message['Requires-Python']!r}, "
            f"expected {pyproject['requires-python']!r}"
        )
    content_type = message["Description-Content-Type"] or ""
    if not content_type.startswith("text/markdown"):
        errors.append(f"{path}: Description-Content-Type is not Markdown: {content_type!r}")

    built_urls: dict[str, str] = {}
    for value in message.get_all("Project-URL", []):
        name, separator, target = value.partition(",")
        if not separator:
            errors.append(f"{path}: malformed Project-URL: {value!r}")
        else:
            built_urls[name.strip()] = target.strip()
    if built_urls != pyproject.get("urls", {}):
        errors.append(f"{path}: built Project-URL metadata differs: {built_urls!r}")

    errors.extend(description_errors(description, source=str(path)))
    return errors


def git_release_errors(tag: str, *, require_annotated: bool, require_main: bool) -> list[str]:
    errors: list[str] = []

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=ROOT, text=True, capture_output=True, check=check
        )

    tag_ref = f"refs/tags/{tag}"
    resolved = git("rev-parse", "--verify", tag_ref, check=False)
    if resolved.returncode:
        return [f"git tag {tag!r} does not exist in the checkout"]
    if require_annotated:
        kind = git("cat-file", "-t", tag_ref).stdout.strip()
        if kind != "tag":
            errors.append(f"git tag {tag!r} must be annotated, got object type {kind!r}")
    if require_main:
        main_ref = "refs/remotes/origin/main"
        if git("rev-parse", "--verify", main_ref, check=False).returncode:
            errors.append(f"cannot verify release ancestry: {main_ref} is missing")
        else:
            commit = git("rev-parse", f"{tag_ref}^{{commit}}").stdout.strip()
            ancestor = git("merge-base", "--is-ancestor", commit, main_ref, check=False)
            if ancestor.returncode:
                errors.append(f"release tag {tag!r} ({commit}) is not contained in origin/main")
    return errors


def _automatic_tag() -> str | None:
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        return os.environ.get("GITHUB_REF_NAME")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="require source metadata to match this release tag")
    parser.add_argument("--distribution", action="append", type=Path, default=[])
    parser.add_argument("--require-annotated-tag", action="store_true")
    parser.add_argument("--require-main", action="store_true")
    parser.add_argument("--notes-for", help="print this version's changelog body and exit")
    args = parser.parse_args(argv)

    if args.notes_for:
        print(changelog_notes((ROOT / "CHANGELOG.md").read_text(), args.notes_for))
        return 0

    tag = args.tag or _automatic_tag()
    errors = source_errors(tag=tag)
    if tag and (args.require_annotated_tag or args.require_main):
        errors.extend(
            git_release_errors(
                tag,
                require_annotated=args.require_annotated_tag,
                require_main=args.require_main,
            )
        )
    for distribution in args.distribution:
        errors.extend(distribution_errors(distribution, expected_version=tag))

    if errors:
        print("release check: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    checked = "source metadata" if not args.distribution else "source and built metadata"
    print(f"release check: OK ({checked})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
