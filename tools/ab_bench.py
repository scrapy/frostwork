#!/usr/bin/env python3
"""A/B two `bench` builds by INTERLEAVING them inside each cell.

Sequential runs on a laptop drift 5-10%, which is larger than most real matcher wins, so each cell runs
both binaries REP times alternating A,B,A,B,... and reports min-of-REP per side. Each cell also carries
its own jitter -- the within-side spread -- because a delta smaller than that is not evidence: two
identical binaries produce per-cell deltas up to +/-2.3% here. Calibrate by passing the same binary as
--a and --b; every cell should then be marked `~`.

Usage:
    ab_bench.py --a <bench-binary-A> --b <bench-binary-B> [--reps N] [--iters N] [--tables ...] [--counts ...]

Page generators and selector pools come from bench_matrix, so each workload is defined once.
"""
import argparse
import functools
import os
import statistics
import subprocess
import sys
import tempfile

# Line-flush every row, so a long sweep read from a log is not lost to buffering.
print = functools.partial(__builtins__.print, flush=True) if hasattr(__builtins__, "print") \
    else functools.partial(__builtins__["print"], flush=True)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bench_matrix as bm  # noqa: E402  (page generators + selector pools, single-sourced)


def run_bench(binary, html_path, sels, iters):
    """One `bench` invocation -> µs/page. Selectors go on stdin (shell quoting can't corrupt them)."""
    env = dict(os.environ, FROSTWORK_BENCH_ITERS=str(iters))
    p = subprocess.run(
        [binary, html_path], input="\n".join(sels), capture_output=True, text=True, env=env
    )
    if p.returncode != 0:
        raise SystemExit(f"{binary} failed: {p.stderr.strip()}")
    # stderr: bytes, nsel, us/page, pages/s, MB/s, vals/page
    fields = p.stderr.strip().split("\t")
    return float(fields[2]), int(fields[5])


def cell(a, b, html_path, sels, iters, reps):
    """Interleaved A,B,A,B,... -> (min_a, min_b, jitter, vals_a, vals_b). Interleaving puts drift on both
    sides rather than on whichever build ran second; `jitter` is the worse within-side spread,
    `(max - min) / min`, which is this cell's error bar."""
    ta, tb, va, vb = [], [], None, None
    for _ in range(reps):
        t, va = run_bench(a, html_path, sels, iters)
        ta.append(t)
        t, vb = run_bench(b, html_path, sels, iters)
        tb.append(t)
    jitter = max((max(ta) - min(ta)) / min(ta), (max(tb) - min(tb)) / min(tb)) * 100.0
    return min(ta), min(tb), jitter, va, vb


def sweep(name, html, counts, pool, a, b, iters, reps, out):
    with tempfile.NamedTemporaryFile("wb", suffix=".html", delete=False) as f:
        f.write(html)
        html_path = f.name
    try:
        kb = len(html) / 1024
        print(f"\n### {name}  ({kb:.0f} KB)")
        print(f"  {'sels':>4} | {'A µs':>10} {'B µs':>10} | {'delta':>8} {'jitter':>7} | {'vals':>7}")
        print("  " + "-" * 58)
        for c in counts:
            sels = pool[:c]
            ma, mb, jit, va, vb = cell(a, b, html_path, sels, iters, reps)
            # A value-count mismatch means the two builds do not extract the same thing. That is a
            # CORRECTNESS failure, not a slow cell, and it must not be reported as a percentage.
            flag = "" if va == vb else f"  ** VALUE MISMATCH A={va} B={vb} **"
            pct = (mb - ma) / ma * 100.0
            # `~` marks a delta inside this cell's own jitter: not evidence, in either direction.
            mark = " " if abs(pct) > jit else "~"
            print(f"  {c:>4} | {ma:>10.1f} {mb:>10.1f} | {pct:>+7.1f}%{mark}{jit:>6.1f}% | {va:>7}{flag}")
            out.append((name, c, ma, mb, pct, va == vb, jit))
    finally:
        os.unlink(html_path)


