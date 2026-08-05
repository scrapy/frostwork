"""Break one load-bearing line in `frostwork.webpoet` and ask whether any gate notices.

`tools/audit_tree_rules.py` answers "is every rule right?"; `tools/mutate_rules.py` answers the harder
question, "if a rule were WRONG, would a gate catch it?" — and its first sweep found 13% of the engine's
rule table protected by nothing at all. This is the same question for the web-poet layer, which until now
had no gate whatsoever and so was 100% unprotected by construction.

Each mutation below is a line whose correctness a real defect depended on. The detector is
`tools/diff_webpoet.py`'s own sweep, deliberately: a mutation that the differential misses is a hole in
the differential, which is exactly what needs reporting. A SURVIVOR — a mutation that leaves every gate
green — means the behaviour it controls is asserted by nothing, and the fix is a new case, not a shrug.

Mutations are applied by monkeypatching the imported module, not by editing source, so a crashed run
cannot leave a broken package behind. That also bounds what this can reach: it probes functions, so a
mutation has to be expressible as "replace this function's behaviour". Say so rather than implying the
whole module is covered — the engine's equivalent learned the same lesson when end-tag scope turned out to
be two `matches!` arms that the sweep could not see.

Run:  .venv/bin/python tools/mutate_webpoet.py
Gate: .venv/bin/python tools/mutate_webpoet.py --gate     # nonzero if any mutation survives
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TESTS = Path(__file__).resolve().parents[1] / "tests" / "test_python.py"

import diff_webpoet
from parsel.selector import SelectorList

from frostwork import page as fpage
from frostwork import webpoet as wp


# --------------------------------------------------------------------------- the mutations
def _mut_drop_set_name():
    """Stop registering the converted field with web-poet. The field then exists as a descriptor but
    web-poet's `to_item()` never lists it, so the item comes back missing that key. This is the line whose
    absence is documented in `_as_wp_field`'s docstring as load-bearing."""
    orig = wp._as_wp_field

    def patched(name, getter, wp_kwargs=None):
        f = orig(name, getter, wp_kwargs)
        f.__set_name__ = lambda owner, n: None  # registration dropped
        return f

    wp._as_wp_field = patched
    return lambda: setattr(wp, "_as_wp_field", orig)


def _mut_no_mro_merge():
    """Use only the class's OWN declarations, dropping inherited ones. This is what a naive
    `__init_subclass__` does, and it silently empties half the schema of any page object with a base."""
    orig = wp._merge_mro
    wp._merge_mro = lambda cls, attr: dict(getattr(cls, attr, {}) or {})
    return lambda: setattr(wp, "_merge_mro", orig)


def _mut_ignore_transforms():
    """Make `_shape` drop `.map()`/`.re_first()`. A transformed field then silently returns the raw value —
    a field-always-wrong foot-gun with no error."""
    orig = fpage._shape
    fpage._shape = lambda col, card, transforms=(): orig(col, card, ())
    wp._shape = fpage._shape
    return lambda: (setattr(fpage, "_shape", orig), setattr(wp, "_shape", orig))


def _mut_nodes_as_plain_list():
    """Return a plain `list` instead of a `SelectorList` from the `all=` node handoff. zyte's
    `_handle_selectorlist` gates on `SelectorList` exactly, so a plain list falls through to the
    "returned as is" path — defect 5 in a new shape, which is why `_as_nodes` says so explicitly."""
    orig = wp._as_nodes

    def patched(value, card, name="<field>", verify=False):
        # mirror the real signature exactly: a patch that raises TypeError on arity is "caught" by every
        # detector for the wrong reason, which reads as coverage nobody has (see the note on
        # `_mut_as_node_ignores_the_element_name` — this slip has now faked a result three times)
        out = orig(value, card, name, verify)
        return list(out) if isinstance(out, SelectorList) else out

    wp._as_nodes = patched
    return lambda: setattr(wp, "_as_nodes", orig)


def _mut_processors_ignore_nested_class():
    """Resolve only `out=`, ignoring the nested `Processors` class. That is the route every
    zyte-common-items base page uses, so this restores the original defect for anyone inheriting
    `ProductPage` while leaving explicit `out=` working."""
    orig = wp._processors_for

    def patched(cls, name):
        info = wp._wp_fields_dict(cls).get(name)
        out = getattr(info, "out", None) if info is not None else None
        return list(out) if out else []

    wp._processors_for = patched
    return lambda: setattr(wp, "_processors_for", orig)


