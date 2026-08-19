"""abi3-floor smoke test: does the built extension work with NO Python dependencies installed?

Frostwork's core (`extract`, `Page`/`Item`, `check`, the `frostwork-audit` CLI) is documented to need
nothing but the compiled extension — no parsel, no web-poet, no pytest. `tests/test_python.py` cannot
show that, because installing it installs those very packages.

This script therefore uses NOTHING but the standard library and `frostwork` itself, and asserts the
dependency-free surface: the primitive, strict/permissive modes, `Page`/`Item` with groups, the schema
audit, and the source scan. Run it on any supported interpreter (>= 3.10, the abi3 floor) after building
the extension:

    .venv/bin/python tools/abi3_smoke.py

Value parity is NOT this script's job — that is the differential gate's, which owns the pinned oracle
(see docs/TESTING.md). This answers only "does the wheel load and behave on this interpreter".
"""

from __future__ import annotations

import os
import sys
import tempfile

import frostwork

PRODUCT = (
    b"<html><body><div class=product><h1>Widget</h1><span class=price>$9</span>"
    b"<a href=/p/1>buy</a><img src=/a.png><img src=/b.png>"
    b'<div class="card"><h3>One</h3></div><div class="card"><h3>Two</h3></div>'
    b"</div></body></html>"
)


def check(label: str, got, want) -> None:
    if got != want:
        raise AssertionError(f"{label}: got {got!r}, want {want!r}")
    print(f"  ok  {label}")


def main() -> int:
    print(f"abi3 smoke: python {sys.version.split()[0]} / frostwork {frostwork.__version__}")

    # 1. the primitive: one column per query, in query order, CSS and XPath
    cols = frostwork.extract(PRODUCT, ["h1::text", "img::attr(src)", "//a/@href"])
    check("extract columns", cols, [["Widget"], ["/a.png", "/b.png"], ["/p/1"]])
    check("extract from str", frostwork.extract("<p>hi</p>", ["p::text"]), [["hi"]])
    check(
        "explicit encoding label",
        frostwork.extract(b"<p>caf\xe9</p>", ["p::text"], "windows-1252"),
        [["café"]],
    )

    # 2. no fallback, both halves: strict raises before scanning, strict=False gives an empty column
    #
    # `:hover` rather than a selector that is merely unsupported TODAY: a coverage gap closes and the
    # probe then fails for the best possible reason. A user-interaction pseudo-class has no answer in a
    # static document, so it can never be implemented into parity and cannot rot that way.
    unsupported = "p:hover::text"
    try:
        frostwork.extract(PRODUCT, [unsupported])
    except frostwork.UnsupportedSelector:
        print("  ok  strict=True raises UnsupportedSelector")
    else:
        raise AssertionError("strict=True should have raised for an unsupported selector")
    check("strict=False empty column", frostwork.extract(PRODUCT, [unsupported], strict=False), [[]])

    # 3. Page / Item, including a grouped Many and the dead-selector signal
    page = (
        frostwork.Page()
        .field("title", "h1::text")
        .field_all("images", "img::attr(src)")
        .field("gone", ".nope::text")
        .many("cards", ".card", {"h": ".//h3/text()"})
    )
    item = page.extract(PRODUCT)
    check("Page field", item.get("title"), "Widget")
    check("Page field_all", item.get_all("images"), ["/a.png", "/b.png"])
    check("Page many rows", item.value("cards"), [{"h": "One"}, {"h": "Two"}])
    check("Item.empty_fields", item.empty_fields(), ["gone"])

    # 4. the schema audit, including the operand rules
    report = frostwork.check(["h1::text", "//*[@id=$pid]", "a[id=2]::text"])
    check("audit ok flag", report.ok, False)
    check("audit supported/unsupported split", [f.supported for f in report.fields], [True, False, False])
    check("audit reason names the variable", "variable" in (report.fields[1].reason or ""), True)

    # 5. the source scan (ast-based, no imports of the target)
    from frostwork.scan import judge, scan_path, summarize

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "spider.py")
        with open(path, "w") as fh:
            fh.write("v = response.css('h1::text')\nw = row.xpath('td/text()')\n")
        verdicts = judge(scan_path(path))
        summary = summarize(verdicts)
    check("scan site count", summary["literal"], 2)
    check("scan supported count", summary["supported"], 1)
    check("scan names the rewrite", "Many/One" in (verdicts[1].reason or ""), True)

    print("abi3 smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
