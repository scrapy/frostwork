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

# ---- meta prescan, oracled against W3LIB (not Parsel) -------------------------------------------
# Parsel is the oracle for VALUES, but not for SNIFFING: `PS(body=…)` with no encoding never looks at
# `<meta>` at all, it just defaults to UTF-8. Oracling the prescan against it was vacuous — it "agreed"
# on every declaration we MISSED, because both produced mojibake. Scrapy resolves a response encoding
# with w3lib.encoding.html_to_unicode, so that is the oracle a prescan has to match.
from w3lib.encoding import html_to_unicode  # noqa: E402

U8 = b'<p class="c">caf\xc3\xa9</p>'
W1252 = b'<p class="c">caf\xe9</p>'
PRESCAN_CASES = [
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
]
for label, head, body in PRESCAN_CASES:
    doc = b"<html><head>" + head + b"</head><body>" + body + b"</body></html>"
    _, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
    oracle = PS(text=txt).css("p.c::text").getall()
    mine = engine(doc, ["p.c::text"], None)
    if mine[0] != oracle:
        fails.append(("meta-prescan", label, mine[0], oracle))
    print(f"meta prescan [{label}]: mine={mine[0]} w3lib={oracle} "
          f"-> {'OK' if mine[0] == oracle else 'MISMATCH'}")

# The ONE deliberate divergence from w3lib: it has no comment handling, so it honours a charset inside a
# comment. WHATWG's prescan and every browser skip comments, and the project's policy is to implement the
# correct behaviour and document the difference rather than reproduce an oracle bug.
doc = b'<html><head><!-- <meta charset=big5> --></head><body>' + U8 + b"</body></html>"
_, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
w3 = PS(text=txt).css("p.c::text").getall()
mine = engine(doc, ["p.c::text"], None)
if mine[0] != ["café"]:
    fails.append(("meta-prescan", "commented charset must be ignored", mine[0], ["café"]))
print(f"meta prescan [commented charset: DOCUMENTED divergence]: mine={mine[0]} w3lib={w3} "
      f"-> {'OK' if mine[0] == ['café'] else 'MISMATCH'}")

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
# Measured for the other legacy labels: on VALIDLY ENCODED text (800 assigned characters each)
# shift_jis / euc-jp / euc-kr / gb18030 / windows-1252 are at full parity. The two decoders differ far
# more widely over UNASSIGNED byte sequences (8-22% of all two-byte combinations), which is inherent —
# a WHATWG index is total, Python's codecs raise — and is not text any page contains.
BIG5_INDEX_DIVERGENCE = {
    b"\xa1\x45": ("\u2027", "\u2022"), b"\xa1\x4e": ("\ufe51", "\uff64"),
    b"\xa1\xc2": ("\u00af", "\u203e"), b"\xa1\xe3": ("\uff5e", "\u223c"),
    b"\xa1\xf2": ("\u2295", "\u2641"), b"\xa1\xf3": ("\u2299", "\u2609"),
    b"\xa2\x41": ("\u2215", "\uff0f"), b"\xa2\x42": ("\ufe68", "\uff3c"),
    b"\xa2\x44": ("\uffe5", "\u00a5"), b"\xa2\x46": ("\uffe0", "\u00a2"),
    b"\xa2\x47": ("\uffe1", "\u00a3"),
}
big5_assigned = []
for _l in range(0x81, 0xFF):
    for _t in range(0x40, 0xFF):
        _b = bytes([_l, _t])
        if _l in b"<>&" or _t in b"<>&\r\n\t \x00":
            continue
        try:
            _b.decode("big5hkscs")
        except Exception:
            continue
        big5_assigned.append(_b)
big5_diff = {}
for _i in range(0, len(big5_assigned), 4000):
    _part = big5_assigned[_i:_i + 4000]
    _ps = b"".join(b'<p class="c">' + _s + b"</p>" for _s in _part)
    _doc = b'<html><head><meta charset="big5"></head><body>' + _ps + b"</body></html>"
    _mine = engine(_doc, ["p.c::text"], None)[0]
    _, _txt = html_to_unicode(None, _doc, auto_detect_fun=None, default_encoding="utf8")
    _theirs = PS(text=_txt).css("p.c::text").getall()
    for _s, _m, _t in zip(_part, _mine, _theirs):
        if _m != _t:
            big5_diff[_s] = (_m, _t)
for _s in sorted(set(big5_diff) - set(BIG5_INDEX_DIVERGENCE)):
    fails.append(("decoder", f"big5 {_s.hex()} diverges and is not a listed index difference",
                  [big5_diff[_s][0]], [big5_diff[_s][1]]))
for _s in sorted(set(BIG5_INDEX_DIVERGENCE) - set(big5_diff)):
    fails.append(("decoder", f"big5 {_s.hex()} no longer diverges — drop it from "
                             f"BIG5_INDEX_DIVERGENCE", "?", "?"))
for _s, (_ours, _theirs2) in BIG5_INDEX_DIVERGENCE.items():
    if _s in big5_diff and big5_diff[_s] != (_ours, _theirs2):
        fails.append(("decoder", f"big5 {_s.hex()} maps differently now", [big5_diff[_s]],
                      [(_ours, _theirs2)]))
print(f"decoder [big5 index]: {len(big5_diff)} of {len(big5_assigned)} ASSIGNED sequences differ from "
      f"Parsel; expected exactly {len(BIG5_INDEX_DIVERGENCE)} "
      f"-> {'OK' if len(big5_diff) == len(BIG5_INDEX_DIVERGENCE) and not (set(big5_diff) ^ set(BIG5_INDEX_DIVERGENCE)) else 'MISMATCH'}")

# THE GATE: any mismatch above is an encoding regression. Without this the target printed MISMATCH and
# still exited 0, so `make gate` and hosted CI stayed green through an encoding bug.
print(f"\nENCODING GATE: mismatches = {len(fails)}  ->  {'PASS' if not fails else 'FAIL'}")
for label, s_, m, t in fails:
    print(f"  MISMATCH [{label}] {s_}: mine={m} lxml={t}")
if fails:
    raise SystemExit(1)
