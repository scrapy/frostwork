"""Oracle-toolchain guard for the differential harness.

Frostwork's correctness bar is value-parity with **libxml2 2.14** (docs/COMPATIBILITY.md — tree
construction is matched empirically to that version, not to the HTML5 spec). `requirements-test.txt`
pins `lxml`, but a pinned lxml does NOT pin the libxml2 it is built against: a wheel VENDORS its own
copy, and the same lxml release ships different ones per platform. lxml 6.1.1 carries libxml2 2.14.x in
its manylinux/macOS wheels and **2.11.9** in the Windows wheel, where CR-in-attribute-values and a raw
`<` in text parse differently — enough for the same Frostwork build to measure 0 DIVERGE on Linux/macOS
and thousands on Windows purely from the oracle (reported by Jan Seidler, 2026-07-23).

So an older oracle is a HARNESS misconfiguration, not an engine result: every verdict it produces is
against the wrong spec. Fail fast and say which piece is wrong, rather than reporting divergences that
the engine is not accountable for. `--allow-old-libxml2` (or `FROSTWORK_ALLOW_OLD_LIBXML2=1`) downgrades
the check to a warning for deliberate exploration on such a platform.
"""

from __future__ import annotations

import os
import sys

# The libxml2 the engine is written against; see docs/TESTING.md ("what the oracle must be").
MIN_LIBXML2 = (2, 14)

_ENV_OVERRIDE = "FROSTWORK_ALLOW_OLD_LIBXML2"


def versions() -> dict:
    """The installed oracle toolchain (imports are lazy so this module stays import-safe)."""
    import cssselect
    import lxml.etree
    import parsel

    return {
        "parsel": parsel.__version__,
        "lxml": lxml.etree.LXML_VERSION,
        "libxml2": lxml.etree.LIBXML_VERSION,
        "cssselect": cssselect.__version__,
    }


def banner() -> str:
    """One line naming every component whose version can move a verdict."""
    v = versions()
    return (
        f"  oracle: parsel={v['parsel']} lxml={'.'.join(map(str, v['lxml']))} "
        f"libxml2={'.'.join(map(str, v['libxml2']))} cssselect={v['cssselect']}"
    )


def add_argument(ap) -> None:
    """Register the escape hatch on a harness `ArgumentParser`."""
    ap.add_argument(
        "--allow-old-libxml2",
        action="store_true",
        help=f"warn instead of exiting when the oracle's libxml2 is older than "
        f"{'.'.join(map(str, MIN_LIBXML2))} (verdicts are then against the wrong spec)",
    )


def require(allow_old: bool = False) -> None:
    """Exit(2) unless the vendored libxml2 is >= :data:`MIN_LIBXML2`. Prints the reason."""
    got = versions()["libxml2"][: len(MIN_LIBXML2)]
    if got >= MIN_LIBXML2:
        return
    want = ".".join(map(str, MIN_LIBXML2))
    have = ".".join(map(str, got))
    msg = (
        f"oracle libxml2 is {have}, but Frostwork's parity contract is libxml2 >= {want}.\n"
        f"{banner()}\n"
        "  A pinned lxml does not pin its vendored libxml2 (the Windows wheel of lxml 6.1.1 carries\n"
        "  2.11.9), and CR-in-attribute-values / raw `<` in text parse differently there — so this run\n"
        "  would grade the engine against the wrong spec. Install an lxml wheel built on libxml2 >=\n"
        f"  {want} (Linux/macOS wheels, or build lxml with --with-libxml2), or pass --allow-old-libxml2 /\n"
        f"  set {_ENV_OVERRIDE}=1 to proceed with a warning."
    )
    if allow_old or os.environ.get(_ENV_OVERRIDE) == "1":
        print(f"WARNING: {msg}\n", file=sys.stderr)
        return
    print(f"oracle-check: {msg}", file=sys.stderr)
    sys.exit(2)
