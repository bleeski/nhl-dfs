"""Exact DraftKings NHL scoring, in integer tenths of a point.

Source: docs/rules/NHL_Classic.txt and docs/rules/NHL_Showdown_Captain_Mode.txt
(identical scoring tables). All arithmetic is integer tenths; the Captain
multiplier is applied in twentieths so 1.5x never touches a float.
"""

from dataclasses import dataclass
from typing import Sequence

GOAL_TENTHS = 85
ASSIST_TENTHS = 50
SOG_TENTHS = 15
BLOCK_TENTHS = 13
SH_POINT_BONUS_TENTHS = 20
SHOOTOUT_GOAL_TENTHS = 15
HAT_TRICK_BONUS_TENTHS = 30
FIVE_PLUS_SOG_BONUS_TENTHS = 30
THREE_PLUS_BLOCKS_BONUS_TENTHS = 30
THREE_PLUS_POINTS_BONUS_TENTHS = 30

GOALIE_WIN_TENTHS = 60
GOALIE_SAVE_TENTHS = 7
GOALIE_GOAL_AGAINST_TENTHS = -35
GOALIE_SHUTOUT_TENTHS = 40
GOALIE_OTL_TENTHS = 20
GOALIE_35_PLUS_SAVES_TENTHS = 30

CAPTAIN_TWENTIETHS_MULTIPLIER = 3  # 1.5x expressed so tenths -> twentieths is exact
BASE_TENTHS_TO_TWENTIETHS_MULTIPLIER = 2


@dataclass(frozen=True)
class SkaterLine:
    goals: int = 0
    assists: int = 0
    sog: int = 0
    blocks: int = 0
    sh_points: int = 0
    shootout_goals: int = 0


@dataclass(frozen=True)
class GoalieLine:
    decision: str = "ND"  # "W" | "L" | "OTL" | "ND"
    saves: int = 0
    goals_against: int = 0
    shutout: bool = False
    goals: int = 0
    assists: int = 0
    sog: int = 0
    blocks: int = 0  # offensive stats a goalie accrues

    def __post_init__(self) -> None:
        if self.decision not in ("W", "L", "OTL", "ND"):
            raise ValueError(f"unrecognized goalie decision: {self.decision!r}")


def _threshold_bonuses_tenths(goals: int, assists: int, sog: int, blocks: int) -> int:
    total = 0
    if goals >= 3:
        total += HAT_TRICK_BONUS_TENTHS
    if sog >= 5:
        total += FIVE_PLUS_SOG_BONUS_TENTHS
    if blocks >= 3:
        total += THREE_PLUS_BLOCKS_BONUS_TENTHS
    if (goals + assists) >= 3:
        total += THREE_PLUS_POINTS_BONUS_TENTHS
    return total


def score_skater_tenths(s: SkaterLine) -> int:
    total = (
        s.goals * GOAL_TENTHS
        + s.assists * ASSIST_TENTHS
        + s.sog * SOG_TENTHS
        + s.blocks * BLOCK_TENTHS
        + s.sh_points * SH_POINT_BONUS_TENTHS
        + s.shootout_goals * SHOOTOUT_GOAL_TENTHS
    )
    total += _threshold_bonuses_tenths(s.goals, s.assists, s.sog, s.blocks)
    return total


def score_goalie_tenths(g: GoalieLine) -> int:
    total = (
        g.goals * GOAL_TENTHS
        + g.assists * ASSIST_TENTHS
        + g.sog * SOG_TENTHS
        + g.blocks * BLOCK_TENTHS
        + g.saves * GOALIE_SAVE_TENTHS
        + g.goals_against * GOALIE_GOAL_AGAINST_TENTHS
    )
    if g.decision == "W":
        total += GOALIE_WIN_TENTHS
    elif g.decision == "OTL":
        total += GOALIE_OTL_TENTHS
    if g.shutout:
        total += GOALIE_SHUTOUT_TENTHS
    if g.saves >= 35:
        total += GOALIE_35_PLUS_SAVES_TENTHS
    total += _threshold_bonuses_tenths(g.goals, g.assists, g.sog, g.blocks)
    return total


def captain_twentieths(base_tenths: int) -> int:
    return base_tenths * CAPTAIN_TWENTIETHS_MULTIPLIER


def lineup_twentieths(base_tenths: Sequence[int], captain_index: int | None) -> int:
    total = 0
    for i, t in enumerate(base_tenths):
        if captain_index is not None and i == captain_index:
            total += captain_twentieths(t)
        else:
            total += t * BASE_TENTHS_TO_TWENTIETHS_MULTIPLIER
    return total


def tenths_to_points(t: int) -> float:
    return t / 10.0
