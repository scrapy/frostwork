"""
Peak-memory + parse-time benchmark for the Frostwork engine: one streaming pass, no DOM, schema
compiled once (what `Page`/`FrostPage` do) vs Parsel (parse once into an lxml tree, then one query
per field). Both sides are measured in their real reuse pattern.

Why this exists: the throughput matrix (bench_matrix.py) proves Frostwork is *fast*, but its
defining property — **no DOM** — is invisible without measuring memory. On a large page from which
you pull a few fields, Parsel must build the entire lxml tree (memory scales with page size) while
Frostwork streams past the filler and stays bounded. This bench makes that concrete.

`--engines` runs the same measurement over every adapter in `bench_engines.py`, because "no DOM" is
a claim against the whole field and not just against Parsel: lexbor's tree is markedly leaner than
libxml2's, which is exactly the kind of thing a throughput-only comparison hides.

Measurement is process-level **peak RSS** (resource.getrusage.ru_maxrss), not tracemalloc: Frostwork
allocates in Rust and Parsel in C (libxml2), both invisible to tracemalloc. RSS is a process-wide
high-water mark, so engines can't share a process — each runs in its own subprocess. To isolate
parse/extract cost from interpreter + import + doc-bytes overhead, every engine is measured twice: a
`baseline` run (import engine, load the doc bytes, do nothing) and a `work` run (also parse +
extract). The reported figure is work_peak − baseline_peak. Parse time is best-of-3 within `work`.

IMPORTANT: build the extension in RELEASE first — `.venv/bin/maturin develop --release` — or the
time columns measure a debug build (~10x slower than release, and unfair vs optimized libxml2). The
RSS columns are unaffected.

Usage:
  .venv/bin/python tools/bench_mem.py                        # synthetic size sweep 1,4,16,64 MB
  .venv/bin/python tools/bench_mem.py 1,4,16,64,256          # custom sizes (MB)
  .venv/bin/python tools/bench_mem.py --real <corpus_dir> [n_docs=8]   # N largest REAL pages
  .venv/bin/python tools/bench_mem.py --engines <corpus_dir> [n=12]    # EVERY competitor (bench_engines)
"""
from __future__ import annotations

import glob
import json
import os
import resource
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
RESULTS = os.path.join(HERE, "results")

# ru_maxrss is bytes on macOS, kilobytes on Linux.
_RSS_UNIT = 1 if sys.platform == "darwin" else 1024


def peak_rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * _RSS_UNIT


def is_xpath(sel):
    s = sel.strip()
    return s.startswith("/") or s.startswith("./")


# A fixed, tiny selection for the synthetic sweep — the number of matched values does NOT grow with
# page size, so any memory growth is pure parser overhead.
SWEEP_FIELDS = ["title::text", "h1.product-name::text", "span.price::text"]


def make_doc(target_bytes):
    """A page with a tiny amount of real content up top, padded with filler markup to ~target_bytes."""
    head = ('<!doctype html><html><head><title>Widget</title></head><body>'
            '<main><h1 class="product-name">The Widget</h1>'
            '<span class="price">$19.99</span></main><div class="filler">')
    tail = "</div></body></html>"
    block = ('<div class="row"><p>lorem ipsum dolor sit amet consectetur adipiscing elit sed do '
             'eiusmod tempor incididunt ut labore et dolore</p><span>filler text node</span></div>')
    parts, n = [head], len(head) + len(tail)
    while n < target_bytes:
        parts.append(block)
        n += len(block)
    parts.append(tail)
    return "".join(parts).encode()


# ---------------------------------------------------------------------------------- child (worker)
def _selectors_for(doc_path, sel_json):
    """The selectors this doc is measured with: its page-object's selectors.json (real mode) or the
    fixed synthetic set."""
    if sel_json and os.path.exists(sel_json):
        with open(sel_json) as f:
            return list(json.load(f).values())
    return SWEEP_FIELDS


