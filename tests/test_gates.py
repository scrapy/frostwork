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
    import decoder_sweep as D

    # ---- the ENUMERATION. Its failure mode is narrowing, so pin the boundaries, not a count.
    cand = D.candidates()
    assert len(cand) == len(D.LEAD) * len(D.TRAIL) == 24066
    assert min(s[0] for s in cand) == 0x81 and max(s[0] for s in cand) == 0xFE
    assert min(s[1] for s in cand) == 0x40 and max(s[1] for s in cand) == 0xFE
    # no candidate can carry a byte that would break the `<p class="c">…</p>` wrapper, which is a
    # property of the ranges — the filter that used to say so was dead code below 0x40
    assert not [s for s in cand if any(b in D.MARKUP_BYTES for b in s)]
    # it must not be a function of any PYTHON CODEC: filtering on what `euc_jp` calls assigned is exactly
    # how the WHATWG-only class stayed invisible. Both witnesses have to be in it.
    assert b"\xad\xa1" in cand, "AD A1 (the `①` euc_jp has no mapping for) must be swept"
    assert b"\xa1\xc1" in cand, "A1 C1 (the crawled wave dash) must be swept"
    assert D.LABELS == ("big5", "euc-jp", "gb18030", "euc-kr", "shift_jis"), \
        "a label with no row is not evidence of parity"

    # ---- the CLASSIFIER. Every class must be reachable and none may swallow another. (The private-use
    # characters are written as escapes on purpose: a literal one is invisible in a source file.)
    pua, fffd = "\ue794", "\ufffd"
    assert D.classify("x", "x") == "agree"
    assert D.classify("\uff5e", "\u301c") == "real"      # index divergence: the wave dash
    assert D.classify(fffd, pua) == "pua"               # Parsel private-use, WHATWG nothing at all
    assert D.classify(fffd + "\uff89", pua + "\uff89") == "pua"  # ...position-wise, mid-string
    assert D.classify("\u2460", fffd) == "whatwg_only"   # AD A1's class: WHATWG has it, Python does not
    assert D.classify(fffd, fffd * 2) == "replacement_shape"
    # a real character opposite a PUA one is NOT the pua class — that would hide a mapping difference
    assert D.classify("\u3000", pua) == "real"

    # ---- the VERDICT. Each class must be able to go red, in both directions.
    label = "euc-jp"
    clean = dict(D.INDEX_DIVERGENCE[label])
    counts = D.expected_counts(label)
    assert D.verify(label, clean, counts) == [], "the recorded state must be clean"
    assert D.verify(label, {**clean, b"\xff\xff": ("a", "b")}, counts), "a NEW pair must fail"
    assert D.verify(label, {}, counts), "a pair that stops diverging must fail as stale"
    assert D.verify(label, {**clean, b"\xa1\xc1": ("x", "y")}, counts), \
        "a pair that maps differently must fail"
    # the three BULK classes are gated by count, so each needs a label where it is actually populated —
    # zeroing a class that is already zero proves nothing, and every class must be reachable somewhere
    for kind in D.BULK:
        where = [lab for lab in D.LABELS if D.expected_counts(lab)[kind]]
        assert where, f"no label populates the {kind} class — it is gated by a count of nothing"
        for lab in where:
            want = D.expected_counts(lab)
            assert D.verify(lab, D.INDEX_DIVERGENCE[lab], want) == [], lab
            assert D.verify(lab, D.INDEX_DIVERGENCE[lab], {**want, kind: want[kind] + 1}), \
                f"a drift in {lab}'s {kind} count must fail"
            assert D.verify(lab, D.INDEX_DIVERGENCE[lab], {**want, kind: 0}), \
                f"{lab}'s {kind} class emptying must fail too — silence is not parity"

    # ---- and the two real-page witnesses, both sides, so a row cannot rot into a silent agreement. The
    # AD A1 row is the load-bearing one: Parsel gets U+FFFD PER BYTE for a character `euc_jp` has no
    # mapping for, which is why enumerating over "what Python calls assigned" could never have seen it.
    for seq, theirs, mine in ((b"\xa1\xc1", "〜", "～"), (b"\xad\xa1", "��", "①")):
        doc = (b'<html><head><meta charset="euc-jp"></head><body><p class="c">'
               + seq + b"</p></body></html>")
        _, txt = html_to_unicode(None, doc, auto_detect_fun=None, default_encoding="utf8")
        assert parsel.Selector(text=txt).css("p.c::text").getall() == [theirs], \
            f"Parsel now decodes euc-jp {seq.hex()} differently — that row is stale"
        assert frostwork.extract(doc, ["p.c::text"], strict=False)[0] == [mine], \
            f"euc-jp {seq.hex()} must follow the WHATWG index, which is what browsers show"


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


