"""The encoding gate's inputs, importable.

`tools/enc_check.py` grades these documents against Parsel, w3lib and recorded browser answers, and
`tests/test_encoding_inputs.py` runs every one of them through each Python entry point. `enc_check` runs
its gate at module level, so nothing defined there can be imported; the tables live here instead.

Each table row says what Frostwork must answer. The reasons are the contract text in
docs/COMPATIBILITY.md; keep the two in step.
"""
from __future__ import annotations

import decoder_sweep

U8 = b'<p class="c">caf\xc3\xa9</p>'
W1252 = b'<p class="c">caf\xe9</p>'
XUD = b'<p class="c">caf\xe9</p>'          # the same bytes read as x-user-defined

# (label, sample non-ASCII text). Labels understood by both Python codecs and encoding_rs/WHATWG.
LABEL_CASES = [
    ("windows-1252", "café — €5 «prix» ™"),
    ("shift_jis", "日本語のテスト"),
    ("euc-jp", "日本語エンコード"),
    ("gbk", "中文测试内容"),
    ("big5", "繁體中文測試"),
    ("koi8-r", "Привет мир"),
    ("utf-8", "café 日本語 €"),
]
SELS = ["p::text", "a::attr(title)", "div::attr(data-v)", "p.c::text", "span ::text"]


def label_page(label: str, sample: str) -> str:
    """A page declaring `label`, carrying `sample` in text and in attribute values."""
    return (f'<html><head><meta charset="{label}"></head><body>'
            f'<p class="c">{sample}</p><a title="{sample}" href="/x">link</a>'
            f'<div data-v="{sample}">d</div><span>a{sample}b</span></body></html>')


# comment decoy (T7): `charset=` in a comment/visible text must NOT switch the decode; no label -> the
# body is real UTF-8 and must stay UTF-8, matching w3lib/Scrapy (which strip comments before sniffing).
DECOY = ('<!-- saved from url=(0037)https://x/ charset=big5 -->'
         '<html><head></head><body><p class="c">café 日本語</p></body></html>').encode("utf-8")
UTF16_BOM = '<html><body><p class="c">café 日本語</p></body></html>'.encode("utf-16")  # adds BOM

SHARED_PRESCAN_CASES = [
    ("data-http-equiv is not http-equiv", b'<meta data-http-equiv="content-type" content="text/html; charset=big5">', U8),
    ("http-equiv=refresh declares nothing", b'<meta http-equiv="refresh" content="0; url=/x?charset=big5">', U8),
    ("charset= in an unrelated attribute", b'<meta http-equiv="content-type" content="text/html" data-note="charset=big5">', U8),
    ("quoted > does not end the tag", b'<meta http-equiv="content-type" content="text/html; charset=windows-1252" title="a>b">', W1252),
    ("whitespace around =", b"<meta charset\n=\nwindows-1252>", W1252),
    ("charset inside a comment declares nothing", b"<!-- <meta charset=big5> -->", U8),
    ("<!--!> does not close a comment", b"<!--!><meta charset=big5>-->", U8),
    ("<!---!> does not close a comment", b"<!---!><meta charset=big5>-->", U8),
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


def prescan_doc(head: bytes, body: bytes) -> bytes:
    """A `SHARED_PRESCAN_CASES` row as a whole document."""
    return b"<html><head>" + head + b"</head><body>" + body + b"</body></html>"


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

# ---- DELIBERATE differences from w3lib: browser/WHATWG behaviour, asserted both ways --------------
# Each row is (name, document, what Frostwork must produce, what w3lib produces, why we differ). The
# "what w3lib produces" column is checked too: if w3lib ever starts agreeing, the row is STALE and the
# gate fails.
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
    ("<!--> is a whole comment",
     b"<html><head><!--><meta charset=windows-1252></head><body>" + W1252 + b"</body></html>",
     ["café"], ["caf�"],
     "WHATWG's prescan ends a comment at the first `>` preceded by `--` and after the `<`, and the "
     "`--` of `<!--` counts, so the <meta> after `<!-->` declares, as it does in Chromium 153 and Firefox "
     "144; w3lib looks for a `-->` after the `<!--` and so treats the rest of the document as a comment"),
    ("--!> ends a comment",
     b"<html><head><!--x--!><meta charset=windows-1252></head><body>" + W1252 + b"</body></html>",
     ["café"], ["caf�"],
     "the HTML tokenizer ends a comment at `--!>`, and the <meta> after it declares in Chromium 153 and "
     "Firefox 144, served as text/html with no charset; WHATWG's prescan algorithm and w3lib only end a "
     "comment at `-->`"),
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
    ("an undeclared document is autodetected",
     b"<html><head></head><body>" + W1252 + b"</body></html>", ["café"], ["caf�"],
     "WHATWG lets a user agent autodetect after the prescan, and Firefox does it with chardetng, which is "
     "what Frostwork runs whenever the document is not valid UTF-8. w3lib only autodetects when given an "
     "auto_detect_fun; Scrapy passes one, and its guess agrees here"),
    ("a UTF-32 BOM is not a BOM",
     _U32LE, ["café"], ["café"],
     "the WHATWG Encoding Standard has no UTF-32, so the leading FF FE IS the UTF-16LE BOM and the "
     "document is read as UTF-16LE — which, with NUL deletion, happens to agree with w3lib's UTF-32 "
     "decode for BMP text; w3lib recognizes UTF-32 explicitly"),
]

