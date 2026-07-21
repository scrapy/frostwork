"""Verify the checked-in representative selector support snapshot against the real compiler."""
from __future__ import annotations

import argparse
from pathlib import Path

import frostwork

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "SUPPORT_SNAPSHOT.md"

ROWS = [
    ("CSS compound", "flat", "div.card[data-x]::text", True),
    ("CSS sibling", "flat", "dt + dd::text", True),
    ("CSS reverse position", "flat", "li:last-child::text", True),
    ("CSS :has", "flat", "div:has(a)::text", True),
    ("CSS widened :has", "flat", "div:has([data-x])::attr(id)", True),
    ("CSS :is", "flat", "div:is(.a, .b)::text", True),
    ("XPath union", "flat", "//dt/text() | //dd/text()", True),
    ("XPath following sibling", "flat", "//dt/following-sibling::dd/text()", True),
    ("XPath upward", "flat", "//a/ancestor::div/@id", True),
    ("XPath text predicate", "flat", '//p[contains(.,"x")]/text()', True),
    ("XPath normalize-space", "flat", "normalize-space(//h1)", True),
    ("Grouped basic container", "container", ".card", True),
    ("Grouped comma container", "container", "div, span", False),
    ("Grouped deferred container", "container", "div:has(a)", False),
    ("Grouped descendant sub-field", "sub-field", ".//a/@href", True),
    ("Grouped comma sub-field", "sub-field", "p::text, a::text", False),
    ("Grouped deferred sub-field", "sub-field", "p:has(a)::text", False),
]


def verdict(context: str, selector: str) -> bool:
    if context == "flat":
        return frostwork.check([selector]).fields[0].supported
    report = frostwork.check([], [("g", selector if context == "container" else ".card", {
        "value": "span::text" if context == "container" else selector,
    })])
    group = report.groups[0]
    return group.container.supported if context == "container" else group.subfields[0].supported


def render() -> str:
    lines = [
        "# Representative selector support snapshot",
        "",
        "Generated/verified by `tools/support_snapshot.py` against the real compiler. The exhaustive",
        "contract remains in `COMPATIBILITY.md`; this table is a drift tripwire for headline features.",
        "",
        "| feature | context | selector | supported |",
        "|---|---|---|---|",
    ]
    for feature, context, selector, expected in ROWS:
        actual = verdict(context, selector)
        if actual != expected:
            raise SystemExit(
                f"support expectation drifted for {feature}: expected {expected}, compiler returned {actual}"
            )
        escaped = selector.replace("|", "\\|")
        lines.append(f"| {feature} | {context} | `{escaped}` | {'yes' if actual else 'no'} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the checked-in snapshot differs")
    args = ap.parse_args()
    content = render()
    if args.check:
        if TARGET.read_text() != content:
            raise SystemExit(f"{TARGET.relative_to(ROOT)} is stale; regenerate from tools/support_snapshot.py")
    else:
        print(content, end="")


if __name__ == "__main__":
    main()
