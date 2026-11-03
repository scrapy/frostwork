#!/usr/bin/env python3
"""Frostwork against the browser column of the encoding-hacks lab, three ways (docs/BROWSER_MODE.md,
section 1 and E1).

Each lab case is exact bytes plus a `Content-Type`, and the lab records what a real browser rendered for
one selector. This runs every case through `frostwork.extract` with three different encoding labels and
counts the cases whose values differ from the browser's:

  header     the raw `Content-Type` charset, which is what the engine is meant to receive
  w3lib      `w3lib.encoding.http_content_type_encoding`, which drops labels Python has no codec for
  web-poet   `HttpResponse.encoding`, w3lib's whole decision, which is what `FrostPage` passes today

    .venv/bin/python tools/browser_oracle/lab_compare.py [--lab ~/src/encoding-hacks]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def header_label(ctype):
    m = re.search(r"charset\s*=\s*([^;\s]+)", ctype or "", re.I)
    return m.group(1).strip("\"'") if m else None


def labels(case):
    from w3lib.encoding import http_content_type_encoding
    from web_poet import HttpResponse

    ctype = case["ctype"]
    headers = {"Content-Type": ctype} if ctype else {}
    return {
        "header": header_label(ctype),
        "w3lib": http_content_type_encoding(ctype) if ctype else None,
        "web-poet": HttpResponse(url="http://example.com/", body=case["body"], headers=headers).encoding,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lab", type=Path, default=Path("~/src/encoding-hacks").expanduser())
    ap.add_argument("--json", action="store_true", help="print the per-case table as JSON")
    args = ap.parse_args()
    sys.path.insert(0, str(args.lab))
    from encoding_hacks.cases import CASES
    from encoding_hacks.report import load_browser

    import frostwork

    browser = load_browser()
    rows = []
    for case in CASES:
        want = browser.get(case["id"], {}).get("values")
        if want is None:
            continue
        row = {"case": case["id"], "browser": want}
        for mode, label in labels(case).items():
            try:
                got = frostwork.extract(case["body"], [case["sel"]], label)[0]
            except ValueError as e:
                got = [f"ERR: {e}"]
            row[mode] = {"label": label, "values": got, "agrees": got == want}
        rows.append(row)

    if args.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    modes = ("header", "w3lib", "web-poet")
    for mode in modes:
        bad = [r["case"] for r in rows if not r[mode]["agrees"]]
        print(f"{mode:9} {len(bad):3} of {len(rows)} differ from the browser: {', '.join(bad)}")
    print()
    for r in rows:
        if r["header"]["agrees"] != r["web-poet"]["agrees"]:
            wp = r["web-poet"]
            print(f"  only through web-poet: {r['case']:16} label={wp['label']!r:14} {json.dumps(wp['values'], ensure_ascii=False)[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
