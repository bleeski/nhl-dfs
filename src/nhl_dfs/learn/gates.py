"""The one evidence gate (plan section 12). Every fit and every promotion asks it first.

Floors live in config/evidence_floors.yaml and are counted per mode. Historical data counts
toward a floor; it never bypasses one. A refusal is a normal outcome, not an error: the caller
stays in prior or shadow mode and reports the reason string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

import yaml

from nhl_dfs.contracts.geometry import Mode

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_FLOORS_YAML = REPO_ROOT / "config" / "evidence_floors.yaml"

Action = Literal["prefit", "field_fit", "mixture_fit", "rate_correction", "strategy_change"]
ACTIONS: tuple[str, ...] = ("prefit", "field_fit", "mixture_fit", "rate_correction", "strategy_change")
BASE_TIER = "every_run"


@dataclass(frozen=True)
class EvidenceCounts:
    """Counts for ONE mode. Thousands of labels from one slate are still one slate group."""

    slate_dates: int = 0
    skater_games: int = 0
    goalie_starts: int = 0
    slate_groups: int = 0  # Classic slate groups, or Showdown games
    ownership_labels: int = 0  # player-contest (Classic) or person-role (Showdown) labels
    holdout_groups: int = 0  # latest groups the caller reserves for validation
    holdout_dates: int = 0
    groups_by_family: Mapping[str, int] = field(default_factory=dict)
    prospective_groups: int = 0
    complete_payout_dates: int = 0

    def value(self, name: str, family: str | None = None) -> int:
        if name == "groups_per_family":
            if family is not None:
                return int(self.groups_by_family.get(family, 0))
            # No family named: the change must hold for every family it would touch.
            return min(self.groups_by_family.values(), default=0)
        return int(getattr(self, name))


@dataclass
class GateReport:
    mode: Mode
    tier: str  # highest tier whose floors are all met, or "every_run"
    met: dict[str, bool]  # tier name -> all floors met
    shortfalls: dict[str, list[str]]  # tier name -> "count have < need" strings
    allowed: dict[str, bool]  # action -> allowed


def load_floors(path: Path = EVIDENCE_FLOORS_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    validate_floors(cfg)
    return cfg


def validate_floors(cfg: dict) -> None:
    tiers = cfg.get("tiers") or {}
    for mode in Mode:
        if not tiers.get(mode.value):
            raise ValueError(f"evidence floors have no tiers for mode {mode.value!r}")
        for tier, floors in tiers[mode.value].items():
            for name, need in (floors or {}).items():
                if name != "groups_per_family" and name not in EvidenceCounts.__dataclass_fields__:
                    raise ValueError(f"{mode.value}/{tier}: unknown count {name!r}")
                if not isinstance(need, int) or need < 0:
                    raise ValueError(f"{mode.value}/{tier}/{name}: floor must be a non-negative integer")
    actions = cfg.get("actions") or {}
    for a in ACTIONS:
        t = actions.get(a)
        if t is None:
            raise ValueError(f"evidence floors map no tier for action {a!r}")
        for mode in Mode:
            if t not in tiers[mode.value]:
                raise ValueError(f"action {a!r} names tier {t!r}, missing for {mode.value}")


def _shortfalls(floors: Mapping[str, int], counts: EvidenceCounts, family: str | None) -> list[str]:
    out = []
    for name, need in floors.items():
        have = counts.value(name, family)
        if have < need:
            out.append(f"{name} {have} < {need}")
    # A holdout is reserved from the groups; it cannot be the whole history.
    if "holdout_groups" in floors and counts.holdout_groups >= counts.slate_groups > 0:
        out.append(f"holdout_groups {counts.holdout_groups} leaves no training groups of {counts.slate_groups}")
    return out


def tier(counts: EvidenceCounts, mode: Mode, *, cfg: dict | None = None, family: str | None = None) -> GateReport:
    cfg = cfg if cfg is not None else load_floors()
    tiers = cfg["tiers"][mode.value]
    met, short = {}, {}
    for name, floors in tiers.items():
        s = _shortfalls(floors or {}, counts, family)
        short[name] = s
        met[name] = not s
    highest = BASE_TIER
    for name in tiers:  # listed lowest first; a tier counts only if every lower one is met too
        if not met[name]:
            break
        highest = name
    allowed = {a: met[cfg["actions"][a]] for a in ACTIONS}
    return GateReport(mode=mode, tier=highest, met=met, shortfalls=short, allowed=allowed)


def allows(action: Action, counts: EvidenceCounts, mode: Mode, *, cfg: dict | None = None,
           family: str | None = None) -> tuple[bool, str]:
    """(allowed, reason). The only gate any fit or promotion may consult."""
    if action not in ACTIONS:
        raise ValueError(f"unknown gated action {action!r}")
    cfg = cfg if cfg is not None else load_floors()
    tier_name = cfg["actions"][action]
    s = _shortfalls(cfg["tiers"][mode.value][tier_name] or {}, counts, family)
    scope = f"{mode.value}" + (f"/{family}" if family else "")
    if s:
        return False, f"{action} gated ({scope}, tier {tier_name}): " + "; ".join(s)
    return True, f"{action} allowed ({scope}, tier {tier_name})"
