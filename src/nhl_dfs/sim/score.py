"""Exact DK scoring on simulated outcomes (C6), vectorized, in integer tenths.

Every constant comes from contracts.scoring, so this cannot drift from the rule files. A role
row copies its person's column (a Showdown CPT row equals its FLEX row); the Captain multiplier
is applied exactly once, in lineup_twentieths.
"""

from __future__ import annotations

import numpy as np

from nhl_dfs.contracts import scoring as sc
from nhl_dfs.sim.outcomes import DECISIONS, Outcomes


def base_tenths(o: Outcomes, params=None) -> np.ndarray:
    """(n, P) int32 base DK points in tenths. `params` is accepted for the card's signature; the
    scoring needs nothing from it (participation is already in the outcomes)."""
    g = o.goals.astype(np.int32)
    a = o.assists.astype(np.int32)
    sog = o.sog.astype(np.int32)
    blk = o.blocks.astype(np.int32)
    total = (g * sc.GOAL_TENTHS + a * sc.ASSIST_TENTHS + sog * sc.SOG_TENTHS + blk * sc.BLOCK_TENTHS
             + o.sh_pts.astype(np.int32) * sc.SH_POINT_BONUS_TENTHS + o.so_goals.astype(np.int32) * sc.SHOOTOUT_GOAL_TENTHS)
    total += sc.HAT_TRICK_BONUS_TENTHS * (g >= 3)
    total += sc.FIVE_PLUS_SOG_BONUS_TENTHS * (sog >= 5)
    total += sc.THREE_PLUS_BLOCKS_BONUS_TENTHS * (blk >= 3)
    total += sc.THREE_PLUS_POINTS_BONUS_TENTHS * ((g + a) >= 3)
    sv = o.saves.astype(np.int32)
    total += (sv * sc.GOALIE_SAVE_TENTHS + o.ga.astype(np.int32) * sc.GOALIE_GOAL_AGAINST_TENTHS
              + sc.GOALIE_WIN_TENTHS * (o.decision == DECISIONS["W"]) + sc.GOALIE_OTL_TENTHS * (o.decision == DECISIONS["OTL"])
              + sc.GOALIE_SHUTOUT_TENTHS * (o.shutout > 0) + sc.GOALIE_35_PLUS_SAVES_TENTHS * (sv >= 35))
    return total.astype(np.int32)


def role_tenths(base_person: np.ndarray, role_map) -> np.ndarray:
    """(n, R): role row r copies person column role_map[r]. CPT rows are NOT multiplied here."""
    return base_person[:, np.asarray(role_map, dtype=np.int64)]


def lineup_twentieths(role_base: np.ndarray, lineups: np.ndarray, captain_col: int | None) -> np.ndarray:
    """(n, L) int32 lineup scores in twentieths. lineups is (L, k): role columns of each lineup;
    captain_col is the position within `lineups` (0..k-1) of the Captain, or None. The Captain's
    base is counted at 3 twentieths per tenth, every other slot at 2: exactly once."""
    lineups = np.asarray(lineups, dtype=np.int64)
    out = np.zeros((role_base.shape[0], lineups.shape[0]), np.int32)
    for j in range(lineups.shape[1]):
        mult = sc.CAPTAIN_TWENTIETHS_MULTIPLIER if captain_col is not None and j == captain_col \
            else sc.BASE_TENTHS_TO_TWENTIETHS_MULTIPLIER
        out += role_base[:, lineups[:, j]].astype(np.int32) * mult
    return out


def role_map_for(pool, person_keys) -> tuple[list[str], np.ndarray]:
    """(role_ids, person column per role) for a SalaryPool against the simulator's sorted person axis.
    A Showdown person's CPT and FLEX rows map to the same column. A role whose person was not
    simulated (excluded before the run) is left out."""
    col = {k: i for i, k in enumerate(person_keys)}
    ids, cols = [], []
    for r in pool.rows:
        if r.person_key in col:
            ids.append(r.role_id)
            cols.append(col[r.person_key])
    return ids, np.asarray(cols, dtype=np.int64)
