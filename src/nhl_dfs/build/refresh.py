"""Refresh a run's slate without a new DK export (card C2c).

Uses the same lock rules and the same re-solve as late swap (late_swap.swap_core, fast repair).
With no current export, the parent is the LAST DELIVERED version, stated as an assumed parent:
it may not match what is actually entered on DK, so the run is DEGRADED_REVIEW and the notes say
so. Re-fetches draftables (participation, eligibility, start times, swappability) when online.
Odds are not re-fetched: nothing consumes them before the model chunks (C5/C6).
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
) -> RunResult:
    runs_root = Path(runs_root)
    outputs_root = Path(outputs_root) if outputs_root is not None else runs_root.parent / "outputs"
    if as_of is not None:
        clock = lambda: as_of  # noqa: E731
    clock = clock or (lambda: datetime.now(timezone.utc))
    parent = open_run(runs_root, run_id)
    delivered = last_delivered(runs_root, outputs_root, read_manifest(parent)["slate_id"], parent)
    if delivered is None:
        raise NoDeliveredVersion(f"run {run_id} has no delivered version to refresh")
    return swap_core(parent, delivered, kind="refresh", offline=offline, fast=True, assumed_parent=True,
                     runs_root=runs_root, outputs_root=outputs_root, clock=clock, cache=cache,
                     salary_path=salary_path, runtime=runtime, caps=caps, rehearsal=as_of)
