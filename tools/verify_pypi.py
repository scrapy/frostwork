#!/usr/bin/env python3
"""Install and verify one Frostwork release from the public PyPI index."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from release_check import REQUIRED_PROJECT_URLS, description_errors


def _retry(operation, label: str, attempts: int = 6, delay: int = 10):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (HTTPError, URLError, subprocess.CalledProcessError) as exc:
            last_error = exc
            if attempt == attempts:
                break
            print(f"{label} not ready ({attempt}/{attempts}): {exc}; retrying in {delay}s")
            time.sleep(delay)
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last_error}")


def _metadata(version: str) -> dict:
    request = Request(
        f"https://pypi.org/pypi/frostwork/{version}/json",
        headers={"User-Agent": "frostwork-release-verifier/1"},
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed PyPI origin
        return json.load(response)


def _install(version: str) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--index-url=https://pypi.org/simple",
            f"frostwork=={version}",
        ],
        check=True,
    )


def verify(version: str) -> None:
    data = _retry(lambda: _metadata(version), "PyPI JSON metadata")
    info = data["info"]
    errors = description_errors(info.get("description", ""), source="PyPI description")
    if info.get("version") != version:
        errors.append(f"PyPI reports version {info.get('version')!r}, expected {version!r}")
    if info.get("project_urls") != REQUIRED_PROJECT_URLS:
        errors.append(f"PyPI project URLs differ: {info.get('project_urls')!r}")
    if errors:
        raise RuntimeError("PyPI metadata check failed:\n- " + "\n- ".join(errors))

    _retry(lambda: _install(version), "wheel installation")
    import frostwork

    installed = importlib.metadata.version("frostwork")
    if installed != version:
        raise RuntimeError(f"installed frostwork {installed}, expected {version}")
    values = frostwork.extract(b"<h1>published</h1>", ["h1::text"])
    if values != [["published"]]:
        raise RuntimeError(f"installed wheel smoke test returned {values!r}")
    print(f"PyPI verification: OK (frostwork {version})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args(argv)
    verify(args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
