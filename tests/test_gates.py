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


# ------------------------------------------------------- rule-table mutation sweep (mutate_rules.py)
def test_mutation_sweep_enumerates_every_rule_table():
    """The sweep's own failure mode is enumerating NOTHING and reporting success.

    Two properties matter. Its universe must be INDEPENDENT of the engine's tables — a name the engine
    wrongly treats as ordinary (`em`, `section`) has no cell to flip, but flipping it TO closing is exactly
    the check that a gate would notice that mistake, and drawing the universe from our own tables is the
    self-referential trap that hid `<colgroup>` having no rule at all. And it must not waste effort on
    cells that are unobservable by construction: a VOID element is never the open element.
    """
    import mutate_rules

    specs = [s for s, _label in mutate_rules.mutants(mutate_rules.tag_ids())]
    kinds = {s.split(":")[0] for s in specs}
    assert kinds == {"close", "scope", "void"}, kinds

    # independence: names the engine lumps into `tag::OTHER` / `sc::OTHER` are still probed
    for n in ("em", "strong", "section", "span", "ruby"):
        assert f"close:{n},p" in specs, f"{n} missing from the close universe"
    # the pair that shipped wrong: `<dd>` closing an open `<dt>`
    assert "close:dd,dt" in specs
    # a void element is never the OPEN element, so no cell should name one there
    voids = set(mutate_rules.VOID_NAMES)
    bad = [s for s in specs if s.startswith("close:") and s.split(",", 1)[1] in voids]
    assert not bad, f"unobservable cells enumerated (void open element): {bad[:5]}"
    # ...but a void tag as the INCOMING tag is a real cell: `<col>` and `<hr>` close an open `<p>`
    assert "close:col,p" in specs and "close:hr,p" in specs
    # and the void set itself must include the HTML5-era names libxml2 deliberately keeps OPEN,
    # or the sweep cannot tell "correctly absent" from "never considered"
    for n in ("embed", "source", "track", "wbr"):
        assert f"void:{n}" in specs


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
            assert line.rstrip().endswith("-> bool {") or line.rstrip().endswith("-> u8 {"), line


def test_encoding_gate_exits_nonzero_on_mismatch():
    """`enc_check` printed MISMATCH and exited 0, so `make gate` and CI stayed green through an encoding
    regression. Guard the exit itself."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                            "tools", "enc_check.py")).read()
    assert "raise SystemExit(1)" in src, "enc_check must exit nonzero on mismatch"
