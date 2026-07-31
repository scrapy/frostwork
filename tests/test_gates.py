"""Do the gates actually FAIL?

Every gate in this repo is a claim of the form "if the engine regresses, this goes red". That claim is
itself untested code, and it has been wrong three times: `enc_check` printed MISMATCH and exited 0; the
fuzzer filed real divergences into a bulk "expected" bucket; the corpus gate treated a supported selector
losing values as a coverage gap and passed. Each time the gate was green while the engine was broken.

So these tests seed a KNOWN regression into each gate's decision function and assert it goes red. They
are cheap (no page generation, no engine) and they fail loudly the moment a gate is loosened.

Run: .venv/bin/python -m pytest tests/test_gates.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

pytest.importorskip("parsel")  # the harness modules import parsel at module level


# --------------------------------------------------------------- corpus gate (tools/bench_corpus.py)
def test_corpus_gate_fails_on_a_lost_value():
    """A SUPPORTED selector going from ["a","b"] to ["a"] is a regression, not a coverage gap.

    `bench_corpus` runs with strict validation, so every selector it measures is one the engine claims to
    support. Grading a lost value as EMPTY/SUBSET and exiting 0 meant the corpus gate could not catch the
    single most likely regression: a rule change that drops values.
    """
    from bench_corpus import divergence_kind, is_value_bug

    # engine lost the second value
    assert is_value_bug(divergence_kind(["a"], ["a", "b"], "p::text"))
    # engine lost the column entirely
    assert is_value_bug(divergence_kind([], ["a"], "p::text"))
    # engine invented a value
    assert is_value_bug(divergence_kind(["a", "z"], ["a"], "p::text"))
    # engine split one text node into two (a One-cardinality field would truncate)
    assert is_value_bug(divergence_kind(["HELLO", "WORLD"], ["HELLOWORLD"], "p::text"))


def test_corpus_gate_still_tolerates_whitespace_only_differences():
    """The bar is NON-whitespace parity; a whitespace-only difference must not fail the gate, or the gate
    becomes unusable on real pages and gets switched off."""
    from bench_corpus import nonws_equal

    assert nonws_equal(["a", " "], ["a"])
    assert nonws_equal(["\n a \n"], ["a"])
    assert not nonws_equal(["a"], ["a", "b"])


# ------------------------------------------------------------------- fuzz attribution (diff_fuzz.py)
def test_fuzz_attribution_leaves_an_unexplained_divergence_novel():
    """A divergence no documented construct explains must reach the NOVEL bucket. The dropped-end-tag bug
    hid for months because a stale `<p>`-closer list filed it under `deep-p` instead."""
    import diff_fuzz

    # a plain document with no documented construct in it
    clean = b"<html><body><div><p>a</p><span>b</span></div></body></html>"
    assert not (diff_fuzz.constructs(clean) & diff_fuzz.DOCUMENTED)

    # `<p>` followed by a NON-closer must NOT be attributed to deep-p (that list was stale and wrong)
    nested = b"<html><body><div><p>a<option>b</option></p></div></body></html>"
    assert "deep-p" not in diff_fuzz.constructs(nested)

    # a genuine deep-`<p>` still IS attributed
    deep_p = b"<html><body><div><p>a<b><div>x</div></b></p></div></body></html>"
    assert "deep-p" in diff_fuzz.constructs(deep_p)


def test_fuzz_documented_constructs_do_not_include_ported_behaviour():
    """A construct that is IMPLEMENTED must not sit in the documented-divergence set: if it ever explains a
    divergence again that is a regression, and belongs in NOVEL.

    `unmatched-end` was the first. `nested-form` is the second — `<form>` closing an open `<form>` came in
    with libxml2's start-close pair table, and leaving it on the allow-list would have made a regression in
    that rule invisible to the fuzzer.
    """
    import diff_fuzz

    assert "unmatched-end" not in diff_fuzz.DOCUMENTED
    assert "nested-form" not in diff_fuzz.DOCUMENTED
    # still recognised as a label — the classifier should describe the page, it just isn't an excuse
    nested = b"<html><body><form>a<form>b</form></form></body></html>"
    assert "nested-form" in diff_fuzz.constructs(nested)


# ------------------------------------------------------------------ differential verdict (diff_lxml.py)
def test_differential_verdict_flags_a_value_regression():
    """The gate's own grader must call a lost/extra value DIVERGE for a non-SKIP bucket."""
    from diff_lxml import verdict

    assert verdict(["a"], ["a", "b"], "CONTROL", "p::text") == "DIVERGE"
    assert verdict(["a", "z"], ["a"], "CONTROL", "p::text") == "DIVERGE"
    assert verdict(["a"], ["a"], "CONTROL", "p::text") == "AGREE"
    # whitespace-only stays WS, not DIVERGE
    assert verdict([" a "], ["a"], "CONTROL", "p::text") == "WS"