# Workloads, chosen for what each can SEE: the tag-led pool barely exercises class work, so a
# class-targeted change is invisible there. Both run over the same page so the contrast is readable.
def tables(which):
    med = 200_000
    t = {}
    t["class-led"] = ("class-led selectors, utility-CSS page", bm.class_heavy(med),
                      bm.CLASS_COUNTS, bm.CLASS_POOL)
    t["tag-led"] = ("tag-led selectors, utility-CSS page", bm.class_heavy(med),
                    bm.CLASS_COUNTS, bm.POOL)
    t["desc-led"] = ("descendant-led selectors, utility-CSS page", bm.class_heavy(med),
                     bm.CLASS_COUNTS, bm.DESC_POOL)
    t["desc-deep"] = ("descendant-led selectors, deep-nested page", bm.deep_nested(med),
                      bm.CLASS_COUNTS, bm.DESC_POOL)
    t["attr-led"] = ("attribute-predicate selectors, attribute-heavy page", bm.attr_heavy(med),
                     bm.CLASS_COUNTS, bm.ATTR_POOL)
    t["attr-page"] = ("class-led selectors, attribute-heavy page", bm.attr_heavy(med),
                      bm.CLASS_COUNTS, bm.CLASS_POOL)
    t["listing"] = ("product listing, tag-led", bm.product_listing(med), bm.CLASS_COUNTS, bm.POOL)
    t["listing-class"] = ("product listing, class-led", bm.product_listing(med), bm.CLASS_COUNTS,
                          bm.CLASS_POOL)
    t["article"] = ("article (text-heavy)", bm.article(med), bm.CLASS_COUNTS, bm.POOL)
    t["deep"] = ("deep-nested (ancestor-chain stress)", bm.deep_nested(med), bm.CLASS_COUNTS,
                 bm.CLASS_POOL)
    t["table"] = ("table (tag-dense)", bm.table(med), bm.CLASS_COUNTS, bm.POOL)
    # The DEFERRED tiers: the only workloads that fill the per-element cold buffers, so the only ones
    # where boxing that state could cost rather than pay.
    t["tail"] = ("deferred-tail selectors (:has / :last-child)", bm.product_listing(med),
                 [1, 2, 4, 8], bm.TAIL_POOL)
    t["reverse-values"] = ("attached reverse-position values", bm.reverse_values(med),
                           [1, 2, 4, 8], bm.REVERSE_VALUE_POOL)
    # A pure scan with no selectors must stay flat: per-element cost shows here with nothing to
    # amortize it.
    t["scan"] = ("pure scan (0 selectors -- must stay flat)", bm.class_heavy(med), [0], [])
    # First-value declarations mixed with an all-value column: the scan must reach EOF, but scalar
    # text/attribute columns need not keep allocating values their consumer discards. `F ` is bench's
    # cardinality prefix; the final all-value selector stays present in every measured cell.
    first_pool = ["F .price::text", "F .title::text", "F img::attr(src)",
                  "F a::attr(href)", "F .desc::text", "a::attr(href)"]
    t["first-mixed"] = ("first-value fields plus all links", bm.product_listing(med), [6], first_pool)
    return [t[k] for k in which]


ALL = ["class-led", "tag-led", "desc-led", "desc-deep", "attr-led", "attr-page", "listing",
       "listing-class", "article", "deep", "table", "tail", "reverse-values", "scan", "first-mixed"]


