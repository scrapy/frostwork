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
    # A label the oracle toolchain cannot handle is an UNTESTED label, not a passing one. Both arms here
    # must not `continue` silently: a codec or parsel change would then drop a whole charset out of
    # the sweep while it went on printing a ratio of the cases that were left.
    try:
        body = html.encode(label)
    except Exception as e:
        fails.append((label, "python cannot encode the sample — the label is untested", str(e), "-"))
        continue
    mine = engine(body, SELS, label)
    for si, s in enumerate(SELS):
        try:
            th = PS(body=body, encoding=label).css(s).getall()
        except Exception as e:
            fails.append((label, f"parsel raised on {s} — the cell is untested", str(e), "-"))
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
_DEEP = (b"<html><head><!--" + b"x" * 4200 + b'--><meta charset="windows-1252"></head><body>'
         + W1252 + b"</body></html>")
BROWSER_DIFFERENCES = [
    ("a HEAD declaration is honoured at any depth",
     _DEEP, ["café"], ["caf�"],
     "w3lib stops at 4096 and Frostwork used to stop there too, for no reason but w3lib parity. "
     "WHATWG's 1024 is a STREAMING budget ('not to stall beyond that'), and a browser that meets the "
     "<meta charset> after it runs 'change the encoding' and re-decodes what it already has. Measured "
     "in Chrome at 1KB/4KB/16KB/64KB/256KB/1MB — honoured at every one, so in the head there is no "
     "bound to match. The BODY half of the rule is the opposite and is gated separately below"),
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
    ("a stray quote is PART of an unquoted charset value, so the label is invalid",
     b'<html><head><meta http-equiv="content-type" content="text/html" charset=big5" />'
     b'<meta charset="windows-1252"></head><body>' + W1252 + b"</body></html>",
     ["café"], ["caf�"],
     "found on a crawled page that writes `content=\"text/html\" charset=iso-8859-1\" />` — a BARE charset "
     "attribute with the quote misplaced. In the prescan's `get an attribute`, an unquoted value ends only "
     "at whitespace or `>`, so the value is `big5\"`, which resolves to nothing and the scan CONTINUES; "
     "html5lib agrees (it reports windows-1252 here, i.e. it ignored the declaration). w3lib's regex stops "
     "the value at the quote and honours `big5` instead, which no browser does"),
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

# ---- the BODY half of the depth rule: a declaration past the floor is not a declaration ----------
# The head half (above) says "no bound". The body half is the opposite, and both are MEASURED in
# Chrome rather than read off the standard: a <meta charset> in the body is honoured at byte 0, 100
# and 512 and IGNORED from 1024 on, because once real content is parsed the browser will not
# re-decode. Getting this half wrong is invisible to the head cases — an unbounded scan passes every
# one of them while honouring body declarations no browser honours.
#
# Frostwork and w3lib AGREE on the ignored cases, for different reasons (w3lib's regex gives up at
# `body`; Frostwork bounds the re-decode at the head). Asserted directly rather than as a w3lib
# difference, since the agreement is a coincidence of two different rules and w3lib is not the target.
def _body_meta_page(pad):
    doc = b"<!DOCTYPE html><html><head><title>t</title></head><body>"
    while len(doc) < pad:
        doc += b"<p>lorem ipsum dolor sit amet consectetur</p>"
    return doc + b'<meta charset="windows-1252">' + W1252 + b"</body></html>"


for _pad, _want, _why in [(0, ["café"], "inside the 1024-byte floor"),
                          (100, ["café"], "inside the floor"),
                          (512, ["café"], "inside the floor"),
                          (1024, ["caf�"], "past the floor, in the body"),
                          (4096, ["caf�"], "past the floor, in the body"),
                          (64 * 1024, ["caf�"], "past the floor, in the body")]:
    _mine = engine(_body_meta_page(_pad), ["p.c::text"], None)[0]
    _ok = _mine == _want
    if not _ok:
        fails.append(("body-depth", f"a BODY declaration at ~{_pad} bytes ({_why})", _mine, _want))
    print(f"body declaration at ~{_pad} bytes ({_why}): mine={_mine} -> {'OK' if _ok else 'MISMATCH'}")

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

# The multi-byte counterpart: every two-byte sequence of every legacy label, decoded both ways. The
# enumeration, the four disagreement classes and the expectation tables live in `tools/decoder_sweep.py`
# — importable, so tests/test_gates.py can exercise them directly; a sweep that quietly narrows or a
# classifier that files a real character into a bulk count both read exactly like a clean run, and
# neither could be tested while they lived in this un-importable script. What is left here is the
# plumbing that asks the two decoders.
import decoder_sweep  # noqa: E402

for _label in decoder_sweep.LABELS:
    _probe = decoder_sweep.candidates()
    _diff, _counts = {}, dict.fromkeys(decoder_sweep.BULK, 0)
    for _i in range(0, len(_probe), 4000):
        _part = _probe[_i:_i + 4000]
        _ps = b"".join(b'<p class="c">' + _s + b"</p>" for _s in _part)
        _doc = (b'<html><head><meta charset="' + _label.encode() + b'"></head><body>'
                + _ps + b"</body></html>")
        _mine = engine(_doc, ["p.c::text"], None)[0]
        _, _txt = html_to_unicode(None, _doc, auto_detect_fun=None, default_encoding="utf8")
        _theirs = PS(text=_txt).css("p.c::text").getall()
        for _s, _m, _t in zip(_part, _mine, _theirs):
            _kind = decoder_sweep.classify(_m, _t)
            if _kind == "real":
                _diff[_s] = (_m, _t)
            elif _kind != "agree":
                _counts[_kind] += 1
    _bad = decoder_sweep.verify(_label, _diff, _counts)
    fails += _bad
    _want = decoder_sweep.expected_counts(_label)
    print(f"decoder [{_label} index]: all {len(_probe)} two-byte sequences: {len(_diff)} disagree on a "
          f"real character (expected {len(decoder_sweep.INDEX_DIVERGENCE[_label])}), "
          f"{_counts['whatwg_only']} are WHATWG-assigned and Python-unassigned (expected "
          f"{_want['whatwg_only']}), {_counts['pua']} Parsel-PUA (expected {_want['pua']}), "
          f"{_counts['replacement_shape']} differ only in replacement shape (expected "
          f"{_want['replacement_shape']}) -> {'OK' if not _bad else 'MISMATCH'}")

# THE GATE: any mismatch above is an encoding regression. Without this the target printed MISMATCH and
# still exited 0, so `make gate` and hosted CI stayed green through an encoding bug.
print(f"\nENCODING GATE: mismatches = {len(fails)}  ->  {'PASS' if not fails else 'FAIL'}")
for label, s_, m, t in fails:
    print(f"  MISMATCH [{label}] {s_}: mine={m} lxml={t}")
if fails:
    raise SystemExit(1)
