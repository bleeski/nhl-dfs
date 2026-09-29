"""Per-person event rates (C5, plan section 5 step 3). DAG node: opportunity (TOI by strength)
-> events per minute -> DK points; no DK APPG enters here.

Every rate is a Gamma-Poisson posterior mean with decay-weighted history:
    rate = (sum w * count + prior_rate * prior_exposure) / (sum w * exposure + prior_exposure)
so zero history returns the prior exactly and long history converges to the sample rate.
Points are split by strength from observed PP and SH points (EV = total - PP - SH) against the
NHL EV/PP/SH minutes. SOG and blocks have per-game totals only, so their exposure weights
minutes by the config strength multipliers (placeholders until per-strength counts are stored).
Goal share and A1 share are Beta posteriors; A1 share and shot quality use Tier A rows only and
carry a missing indicator when a person has none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from nhl_dfs.data.history.combine import group_of

REPO_ROOT = Path(__file__).resolve().parents[3]
MODEL_YAML = REPO_ROOT / "config" / "model.yaml"
STRENGTHS = ("ev", "pp", "sh")


def load_model_config(path: Path = MODEL_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass
class Rates:
    nhl_id: int
    group: str
    pts60: dict[str, float]  # by strength
    goal_share: float
    a1_share: float
    g60: dict[str, float] = field(default_factory=dict)
    a1_60: dict[str, float] = field(default_factory=dict)
    a2_60: dict[str, float] = field(default_factory=dict)
    sog60: dict[str, float] = field(default_factory=dict)
    blk60: dict[str, float] = field(default_factory=dict)
    phi_sog: float = 0.0
    phi_blk: float = 0.0
    shot_quality: float = 0.0  # xG per SOG
    shot_quality_missing: int = 1
    a1_share_missing: int = 1
    n_games: int = 0
    n_ev_min: float = 0.0
    n_st_min: float = 0.0
    prior_exposure_min: float = 0.0


def decay_weights(n: int, half_life: float) -> np.ndarray:
    """Weights for a player's games ordered most recent first."""
    return 0.5 ** (np.arange(n) / float(half_life))


def gamma_poisson(count: float, exposure_min: float, prior_rate60: float, prior_min: float) -> float:
    """Posterior mean per 60 minutes."""
    return (count + prior_rate60 * prior_min / 60.0) / ((exposure_min + prior_min) / 60.0)


def beta_share(hits: float, total: float, prior: float, k: float) -> float:
    return (hits + prior * k) / (total + k)


def _prior_base(p: dict, cfg: dict, stat: str) -> float:
    """League all-situations rate expressed per 60 exposure-minutes (multiplier-weighted)."""
    m = cfg["strength_mult"][stat]
    toi = p["toi_s"]
    total = sum(toi[s] for s in STRENGTHS)
    weighted = sum(m[s] * toi[s] for s in STRENGTHS)
    return p[f"{stat}60"] * total / weighted


def prior_rates(nhl_id: int, group: str, cfg: dict) -> Rates:
    return _finish(Rates(nhl_id, group, dict(cfg["skaters"][group]["pts60"]), cfg["skaters"][group]["goal_share"],
                         cfg["skaters"][group]["a1_share"],
                         shot_quality=cfg["skaters"][group]["ixg_per_sog"],
                         phi_sog=cfg["skaters"][group]["nb_phi"]["sog"], phi_blk=cfg["skaters"][group]["nb_phi"]["blk"],
                         prior_exposure_min=float(cfg["ev_prior_min"] + cfg["st_prior_min"])),
                   _prior_base(cfg["skaters"][group], cfg, "sog"), _prior_base(cfg["skaters"][group], cfg, "blk"), cfg)


def _finish(r: Rates, sog_base: float, blk_base: float, cfg: dict) -> Rates:
    for s in STRENGTHS:
        r.g60[s] = r.pts60[s] * r.goal_share
        assists = r.pts60[s] * (1.0 - r.goal_share)
        r.a1_60[s] = assists * r.a1_share
        r.a2_60[s] = assists - r.a1_60[s]
        r.sog60[s] = sog_base * cfg["strength_mult"]["sog"][s]
        r.blk60[s] = blk_base * cfg["strength_mult"]["blk"][s]
    return r


def _phi(y: np.ndarray, mu: np.ndarray, w: np.ndarray, prior: float, k: float) -> float:
    """Weighted method-of-moments NB dispersion, shrunk to the prior with k pseudo-games."""
    if len(y) == 0 or (w * mu * mu).sum() <= 0:
        return prior
    hat = float((w * ((y - mu) ** 2 - mu)).sum() / (w * mu * mu).sum())
    n = float(w.sum())
    return max(0.0, (n * hat + k * prior) / (n + k))