def _mut_never_node():
    """Treat no field as node-valued: the raw-source string goes to the processor. This is defect 5
    exactly as it shipped."""
    orig = wp._terminals
    wp._terminals = lambda qs: ["text" for _ in qs]
    return lambda: setattr(wp, "_terminals", orig)


def _mut_always_node():
    """The opposite error: treat EVERY field as node-valued. This is the mistake the fix was designed
    around — `images_processor` takes URL strings and has no `Selector` branch, so a node reaches it and
    comes back untouched. A gate that only tested node-taking processors would miss this entirely."""
    orig = wp._terminals
    wp._terminals = lambda qs: ["outer" for _ in qs]
    return lambda: setattr(wp, "_terminals", orig)


def _mut_out_truthiness():
    """Resolve `out=` by TRUTHINESS instead of `out is not None`, which is what web-poet itself does. An
    `out=[]` — the documented way to cancel one inherited `Processors` entry, and the only way to decline one
    of the nine that zyte's `ProductPage` attaches by name — then falls through to the nested class, so the
    field is treated as processor-bearing and hands back a node where web-poet hands back raw HTML."""
    orig = wp._processors_for

    def patched(cls, name):
        info = wp._wp_fields_dict(cls).get(name)
        out = getattr(info, "out", None) if info is not None else None
        if out:  # the bug: an empty list is an ANSWER, not the absence of one
            return list(out)
        procs = getattr(cls, "Processors", None)
        if procs is not None:
            return list(getattr(procs, name, ()) or ())
        return []

    wp._processors_for = patched
    return lambda: setattr(wp, "_processors_for", orig)


def _mut_field_drops_empty_out():
    """The same defect one step earlier: `field()` forwards only the keywords it was given, so forwarding
    `out` by truthiness silently drops `out=[]` and web-poet never learns the field opted out.

    Rebound in the harness's namespace as well as the module's, the way `_mut_ignore_transforms` does for
    `_shape`: `tools/diff_webpoet.py` imports `field` by name, so patching only `frostwork.webpoet.field`
    would leave the differential calling the unmutated one."""
    orig = wp.field

    def patched(selector, *, all=False, join=None, cached=False, meta=None, out=None):
        f = orig(selector, all=all, join=join, cached=cached, meta=meta, out=out)
        if out is not None and not out:
            f.wp_kwargs.pop("out", None)  # the bug: `if out:` when forwarding
        return f

    wp.field = patched
    diff_webpoet.field = patched
    return lambda: (setattr(wp, "field", orig), setattr(diff_webpoet, "field", orig))


def _mut_node_before_map():
    """Convert to a node BEFORE running `.map()`/`.re_first()`, which inverts the documented pipeline
    (shape -> transforms -> processors). A string transform then receives a `Selector`: `AttributeError` on
    a field that works the moment its processor is removed."""
    orig = wp._make_field

    def patched(name, card, transforms, node=False, wp_kwargs=None):
        def getter(self):
            cols = self._frostwork_columns()
            col = cols[name]
            if node and wp._processors_for(type(self), name):
                value = wp._as_nodes(wp._shape(col, card, ()), card, name)
                for fn in transforms:
                    value = fn(value)
                return value
            return wp._shape(col, card, transforms)

        return wp._as_wp_field(name, getter, wp_kwargs)

    wp._make_field = patched
    return lambda: setattr(wp, "_make_field", orig)


def _mut_as_node_ignores_the_element_name():
    """Hand the processor whatever `lxml.html.fromstring` returns, without looking for the element the
    selector matched. That is faithful for a `<div>` and wrong for the document frame: `<body>` with a lone
    child comes back as the CHILD, and `<head>`/`<title>`/`<meta>`/`<link>`/`<base>` as a synthesised
    `<html>`.

    Expected to survive the DIFFERENTIAL, and that is the finding rather than a failure: the real zyte
    processors are too lenient to notice (clear-html renders the same text either way; breadcrumbs finds the
    same links in a lone child), so generated end-to-end pairs cannot discriminate it. The `unit` detector
    is what covers it, via the element-universe sweep in tests/test_python.py."""
    orig = wp._as_node

    # Mirror the real signature, `what` included. A patch that raises TypeError on arity is "caught" by
    # every detector for the wrong reason and reads as coverage nobody has — this exact slip has now faked a
    # result twice in this file, so check the signature after touching the function being patched.
    def patched(raw, what="the node handoff", verify=False):
        from lxml.html import fromstring
        from parsel import Selector

        return Selector(root=fromstring(raw))

    wp._as_node = patched
    return lambda: setattr(wp, "_as_node", orig)


