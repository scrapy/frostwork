import codecs
import io
import os
import subprocess
import sys

import pytest

import frostwork
from frostwork._frostwork import decode as native_decode
from frostwork.encoding import (
    _CODEC_NAMES,
    autodetect_encoding,
    bom_encoding,
    codec_name,
    declared_encoding,
    decode,
    detect_encoding,
    resolve_label,
)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import enc_cases  # noqa: E402

DOCUMENTS = [
    *enc_cases.documents(),
    ("UTF-16 with a BOM", '<html><body><p class="c">café</p></body></html>'.encode("utf-16")),
    ("UTF-8 with a BOM", b"\xef\xbb\xbf<html><body>" + enc_cases.U8 + b"</body></html>"),
    ("undeclared Shift_JIS", '<p class="c">日本語のページです。こんにちは、世界。</p>'.encode("shift_jis")),
    ("<meta charset=iso-2022-kr>", b"<meta charset=iso-2022-kr>" + enc_cases.W1252),
]


def _steps(body, label=None, url=None):
    """`detect_encoding`, composed from the steps the way w3lib would compose them."""
    bom = bom_encoding(body)
    if bom is not None:
        return bom[0]
    resolved = label and resolve_label(label)
    return resolved or declared_encoding(body) or autodetect_encoding(body, url=url)


@pytest.mark.parametrize("name,body", DOCUMENTS, ids=[name for name, _ in DOCUMENTS])
@pytest.mark.parametrize("label", [None, "windows-1251"])
def test_the_steps_decide_and_decode_like_extract(name, body, label):
    encoding = _steps(body, label)
    assert detect_encoding(body, label) == encoding
    bom = bom_encoding(body)
    bom_length = bom[1] if bom else 0
    text = decode(body[bom_length:], encoding)
    assert frostwork.extract(text, ["p.c::text"]) == frostwork.extract(body, ["p.c::text"], label)


def test_detect_encoding_resolves_python_codec_names():
    body = "<p>日本</p>".encode("cp932")
    assert detect_encoding(body, "cp932") == "Shift_JIS"
    assert detect_encoding(body, "cp437") == detect_encoding(body)
    assert detect_encoding(b"\xef\xbb\xbf" + body, "cp932") == "UTF-8"


def test_meta_utf_16_is_read_as_utf_8():
    body = b'<meta charset="utf-16"><p class="c">caf\xc3\xa9</p>'
    assert declared_encoding(body) == "UTF-8"
    assert decode(body, declared_encoding(body)).endswith("café</p>")


def test_euc_jp_decodes_like_a_browser():
    assert decode(b"\xa1\xc1 \xad\xa1", "euc_jp") == "～ ①"


@pytest.mark.parametrize(
    "label,body,text",
    [
        ("x-user-defined", b"caf\xe9 ok", "caf ok"),
        ("iso-2022-kr", b"caf\xe9 ok", "�"),
        ("hz-gb-2312", b"caf\xe9 ok", "�"),
        ("iso-2022-kr", b"", ""),
        ("gb18030", b"\x80", "€"),
        ("windows-1252", b"\x81", "\x81"),
        ("utf-8", b"\xed\xa0\x80 \xf0\x9f", "��� �"),
    ],
)
def test_decode(label, body, text):
    assert decode(body, label) == text


@pytest.mark.parametrize("label", ["utf-8", "windows-1252", "gbk"])
@pytest.mark.parametrize("buffer", [bytearray, memoryview])
def test_decode_accepts_any_buffer(label, buffer):
    assert decode(buffer(b"caf\xe9"), label) == decode(b"caf\xe9", label)


@pytest.mark.parametrize("buffer", [bytearray, memoryview])
def test_the_steps_accept_any_buffer(buffer):
    body = b"\xef\xbb\xbf<meta charset=gbk><p>caf\xe9"
    assert detect_encoding(buffer(body), "cp1252") == detect_encoding(body, "cp1252")
    assert bom_encoding(buffer(body)) == bom_encoding(body)
    assert declared_encoding(buffer(body)) == declared_encoding(body)
    assert autodetect_encoding(buffer(body), url="https://example.jp/") == autodetect_encoding(
        body, url="https://example.jp/"
    )


def test_decode_refuses_an_unknown_label():
    with pytest.raises(LookupError):
        decode(b"x", "cp437")


def test_every_whatwg_encoding_has_a_python_codec():
    assert len(_CODEC_NAMES) == 40  # https://encoding.spec.whatwg.org/#names-and-labels
    for whatwg, python in _CODEC_NAMES.items():
        assert resolve_label(whatwg) == whatwg
        assert codecs.lookup(python).name == python
        assert codec_name(whatwg) == python
        if whatwg != "ISO-8859-8-I":  # one index, two names
            assert resolve_label(python) == whatwg


