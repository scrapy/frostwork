from __future__ import annotations

import codecs
import encodings
import sys
from functools import lru_cache
from typing import TYPE_CHECKING, Optional, Tuple, Union, cast

if TYPE_CHECKING:
    if sys.version_info >= (3, 12):
        from collections.abc import Buffer
    else:
        from typing_extensions import Buffer

from ._frostwork import autodetect_encoding as _autodetect_encoding
from ._frostwork import bom_encoding as _bom_encoding
from ._frostwork import declared_encoding as _declared_encoding
from ._frostwork import decode as _decode
from ._frostwork import detect_encoding as _detect_encoding
from ._frostwork import resolve_label as _resolve_label

__all__ = [
    "autodetect_encoding",
    "bom_encoding",
    "codec_name",
    "declared_encoding",
    "decode",
    "detect_encoding",
    "resolve_label",
]

# WHATWG encoding name -> Python codec name, for every encoding of the Encoding Standard.
_CODEC_NAMES = {
    "UTF-8": "utf-8",
    "IBM866": "cp866",
    **{f"ISO-8859-{n}": f"iso8859-{n}" for n in (2, 3, 4, 5, 6, 7, 8, 10, 13, 14, 15, 16)},
    # Same index as ISO-8859-8; the two differ only in how a browser lays out bidirectional text.
    "ISO-8859-8-I": "iso8859-8",
    "KOI8-R": "koi8-r",
    "KOI8-U": "koi8-u",
    "macintosh": "mac-roman",
    "windows-874": "cp874",
    **{f"windows-125{n}": f"cp125{n}" for n in range(9)},
    "x-mac-cyrillic": "mac-cyrillic",
    "GBK": "gbk",
    "gb18030": "gb18030",
    "Big5": "big5hkscs",
    "EUC-JP": "euc_jp",
    "ISO-2022-JP": "iso2022_jp",
    "Shift_JIS": "cp932",
    "EUC-KR": "cp949",
    "replacement": "replacement",
    "UTF-16BE": "utf-16-be",
    "UTF-16LE": "utf-16-le",
    "x-user-defined": "x-user-defined",
}


_cached_resolve_label = lru_cache(maxsize=256)(_resolve_label)


def resolve_label(label: str) -> Optional[str]:
    """The WHATWG name of the encoding charset *label* names (``"cp874"`` → ``"windows-874"``), or
    ``None`` if it names none (``"cp437"``).

    *label* can be a WHATWG label or a Python codec name, e.g. Scrapy's ``response.encoding``.
    """
    return _cached_resolve_label(label)


def codec_name(label: str) -> Optional[str]:
    """The Python codec name of the encoding *label* names (``"latin1"`` → ``"cp1252"``), or ``None``
    if :func:`resolve_label` resolves it to no WHATWG encoding.

    Where a Python codec decodes differently from the WHATWG decoder, as with ``euc_jp``, the name
    still identifies the WHATWG encoding to every function here and to :func:`frostwork.extract`; use
    :func:`decode` to decode with it. The name of ``ISO-8859-8-I`` resolves back to ``ISO-8859-8``,
    which decodes the same.
    """
    name = resolve_label(label)
    return None if name is None else _CODEC_NAMES[name]


def detect_encoding(
    html: Union[Buffer, str], encoding: Optional[str] = None, *, url: Optional[str] = None
) -> str:
    """The encoding :func:`frostwork.extract` would scan *html* with, as a WHATWG name.

    That is the first answer of :func:`bom_encoding`, the *encoding* label, :func:`declared_encoding`
    and :func:`autodetect_encoding`, in that order. A label that names no encoding is ignored, as
    browsers do. A ``str`` is already-decoded text, so the answer is ``"UTF-8"``. See the Encoding
    section of docs/COMPATIBILITY.md for how this differs from w3lib.
    """
    return _detect_encoding(html if isinstance(html, (bytes, str)) else bytes(html), encoding, url=url)


def bom_encoding(body: Buffer) -> Optional[Tuple[str, int]]:
    """The encoding the byte order mark at the start of *body* names, and the length of that BOM.

    A UTF-16 document that starts with an XML declaration and no BOM is recognized too, with length
    0. A UTF-32LE BOM reads as a UTF-16LE one, as in browsers.
    """
    return _bom_encoding(bytes(body))


def declared_encoding(body: Buffer) -> Optional[str]:
    """The encoding a ``<meta>`` tag or an XML declaration in *body* declares, as a browser finds it.

    A declaration of a UTF-16 encoding gives ``"UTF-8"``, and of ``x-user-defined``,
    ``"windows-1252"``.
    """
    return _declared_encoding(bytes(body))


def autodetect_encoding(body: Buffer, *, url: Optional[str] = None) -> str:
    """``"UTF-8"`` if *body* is valid UTF-8, else the encoding guessed from its bytes and from the
    top-level domain of *url*, as Firefox does for a page that declares none."""
    return _autodetect_encoding(bytes(body), url=url)


def decode(body: Buffer, label: str) -> str:
    """*body* decoded with the WHATWG decoder of *label*, a WHATWG label or a Python codec name,
    replacing invalid input with U+FFFD.

    A BOM is decoded like any other bytes; strip the length :func:`bom_encoding` returns first. Raises
    :exc:`LookupError` if :func:`resolve_label` cannot resolve *label*.
    """
    name = resolve_label(label)
    if name is None:
        raise LookupError(f"unknown encoding: {label}")
    body = bytes(body)
    if name == "UTF-8":
        return body.decode("utf-8", "replace")
    if name in _SINGLE_BYTE:
        return codecs.charmap_decode(body, "strict", _single_byte_table(name))[0]  # type: ignore[arg-type]
    return _decode(body, name)