def _mut_many_ignores_subfield_kwargs():
    """Accept `out=`/`cached=`/`meta=` on a `Many`/`One` subfield and drop them, instead of refusing at
    declaration. A processor written into a subfield then never runs — silently, which is the failure mode
    this integration already shipped once."""
    orig = wp.Many

    def patched(container, *, item=None, **subfields):
        for f in subfields.values():
            if isinstance(f, wp._FrostField):
                f.wp_kwargs = {}
        return orig(container, item=item, **subfields)

    wp.Many = patched
    diff_webpoet.Many = patched
    return lambda: (setattr(wp, "Many", orig), setattr(diff_webpoet, "Many", orig))


def _mut_resolve_by_merge_only():
    """Take the schema from the MERGED declarations instead of asking the MRO what each name resolves to.
    That is the shape the reconciliation replaced: it keeps a selector the page object no longer answers
    with, so the plan scans a dead column and strict validation can refuse a class over a replaced field —
    and the resurrection only shows one GENERATION down, which is why the direct-override test passed."""
    orig = wp._resolved_schema

    def patched(cls):
        return (
            dict(wp._merge_mro(cls, "_frostwork_own_specs")),
            dict(wp._merge_mro(cls, "_frostwork_own_groups")),
        )

    wp._resolved_schema = patched
    return lambda: setattr(wp, "_resolved_schema", orig)


# (name, apply, detectors that MUST catch it). The expected set is the point: "some detector noticed" lets a
# mutation the differential used to catch quietly become unit-only — the differential losing a column reads
# exactly like the unit vector doing its job. Two entries expect `unit` alone, and each says why the
# generated pairs cannot see it.
MUTATIONS = [
    ("drop-__set_name__", _mut_drop_set_name, {"diff", "unit"}),
    ("no-mro-merge", _mut_no_mro_merge, {"diff", "unit"}),
    ("resolve-by-merge-only", _mut_resolve_by_merge_only, {"unit"}),  # multi-generation: unit vectors only
    ("ignore-map-transforms", _mut_ignore_transforms, {"diff", "unit"}),
    ("nodes-as-plain-list", _mut_nodes_as_plain_list, {"diff", "unit", "surface"}),
    ("processors-ignore-nested-class", _mut_processors_ignore_nested_class, {"diff", "unit"}),
    ("never-node", _mut_never_node, {"diff", "unit"}),
    ("always-node", _mut_always_node, {"diff", "unit"}),
    ("out-truthiness", _mut_out_truthiness, {"diff", "unit"}),
    ("field-drops-empty-out", _mut_field_drops_empty_out, {"diff", "unit"}),
    ("node-before-map", _mut_node_before_map, {"diff", "unit"}),
    # the real zyte processors are too lenient to see a wrong node end-to-end (clear-html renders the same
    # text from a <title> as from the <html> around it), so the element-universe sweep is the gate
    ("as-node-ignores-element-name", _mut_as_node_ignores_the_element_name, {"unit"}),
    # a declaration-time refusal: no generated schema puts web-poet keywords on a subfield
    ("many-ignores-subfield-kwargs", _mut_many_ignores_subfield_kwargs, {"unit"}),
]

# Logic that CANNOT be expressed as a function patch, named rather than silently omitted — because
# "N mutations, 0 survivors" reads as "the module is covered", and the module is bigger than what a
# function patch can reach. The engine's sweep learned this the hard way: end-tag scope was two `matches!`
# arms, so the sweep reported the rule fully protected while the real logic was invisible to it.
#
# Everything here lives inline in `__init_subclass__`, which runs at class creation and so cannot be
# swapped out after import. Each names the gate that DOES cover it, so the entry is a pointer rather than
# an excuse.
UNREACHABLE = {
    "spec-recovery (defect 1)": (
        "the `_frostwork_spec` harvest — permanently covered by the `attrs_slots`, `inherit_attrs` and "
        "`rebuilt_metaclass` columns of make gate-webpoet, which fail outright without it"
    ),
    "strict-through-a-rebuild": (
        "`kwargs.pop('strict', cls.__dict__.get(...))` — a class keyword exists only in the `class` "
        "statement, which `@attrs.define` throws away, so the opt-out has to be carried on the class. "
        "Covered by tests/test_python.py's strict-survives-recreation sweep over the attrs variants"
    ),

    "injectable page bases": (
        "`FrostFields(ItemPage)` is a class statement, not a function: covered by the `is_injectable` "
        "assertion in tools/webpoet_surface.py (the `surface` detector) and by the andi callback-plan test"
    ),
    "group-flat reconciliation": (
        "resolving a name that is a group in one generation and a flat field in another — covered by "
        "tests/test_python.py::test_an_override_stays_dropped_in_the_next_generation, which runs both "
        "directions over three generations"
    ),
    "strict-schema-validation": (
        "`check_schema().raise_for_status()` — covered by the strict / strict=False cases in "
        "tests/test_python.py"
    ),
    "marker-owner guard (defect 4)": (
        "`_require_frost_owner`, invoked from `__set_name__` during class creation — covered by "
        "tests/test_python.py's non-Frost-owner case"
    ),
}


