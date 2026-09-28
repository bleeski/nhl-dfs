"""C2a exit benchmark: distinct candidates per mode from the real fixtures, with wall clock.

    python tools/bench_candidates.py [--n 150] [--seconds 10] [--date 2026-09-29]

For each mode, builds priors from the real DKSalaries.csv, generates n distinct candidates
(min pairwise difference 2), re-validates every one with check_lineup, and prints one line.
Exit code 0 only when both modes reach n distinct legal candidates within the limit.
Timing lives here, not in the pytest suite, so a slow machine cannot block later chunks.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    import scipy  # noqa: F401
    import yaml  # noqa: F401
except ModuleNotFoundError:  # run under the repo venv when started with a bare system python
    import subprocess

    for _venv_py in (REPO_ROOT / ".venv" / "Scripts" / "python.exe", REPO_ROOT / ".venv" / "bin" / "python"):
        if _venv_py.exists():
            sys.exit(subprocess.run([str(_venv_py), *sys.argv]).returncode)
    sys.stderr.write("scipy/pyyaml not importable and no .venv was found; run `uv sync` first.\n")
    sys.exit(1)
sys.path.insert(0, str(REPO_ROOT / "src"))

from nhl_dfs.build.candidates import generate  # noqa: E402
from nhl_dfs.build.milp import overlap_units  # noqa: E402
from nhl_dfs.contracts.geometry import check_lineup  # noqa: E402
from nhl_dfs.intake.salary import read_salary  # noqa: E402
from nhl_dfs.models.priors import prior_objective, prior_table  # noqa: E402

REAL = REPO_ROOT / "tests" / "fixtures" / "real"


def _salary_file(mode: str, date: str | None) -> Path | None:
    hits = sorted(REAL.glob(f"{date or '*'}/{mode}/DKSalaries.csv"))
    return hits[-1] if hits else None


def bench(mode: str, n: int, limit_s: float, date: str | None, seed: int) -> bool:
    path = _salary_file(mode, date)
    if path is None:
        print(f"{mode}: REAL FIXTURE MISSING under tests/fixtures/real/{date or '<date>'}/{mode}/")
        return False
    pool = read_salary(path)
    objective = prior_objective(pool, prior_table(pool))
    t0 = time.perf_counter()
    cands = generate(pool, pool.mode, objective, n, seed=seed, perturb_sd=2.0, time_limit_total_s=limit_s)
    elapsed = time.perf_counter() - t0
    legal = sum(check_lineup([pool.by_role_id[r] for r in c.role_ids], pool.mode).ok for c in cands)
    distinct = len({c.key for c in cands})
    units = [overlap_units([pool.by_role_id[r] for r in c.role_ids], pool.mode) for c in cands]
    size = len(cands[0].role_ids) if cands else 0
    min_diff = min((size - len(a & b) for i, a in enumerate(units) for b in units[i + 1:]), default=0)
    ok = distinct >= n and legal == len(cands) and elapsed <= limit_s and min_diff >= 2
    print(
        f"{mode}: {distinct} distinct / {len(cands)} generated, {legal} legal, min pairwise diff {min_diff}, "
        f"{elapsed:.2f}s (limit {limit_s:.0f}s), pool {len(pool.rows)} rows, {path.parent.parent.name} "
        f"-> {'PASS' if ok else 'FAIL'}"
    )
    return ok


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--date", default=None, help="fixture date folder; default is the latest")
    ap.add_argument("--seed", type=int, default=20260929)
    args = ap.parse_args(argv)
    results = [bench(mode, args.n, args.seconds, args.date, args.seed) for mode in ("classic", "showdown")]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