def test_differential_batches_stay_inside_the_member_budget():
    """An over-budget batch returns empty columns for the surplus, which reads as divergence in every one
    of them — so the harness must batch below the engine's advertised limit."""
    import conformant
    from diff_lxml import MAX_SELECTORS_PER_PASS, _batches

    frostwork = pytest.importorskip("frostwork")
    limit = frostwork.check(["p::text"]).max_members
    assert MAX_SELECTORS_PER_PASS <= limit
    assert all(len(b) <= MAX_SELECTORS_PER_PASS for b in _batches(conformant.BASKET))


# ------------------------------------------------------------------- encoding gate (tools/enc_check.py)
def test_meta_prescan_matches_w3lib():
    """**w3lib** is the oracle for SNIFFING, not Parsel.

    `parsel.Selector(body=…)` with no encoding does not sniff `<meta>` at all — it defaults to UTF-8 — so
    oracling the prescan against it was meaningless: it "agreed" on every missed declaration because both
    produced mojibake. Scrapy picks a response encoding with `w3lib.encoding.html_to_unicode`, so that is
    what a prescan has to match.

    ONE deliberate divergence, per the project's oracle-bug policy: w3lib has no comment handling and so
    honours `<!-- <meta charset=big5> -->`, contrary to WHATWG's prescan and every browser. We skip
    comments and document the difference rather than reproduce the bug (see COMPATIBILITY.md).
    """
    frostwork = pytest.importorskip("frostwork")
    html_to_unicode = pytest.importorskip("w3lib.encoding").html_to_unicode
    parsel = pytest.importorskip("parsel")

    U8, W1252 = b'<p class="c">caf\xc3\xa9</p>', b'<p class="c">caf\xe9</p>'
    cases = [
        # (head, body bytes) — every one of these w3lib and a correct prescan must agree on
        (b'<meta data-http-equiv="content-type" content="text/html; charset=big5">', U8),
        (b'<meta http-equiv="refresh" content="0; url=/x?charset=big5">', U8),
        (b'<meta http-equiv="content-type" content="text/html" data-note="charset=big5">', U8),
        (b'<meta http-equiv="content-type" content="text/html; charset=windows-1252" title="a>b">', W1252),
        (b"<meta charset\n=\nwindows-1252>", W1252),
        (b"<!--><meta charset=windows-1252>", W1252),
        (b"<!--x--!><meta charset=windows-1252>", W1252),
        (b'<meta content="text/html; charset=big5">', U8),
        (b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">', W1252),
        (b"<meta charset=utf-8>", U8),
    ]
    for head, body in cases:
        doc = b"<html><head>" + head + b"</head><body>" + body + b"</body></html>"
        _, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
        want = parsel.Selector(text=txt).css("p.c::text").getall()
        got = frostwork.extract(doc, ["p.c::text"], strict=False)[0]
        assert got == want, f"prescan disagrees with w3lib for {head!r}"


def test_commented_charset_is_a_documented_divergence_from_w3lib():
    """Pin the one deliberate difference so it cannot drift silently in either direction."""
    frostwork = pytest.importorskip("frostwork")
    html_to_unicode = pytest.importorskip("w3lib.encoding").html_to_unicode
    parsel = pytest.importorskip("parsel")

    doc = (b"<html><head><!-- <meta charset=big5> --></head>"
           b'<body><p class="c">caf\xc3\xa9</p></body></html>')
    _, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
    w3lib_says = parsel.Selector(text=txt).css("p.c::text").getall()
    # don't hardcode the mojibake: the point is only that w3lib DID honour the commented declaration
    assert w3lib_says != ["café"], \
        "w3lib no longer honours a commented charset — drop our documented divergence"
    # we follow WHATWG/browsers: a declaration inside a comment declares nothing
    assert frostwork.extract(doc, ["p.c::text"], strict=False)[0] == ["café"]


def test_the_browser_difference_list_is_two_sided():
    """w3lib is the oracle for the SHARED encoding cases, not the target.

    The intended policy is browser/WHATWG correctness, so where w3lib and browsers disagree the gate has
    to assert the difference rather than report it as a mismatch — otherwise the only way to keep the
    target green is to reproduce w3lib's bugs. Two things make that list honest, and both are checked
    here: it exists as an enumerated table, and each row asserts w3lib's side TOO, so a difference that
    gets fixed upstream fails as STALE instead of quietly becoming a hole.

    The "does the gate exit nonzero" half is `test_encoding_gate_exits_nonzero_on_mismatch`; this test
    owns the "is the w3lib column still true" half, spot-checked on one row.
    """
    frostwork = pytest.importorskip("frostwork")
    html_to_unicode = pytest.importorskip("w3lib.encoding").html_to_unicode
    parsel = pytest.importorskip("parsel")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "tools", "enc_check.py")).read()
    assert "BROWSER_DIFFERENCES" in src, "the encoding gate must enumerate its browser differences"
    assert "is STALE" in src, "each row must also assert w3lib's side, or a fixed row becomes a hole"

    doc = (b'<html><head></head><body><meta charset="windows-1252">'
           b'<p class="c">caf\xe9</p></body></html>')
    _, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
    assert parsel.Selector(text=txt).css("p.c::text").getall() != ["café"], \
        "w3lib now honours a <meta charset> after <body> — that row of the list is stale"
    assert frostwork.extract(doc, ["p.c::text"], strict=False)[0] == ["café"], \
        "the prescan must not stop at <body> (browser behaviour)"