# ---- DELIBERATE differences from the BROWSER: the encoding rule's named exceptions ---------------
# Each row is (name, document, Frostwork's encoding, Frostwork's values, the browser's encoding, the
# browser's values, the browser build that was measured, why). The browser column is a recorded
# browser measurement, so the gate checks it two ways instead of re-measuring it:
# decoding the document with the browser's encoding must give the browser's values (the record is
# self-consistent), and Frostwork must still differ from them (the exception is still real).
BROWSER_EXCEPTIONS = [
    ("unlabelled valid UTF-8 is read as UTF-8",
     b"<html><head></head><body>" + U8 + b"</body></html>",
     "UTF-8", ["café"], "windows-1252", ["cafÃ©"], "Chrome 156",
     "browsers refuse to detect UTF-8 on a text/html page from the network, by policy: Chrome 156 "
     "shows windows-1252 mojibake on the lab's `no-declaration` case, and chardetng documents "
     "`Utf8Detection::Deny` as what Firefox passes. The result is text nobody intended, and a page "
     "relying on it would look broken to its own users, so Frostwork reads UTF-8 (BROWSER_MODE.md, E2)"),
]

# ---- the PRECEDENCE table: which input wins when several are present -----------------------------
# Each row is (name, document, encoding=, content_type=, the encoding that must win, the rank that must
# decide it, the values, why). The ranks are BOM, BOM-less UTF-16 prefix, `encoding=`, `content_type=`,
# the document's declarations, then autodetection; a rank with nothing usable hands over to the next.
_META_KOI8 = b'<html><head><meta charset="koi8-r"></head><body>' + W1252 + b"</body></html>"
_META_1252 = b'<html><head><meta charset="windows-1252"></head><body>' + W1252 + b"</body></html>"
PRECEDENCE = [
    ("a BOM outranks encoding=",
     b"\xef\xbb\xbf<html><body>" + U8 + b"</body></html>", "windows-1252", None,
     "UTF-8", "bom", ["café"],
     "a BOM-marked document read as anything else is mojibake. web-poet and Scrapy rank a forced "
     "`_encoding` ahead of the BOM, so this is the one shape where they and the engine disagree"),
    ("encoding= outranks content_type=",
     _META_KOI8, "windows-1252", "text/html; charset=koi8-r",
     "windows-1252", "encoding", ["café"],
     "`encoding=` is a label the user forced, so it beats what the server sent"),
    ("content_type= outranks <meta>",
     _META_KOI8, None, "text/html; charset=windows-1252",
     "windows-1252", "content-type", ["café"],
     "an HTTP charset wins over the document's own declaration, in every browser"),
    ("an unusable content_type= charset is ignored",
     _META_1252, None, "text/html; charset=no-such-charset",
     "windows-1252", "meta", ["café"],
     "the header is server data, so a label nothing knows is skipped rather than raised"),
    ("a content_type= with no charset declares nothing",
     _META_1252, None, "text/html",
     "windows-1252", "meta", ["café"],
     "only the charset parameter is read"),
    ("a quoted content_type= charset",
     _META_KOI8, None, 'text/html; Charset = "windows-1252"',
     "windows-1252", "content-type", ["café"],
     "the parameter name is case-insensitive and the value may be quoted"),
    ("a real encoding WHATWG lacks is ignored at encoding=",
     _META_KOI8, "utf-7", "text/html; charset=windows-1252",
     "windows-1252", "content-type", ["café"],
     "WHATWG has no UTF-7, so the label is 'failure, continue', as it is in a browser"),
    ("x-user-defined via content_type= is taken literally",
     b"<html><body>" + XUD + b"</body></html>", None, "text/html; charset=x-user-defined",
     "x-user-defined", "content-type", ["caf"],
     "the `<meta>` rewrite to windows-1252 applies to a document declaration only; from HTTP the label "
     "maps every high byte into the private use area, as measured on the lab's `http-x-user-defined` "
     "case. A w3lib detour drops the label, because Python has no codec for it"),
    ("a replacement label via content_type= decodes to one U+FFFD",
     b"<html><body>" + U8 + b"</body></html>", None, "text/html; charset=iso-2022-kr",
     "replacement", "content-type", [],
     "WHATWG maps iso-2022-kr to the replacement decoder, which a browser uses to refuse the document; "
     "Python has an iso2022_kr codec, so a w3lib detour would decode it"),
]