def _child(engine, doc_path, mode, sel_json):
    with open(doc_path, "rb") as f:
        body = f.read()  # held in both modes so the doc-bytes offset cancels in the RSS delta
    queries = _selectors_for(doc_path, sel_json)

    if engine == "frostwork":
        import frostwork
        from frostwork._frostwork import Plan

        # Compiled ONCE, outside `work`, for the same reason the throughput benches do it: a page
        # object's selectors are compiled at class-definition time, not per response. It also keeps the
        # measurement honest in the direction that matters here — the plan is a fixed per-SCHEMA cost, so
        # counting it inside the per-page peak would attribute schema memory to page size, which is the
        # exact claim this file exists to test.
        frostwork.check(list(queries)).raise_for_status()
        plan = Plan(list(queries), [])

        def work():
            return plan.extract(body, "utf-8")
    elif engine == "parsel":
        import parsel

        def work():
            # No batch API: parse once (build the tree), then one cheap query per field — the
            # realistic spider pattern of reusing a single Selector.
            sel = parsel.Selector(body=body, encoding="utf-8")
            cols = []
            for q in queries:
                try:
                    cols.append((sel.xpath(q) if is_xpath(q) else sel.css(q)).getall())
                except Exception:
                    cols.append([])
            return cols
    else:
        # Competitive mode: any adapter from the bench_engines registry. The registry import and the
        # engine's own lazy library import both happen HERE, before the mode branch, so they land in
        # the baseline run too and cancel out of the delta — otherwise selectolax's module would be
        # charged to lexbor's DOM.
        import bench_engines

        by_key = {e.key: e for e in bench_engines.ENGINES}
        if engine not in by_key:
            raise SystemExit(f"unknown engine {engine!r}")
        eng = by_key[engine]
        eng.run(b"<html><p>x</p></html>", [])

        def work():
            return eng.run(body, queries)

    secs = 0.0
    keep = None
    if mode == "work":
        best = float("inf")
        for _ in range(3):
            t = time.perf_counter()
            keep = work()
            best = min(best, time.perf_counter() - t)
        secs = best
    print(json.dumps({"rss": peak_rss(), "secs": secs, "len": len(body), "keep": bool(keep)}))


# ----------------------------------------------------------------------------------------- parent
def measure(engine, doc_path, sel_json=""):
    """-> (parse_rss_delta_bytes, best_secs) for one engine/doc, via 2 subprocesses."""
    def run(mode):
        out = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--child", engine, doc_path, mode, sel_json],
            capture_output=True, text=True)
        if out.returncode:
            raise RuntimeError(f"{engine}/{mode} on {doc_path}:\n{out.stderr.strip()}")
        return json.loads(out.stdout.strip().splitlines()[-1])
    base = run("baseline")["rss"]
    w = run("work")
    return max(0, w["rss"] - base), w["secs"]


def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


def run_sweep(sizes_mb):
    import tempfile
    print("Selective extraction (3 fixed fields) vs page size — peak RSS attributable to parsing, "
          "and best-of-3 parse+extract time.\n")
    print(f"{'page':>7} | {'parsel RSS':>11} {'frost RSS':>10} {'leaner':>7} | "
          f"{'parsel':>9} {'frost':>8} {'faster':>7}")
    print("-" * 66)
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for mb in sizes_mb:
            p = os.path.join(td, f"d{mb}.html")
            with open(p, "wb") as f:
                f.write(make_doc(mb * 1024 * 1024))
            pr, ps = measure("parsel", p)
            fr, fs = measure("frostwork", p)
            rows.append({"mb": mb, "parsel_rss_mb": pr / 1048576, "frost_rss_mb": fr / 1048576,
                         "parsel_ms": ps * 1000, "frost_ms": fs * 1000})
            print(f"{mb:>5}MB | {pr/1048576:>8.1f}MB {fr/1048576:>7.1f}MB "
                  f"{pr/fr if fr else 0:>5.1f}x | "
                  f"{ps*1000:>7.1f}ms {fs*1000:>6.1f}ms {ps/fs if fs else 0:>5.1f}x")
    return {"mode": "sweep", "fields": SWEEP_FIELDS, "rows": rows, "platform": sys.platform}


def run_real(corpus_dir, n_docs):
    corpus_dir = os.path.abspath(corpus_dir)
    all_pages = glob.glob(os.path.join(corpus_dir, "*", "pages", "*.html"))
    if not all_pages:
        raise SystemExit(f"no <dir>/*/pages/*.html under {corpus_dir}")
    paths = sorted(all_pages, key=os.path.getsize, reverse=True)[:n_docs]
    print(f"{len(paths)} largest real pages from {corpus_dir}\n")
    print(f"{'page':>8} {'nsel':>4} | {'parsel RSS':>11} {'frost RSS':>10} {'leaner':>7} | "
          f"{'parsel':>9} {'frost':>8} {'faster':>7}")
    print("-" * 74)
    rows = []
    for p in paths:
        sel_json = os.path.join(os.path.dirname(os.path.dirname(p)), "selectors.json")
        nsel = len(_selectors_for(p, sel_json))
        size_mb = os.path.getsize(p) / 1048576
        pr, ps = measure("parsel", p, sel_json)
        fr, fs = measure("frostwork", p, sel_json)
        rows.append({"page": os.path.relpath(p, corpus_dir), "mb": size_mb, "nsel": nsel,
                     "parsel_rss_mb": pr / 1048576, "frost_rss_mb": fr / 1048576,
                     "parsel_ms": ps * 1000, "frost_ms": fs * 1000})
        print(f"{size_mb:>6.1f}MB {nsel:>4} | {pr/1048576:>8.1f}MB {fr/1048576:>7.1f}MB "
              f"{pr/fr if fr else 0:>5.1f}x | "
              f"{ps*1000:>7.1f}ms {fs*1000:>6.1f}ms {ps/fs if fs else 0:>5.1f}x")
    # medians across the sampled pages
    if rows:
        print("-" * 74)
        print(f"{'median':>8} {'':>4} | "
              f"{_median([r['parsel_rss_mb'] for r in rows]):>8.1f}MB "
              f"{_median([r['frost_rss_mb'] for r in rows]):>7.1f}MB "
              f"{_median([r['parsel_rss_mb']/r['frost_rss_mb'] for r in rows if r['frost_rss_mb']]):>5.1f}x | "
              f"{_median([r['parsel_ms'] for r in rows]):>7.1f}ms "
              f"{_median([r['frost_ms'] for r in rows]):>6.1f}ms "
              f"{_median([r['parsel_ms']/r['frost_ms'] for r in rows if r['frost_ms']]):>5.1f}x")
    return {"mode": "real", "corpus": corpus_dir, "rows": rows, "platform": sys.platform}