def test_the_decoder_sweep_is_exhaustive_not_sampled():
    """A gate that SAMPLES must not be read as a gate that proves.

    This one did. It checked 800 assigned characters per legacy label, found nothing, and the contract
    then said shift_jis / euc-jp / euc-kr / gb18030 were "at full parity on validly encoded text". They
    are not: a crawled EUC-JP wiki containing the byte pair A1 C1 — the JIS wave dash, U+FF5E to WHATWG
    and every browser, U+301C to Python's `euc_jp` — walked straight through it. 800 samples out of
    ~6,900 assigned sequences is a 12% chance of catching any one of them, and a green run reads exactly
    like a proof.

    So the sweep now enumerates every assigned two-byte sequence for every label, and this test owns the
    "it did not quietly go back to sampling" half: the table must name all five labels, and the witness
    row must still be true on BOTH sides (as with the browser-difference list, a row that gets fixed
    upstream has to fail as stale rather than become a hole).
    """
    frostwork = pytest.importorskip("frostwork")
    html_to_unicode = pytest.importorskip("w3lib.encoding").html_to_unicode
    parsel = pytest.importorskip("parsel")
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "tools", "enc_check.py")).read()
    assert "INDEX_DIVERGENCE" in src, "the decoder gate must enumerate its index differences"
    for label in ("big5", "euc-jp", "euc-kr", "gb18030", "shift_jis"):
        assert f'"{label}"' in src, f"{label} is not swept — a label with no row is not 'full parity'"
    assert "PUA_UNASSIGNED" in src, \
        "the vendor private-use class must be COUNTED, or it hides real divergences in bulk"

    # the real-page witness, both sides. EUC-JP A1 C1.
    doc = b'<html><head><meta charset="euc-jp"></head><body><p class="c">\xa1\xc1</p></body></html>'
    _, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
    assert parsel.Selector(text=txt).css("p.c::text").getall() == ["〜"], \
        "Parsel now decodes euc-jp A1C1 as something else — that INDEX_DIVERGENCE row is stale"
    assert frostwork.extract(doc, ["p.c::text"], strict=False)[0] == ["～"], \
        "euc-jp A1C1 must be U+FF5E (the WHATWG index, what browsers show)"


