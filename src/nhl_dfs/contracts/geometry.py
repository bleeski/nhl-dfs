"""Hard roster contracts for DraftKings NHL Classic and Showdown.

Source: plan section 7 "Hard roster contracts" table, cross-checked against
docs/rules/. Adds no invented rule (no per-team maximum, no one-goalie limit).
"""

from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class Mode(Enum):
    CLASSIC = "classic"
    SHOWDOWN = "showdown"


CLASSIC_SLOTS = ("C", "C", "W", "W", "W", "D", "D", "UTIL", "G")
SHOWDOWN_SLOTS = ("CPT", "FLEX", "FLEX", "FLEX", "FLEX", "FLEX")
SALARY_CAP = 50_000

_CLASSIC_MIN_SKATER_TEAMS = 3
_SHOWDOWN_MIN_TEAMS = 2


@dataclass(frozen=True)
class PoolRow:
    role_id: str
    person_key: str
    name: str
    team: str
    position: str
    roster_positions: frozenset[str]
    salary: int
    game_info: str
    appg_raw: float | None
    appg_flag: str  # "VALUE" | "APPG_ZERO" | "MISSING"

    @property
    def is_goalie(self) -> bool:
        return self.position == "G"


@dataclass
class LegalityResult:
    ok: bool
    reasons: list[str]
    salary_total: int
    skater_teams: set[str]
    teams: set[str]


def slot_accepts(slot: str, row: PoolRow, mode: Mode) -> bool:
    if mode is Mode.CLASSIC:
        if slot == "UTIL":
            return "UTIL" in row.roster_positions and not row.is_goalie
        if slot == "G":
            return "G" in row.roster_positions and row.is_goalie
        return slot in row.roster_positions
    # Mode.SHOWDOWN
    return slot in row.roster_positions


def _expected_slots(mode: Mode) -> tuple[str, ...]:
    return CLASSIC_SLOTS if mode is Mode.CLASSIC else SHOWDOWN_SLOTS


def check_lineup(rows: Sequence[PoolRow], mode: Mode) -> LegalityResult:
    slots = _expected_slots(mode)
    reasons: list[str] = []

    if len(rows) != len(slots):
        reasons.append(f"expected {len(slots)} rows, got {len(rows)}")

    for i in range(min(len(rows), len(slots))):
        slot = slots[i]
        row = rows[i]
        if not slot_accepts(slot, row, mode):
            reasons.append(
                f"slot {i} ({slot}) rejects role {row.role_id} ({row.name}, "
                f"eligible={sorted(row.roster_positions)})"
            )

    person_keys = [r.person_key for r in rows]
    seen: set[str] = set()
    dupes: set[str] = set()
    for pk in person_keys:
        if pk in seen:
            dupes.add(pk)
        seen.add(pk)
    if dupes:
        reasons.append(f"duplicate person(s): {sorted(dupes)}")

    salary_total = sum(r.salary for r in rows)
    if salary_total > SALARY_CAP:
        reasons.append(f"salary {salary_total} exceeds cap {SALARY_CAP}")

    teams = {r.team for r in rows}
    skater_teams = {r.team for r in rows if not r.is_goalie}

    if mode is Mode.CLASSIC:
        if len(skater_teams) < _CLASSIC_MIN_SKATER_TEAMS:
            reasons.append(
                f"skaters span {len(skater_teams)} team(s), need at least "
                f"{_CLASSIC_MIN_SKATER_TEAMS}"
            )
    else:
        if len(teams) < _SHOWDOWN_MIN_TEAMS:
            reasons.append(
                f"lineup spans {len(teams)} team(s), need at least {_SHOWDOWN_MIN_TEAMS}"
            )

    return LegalityResult(
        ok=not reasons,
        reasons=reasons,
        salary_total=salary_total,
        skater_teams=skater_teams,
        teams=teams,
    )


def lineup_key(rows: Sequence[PoolRow], mode: Mode) -> str:
    if mode is Mode.CLASSIC:
        return ",".join(sorted(r.person_key for r in rows))
    # Mode.SHOWDOWN: rows[0] is the CPT slot, rows[1:] are FLEX, per SHOWDOWN_SLOTS order.
    cpt_key = rows[0].person_key
    flex_keys = sorted(r.person_key for r in rows[1:])
    return cpt_key + "|" + ",".join(flex_keys)