def test_the_sequence_gate_fails_on_a_tree_difference():
    """`gate-seq` compares whole TREES over enumerated token sequences, and its failure mode is grading
    nothing: an alphabet that generates no documents, or a fingerprint that cannot see a reshaped tree,
    both read as PASS. Seed a tree difference into the comparison and require the verdict to go red.

    The fingerprint half is the part worth pinning. Three of the bugs this gate found move an element
    without moving any `::text` value, so a comparison of a few columns would have passed on all three —
    which is what every other differential here does.
    """
    import seq_sweep

    # the alphabet must cover the shapes the sweep exists for, or "0 differing shapes" means nothing
    assert len(seq_sweep.ALPHABET) > 15
    for token in ("<html{}>", "</html>", "<head{}>", "<body{}>", "</body>", "</%>", "</>", "x"):
        assert token in seq_sweep.ALPHABET, f"{token} is the shape of a real bug and is not enumerated"

    frostwork = pytest.importorskip("frostwork")

    # a real document, its real fingerprint, and a tree RESHAPED the way the bugs this gate found were:
    # the span stops being the div's child while every text node stays exactly where it was.
    doc, ids = seq_sweep.build(("<div{}>", "<span{}>", "x"))
    sels = seq_sweep.fingerprint_selectors(ids)
    mine = frostwork.extract(doc, sels, "utf-8")
    assert seq_sweep.compare(sels, mine, mine) is None, "identical answers are not a difference"

    nested = sels.index('//*[@id="0"]//*/@id')
    assert mine[nested] == ["1"], f"the fingerprint must read placement: {doc!r} -> {mine[nested]}"
    reshaped = [list(c) for c in mine]
    reshaped[nested] = []
    diff = seq_sweep.compare(sels, mine, reshaped)
    assert diff and diff[0] == sels[nested], "a moved element must be a difference"
    # ...and it is invisible to the text columns, which is why this gate compares the whole tree
    texts = [i for i, s in enumerate(sels) if s.endswith("/text()")]
    assert texts and all(mine[i] == reshaped[i] for i in texts)


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


# ------------------------------------------------- web-poet differential (tools/diff_webpoet.py)
def test_webpoet_gate_flags_a_processor_receiving_a_string():
    """The defect this gate exists for: a zyte processor gated on `isinstance(value, Selector|HtmlElement)`
    receives Frostwork's `str`, matches nothing, and returns it UNCHANGED. No exception is ever raised, so
    the only thing that can catch it is a value comparison against parsel — and only if the comparison
    refuses to make a raw-source allowance on a PROCESSED field. Seed exactly that shape."""
    from diff_webpoet import field_verdict

    raw = '<nav class="crumbs"><a href="/c0">Cat 0</a></nav>'
    processed = [{"name": "Cat 0", "url": "http://example.com/c0"}]
    # a processed field must never be excused: same non-whitespace text, still a DIVERGE
    assert field_verdict(raw, processed, ".crumbs", True) == "DIVERGE"
    # ...and the str-passthrough of a str-tolerant processor that INVENTED a value from raw HTML
    assert field_verdict(f"Brand(name={raw!r})", "Brand(name='Acme')", ".brand", True) == "DIVERGE"
    # the same raw-source string on a NON-processor bare-element field is the documented divergence and
    # must still be excused, or the gate would go red on shipped, correct behaviour
    assert field_verdict(raw, raw.replace("&gt;", ">"), ".crumbs", False) in ("AGREE", "WS")


