"""The legacy multi-byte DECODER sweep: which two-byte sequences to try, and how a disagreement with
Python's codec is classified.

Split out of `tools/enc_check.py` so it can be IMPORTED. That script runs its gate at module level, so
nothing in it can be exercised by a test, and this is the part where a mistake is invisible: an
enumeration that quietly narrows, or a classifier that files a real character difference into a bulk
"expected" count, both read exactly like a clean run. `tests/test_gates.py` owns those two failure modes
and can only do it against real functions.

The history that shaped it, because it is the reason for every choice here:

  * The sweep used to SAMPLE — 800 assigned characters per label — and concluded shift_jis / euc-jp /
    euc-kr / gb18030 were at full parity. They are not. A crawled EUC-JP wiki containing `A1 C1` (the JIS
    wave dash: U+FF5E to WHATWG and every browser, U+301C to Python's `euc_jp`) walked straight through.
  * It then enumerated over the sequences the PYTHON codec calls assigned, which is a filter shaped like
    the oracle's own limitations. `euc_jp` is strict JIS X 0208 with no NEC row 13, so `AD A1` — the `①`
    of ordinary Japanese prose — was simply skipped, and the whole `WHATWG_ONLY` class below could not be
    counted, let alone gated. The enumeration is now over the byte space.

Which side is right differs per class, so they are kept apart rather than summed: WHATWG's indexes are
what browsers implement, so the ENGINE is right for `WHATWG_ONLY` and `PUA_UNASSIGNED`, while
`INDEX_DIVERGENCE` names the handful of real characters where the two indexes genuinely disagree.
"""
from __future__ import annotations

# Every two-byte sequence in the legacy lead/trail space. NOT filtered against a Python codec (see the
# module docstring), and no byte needs excluding for the markup the probe wraps them in: `<`, `>`, `&`,
# the whitespace bytes and NUL are all below both ranges. `tests/test_gates.py` asserts that property
# rather than trusting it — a filter for those bytes used to be written here and was dead code.
LEAD = range(0x81, 0xFF)
TRAIL = range(0x40, 0xFF)
#: bytes that would end the probe's `<p class="c">…</p>` wrapper early, or be stripped from it
MARKUP_BYTES = b"<>&\r\n\t \x00"


def candidates() -> list[bytes]:
    """Every two-byte sequence the sweep decodes, in a stable order."""
    return [bytes([lead, trail]) for lead in LEAD for trail in TRAIL]


def pua_for_unassigned(mine: str, theirs: str) -> bool:
    """Do the two values differ ONLY where Parsel has a private-use character and we have U+FFFD?

    Position-wise, not whole-string: a sequence in cp932's user-defined area often decodes to a PUA
    character followed by an ordinary one (`` + halfwidth katakana), and only the first half is
    the vendor extension.
    """
    if len(mine) != len(theirs):
        return False
    return all(m == t or (m == "�" and 0xE000 <= ord(t) <= 0xF8FF)
               for m, t in zip(mine, theirs))


def classify(mine: str, theirs: str) -> str:
    """Which of the four ways two decoders can disagree about one byte sequence is this?

    `real` is the only class that names a character a page MEANS, so it is the only one listed pair by
    pair; the other three are counted, because each runs to hundreds or thousands of sequences.
    """
    if mine == theirs:
        return "agree"
    if pua_for_unassigned(mine, theirs):
        return "pua"                # Parsel has a vendor private-use char, WHATWG has nothing
    if "�" not in mine and "�" in theirs:
        return "whatwg_only"        # WHATWG assigns a real character, the Python codec does not
    if "�" in mine and "�" in theirs:
        return "replacement_shape"  # both replace; only the number of U+FFFD differs
    return "real"