def _detect_diff(schemas: int, seed: int) -> tuple:
    """The differential as detector. Returns `(red, summary)`.

    Coverage failures count as red as well as divergences: the sweep now refuses to pass when a column
    stopped being generated, and a mutation that empties a column is still a mutation the gate noticed."""
    try:
        stat, by_shape, by_proc, meaningful, _examples = diff_webpoet.sweep(
            seed=seed, schemas=schemas, show=0
        )
    except Exception as exc:  # noqa: BLE001 - a mutation that breaks the harness outright is still caught
        return True, f"harness raised: {type(exc).__name__}"
    coverage = diff_webpoet.coverage_failures(by_shape, by_proc, meaningful)
    bad = stat["DIVERGE"] + stat["CRASH"] + len(coverage)
    return bad > 0, (
        f"DIVERGE={stat['DIVERGE']} CRASH={stat['CRASH']} coverage={len(coverage)} "
        f"pairs={stat['pairs']}"
    )


# The unit vectors that assert this integration's contracts, by NODE ID. Targeted rather than the whole
# suite: a mutation that reddens 90 unrelated engine tests tells you nothing about which contract it broke,
# and a run per mutant should cost a second, not ten. A renamed test makes pytest exit with a collection
# error, which the baseline check below reports rather than silently reducing the detector's reach.
UNIT_NODES = (
    "test_processor_on_a_bare_element_field_receives_a_node_not_raw_html",
    "test_all_true_node_handoff_produces_a_selectorlist_not_a_plain_list",
    "test_processor_on_a_scalar_terminal_field_still_gets_strings",
    "test_a_real_zyte_product_page_composes_and_every_processor_fires",
    "test_node_handoff_reparses_the_subtree_not_the_document",
    "test_node_handoff_returns_the_matched_element_for_every_element_name",
    "test_frame_element_node_handoff_end_to_end",
    "test_out_empty_list_cancels_an_inherited_processor",
    "test_processor_resolution_matches_web_poets_own",
    "test_map_on_a_processor_bearing_node_field_runs_on_the_source",
    "test_a_transform_that_breaks_the_node_handoff_fails_closed",
    "test_insignificant_whitespace_around_a_transformed_node_is_allowed",
    "test_out_processors_run_after_map_transforms",
    "test_out_on_a_bare_element_field_also_gets_a_node",
    "test_frostpage_field_map_and_re_first",
    "test_attrs_variants_and_groups_survive_class_recreation",
    "test_strict_false_survives_class_recreation",
    "test_a_manual_override_drops_the_inherited_selector",
    "test_an_override_stays_dropped_in_the_next_generation",
    "test_a_manual_field_mixin_before_the_frost_base_wins",
    "test_an_override_also_clears_a_stale_strict_failure",
    "test_many_rejects_web_poet_keywords_on_a_subfield",
    "test_every_shipped_base_is_injectable",
    "test_frostpage_is_one_pass",
    "test_frost_schema_includes_inherited_and_groups",
)


class _FailedNodes:
    """pytest plugin: collect the node ids that failed, so the report names the CONTRACT a mutation broke
    rather than only that something went red."""

    def __init__(self):
        self.failed = []

    def pytest_runtest_logreport(self, report):
        if report.failed and report.when in ("call", "setup"):
            self.failed.append(report.nodeid.rsplit("::", 1)[-1])


def _detect_unit(schemas: int, seed: int) -> tuple:
    """The targeted unit vectors as detector, run IN-PROCESS so they see the monkeypatched module.

    The differential is not the only gate, and treating it as the only one mis-reports two things: a
    mutation it misses looks like dead code (a unit vector may cover it), and a contract the generated pages
    genuinely cannot discriminate looks like a hole in the generator. Same idea as `tools/mutate_rules.py`'s
    `--detectors`."""
    import contextlib
    import io

    import pytest as _pytest

    collector = _FailedNodes()
    buf = io.StringIO()
    args = ["-q", "--no-header", "-p", "no:cacheprovider", "-p", "no:randomly"]
    args += [f"{TESTS}::{node}" for node in UNIT_NODES]
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = _pytest.main(args, plugins=[collector])
    if code == 4:  # usage/collection error — a node id here no longer exists
        return True, f"pytest could not collect (exit 4); check UNIT_NODES: {buf.getvalue()[-200:]}"
    shown = ", ".join(sorted(set(collector.failed))[:3]) or "-"
    return code != 0, f"exit={code} failed={len(set(collector.failed))} [{shown}]"


