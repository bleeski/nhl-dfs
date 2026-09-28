"""Population priors for the baseline projection (plan section 10, ladder step 1).

mean = w * APPG + (1 - w) * bucket_mean, in integer tenths of a DK point.
w comes from config/priors.yaml (default 0.6) and is a challenger setting, not an
established edge. APPG_ZERO keeps its flag and gets w = 0; MISSING uses the bucket.
APPG never enters the rate model (C5); this table is the cold-start baseline only.

Priors are the person's uncaptained expectation. A Showdown CPT row and its FLEX
row carry the same Prior; the Captain's 1.5x is applied once, in prior_objective.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from nhl_dfs.contracts.geometry import Mode, PoolRow
from nhl_dfs.contracts.ids import position_group
from nhl_dfs.intake.salary import SalaryPool

REPO_ROOT = Path(__file__).resolve().parents[3]
PRIORS_YAML = REPO_ROOT / "config" / "priors.yaml"

CAPTAIN_MULTIPLIER = 1.5
_GROUPS = ("F", "D", "G")


@dataclass(frozen=True)
class Prior:
    mean_tenths: int
    sd_tenths: int
    source: str  # "APPG_SHRUNK" | "BUCKET"
    appg_flag: str  # the row's flag, kept as received: "VALUE" | "APPG_ZERO" | "MISSING"


def load_priors_config(path: Path = PRIORS_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    validate_priors_config(cfg)
    return cfg


def validate_priors_config(cfg: dict) -> None:
    """Raise ValueError unless every (mode, group) has ascending bands ending open-ended."""
    w = cfg.get("appg_weight")
    if not isinstance(w, (int, float)) or not 0.0 <= w <= 1.0:
        raise ValueError(f"appg_weight must be a number in [0, 1], got {w!r}")
    buckets = cfg.get("buckets") or {}
    for mode in Mode:
        by_group = buckets.get(mode.value)
        if not by_group:
            raise ValueError(f"priors config has no buckets for mode {mode.value!r}")
        for group in _GROUPS:
            bands = by_group.get(group)
            if not bands:
                raise ValueError(f"priors config has no {mode.value}/{group} bands")
            caps = [b.get("max_salary") for b in bands]
            if caps[-1] is not None:
                raise ValueError(f"{mode.value}/{group}: last band must have max_salary: null")
            closed = caps[:-1]
            if any(c is None for c in closed) or closed != sorted(closed) or len(set(closed)) != len(closed):
                raise ValueError(f"{mode.value}/{group}: max_salary must be strictly ascending")
            for b in bands:
                if int(b["sd_tenths"]) <= 0:
                    raise ValueError(f"{mode.value}/{group}: sd_tenths must be positive")


def _bucket(cfg: dict, mode: Mode, group: str, salary: int) -> tuple[int, int]:
    for band in cfg["buckets"][mode.value][group]:
        cap = band["max_salary"]
        if cap is None or salary <= cap:
            return int(band["mean_tenths"]), int(band["sd_tenths"])
    raise AssertionError("unreachable: validated config ends with an open band")


def _band_salary(pool: SalaryPool, row: PoolRow) -> int:
    """Salary used for the band lookup: the FLEX salary for a Showdown person."""
    if pool.mode is Mode.SHOWDOWN and "CPT" in row.roster_positions:
        pair = pool.persons.get(row.person_key)
        if pair is not None and pair.flex is not None:
            return pair.flex.salary
        return round(row.salary / CAPTAIN_MULTIPLIER)
    return row.salary


def prior_for_row(pool: SalaryPool, row: PoolRow, cfg: dict) -> Prior:
    bucket_mean, bucket_sd = _bucket(cfg, pool.mode, position_group(row.position), _band_salary(pool, row))
    if row.appg_flag == "VALUE" and row.appg_raw is not None:
        w = float(cfg["appg_weight"])
        mean = round(w * row.appg_raw * 10 + (1.0 - w) * bucket_mean)
        source = "APPG_SHRUNK" if w > 0 else "BUCKET"
        return Prior(mean_tenths=mean, sd_tenths=bucket_sd, source=source, appg_flag=row.appg_flag)
    # APPG_ZERO (w = 0, flag kept) and MISSING both fall to the bucket.
    return Prior(mean_tenths=bucket_mean, sd_tenths=bucket_sd, source="BUCKET", appg_flag=row.appg_flag)


def prior_table(pool: SalaryPool, cfg: dict | None = None) -> dict[str, Prior]:
    """Prior per selectable role_id. cfg defaults to config/priors.yaml."""
    if cfg is None:
        cfg = load_priors_config()
    else:
        validate_priors_config(cfg)
    return {row.role_id: prior_for_row(pool, row, cfg) for row in pool.rows}


def prior_objective(pool: SalaryPool, priors: dict[str, Prior]) -> dict[str, float]:
    """Solver objective in DK points per role_id: prior mean, times 1.5 on CPT rows only."""
    out: dict[str, float] = {}
    for row in pool.rows:
        points = priors[row.role_id].mean_tenths / 10.0
        if pool.mode is Mode.SHOWDOWN and "CPT" in row.roster_positions:
            points *= CAPTAIN_MULTIPLIER
        out[row.role_id] = points
    return out
