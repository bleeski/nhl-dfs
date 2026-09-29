"""Realized outcomes of simulated scenarios (C6): per PERSON arrays, never per role row.

Person columns are sorted by person_key (the axis is part of the reproducibility contract). All
per-person arrays are (n, P); team arrays are (n, T) in `teams` order; game arrays are (n, G) in
`games` order. Integer stat lines only: the DK scoring function is applied later (sim/score.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

DECISIONS = {"ND": 0, "W": 1, "L": 2, "OTL": 3}

PERSON_2D = ("dressed", "goals", "assists", "sog", "blocks", "sh_pts", "so_goals", "saves", "ga", "decision", "shutout")
TEAM_2D = ("team_goals", "team_en", "team_sog", "team_pace")
GAME_2D = ("game_tie", "game_ot", "game_so", "game_home_win")


@dataclass
class Outcomes:
    person_keys: list[str]
    is_goalie: np.ndarray  # (P,) bool
    dressed: np.ndarray  # (n, P) bool: dressed (skater) or played in net (goalie)
    goals: np.ndarray  # (n, P) int16 regular and overtime goals (no shootout goals)
    assists: np.ndarray
    sog: np.ndarray  # includes goals; excludes shootout attempts
    blocks: np.ndarray
    sh_pts: np.ndarray  # shorthanded goals plus assists
    so_goals: np.ndarray  # shootout goals (own column: never goals, SOG or GA)
    saves: np.ndarray  # goalies only
    ga: np.ndarray  # goalies only; empty-net goals are not charged
    decision: np.ndarray  # (n, P) int8: 0 ND, 1 W, 2 L, 3 OTL
    shutout: np.ndarray  # (n, P) int8
    teams: list[str] = field(default_factory=list)
    team_goals: np.ndarray | None = None  # (n, T) goals for, overtime included, shootout excluded
    team_en: np.ndarray | None = None  # (n, T) empty-net goals scored by the team
    team_sog: np.ndarray | None = None  # (n, T) = sum of the team's skaters' SOG
    team_pace: np.ndarray | None = None  # (n, T) pace factor drawn
    games: list[str] = field(default_factory=list)  # "AWAY@HOME"
    game_tie: np.ndarray | None = None  # (n, G) tied after regulation
    game_ot: np.ndarray | None = None  # (n, G) an OT goal ended it
    game_so: np.ndarray | None = None  # (n, G) shootout
    game_home_win: np.ndarray | None = None  # (n, G)
    seed: int = 0
    purpose: str = "design"

    @property
    def n(self) -> int:
        return int(self.goals.shape[0])

    @property
    def p(self) -> int:
        return int(self.goals.shape[1])

    def nbytes(self) -> int:
        return sum(a.nbytes for a in vars(self).values() if isinstance(a, np.ndarray))


def empty(person_keys: list[str], is_goalie: np.ndarray, n: int) -> Outcomes:
    p = len(person_keys)

    def z16():
        return np.zeros((n, p), np.int16)

    return Outcomes(person_keys, np.asarray(is_goalie, bool), np.zeros((n, p), bool), z16(), z16(), z16(), z16(), z16(), z16(),
                    z16(), z16(), np.zeros((n, p), np.int8), np.zeros((n, p), np.int8))


def concat(parts: list[Outcomes]) -> Outcomes:
    first = parts[0]
    kw = dict(vars(first))
    for name in PERSON_2D + TEAM_2D + GAME_2D:
        if getattr(first, name) is not None:
            kw[name] = np.concatenate([getattr(p, name) for p in parts], axis=0)
    return Outcomes(**kw)
