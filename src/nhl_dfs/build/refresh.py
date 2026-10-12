"""Refresh a run's slate without a new DK export (card C2c).

Uses the same lock rules and the same re-solve as late swap (late_swap.swap_core, fast repair).
With no current export, the parent is the LAST DELIVERED version, stated as an assumed parent:
it may not match what is actually entered on DK, so the run is DEGRADED_REVIEW and the notes say
so. Re-fetches draftables (participation, eligibility, start times, swappability) when online.

C9: refresh resolves its objective even when nothing needs a repair: it rebuilds the ParamTable, applies
the role state once (Daily Faceoff pages, cache first; stored pages offline), re-fetches odds when
online, re-simulates only the games whose inputs changed and have not started, and writes the updated
draws to its own run's scenario/ folder, so a later late swap from this run starts from them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from nhl_dfs.build.assign import Caps
from nhl_dfs.build.late_swap import last_delivered, swap_core
from nhl_dfs.build.manifest import read_manifest
from nhl_dfs.build.run import RunResult
from nhl_dfs.build.state import open_run


class NoDeliveredVersion(Exception):
    pass


def run(
    run_id: str,
    *,
    offline: bool,
    runs_root="runs",
    outputs_root=None,
    clock: Callable[[], datetime] | None = None,
    cache=None,
    salary_path=None,
    runtime: dict | None = None,
    caps: Caps | None = None,
    as_of: datetime | None = None,
    objective: str = "auto",
    standings=None,
    apply_state=None,
) -> RunResult:
    runs_root = Path(runs_root)
    outputs_root = Path(outputs_root) if outputs_root is not None else runs_root.parent / "outputs"
    if as_of is not None:
        clock = lambda: as_of  # noqa: E731
    clock = clock or (lambda: datetime.now(timezone.utc))
    parent = open_run(runs_root, run_id)
    slate_id = read_manifest(parent)["slate_id"]
    delivered = last_delivered(runs_root, outputs_root, slate_id, parent)
    if delivered is None:
        raise NoDeliveredVersion(f"run {run_id} has no delivered version to refresh")
    holder = _delivery_run(runs_root, outputs_root, slate_id, parent)
    return swap_core(parent, delivered, kind="refresh", offline=offline, fast=True, assumed_parent=True,
                     runs_root=runs_root, outputs_root=outputs_root, clock=clock, cache=cache,
                     salary_path=salary_path, runtime=runtime, caps=caps, rehearsal=as_of, objective=objective,
                     standings=standings, eager_objective=True, apply_state=apply_state, fetch_odds=True,
                     overrides_run=holder)


def _delivery_run(runs_root: Path, outputs_root: Path, slate_id: str, parent):
    """The run that published the slate's current file when it is not `parent` (a late swap delivered after the run
    being refreshed), else None: its accepted overrides, Ben's report included, are the ones in force (C53 review)."""
    import json

    try:
        s = json.loads((outputs_root / slate_id / "published.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rid = s.get("run_id")
    if not rid or rid == parent.run_id or not (runs_root / rid).is_dir():
        return None
    return open_run(runs_root, rid)
