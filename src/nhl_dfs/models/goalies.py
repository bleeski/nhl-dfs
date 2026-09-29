"""Goalie parameters (C5, plan section 5; section 4 goalie rows).

p_start: decay-weighted share of the team's recent starts (Beta-shrunk to a prior share), times
b2b_start_mult for the goalie who started the team's game the day before the slate game, then
renormalized so the team's pool goalies sum to 1. Only games before as_of are read.
save_skill: goals saved above expected per shot faced, (sum xga - sum ga) / (shots + 1,500-shot
prior); Tier B rows have no xG, so skill stays 0 with save_skill_missing = 1.
workload_adj: the OPPONENT's shots-for per game over the league mean (shrunk), which scales the
expected shots against (what the opponent generates, not what it allows).
pull_prob_per_ga: partial starts (started, under 55 minutes) per goal against, shrunk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from nhl_dfs.models.rates import decay_weights


@dataclass
class GoalieParams:
    key: object  # nhl_id, or person_key when unmatched
    team: str
    p_start: float
    save_skill: float
    sv_pct: float
    shots_against: float
    workload_adj: float
    pull_prob_per_ga: float
    save_skill_missing: int = 1
    n_starts: int = 0
    n_shots: float = 0.0
    b2b: int = 0
    source: str = "prior"


def team_sog_for(skaters: pd.DataFrame) -> pd.Series:
    """team -> list of per-game SOG totals (from skater rows)."""
    if skaters.empty:
        return pd.Series(dtype=object)
    return skaters.groupby(["team", "game_id"])["sog"].sum().groupby(level=0).apply(list)


def estimate(features, schedule: dict, cfg: dict, *, team_goalies: dict, as_of: date) -> dict:
    """schedule: team -> {"opponent": code, "game_date": date}; team_goalies: team -> [keys]
    (nhl_id for matched goalies, person_key otherwise)."""
    gcfg = cfg["goalies"]
    gl = features.goalies if hasattr(features, "goalies") else pd.DataFrame()
    sk = features.skaters if hasattr(features, "skaters") else pd.DataFrame()
    if not gl.empty:
        gl = gl[(gl["game_date"] < as_of) & gl["regime"].isin(cfg.get("regimes", ["regular"]))]
    if not sk.empty:
        sk = sk[(sk["game_date"] < as_of) & sk["regime"].isin(cfg.get("regimes", ["regular"]))]
    league_sf = float(cfg["team"]["sog_for"])
    sf = team_sog_for(sk)
    k_w = float(gcfg["workload_prior_games"])
    out = {}
    for team, keys in team_goalies.items():
        info = schedule.get(team, {})
        opp = info.get("opponent")
        slate_day = info.get("game_date", as_of)
        opp_games = sf.get(opp, []) if opp is not None else []
        workload = ((sum(opp_games) + k_w * league_sf) / (len(opp_games) + k_w)) / league_sf
        starts = gl[(gl["team"] == team) & gl["started"]] if not gl.empty else pd.DataFrame()
        team_games = starts.drop_duplicates("game_id").sort_values("game_date", ascending=False) if len(starts) else starts
        w_game = dict(zip(team_games["game_id"], decay_weights(len(team_games), gcfg["start_half_life_games"]))) if len(team_games) else {}
        last = team_games.iloc[0] if len(team_games) else None
        yesterday = last is not None and last["game_date"] == slate_day - timedelta(days=1)
        weights = {}
        rows = {}
        for key in keys:
            mine = starts[starts["nhl_id"] == key] if len(starts) and isinstance(key, (int, np.integer)) else pd.DataFrame()
            num = sum(w_game.get(g, 0.0) for g in mine["game_id"]) if len(mine) else 0.0
            den = sum(w_game.values())
            k = float(gcfg["start_prior_games"])
            share = (num + k * float(gcfg["p_start_prior_share"])) / (den + k)
            b2b = int(bool(yesterday) and len(mine) and last["nhl_id"] == key)
            if b2b:
                share *= float(gcfg["b2b_start_mult"])
            weights[key] = share
            apps = gl[gl["nhl_id"] == key] if not gl.empty and isinstance(key, (int, np.integer)) else pd.DataFrame()
            tier_a = apps[apps["xga_missing"] == 0] if len(apps) else apps
            shots = float(tier_a["shots_against"].sum()) if len(tier_a) else 0.0
            skill = float((tier_a["xga"] - tier_a["goals_against"]).sum()) / (shots + float(gcfg["prior_shots"])) if len(tier_a) else 0.0
            my_starts = apps[apps["started"]] if len(apps) else apps
            pulls = int((my_starts["toi_s"] < 3300).sum()) if len(my_starts) else 0
            ga = float(my_starts["goals_against"].sum()) if len(my_starts) else 0.0
            kp = float(gcfg["pull_prior_ga"])
            pull = (pulls + kp * float(gcfg["pull_per_ga"])) / (ga + kp)
            rows[key] = dict(save_skill=skill, save_skill_missing=int(not len(tier_a)), n_starts=len(my_starts),
                             n_shots=shots, b2b=b2b, pull=pull, source="history" if len(apps) else "prior")
        total = sum(weights.values())
        for key in keys:
            r = rows[key]
            p = weights[key] / total if total > 0 else 1.0 / len(keys)
            out[key] = GoalieParams(key, team, p, r["save_skill"], float(gcfg["sv_pct"]) + r["save_skill"],
                                    float(gcfg["shots_against"]) * workload, workload, r["pull"],
                                    r["save_skill_missing"], r["n_starts"], r["n_shots"], r["b2b"], r["source"])
    return out