def estimate_one(nhl_id: int, games: pd.DataFrame, cfg: dict) -> Rates:
    """games: one person's skater_games rows (any order)."""
    group = group_of(games["position"].iloc[-1]) if len(games) else "F"
    if group == "G":
        group = "F"
    if games.empty:
        return prior_rates(nhl_id, group, cfg)
    p = cfg["skaters"][group]
    g = games.sort_values(["game_date", "game_id"], ascending=False)
    w = decay_weights(len(g), cfg["decay_half_life_games"])
    mins = {s: g[f"toi_{s}_s"].to_numpy(float) / 60.0 for s in STRENGTHS}
    pp_pts = g["pp_points"].fillna(0).to_numpy(float)
    sh_pts = g["sh_points"].fillna(0).to_numpy(float)
    goals, assists = g["goals"].to_numpy(float), g["assists"].to_numpy(float)
    counts = {"ev": np.clip(goals + assists - pp_pts - sh_pts, 0, None), "pp": pp_pts, "sh": sh_pts}
    pri_min = {"ev": cfg["ev_prior_min"], "pp": cfg["st_prior_min"], "sh": cfg["st_prior_min"]}
    pts60 = {s: gamma_poisson((w * counts[s]).sum(), (w * mins[s]).sum(), p["pts60"][s], pri_min[s]) for s in STRENGTHS}
    k = cfg["prior_games"]["share"]
    goal_share = beta_share((w * goals).sum(), (w * (goals + assists)).sum(), p["goal_share"], k)
    tier_a = (g["a1_missing"].to_numpy() == 0) if "a1_missing" in g else np.zeros(len(g), bool)
    a1_share = beta_share((w * g["a1"].to_numpy(float) * tier_a).sum(), (w * assists * tier_a).sum(), p["a1_share"], k)
    prior_exp = float(cfg["ev_prior_min"] + cfg["st_prior_min"])
    bases = {}
    phis = {}
    for stat, col in (("sog", "sog"), ("blk", "blocks")):
        m = cfg["strength_mult"][stat]
        expo = sum(m[s] * mins[s] for s in STRENGTHS)
        y = g[col].fillna(0).to_numpy(float)
        base = gamma_poisson((w * y).sum(), (w * expo).sum(), _prior_base(p, cfg, stat), prior_exp)
        bases[stat] = base
        phis[stat] = _phi(y, base * expo / 60.0, w, p["nb_phi"][stat], cfg["prior_games"]["dispersion"])
    sq_rows = (g["ixg_missing"].to_numpy() == 0) if "ixg_missing" in g else np.zeros(len(g), bool)
    ks = cfg["shot_quality_prior_sog"]
    shot_q = beta_share((w * g["ixg"].to_numpy(float) * sq_rows).sum(), (w * g["sog"].to_numpy(float) * sq_rows).sum(),
                        p["ixg_per_sog"], ks)
    r = Rates(nhl_id, group, pts60, goal_share, a1_share, phi_sog=phis["sog"], phi_blk=phis["blk"],
              shot_quality=shot_q, shot_quality_missing=int(not sq_rows.any()), a1_share_missing=int(not tier_a.any()),
              n_games=len(g), n_ev_min=float((w * mins["ev"]).sum()),
              n_st_min=float((w * (mins["pp"] + mins["sh"])).sum()), prior_exposure_min=prior_exp)
    return _finish(r, bases["sog"], bases["blk"], cfg)


def regular_games(features, cfg: dict) -> pd.DataFrame:
    sk = features.skaters if hasattr(features, "skaters") else features
    if sk.empty:
        return sk
    return sk[sk["regime"].isin(cfg.get("regimes", ["regular"]))]


def estimate(features, cfg: dict | None = None, *, nhl_ids=None, groups: dict[int, str] | None = None) -> dict[int, Rates]:
    """Rates per nhl_id present in the features (plus prior rows for nhl_ids given without history)."""
    cfg = cfg if cfg is not None else load_model_config()
    sk = regular_games(features, cfg)
    out: dict[int, Rates] = {}
    if not sk.empty:
        for pid, games in sk.groupby("nhl_id"):
            out[int(pid)] = estimate_one(int(pid), games, cfg)
    for pid in nhl_ids or []:
        if int(pid) not in out:
            out[int(pid)] = prior_rates(int(pid), (groups or {}).get(int(pid), "F"), cfg)
    return out