def test_webpoet_gate_flags_a_field_that_vanished_from_the_item():
    """Two of the five defects delete fields rather than corrupt them: `@attrs.define` drops the class's own
    fields from the plan, and `field()` on web-poet's `BrowserPage` converts no markers at all so
    `to_item()` returns `{}`. A per-field sweep over the keys the engine reports cannot see either. The
    comparison must run over the UNION of both items' keys."""
    from diff_webpoet import item_verdicts

    schema = {"fields": [("name", "h1::text", None), ("price", ".price::text", None)], "cards": {}}
    # the whole item went missing (the BrowserPage silent-{} shape)
    v = item_verdicts({}, {"name": "Widget", "price": "$9"}, schema)
    assert [k for k, verdict, _g, _w in v if verdict == "DIVERGE"] == ["name", "price"]
    # one field went missing (the attrs own-fields-drop shape, when a base supplied the rest)
    v = item_verdicts({"name": "Widget"}, {"name": "Widget", "price": "$9"}, schema)
    assert [(k, verdict) for k, verdict, _g, _w in v] == [("name", "AGREE"), ("price", "DIVERGE")]
    # and an extra key on OUR side is a divergence too, not a silently ignored bonus
    v = item_verdicts({"name": "Widget", "ghost": "x"}, {"name": "Widget"}, schema)
    assert [k for k, verdict, _g, _w in v if verdict == "DIVERGE"] == ["ghost"]


def test_webpoet_gate_would_not_pass_vacuously():
    """A processor that returns None on both sides proves nothing, so the gate prints how many pairs carried
    a non-empty expected value. Guard the predicate behind that number: if it ever counted `None`/`[]` as
    meaningful, a run with no parsable content would read as full coverage."""
    from diff_webpoet import _expected_is_meaningful

    assert not _expected_is_meaningful(None)
    assert not _expected_is_meaningful([])
    assert not _expected_is_meaningful("")
    assert _expected_is_meaningful("Acme")
    assert _expected_is_meaningful([{"name": "Cat 0"}])
    assert _expected_is_meaningful(0.0)  # a real extracted rating of 0 is information, not absence


def test_webpoet_gate_fails_when_a_column_went_quiet():
    """A differential can be green because everything agreed or because nothing ran, and only the run's own
    output showed the difference. Three ways that hid here, all of them real:

    * a class shape whose oracle stopped building produced nothing but ORACLE-SKIPs and still exited 0;
    * a processor row whose expected value was empty on every page could not go red;
    * a COMBINATION the generator never emitted (`out=[]`, a `.map()` beside a processor) was reported by
      nothing at all — which is exactly how the `all=True` node-processor branch survived a whole
      differential before the mutation sweep found it.

    So the coverage check is part of the gate's exit condition, and this seeds each of its failures."""
    from diff_webpoet import (
        EXPECTED_ORACLE_SKIP,
        SHAPES,
        coverage_failures,
        expected_cells,
        required_columns,
    )

    def clean():
        """A by_shape/by_proc/meaningful triple that passes, as the baseline to break."""
        by_shape = {cell: {"AGREE": 10, "WS": 0, "DIVERGE": 0, "CRASH": 0} for cell in expected_cells()}
        by_shape.update(
            {f"{s}/{k}": {"ORACLE-SKIP": 10} for s in EXPECTED_ORACLE_SKIP for k in ("http", "browser")}
        )
        buckets = []
        for marker, _why, exact in required_columns():
            # an exact requirement is its own bucket; a marker is a suffix on some processor's bucket
            buckets.append(marker if exact else f"breadcrumbs {marker}")
        by_proc = {b: {"AGREE": 5} for b in buckets}
        return by_shape, by_proc, {b: 5 for b in by_proc}

    by_shape, by_proc, meaningful = clean()
    witnessed = dict(meaningful)  # every processor column showed its processor changing a value
    assert coverage_failures(by_shape, by_proc, meaningful, witnessed) == []

    # THE seeded regression for a symmetric differential: remove the processors from both sides and every
    # value still agrees, every bucket still has a label, and every count stays non-zero — only the evidence
    # that a processor ran disappears.
    no_evidence = {b: 0 for b in witnessed}
    failures = coverage_failures(by_shape, by_proc, meaningful, no_evidence)
    assert any("never showed a processor CHANGING" in f for f in failures), failures
    assert sum("never showed a processor CHANGING" in f for f in failures) >= 2, failures

    # 1. a CELL that graded nothing. Per cell, not per shape: the two response inputs go through different
    #    bases, so every BrowserResponse pair could become an ORACLE-SKIP while the HTTP half kept the
    #    shape's total non-zero — a whole input type untested, reported as a pass.
    for cell in ("plain/http", "plain/browser", "zyte_productpage/http", "contract/http", "contract/browser"):
        broken = dict(by_shape)
        broken[cell] = {"ORACLE-SKIP": 10}
        failures = coverage_failures(broken, by_proc, meaningful, witnessed)
        assert any(f"cell {cell!r} graded 0 pairs" in f for f in failures), (cell, failures)

    # 2. a cell that grades SOME pairs and skips others — a partial hole, which the totals hide
    broken = dict(by_shape)
    broken["plain/browser"] = {"AGREE": 4, "ORACLE-SKIP": 6}
    failures = coverage_failures(broken, by_proc, meaningful, witnessed)
    assert any("skipped 6" in f for f in failures), failures

    # 3. a shape listed as an expected skip that now builds — a stale expectation is a rotting gate
    broken = dict(by_shape)
    broken["attrs_frozen/http"] = {"AGREE": 3}
    failures = coverage_failures(broken, by_proc, meaningful, witnessed)
    assert any("EXPECTED_ORACLE_SKIP" in f and "attrs_frozen" in f for f in failures), failures

    # ...and one that is not even PROBED: an expectation no run exercises describes a shape that stopped
    # being built, which reads exactly like one being skipped for the documented reason
    broken = {k: v for k, v in by_shape.items() if k != "dataclass/browser"}
    failures = coverage_failures(broken, by_proc, meaningful, witnessed)
    assert any("expected to ORACLE-SKIP" in f and "dataclass/browser" in f for f in failures), failures

    # 4. a processor column whose expected value is empty everywhere
    thin = dict(meaningful)
    thin["breadcrumbs"] = 0
    failures = coverage_failures(by_shape, by_proc, thin, witnessed)
    assert any("never carried a non-empty expected value" in f for f in failures), failures

    # 5. every required column, dropped one at a time — including each processor in the shared registry, so
    #    a whole processor family leaving the sweep is a failure rather than a smaller table
    for marker, _why, exact in required_columns():
        missing = {
            b: c for b, c in by_proc.items() if (b != marker if exact else marker not in b)
        }
        failures = coverage_failures(by_shape, missing, {b: 5 for b in missing}, {b: 5 for b in missing})
        assert any(repr(marker) in f for f in failures), (marker, failures)
    assert len(SHAPES) > 1  # the sweep still has a shape axis to gate


