"""Build the combined per-game tables from the per-source tables (C4).

skater_games: the NHL per-game reports are the backbone in both tiers (TOI by strength always
has the NHL definition). Tier A adds MoneyPuck columns (A1/A2, shot attempts, ixG, high-danger
xG, on-ice xG, MoneyPuck situation times, shared ice) with <col>_missing = 0. Tier B fills the
same columns from config/history_priors.yaml with <col>_missing = 1 (MoneyPuck situation times
have no prior: NaN, missing = 1). A game MoneyPuck has but the NHL reports lack is kept from
MoneyPuck alone, with toi_strength_source = "moneypuck_situations" (EV = all - 5on4 - 4on5,
PP = 5on4, SH = 4on5), and pp_points / sh_points missing.

goalie_games: NHL goalie per-game rows; Tier A adds MoneyPuck expected goals against (xga) and
gsax = xga - goals_against; Tier B uses the prior xga rate and gsax = 0, both flagged missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from nhl_dfs.contracts.ids import position_group
from nhl_dfs.data.history import mp_year, regime_of
from nhl_dfs.data.history import moneypuck as mp
from nhl_dfs.data.history import store as store_mod
from nhl_dfs.data.http import SourceUnavailable, load_sources_config

REPO_ROOT = Path(__file__).resolve().parents[4]
PRIORS_YAML = REPO_ROOT / "config" / "history_priors.yaml"

SKATER_ENRICH = ("a1", "a2", "attempts", "ixg", "hd_xg", "onice_xgf", "onice_xga", "onice_xgf_5on5",
                 "onice_xga_5on5", "shared_ice", "mp_toi_5on5_s", "mp_toi_5on4_s", "mp_toi_4on5_s", "mp_toi_other_s")
NO_PRIOR = ("mp_toi_5on5_s", "mp_toi_5on4_s", "mp_toi_4on5_s", "mp_toi_other_s")
BACKBONE = ("toi_s", "toi_ev_s", "toi_pp_s", "toi_sh_s", "shifts", "goals", "assists", "sog", "blocks",
            "pp_points", "sh_points")
GOALIE_ENRICH = ("xga", "gsax")
_NHL_POS = {"L": "LW", "R": "RW"}


def load_priors(path: Path = PRIORS_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def group_of(position) -> str:
    p = str(position or "").upper()
    return position_group(_NHL_POS.get(p, p)) if p else "F"


@dataclass
class CombineResult:
    rows: dict[str, int] = field(default_factory=dict)
    tiers: dict[str, int] = field(default_factory=dict)


def moneypuck_season(season: int, stats, *, cfg: dict | None = None, store_root=None, raw_root=None,
                     enabled: bool | None = None, transport=None) -> None:
    """Download and store the backfill kinds for one season; status per kind goes to stats."""
    cfg = cfg if cfg is not None else load_sources_config()
    mpc = cfg["moneypuck"]
    on = mpc.get("enabled", False) if enabled is None else enabled
    for kind in mpc.get("backfill_kinds", []):
        key = f"{kind}/{season}"
        if not on:
            stats.moneypuck[key] = "disabled"
            continue
        try:
            mp.download(kind, mp_year(season), "game", cfg=cfg, raw_root=raw_root, transport=transport)
            df = mp.load(kind, mp_year(season), "game", raw_root=raw_root, cfg=cfg)
        except (SourceUnavailable, mp.NotListed, OSError, ValueError) as exc:
            stats.moneypuck[key] = f"unavailable: {type(exc).__name__}: {str(exc)[:80]}"
            continue
        name = {"skaters": "mp_skater_games", "goalies": "mp_goalie_games", "lines": "line_games"}[kind]
        store_mod.write(name, season, df, root=store_root)
        stats.moneypuck[key] = f"ok ({len(df)} rows)"


def _priors_for(df: pd.DataFrame, priors: dict) -> pd.DataFrame:
    """Prior value of every priored enrichment column, for every row (see history_priors.yaml)."""
    grp = df["position"].map(group_of)
    pr = pd.DataFrame([priors["skaters"].get(g, priors["skaters"]["F"]) for g in grp], index=df.index)
    a1 = df["assists"] * pr["a1_share"]
    return pd.DataFrame({
        "a1": a1, "a2": df["assists"] - a1, "attempts": df["sog"] * pr["attempts_per_sog"],
        "ixg": df["sog"] * pr["ixg_per_sog"], "hd_xg": df["sog"] * pr["ixg_per_sog"] * pr["hd_xg_share"],
        "onice_xgf": df["toi_s"] / 3600 * pr["onice_xgf_per60"], "onice_xga": df["toi_s"] / 3600 * pr["onice_xga_per60"],
        "onice_xgf_5on5": df["toi_ev_s"] / 3600 * pr["onice_xgf_per60_5on5"],
        "onice_xga_5on5": df["toi_ev_s"] / 3600 * pr["onice_xga_per60_5on5"],
        "shared_ice": pr["shared_ice"],
    }, index=df.index)


def combine_skaters(nhl_df: pd.DataFrame, mp_df: pd.DataFrame, lines: pd.DataFrame, priors: dict) -> pd.DataFrame:
    keys = ["nhl_id", "game_id"]
    nhl_df = nhl_df.copy()
    enrich = pd.DataFrame(columns=keys)
    if not mp_df.empty:
        enrich = mp_df[keys + [c for c in SKATER_ENRICH if c in mp_df.columns and c != "shared_ice"]].copy()
        if not lines.empty:
            enrich = enrich.merge(mp.shared_ice(lines), on=keys, how="left")
    if not nhl_df.empty:
        nhl_df["toi_strength_source"] = "nhl"
        for c in ("pp_points", "sh_points"):
            nhl_df[f"{c}_missing"] = nhl_df[c].isna().astype(int)
    # MoneyPuck-only games (NHL reports lacked them): keep with the MoneyPuck situation mapping.
    mp_only = pd.DataFrame()
    if not mp_df.empty:
        have = set(zip(nhl_df["nhl_id"], nhl_df["game_id"])) if not nhl_df.empty else set()
        extra = mp_df[[k not in have for k in zip(mp_df["nhl_id"], mp_df["game_id"])]]
        if not extra.empty:
            mp_only = pd.DataFrame({
                "nhl_id": extra["nhl_id"], "game_id": extra["game_id"], "name": extra["name"],
                "game_date": extra["game_date"], "team": extra["team"], "opponent": extra["opponent"],
                "home": extra["home"], "position": extra["position"], "toi_s": extra["mp_toi_s"],
                "toi_pp_s": extra["mp_toi_5on4_s"], "toi_sh_s": extra["mp_toi_4on5_s"],
                "toi_ev_s": extra["mp_toi_s"] - extra["mp_toi_5on4_s"] - extra["mp_toi_4on5_s"],
                "shifts": extra["mp_shifts"], "goals": extra["goals"], "assists": extra["assists"],
                "sog": extra["sog"], "blocks": extra["blocks"], "pp_points": np.nan, "sh_points": np.nan,
                "pp_points_missing": 1, "sh_points_missing": 1, "toi_strength_source": "moneypuck_situations",
            })
            gid = mp_only["game_id"].astype("int64")
            from nhl_dfs.data.history import nhl_season

            mp_only["season"] = (gid // 1_000_000).map(nhl_season)
            mp_only["game_type"] = (gid // 10_000 % 100).astype(int)
            mp_only["regime"] = gid.map(regime_of)
    base = pd.concat([d for d in (nhl_df, mp_only) if not d.empty], ignore_index=True) if (
        not nhl_df.empty or not mp_only.empty) else pd.DataFrame()
    if base.empty:
        return base
    base = base.merge(enrich, on=keys, how="left", suffixes=("", "_mp"))
    has_mp = base["ixg"].notna() if "ixg" in base else pd.Series(False, index=base.index)
    for c in SKATER_ENRICH:
        if c not in base:
            base[c] = np.nan
    missing = {c: base[c].isna() for c in SKATER_ENRICH}  # before any prior is filled in
    pr = _priors_for(base, priors)
    for c in pr.columns:
        base[c] = base[c].fillna(pr[c])  # MoneyPuck situation times have no prior and stay NaN
    for c in SKATER_ENRICH:
        base[f"{c}_missing"] = missing[c].astype(int)
    base["tier"] = np.where(has_mp, "A", "B")
    return base.sort_values(["game_date", "game_id", "nhl_id"]).reset_index(drop=True)


def combine_goalies(nhl_df: pd.DataFrame, mp_df: pd.DataFrame, priors: dict) -> pd.DataFrame:
    if nhl_df.empty:
        return nhl_df
    g = nhl_df.copy()
    if not mp_df.empty:
        g = g.merge(mp_df[["nhl_id", "game_id", "xga"]], on=["nhl_id", "game_id"], how="left")
    else:
        g["xga"] = np.nan
    has = g["xga"].notna()
    g.loc[~has, "xga"] = g.loc[~has, "shots_against"] * float(priors["goalies"]["xga_per_shot_against"])
    g["gsax"] = np.where(has, g["xga"] - g["goals_against"], 0.0)
    g["xga_missing"] = (~has).astype(int)
    g["gsax_missing"] = (~has).astype(int)
    g["tier"] = np.where(has, "A", "B")
    return g.sort_values(["game_date", "game_id", "nhl_id"]).reset_index(drop=True)


def combine_season(season: int, *, store_root=None, priors: dict | None = None) -> CombineResult:
    priors = priors if priors is not None else load_priors()
    res = CombineResult()
    sk = combine_skaters(store_mod.read("nhl_skater_games", [season], root=store_root),
                         store_mod.read("mp_skater_games", [season], root=store_root),
                         store_mod.read("line_games", [season], root=store_root), priors)
    gl = combine_goalies(store_mod.read("nhl_goalie_games", [season], root=store_root),
                         store_mod.read("mp_goalie_games", [season], root=store_root), priors)
    if not sk.empty:
        store_mod.write("skater_games", season, sk, root=store_root)
        res.tiers = {t: int((sk["tier"] == t).sum()) for t in ("A", "B")}
    if not gl.empty:
        store_mod.write("goalie_games", season, gl, root=store_root)
    res.rows = {"skater_games": len(sk), "goalie_games": len(gl)}
    return res
