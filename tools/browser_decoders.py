"""
tools/browser_decoders.py: every legacy decoder against the browsers (part of `make gate-browser`).

`decoder_sweep.py` compares the engine's decoders with Python's codecs; this compares them with what a
browser shows. Each single-byte encoding's high half, and each multi-byte encoding's whole two-byte space
(`decoder_sweep.candidates`), is served as one page with its charset in the Content-Type: a `<pre>` with
one sequence per line, since LF is no encoding's trail byte. The sequences follow one another, so state a
malformed sequence leaves in a decoder reaches the next one. Every line is compared with the engine's.

The engine decodes as WHATWG's Encoding Standard does, which Firefox matches everywhere here. Where Chrome
breaks that standard, the engine keeps WHATWG's answer: Chrome's answer is a decoder bug, and for Big5 it
is not even well-formed text. BROWSER_BUGS lists each such line with Chrome's answer. The gate fails on any
other difference and on a listed one that no longer occurs, so a Chrome fix shows as stale.

Not reached: ISO-2022-JP (escape sequences switch its state, which one sequence per line cannot sweep),
gb18030's four-byte sequences, UTF-16 and `replacement`.

    tools/browser_decoders.py [--browsers chrome,firefox] [--gate]
"""
from __future__ import annotations

import argparse
import os
import sys

import frostwork

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import decoder_sweep  # noqa: E402  (the two-byte space)
from browser_oracle.client import BrowserOracle  # noqa: E402

# WHATWG's legacy encodings, by canonical name
SINGLE_BYTE = [
    "IBM866", "ISO-8859-2", "ISO-8859-3", "ISO-8859-4", "ISO-8859-5", "ISO-8859-6", "ISO-8859-7",
    "ISO-8859-8", "ISO-8859-8-I", "ISO-8859-10", "ISO-8859-13", "ISO-8859-14", "ISO-8859-15",
    "ISO-8859-16", "KOI8-R", "KOI8-U", "macintosh", "windows-874", "windows-1250", "windows-1251",
    "windows-1252", "windows-1253", "windows-1254", "windows-1255", "windows-1256", "windows-1257",
    "windows-1258", "x-mac-cyrillic", "x-user-defined",
]
MULTI_BYTE = ["GBK", "gb18030", "Big5", "EUC-JP", "Shift_JIS", "EUC-KR"]

# (browser, encoding, sequence) -> the browser's line, where it breaks WHATWG's decoder
BROWSER_BUGS: dict[tuple[str, str, str], str] = {
    # the four pointers WHATWG maps to a letter and a combining mark (Ê̄ Ê̌ ê̄ ê̌): Chrome writes a C1
    # control and a lone low surrogate, as if the pair were a surrogate pair cut to eight bits
    ("chrome", "Big5", "8862"): "\x93\udf04", ("chrome", "Big5", "8864"): "\x93\udf0c",
    ("chrome", "Big5", "88a3"): "\xb3\udf04", ("chrome", "Big5", "88a5"): "\xb3\udf0c",
    # after `8F FE` and an LF, Chrome keeps the JIS X 0212 flag WHATWG clears, so the next sequence
    # is looked up in JIS X 0212: `8F FE 0A A1 A1` gives U+FFFD, not U+3000
    ("chrome", "EUC-JP", "a1a1"): "�",
}


def sequences(encoding: str) -> list[bytes]:
    return [bytes([b]) for b in range(0x80, 0x100)] if encoding in SINGLE_BYTE else decoder_sweep.candidates()


def page(encoding: str) -> bytes:
    return b"<pre id=s>\nx\n" + b"\n".join(sequences(encoding)) + b"\n</pre>"


def content_type(encoding: str) -> str:
    return f"text/html; charset={encoding}"


def lines(text: str) -> list[str]:
    """The sequences' lines of the `<pre>`'s text: after the `x` line, before the final LF."""
    return text.split("\n")[1:-1]


def engine_lines(encoding: str) -> list[str]:
    got = frostwork.extract(page(encoding), ["#s::text"], content_type=content_type(encoding))
    return lines(got[0][0])


def differences(encoding: str, browser: list[str], engine: list[str]) -> dict[str, str]:
    """Sequence (hex) -> the browser's line, wherever it differs from the engine's. A line count that
    differs is one difference under the key `lines`."""
    if len(browser) != len(engine):
        return {"lines": f"{len(browser)} lines, the engine {len(engine)}"}
    return {s.hex(): b for s, b, e in zip(sequences(encoding), browser, engine) if b != e}


def gate_failures(found: dict[tuple[str, str, str], str], browsers: list[str], charsets: dict[tuple[str, str], str],
                  labels_resolved: dict[str, str]) -> list[str]:
    """Why the sweep fails: a label the engine does not resolve to itself, a page the browser decoded
    with another encoding, a difference BROWSER_BUGS does not list (or lists with another answer), and a
    listed one for a browser that ran which no longer occurs."""
    fails = [f"the engine resolves {name!r} to {got!r}" for name, got in labels_resolved.items() if got != name]
    fails += [f"{b} decoded the {enc} page as {cs}" for (b, enc), cs in charsets.items() if cs != enc]
    for key, line in sorted(found.items()):
        if BROWSER_BUGS.get(key) != line:
            fails.append(f"{key[0]} {key[1]} {key[2]}: {ascii(line)} where the engine differs")
    fails += [f"stale: {k[0]} {k[1]} {k[2]} now matches the engine"
              for k in sorted(BROWSER_BUGS) if k[0] in browsers and k not in found]
    return fails


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--browsers", default="chrome")
    ap.add_argument("--gate", action="store_true")
    args = ap.parse_args()
    browsers = args.browsers.split(",")
    encodings = SINGLE_BYTE + MULTI_BYTE
    resolved = {e: frostwork.resolve_label(e) for e in encodings}
    engine = {e: engine_lines(e) for e in encodings}
    found: dict[tuple[str, str, str], str] = {}
    charsets: dict[tuple[str, str], str] = {}
    for b in browsers:
        with BrowserOracle(b) as oracle:
            _, answers = oracle.query([page(e) for e in encodings], ["#s::text"],
                                      content_types=[content_type(e) for e in encodings])
            for e, cs in zip(encodings, oracle.last_charsets):
                charsets[(b, e)] = cs
            build = oracle.build
        for e, a in zip(encodings, answers):
            diff = differences(e, lines(a[0][0]) if a else [], engine[e])
            found.update({(b, e, s): line for s, line in diff.items()})
            print(f"{b:8} {e:15} {len(sequences(e)):6} sequences, {len(diff)} differ from the engine")
        print(f"         ({build})")
    for (b, e, s), line in sorted(found.items()):
        listed = "listed" if BROWSER_BUGS.get((b, e, s)) == line else "NOT LISTED"
        print(f"  {b} {e} {s}: {ascii(line)}  ({listed})")
    fails = gate_failures(found, browsers, charsets, resolved)
    for f in fails:
        print(f"  GATE: {f}")
    print(f"  GATE: {'FAIL' if fails else 'PASS'}")
    return 1 if args.gate and fails else 0


if __name__ == "__main__":
    sys.exit(main())