def corpus_cells(corpus_dir, limit):
    """[(label, html_path, selectors)] — one cell per REAL page, with its page object's own selectors.

    The generated tables above can only see what their author thought to generate: a pool is one shape
    applied to one page, and DESIGN.md's rejected list is mostly optimizations that won a synthetic table
    and lost on the schemas real page objects actually carry (the ancestor signature helps above ~16
    descendant-led fields; the corpus median is 11). This mode runs the thing the corpus number is
    measured on, so a delta here is the delta that moves it.

    Selectors the engine declines are dropped: they compile to an empty column on both sides, so keeping
    them would dilute every cell with work neither build does.
    """
    import glob
    import json

    try:
        import frostwork
    except ImportError:
        raise SystemExit("corpus mode needs the frostwork extension: .venv/bin/maturin develop --release")

    pages = sorted(glob.glob(os.path.join(os.path.expanduser(corpus_dir), "*", "pages", "*.html")),
                   key=os.path.getsize)
    if not pages:
        raise SystemExit(f"no <dir>/*/pages/*.html under {corpus_dir}")
    if limit:  # a size spread, not the N smallest
        step = max(1, len(pages) // limit)
        pages = pages[::step][:limit]

    supported = {}
    cells = []
    for p in pages:
        sel_json = os.path.join(os.path.dirname(os.path.dirname(p)), "selectors.json")
        if not os.path.exists(sel_json):
            continue
        with open(sel_json) as f:
            raw = [q for q in json.load(f).values() if isinstance(q, str) and q.strip()]
        sels = []
        for q in raw:
            if q not in supported:
                try:
                    supported[q] = not frostwork.check([q]).unsupported
                except Exception:
                    supported[q] = False
            if supported[q]:
                sels.append(q)
        if not sels or not os.path.getsize(p):
            continue
        cells.append((f"{os.path.basename(os.path.dirname(os.path.dirname(p)))[:34]}", p, sels))
    return cells


def corpus_sweep(corpus_dir, limit, a, b, iters, reps, out):
    cells = corpus_cells(corpus_dir, limit)
    print(f"\n### real corpus: {corpus_dir}  ({len(cells)} pages, own selectors)")
    print(f"  {'KB':>6} {'sels':>4} | {'A µs':>10} {'B µs':>10} | {'delta':>8} {'jitter':>7} | page object")
    print("  " + "-" * 78)
    for label, path, sels in cells:
        ma, mb, jit, va, vb = cell(a, b, path, sels, iters, reps)
        flag = "" if va == vb else f"  ** VALUE MISMATCH A={va} B={vb} **"
        pct = (mb - ma) / ma * 100.0
        mark = " " if abs(pct) > jit else "~"
        print(f"  {os.path.getsize(path)/1024:>6.0f} {len(sels):>4} | {ma:>10.1f} {mb:>10.1f} | "
              f"{pct:>+7.1f}%{mark}{jit:>6.1f}% | {label}{flag}")
        out.append((label, len(sels), ma, mb, pct, va == vb, jit))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="baseline bench binary")
    ap.add_argument("--b", required=True, help="candidate bench binary")
    ap.add_argument("--reps", type=int, default=3, help="interleaved repetitions per cell (min taken)")
    ap.add_argument("--iters", type=int, default=1200, help="pages per bench invocation")
    ap.add_argument("--tables", default=",".join(ALL), help=f"comma list of {ALL}")
    ap.add_argument("--counts", default="", help="override each table's selector counts, e.g. 8,16,32")
    ap.add_argument("--corpus", default=None,
                    help="A/B over a REAL page corpus (each page with its own selectors) instead of "
                         "the generated tables — the workload the corpus number is measured on")
    ap.add_argument("--limit", type=int, default=40, help="corpus mode: size-spread sample of N pages")
    args = ap.parse_args()

    which = [w.strip() for w in args.tables.split(",") if w.strip()]
    bad = [w for w in which if w not in ALL]
    if bad and not args.corpus:
        raise SystemExit(f"unknown table(s) {bad}; choose from {ALL}")

    print("=" * 72)
    print(f"A/B interleaved  reps={args.reps} iters={args.iters}  (min-of-reps per side)")
    print(f"  A = {args.a}")
    print(f"  B = {args.b}")
    print("  delta < 0  =>  B (candidate) is FASTER")
    print("=" * 72)

    override = [int(c) for c in args.counts.split(",") if c.strip()] if args.counts else None
    out = []
    if args.corpus:
        corpus_sweep(args.corpus, args.limit, args.a, args.b, args.iters, args.reps, out)
    else:
        for name, html, counts, pool in tables(which):
            sweep(name, html, override or counts, pool, args.a, args.b, args.iters, args.reps, out)

    print("\n" + "=" * 72)
    ok = all(same for *_, _, same, _ in out)
    deltas = [pct for *_, pct, _, _ in out]
    solid = [pct for *_, pct, _, jit in out if abs(pct) > jit]
    print(f"cells={len(out)}  median delta={statistics.median(deltas):+.1f}%  "
          f"best={min(deltas):+.1f}%  worst={max(deltas):+.1f}%")
    print(f"  above own jitter: {len(solid)}/{len(out)} cells"
          + (f"  (median {statistics.median(solid):+.1f}%)" if solid else "")
          + "\n  cells marked ~ are inside their own jitter and are not evidence either way."
          + "\n  To calibrate: run with --a and --b set to the SAME binary; every cell should read ~.")
    if not ok:
        print("VALUE MISMATCH in at least one cell -- the two builds do not agree on what they "
              "extract. Timing is meaningless until that is fixed.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
