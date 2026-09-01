"""Does the type checker see what actually happens at runtime?

`frostwork` ships `py.typed`, which makes its annotations a promise to every user's type checker — and the
promise was broken in the most visible place. `field()` returned the internal marker class and nothing
replaced that annotation, so `page.name` typed as `_FrostField` and *correct* code was an error in the
user's CI:

    x: str = page.name       # error: Incompatible types (expression has type "_FrostField")

No test could catch that, because every test here runs the code rather than checking it. So this file runs
mypy over `tests/typing_fixture.py`, whose `assert_type(...)` calls state the expected static type of every
declaration form, and requires zero errors.

The second test is the one that makes this a gate rather than a decoration: it seeds a WRONG expectation
into a copy of the fixture and asserts mypy rejects it. A type check that passes no matter what the
annotations say is worth nothing.
"""

from pathlib import Path

import pytest

pytest.importorskip("mypy", reason="mypy is pinned in requirements-test.txt")
pytest.importorskip("web_poet")

FIXTURE = Path(__file__).parent / "typing_fixture.py"


def _run_mypy(target: Path):
    """`(stdout, exit_code)`. `--no-incremental` because these runs deliberately edit the same module and a
    warm cache would answer for the previous copy."""
    from mypy import api

    stdout, _stderr, code = api.run([str(target), "--no-incremental", "--no-error-summary"])
    return stdout, code


def test_page_object_annotations_match_runtime():
    """Every declaration form in the fixture types as the value it actually produces."""
    stdout, code = _run_mypy(FIXTURE)
    assert code == 0, f"mypy rejected the typing fixture:\n{stdout}"


def test_the_typing_gate_can_go_red(tmp_path):
    """Seed the exact regression this file exists for — a field annotated as the marker class instead of
    its value type — and require mypy to catch it."""
    src = FIXTURE.read_text()
    seeded = src.replace(
        "    assert_type(p.name, Optional[str])\n    assert_type(p.images, List[str])",
        "    assert_type(p.name, int)  # SEEDED: wrong on purpose\n    assert_type(p.images, List[str])",
        1,
    )
    assert seeded != src, "the seed did not apply — has the fixture been reworded?"
    target = tmp_path / "typing_fixture.py"
    target.write_text(seeded)

    stdout, code = _run_mypy(target)
    assert code != 0, f"mypy accepted a deliberately wrong assert_type:\n{stdout}"
    assert "Expression is of type" in stdout or "assert_type" in stdout, stdout


def test_a_field_is_not_annotated_as_the_marker_class(tmp_path):
    """The precise pre-fix symptom, stated as its own case: assigning a field's value to its declared type
    must be legal. If `field()` ever regresses to leaking `_FrostField`, this is the line that fails."""
    target = tmp_path / "assignment.py"
    target.write_text(
        "from typing import List, Optional\n"
        "from frostwork.webpoet import FrostPage, field\n"
        "class P(FrostPage):\n"
        "    name = field('h1::text')\n"
        "    imgs = field('img::attr(src)', all=True)\n"
        "    joined = field('.s ::text', join=' ')\n"
        "def use(p: P) -> None:\n"
        "    a: Optional[str] = p.name\n"
        "    b: List[str] = p.imgs\n"
        "    c: str = p.joined\n"
        "    del a, b, c\n"
    )
    stdout, code = _run_mypy(target)
    assert code == 0, f"assigning a field's value to its own type was rejected:\n{stdout}"
