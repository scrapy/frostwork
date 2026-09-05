"""Verify the checked-in representative selector support snapshot against the real compiler."""
from __future__ import annotations

import argparse
from pathlib import Path

import frostwork

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "SUPPORT_SNAPSHOT.md"

ROWS = [
    ("CSS compound", "flat", "div.card[data-x]::text", True),
    ("CSS escaped class", "flat", r".sm\:text-lg::text", True),
    ("CSS hex escaped id", "flat", r"#i\31::attr(id)", True),
    ("CSS escaped tag", "flat", r"\70::text", False),
    ("CSS sibling", "flat", "dt + dd::text", True),
    ("CSS reverse position", "flat", "li:last-child::text", True),
    ("CSS reverse subtree", "flat", "li:last-child ::text", True),
    ("CSS reverse on ancestor", "flat", "li:last-child b::text", True),
    ("CSS reverse child tail", "flat", "li:last-child > b::text", False),
    ("CSS :has subtree", "flat", "div:has(a) ::text", True),
    ("CSS :has on ancestor", "flat", "div:has(a) a::attr(href)", True),
    ("XPath text-pred descendant", "flat", '//div[contains(.,"x")]//a/@href', True),
    ("CSS :contains", "flat", 'p:contains("x")::text', True),
    ("CSS :contains sibling", "flat", 'dt:contains("x") + dd::text', True),
    ("CSS :contains descendant", "flat", 'div:contains("x") a::attr(href)', True),
    ("CSS :contains doubled", "flat", 'p:contains("a"):contains("b")::text', False),
    ("CSS :contains non-string arg", "flat", "p:contains(2)::text", False),
    ("CSS implicit subject after +", "flat", "dt + ::text", True),
    ("CSS implicit subject after >", "flat", "div > ::attr(id)", True),
    ("CSS dangling combinator", "flat", "dt +", False),
    ("XPath reverse subtree", "flat", "//li[last()]//text()", True),
    ("CSS :has", "flat", "div:has(a)::text", True),
    ("CSS widened :has", "flat", "div:has([data-x])::attr(id)", True),
    # beyond-lxml forms: valid CSS cssselect rejects. Each has a twin below that must stay `no`, so the
    # row proves a capability rather than just a parser that accepts more.
    ("CSS :has list", "flat", "div:has(a, img)::attr(id)", True),
    ("CSS :has mixed-rel list", "flat", "div:has(> a, img)::attr(id)", False),
    ("CSS :not list", "flat", "p:not(.a, .b)::text", True),
    ("CSS :not empty member", "flat", "p:not(.a, )::text", False),
    ("CSS attr case flag", "flat", "[type=submit i]::attr(id)", True),
    ("CSS attr bogus flag", "flat", "[type=submit x]::attr(id)", False),
    ("CSS :is", "flat", "div:is(.a, .b)::text", True),
    # a `)` inside a QUOTED value is data, not the end of the pseudo; the malformed twin must stay no
    ("CSS quoted delimiter", "flat", 'div:is(#a, [data-x=")"])::attr(id)', True),
    ("CSS unterminated pseudo", "flat", 'div:is(#a, [data-x=")"]::attr(id)', False),
    ("XPath union", "flat", "//dt/text() | //dd/text()", True),
    ("XPath following sibling", "flat", "//dt/following-sibling::dd/text()", True),
    ("XPath upward", "flat", "//a/ancestor::div/@id", True),
    ("XPath text predicate", "flat", '//p[contains(.,"x")]/text()', True),
    ("XPath normalize-space", "flat", "normalize-space(//h1)", True),
    ("XPath variable reference", "flat", "//*[@id=$pid]", False),
    ("XPath unquoted operand", "flat", "//span[@x=2]/text()", False),
    ("Grouped basic container", "container", ".card", True),
    ("Grouped comma container", "container", "div, span", False),
    ("Grouped deferred container", "container", "div:has(a)", False),
    ("Grouped descendant sub-field", "sub-field", ".//a/@href", True),
    ("Grouped child sub-field", "sub-field", "./a/@href", True),
    ("Flat child anchor", "flat", "./a/@href", False),
    ("Grouped own attribute", "sub-field", "@id", True),
    ("Grouped own text", "sub-field", "text()", True),
    ("Grouped subtree text", "sub-field", ".//text()", True),
    ("Grouped own node", "sub-field", ".", True),
    ("Grouped normalize-space", "sub-field", "normalize-space(//a)", False),
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
