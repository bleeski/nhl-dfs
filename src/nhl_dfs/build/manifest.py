"""manifest.json: the machine-readable record of one run (card C2b).

Required fields: run_id, created_utc, mode, slate_id, salary_sha256, entries_sha256,
export_sha256, statuses (the eight report statuses), entry_count, exposures_top20,
relaxations, phase_timings, versions. Extra keys carry detail for RUN_NOTES and verify.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from nhl_dfs.build.assign import Assignment
from nhl_dfs.build.state import RunDir, atomic_write
from nhl_dfs.intake.salary import SalaryPool

STATUS_KEYS = (
    "FILE_VALID",
    "NEWS_STATE",
    "MODEL_STATUS",
    "SEARCH_STATUS",
    "DELIVERY_STATUS",
    "PAYOUT_SOURCE",
    "OUTCOME_CALIBRATION",
    "FIELD_CALIBRATION",
)
REQUIRED_KEYS = (
    "run_id",
    "created_utc",
    "mode",
    "slate_id",
    "salary_sha256",
    "entries_sha256",
    "export_sha256",
    "statuses",
    "entry_count",
    "exposures_top20",
    "relaxations",
    "phase_timings",
    "versions",
)


def exposures_top20(assignment: Assignment, pool: SalaryPool) -> list[dict[str, Any]]:
    """The 20 most-used people: name, team, entries, share, and Captain count (Showdown)."""
    n = max(1, len(assignment.by_entry))
    names: dict[str, tuple[str, str]] = {}
    for r in pool.rows:
        names.setdefault(r.person_key, (r.name, r.team))
    top = Counter(assignment.person_exposures).most_common()
    top.sort(key=lambda kv: (-kv[1], kv[0]))
    out = []
    for pk, count in top[:20]:
        name, team = names.get(pk, (pk, ""))
        row = {"person_key": pk, "name": name, "team": team, "entries": count, "share": round(count / n, 4)}
        if assignment.captain_exposures:
            row["captain"] = assignment.captain_exposures.get(pk, 0)
        out.append(row)
    return out


def write_manifest(run: RunDir, manifest: dict[str, Any]) -> Path:
    missing = [k for k in REQUIRED_KEYS if k not in manifest]
    if missing:
        raise ValueError(f"manifest missing {missing}")
    missing_status = [k for k in STATUS_KEYS if k not in manifest["statuses"]]
    if missing_status:
        raise ValueError(f"manifest statuses missing {missing_status}")
    path = run.path / "manifest.json"
    atomic_write(path, (json.dumps(manifest, indent=2, sort_keys=False) + "\n").encode("utf-8"))
    return path


def read_manifest(run: RunDir) -> dict[str, Any]:
    return json.loads((run.path / "manifest.json").read_text(encoding="utf-8"))
