"""Rebuild tests/fixtures/c41/graded_ownership.csv and contests.json from the local settles (C41, flag 119).

Every run with `settle/grades.json` names its forecast run and the standings file of each contest it graded. For each large_gpp contest
this rebuilds, with the settle's own code (`learn.grade_ownership.forecast_from_run`, `learn.standings.read` and `join`), the role set
`grade` scores (the forecast's whole pool plus anything drafted; drafted roles only when the export left a zero unobserved) and writes
each role's forecast and actual ownership at full precision (repr), one row per contest and role; `contests.json` names each
contest's forecast run, graded run, family and mode. No names, lineups or entries: role ids and percentages only.

    python tests/fixtures/c41/make_fixture.py [--runs-root runs] [--out-dir tests/fixtures/c41]

The runs and standings are Ben's local, gitignored files; without them it says so and writes nothing.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
FIELDS = ("contest_id", "role_id", "pred_pct", "actual_pct")


def rows(runs_root: Path, families: tuple[str, ...] = ("large_gpp",)) -> list[dict]:
    from nhl_dfs.build.run import load_run_pool
    from nhl_dfs.build.state import open_run
    from nhl_dfs.learn import grade_ownership, standings

    out = []
    for g in sorted(runs_root.glob("*/settle/grades.json")):
        rec = json.loads(g.read_text(encoding="utf-8"))
        own = {o["contest_id"]: o for o in rec.get("ownership") or []}
        if not own:
            continue
        run = open_run(runs_root, rec["run_id"])
        frun = open_run(runs_root, rec["forecast"]["run_id"])
        pool = load_run_pool(run)[0]
        for cid, src in sorted(rec["standings"]["sources"].items()):
            if cid not in own or own[cid]["family"] not in families:
                continue
            f = grade_ownership.forecast_from_run(frun, cid, pool)
            j = standings.join(standings.read(REPO / src), pool)
            roles = sorted(set(f.own) | set(j.own)) if j.complete else sorted(set(j.own))
            for r in roles:
                out.append({"graded_run": run.run_id, "forecast_run": frun.run_id, "contest_id": str(cid),
                            "family": own[cid]["family"], "mode": pool.mode.value, "role_id": r,
                            "pred_pct": repr(float(f.own.get(r, 0.0))), "actual_pct": repr(float(j.own.get(r, 0.0)))})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-root", default=str(REPO / "runs"))
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent))
    a = ap.parse_args(argv)
    got = rows(Path(a.runs_root))
    if not got:
        print(f"NOT WRITTEN: no settled run with graded large_gpp ownership under {a.runs_root} (Ben's local files)")
        return 1
    out = Path(a.out_dir)
    contests = {r["contest_id"]: {k: r[k] for k in ("forecast_run", "graded_run", "family", "mode")} for r in got}
    (out / "contests.json").write_text(json.dumps(contests, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    with open(out / "graded_ownership.csv", "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        w.writerows(got)
    print(f"wrote {len(got)} rows, {len(contests)} contests, to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
