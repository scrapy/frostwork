"""Static selector scan — mine selector literals straight out of Python source.

`frostwork-audit <module>` audits *schema objects* (`Page` / `FrostPage`), which means it can only see
code that has already been ported to a Frostwork schema. Real projects also carry selectors the audit
could not reach: inline `response.css(...)` / `.xpath(...)` in a spider callback, `ItemLoader.add_css`,
`LinkExtractor(restrict_css=...)`. Those had to be rewritten *before* they could be audited — exactly
backwards, since the audit is what tells you whether a rewrite is even needed (reported by Jan Seidler,
2026-07-23).

This module answers the same question one step earlier: parse the file with `ast` (never importing it,
so it is safe on spiders with heavy import-time setup) and classify every selector *literal* it finds,
with `file:line` for each site. Dynamic selectors (f-strings, concatenation, variables) cannot be
decided statically and are reported as SKIPPED rather than quietly dropped.

Not a replacement for the schema audit: it sees a call site, not a complete schema, so it cannot check
the combined member/sibling budget. Literal group builders retain their container/sub-field context.
Use it to size a migration and to keep
un-ported selectors visible in CI; use the schema audit for ported page objects.
"""

from __future__ import annotations

import ast
import os
from typing import Iterable, List, NamedTuple, Optional

# Calls whose argument is a selector: name -> (positional index, flavour). The flavour is what we hold
# the string to — a `.xpath()` argument is XPath even when it would also parse as CSS. Note the index:
# `sel.css(q)` and `ItemLoader.get_css(q)` take the selector FIRST, while `add_css`/`replace_css` take
# `(field_name, selector)`.
SELECTOR_CALLS = {
    "css": (0, "css"),
    "xpath": (0, "xpath"),
    "get_css": (0, "css"),  # ItemLoader.get_css(css, *processors)
    "get_xpath": (0, "xpath"),
    "add_css": (1, "css"),  # ItemLoader.add_css(field_name, css, *processors)
    "add_xpath": (1, "xpath"),
    "replace_css": (1, "css"),
    "replace_xpath": (1, "xpath"),
}

# Keyword arguments carrying selectors — `LinkExtractor(restrict_css=…, restrict_xpaths=…)`. The value
# may be a string or a list/tuple of strings. Deliberately narrow: a generic `css=`/`selector=` kwarg on
# an unrelated call would only add noise.
KEYWORD_ARGS = {
    "restrict_css": "css",
    "restrict_xpaths": "xpath",
}

# Frostwork's own builders, in case a project mixes them into code the import-mode audit can't load.
# Two spellings, told apart by arity/case: the `Page` builders take a NAME first — `field(name, sel)`,
# `many(name, container, {sub: sel})` — while the web-poet ones take the selector/container first:
# `field(sel)`, `Many(container, sub=field(sel))` (each inner `field(...)` is its own call node, so it is
# picked up on its own).
FIELD_CALLS = {"field", "field_all", "field_join"}
GROUP_CALLS = {"many", "one"}
WEBPOET_GROUP_CALLS = {"Many", "One"}
FIELD_MODIFIERS = {"map", "re_first", "as_node", "as_value", "typed_as"}


# Pseudo call name for a file this interpreter could not parse — a scan FAILURE (nothing was audited in
# that file), reported separately from a dynamic selector that was seen and skipped.
SYNTAX_ERROR = "<syntax-error>"


class Site(NamedTuple):
    """One selector call site."""

    path: str
    line: int
    call: str  # the attribute/function name it appeared in (`css`, `add_xpath`, `field`, …)
    kind: str  # "css" | "xpath" | "auto" (decide by syntax)
    selector: Optional[str]  # None when the argument is not a literal (see `dynamic`)
    dynamic: bool = False
    context: str = "flat"  # flat | group-container | group-subfield | group-schema

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"