# WHATWG's index resolves a handful of duplicate/legacy pointers to different code points than Python's
# codec. These are assigned characters a real page can contain, so unlike the bulk classes they are named
# one by one, with BOTH answers, and gated in both directions: a new one fails, and one that stops
# diverging fails as stale.
INDEX_DIVERGENCE: dict[str, dict[bytes, tuple[str, str]]] = {
    # WHATWG resolves several duplicate big5 pointers differently from `big5hkscs`
    "big5": {
        b"\xa1\x45": ("‧", "•"), b"\xa1\x4e": ("﹑", "､"),
        b"\xa1\xc2": ("¯", "‾"), b"\xa1\xe3": ("～", "∼"),
        b"\xa1\xf2": ("⊕", "♁"), b"\xa1\xf3": ("⊙", "☉"),
        b"\xa2\x41": ("∕", "／"), b"\xa2\x42": ("﹨", "＼"),
        b"\xa2\x44": ("￥", "¥"), b"\xa2\x46": ("￠", "¢"),
        b"\xa2\x47": ("￡", "£"),
    },
    # The JIS-vs-CP932 round-trip family: wave dash, double vertical line, minus, cent, pound, not sign.
    # WHATWG standardised on the Microsoft mappings because that is what the web contains; Python's
    # `euc_jp` keeps the JIS ones. A1 C1 is the one a crawled page actually hit.
    "euc-jp": {
        b"\xa1\xc1": ("～", "〜"), b"\xa1\xc2": ("∥", "‖"),
        b"\xa1\xdd": ("－", "−"), b"\xa1\xf1": ("￠", "¢"),
        b"\xa1\xf2": ("￡", "£"), b"\xa2\xcc": ("￢", "¬"),
    },
    # GB18030-2005 moved these OUT of the private use area and gave them real code points. WHATWG's index
    # is the newer revision and Python's `gb18030` is the older one, so here it is the ENGINE that
    # returns a real character and Parsel that returns a PUA placeholder.
    "gb18030": {
        b"\xa3\xa0": ("\u3000", "\ue5e5"),  b"\xa6\xd9": ("︐", "\ue78d"),
        b"\xa6\xda": ("︒", "\ue78e"),  b"\xa6\xdb": ("︑", "\ue78f"),
        b"\xa6\xdc": ("︓", "\ue790"),  b"\xa6\xdd": ("︔", "\ue791"),
        b"\xa6\xde": ("︕", "\ue792"),  b"\xa6\xdf": ("︖", "\ue793"),
        b"\xa6\xec": ("︗", "\ue794"),  b"\xa6\xed": ("︘", "\ue795"),
        b"\xa6\xf3": ("︙", "\ue796"),  b"\xa8\xbc": ("ḿ", "\ue7c7"),
        b"\xfe\x59": ("龴", "\ue81e"),  b"\xfe\x61": ("龵", "\ue826"),
        b"\xfe\x66": ("龶", "\ue82b"),  b"\xfe\x67": ("龷", "\ue82c"),
        b"\xfe\x6d": ("龸", "\ue832"),  b"\xfe\x7e": ("龹", "\ue843"),
        b"\xfe\x90": ("龺", "\ue854"),  b"\xfe\xa0": ("龻", "\ue864"),
    },
    "euc-kr": {},      # full parity across every assigned sequence
    "shift_jis": {},   # no real-character disagreement at all; see PUA_UNASSIGNED
}
#: Parsel returns a PRIVATE-USE character (cp932's user-defined area) where WHATWG has nothing at all.
PUA_UNASSIGNED = {"big5": 0, "shift_jis": 762, "euc-jp": 0, "euc-kr": 0, "gb18030": 0}
#: WHATWG assigns a REAL character and the Python codec assigns nothing (Parsel gets U+FFFD). Browsers
#: use the WHATWG index, so the ENGINE is right and this is an oracle limitation — `AD A1` is the witness.
WHATWG_ONLY = {"big5": 192, "shift_jis": 0, "euc-jp": 457, "euc-kr": 0, "gb18030": 0}
#: Both sides replace an INVALID sequence and only the SHAPE differs — WHATWG's decoder emits one U+FFFD
#: per maximal subpart (encoding_rs implements it), Python's `errors="replace"` one per byte.
REPLACEMENT_SHAPE = {"big5": 4950, "shift_jis": 1304, "euc-jp": 4793, "euc-kr": 2560, "gb18030": 0}
#: the labels swept. Every one of them, because a label with no row is not evidence of parity.
LABELS = tuple(INDEX_DIVERGENCE)
#: the bulk classes, each gated by COUNT
BULK = ("pua", "whatwg_only", "replacement_shape")


def expected_counts(label: str) -> dict[str, int]:
    return {"pua": PUA_UNASSIGNED[label], "whatwg_only": WHATWG_ONLY[label],
            "replacement_shape": REPLACEMENT_SHAPE[label]}


def verify(label: str, diff: dict[bytes, tuple[str, str]],
           counts: dict[str, int]) -> list[tuple[str, str, object, object]]:
    """Grade one label's sweep: `(kind, message, mine, theirs)` for every mismatch, empty if clean.

    Both directions, for the named class and the counted ones alike — a divergence that DISAPPEARS is as
    much a drift of the contract as a new one, and leaving it unreported is how an allow-list outlives
    the thing it documents.
    """
    expected = INDEX_DIVERGENCE[label]
    fails: list[tuple[str, str, object, object]] = []
    for s in sorted(set(diff) - set(expected)):
        fails.append(("decoder", f"{label} {s.hex()} diverges and is not a listed index difference",
                      [diff[s][0]], [diff[s][1]]))
    for s in sorted(set(expected) - set(diff)):
        fails.append(("decoder", f"{label} {s.hex()} no longer diverges — drop it from "
                                 f"INDEX_DIVERGENCE[{label!r}]", "?", "?"))
    for s, pair in expected.items():
        if s in diff and diff[s] != pair:
            fails.append(("decoder", f"{label} {s.hex()} maps differently now", [diff[s]], [pair]))
    for kind, want in expected_counts(label).items():
        if counts[kind] != want:
            fails.append(("decoder", f"{label}: {counts[kind]} sequences in the {kind} class, not the "
                                     f"{want} recorded", counts[kind], want))
    return fails
