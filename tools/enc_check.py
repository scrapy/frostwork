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

# THE GATE: any mismatch above is an encoding regression. Without this the target printed MISMATCH and
# still exited 0, so `make gate` and hosted CI stayed green through an encoding bug.
print(f"\nENCODING GATE: mismatches = {len(fails)}  ->  {'PASS' if not fails else 'FAIL'}")
for label, s_, m, t in fails:
    print(f"  MISMATCH [{label}] {s_}: mine={m} lxml={t}")
if fails:
    raise SystemExit(1)
