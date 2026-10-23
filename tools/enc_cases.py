# The documents tools/enc_check.py grades, importable so that tests/test_encoding.py can run every one
# of them through `frostwork.encoding` too.

U8 = b'<p class="c">caf\xc3\xa9</p>'
W1252 = b'<p class="c">caf\xe9</p>'
XUD = b'<p class="c">caf\xe9</p>'          # the same bytes read as x-user-defined
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

# Each row is (name, document, what Frostwork must produce, what w3lib produces, why we differ). The
# "what w3lib produces" column is checked too: if w3lib ever starts agreeing, the row is STALE and this
# fails, which is the same anti-rot discipline as WHATWG_ONLY_BYTES / BIG5_INDEX_DIVERGENCE in
# tools/enc_check.py. The reasons are the contract text in docs/COMPATIBILITY.md; keep the two in step.
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
     "bound to match. The BODY half of the rule is the opposite and is gated separately in "
     "tools/enc_check.py"),
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


def prescan_document(head, body):
    return b"<html><head>" + head + b"</head><body>" + body + b"</body></html>"


def documents():
    """Every document above, as ``(name, document)``."""
    for name, head, body in SHARED_PRESCAN_CASES:
        yield name, prescan_document(head, body)
    yield from SHARED_DOC_CASES
    for name, doc, *_ in BROWSER_DIFFERENCES:
        yield name, doc