def test_every_required_column_comes_from_the_fixed_schemas_not_from_a_lucky_seed():
    """The coverage check is only as deterministic as the schemas that feed it.

    A variant REPLACES the column it varies, so one schema declaring every processor "and its `out=[]` and
    `.map()` forms" left the plain forms to the random sweep — the gate passed on seed 0 and failed on
    seed 1. This runs the contract pass with the generator DISABLED, so a required column that still needs
    it raises rather than quietly succeeding."""
    import diff_webpoet

    def no_random(*_a, **_kw):
        raise AssertionError("the contract pass must not generate a random schema")

    original = diff_webpoet.gen_schema
    diff_webpoet.gen_schema = no_random
    try:
        stat, _by_shape, by_proc, meaningful, witnessed, _ex = diff_webpoet.sweep(
            contract_only=True, show=0
        )
    finally:
        diff_webpoet.gen_schema = original

    assert stat["DIVERGE"] == 0 and stat["CRASH"] == 0, stat
    for marker, why, exact in diff_webpoet.required_columns():
        present = marker in by_proc if exact else any(marker in bucket for bucket in by_proc)
        assert present, f"{marker!r} ({why}) is not produced by the fixed schemas: {sorted(by_proc)}"
        # ...and it has to be a column that could go RED: a bucket with no non-empty expected value, or (for
        # a processor column) no evidence its processor changed anything, proves nothing by being present
        if exact:
            assert meaningful.get(marker, 0), f"{marker!r} carried no non-empty expected value"
    for case in webpoet_cases_module().cases_for("generated"):
        assert witnessed.get(case.field_name, 0), (
            f"the fixed schemas never showed {case.processor} CHANGING its field's value"
        )


def webpoet_cases_module():
    import webpoet_cases

    return webpoet_cases