# ------------------------------------------------------- rule-table mutation sweep (mutate_rules.py)
def test_mutation_sweep_enumerates_every_rule_table():
    """The sweep's own failure mode is enumerating NOTHING and reporting success.

    Its universe must be INDEPENDENT of the engine's tables — a name the engine wrongly treats as ordinary
    (`em`, `section`, and until recently `listing`) has no cell to flip, but flipping it TO closing is
    exactly the check that a gate would notice that mistake, and drawing the universe from our own tables
    is the self-referential trap that hid `<colgroup>` having no rule at all and then `head`/`listing`/
    `xmp`/`plaintext` having no rows at all.

    A full name-pair sweep would be 142² mutants (about 13 hours), so the close dimension runs ONE
    REPRESENTATIVE PER ORACLE-DERIVED BEHAVIOUR CLASS. That compression is only sound if (a) the classes
    come from the measurement rather than from memory, and (b) every name is in some class — which is what
    this test pins. Per-name coverage is carried by the rule AUDIT, which probes all 142 names.
    """
    import mutate_rules
    from gen_tree_rules import ELEMENTS, Oracle, classify

    specs = [s for s, _label in mutate_rules.mutants(mutate_rules.tag_ids())]
    kinds = {s.split(":")[0] for s in specs}
    assert kinds == {"close", "scope", "void", "mode"}, kinds

    name_class, by_class, _ = classify(Oracle())
    reps = set(mutate_rules.CLOSE_NAMES)
    # (a) every behaviour class the oracle found has a representative in the sweep
    assert {name_class[r] for r in reps} == set(by_class), \
        f"classes with no representative: {set(by_class) - {name_class[r] for r in reps}}"
    # (b) every element name in the universe belongs to a represented class — including the names the
    # engine treats as ordinary, and the four the hand-written lists forgot
    for n in ELEMENTS + ["em", "strong", "section", "head", "listing", "xmp", "plaintext"]:
        assert name_class[n] in by_class, f"{n} is in no class"
    # the pair that shipped wrong is its own class either way: `<dd>` closing an open `<dt>`
    assert "close:dd,dt" in specs
    # a void element is never the OPEN element, so no cell should name one there
    voids = set(mutate_rules.VOID_NAMES)
    bad = [s for s in specs if s.startswith("close:") and s.split(",", 1)[1] in voids]
    assert not bad, f"unobservable cells enumerated (void open element): {bad[:5]}"
    # ...but a void tag as the INCOMING tag is a real cell: `<col>` closes an open `<p>`
    assert "close:col,p" in specs
    # the void set is derived, so the HTML4 names libxml2 treats as empty are in it...
    for n in ("basefont", "frame", "isindex"):
        assert f"void:{n}" in specs, f"{n} missing from the void universe"
    # ...and so are the HTML5-era names it deliberately keeps OPEN, or the sweep cannot tell
    # "correctly absent" from "never considered"
    for n in ("embed", "source", "track", "wbr"):
        assert f"void:{n}" in specs
    # every DATA MODE is mutable, over the WHOLE universe rather than the modes we already know about:
    # "the raw-text set is the four names we remembered" is the bug this dimension exists to catch
    assert {f"mode:{n}" for n in ELEMENTS} <= set(specs)
    assert {"mode:iframe", "mode:noembed", "mode:xmp", "mode:plaintext", "mode:listing"} <= set(specs)


def test_the_known_start_close_gap_may_shrink_but_never_grow():
    """The cheapest way to make the rule audit green is to append to `KNOWN_START_CLOSE_GAP`.

    It held 87 pairs, and the fix was to port libxml2's table rather than live with them, so the list is
    now EMPTY and this test keeps it that way: appending an entry is appending a divergence.
    """
    from audit_tree_rules import KNOWN_START_CLOSE_GAP

    total = sum(len(v) for v in KNOWN_START_CLOSE_GAP.values())
    assert total == 0, (f"the known start-close gap is {total}, expected 0 — it was closed by porting "
                        f"libxml2's htmlStartClose table into implied_close::start_closes. A new entry "
                        f"here is a NEW divergence and needs to be a deliberate, reviewed decision.")
    # every entry must name real tags (a typo would silently mask a real divergence forever)
    for inc, opens in KNOWN_START_CLOSE_GAP.items():
        assert inc.isalnum() and opens, (inc, opens)
        assert len(set(opens)) == len(opens), f"duplicate open tags for <{inc}>: {opens}"


