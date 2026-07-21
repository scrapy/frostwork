"""Static schema-audit CLI — ``frostwork-audit path/to/pageobjects.py`` (or
``python -m frostwork.audit``).

Import a module of page objects and report, WITHOUT parsing any HTML, which of their selectors the
engine supports (with an advisory reason for those it does not) and the budget usage. Python's public
extraction APIs fail fast by default; this command provides a consolidated, greppable report for CI
and for page objects that explicitly use ``strict=False``.

It discovers, in the target module's namespace:
  * ``frostwork.Page`` instances (module-level schema objects), audited with :meth:`Page.check`;
  * ``frostwork.webpoet.FrostPage`` subclasses (if web-poet is installed), audited with
    :meth:`FrostPage.check_schema`.

Or pass ``module:REGISTRY`` / ``path.py:REGISTRY`` where the attribute is a mapping or iterable of
specific ``Page``/``FrostPage`` objects. This avoids namespace discovery and lets projects expose a
small import-safe audit surface explicitly.

Exit status is ``0`` when every discovered schema is OK, ``1`` when any has an unsupported selector or
is over budget, and ``2`` for a usage error (bad path, import failure). So a CI step can be simply::

    frostwork-audit myproject/pages.py

IMPORT-SAFETY: discovery works by importing ``target`` and reading its namespace, so the module's
top-level code RUNS (``exec_module`` for a file path, ``import_module`` for a dotted name). Point this
at import-safe page-object modules — ones whose import does not open network/service clients, read
required env, or perform other side effects. If a project mixes schemas with heavy import-time setup,
put the page objects in a module that only defines them and audit that module.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import json
import os
import sys
from collections.abc import Mapping
from typing import List, Tuple

from .page import Page, SchemaReport


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("frostwork")
    except Exception:  # pragma: no cover - package not installed (e.g. source tree on path)
        return "unknown"


def _load_module(target: str):
    """Import ``target`` — a filesystem path to a ``.py`` file, or a dotted module name."""
    if os.path.sep in target or target.endswith(".py") or os.path.exists(target):
        path = os.path.abspath(target)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"no such file: {target}")
        # Make sibling imports work (the page-object module may import project-local helpers).
        pkg_dir = os.path.dirname(path)
        if pkg_dir not in sys.path:
            sys.path.insert(0, pkg_dir)
        name = "_frostwork_audit_target_" + os.path.splitext(os.path.basename(path))[0]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {target}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    return importlib.import_module(target)


def _discover(module, registry=None) -> List[Tuple[str, SchemaReport]]:
    """Find page-object schemas in ``module`` and audit each. Returns ``[(label, report), ...]`` in
    definition order, de-duplicated by identity (a class imported under two names is audited once)."""
    # FrostPage is optional (needs web-poet); tolerate its absence.
    try:
        from .webpoet import FrostPage
    except Exception:  # pragma: no cover - only when web-poet is missing
        FrostPage = None

    out: List[Tuple[str, SchemaReport]] = []
    seen = set()
    if registry is None:
        objects = list(vars(module).items())
    elif isinstance(registry, Mapping):
        objects = [(str(name), obj) for name, obj in registry.items()]
    else:
        objects = [
            (getattr(obj, "__name__", f"schema[{i}]"), obj)
            for i, obj in enumerate(registry)
        ]

    for name, obj in objects:
        # Namespace discovery honors the privacy convention; an explicit registry is intentional.
        if registry is None and name.startswith("_"):
            continue
        key = id(obj)
        if isinstance(obj, Page):
            if key in seen:
                continue
            seen.add(key)
            out.append((name, obj.check()))
        elif (
            FrostPage is not None
            and inspect.isclass(obj)
            and issubclass(obj, FrostPage)
            and obj is not FrostPage
            # Namespace discovery skips imported bases/re-exports; an explicit registry may intentionally
            # contain classes from another module.
            and (registry is not None or getattr(obj, "__module__", None) == getattr(module, "__name__", None))
        ):
            if key in seen:
                continue
            seen.add(key)
            out.append((name, obj.check_schema()))
    return out


def _format(label: str, report: SchemaReport, verbose: bool) -> str:
    status = "OK" if report.ok else "PROBLEMS"
    head = (
        f"{status:9} {label}  "
        f"(members {report.members}/{report.max_members}, "
        f"sib-bits {report.sib_bits}/{report.max_sib_bits})"
    )
    lines = [head]
    if report.over_budget:
        lines.append("    ! schema is OVER BUDGET (too many selectors — split it)")
    for f in report.unsupported:
        lines.append(f"    ✗ {f.name} = {f.selector!r}")
        lines.append(f"        {f.reason}")
    if verbose:
        supported = [f for f in report.fields if f.supported]
        for g in report.groups:
            if g.container.supported:
                supported.append(g.container)
            supported.extend(sf for sf in g.subfields if sf.supported)
        for f in supported:
            lines.append(f"    ✓ {f.name} = {f.selector!r}")
    return "\n".join(lines)


def _field_dict(f) -> dict:
    return {
        "name": f.name,
        "selector": f.selector,
        "supported": f.supported,
        "reason": f.reason,
    }


def _report_dict(label: str, report: SchemaReport) -> dict:
    return {
        "name": label,
        "ok": report.ok,
        "over_budget": report.over_budget,
        "budget": {
            "members": report.members,
            "max_members": report.max_members,
            "sib_bits": report.sib_bits,
            "max_sib_bits": report.max_sib_bits,
        },
        "fields": [_field_dict(f) for f in report.fields],
        "groups": [
            {
                "name": g.name,
                "container": _field_dict(g.container),
                "subfields": [_field_dict(sf) for sf in g.subfields],
            }
            for g in report.groups
        ],
    }


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="frostwork-audit",
        description="Statically audit Frostwork page-object schemas (no HTML parsed).",
    )
    parser.add_argument(
        "target",
        help="a .py path or dotted module, optionally suffixed :REGISTRY for import-safe explicit discovery",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="also list supported selectors"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit the full report as JSON (for CI annotations)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {_version()}")
    args = parser.parse_args(argv)

    # Keep the report printable on narrow terminal encodings (e.g. Windows cp1252 when piped).
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    # A `:REGISTRY` suffix names an attribute, so it must be an identifier; anything else (a
    # Windows drive letter, a path) is part of the target itself.
    module_target, sep, registry_name = args.target.rpartition(":")
    if not sep or not registry_name.isidentifier():
        module_target, registry_name = args.target, ""

    def _usage_error(msg: str) -> int:
        if args.json:
            print(json.dumps({"ok": False, "schemas": [], "error": msg}))
        print(f"frostwork-audit: {msg}", file=sys.stderr)
        return 2

    try:
        module = _load_module(module_target)
    except Exception as exc:
        return _usage_error(f"could not import {module_target!r}: {exc}")
    if registry_name:
        try:
            registry = getattr(module, registry_name)
        except AttributeError:
            return _usage_error(
                f"module {module_target!r} imported OK but has no attribute {registry_name!r} "
                "(the :REGISTRY suffix names a mapping/iterable of Page/FrostPage objects)"
            )
    else:
        registry = None

    reports = _discover(module, registry)
    if not reports:
        msg = (
            f"no frostwork Page instances or FrostPage subclasses found in {args.target!r}"
        )
        if args.json:
            print(json.dumps({"ok": False, "schemas": [], "error": msg}))
        print(f"frostwork-audit: {msg}", file=sys.stderr)
        return 2

    problems = sum(1 for _, report in reports if not report.ok)

    if args.json:
        payload = {
            "ok": problems == 0,
            "schemas": [_report_dict(label, report) for label, report in reports],
            "summary": {
                "total": len(reports),
                "ok": len(reports) - problems,
                "problems": problems,
            },
        }
        print(json.dumps(payload, indent=2))
        return 1 if problems else 0

    for label, report in reports:
        print(_format(label, report, args.verbose))

    n = len(reports)
    ok = n - problems
    print(f"\n{ok}/{n} schema(s) OK" + (f", {problems} with problems" if problems else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