# https://encoding.spec.whatwg.org/#legacy-single-byte-encodings, plus x-user-defined, whose decoder maps
# each byte on its own too.
_SINGLE_BYTE = frozenset(
    name
    for name in _CODEC_NAMES
    if name.startswith(("ISO-8859-", "windows-", "KOI8-"))
    or name in {"IBM866", "macintosh", "x-mac-cyrillic", "x-user-defined"}
)


@lru_cache(maxsize=None)
def _single_byte_table(name: str) -> str:
    """The decoder of the single-byte encoding *name*, as a table for :func:`codecs.charmap_decode`."""
    return _decode(bytes(range(256)), name)


_X_USER_DEFINED_DECODING = _single_byte_table("x-user-defined")
_X_USER_DEFINED_ENCODING = codecs.charmap_build(_X_USER_DEFINED_DECODING)


def _x_user_defined_encode(text: str, errors: str = "strict") -> Tuple[bytes, int]:
    return codecs.charmap_encode(text, errors, _X_USER_DEFINED_ENCODING)


def _x_user_defined_decode(data: Buffer, errors: str = "strict", /) -> Tuple[str, int]:
    return codecs.charmap_decode(data, errors, _X_USER_DEFINED_DECODING)  # type: ignore[arg-type]


def _replacement_encode(text: str, errors: str = "strict") -> Tuple[bytes, int]:
    return codecs.utf_8_encode(text, errors)


def _replacement_decode(data: Buffer, errors: str = "strict", /) -> Tuple[str, int]:
    data = bytes(data)
    if not data:
        return "", 0
    error = UnicodeDecodeError("replacement", data, 0, len(data), "the encoding decodes nothing")
    text, _ = codecs.lookup_error(errors)(error)
    return cast(str, text), len(data)


class _XUserDefinedIncrementalEncoder(codecs.IncrementalEncoder):
    def encode(self, input: str, final: bool = False) -> bytes:
        return _x_user_defined_encode(input, self.errors)[0]


class _XUserDefinedIncrementalDecoder(codecs.IncrementalDecoder):
    def decode(self, input: Buffer, final: bool = False) -> str:
        return _x_user_defined_decode(input, self.errors)[0]


class _XUserDefinedStreamWriter(codecs.StreamWriter):
    def encode(self, input: str, errors: str = "strict") -> Tuple[bytes, int]:
        return _x_user_defined_encode(input, errors)


class _XUserDefinedStreamReader(codecs.StreamReader):
    def decode(self, input: Buffer, errors: str = "strict") -> Tuple[str, int]:
        return _x_user_defined_decode(input, errors)


class _ReplacementIncrementalEncoder(codecs.IncrementalEncoder):
    def encode(self, input: str, final: bool = False) -> bytes:
        return _replacement_encode(input, self.errors)[0]


class _ReplacementIncrementalDecoder(codecs.IncrementalDecoder):
    """Decodes the first non-empty input into one error and everything after it into nothing."""

    def __init__(self, errors: str = "strict") -> None:
        super().__init__(errors)
        self._started = False

    def decode(self, input: Buffer, final: bool = False) -> str:
        if self._started:
            return ""
        text, consumed = _replacement_decode(input, self.errors)
        self._started = consumed > 0
        return text

    def reset(self) -> None:
        self._started = False

    def getstate(self) -> Tuple[bytes, int]:
        return b"", int(self._started)

    def setstate(self, state: Tuple[bytes, int]) -> None:
        self._started = bool(state[1])


class _ReplacementStreamWriter(codecs.StreamWriter):
    def encode(self, input: str, errors: str = "strict") -> Tuple[bytes, int]:
        return _replacement_encode(input, errors)


class _ReplacementStreamReader(codecs.StreamReader):
    _started = False

    def decode(self, input: Buffer, errors: str = "strict") -> Tuple[str, int]:
        if self._started:
            return "", len(bytes(input))
        text, consumed = _replacement_decode(input, errors)
        self._started = consumed > 0
        return text, consumed

    def reset(self) -> None:
        super().reset()
        self._started = False


_CODECS = {
    "x_user_defined": codecs.CodecInfo(
        _x_user_defined_encode,
        _x_user_defined_decode,
        _XUserDefinedStreamReader,
        _XUserDefinedStreamWriter,
        _XUserDefinedIncrementalEncoder,
        _XUserDefinedIncrementalDecoder,
        name="x-user-defined",
    ),
    "replacement": codecs.CodecInfo(
        _replacement_encode,
        _replacement_decode,
        _ReplacementStreamReader,
        _ReplacementStreamWriter,
        _ReplacementIncrementalEncoder,
        _ReplacementIncrementalDecoder,
        name="replacement",
    ),
}
# Python has no codec for these two WHATWG encodings, so without them the names `codec_name()` returns
# for them would fail in `str.encode()`, `bytes.decode()` and w3lib's `resolve_encoding()` and
# `to_unicode()`. The codec registry is process-wide, so docs/PYTHON.md tells users that importing
# this module registers them.
codecs.register(lambda name: _CODECS.get(encodings.normalize_encoding(name)))
