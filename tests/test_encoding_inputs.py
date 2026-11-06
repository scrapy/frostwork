"""One encoding decision behind every Python entry point.

Every document `tools/enc_check.py` grades (`tools/enc_cases.every_input()`) goes through `extract`,
`extract_grouped`, `detect_encoding`, `Plan.extract*`, `Page.extract`, `Page.extract_response` and a real
web-poet `FrostPage`, and each must give the same answer, a raised error included. `detect_encoding` makes
the same decision, except that it ignores a label no codec knows where the extraction functions refuse it. The precedence table and
the integrator contract they implement are in docs/PYTHON.md ("Encoding inputs").
"""

import asyncio
import os
import sys

import pytest

import frostwork
from frostwork._frostwork import Plan

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import enc_cases  # noqa: E402

web_poet = pytest.importorskip("web_poet")
from web_poet import HttpResponse  # noqa: E402

from frostwork.webpoet import FrostPage, field  # noqa: E402

URL = "http://example.com/"
SELS = enc_cases.SELS
INPUTS = enc_cases.every_input()
SOURCES = {"bom", "utf16-prefix", "encoding", "content-type", "xml-declaration", "meta", "utf8", "autodetect"}

# Applied to every document on top of the label the gate gives it, so the raising and the ignored paths
# of ranks 3 and 4 reach every entry point too.
VARIANTS = [
    (None, None),
    ("not-a-charset", None),                       # rank 3, no codec knows it: raises
    ("utf-7", None),                               # rank 3, a real encoding WHATWG lacks: ignored
    (None, "text/html; charset=no-such-charset"),  # rank 4, unusable: ignored, never raises
]


class EncPage(FrostPage):
    f0 = field(SELS[0], all=True)
    f1 = field(SELS[1], all=True)
    f2 = field(SELS[2], all=True)
    f3 = field(SELS[3], all=True)
    f4 = field(SELS[4], all=True)


PAGE = frostwork.Page()
for _i, _sel in enumerate(SELS):
    PAGE.field_all(f"f{_i}", _sel)
PLAN = Plan(SELS, [])


def _response(body, encoding, content_type):
    headers = {"Content-Type": content_type} if content_type is not None else {}
    return HttpResponse(url=URL, body=body, headers=headers, encoding=encoding)


def _answer(fn):
    """`("ok", value)` or `("raise", type name, message)`, so an error compares like a value."""
    try:
        return ("ok", fn())
    except Exception as exc:  # noqa: BLE001 - a raise is an answer here
        return ("raise", type(exc).__name__, str(exc))


def _columns(item) -> list:
    return [list(item[f"f{i}"]) for i in range(len(SELS))]


def _every_entry_point(body, encoding, content_type) -> dict:
    kw = {"content_type": content_type, "url": URL}
    response = _response(body, encoding, content_type)
    return {
        "extract": _answer(lambda: frostwork.extract(body, SELS, encoding, **kw)),
        "extract_grouped": _answer(lambda: frostwork.extract_grouped(body, SELS, [], encoding, **kw)[0]),
        "Plan.extract": _answer(lambda: PLAN.extract(body, encoding, **kw)),
        "Plan.extract_grouped": _answer(lambda: PLAN.extract_grouped(body, encoding, **kw)[0]),
        "Page.extract": _answer(lambda: _columns(PAGE.extract(body, encoding, **kw).to_dict())),
        "Page.extract_response": _answer(lambda: _columns(PAGE.extract_response(response).to_dict())),
        "FrostPage": _answer(lambda: _columns(asyncio.run(EncPage(response=response).to_item()))),
    }


def _cases():
    for name, body, encoding, content_type in INPUTS:
        for variant in [(encoding, content_type), *VARIANTS]:
            yield name, body, *variant


@pytest.mark.parametrize("name,body,encoding,content_type", list(_cases()),
                         ids=lambda v: v if isinstance(v, str) and len(v) < 80 else None)
def test_every_entry_point_makes_the_same_decision(name, body, encoding, content_type):
    answers = _every_entry_point(body, encoding, content_type)
    want = answers["extract"]
    wrong = {entry: got for entry, got in answers.items() if got != want}
    assert not wrong, f"{name}: entry points disagree with extract's {want!r}: {wrong!r}"

    detected = _answer(lambda: frostwork.detect_encoding(
        body, encoding, content_type=content_type, url=URL, explain=True))
    if want[0] == "raise" and "unknown encoding label" in want[2]:
        # detect_encoding ignores a label no codec knows, as a browser does, where extract refuses it
        unlabelled = _answer(lambda: frostwork.detect_encoding(
            body, None, content_type=content_type, url=URL, explain=True))
        assert detected == unlabelled, f"{name}: detect_encoding must ignore an unknown label"
        return
    if want[0] == "raise":
        assert detected == want, f"{name}: detect_encoding must raise exactly as extract does"
        return
    assert detected[0] == "ok", f"{name}: detect_encoding raised where extract did not: {detected!r}"
    enc, source = detected[1]
    assert source in SOURCES, source
    assert frostwork.detect_encoding(body, encoding, content_type=content_type, url=URL) == enc
    # the decision is the one the scan used: forcing it gives the same values
    if frostwork.resolve_label(enc) is not None:
        assert frostwork.extract(body, SELS, enc, url=URL) == want[1], f"{name}: {enc} from {source}"


