"""Exercise the actual Make recipe: a passing detector is useless if its exit status gets lost."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == 'nt' or shutil.which('make') is None, reason='local POSIX Make gate')
@pytest.mark.parametrize('failure', [None, 'cargo-mutant', 'extension-mutant', 'detector',
                                    'cargo-normal', 'extension-normal'])
def test_mutation_gate_propagates_failure_and_restores_both_builds(tmp_path, failure):
    for name in ('Makefile', 'pyproject.toml'):
        shutil.copyfile(ROOT / name, tmp_path / name)
    runner = tmp_path / 'runner.py'
    runner.write_text(
        'import os, sys\n'
        'from pathlib import Path\n'
        'kind = sys.argv[1]\n'
        'if kind != "detector":\n'
        '    kind += "-mutant" if any("mutate" in arg for arg in sys.argv[2:]) else "-normal"\n'
        'with Path(os.environ["COMMAND_LOG"]).open("a") as log: log.write(kind + "\\n")\n'
        'raise SystemExit(7 if kind == os.environ.get("FAIL_COMMAND") else 0)\n',
        encoding='utf-8',
    )
    cargo = tmp_path / 'cargo'
    cargo.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(runner), 'cargo']) + ' "$@"\n')
    cargo.chmod(0o755)
    log = tmp_path / 'commands.log'
    env = {**os.environ, 'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
           'COMMAND_LOG': str(log), 'FAIL_COMMAND': failure or ''}
    run = subprocess.run(
        ['make', 'gate-mutate', 'PY=' + shlex.join([sys.executable, str(runner), 'detector']),
         'MATURIN=' + shlex.join([sys.executable, str(runner), 'extension'])],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    commands = log.read_text().splitlines()
    assert (run.returncode == 0) == (failure is None), run.stdout + run.stderr
    # Cleanup must attempt both restorations even when setup, the gate or the first restoration fails.
    assert commands[-2:] == ['cargo-normal', 'extension-normal']
    if failure not in ('cargo-mutant', 'extension-mutant'):
        assert 'detector' in commands