def _literal_strings(node) -> Optional[List[str]]:
    """The string literal(s) in `node`, or None if it is not statically a string / list of strings."""
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else None
    if isinstance(node, (ast.List, ast.Tuple)):
        out: List[str] = []
        for el in node.elts:
            got = _literal_strings(el)
            if got is None:
                return None
            out.extend(got)
        return out
    return None


def _kw(node: ast.Call, name: str):
    """The value of keyword argument `name`, if this call passes it."""
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _pick(node: ast.Call, pos: int, kwname: str):
    """The argument at positional index `pos` OR keyword `kwname` — builders are called both ways, and
    mixed (`many("cards", container=".card", subfields={...})`), so neither form may be assumed."""
    return _kw(node, kwname) or (node.args[pos] if len(node.args) > pos else None)


def _call_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _field_marker(node) -> Optional[ast.Call]:
    """Follow a field's fluent receiver, never its callbacks or an arbitrary factory's arguments.

    Finding a `field()` somewhere inside an expression does not prove the expression returns it.
    An unrecognized wrapper stays unresolved instead of making a partial scan look complete.
    """
    while isinstance(node, ast.Call):
        if _call_name(node) in FIELD_CALLS:
            return node
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in FIELD_MODIFIERS:
            break
        node = node.func.value
    return None


def scan_source(source: str, path: str) -> List[Site]:
    """Every selector call site in `source` (a parsed Python module), in file order."""
    tree = ast.parse(source, filename=path)
    sites: List[Site] = []
    contexts: dict[int, str] = {}

    def add(node, call: str, kind: str, context: str = "flat") -> None:
        if node is None:
            return  # the call does not pass this argument at all
        got = _literal_strings(node)
        if got is None:
            # An f-string / concatenation / variable: honest SKIPPED, never silently dropped.
            sites.append(Site(path, node.lineno, call, kind, None, dynamic=True, context=context))
            return
        for s in got:
            sites.append(Site(path, node.lineno, call, kind, s, context=context))

    # ast.walk visits parents before their descendants. A group records its actual field markers
    # here before those calls are visited, so context detection needs no separate tree traversal.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in SELECTOR_CALLS:
            idx, kind = SELECTOR_CALLS[name]
            if len(node.args) > idx:
                add(node.args[idx], name, kind)
            else:  # the keyword form: `css(query=…)` (parsel), `add_css(field, css=…)` (ItemLoader)
                for kw in node.keywords:
                    if kw.arg in ("query", "css", "xpath"):
                        add(kw.value, name, kind)
        elif name in FIELD_CALLS:
            # Resolve by ARGUMENT IDENTITY, not arity. Counting positionals lost the all-keyword form
            # entirely (a silently "clean" migration report) and, for `field("title", selector=...)`,
            # audited the FIELD NAME as a selector — noise that then failed the audit.
            sel = _pick(node, 1, "selector")
            if sel is None and len(node.args) == 1 and not _kw(node, "selector"):
                sel = node.args[0]  # web-poet: field(selector)
            add(sel, name, "auto", contexts.get(id(node), "flat"))
        elif name in GROUP_CALLS:
            add(_pick(node, 1, "container"), name, "auto", "group-container")
            subs = _pick(node, 2, "subfields")
            if isinstance(subs, ast.Dict):
                for value in subs.values:
                    # a sub-field may be a cardinality TUPLE — `(".tag::text", "join", " ")`. Only its
                    # first element is a selector; scanning the whole tuple reported "join" and " " as
                    # selector sites, and the separator then failed the audit.
                    add(value.elts[0] if isinstance(value, (ast.Tuple, ast.List)) and value.elts else value,
                        name, "auto", "group-subfield")
            elif subs is not None:
                add(subs, name, "auto", "group-schema")
        elif name in WEBPOET_GROUP_CALLS:
            add(_pick(node, 0, "container"), name, "auto", "group-container")
            for kw in node.keywords:
                if kw.arg is None:
                    add(kw.value, name, "auto", "group-schema")
                elif kw.arg not in ("item", "container"):
                    marker = _field_marker(kw.value)
                    if marker is not None:
                        contexts[id(marker)] = "group-subfield"
                    else:
                        sites.append(Site(path, kw.value.lineno, name, "auto", None,
                                          dynamic=True, context="group-schema"))
        for kw in node.keywords:
            if kw.arg in KEYWORD_ARGS:
                add(kw.value, name, KEYWORD_ARGS[kw.arg])

    sites.sort(key=lambda s: (s.line,))
    return sites


