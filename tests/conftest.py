import sys
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

MINI = TESTS / "fixtures" / "mini"
REAL = TESTS / "fixtures" / "real"


def real_pair(mode: str) -> tuple[Path, Path]:
    """Latest real (DKSalaries.csv, DKEntries.csv) for a mode; skips loudly when absent."""
    dirs = sorted(p.parent for p in REAL.glob(f"*/{mode}/DKSalaries.csv") if (p.parent / "DKEntries.csv").exists())
    if not dirs:
        msg = f"REAL FIXTURE MISSING: tests/fixtures/real/<date>/{mode}/DKSalaries.csv + DKEntries.csv"
        warnings.warn(msg)
        pytest.skip(msg)
    return dirs[-1] / "DKSalaries.csv", dirs[-1] / "DKEntries.csv"


def mini_pair(mode: str) -> tuple[Path, Path]:
    return MINI / mode / "DKSalaries.csv", MINI / mode / "DKEntries.csv"