def test_the_raw_source_allowance_excuses_serialization_and_nothing_else():
    """What "the raw-source divergence is acceptable" is allowed to mean.

    The allowance compared non-whitespace TEXT, which is far too weak: `<p class=a>same</p>` and
    `<section id=wrong>same</section>` have identical text, so a different tag, lost attributes and a
    reshaped subtree were all excused — on the one column where the engine's outer HTML is the value. The
    comparison is now a structural signature, so quoting/entities/implied end tags still pass and structure
    does not."""
    from diff_webpoet import field_verdict

    # serialization differences: attribute quoting, entity SPELLING (`&gt;` vs a literal `>`, which parse to
    # the same character), an implied end tag
    assert field_verdict("<p class=a>x &amp; y</p>", '<p class="a">x &amp; y</p>', ".a", False) == "AGREE"
    assert field_verdict("<p>a &gt; b</p>", "<p>a > b</p>", "p", False) == "AGREE"
    assert field_verdict("<ul><li>a<li>b</ul>", "<ul><li>a</li><li>b</li></ul>", "ul", False) == "AGREE"

    # ...and everything a processor would actually read differently
    assert field_verdict("<p class=a>same</p>", "<section id=wrong>same</section>", ".a", False) == "DIVERGE"
    assert field_verdict('<p class=a>x</p>', '<p class=a id=extra>x</p>', ".a", False) == "DIVERGE"
    assert field_verdict("<p><b>x</b></p>", "<p>x</p>", "p", False) == "DIVERGE"
    # ...including comments and the text AFTER them, which a signature built from elements alone drops
    assert field_verdict("<p>x<!--a-->y</p>", "<p>x<!--b-->y</p>", "p", False) == "DIVERGE"
    assert field_verdict("<p>x<!--a-->y</p>", "<p>x<!--a-->z</p>", "p", False) == "DIVERGE"
    assert field_verdict("<p>x<!--a-->y</p>", "<p>x<!--a-->y</p>", "p", False) == "AGREE"

    # the same rules per item for an `all=True` bare-element field — a shape that did not exist until
    # `out=[]` made an all=True node field processor-free
    mine = ["<p class=a>one</p>", "<p class=a>two</p>"]
    reflow = ['<p class="a">one</p>', '<p class="a">two</p>']
    assert field_verdict(mine, reflow, ".a", False) == "AGREE"
    assert field_verdict(mine[:1], reflow, ".a", False) == "DIVERGE"
    assert field_verdict(["<p class=a>one</p>", "<div>two</div>"], reflow, ".a", False) == "DIVERGE"
    # ...and the allowance never applies to a PROCESSED field, whatever the shape
    assert field_verdict(mine, reflow, ".a", True) == "DIVERGE"


def test_the_processor_registry_covers_the_installed_library():
    """The shared registry is the universe both web-poet gates read, so a processor upstream that it does
    not classify is a hole in BOTH. Two of its decline reasons were factually wrong about upstream once
    (`description_processor` "reads a side channel" — it writes one; `gtin_processor` "takes a GTIN
    argument" — it is a plain `(value, page)`), which is what a hand-written universe costs."""
    import webpoet_cases

    assert webpoet_cases.coverage_gaps() == []

    # a case that claims no gate would leave the differential smaller with every check still green, because
    # the required-column set is derived from these tuples
    for bad_gates in ((), ("nonesuch",), ("generated", "typo")):
        with pytest.raises(ValueError, match="non-empty subset"):
            webpoet_cases.ProcessorCase("breadcrumbs_processor", "breadcrumbs", ".c", "node", bad_gates)
    with pytest.raises(ValueError, match="unknown input_kind"):
        webpoet_cases.ProcessorCase("breadcrumbs_processor", "breadcrumbs", ".c", "nodes", ("generated",))

    # ...and the dangerous direction: a mapping UPSTREAM adds that no case drives
    original = webpoet_cases.product_page_processors

    def with_wiring(**changes):
        return lambda: {**original(), **changes}

    try:
        webpoet_cases.product_page_processors = with_wiring(newField=["breadcrumbs_processor"])
        assert any("newField" in g for g in webpoet_cases.coverage_gaps()), "an added field must be a gap"
        # A DECLINED field is declined for a specific wiring, and storing only the reason degraded the
        # check to "it is wired to something": re-pointing it, appending to it, or emptying it all passed
        # while the field went on doing something this registry no longer describes.
        for wiring in (["brand_processor"], ["metadata_processor", "brand_processor"], []):
            webpoet_cases.product_page_processors = with_wiring(metadata=wiring)
            gaps = webpoet_cases.coverage_gaps()
            assert any("metadata" in g and "not" in g for g in gaps), (wiring, gaps)
    finally:
        webpoet_cases.product_page_processors = original
    # every covered case must name a real callable and a field name zyte actually wires, where it claims to
    wired = webpoet_cases.product_page_processors()
    for case in webpoet_cases.CASES:
        assert callable(case.callable), case
        if "productpage" in case.gates:
            assert case.processor in wired.get(case.field_name, []), (
                f"{case.processor} is claimed to arrive through ProductPage.Processors under "
                f"{case.field_name!r}, but zyte wires {wired.get(case.field_name)} there"
            )


