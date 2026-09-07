"""No undefined names anywhere in the source tree.

A `NameError` in a report only shows itself when someone opens that page
against a real database, so it survives a green test suite and reaches
production as a 500. That is exactly how `trending_queries` shipped with a
stray `.offset(start)` — `start` was never defined in that function, every
unit test passed, and the console's trending panel returned
`server/internal` for every request.

Pyflakes finds the whole class in under a second, so there is no reason for
the next one to reach a user.
"""
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"


def test_no_undefined_names():
    result = subprocess.run(
        [sys.executable, "-m", "pyflakes", str(SRC)],
        capture_output=True,
        text=True,
    )
    # Pyflakes reports plenty this codebase tolerates (unused imports kept for
    # re-export, star imports). Only undefined names are errors here: every one
    # of them is a crash waiting for the right request.
    undefined = [
        line for line in result.stdout.splitlines()
        if "undefined name" in line
    ]
    assert not undefined, "undefined names will raise NameError at runtime:\n" + "\n".join(undefined)
