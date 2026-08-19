"""Reproducible multi-million-pair correctness soak.

Runs the clean differential and support-aware selector fuzzer across several independent seeds, then
one larger malformed-HTML crash soak. The default workload is intentionally above four million total
page/query pairs while remaining practical on a developer machine.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "bin" / "python"


def run(args: list[str]) -> int:
    print("\n$ " + " ".join(args), flush=True)
    proc = subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(proc.stdout, end="", flush=True)
    if proc.returncode:
        raise SystemExit(proc.returncode)
    matches = re.findall(r"\bpairs=(\d+)", proc.stdout)
    return int(matches[-1]) if matches else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="run Frostwork's multi-million differential soak")
    ap.add_argument("--seeds", type=int, default=5, help="number of independent clean/fuzzer seeds")
    ap.add_argument("--selector-iters", type=int, default=12_000, help="selectors per seed")
    ap.add_argument("--malformed-iters", type=int, default=12_000, help="malformed mutations (one seed)")
    args = ap.parse_args()

    if not PY.exists():
        raise SystemExit("missing .venv; run `make bootstrap` first")

    total = 0
    for seed in range(args.seeds):
        total += run([str(PY), "tools/diff_lxml.py", "--seed", str(seed)])
        total += run([
            str(PY), "tools/sel_fuzz.py", "--seed", str(seed),
            "--iters", str(args.selector_iters), "--gate",
        ])
    total += run([
        str(PY), "tools/diff_fuzz.py", "--seed", "31337",
        "--iters", str(args.malformed_iters), "--gate",
    ])
    print(f"\nSOAK PASS: {total:,} total page/query pairs across {args.seeds} clean/fuzzer seeds")


if __name__ == "__main__":
    main()