def run_engines(corpus_dir, n_docs):
    """Peak RSS for EVERY engine in the competitive registry, over the N largest real pages.

    A throughput chart cannot show Frostwork's defining property, and it is the property the fast
    competitor does not share: lexbor's DOM is compact, but it is still a DOM and still O(page).
    Every engine is measured on the SAME selectors — the ones all of them can express — so the column
    that differs is the tree, not the workload.
    """
    import tempfile

    import bench_engines

    corpus_dir = os.path.abspath(os.path.expanduser(corpus_dir))
    all_pages = glob.glob(os.path.join(corpus_dir, "*", "pages", "*.html"))
    if not all_pages:
        raise SystemExit(f"no <dir>/*/pages/*.html under {corpus_dir}")
    for label, reason in bench_engines.unavailable_report(bench_engines.ENGINES):
        print(f"SKIPPED ENGINE: {label} — {reason}")
    engines = [e for e in bench_engines.ENGINES if not e.unavailable()]
    paths = sorted(all_pages, key=os.path.getsize, reverse=True)[:n_docs]

    print(f"\n{len(paths)} largest real pages from {corpus_dir}")
    print("peak RSS attributable to parse+extract (work − baseline), best-of-3 time\n")
    print(f"{'page':>8} {'nsel':>4} | " + " ".join(f"{e.label[:13]:>13}" for e in engines))
    print("-" * (16 + 14 * len(engines)))
    rows = []
    with tempfile.TemporaryDirectory() as td:
        for p in paths:
            sel_json = os.path.join(os.path.dirname(os.path.dirname(p)), "selectors.json")
            queries = [q for q in _selectors_for(p, sel_json) if isinstance(q, str) and q.strip()]
            shared = [q for q in queries if all(e.expressible(q) for e in engines)]
            if not shared:
                continue
            shared_json = os.path.join(td, "shared.json")
            with open(shared_json, "w") as f:
                json.dump({str(i): q for i, q in enumerate(shared)}, f)
            row = {"page": os.path.relpath(p, corpus_dir), "mb": os.path.getsize(p) / 1048576,
                   "nsel": len(shared), "engines": {}}
            for e in engines:
                rss, secs = measure(e.key, p, shared_json)
                row["engines"][e.key] = {"rss_mb": rss / 1048576, "ms": secs * 1000}
            rows.append(row)
            print(f"{row['mb']:>6.1f}MB {len(shared):>4} | "
                  + " ".join(f"{row['engines'][e.key]['rss_mb']:>11.1f}MB" for e in engines))
    if rows:
        print("-" * (16 + 14 * len(engines)))
        print(f"{'median':>8} {'':>4} | " + " ".join(
            f"{_median([r['engines'][e.key]['rss_mb'] for r in rows]):>11.1f}MB" for e in engines))
        print(f"{'ms':>8} {'':>4} | " + " ".join(
            f"{_median([r['engines'][e.key]['ms'] for r in rows]):>11.1f}ms" for e in engines))
    return {"mode": "engines", "corpus": corpus_dir, "rows": rows,
            "engines": [e.key for e in engines], "platform": sys.platform}


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--child":
        _, engine, doc_path, mode, sel_json = (argv + [""])[:5]
        return _child(engine, doc_path, mode, sel_json)

    if argv and argv[0] == "--engines":
        result = run_engines(argv[1], int(argv[2]) if len(argv) > 2 else 12)
        fname = "membench_engines.json"
        os.makedirs(RESULTS, exist_ok=True)
        with open(os.path.join(RESULTS, fname), "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nwrote {os.path.join('tools/results', fname)}")
        return

    if argv and argv[0] == "--real":
        corpus_dir = argv[1]
        n_docs = int(argv[2]) if len(argv) > 2 else 8
        result = run_real(corpus_dir, n_docs)
        fname = "membench_real.json"
    else:
        sizes = [int(x) for x in (argv[0].split(",") if argv else ["1", "4", "16", "64"])]
        result = run_sweep(sizes)
        fname = "membench_sweep.json"

    os.makedirs(RESULTS, exist_ok=True)
    with open(os.path.join(RESULTS, fname), "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nwrote {os.path.join('tools/results', fname)}")


if __name__ == "__main__":
    main()