@pytest.mark.parametrize("name,body,encoding,content_type", INPUTS,
                         ids=lambda v: v if isinstance(v, str) and len(v) < 80 else None)
def test_a_webpoet_response_gives_the_engines_header_only_answer(name, body, encoding, content_type):
    """With the label in the `Content-Type` header and nothing forced, a `FrostPage` and
    `Page.extract_response` over a real web-poet response answer what the engine answers from the header
    alone, not what web-poet's own `response.encoding` would make of the page."""
    header = content_type or (f"text/html; charset={encoding}" if encoding else None)
    want = frostwork.extract(body, SELS, content_type=header, url=URL)
    response = _response(body, None, header)
    assert _columns(asyncio.run(EncPage(response=response).to_item())) == want, name
    assert _columns(PAGE.extract_response(response).to_dict()) == want, name


class _NoDecision(HttpResponse):
    """A response whose own encoding decision must never be consulted."""

    @property
    def encoding(self):
        raise AssertionError("response.encoding was read")

    @property
    def text(self):
        raise AssertionError("response.text was read")


def test_the_integrations_never_read_the_frameworks_decision():
    body = b'<html><head><meta charset="koi8-r"></head><body><p>caf\xe9</p></body></html>'
    response = _NoDecision(url=URL, body=body, headers={"Content-Type": "text/html; charset=windows-1252"})
    assert asyncio.run(EncPage(response=response).to_item())["f0"] == ["café"]
    assert PAGE.extract_response(response).value("f0") == ["café"]


def test_webpoet_keeps_a_forced_encoding_in_its_private_field():
    """`FrostPage` and `Page.extract_response` read web-poet's private `_encoding`, the only place a forced
    label survives, because `.encoding` mixes it with web-poet's own sniffing. Pin that dependency."""
    body = b'<html><head><meta charset="koi8-r"></head><body><p>caf\xe9</p></body></html>'
    assert HttpResponse(url=URL, body=body, encoding="windows-1252")._encoding == "windows-1252"
    assert HttpResponse(url=URL, body=body)._encoding is None
    forced = HttpResponse(url=URL, body=body, encoding="windows-1252")
    assert asyncio.run(EncPage(response=forced).to_item())["f0"] == ["café"]
    assert PAGE.extract_response(forced).value("f0") == ["café"]
    assert PAGE.extract_response(HttpResponse(url=URL, body=body)).value("f0") == ["cafИ"]

    # web-poet ranks `_encoding` ahead of a BOM; the engine keeps the BOM first, as browsers do
    bom = b"\xef\xbb\xbf<p>caf\xc3\xa9</p>"
    assert HttpResponse(url=URL, body=bom, encoding="windows-1252").encoding == "windows-1252"
    response = HttpResponse(url=URL, body=bom, encoding="windows-1252")
    assert asyncio.run(EncPage(response=response).to_item())["f0"] == ["café"]


def test_scrapy_keeps_a_forced_encoding_in_its_private_field():
    """The same pin for Scrapy, whose headers return `bytes`."""
    scrapy_http = pytest.importorskip("scrapy.http")
    body = b'<html><head><meta charset="koi8-r"></head><body><p>caf\xe9</p></body></html>'
    assert scrapy_http.HtmlResponse(url=URL, body=body, encoding="windows-1252")._encoding == "windows-1252"
    assert scrapy_http.HtmlResponse(url=URL, body=body)._encoding is None
    forced = scrapy_http.HtmlResponse(url=URL, body=body, encoding="windows-1252")
    assert PAGE.extract_response(forced).value("f0") == ["café"]
    labelled = scrapy_http.HtmlResponse(
        url=URL, body=body, headers={"Content-Type": "text/html; charset=windows-1252"})
    assert PAGE.extract_response(labelled).value("f0") == ["café"]


def test_bytes_headers_are_read_without_importing_scrapy():
    """Scrapy's `Headers.get` returns `bytes`; the adapter decodes it rather than importing Scrapy."""

    class Headers:
        def get(self, key):
            return b"text/html; charset=windows-1252" if key.lower() == "content-type" else None

    class Response:
        body = b'<meta charset="koi8-r"><p>caf\xe9</p>'
        headers = Headers()
        url = "https://example.ru/p"

        @property
        def encoding(self):
            raise AssertionError("response.encoding was read")

    assert PAGE.extract_response(Response()).value("f0") == ["café"]


def test_a_str_ignores_content_type_and_refuses_a_conflicting_label():
    text = '<meta charset="koi8-r"><p>café</p>'
    header = "text/html; charset=windows-1252"
    assert frostwork.extract(text, ["p::text"], content_type=header) == [["café"]]
    assert frostwork.detect_encoding(text, content_type=header, explain=True) == ("UTF-8", "str")
    for entry in (lambda: frostwork.extract(text, ["p::text"], "windows-1252"),
                  lambda: PLAN.extract(text, "windows-1252"),
                  lambda: frostwork.detect_encoding(text, "windows-1252")):
        with pytest.raises(ValueError, match="already-decoded str"):
            entry()
    with pytest.raises(ValueError, match="unknown encoding label"):
        PLAN.extract(text, "not-a-charset")