def test_the_rule_audit_notices_a_stale_gap_entry():
    """A pair that stops diverging must fail, or the allow-list outlives the bug it documents.

    Seed a KNOWN-GAP entry for a pair that actually AGREES (`<td>` does not close an open `<em>`) and check
    the audit records a failure. Without this the list could accumulate entries for divergences that were
    fixed years earlier, each one a hole where a REGRESSION would now pass.
    """
    pytest.importorskip("frostwork")
    import audit_tree_rules as A

    original = A.KNOWN_START_CLOSE_GAP
    try:
        A.KNOWN_START_CLOSE_GAP = dict(original, td=list(original.get("td", [])) + ["em"])
        audit = A.Audit(verbose=False)
        A.audit_start_close_pairs(audit)
        stale = [f for f in audit.fails if "now AGREE" in str(f[1])]
        assert stale, "a KNOWN_START_CLOSE_GAP entry that agrees must be reported as stale"
    finally:
        A.KNOWN_START_CLOSE_GAP = original

    # and with the real list, that section is clean
    audit = A.Audit(verbose=False)
    A.audit_start_close_pairs(audit)
    assert not audit.fails, audit.fails[:3]


def test_the_oracle_version_guard_actually_rejects_an_old_libxml2():
    """Every gate's verdict is meaningless if the oracle is the wrong version.

    lxml vendors its own libxml2 and a pinned lxml does NOT pin it — the same release ships 2.14 on
    Linux/macOS and 2.11.9 on Windows, where CR-in-attributes and a raw `<` in text parse differently. So
    `tools/oracle.py` refuses to run. That refusal is the load-bearing part: if it silently passed, a
    Windows CI job would grade the engine against a spec it never claimed, and the divergences would look
    like engine bugs. Simulate the old version and require the exit.
    """
    import lxml.etree

    import oracle

    original = lxml.etree.LIBXML_VERSION
    try:
        lxml.etree.LIBXML_VERSION = (2, 11, 9)
        with pytest.raises(SystemExit) as e:
            oracle.require(False)
        assert e.value.code == 2, f"expected exit 2 (toolchain unusable), got {e.value.code}"
        # ...and the documented escape hatch must still work, or exploring on such a platform is impossible
        oracle.require(True)
    finally:
        lxml.etree.LIBXML_VERSION = original
    oracle.require(False)  # the real toolchain is fine


def test_the_mutate_feature_can_never_reach_a_shipped_build():
    """`--features mutate` makes the tree-construction tables depend on an ENVIRONMENT VARIABLE. That is
    the right trade for a mutation sweep and a catastrophe in a released wheel, so pin the two things that
    keep it out: it must be off by default, and it must not be in the feature list maturin builds from."""
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    cargo = open(os.path.join(root, "Cargo.toml")).read()
    pyproject = open(os.path.join(root, "pyproject.toml")).read()

    feats = cargo.split("[features]", 1)[1]
    assert "mutate = []" in feats, "the mutate feature must exist and be empty (opt-in only)"
    assert "default =" not in feats, "mutate must not be reachable through a default feature set"
    # maturin builds the wheel from pyproject's feature list
    ml = [ln for ln in pyproject.splitlines() if ln.strip().startswith("features")]
    assert ml and all("mutate" not in ln for ln in ml), f"mutate must not be in {ml}"

    # and with the feature OFF the hook is compiled out entirely, so the module is an identity
    src = open(os.path.join(root, "src", "mutate.rs")).read()
    off = src.split('#[cfg(not(feature = "mutate"))]', 1)[1].split('#[cfg(feature = "mutate")]', 1)[0]
    assert "env" not in off, "the feature-off path must not read the environment"
    # every hook must be an inline identity when the feature is off — asserted structurally rather than
    # as a count, so adding a hook cannot quietly ship one that does real work
    fns = off.count("pub fn ")
    assert fns and off.count("inline(always)") == fns, \
        f"{fns} hooks but {off.count('inline(always)')} marked inline(always)"
    for line in off.splitlines():
        if line.strip().startswith("pub fn "):
            assert line.rstrip().endswith(("-> bool {", "-> u8 {", "-> DataMode {")), line


def test_encoding_gate_exits_nonzero_on_mismatch():
    """`enc_check` printed MISMATCH and exited 0, so `make gate` and CI stayed green through an encoding
    regression. Guard the exit itself."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "tools", "enc_check.py")).read()
    assert "raise SystemExit(1)" in src, "enc_check must exit nonzero on mismatch"
