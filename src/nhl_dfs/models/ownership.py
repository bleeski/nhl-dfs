"""Hand-weighted ownership prior (C3, plan section 6 "Cold start").

utilities() gives each role's perceived value to the field, in uncaptained DK points, per
contest family. It is not an ownership percentage: ownership is read off the sampled field
(models/field.py), so it is coherent with salary, position and team rules by construction.
The same feature_table feeds models/prefit.py, so fitted weights drop into config/ownership.yaml.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Mapping

import yaml

from nhl_dfs.contracts.geometry import Mode, PoolRow
from nhl_dfs.contracts.ids import position_group
from nhl_dfs.contracts.statuses import Participation
from nhl_dfs.data.sources.nhl import OddsSnapshot
from nhl_dfs.intake.salary import SalaryPool
from nhl_dfs.models.projection import Projection

REPO_ROOT = Path(__file__).resolve().parents[3]
OWNERSHIP_YAML = REPO_ROOT / "config" / "ownership.yaml"

FEATURES = ("appg", "value_z", "salary_rank", "implied_total", "pp1", "line1", "goalie_start_win",
            "questionable", "unknown", "news_recent")
DEFAULT_FAMILY = "large_gpp"


def load_ownership_config(path: Path = OWNERSHIP_YAML) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    validate_ownership_config(cfg)
    return cfg


def validate_ownership_config(cfg: dict) -> None:
    w = cfg.get("weights") or {}
    missing = [k for k in FEATURES if k not in w]
    if missing:
        raise ValueError(f"ownership weights missing {missing}")
    for fam, over in (cfg.get("families") or {}).items():
        bad = [k for k in (over or {}) if k not in FEATURES]
        if bad:
            raise ValueError(f"ownership family {fam!r} overrides unknown features {bad}")
    gsp = cfg.get("goalie_start_prior") or {}
    for k in ("top_salary_on_team", "other"):
        if not 0.0 <= float(gsp.get(k, -1)) <= 1.0:
            raise ValueError(f"goalie_start_prior.{k} must be in [0, 1]")
    fld = cfg.get("field") or {}
    behaviors = fld.get("behaviors") or {}
    if not behaviors:
        raise ValueError("ownership field has no behaviors")
    for name, b in behaviors.items():
        if float(b["noise_sd"]) < 0:
            raise ValueError(f"behavior {name}: noise_sd must be non-negative")
        if b["stack_rule"] not in ("none", "team3"):
            raise ValueError(f"behavior {name}: stack_rule must be none or team3")
        if b["captain_rule"] not in (fld.get("captain_rules") or {}):
            raise ValueError(f"behavior {name}: unknown captain_rule {b['captain_rule']!r}")
    for fam, mix in (fld.get("mixtures") or {}).items():
        unknown = [k for k in mix if k not in behaviors]
        if unknown:
            raise ValueError(f"mixture {fam!r} names unknown behaviors {unknown}")
        if any(float(v) < 0 for v in mix.values()) or abs(sum(float(v) for v in mix.values()) - 1.0) > 1e-6:
            raise ValueError(f"mixture {fam!r} weights must be non-negative and sum to 1")
    if DEFAULT_FAMILY not in (fld.get("mixtures") or {}):
        raise ValueError(f"ownership field needs a {DEFAULT_FAMILY!r} mixture")
    buckets = (cfg.get("dup_proxy") or {}).get("salary_left_buckets") or []
    if not buckets or buckets[-1].get("max_left") is not None:
        raise ValueError("dup_proxy.salary_left_buckets must end with max_left: null")
    if float((cfg.get("dup_proxy") or {}).get("own_floor_pct", 0)) <= 0:
        raise ValueError("dup_proxy.own_floor_pct must be positive")


# -- odds ---------------------------------------------------------------------------------------

def _implied_prob(price: int) -> float:
    return 100.0 / (price + 100.0) if price > 0 else -price / (-price + 100.0)


def team_odds(pool: SalaryPool, odds: OddsSnapshot | None, cfg: dict) -> tuple[dict[str, float], dict[str, float]]:
    """(team implied goals, team no-vig win probability) for pool teams the odds name exactly."""
    totals: dict[str, float] = {}
    wins: dict[str, float] = {}
    if odds is None:
        return totals, wins
    slope = float(cfg.get("implied_split_slope", 0.5))
    for g in odds.games:
        if g.home_abbrev not in pool.teams or g.away_abbrev not in pool.teams:
            continue
        p_home = None
        if g.home_ml is not None and g.away_ml is not None:
            ph, pa = _implied_prob(g.home_ml), _implied_prob(g.away_ml)
            p_home = ph / (ph + pa)
            wins[g.home_abbrev], wins[g.away_abbrev] = p_home, 1.0 - p_home
        if g.total_line is not None:
            ph = 0.5 if p_home is None else p_home
            totals[g.home_abbrev] = g.total_line * (0.5 + slope * (ph - 0.5))
            totals[g.away_abbrev] = g.total_line * (0.5 - slope * (ph - 0.5))
    return totals, wins


# -- features -----------------------------------------------------------------------------------

def _person_rows(pool: SalaryPool) -> dict[str, PoolRow]:
    """One salary-bearing row per person: the Classic row, or the Showdown FLEX row."""
    out: dict[str, PoolRow] = {}
    for r in pool.rows:
        if pool.mode is Mode.SHOWDOWN and "CPT" in r.roster_positions:
            out.setdefault(r.person_key, r)
        else:
            out[r.person_key] = r
    return out


def _base_salary(pool: SalaryPool, r: PoolRow) -> float:
    if pool.mode is Mode.SHOWDOWN and "CPT" in r.roster_positions:
        return r.salary / 1.5  # a CPT row with no FLEX row in the pool
    return float(r.salary)


def feature_table(
    pool: SalaryPool,
    proj: Projection,
    odds: OddsSnapshot | None = None,
    roles: Mapping[str, Mapping[str, object]] | None = None,
    cfg: dict | None = None,
    *,
    statuses: Mapping[str, Participation] | None = None,
    news_age_h: Mapping[str, float] | None = None,
) -> dict[str, dict[str, float]]:
    """role_id -> feature -> value (see config/ownership.yaml). A Showdown CPT row carries its
    person's features. roles: role_id -> {"pp1": bool, "line": int} (C7). statuses: role_id ->
    Participation from the DK status. news_age_h: role_id -> hours since the latest news."""
    cfg = cfg if cfg is not None else load_ownership_config()
    roles, statuses, news_age_h = roles or {}, statuses or {}, news_age_h or {}
    base = _person_rows(pool)
    totals, wins = team_odds(pool, odds, cfg)
    avg_total = statistics.fmean(totals.values()) if totals else 0.0

    by_group: dict[str, list[PoolRow]] = defaultdict(list)
    for r in base.values():
        by_group[position_group(r.position)].append(r)
    rank: dict[str, float] = {}
    value: dict[str, float] = {}
    for rows in by_group.values():
        sal = sorted(_base_salary(pool, r) for r in rows)
        for r in rows:
            s = _base_salary(pool, r)
            below = sum(1 for x in sal if x < s)
            ties = sum(1 for x in sal if x == s) - 1
            rank[r.person_key] = (below + ties / 2.0) / (len(sal) - 1) if len(sal) > 1 else 0.5
        vals = {r.person_key: (proj.mean_tenths(r.role_id) / 10.0) / (_base_salary(pool, r) / 1000.0) for r in rows}
        mu = statistics.fmean(vals.values())
        sd = statistics.pstdev(vals.values()) if len(vals) > 1 else 0.0
        for k, v in vals.items():
            value[k] = (v - mu) / sd if sd > 0 else 0.0

    gsp = cfg["goalie_start_prior"]
    top_goalie: dict[str, str] = {}
    for r in sorted((r for r in base.values() if r.is_goalie), key=lambda r: (-r.salary, r.role_id)):
        top_goalie.setdefault(r.team, r.person_key)
    even = float(gsp["top_salary_on_team"]) * 0.5
    recent_h = float(cfg.get("news_recent_hours", 3))

    person_feats: dict[str, dict[str, float]] = {}
    for pk, r in base.items():
        mean_pts = proj.mean_tenths(r.role_id) / 10.0
        appg = r.appg_raw if r.appg_flag in ("VALUE", "APPG_ZERO") and r.appg_raw is not None else None
        f = {
            "appg": (appg - mean_pts) if appg is not None else 0.0,
            "value_z": value[pk],
            "salary_rank": rank[pk] - 0.5,
            "implied_total": (totals[r.team] - avg_total) if r.team in totals else 0.0,
            "pp1": 0.0,
            "line1": 0.0,
            "goalie_start_win": 0.0,
            "questionable": 0.0,
            "unknown": 0.0,
            "news_recent": 0.0,
        }
        if r.is_goalie:
            p_start = float(gsp["top_salary_on_team"] if top_goalie.get(r.team) == pk else gsp["other"])
            f["goalie_start_win"] = p_start * wins.get(r.team, 0.5) - even
        person_feats[pk] = f

    out: dict[str, dict[str, float]] = {}
    for r in pool.rows:
        f = dict(person_feats[r.person_key])
        role = roles.get(r.role_id) or {}
        f["pp1"] = 1.0 if role.get("pp1") else 0.0
        f["line1"] = 1.0 if role.get("line") == 1 else 0.0
        st = statuses.get(r.role_id)
        f["questionable"] = 1.0 if st is Participation.QUESTIONABLE else 0.0
        f["unknown"] = 1.0 if st is Participation.UNKNOWN else 0.0
        age = news_age_h.get(r.role_id)
        f["news_recent"] = 1.0 if age is not None and age <= recent_h else 0.0
        out[r.role_id] = f
    return out


def family_weights(cfg: dict, family: str) -> dict[str, float]:
    w = {k: float(v) for k, v in cfg["weights"].items()}
    w.update({k: float(v) for k, v in ((cfg.get("families") or {}).get(family) or {}).items()})
    return w


def perceived(pool: SalaryPool, proj: Projection, feats: Mapping[str, Mapping[str, float]],
              weights: Mapping[str, float]) -> dict[str, float]:
    return {r.role_id: proj.mean_tenths(r.role_id) / 10.0 + sum(weights[k] * feats[r.role_id][k] for k in FEATURES)
            for r in pool.rows}


def utilities(
    pool: SalaryPool,
    proj: Projection,
    contests: Iterable,
    odds: OddsSnapshot | None,
    roles: Mapping[str, Mapping[str, object]] | None = None,
    cfg: dict | None = None,
    *,
    statuses: Mapping[str, Participation] | None = None,
    news_age_h: Mapping[str, float] | None = None,
) -> dict[str, dict[str, float]]:
    """family -> role_id -> perceived uncaptained points. contests: anything with a .family
    (models.contests.ContestContext or FamilyPrior); none gives the default family."""
    cfg = cfg if cfg is not None else load_ownership_config()
    families = sorted({c.family for c in contests}) or [DEFAULT_FAMILY]
    feats = feature_table(pool, proj, odds, roles, cfg, statuses=statuses, news_age_h=news_age_h)
    return {fam: perceived(pool, proj, feats, family_weights(cfg, fam)) for fam in families}


def log_floor(pct: float, floor_pct: float) -> float:
    return math.log(max(pct, floor_pct) / 100.0)
