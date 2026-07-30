# Security policy

Frostwork parses untrusted HTML and untrusted selector strings, so its threat model is real:
malformed or adversarial input must never cause memory unsafety, and must not crash the process.

## What is in scope

- Memory unsafety of any kind (the engine uses no `unsafe`; a report of `unsafe` being introduced
  or of undefined behavior is in scope).
- A panic, abort, or infinite loop reachable from **HTML input** or from a **selector/XPath string**
  via the public API (`frostwork.extract`, `Page`, `FrostPage`, `check`, the Rust `extract`/`Plan`,
  or the `frostwork-audit` CLI). Note that an *unsupported* selector returning an empty column is
  intended behavior, not a vulnerability.
- A supported selector producing a **wrong non-empty value** versus the lxml/libxml2 oracle. This is
  the project's core correctness invariant; treat a reproducible case as a release blocker. (See
  [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) for documented, intentional divergences, which are
  not in scope.)

## What is not a vulnerability

- Documented divergences from lxml (foster-parenting, adoption-agency, outer-HTML raw source).
- Unsupported selectors returning an empty column (the no-fallback contract).
- Pathological worst-case CPU time on adversarial inputs **within documented complexity bounds**. Matching
  one selector segment against an element is `O(ancestor-depth x compounds-in-the-segment)`: the ancestor
  chain is a path, so the kernel places maximal `>`-runs greedily rather than searching combinations
  (`matcher/matching.rs::seg_match_anchored`). A case that is dramatically worse than that bound is worth
  reporting — an earlier version searched overlapping states without memoization and was *exponential*
  (~28 s for an 11-compound selector against 40 nested `<div>`s), which was reachable from a selector
  string alone and would have been in scope.

## Reporting

Please report suspected vulnerabilities privately using GitHub's
[private vulnerability reporting](https://github.com/shaneaevans/frostwork/security/advisories/new)
rather than opening a public issue. Include a minimal reproducer (the HTML bytes and the selector
list) where possible. We aim to acknowledge reports within a few business days.
