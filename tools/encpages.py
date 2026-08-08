"""Legacy-encoding pages crossed with non-ASCII SELECTOR literals.

Every other generator here emits UTF-8, and both selector-construction sites in `diff_lxml.py`
hardcoded `encoding="utf-8"` — so the correctness gate had **never run a non-UTF-8 page**. That is
the hole that let a silent value loss ship: an attribute NAME arrives as raw page bytes while a
selector's is UTF-8, so `[data-año]` matched a UTF-8 page and returned NOTHING for the same document
in windows-1252, where lxml (which decodes before tokenizing) matches. No generated pair could have
caught it, and the same was true of the XPath spelling being refused in every encoding.

A case is `(bytes, label, [(selector, oracle_should_be_nonempty)])`: one document emitted in `label`,
with non-ASCII in every position that reaches the byte/UTF-8 boundary — a class, an id, an attribute
NAME, an attribute VALUE and element TEXT — in both the CSS and the XPath spelling.

Two rules this generator has to keep, both of them lessons the repo already paid for:

* **A selector the ORACLE answers nowhere can only catch an over-match.** Every positive selector
  here is emitted with `True` so the harness can assert lxml actually returned something; a family of
  always-empty columns would grade AGREE against an engine that had been ripped out.
* **The alphabet is per ENCODING, derived by asking the codec.** A hand-written word list that
  windows-1252 cannot represent would silently degrade to a `?` and test nothing — `usable()` drops
  a label whose words do not round-trip instead.
"""
from __future__ import annotations

# Per label: words used for class / id / attribute-name / attribute-value / text. All ASCII-lowercase
# where they contain ASCII at all, because an XPath name literal must be (libxml2 lowercases ASCII in
# the tree and XPath compares case-sensitively — see `xpath::valid_name_impl`).
ALPHABETS = {
    "windows-1252": ["café", "año", "über", "niño", "prêt"],
    "iso-8859-7": ["αλφα", "βητα", "γαμμα", "δελτα", "εψιλον"],
    "windows-1251": ["привет", "значение", "имя", "текст", "цена"],
    "shift_jis": ["属性", "日本語", "値", "名前", "価格"],
    "euc-jp": ["属性", "日本語", "値", "名前", "価格"],
    "gb18030": ["属性", "中文", "值", "名字", "价格"],
    "big5": ["屬性", "中文", "值", "名字", "價格"],
    "euc-kr": ["속성", "한국어", "값", "이름", "가격"],
}


def usable(label):
    """Labels whose whole alphabet round-trips through the codec. Asked, not assumed: a word the
    codec cannot represent encodes to `?` and would test ASCII while claiming to test the encoding."""
    words = ALPHABETS.get(label)
    if not words:
        return False
    try:
        return all(w.encode(label).decode(label) == w for w in words)
    except (LookupError, UnicodeError):
        return False


LABELS = tuple(l for l in ALPHABETS if usable(l))


def generate(rng, label):
    """One (bytes, label, [(selector, expect_nonempty)]) case in `label`."""
    words = ALPHABETS[label]
    cls, ident, aname, aval, text = (rng.choice(words) for _ in range(5))
    # a value the page does NOT carry, for the over-match half
    absent = next(w for w in words + ["zzz"] if w != aval)

    doc = (
        "<html><head><title>t</title></head><body>"
        f'<div class="{cls}" id="{ident}" data-{aname}="{aval}">{text}</div>'
        f'<p class="other">plain</p>'
        "</body></html>"
    )
    sels = [
        # CSS: every position that crosses the boundary
        (f".{cls}::text", True),
        (f"#{ident}::text", True),
        (f"[data-{aname}]::text", True),
        (f'[data-{aname}="{aval}"]::text', True),
        (f'[data-{aname}^="{aval[:1]}"]::text', True),
        (f'[data-{aname}*="{aval[1:2] or aval[:1]}"]::text', True),
        (f"div::attr(data-{aname})", True),
        (f".{cls}[data-{aname}]::text", True),
        (f'div:contains("{text}")::text', True),
        # XPath: the same questions in the other front-end
        (f"//div[@data-{aname}]/text()", True),
        (f"//div/@data-{aname}", True),
        (f'//div[@data-{aname}="{aval}"]/text()', True),
        (f'//div[contains(@data-{aname},"{aval[:1]}")]/text()', True),
        (f'//*[@id="{ident}"]/text()', True),
        (f'//div[.="{text}"]/text()', True),
        # OVER-MATCH half: the oracle answers these nowhere, so they can only catch a false positive
        (f'[data-{aname}="{absent}"]::text', False),
        (f"[data-{aname}zz]::text", False),
        (f"//div[@data-{aname}zz]/text()", False),
    ]
    return doc.encode(label), label, sels
