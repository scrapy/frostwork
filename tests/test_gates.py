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
    this test pins. Per-name coverage is carried by the rule AUDIT, which probes every name.
    """
    import mutate_rules
    from gen_tree_rules import ELEMENTS, Oracle, classify

    specs = [s for s, _label in mutate_rules.mutants()]
    kinds = {s.split(":")[0] for s in specs}
    assert kinds == {"close", "prio", "void", "mode"}, kinds

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
    # A name that can never be on the stack when a start tag arrives is unobservable BY CONSTRUCTION, so
    # enumerating it wastes a mutant and lands in the survivor list as a FALSE ALARM. Restricting this to
    # the void half did exactly that: a full sweep reported 39 `close:<X>,title` survivors, because
    # `title` is RCDATA. The exclusion is derived from the oracle (void + non-normal data mode).
    unobs = set(mutate_rules.UNOBSERVABLE_AS_OPEN)
    assert {"title", "script", "style", "iframe", "xmp", "plaintext", "br", "col"} <= unobs
    bad = [s for s in specs if s.startswith("close:") and s.split(",", 1)[1] in unobs]
    assert not bad, f"unobservable cells enumerated (open element can never be on the stack): {bad[:5]}"
    # ...but `html`/`body` ARE on the stack — nothing closes them, yet a mutation that makes something
    # close one IS observable, so they must not be excluded on that basis
    assert "body" not in unobs and "html" not in unobs
    # ...and no class may be represented by an unobservable name while it holds an observable one, or the
    # whole column disappears from the sweep (`body`/`div` share a class with the void `hr` and raw `xmp`)
    tops = {s.split(",", 1)[1] for s in specs if s.startswith("close:")}
    for cls, names in by_class.items():
        if any(n not in unobs for n in names):
            assert tops & set(names), f"class {cls} has an observable name but no swept representative"
    # a void tag as the INCOMING tag is a real cell: `<col>` closes an open `<p>`
    assert "close:col,p" in specs
    # END-TAG PRIORITY is one mutant per NAME (one table feeds the answer, so nothing can mask the flip),
    # over the same universe rather than the engine's own ids — the `scope:<tag_id>` enumeration it
    # replaced could only reach names the engine already treated as special, which is why the ORDER inside
    # the table machinery (`</tr>` cannot unwind an open `<tbody>`) was never probed at all.
    prio = {s.split(":", 1)[1] for s in specs if s.startswith("prio:")}
    assert {"table", "tbody", "thead", "tfoot", "tr", "td", "th", "div"} <= prio, "the chain itself"
    assert {"em", "section", "listing", "caption", "colgroup", "s"} <= prio, "and names it must NOT rank"
    # ...minus `<html>`/`<head>`, which cannot be open above a match at all. `<body>` CAN — after a
    # `</body>` libxml2 starts a second one, and it out-ranks every end tag there — so it must stay in the
    # sweep. It was excluded once on exactly the reasoning that fits the other two, and a crawled page
    # proved that reasoning wrong.
    assert not (prio & {"html", "head"})
    assert "body" in prio, "a <body> after </body> is a reachable open element"
    assert not (prio & set(mutate_rules.UNOBSERVABLE_AS_OPEN)), "a void/rawtext name is never on the stack"
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


def test_the_mutation_sweep_refuses_to_run_against_an_inert_build():
    """The sweep's own worst failure is reporting survivors when no mutation was applied at all.

    Its baseline check only proves the detectors are green when NOTHING is mutated — a build without the
    `mutate` feature passes that and then reports every mutant as a survivor. The `mutate` artifacts are
    shared state (a release binary plus whatever `maturin develop` last installed into the venv), so
    anything else that builds swaps them mid-run: that happened during a full sweep and 451 of 1621
    mutants came back as a contiguous tail of false survivors, which reads as a coverage collapse and
    means nothing. So a canary mutation must be SEEN before and during the run, and failure is fatal.
    """
    import mutate_rules

    env = {"PATH": os.environ.get("PATH", "")}
    green = mutate_rules.Detector("never-red", [sys.executable, "-c", "raise SystemExit(0)"], "green")
    red = mutate_rules.Detector("goes-red", [sys.executable, "-c", "raise SystemExit(1)"], "red")

    # nothing notices the canary -> the run must abort, not proceed
    with pytest.raises(SystemExit) as e:
        mutate_rules.check_canary([green], env, "in a test")
    assert "CANARY FAILED" in str(e.value) and "mutate" in str(e.value)
    # ...and a detector that does notice lets the sweep proceed
    mutate_rules.check_canary([red], env, "in a test")
    mutate_rules.check_canary([green, red], env, "in a test")
    # the canary itself must be a mutation a gate really covers, not an arbitrary spec
    assert mutate_rules.CANARY_SPEC in dict(mutate_rules.mutants())
    assert mutate_rules.CANARY_EVERY > 0, "a start-only canary cannot catch a mid-run rebuild"


def test_the_element_universe_sees_every_engine_owned_name():
    """A rule can name a tag from anywhere in the engine, and the universe must contain every such name —
    a rule with no name to probe cannot fail a gate.

    The scan behind that invariant has been too narrow twice. It first NAMED `src/implied_close.rs` and
    `src/tokenizer.rs`, so moving the tables into `src/implied_close/` would have stopped scanning them;
    a glob over those two paths then missed `src/matcher/frame.rs` and `src/matcher/mod.rs` when the
    document-frame rules moved there — four element names decided by rules no scan was reading. It now
    reads the whole tree, which is the only version that cannot go stale as files move.
    """
    import glob

    import gen_tree_rules as G

    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    every = {os.path.realpath(p) for p in glob.glob(os.path.join(root, "src", "**", "*.rs"),
                                                    recursive=True)}
    assert every, "no Rust sources found — this test is not looking where it thinks it is"
    assert {os.path.realpath(p) for p in G.rule_sources()} == every, \
        "the universe check does not read every engine source"

    # ...and that is not a formality: names live outside the rule tables. Derived, not listed, so the
    # test keeps discriminating as code moves.
    tables = [p for p in every if "implied_close" in p or p.endswith("tokenizer.rs")]
    elsewhere = [p for p in every if p not in tables]
    only_elsewhere = (G.names_in(elsewhere) & set(G.ELEMENTS)) - G.names_in(tables)
    assert only_elsewhere, "every element name is in the rule tables — this test no longer discriminates"
    assert only_elsewhere <= G.engine_names(), f"unscanned engine names: {only_elsewhere}"

    # and the invariant can go RED: drop a frame name the engine decides rules with, and the check that
    # gates `--write`/`--check` must report it.
    full = list(G.ELEMENTS)
    try:
        G.ELEMENTS[:] = [e for e in full if e != "body"]
        assert "body" in G.check_universe(), \
            "a name the engine mentions and the universe omits must fail the universe check"
    finally:
        G.ELEMENTS[:] = full
    assert not G.check_universe(), "the real universe must be a superset"


def test_the_rule_audit_has_no_bypass_for_a_start_close_divergence():
    """A disagreeing start-close cell must fail the audit outright.

    It did not have to. Every cell was graded through an allow-list (`KNOWN_START_CLOSE_GAP` plus a
    `check_gap` that recorded a failure only for UNLISTED pairs), which held 87 entries, then zero — and
    an empty allow-list is not a safeguard, it is a documented way to make this gate green. Deriving the
    table from the oracle removed the reason for it, so the mechanism is gone; this pins that nothing
    grew back, and that the audit really does record a divergence rather than absorb it.
    """
    import audit_tree_rules as A

    assert not hasattr(A, "KNOWN_START_CLOSE_GAP"), "the start-close allow-list is back"
    assert not hasattr(A.Audit, "check_gap"), "the divergence-tolerating check is back"
    # every cell disagreeing must produce fails, not a tolerated gap. Stubbed rather than run for real:
    # the honest 142x142 sweep is `tools/audit_tree_rules.py --gate` (in `make py` and hosted CI), and
    # what this owns is the VERDICT, which no engine is needed to exercise.
    audit = A.Audit(verbose=False)
    real = A.both_multi
    A.both_multi = lambda html, sels: ([["lxml"]] * len(sels), [["engine"]] * len(sels))
    try:
        A.audit_start_close_pairs(audit)
    finally:
        A.both_multi = real
    assert audit.checked and len(audit.fails) == audit.checked, \
        f"{len(audit.fails)} of {audit.checked} disagreeing cells reported"


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
