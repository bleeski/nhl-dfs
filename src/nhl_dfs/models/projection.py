"""The projection interface (C3). Priors satisfy it now; per-person params (C5) will later.

mean_tenths and sd_tenths are the person's UNCAPTAINED expectation and spread, in integer
tenths, for any selectable role_id: a Showdown CPT row returns the same values as its FLEX row.
The Captain's 1.5x is applied once, in lineup_mean_tenths / lineup_sd_tenths (and in the
solver objective, models.priors.prior_objective), never inside a Projection.
"""

from __future__ import annotations

import math
from typing import Iterable, Protocol

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.statuses import ModelStatus
from nhl_dfs.intake.salary import SalaryPool
from nhl_dfs.models.priors import CAPTAIN_MULTIPLIER, Prior, prior_table


class Projection(Protocol):
    def mean_tenths(self, role_id: str) -> int: ...

    def sd_tenths(self, role_id: str) -> int: ...

    def source(self) -> ModelStatus: ...


class PriorProjection:
    """Projection backed by models.priors.prior_table (MODEL_STATUS=PRIOR)."""

    def __init__(self, pool: SalaryPool, cfg: dict | None = None, *, table: dict[str, Prior] | None = None):
        self._table = table if table is not None else prior_table(pool, cfg)

    def mean_tenths(self, role_id: str) -> int:
        return self._table[role_id].mean_tenths

    def sd_tenths(self, role_id: str) -> int:
        return self._table[role_id].sd_tenths

    def source(self) -> ModelStatus:
        return ModelStatus.PRIOR


def _is_captain(pool: SalaryPool, role_id: str) -> bool:
    return pool.mode is Mode.SHOWDOWN and "CPT" in pool.by_role_id[role_id].roster_positions


def lineup_mean_tenths(pool: SalaryPool, proj: Projection, role_ids: Iterable[str]) -> float:
    """Lineup mean in tenths, with the Captain row at 1.5x."""
    return sum(proj.mean_tenths(r) * (CAPTAIN_MULTIPLIER if _is_captain(pool, r) else 1.0) for r in role_ids)


def lineup_sd_tenths(pool: SalaryPool, proj: Projection, role_ids: Iterable[str]) -> float:
    """Independence sd of the lineup total in tenths (Captain sd at 1.5x). A prior-only spread,
    used as a band floor, never reported as a ceiling or probability."""
    var = 0.0
    for r in role_ids:
        s = proj.sd_tenths(r) * (CAPTAIN_MULTIPLIER if _is_captain(pool, r) else 1.0)
        var += s * s
    return math.sqrt(var)
