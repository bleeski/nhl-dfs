"""As-of feature frame (C4): only information dated before the build date.

Rules:
- A game row is used only when its game_date is strictly before as_of (same-day games are out:
  they may not have been played, or not be final, when the slate is built).
- A season-level row (MoneyPuck season summaries, store kind mp_skater_seasons) is used only
  for a season whose regular season finished before as_of; a summary of the season in progress
  would include games after as_of.
- The regime comes from the game id's type digits (preseason / regular / playoffs). Per-player
  regime signals: team_stint increments when a player's team changes (trade or waiver claim),
  games_before counts prior stored games (a call-up or rookie has few), days_rest is the gap to
  the previous game.
- Every <col>_missing indicator exists in both tiers; players aggregates carry the share of the
  player's games whose value was a league prior (<col>_missing_share).
Descriptive only: no fitting or projection happens here (models start in C5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

import numpy as np
import pandas as pd

from nhl_dfs.data.history import history_seasons, regime_of, regular_season_complete
from nhl_dfs.data.history import store as store_mod
from nhl_dfs.data.history.combine import SKATER_ENRICH

RATE_COLS = ("goals", "assists", "a1", "a2", "sog", "attempts", "blocks", "ixg", "hd_xg", "pp_points")
TOI_COLS = ("toi_s", "toi_ev_s", "toi_pp_s", "toi_sh_s")


@dataclass
class FeatureFrame:
    as_of: date
    seasons: list[int]
    skaters: pd.DataFrame  # per-game rows before as_of
    goalies: pd.DataFrame
    players: pd.DataFrame  # per-player aggregates over regular-season games before as_of
    season_level: pd.DataFrame  # completed-season summaries only
    notes: list[str] = field(default_factory=list)


def _regime_signals(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.sort_values(["nhl_id", "game_date", "game_id"]).copy()
    df["regime"] = df["game_id"].map(regime_of)
    g = df.groupby("nhl_id", sort=False)
    df["games_before"] = g.cumcount()
    changed = g["team"].transform(lambda s: (s != s.shift()).astype(int))
    df["team_stint"] = changed.groupby(df["nhl_id"]).cumsum() - 1
    prev = g["game_date"].shift()
    df["days_rest"] = [(a - b).days if isinstance(b, date) else np.nan for a, b in zip(df["game_date"], prev)]
    return df.reset_index(drop=True)


def _players(sk: pd.DataFrame) -> pd.DataFrame:
    reg = sk[sk["regime"] == "regular"] if not sk.empty else sk
    if reg.empty:
        return pd.DataFrame(columns=["nhl_id", "games"])
    g = reg.groupby("nhl_id")
    out = pd.DataFrame({"games": g.size()})
    last = reg.sort_values(["game_date", "game_id"]).groupby("nhl_id").tail(1).set_index("nhl_id")
    out["last_game_date"] = last["game_date"]
    out["last_team"] = last["team"]
    out["team_stint"] = last["team_stint"]
    for c in TOI_COLS:
        out[f"{c}_mean"] = g[c].mean()
    hours = g["toi_s"].sum() / 3600.0
    for c in RATE_COLS:
        if c in reg:
            out[f"{c}_per60"] = g[c].sum() / hours.where(hours > 0)
    out["a1_share"] = g["a1"].sum() / g["assists"].sum().where(g["assists"].sum() > 0)
    for c in SKATER_ENRICH:
        m = f"{c}_missing"
        if m in reg:
            out[f"{m}_share"] = g[m].mean()
    out["tier_a_share"] = g["tier"].agg(lambda s: float((s == "A").mean())) if "tier" in reg else np.nan
    return out.reset_index()


def frame(as_of: date, nhl_ids: Iterable[int] | None, *, seasons: int = 2, store_root=None) -> FeatureFrame:
    """History for nhl_ids (None: everyone) from the stored seasons around as_of."""
    ss = history_seasons(as_of, seasons)
    ids = None if nhl_ids is None else {int(x) for x in nhl_ids}
    notes: list[str] = []

    def rows(kind: str) -> pd.DataFrame:
        df = store_mod.read(kind, ss, root=store_root)
        if df.empty:
            notes.append(f"{kind}: nothing stored for seasons {ss}")
            return df
        if ids is not None:
            df = df[df["nhl_id"].isin(ids)]
        return df[df["game_date"] < as_of]

    sk = _regime_signals(rows("skater_games"))
    gl = _regime_signals(rows("goalie_games"))
    season_level = store_mod.read("mp_skater_seasons", ss, root=store_root)
    if not season_level.empty:
        ok = season_level["season"].map(lambda s: regular_season_complete(int(s), as_of))
        dropped = int((~ok).sum())
        if dropped:
            notes.append(f"mp_skater_seasons: {dropped} row(s) from a season still in progress at {as_of} excluded")
        season_level = season_level[ok]
        if ids is not None:
            season_level = season_level[season_level["nhl_id"].isin(ids)]
    return FeatureFrame(as_of, ss, sk, gl, _players(sk), season_level.reset_index(drop=True), notes)