def test_the_mutation_sweep_refuses_a_patch_that_changes_a_default():
    """A mutant must differ from the real function in BEHAVIOUR and nothing else.

    The guard compared whether a default existed, not what it WAS, so a patch could flip `all=False` to
    `all=True` and pass as the same signature — a second, unmeasured mutation inside the one being
    measured, caught by everything for the wrong reason."""
    import mutate_webpoet

    def real(selector, *, all=False, join=None):
        return selector

    def flipped_default(selector, *, all=True, join=None):
        return selector

    def lost_default(selector, *, all, join=None):
        return selector

    def renamed_kind(selector, all=False, join=None):
        return selector

    mutate_webpoet._same_signature(real, lambda selector, *, all=False, join=None: selector, "same")
    for bad, why in (
        (flipped_default, "a flipped default"),
        (lost_default, "a lost default"),
        (renamed_kind, "a keyword-only turned positional"),
    ):
        with pytest.raises(SystemExit, match="patch takes"):
            mutate_webpoet._same_signature(real, bad, why)


# --------------------------------------------- web-poet surface snapshot (tools/webpoet_surface.py)
def test_the_surface_gate_fails_on_an_unclassified_upstream_name():
    """The gate exists because five defects were hand-written lists that omitted something. So the failure
    mode it must catch is a name that exists UPSTREAM and in neither the covered nor the declined list —
    if that merely warned, or was silently ignored, the next omission would ship exactly like the last
    five did."""
    import pytest as _pytest
    import webpoet_surface

    known = {"WebPage": ("FrostPage", None)}
    # a name upstream added and nobody classified
    with _pytest.raises(SystemExit) as e:
        webpoet_surface._gate("a page base class", ["WebPage", "NewShinyPage"], known)
    assert "NewShinyPage" in str(e.value)
    assert "Do not delete the name" in str(e.value)

    # ...and the inverse: something we still target that upstream has removed
    with _pytest.raises(SystemExit) as e:
        webpoet_surface._gate("a page base class", [], known)
    assert "no longer exists upstream" in str(e.value) and "WebPage" in str(e.value)

    # a fully classified surface passes and returns its rows in upstream order
    assert webpoet_surface._gate("a page base class", ["WebPage"], known) == [
        ("WebPage", ("FrostPage", None))
    ]


def test_the_surface_gate_checks_the_node_handoff_really_produces_nodes():
    """The value-type table is a CLAIM about what the integration can hand a processor. Rendering asserts
    it against the real functions, so the table cannot go on saying `parsel.Selector` after a refactor
    that quietly returns something else — which is the exact shape of defect 5."""
    import webpoet_surface
    from parsel import Selector
    from parsel.selector import SelectorList

    from frostwork.webpoet import _as_node, _as_nodes

    assert isinstance(_as_node("<p>x</p>"), Selector)
    assert isinstance(_as_nodes(["<p>x</p>"], ("all", None)), SelectorList)
    # and the renderer is what enforces it, so it must run clean on the installed libraries
    assert "Page / extractor base classes" in webpoet_surface.render()


