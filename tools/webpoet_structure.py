"""One structural signature for parsed HTML, shared by everything that compares a Frostwork value with
lxml's.

Frostwork's outer HTML is RAW SOURCE and lxml's is a re-serialization, so every comparison of the two has
to say what the allowance is: SERIALIZATION only. Attribute quoting, entity spelling and an implied end tag
all vanish when both sides are parsed; text, tails, attribute values and comments are compared exactly.

One function because two were two standards — the benchmark's copy collapsed whitespace, so `<pre>a  b</pre>`
and `<pre>a b</pre>` were the same page while the differential's copy said otherwise.
"""

from __future__ import annotations


def structure(el) -> tuple:
    """The exact structure of one parsed element: tag, attributes, text, children, tail.

    Comments and processing instructions keep their text too — a signature built from elements alone drops
    them AND the text that follows them."""
    if not isinstance(el.tag, str):  # comment or processing instruction
        return ("#" + type(el).__name__, (), el.text or "", (), el.tail or "")
    return (
        el.tag,
        tuple(sorted(el.attrib.items())),
        el.text or "",
        tuple(structure(c) for c in el),
        el.tail or "",
    )


def subtree(el) -> tuple:
    """:func:`structure` without the root's own TAIL — the signature of one node's subtree.

    A tail is text that follows the node in its PARENT, so it belongs to the document the node came out of:
    `<area class=k>a` has a tail of `"a"` in the original tree and none once it stands alone. Descendants
    keep theirs, which is where a tail is content."""
    return structure(el)[:4]


def structure_of(html: str) -> tuple:
    """The same, for one HTML fragment. Raises if it cannot be parsed — a caller that wants to grade that as
    a divergence should catch it rather than be handed a value equal to nothing."""
    from lxml.html import document_fromstring  # noqa: PLC0415

    return structure(document_fromstring(html))
