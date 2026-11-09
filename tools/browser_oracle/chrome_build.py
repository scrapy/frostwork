"""Print the Chrome build the HTML5 rule tables were derived from, as `156.0.8078.80`.

The browser gates run against this build, so CI installs exactly it from Chrome for Testing. It is read
from `html5_derivation.json`, which `gen_html5_rules.py --write` stamps, so re-deriving the tables moves
the pin with them. Standard library only: CI calls it before any dependency is installed.
"""
from __future__ import annotations

import json
import os

DERIVATION = os.path.join(os.path.dirname(os.path.abspath(__file__)), "html5_derivation.json")


def chrome_build() -> str:
    with open(DERIVATION, encoding="utf-8") as f:
        build = json.load(f)["browsers"]["chrome"]["build"]
    name, _, version = build.partition("/")
    if name != "Chrome" or not version:
        raise ValueError(f"{DERIVATION}: not a Chrome build: {build!r}")
    return version


if __name__ == "__main__":
    print(chrome_build())
