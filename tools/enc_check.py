"""Encoding parity: pages in various charsets (non-ASCII text + attr values) vs Parsel given the
same label. Validates the engine decodes emitted values per the resolved encoding."""
import subprocess, json, os, sys
from parsel import Selector as PS
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oracle  # parity is defined against libxml2 >= 2.14 here too (meta sniffing moved between them)
oracle.require()
BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "target", "release", "differ")

def engine(body, sels, label):
    line = (label or "") + "\t" + body.hex() + "\t" + "\t".join(sels) + "\n"
    p = subprocess.Popen([BIN], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(line.encode())
    return json.loads(out.decode().splitlines()[0])

# (label, sample non-ASCII text). Labels understood by both Python codecs and encoding_rs/WHATWG.
CASES = [
    ("windows-1252", "café — €5 «prix» ™"),
    ("shift_jis", "日本語のテスト"),
    ("euc-jp", "日本語エンコード"),
    ("gbk", "中文测试内容"),
    ("big5", "繁體中文測試"),
    ("koi8-r", "Привет мир"),
    ("utf-8", "café 日本語 €"),
]
SELS = ["p::text", "a::attr(title)", "div::attr(data-v)", "p.c::text", "span ::text"]
tot = ok = 0
fails = []
for label, sample in CASES:
    html = (f'<html><head><meta charset="{label}"></head><body>'
            f'<p class="c">{sample}</p><a title="{sample}" href="/x">link</a>'
            f'<div data-v="{sample}">d</div><span>a{sample}b</span></body></html>')
    try:
        body = html.encode(label)
    except Exception as e:
        print(f"  {label:14} SKIP (python can't encode: {e})"); continue
    mine = engine(body, SELS, label)
    for si, s in enumerate(SELS):
        try:
            th = PS(body=body, encoding=label).css(s).getall()
        except Exception:
            th = None
        if th is None:
            continue
        tot += 1
        if mine[si] == th:
            ok += 1
        else:
            fails.append((label, s, mine[si], th))
    # also test BOM-sniffed UTF-16 for this sample (no label -> sniff)
print(f"explicit-label parity: {ok}/{tot} match")
for label, s, m, t in fails[:8]:
    print(f"  MISMATCH [{label}] {s}: mine={m} lxml={t}")

# comment decoy (T7): `charset=` in a comment/visible text must NOT switch the decode; no label -> the
# body is real UTF-8 and must stay UTF-8, matching w3lib/Scrapy (which strip comments before sniffing).
decoy = ('<!-- saved from url=(0037)https://x/ charset=big5 -->'
         '<html><head></head><body><p class="c">café 日本語</p></body></html>').encode("utf-8")
mine = engine(decoy, ["p.c::text"], None)
oracle = PS(body=decoy, encoding="utf-8").css("p.c::text").getall()  # w3lib ignores the comment -> utf-8
if mine[0] != oracle:
    fails.append(("sniffed", "comment charset= decoy", mine[0], oracle))
print(f"comment charset= decoy (sniffed): mine={mine[0]} utf-8-oracle={oracle} "
      f"-> {'OK' if mine[0] == oracle else 'MISMATCH'}")

# UTF-16 via BOM (encoding sniffed from BOM, label None)
u16 = '<html><body><p class="c">café 日本語</p></body></html>'.encode("utf-16")  # adds BOM
mine = engine(u16, ["p::text"], None)
# oracle = decode-first (what Scrapy does); lxml's body+encoding path can't parse UTF-16 bytes
oracle = PS(text=u16.decode("utf-16")).css("p::text").getall()
if mine[0] != oracle:
    fails.append(("bom-sniffed", "UTF-16", mine[0], oracle))
ok = "OK" if mine[0] == oracle else "MISMATCH"
print(f"UTF-16 (BOM-sniffed): mine={mine[0]} decode-first-oracle={oracle} -> {ok}  (lxml body-path can't do UTF-16)")

# ---- charset resolution: SHARED cases vs w3lib, then the deliberate BROWSER differences -----------
# Parsel is the oracle for VALUES, but not for SNIFFING: `PS(body=…)` with no encoding never looks at
# `<meta>` at all, it just defaults to UTF-8. Oracling the prescan against it was vacuous — it "agreed"
# on every declaration we MISSED, because both produced mojibake. Scrapy resolves a response encoding
# with w3lib.encoding.html_to_unicode, so that is the oracle for the SHARED cases below.
#
# w3lib is NOT the target, though, and pretending it is made this file misleading: the intended policy is
# browser/WHATWG correctness, so wherever the two disagree the browser wins and the difference is an
# ASSERTION, not a mismatch. The file is therefore split in two — `SHARED_PRESCAN_CASES` (must equal
# w3lib) and `BROWSER_DIFFERENCES` (must equal the browser answer, AND w3lib must still give the other
# one, so a list entry cannot rot into a silent agreement).
from w3lib.encoding import html_to_unicode  # noqa: E402

U8 = b'<p class="c">caf\xc3\xa9</p>'
W1252 = b'<p class="c">caf\xe9</p>'
XUD = b'<p class="c">caf\xe9</p>'          # the same bytes read as x-user-defined
SHARED_PRESCAN_CASES = [
    ("data-http-equiv is not http-equiv", b'<meta data-http-equiv="content-type" content="text/html; charset=big5">', U8),
    ("http-equiv=refresh declares nothing", b'<meta http-equiv="refresh" content="0; url=/x?charset=big5">', U8),
    ("charset= in an unrelated attribute", b'<meta http-equiv="content-type" content="text/html" data-note="charset=big5">', U8),
    ("quoted > does not end the tag", b'<meta http-equiv="content-type" content="text/html; charset=windows-1252" title="a>b">', W1252),
    ("whitespace around =", b"<meta charset\n=\nwindows-1252>", W1252),
    ("abrupt comment close <!-->", b"<!--><meta charset=windows-1252>", W1252),
    ("comment-end-bang --!>", b"<!--x--!><meta charset=windows-1252>", W1252),
    ("content= without http-equiv", b'<meta content="text/html; charset=big5">', U8),
    ("http-equiv content-type", b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">', W1252),
    ("bare charset attribute", b"<meta charset=utf-8>", U8),
    # A declaration is honoured wherever it is in the head, not just in WHATWG's suggested first 1024
    # bytes. That number is a STREAMING budget ("encouraged to ... prescan the first 1024 bytes, but not
    # to stall beyond that"), and nothing stalls here — the whole document is already in memory. Three
    # pages in one 1000-page Common Crawl sample put their Content-Type at byte 1080/1532/1611 behind a
    # producer comment or a block of og: metas, and every value on them came back full of U+FFFD.
    ("declaration past byte 1024", b"<!--" + b"x" * 1100 + b'--><meta charset="windows-1252">', W1252),
    # ...but the window is still bounded, and at the same place w3lib bounds it, so a declaration past
    # 4096 is not a declaration in either. Keeps "widen it" from silently becoming "unbounded".
    ("declaration past byte 4096", b"<!--" + b"x" * 4200 + b'--><meta charset="windows-1252">', W1252),
]
# XML-declaration compatibility. These need the declaration at offset 0 (where an XML declaration is the
# only thing that may appear), so they are whole documents rather than `<head>` fragments. Browsers honour
# it for XHTML-era pages served as text/html and so does w3lib, which puts them in the SHARED set.
SHARED_DOC_CASES = [
    ('<?xml encoding="ISO-8859-1"?>',
     b'<?xml version="1.0" encoding="ISO-8859-1"?><html><body>' + W1252 + b"</body></html>"),
    ("<?xml encoding='windows-1252'?>",
     b"<?xml version='1.0' encoding='windows-1252'?><html><body>" + W1252 + b"</body></html>"),
    ('<?xml encoding="utf-8"?>',
     b'<?xml version="1.0" encoding="utf-8"?><html><body>' + U8 + b"</body></html>"),
    # read in document order, so a first-position XML declaration outranks a later <meta>
    ('<?xml encoding="ISO-8859-1"?> before <meta utf-8>',
     b'<?xml version="1.0" encoding="ISO-8859-1"?><html><head><meta charset="utf-8"></head>'
     b"<body>" + W1252 + b"</body></html>"),
    # ...and a declaration with no usable encoding falls through to the <meta> scan
    ('<?xml version="1.0"?> then <meta windows-1252>',
     b'<?xml version="1.0"?><html><head><meta charset="windows-1252"></head><body>'
     + W1252 + b"</body></html>"),
]
for label, head, body in SHARED_PRESCAN_CASES:
    doc = b"<html><head>" + head + b"</head><body>" + body + b"</body></html>"
    _, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
    oracle = PS(text=txt).css("p.c::text").getall()
    mine = engine(doc, ["p.c::text"], None)
    if mine[0] != oracle:
        fails.append(("meta-prescan", label, mine[0], oracle))
    print(f"meta prescan [{label}]: mine={mine[0]} w3lib={oracle} "
          f"-> {'OK' if mine[0] == oracle else 'MISMATCH'}")
for label, doc in SHARED_DOC_CASES:
    _, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
    oracle = PS(text=txt).css("p.c::text").getall()
    mine = engine(doc, ["p.c::text"], None)
    if mine[0] != oracle:
        fails.append(("meta-prescan", label, mine[0], oracle))
    print(f"meta prescan [{label}]: mine={mine[0]} w3lib={oracle} "
          f"-> {'OK' if mine[0] == oracle else 'MISMATCH'}")

# ---- DELIBERATE differences from w3lib: browser/WHATWG behaviour, asserted both ways --------------
# Each row is (name, document, what Frostwork must produce, what w3lib produces, why we differ). The
# "what w3lib produces" column is checked too: if w3lib ever starts agreeing, the row is STALE and this
# fails, which is the same anti-rot discipline as WHATWG_ONLY_BYTES / BIG5_INDEX_DIVERGENCE below. The
# reasons are the contract text in docs/COMPATIBILITY.md; keep the two in step.
_U32LE = b"\xff\xfe\x00\x00" + '<html><body><p class="c">café</p></body></html>'.encode("utf-32-le")
_U16LE_NOBOM = '<?xml version="1.0"?><html><body><p class="c">café</p></body></html>'.encode("utf-16-le")
_U16BE_NOBOM = '<?xml version="1.0"?><html><body><p class="c">café</p></body></html>'.encode("utf-16-be")
BROWSER_DIFFERENCES = [
    ("charset inside a COMMENT is ignored",
     b"<html><head><!-- <meta charset=big5> --></head><body>" + U8 + b"</body></html>",
     ["café"], ["caf矇"],
     "w3lib has no comment handling; WHATWG's prescan and every browser skip comments"),
    ("the prescan does NOT stop at <body>",
     b'<html><head></head><body><meta charset="windows-1252">' + W1252 + b"</body></html>",
     ["café"], ["caf�"],
     "w3lib's regex has a `|body` alternative and gives up there; browsers honour a late <meta charset>"),
    ("an invalid charset does not end the prescan",
     b'<html><head><meta charset="not-a-charset"><meta charset="windows-1252"></head><body>'
     + W1252 + b"</body></html>",
     ["café"], ["caf�"],
     "WHATWG: an unsupported label is 'failure, continue'; w3lib stops at its first regex hit"),
    ("<meta charset=utf-16> is read as UTF-8",
     b'<html><head><meta charset="utf-16"></head><body>' + U8 + b"</body></html>",
     ["café"], [],
     "the prescan could only read that declaration by treating the bytes as ASCII-compatible, so it "
     "contradicts itself; w3lib honours it and Parsel then finds nothing in the garbage"),
    ("<meta charset=x-user-defined> means windows-1252",
     b'<html><head><meta charset="x-user-defined"></head><body>' + XUD + b"</body></html>",
     ["café"], ["caf�"],
     "'get an encoding from a meta element' step 5; w3lib does not resolve the label at all and falls "
     "back to its default, and taking it literally maps every high byte into the private use area"),
    ("BOM-less UTF-16LE is detected from the XML prefix",
     _U16LE_NOBOM, ["café"], ["caf�"],
     "XML 1.0 Appendix F (and libxml2's own xmlDetectEncoding); w3lib only looks for a real BOM"),
    ("BOM-less UTF-16BE is detected from the XML prefix",
     _U16BE_NOBOM, ["café"], ["caf�"],
     "as above, big-endian"),
    ("an XML declaration counts only at offset 0",
     b'<html><head><?xml version="1.0" encoding="windows-1252"?></head><body>' + U8 + b"</body></html>",
     ["café"], ["cafÃ©"],
     "w3lib's regex searches for `<?xml … encoding=…` anywhere in its window; a `<?` after the start of "
     "the document is a bogus comment to a browser and declares nothing"),
    ("a UTF-32 BOM is not a BOM",
     _U32LE, ["café"], ["café"],
     "the WHATWG Encoding Standard has no UTF-32, so the leading FF FE IS the UTF-16LE BOM and the "
     "document is read as UTF-16LE — which, with NUL deletion, happens to agree with w3lib's UTF-32 "
     "decode for BMP text; w3lib recognizes UTF-32 explicitly"),
]
for name, doc, want_mine, want_w3, why in BROWSER_DIFFERENCES:
    mine = engine(doc, ["p.c::text"], None)[0]
    try:
        _, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
        w3 = PS(text=txt).css("p.c::text").getall()
    except Exception as e:                       # pragma: no cover — w3lib should not raise here
        w3 = [f"ERROR {e}"]
    if mine != want_mine:
        fails.append(("browser-diff", f"{name}: Frostwork must produce the browser answer", mine,
                      want_mine))
    if w3 != want_w3:
        fails.append(("browser-diff", f"{name}: w3lib now produces {w3!r}, not the {want_w3!r} this row "
                                      f"documents — the difference list is STALE", w3, want_w3))
    ok_row = mine == want_mine and w3 == want_w3
    print(f"browser difference [{name}]: mine={mine} w3lib={w3} -> {'OK' if ok_row else 'MISMATCH'}")
    print(f"    why: {why}")

# ---------------------------------------------------------------- DECODER divergence from Parsel
# The engine decodes with `encoding_rs` (the WHATWG Encoding Standard — what browsers do). Parsel decodes
# with Python's stdlib codecs, and the two are NOT the same function: a WHATWG index is TOTAL, so every
# byte maps to a character, while Python's `cp1252` leaves five bytes undefined and yields U+FFFD for them.
# WHATWG is the correct, lossless behaviour, so per the project's oracle-bug policy we keep it and
# enumerate the difference here instead of matching a codec that discards data.
#
# The list is checked in BOTH directions: a byte outside it that diverges FAILS (a real regression), and a
# listed byte that starts AGREEING also fails, so the list cannot rot. That matters because the 35 parity
# vectors above are all ordinary text — they never touch these bytes, so nothing else here can see them.
WHATWG_ONLY_BYTES = {
    # byte -> the character the WHATWG windows-1252 index defines (Python's cp1252: undefined -> U+FFFD)
    0x81: "\u0081", 0x8D: "\u008d", 0x8F: "\u008f", 0x90: "\u0090", 0x9D: "\u009d",
}
SKIP_BYTES = set(b"<>&\r\n\t \x00")  # HTML delimiters / whitespace: not a decoder question
for label in ("windows-1252", "iso-8859-1"):  # WHATWG maps the iso-8859-1 LABEL to windows-1252
    probe = [bytes([b]) for b in range(0x20, 0x100) if b not in SKIP_BYTES]
    ps = b"".join(b'<p class="c">' + s_ + b"</p>" for s_ in probe)
    doc = b'<html><head><meta charset="' + label.encode() + b'"></head><body>' + ps + b"</body></html>"
    mine = engine(doc, ["p.c::text"], None)[0]
    _, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
    theirs = PS(text=txt).css("p.c::text").getall()
    diverged = {s_[0] for s_, m, t in zip(probe, mine, theirs) if m != t}
    unexpected = diverged - set(WHATWG_ONLY_BYTES)
    stale = set(WHATWG_ONLY_BYTES) - diverged
    for b in sorted(unexpected):
        fails.append(("decoder", f"{label} byte {b:#04x} diverges from Parsel and is not a known "
                                 f"WHATWG-only byte", "?", "?"))
    for b in sorted(stale):
        fails.append(("decoder", f"{label} byte {b:#04x} no longer diverges — drop it from "
                                 f"WHATWG_ONLY_BYTES", "?", "?"))
    # and the character we produce must be the one WHATWG defines, not merely "something"
    for s_, m in zip(probe, mine):
        want = WHATWG_ONLY_BYTES.get(s_[0])
        if want is not None and m != want:
            fails.append(("decoder", f"{label} byte {s_[0]:#04x}", [m], [want]))
    print(f"decoder [{label}]: {len(diverged)} of {len(probe)} bytes differ from Parsel; "
          f"expected exactly {sorted(hex(b) for b in WHATWG_ONLY_BYTES)} "
          f"-> {'OK' if not unexpected and not stale else 'MISMATCH'}")

# WHATWG: a `<meta charset=utf-16*>` becomes UTF-8, because the prescan could only READ that declaration
# by treating the bytes as ASCII-compatible — the declaration contradicts itself. w3lib honours the label,
# decodes the whole document as UTF-16 and produces garbage Parsel finds nothing in. Divergence in our
# favour, pinned here so it cannot regress into honouring the label.
for _lab in (b"utf-16", b"utf-16le", b"utf-16be", b"UTF-16"):
    _doc = (b'<html><head><meta charset="' + _lab + b'"></head><body><p class="c">'
            + b"caf\xc3\xa9" + b"</p></body></html>")
    _mine = engine(_doc, ["p.c::text"], None)[0]
    if _mine != ["café"]:
        fails.append(("meta-prescan", f"<meta charset={_lab.decode()}> must be read as UTF-8",
                      _mine, ["café"]))
# ...and a genuine UTF-16 document (BOM) must still decode as UTF-16
_doc16 = '<html><head></head><body><p class="c">café</p></body></html>'.encode("utf-16")
_m16 = engine(_doc16, ["p.c::text"], None)[0]
if _m16 != ["café"]:
    fails.append(("bom", "real UTF-16 document with a BOM", _m16, ["café"]))
print(f"meta prescan [utf-16 label -> UTF-8, BOM still UTF-16]: "
      f"-> {'OK' if not [f for f in fails if f[0] in ('bom',) or 'utf-16' in str(f[1])] else 'MISMATCH'}")

# The multi-byte counterpart. WHATWG's big5 index resolves a handful of DUPLICATE pointers to different
# code points than Python's `big5hkscs` — these are assigned characters a real page can contain, so unlike
# the unassigned ranges they are worth naming one by one. Enumerated over every assigned two-byte
# sequence (~18k), both directions gated: a new divergence fails, and one that disappears fails too.
#
# The other legacy labels are swept the SAME exhaustive way, and an earlier version of this file did not
# sweep them: it sampled "800 assigned characters each" and concluded shift_jis / euc-jp / euc-kr /
# gb18030 were at full parity. They are not, and a real crawled page is what proved it — an EUC-JP wiki
# whose text contains the byte pair A1 C1. That is the JIS wave dash, which Python's `euc_jp` maps to
# U+301C and the WHATWG index (like CP932, like every browser) maps to U+FF5E. A sample big enough to
# feel thorough missed a character common enough to appear in the first 1000-page crawl sample, so the
# sample is gone: every label below is enumerated over every assigned two-byte sequence.
#
# Two classes of difference, kept apart because only one of them is about characters a page MEANS:
#
#  * `INDEX_DIVERGENCE` — the two indexes give the same sequence DIFFERENT real characters. Small enough
#    to name one by one, and each is a byte pair a real page can contain, so each is listed with both
#    answers and gated in both directions.
#  * `PUA_UNASSIGNED` — Parsel returns a PRIVATE-USE character (cp932's user-defined area) where the
#    WHATWG index has nothing at all. Not text with a meaning, and there are hundreds, so the COUNT is
#    gated rather than the list. A sequence moving between the two classes fails either way.
INDEX_DIVERGENCE = {
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
        b"\xa3\xa0": ("　", ""), b"\xa6\xd9": ("︐", ""),
        b"\xa6\xda": ("︒", ""), b"\xa6\xdb": ("︑", ""),
        b"\xa6\xdc": ("︓", ""), b"\xa6\xdd": ("︔", ""),
        b"\xa6\xde": ("︕", ""), b"\xa6\xdf": ("︖", ""),
        b"\xa6\xec": ("︗", ""), b"\xa6\xed": ("︘", ""),
        b"\xa6\xf3": ("︙", ""), b"\xa8\xbc": ("ḿ", ""),
        b"\xfe\x59": ("龴", ""), b"\xfe\x61": ("龵", ""),
        b"\xfe\x66": ("龶", ""), b"\xfe\x67": ("龷", ""),
        b"\xfe\x6d": ("龸", ""), b"\xfe\x7e": ("龹", ""),
        b"\xfe\x90": ("龺", ""), b"\xfe\xa0": ("龻", ""),
    },
    "euc-kr": {},      # full parity across all ~17k assigned sequences
    "shift_jis": {},   # no real-character disagreement at all; see PUA_UNASSIGNED
}
# The Python codec w3lib resolves each label to — it TRANSLATES (big5 -> big5hkscs, shift_jis -> cp932,
# euc-kr -> cp949) — used to enumerate which sequences are assigned in the first place.
PY_CODEC = {"big5": "big5hkscs", "shift_jis": "cp932", "euc-jp": "euc_jp",
            "euc-kr": "cp949", "gb18030": "gb18030"}
PUA_UNASSIGNED = {"big5": 0, "shift_jis": 762, "euc-jp": 0, "euc-kr": 0, "gb18030": 0}
# WHATWG's index assigns a REAL character and the Python codec assigns nothing (Parsel gets U+FFFD).
# This class was structurally invisible until the sweep stopped enumerating over the sequences PYTHON
# calls assigned: `euc_jp` is strict JIS X 0208 and has no NEC row 13, so `AD A1` — the `①` that shows up
# in ordinary Japanese prose — decoded to U+FFFD for the oracle and `①` here, and a crawled page hit it.
# Browsers use the WHATWG index, so the ENGINE is right and this is an oracle limitation, counted rather
# than named: it is hundreds of sequences (row 13 plus the IBM extension rows), not a handful.
WHATWG_ONLY = {"big5": 192, "shift_jis": 0, "euc-jp": 457, "euc-kr": 0, "gb18030": 0}
# Both sides replace an INVALID sequence; only the SHAPE differs — WHATWG's decoder emits one U+FFFD per
# maximal subpart (encoding_rs implements it), while Python's `errors="replace"` replaces per byte. No
# real character disagrees, so this is also a count rather than a list.
REPLACEMENT_SHAPE = {"big5": 4950, "shift_jis": 1304, "euc-jp": 4793, "euc-kr": 2560, "gb18030": 0}


def _classify(mine, theirs):
    """Which of the four ways these two decoders can disagree about one byte sequence is this?"""
    if mine == theirs:
        return "agree"
    if _pua_for_unassigned(mine, theirs):
        return "pua"
    if "�" not in mine and "�" in theirs:
        return "whatwg_only"
    if "�" in mine and "�" in theirs:
        return "replacement_shape"
    return "real"  # a real character disagrees — small enough to name one by one


def _pua_for_unassigned(mine, theirs):
    """Do the two values differ ONLY where Parsel has a private-use character and we have U+FFFD?

    Position-wise, not whole-string: a sequence in cp932's user-defined area often decodes to a PUA
    character followed by an ordinary one (`` + halfwidth katakana), and only the first half is
    the vendor extension.
    """
    if len(mine) != len(theirs):
        return False
    return all(m == t or (m == "�" and 0xE000 <= ord(t) <= 0xF8FF)
               for m, t in zip(mine, theirs))


for _label, _expected in INDEX_DIVERGENCE.items():
    # EVERY two-byte sequence, not the ones Python happens to decode. Filtering on the Python codec is how
    # this sweep read "full parity" while `euc_jp` was returning U+FFFD for hundreds of assigned
    # characters: a sequence it rejects was simply skipped, so the whole `WHATWG_ONLY` class below could
    # not be counted, let alone gated.
    _probe = [bytes([_l, _t]) for _l in range(0x81, 0xFF) for _t in range(0x40, 0xFF)
              if _l not in b"<>&" and _t not in b"<>&\r\n\t \x00"]
    _diff, _counts = {}, {"pua": 0, "whatwg_only": 0, "replacement_shape": 0}
    for _i in range(0, len(_probe), 4000):
        _part = _probe[_i:_i + 4000]
        _ps = b"".join(b'<p class="c">' + _s + b"</p>" for _s in _part)
        _doc = (b'<html><head><meta charset="' + _label.encode() + b'"></head><body>'
                + _ps + b"</body></html>")
        _mine = engine(_doc, ["p.c::text"], None)[0]
        _, _txt = html_to_unicode(None, _doc, auto_detect_fun=None, default_encoding="utf8")
        _theirs = PS(text=_txt).css("p.c::text").getall()
        for _s, _m, _t in zip(_part, _mine, _theirs):
            _kind = _classify(_m, _t)
            if _kind == "real":
                _diff[_s] = (_m, _t)
            elif _kind != "agree":
                _counts[_kind] += 1
    for _s in sorted(set(_diff) - set(_expected)):
        fails.append(("decoder", f"{_label} {_s.hex()} diverges and is not a listed index difference",
                      [_diff[_s][0]], [_diff[_s][1]]))
    for _s in sorted(set(_expected) - set(_diff)):
        fails.append(("decoder", f"{_label} {_s.hex()} no longer diverges — drop it from "
                                 f"INDEX_DIVERGENCE[{_label!r}]", "?", "?"))
    for _s, _pair in _expected.items():
        if _s in _diff and _diff[_s] != _pair:
            fails.append(("decoder", f"{_label} {_s.hex()} maps differently now", [_diff[_s]], [_pair]))
    for _kind, _want in (("pua", PUA_UNASSIGNED[_label]),
                         ("whatwg_only", WHATWG_ONLY[_label]),
                         ("replacement_shape", REPLACEMENT_SHAPE[_label])):
        if _counts[_kind] != _want:
            fails.append(("decoder", f"{_label}: {_counts[_kind]} sequences in the {_kind} class, not "
                                     f"the {_want} recorded", _counts[_kind], _want))
    _ok = not (set(_diff) ^ set(_expected)) and all(
        _counts[_k] == _w for _k, _w in (("pua", PUA_UNASSIGNED[_label]),
                                         ("whatwg_only", WHATWG_ONLY[_label]),
                                         ("replacement_shape", REPLACEMENT_SHAPE[_label])))
    print(f"decoder [{_label} index]: all {len(_probe)} two-byte sequences: {len(_diff)} disagree on a "
          f"real character (expected {len(_expected)}), {_counts['whatwg_only']} are WHATWG-assigned and "
          f"Python-unassigned (expected {WHATWG_ONLY[_label]}), {_counts['pua']} Parsel-PUA (expected "
          f"{PUA_UNASSIGNED[_label]}), {_counts['replacement_shape']} differ only in replacement shape "
          f"(expected {REPLACEMENT_SHAPE[_label]}) -> {'OK' if _ok else 'MISMATCH'}")

# THE GATE: any mismatch above is an encoding regression. Without this the target printed MISMATCH and
# still exited 0, so `make gate` and hosted CI stayed green through an encoding bug.
print(f"\nENCODING GATE: mismatches = {len(fails)}  ->  {'PASS' if not fails else 'FAIL'}")
for label, s_, m, t in fails:
    print(f"  MISMATCH [{label}] {s_}: mine={m} lxml={t}")
if fails:
    raise SystemExit(1)
