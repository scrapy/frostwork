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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

    def patched(col, card):
        out = orig(col, card)
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


MUTATIONS = [
    ("drop-__set_name__", _mut_drop_set_name),
    ("no-mro-merge", _mut_no_mro_merge),
    ("ignore-map-transforms", _mut_ignore_transforms),
    ("nodes-as-plain-list", _mut_nodes_as_plain_list),
    ("processors-ignore-nested-class", _mut_processors_ignore_nested_class),
    ("never-node", _mut_never_node),
    ("always-node", _mut_always_node),
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
    "group-flat-reconciliation": (
        "the `.pop()` pair that lets a nearest-class flat field replace an inherited group of the same "
        "name — covered by tests/test_python.py's group-override case"
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


def _detect(schemas: int, seed: int) -> tuple:
    """Run the differential as the detector. Returns `(red, summary)`."""
    try:
        stat, _by_shape, _by_proc, _meaningful, _examples = diff_webpoet.sweep(
            seed=seed, schemas=schemas, show=0
        )
    except Exception as exc:  # noqa: BLE001 - a mutation that breaks the harness outright is still caught
        return True, f"harness raised: {type(exc).__name__}"
    bad = stat["DIVERGE"] + stat["CRASH"]
    return bad > 0, f"DIVERGE={stat['DIVERGE']} CRASH={stat['CRASH']} pairs={stat['pairs']}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schemas", type=int, default=8, help="schemas per shape per mutant (sampled)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--gate", action="store_true", help="exit nonzero if any mutation survives")
    args = ap.parse_args()

    baseline_red, baseline = _detect(args.schemas, args.seed)
    print(f"  baseline (unmutated): {baseline}")
    if baseline_red:
        raise SystemExit(
            "mutate-webpoet: the UNMUTATED build already fails the detector, so no result here means "
            f"anything. Fix the gate first: {baseline}"
        )

    print(f"\n  {'mutation':34} {'caught':7} detector")
    survivors = []
    for name, apply_mut in MUTATIONS:
        undo = apply_mut()
        try:
            red, summary = _detect(args.schemas, args.seed)
        finally:
            undo()
        print(f"  {name:34} {'yes' if red else 'NO':7} {summary}")
        if not red:
            survivors.append(name)

    # prove the teardown restored the module: a survivor list is meaningless if the last mutation stuck
    after_red, after = _detect(args.schemas, args.seed)
    if after_red:
        raise SystemExit(f"mutate-webpoet: teardown failed to restore the module ({after})")

    print(f"\n  not expressible as a function patch (covered by unit vectors instead):")
    for name, why in UNREACHABLE.items():
        print(f"    {name}: {why}")

    print(
        f"\n  MUTATIONS: {len(MUTATIONS)} applied, {len(MUTATIONS) - len(survivors)} caught, "
        f"{len(survivors)} SURVIVED"
    )
    print(f"  (sampled: {args.schemas} schemas per shape per mutant, seed {args.seed})")
    if survivors:
        print(f"\n  SURVIVORS -> the behaviour these control is asserted by nothing: {survivors}")
    if args.gate and survivors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
