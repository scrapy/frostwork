from typing import Optional

from ._frostwork import EncodingDecision, resolve_document

__all__ = ["EncodingDecision", "FrostworkEncodingBackend"]


class FrostworkEncodingBackend:
    """Frostwork's detection and decoding as a ``w3lib.encoding`` encoding backend.

    Handed to a ``w3lib.encoding.EncodingContext`` — and through it to a Scrapy or web-poet
    response — it makes ``.encoding`` and ``.text`` answer with the engine's own decoder, so the
    characters a scraper reads and the characters :func:`frostwork.extract` matches over are the
    same ones. The policy is the browser's rather than w3lib's; where the two differ is tabulated
    under Encoding in ``docs/COMPATIBILITY.md``.

    ``policy_id`` names that policy wherever one is recorded (a web-poet fixture, a response
    cache), so what was recorded under one policy is not replayed under another.

    w3lib's backend protocol is structural, so nothing here imports w3lib: the package needs no
    runtime dependency to be a backend, and an installed w3lib without the protocol still works.
    """

    policy_id = "frostwork-v1"

    def resolve(
        self, body: bytes, content_type: str = "", encoding: Optional[str] = None
    ) -> EncodingDecision:
        """The encoding of *body*: BOM → *encoding* → *content_type*'s charset → ``<meta>`` → UTF-8.

        A label that names no encoding is ignored rather than refused, so a response never fails to
        report an encoding because of what a publisher wrote.
        """
        return resolve_document(body, content_type, encoding)