# ------------------------------------------- competitive benchmark (tools/bench_engines.py)
# `bench_engines.py` is not a gate, but it publishes a table, and the only thing standing between that
# table and a comparison of engines computing DIFFERENT answers is its parity filter. That filter is
# untested code with a published number attached, which is the exact shape of every gate above.
def test_the_competitive_benchmark_drops_a_column_an_engine_gets_wrong():
    """An engine that returns the wrong value must LOSE the column, not keep it and be timed on work the
    others are doing correctly. Faster-because-wrong is the failure mode a speed table cannot show."""
    bench_engines = pytest.importorskip("bench_engines")

    keys = ["frostwork", "parsel", "selectolax"]
    ok = {k: [True, True] for k in keys}
    clean = {k: ["AGREE", "AGREE"] for k in keys}

    assert bench_engines.workload_columns(2, ok, keys, clean) == [0, 1]
    # whitespace-only differences stay in — that is the project's parity bar everywhere else
    ws = {**clean, "selectolax": ["WS", "AGREE"]}
    assert bench_engines.workload_columns(2, ok, keys, ws) == [0, 1]
    # a real divergence takes the column away from EVERY engine, not just the one that got it wrong
    bad = {**clean, "selectolax": ["DIVERGE", "AGREE"]}
    assert bench_engines.workload_columns(2, ok, keys, bad) == [1]
    # and so does a column an engine cannot express at all
    cant = {**ok, "selectolax": [False, True]}
    assert bench_engines.workload_columns(2, cant, keys, clean) == [1]


def test_the_competitive_benchmark_refuses_to_time_engines_on_different_work():
    """The scopes exist so every row is measured on the same columns. If an adapter quietly drops a
    selector it cannot run, its row gets a cheaper workload and the table silently stops being a
    comparison — so the invariant is asserted at runtime, not assumed."""
    bench_engines = pytest.importorskip("bench_engines")

    assert bench_engines.assert_same_work("W-common", {"a": [0, 1, 2], "b": [0, 1, 2]})
    with pytest.raises(AssertionError, match="different columns"):
        bench_engines.assert_same_work("W-common", {"a": [0, 1, 2], "b": [0, 1]})


def test_the_competitive_benchmark_names_an_engine_it_could_not_run():
    """A competitor that is not installed must be REPORTED, not skipped. "0 engines skipped" is how an
    absent competitor should never look — it reads as "we measured the field" when a row is missing."""
    bench_engines = pytest.importorskip("bench_engines")

    class Absent(bench_engines.Engine):
        key, label = "absent", "absent-engine"

        def unavailable(self):
            return "not installed"

    assert bench_engines.unavailable_report([Absent()]) == [("absent-engine", "not installed")]
    # the installed registry reports itself honestly too — every listed engine either runs or says why
    for e in bench_engines.ENGINES:
        why = e.unavailable()
        assert why is None or isinstance(why, str) and why


def test_the_competitive_benchmark_translates_parsel_text_pseudos_faithfully():
    """`X::text` is X's CHILD text nodes, `X ::text` and `X *::text` are its descendants. Three spellings
    that differ by a space, and an adapter that reads them off the string instead of off parsel's own
    translator returns a subset — which looks like a fast engine rather than a broken one."""
    bench_engines = pytest.importorskip("bench_engines")
    CHILD, DESC = bench_engines.CHILD_TEXT, bench_engines.DESC_TEXT

    assert bench_engines.css_plan("p::text") == ("p", CHILD, None)
    assert bench_engines.css_plan("p ::text") == ("p", DESC, None)
    assert bench_engines.css_plan("p *::text") == ("p", DESC, None)
    assert bench_engines.css_plan("a::attr(href)") == ("a", "attr", "href")
    # ...and the same trap on the attribute side: `X ::attr(a)` reads the attribute off X AND its
    # descendants, which CSS can only name as two selectors
    assert bench_engines.css_plan("div ::attr(id)") == ("div, div *", "attr", "id")
    assert bench_engines.css_plan("div *::attr(id)") == ("div, div *", "attr", "id")
    assert bench_engines.css_plan("div.card") == ("div.card", "node", None)
    # a comma list is only merged when every branch agrees on the terminal; otherwise the CSS-only
    # engines are told they cannot express it, with the reason, rather than guessing at document order
    assert bench_engines.css_plan("h1::text, h2::text") == ("h1, h2", CHILD, None)
    assert bench_engines.css_translate("h1::text, img::attr(src)")[1].startswith("comma list")
    assert bench_engines.css_translate("//div/text()")[1].startswith("xpath")
