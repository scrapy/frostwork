"""Encoding parity: pages in various charsets (non-ASCII text + attr values) vs Parsel given the
same label. Validates the engine decodes emitted values per the resolved encoding."""
import subprocess, json, os, sys
from parsel import Selector as PS
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import oracle  # parity is defined against libxml2 >= 2.15 here too (meta sniffing moved between them)
oracle.require()
BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "target", "release", "differ")
# Encoding resolution is the same in both parse modes; the values are graded in lxml mode, Parsel's.
ENGINE = [BIN, "--parser", "lxml"]

from enc_cases import (  # noqa: E402
    BODY_META_CASES, BROWSER_DIFFERENCES, C1_LABELS, DECOY, LABEL_CASES, PRECEDENCE,
    SELS, SHARED_DOC_CASES, SHARED_PRESCAN_CASES, UTF16_BOM, UTF16_BOM_DOC, UTF16_META_LABELS,
    UTF8_DETECTION, WHATWG_ONLY_BYTES, body_meta_page, c1_probe, label_page, prescan_doc, sweep_docs,
    utf16_meta_doc, utf8_detection_failures,
)


def engine(body, sels, label):
    line = (label or "") + "\t" + body.hex() + "\t" + "\t".join(sels) + "\n"
    p = subprocess.Popen(ENGINE, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(line.encode())
    return json.loads(out.decode().splitlines()[0])


def engine_inputs(body, sels, encoding, content_type, url=None):
    """`{"encoding", "source", "values"}` for a document given the caller's inputs (differ's `T` line)."""
    fields = [encoding, content_type, url]
    line = "\t".join(["T", *(f or "" for f in fields), body.hex(), *sels]) + "\n"
    p = subprocess.Popen(ENGINE, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out, _ = p.communicate(line.encode())
    return json.loads(out.decode().splitlines()[0])


tot = ok = 0
fails = []
for label, sample in LABEL_CASES:
    html = label_page(label, sample)
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
mine = engine(DECOY, ["p.c::text"], None)
oracle = PS(body=DECOY, encoding="utf-8").css("p.c::text").getall()  # w3lib ignores the comment -> utf-8
if mine[0] != oracle:
    fails.append(("sniffed", "comment charset= decoy", mine[0], oracle))
print(f"comment charset= decoy (sniffed): mine={mine[0]} utf-8-oracle={oracle} "
      f"-> {'OK' if mine[0] == oracle else 'MISMATCH'}")

# UTF-16 via BOM (encoding sniffed from BOM, label None)
mine = engine(UTF16_BOM, ["p::text"], None)
# oracle = decode-first (what Scrapy does); lxml's body+encoding path can't parse UTF-16 bytes
oracle = PS(text=UTF16_BOM.decode("utf-16")).css("p::text").getall()
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

for label, head, body in SHARED_PRESCAN_CASES:
    doc = prescan_doc(head, body)
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
# Each row of `enc_cases.BROWSER_DIFFERENCES` is (name, document, what Frostwork must produce, what w3lib
# produces, why we differ). The "what w3lib produces" column is checked too: if w3lib ever starts
# agreeing, the row is STALE and this fails, which is the same anti-rot discipline as WHATWG_ONLY_BYTES /
# BIG5_INDEX_DIVERGENCE below.
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
for _pad, _want, _why in BODY_META_CASES:
    _mine = engine(body_meta_page(_pad), ["p.c::text"], None)[0]
    _ok = _mine == _want
    if not _ok:
        fails.append(("body-depth", f"a BODY declaration at ~{_pad} bytes ({_why})", _mine, _want))
    print(f"body declaration at ~{_pad} bytes ({_why}): mine={_mine} -> {'OK' if _ok else 'MISMATCH'}")

# ---- autodetection of valid UTF-8: the measured browser answers by kind of URL ------------------
# A browser record is a measurement, so each row is checked two ways rather than re-measured: decoding
# the document with the browser's encoding must reproduce the browser's values, and Frostwork must
# return them (`enc_cases.utf8_detection_failures`).
for row in UTF8_DETECTION:
    name, doc, url, *_rest, browsers, why = row
    got = engine_inputs(doc, ["p.c::text"], None, None, url)
    as_browser = [PS(text=doc.decode(enc)).css("p.c::text").getall() for enc, *_ in browsers]
    row_fails = utf8_detection_failures(row, got, as_browser)
    fails += [("utf8-detection", msg, got, None) for msg in row_fails]
    print(f"utf8 detection [{name}]: {got['encoding']} from {got['source']}, mine={got['values'][0]} "
          f"-> {'OK' if not row_fails else 'MISMATCH'}")
    print(f"    why: {why}")

# ---- the PRECEDENCE table: which caller input wins -----------------------------------------------
for name, doc, enc, ctype, want_enc, want_src, want_vals, why in PRECEDENCE:
    got = engine_inputs(doc, ["p.c::text"], enc, ctype)
    want = {"encoding": want_enc, "source": want_src, "values": [want_vals]}
    if got != want:
        fails.append(("precedence", name, got, want))
    print(f"precedence [{name}]: {got['encoding']} from {got['source']}, mine={got['values'][0]} "
          f"-> {'OK' if got == want else 'MISMATCH'}")
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
for label in C1_LABELS:  # WHATWG maps the iso-8859-1 LABEL to windows-1252
    probe, doc = c1_probe(label)
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
for _lab in UTF16_META_LABELS:
    _mine = engine(utf16_meta_doc(_lab), ["p.c::text"], None)[0]
    if _mine != ["café"]:
        fails.append(("meta-prescan", f"<meta charset={_lab.decode()}> must be read as UTF-8",
                      _mine, ["café"]))
# ...and a genuine UTF-16 document (BOM) must still decode as UTF-16
_m16 = engine(UTF16_BOM_DOC, ["p.c::text"], None)[0]
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
    for _part, _doc in sweep_docs(_label):
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
