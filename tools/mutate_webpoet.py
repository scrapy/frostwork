"""Break one load-bearing line in `frostwork.webpoet` and ask whether the gates notice.

`tools/diff_webpoet.py` asks "is the behaviour right?"; this asks the harder question, "if it were WRONG,
would anything go red?" — the only check here that finds blind spots without a human guessing where they are.

Each mutation is applied by monkeypatching the imported module (so a crashed run cannot leave a broken package
behind) and graded by three DETECTORS:

  * `diff`    — the differential sweep, including its coverage failures
  * `unit`    — tests marked `webpoet_contract`, run in-process so they see the patch
  * `surface` — the derived upstream-surface snapshot, which asserts properties no value comparison can see

Every mutation DECLARES which detectors must catch it, and the report fails when the caught set differs —
"something noticed" would let a mutation the differential is declared to catch pass as unit-only, and a differential
losing a column looks exactly like a unit vector doing its job. Several are expected to be caught by `unit`
alone; that is a finding rather than a gap, and the entry says why (the real zyte processors are too lenient
to discriminate a wrong node end-to-end; a multi-generation override needs three classes no schema generates).

Two things bound what this can reach. It patches functions, so non-patchable class-definition behavior is
listed in `UNREACHABLE` with its detector. A patch must also mirror the real signature (`_patch` checks),
otherwise an arity error would make every detector fail for the wrong reason.

Run:  .venv/bin/python tools/mutate_webpoet.py
Gate: .venv/bin/python tools/mutate_webpoet.py --gate     # nonzero if any mutation survives
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TESTS = Path(__file__).resolve().parents[1] / "tests" / "test_python.py"

import diff_webpoet
from parsel.selector import SelectorList

# A mutant is broken ON PURPOSE, and a `to_item()` that raises part-way through web-poet's field gather leaves
# an async field's coroutine unawaited (zyte's `description`/`currencyRaw` are two). Python reports that at GC
# time — arbitrarily later, in the middle of an unrelated line of this report, and from outside any
# suppression the failing call could install. Filtered for the whole run so the output is about mutations
# rather than their fallout; state bleed is guarded separately, by re-running the detectors after teardown.
warnings.filterwarnings("ignore", category=RuntimeWarning, message=r"coroutine .* was never awaited")

from frostwork import page as fpage
from frostwork import webpoet as wp


# --------------------------------------------------------------------------- the mutations
# Each returns its undo. Install through `_patch` (below the detectors), which checks the replacement's
# signature against the real function: an arity mismatch is "caught" by every detector for a reason that has
# nothing to do with the behaviour being mutated, and that has faked a result here three times.
def _mut_drop_set_name():
    """Stop registering the converted field with web-poet. The field exists as a descriptor but `to_item()`
    never lists it, so the item comes back missing that key."""
    orig = wp._as_wp_field

    def _as_wp_field(name, getter, wp_kwargs=None):
        f = orig(name, getter, wp_kwargs)
        f.__set_name__ = lambda owner, n: None  # registration dropped
        return f

    return _patch(wp, "_as_wp_field", _as_wp_field)


def _mut_no_mro_merge():
    """Use only the class's OWN declarations, dropping inherited ones — what a naive `__init_subclass__`
    does, which silently empties half the schema of any page object with a base."""
    return _patch(wp, "_merge_mro", lambda cls, attr: dict(getattr(cls, attr, {}) or {}))


def _mut_resolve_by_merge_only():
    """Take the schema from the MERGED declarations instead of asking the MRO what each name resolves to.
    That keeps a selector the page object no longer answers with, and the resurrection only shows one
    GENERATION down — which is why the direct-override test passed while the bug was live."""
    def _resolved_schema(cls):
        return (
            dict(wp._merge_mro(cls, "_frostwork_own_specs")),
            dict(wp._merge_mro(cls, "_frostwork_own_groups")),
        )

    return _patch(wp, "_resolved_schema", _resolved_schema)


def _mut_ignore_transforms():
    """Make `_shape` drop `.map()`/`.re_first()`, so a transformed field silently returns the raw value."""
    orig = fpage._shape
    shape = lambda col, card, transforms=(): orig(col, card, ())  # noqa: E731
    undo_page = _patch(fpage, "_shape", shape)
    undo_wp = _patch(wp, "_shape", shape)
    return lambda: (undo_page(), undo_wp())


def _mut_nodes_as_plain_list():
    """Return a plain `list` instead of a `SelectorList` from the `all=` node handoff. zyte's
    `_handle_selectorlist` gates on `SelectorList` exactly, so a plain list falls through to their
    "returned as is" path."""
    orig = wp._as_nodes

    def _as_nodes(value, card, name="<field>", pinned=None):
        out = orig(value, card, name, pinned)
        return list(out) if isinstance(out, SelectorList) else out

    return _patch(wp, "_as_nodes", _as_nodes)


def _mut_processors_ignore_nested_class():
    """Resolve only `out=`, ignoring the nested `Processors` class — the route every zyte-common-items base
    page uses, so this restores the original defect for anyone inheriting `ProductPage`."""
    def _processors_for(cls, name):
        declaring = next((k for k in cls.__mro__ if name in vars(k)), None)
        info = wp._wp_fields_dict(declaring).get(name) if declaring is not None else None
        out = getattr(info, "out", None) if info is not None else None
        return list(out) if out is not None else []

    return _patch(wp, "_processors_for", _processors_for)


def _mut_processors_from_merged_view():
    """Read `out=` from web-poet's MERGED field info instead of from the class that declares the resolved
    descriptor. web-poet merges in `cls.__bases__` order (last wins) while the MRO selects the first, so
    under multiple inheritance the two disagree and the wrong TYPE is handed to the processor that runs."""
    def _processors_for(cls, name):
        info = wp._wp_fields_dict(cls).get(name)
        out = getattr(info, "out", None) if info is not None else None
        if out is not None:
            return list(out)
        procs = getattr(cls, "Processors", None)
        return list(getattr(procs, name, ()) or ()) if procs is not None else []

    return _patch(wp, "_processors_for", _processors_for)


def _mut_ignore_as_node():
    """Ignore `.as_node()`: hand every processor the field's value. This is defect 5 exactly as it shipped —
    a zyte node processor receives a `str`, matches none of its isinstance gates and returns it UNCHANGED."""
    orig = wp._make_field

    def _make_field(name, card, transforms, node_input=None, wp_kwargs=None, selector=""):
        return orig(name, card, transforms, None, wp_kwargs, selector)

    return _patch(wp, "_make_field", _make_field)


def _mut_node_on_processor_presence():
    """The opposite error, and the one the explicit declaration replaced: infer the input contract from
    processor PRESENCE. Then `out=[lambda v: v.upper()]` over a bare element gets a `Selector` and raises,
    and `images_processor` gets a node it hands straight back."""
    orig = wp._make_field

    def _make_field(name, card, transforms, node_input=None, wp_kwargs=None, selector=""):
        return orig(name, card, transforms, True, wp_kwargs, selector)

    return _patch(wp, "_make_field", _make_field)


def _mut_out_truthiness():
    """Resolve `out=` by TRUTHINESS instead of `out is not None`. An `out=[]` — the documented way to decline
    one of the nine processors zyte's `ProductPage` attaches by name — then falls through to the nested
    class, so the field is treated as processor-bearing."""
    def _processors_for(cls, name):
        declaring = next((k for k in cls.__mro__ if name in vars(k)), None)
        info = wp._wp_fields_dict(declaring).get(name) if declaring is not None else None
        out = getattr(info, "out", None) if info is not None else None
        if out:  # the bug: an empty list is an ANSWER, not the absence of one
            return list(out)
        procs = getattr(cls, "Processors", None)
        return list(getattr(procs, name, ()) or ()) if procs is not None else []

    return _patch(wp, "_processors_for", _processors_for)


def _mut_field_drops_empty_out():
    """The same defect one step earlier: `field()` forwards only the keywords it was given, so forwarding
    `out` by truthiness silently drops `out=[]` and web-poet never learns the field opted out."""
    orig = wp.field

    def field(selector, *, all=False, join=None, cached=False, meta=None, out=None):
        f = orig(selector, all=all, join=join, cached=cached, meta=meta, out=out)
        if out is not None and not out:
            f.wp_kwargs.pop("out", None)  # the bug: `if out:` when forwarding
        return f

    return _patch(wp, "field", field, diff_webpoet)


def _mut_as_node_ignores_the_element_name():
    """Hand the processor whatever `lxml.html.fromstring` returns, without looking for the element the
    selector matched. Faithful for a `<div>` and wrong for the document frame: `<body>` with a lone child
    comes back as the CHILD, and `<head>`/`<title>`/`<meta>`/`<link>`/`<base>` as a synthesised `<html>`."""
    def _as_node(raw, pinned=None, what="the node handoff"):
        from lxml.html import fromstring
        from parsel import Selector

        return Selector(root=fromstring(raw))

    return _patch(wp, "_as_node", _as_node)


def _mut_as_node_reads_the_identity_off_the_source():
    """Ignore the identity the ENGINE resolved and read the tag off the raw source. Faithful for every
    element with a start tag of its own, and wrong for exactly the ones without: a synthesized frame's outer
    HTML starts with its CONTENT, so `<p>x</p>` hands the `<p>` to an `html`, a `body` and a `p` field."""
    orig = wp._as_node

    def _as_node(raw, pinned=None, what="the node handoff"):
        return orig(raw, None, what)

    return _patch(wp, "_as_node", _as_node)


def _mut_node_keeps_its_invented_ancestors():
    """Skip the detach, so the node is still parented by the frame the re-parse invented and `ancestor::*`
    answers with elements no selector matched — outside the subtree-local contract."""
    def _as_node(raw, pinned=None, what="the node handoff"):
        from parsel import Selector

        if pinned is None:
            m = wp._START_TAG_NAME.match(raw)
            pinned = m.group(1).lower() if m else None
        return Selector(root=wp._reparse(raw, pinned, what))

    return _patch(wp, "_as_node", _as_node)


def _mut_node_document_parses_everything():
    """Use the frame's document parse for every element. It ADDS what the document rules imply: a nested
    `<frameset>`'s children come back wrapped in a `<body>` that is in neither the page nor parsel's tree."""
    def _reparse(raw, want, what):
        from lxml.html import document_fromstring

        root = document_fromstring(raw)
        return root if root.tag == want else next((el for el in root.iter(want)), None)

    return _patch(wp, "_reparse", _reparse)


def _mut_many_ignores_subfield_kwargs():
    """Accept `out=`/`cached=`/`meta=` on a `Many`/`One` subfield and drop them, instead of refusing at
    declaration. A processor written into a subfield then never runs, silently."""
    orig = wp.Many

    def Many(container, *, item=None, **subfields):
        for f in subfields.values():
            if isinstance(f, wp._FrostField):
                f.wp_kwargs = {}
        return orig(container, item=item, **subfields)

    return _patch(wp, "Many", Many, diff_webpoet)


def _mut_accept_unstated_processor_input():
    """Skip the class-definition validation entirely: an unstated processor-bearing element field, a `join=`
    with `.as_node()`, a transform on one. Each is a combination whose meaning would have to be guessed at,
    and the `join=` case is the original silent defect — a node processor handed raw HTML."""
    return _patch(wp.FrostFields, "_frostwork_validate_fields", classmethod(lambda cls: None))


# (name, apply, the detectors that must catch it). The expected set is checked for EQUALITY: "some detector
# noticed" would let a mutation the differential is declared to catch pass as unit-only, and a differential losing a
# column looks exactly like a unit vector doing its job.
MUTATIONS = [
    ("drop-__set_name__", _mut_drop_set_name, {"diff", "unit"}),
    ("no-mro-merge", _mut_no_mro_merge, {"diff", "unit"}),
    ("resolve-by-merge-only", _mut_resolve_by_merge_only, {"unit"}),
    ("ignore-map-transforms", _mut_ignore_transforms, {"diff", "unit"}),
    ("nodes-as-plain-list", _mut_nodes_as_plain_list, {"diff", "unit", "surface"}),
    ("processors-ignore-nested-class", _mut_processors_ignore_nested_class, {"diff", "unit"}),
    ("processors-from-merged-view", _mut_processors_from_merged_view, {"unit"}),
    ("ignore-as-node", _mut_ignore_as_node, {"diff", "unit"}),
    ("node-on-processor-presence", _mut_node_on_processor_presence, {"diff", "unit"}),
    ("out-truthiness", _mut_out_truthiness, {"diff", "unit"}),
    ("field-drops-empty-out", _mut_field_drops_empty_out, {"diff", "unit"}),
    ("as-node-ignores-element-name", _mut_as_node_ignores_the_element_name, {"unit"}),
    ("as-node-reads-identity-off-source", _mut_as_node_reads_the_identity_off_the_source, {"unit"}),
    ("node-keeps-invented-ancestors", _mut_node_keeps_its_invented_ancestors, {"unit"}),
    ("node-document-parses-everything", _mut_node_document_parses_everything, {"unit"}),
    ("many-ignores-subfield-kwargs", _mut_many_ignores_subfield_kwargs, {"unit"}),
    ("accept-unstated-processor-input", _mut_accept_unstated_processor_input, {"unit"}),
]

# Logic that CANNOT be expressed as a function patch, named rather than silently omitted — because
# "N mutations, 0 survivors" reads as "the module is covered", and the module is bigger than what a
# function patch can reach. The engine's sweep learned this the hard way: end-tag scope was two `matches!`
# arms, so the sweep reported the rule fully protected while the real logic was invisible to it.
#
# These class-definition behaviors are not represented by a replaceable integration function. Each entry
# names the detector that covers it.
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
        stat, by_shape, by_proc, meaningful, witnessed, _examples = diff_webpoet.sweep(
            seed=seed, schemas=schemas, show=0
        )
    except Exception as exc:  # noqa: BLE001 - a mutation that breaks the harness outright is still caught
        return True, f"harness raised: {type(exc).__name__}"
    coverage = diff_webpoet.coverage_failures(by_shape, by_proc, meaningful, witnessed)
    bad = stat["DIVERGE"] + stat["CRASH"] + len(coverage)
    return bad > 0, (
        f"DIVERGE={stat['DIVERGE']} CRASH={stat['CRASH']} coverage={len(coverage)} "
        f"pairs={stat['pairs']}"
    )


# The unit vectors that assert this integration's contracts, by NODE ID. Targeted rather than the whole
# suite: a mutation that reddens 90 unrelated engine tests tells you nothing about which contract it broke,
# and a run per mutant should cost a second, not ten. A renamed test makes pytest exit with a collection
# error, which the baseline check below reports rather than silently reducing the detector's reach.
# The `unit` detector runs the tests MARKED `@pytest.mark.webpoet_contract`, not a list of names kept here.
# The list was a hand-written universe and behaved like one: three mutations survived a sweep only because
# tests written in the same commit had not been added to it. A marker cannot drift from the test it is on.
UNIT_MARKER = "webpoet_contract"


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
    args = ["-q", "--no-header", "-p", "no:cacheprovider", "-m", UNIT_MARKER, str(TESTS)]
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = _pytest.main(args, plugins=[collector])
    if code == 5:  # no tests collected — the marker is gone, so this detector proves nothing
        return True, f"no tests marked @pytest.mark.{UNIT_MARKER} were collected"
    if code == 4:
        return True, f"pytest usage error: {buf.getvalue()[-200:]}"
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


def _same_signature(original, replacement, label: str) -> None:
    """Refuse a patch whose signature differs from the function it replaces — in parameter names, KINDS,
    default VALUES or descriptor kind.

    A patch that raises `TypeError` on arity is "caught" by every detector for a reason that has nothing to do
    with the behaviour being mutated. Names alone were not enough (a kind swap or a plain function replacing a
    classmethod changes the call site while the names line up), and nor was "does a default exist": flipping
    `all=False` to `all=True` passed as the same signature while changing what every caller gets."""
    import inspect

    def descriptor_kind(fn):
        for kind in (classmethod, staticmethod, property):
            if isinstance(fn, kind):
                return kind.__name__
        return "function"

    def unwrap(fn):
        return fn.__func__ if isinstance(fn, (classmethod, staticmethod)) else fn

    def default(p):
        """The default VALUE, normalized so only a real difference shows: `False` and `True` differ, two
        `None`s do not, and an object whose repr carries its address does not differ from itself."""
        if p.default is inspect.Parameter.empty:
            return ("required",)
        d = p.default
        if isinstance(d, (type(None), bool, int, float, complex, str, bytes)):
            return ("value", type(d).__name__, d)
        if isinstance(d, (tuple, frozenset)) and not d:
            return ("empty", type(d).__name__)
        return ("object", type(d).__name__)

    def shape(fn):
        params = inspect.signature(unwrap(fn)).parameters.values()
        return [
            (p.name, p.kind, default(p))
            for p in params
            if p.name not in ("cls", "self")  # a bound original has already lost it
        ]

    if descriptor_kind(original) != descriptor_kind(replacement):
        raise SystemExit(
            f"mutate-webpoet: the {label} patch is a {descriptor_kind(replacement)} but the real attribute is "
            f"a {descriptor_kind(original)}; restoring it would rebind differently."
        )
    want, got = shape(original), shape(replacement)
    if want != got:
        raise SystemExit(
            f"mutate-webpoet: the {label} patch takes {got} but the real function takes {want} (name, kind, "
            f"default). Mirror it — an arity, kind or default mismatch changes what callers get for a reason "
            f"that has nothing to do with the behaviour being mutated."
        )


def _patch(module, name: str, replacement, *extra_modules):
    """Install `replacement` over `module.name` (and any alias namespaces), signature-checked, and return the
    undo. `extra_modules` exist because `tools/diff_webpoet.py` imports some names directly, so patching only
    `frostwork.webpoet` would leave the differential calling the unmutated one."""
    # the RAW attribute, from the namespace: `getattr` on a class unwraps a classmethod into a bound method,
    # and restoring that would leave `cls` pinned to the base — a teardown that looks like it worked
    original = vars(module).get(name, getattr(module, name, None))
    _same_signature(original, replacement, f"{module.__name__}.{name}")
    targets = (module, *extra_modules)
    for target in targets:
        setattr(target, name, replacement)

    def undo():
        for target in targets:
            setattr(target, name, original)

    return undo


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
        label = "+".join(sorted(caught)) if caught else "NOTHING"
        print(f"  {name:38} {label:22} {'+'.join(sorted(want)) or '-':22} {summaries.get('unit', '')}")
        if not caught:
            survivors.append(name)
        elif set(caught) != want:
            # EQUALITY, not "the expected ones are a subset": an extra detector is as much a change of
            # provenance as a missing one, and usually means the mutation stopped being what it says it is
            # (an arity slip makes everything go red) or that a gate grew a column nobody recorded.
            wrong_provenance.append((name, sorted(want), sorted(caught)))

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
        print("\n  PROVENANCE MISMATCH -> the detectors that caught these are not the ones declared. The")
        print("  mutation is still caught, so a survivor count cannot see it; it means a gate lost a column:")
        for name, want, caught in wrong_provenance:
            print(f"    {name}: expected exactly {'+'.join(want) or '-'}, got {'+'.join(caught) or '-'}")
    if survivors:
        print(f"\n  SURVIVORS -> the behaviour these control is asserted by nothing: {survivors}")
    if args.gate and (survivors or wrong_provenance):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