def scan_path(target: str) -> List[Site]:
    """Scan a `.py` file, or every `.py` file under a directory (recursively, sorted)."""
    files: List[str] = []
    if os.path.isdir(target):
        for root, dirs, names in os.walk(target):
            dirs[:] = sorted(d for d in dirs if d not in {"__pycache__", ".git", ".venv", "node_modules"})
            files.extend(os.path.join(root, n) for n in sorted(names) if n.endswith(".py"))
    else:
        files.append(target)

    sites: List[Site] = []
    for f in files:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
        try:
            sites.extend(scan_source(source, f))
        except SyntaxError as exc:  # a file this Python can't parse: report, don't abort the scan
            sites.append(Site(f, getattr(exc, "lineno", 0) or 0, SYNTAX_ERROR, "auto", None, True))
    return sites


class Verdict(NamedTuple):
    site: Site
    supported: bool
    reason: Optional[str]


# XPath prefixes Frostwork routes as XPath (see `frostwork.check` / diagnostics routing).
_XPATH_PREFIXES = ("/", "./", "normalize-space(")


def judge(sites: Iterable[Site]) -> List[Verdict]:
    """Decide each site's support. Literal selectors go through the real compiler (`frostwork.check`),
    so the verdict is the engine's own, not a second parser."""
    from .page import check

    out: List[Verdict] = []
    for site in sites:
        if site.call == SYNTAX_ERROR:
            out.append(Verdict(site, False, "file could not be parsed — NOTHING in it was audited"))
            continue
        if site.dynamic or site.selector is None:
            out.append(Verdict(site, False, "not a literal (f-string/variable) — cannot audit statically"))
            continue
        sel = site.selector
        if site.kind == "xpath" and not sel.lstrip().startswith(_XPATH_PREFIXES):
            # A relative step (`td/text()`, `./x`) inside a per-container loop. `check()` would route it
            # as CSS and blame CSS syntax; name the real cause and the rewrite instead. This is the
            # dominant un-portable shape in production page objects (docs/MIGRATION.md).
            out.append(
                Verdict(
                    site,
                    False,
                    "relative XPath step — a per-container loop's selector. Audit the intended "
                    "Many/One context before porting; `.//x` includes nested descendants and is not "
                    "equivalent to the child path `./x`; see docs/MIGRATION.md",
                )
            )
            continue
        if site.context == "group-container":
            field = check([], [(sel, [])]).groups[0].container
        elif site.context == "group-subfield":
            field = check([], [("*", [("value", sel)])]).groups[0].subfields[0]
        else:
            field = check([sel]).fields[0]
        out.append(Verdict(site, field.supported, field.reason))
    return out


def summarize(verdicts: List[Verdict]) -> dict:
    errors = [v for v in verdicts if v.site.call == SYNTAX_ERROR]
    literal = [v for v in verdicts if not v.site.dynamic]
    supported = [v for v in literal if v.supported]
    return {
        "sites": len(verdicts) - len(errors),
        "literal": len(literal),
        "supported": len(supported),
        "unsupported": len(literal) - len(supported),
        "skipped": len(verdicts) - len(literal) - len(errors),
        "errors": len(errors),  # unparseable files: the scan is INCOMPLETE, not clean
        "coverage": (len(supported) / len(literal)) if literal else None,
        "complete": bool(literal) and len(literal) == len(verdicts) and not errors,
    }