def test_codec_name():
    assert codec_name("latin1") == "cp1252"
    assert codec_name("shift_jis") == "cp932"
    assert codec_name("cp437") is None


@pytest.mark.parametrize("errors", ["strict", "replace", "ignore", "xmlcharrefreplace"])
def test_x_user_defined_codec(errors):
    assert bytes(range(256)).decode("x-user-defined", errors) == decode(bytes(range(256)), "x-user-defined")
    assert "a".encode("x-user-defined", errors) == b"a\xe9"
    if errors == "strict":
        with pytest.raises(UnicodeEncodeError):
            "é".encode("x-user-defined", errors)
    else:
        want = {"replace": b"?", "ignore": b"", "xmlcharrefreplace": b"&#233;"}[errors]
        assert "é".encode("x-user-defined", errors) == want


def test_replacement_codec():
    assert b"".decode("replacement") == ""
    assert b"abc".decode("replacement", "replace") == "�"
    assert b"abc".decode("replacement", "ignore") == ""
    with pytest.raises(UnicodeDecodeError):
        b"abc".decode("replacement")
    assert "café".encode("replacement") == "café".encode()


def test_the_codecs_stream():
    body = bytes(range(256))
    text = decode(body, "x-user-defined")
    assert io.TextIOWrapper(io.BytesIO(body), encoding="x-user-defined", newline="").read() == text
    assert "".join(codecs.iterdecode([body[:100], body[100:]], "x-user-defined")) == text
    assert b"".join(codecs.iterencode([text[:100], text[100:]], "x-user-defined")) == body
    stream = io.BytesIO()
    codecs.getwriter("x-user-defined")(stream).write(text)
    assert stream.getvalue() == body

    chunks = [b"", b"a", b"bc"]
    assert "".join(codecs.iterdecode(chunks, "replacement", "replace")) == "\ufffd"
    assert io.TextIOWrapper(io.BytesIO(b"abc"), encoding="replacement", errors="replace").read() == "\ufffd"
    assert codecs.getreader("replacement")(io.BytesIO(b"abc"), "replace").read() == "\ufffd"
    assert b"".join(codecs.iterencode(["caf", "é"], "replacement")) == "café".encode()


def test_bom_encoding():
    assert bom_encoding(b"\xef\xbb\xbfx") == ("UTF-8", 3)
    assert bom_encoding(b"\xfe\xffx") == ("UTF-16BE", 2)
    assert bom_encoding(b"\xff\xfe\x00\x00") == ("UTF-16LE", 2)
    assert bom_encoding(b"\x00<\x00?") == ("UTF-16BE", 0)
    assert bom_encoding(b"<p>") is None


def test_autodetect_encoding_reads_the_tld():
    assert autodetect_encoding(b"<p>\x82\xa0</p>") == "IBM866"
    assert autodetect_encoding(b"<p>\x82\xa0</p>", url="https://example.jp/") == "Shift_JIS"
    assert autodetect_encoding(b"<p>\xc3") == "UTF-8"


def test_importing_the_encoding_api_skips_the_page_object_layer():
    code = "import sys, frostwork.encoding; print('frostwork.page' in sys.modules)"
    assert subprocess.check_output([sys.executable, "-c", code], text=True).strip() == "False"


def test_the_page_object_layer_registers_no_codecs():
    code = "import sys, frostwork; frostwork.extract(b'', []); print('frostwork.encoding' in sys.modules)"
    assert subprocess.check_output([sys.executable, "-c", code], text=True).strip() == "False"


@pytest.mark.parametrize(
    "name,labels",
    [
        ("x-user-defined", ["x-user-defined", "X_User_Defined", "x user defined"]),
        ("replacement", ["replacement", "Replacement!", "_replacement"]),
    ],
)
def test_resolves_without_its_codec(name, labels):
    code = (
        "import sys, frostwork, frostwork._frostwork as native;"
        f"print([native.resolve_label(label) for label in {labels!r}], 'frostwork.encoding' in sys.modules)"
    )
    expected = f"{[name] * len(labels)!r} False"
    assert subprocess.check_output([sys.executable, "-c", code], text=True).strip() == expected
    assert [resolve_label(label) for label in labels] == [name] * len(labels)


@pytest.mark.parametrize("name", list(_CODEC_NAMES))
def test_decode_matches_the_native_decoder(name):
    pairs = bytes(b for b1 in range(0x80, 0x100, 7) for b in (b1, 0xA1))
    body = bytes(range(256)) * 2 + pairs + b"\x1b$B<R\x1b(B"
    assert decode(body, name) == native_decode(body, name)