def body_meta_page(pad: int) -> bytes:
    """A `<meta charset=koi8-r>` past `pad` bytes of BODY, over windows-1252 bytes. The declaration names
    koi8-r so that an ignored one is visible: the undeclared page is then autodetected as windows-1252,
    as a browser autodetects it, and `caf\\xe9` reads `café`, not `cafИ`."""
    doc = b"<!DOCTYPE html><html><head><title>t</title></head><body>"
    while len(doc) < pad:
        doc += b"<p>lorem ipsum dolor sit amet consectetur</p>"
    return doc + b'<meta charset="koi8-r">' + W1252 + b"</body></html>"


# (pad, Frostwork's values, why): honoured within the 1024-byte floor, ignored past it.
BODY_META_CASES = [
    (0, ["cafИ"], "inside the 1024-byte floor"),
    (100, ["cafИ"], "inside the floor"),
    (512, ["cafИ"], "inside the floor"),
    (1024, ["café"], "past the floor, in the body"),
    (4096, ["café"], "past the floor, in the body"),
    (64 * 1024, ["café"], "past the floor, in the body"),
]

# byte -> the character the WHATWG windows-1252 index defines (Python's cp1252: undefined -> U+FFFD)
WHATWG_ONLY_BYTES = {
    0x81: "\u0081", 0x8D: "\u008d", 0x8F: "\u008f", 0x90: "\u0090", 0x9D: "\u009d",
}
SKIP_BYTES = set(b"<>&\r\n\t \x00")  # HTML delimiters / whitespace: not a decoder question
C1_LABELS = ("windows-1252", "iso-8859-1")  # WHATWG maps the iso-8859-1 LABEL to windows-1252


def c1_probe(label: str) -> tuple[list[bytes], bytes]:
    """`(probe bytes, document)`: every single byte from 0x20 up, each in its own `<p class="c">`."""
    probe = [bytes([b]) for b in range(0x20, 0x100) if b not in SKIP_BYTES]
    ps = b"".join(b'<p class="c">' + s + b"</p>" for s in probe)
    return probe, b'<html><head><meta charset="' + label.encode() + b'"></head><body>' + ps + b"</body></html>"


UTF16_META_LABELS = (b"utf-16", b"utf-16le", b"utf-16be", b"UTF-16")


def utf16_meta_doc(label: bytes) -> bytes:
    """A UTF-8 document whose `<meta>` declares a UTF-16 label, which WHATWG reads as UTF-8."""
    return (b'<html><head><meta charset="' + label + b'"></head><body><p class="c">'
            + b"caf\xc3\xa9" + b"</p></body></html>")


UTF16_BOM_DOC = '<html><head></head><body><p class="c">café</p></body></html>'.encode("utf-16")

SWEEP_CHUNK = 4000


def sweep_docs(label: str) -> list[tuple[list[bytes], bytes]]:
    """`[(sequences, document)]`: the decoder sweep's two-byte sequences for `label`, in chunks."""
    probe = decoder_sweep.candidates()
    out = []
    for i in range(0, len(probe), SWEEP_CHUNK):
        part = probe[i:i + SWEEP_CHUNK]
        ps = b"".join(b'<p class="c">' + s + b"</p>" for s in part)
        out.append((part, b'<html><head><meta charset="' + label.encode() + b'"></head><body>'
                          + ps + b"</body></html>"))
    return out


def every_input() -> list[tuple[str, bytes, str | None, str | None]]:
    """`(name, document, encoding=, content_type=)` for every document the encoding gate grades, with
    the label the gate passes for it."""
    out: list[tuple[str, bytes, str | None, str | None]] = []
    for label, sample in LABEL_CASES:
        try:
            body = label_page(label, sample).encode(label)
        except (LookupError, UnicodeError):
            continue  # enc_check reports an unencodable label as untested
        out.append((f"label page {label}", body, label, None))
    out.append(("comment decoy", DECOY, None, None))
    out.append(("UTF-16 BOM", UTF16_BOM, None, None))
    out += [(f"shared prescan: {n}", prescan_doc(head, body), None, None)
            for n, head, body in SHARED_PRESCAN_CASES]
    out += [(f"shared document: {n}", doc, None, None) for n, doc in SHARED_DOC_CASES]
    out += [(f"browser difference: {n}", doc, None, None) for n, doc, *_ in BROWSER_DIFFERENCES]
    out += [(f"browser exception: {n}", doc, None, None) for n, doc, *_ in BROWSER_EXCEPTIONS]
    out += [(f"precedence: {n}", doc, enc, ctype) for n, doc, enc, ctype, *_ in PRECEDENCE]
    out += [(f"body declaration at {pad}", body_meta_page(pad), None, None)
            for pad, _want, _why in BODY_META_CASES]
    out += [(f"C1 probe {label}", c1_probe(label)[1], None, None) for label in C1_LABELS]
    out += [(f"meta {label.decode()}", utf16_meta_doc(label), None, None) for label in UTF16_META_LABELS]
    out.append(("UTF-16 BOM document", UTF16_BOM_DOC, None, None))
    for label in decoder_sweep.LABELS:
        out += [(f"decoder sweep {label} #{i}", doc, None, None) for i, (_part, doc) in enumerate(sweep_docs(label))]
    return out