def _detect_surface(schemas: int, seed: int) -> tuple:
    """The derived upstream-surface snapshot as detector — it asserts properties of the shipped bases
    (every one `is_injectable`, the handoff really produces `Selector`/`SelectorList`), which no value
    comparison can see."""
    try:
        import webpoet_surface

        webpoet_surface.render()
    except SystemExit as exc:
        return True, f"surface: {str(exc)[:60]}"
    except Exception as exc:  # noqa: BLE001
        return True, f"surface raised: {type(exc).__name__}"
    return False, "surface: clean"


DETECTORS = {"diff": _detect_diff, "unit": _detect_unit, "surface": _detect_surface}


def _run_detectors(names, schemas: int, seed: int) -> tuple:
    """`(caught_by, summaries)` — every named detector's verdict, not just the first one to go red, so the
    report can say which gate covers each mutation."""
    caught, summaries = [], {}
    for name in names:
        red, summary = DETECTORS[name](schemas, seed)
        summaries[name] = summary
        if red:
            caught.append(name)
    return caught, summaries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schemas", type=int, default=8, help="schemas per shape per mutant (sampled)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--detectors", default="diff,unit,surface",
                    help=f"comma-separated subset of {sorted(DETECTORS)}")
    ap.add_argument("--gate", action="store_true", help="exit nonzero if any mutation survives")
    args = ap.parse_args()

    names = [n.strip() for n in args.detectors.split(",") if n.strip()]
    unknown = [n for n in names if n not in DETECTORS]
    if unknown:
        raise SystemExit(f"mutate-webpoet: unknown detector(s) {unknown}; known: {sorted(DETECTORS)}")

    baseline_caught, baseline = _run_detectors(names, args.schemas, args.seed)
    print(f"  baseline (unmutated): {'; '.join(f'{k}: {v}' for k, v in baseline.items())}")
    if baseline_caught:
        raise SystemExit(
            "mutate-webpoet: the UNMUTATED build already fails "
            f"{baseline_caught}, so no result here means anything. Fix the gate first: {baseline}"
        )

    print(f"\n  {'mutation':34} {'caught by':18} {'expected':18} detector detail")
    survivors, wrong_provenance = [], []
    for name, apply_mut, expected in MUTATIONS:
        undo = apply_mut()
        try:
            caught, summaries = _run_detectors(names, args.schemas, args.seed)
        finally:
            undo()
        # only the detectors actually being run can be expected to catch anything
        want = expected & set(names)
        missing = want - set(caught)
        label = "+".join(sorted(caught)) if caught else "NOTHING"
        print(f"  {name:34} {label:18} {'+'.join(sorted(want)) or '-':18} {summaries.get('unit', '')}")
        if not caught:
            survivors.append(name)
        elif missing:
            wrong_provenance.append((name, sorted(missing), sorted(caught)))

    # prove the teardown restored the module: a survivor list is meaningless if the last mutation stuck
    after_caught, after = _run_detectors(names, args.schemas, args.seed)
    if after_caught:
        raise SystemExit(f"mutate-webpoet: teardown failed to restore the module ({after})")

    print(f"\n  not expressible as a function patch (covered by unit vectors instead):")
    for name, why in UNREACHABLE.items():
        print(f"    {name}: {why}")

    print(
        f"\n  MUTATIONS: {len(MUTATIONS)} applied, {len(MUTATIONS) - len(survivors)} caught, "
        f"{len(survivors)} SURVIVED"
    )
    print(f"  (detectors: {names}; sampled: {args.schemas} schemas per shape per mutant, seed {args.seed})")
    unit_only = [n for n, _f, exp in MUTATIONS if exp == {"unit"}]
    print(f"  expected unit-only (generated pairs cannot discriminate these): {unit_only}")
    if wrong_provenance:
        print("\n  PROVENANCE CHANGED -> a detector that used to catch these no longer does. The mutation is")
        print("  still caught, so a survivor count cannot see this; it means a gate lost a column:")
        for name, missing, caught in wrong_provenance:
            print(f"    {name}: expected {'+'.join(missing)} to catch it, only {'+'.join(caught)} did")
    if survivors:
        print(f"\n  SURVIVORS -> the behaviour these control is asserted by nothing: {survivors}")
    if args.gate and (survivors or wrong_provenance):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
