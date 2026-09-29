"""Per-person parameter table (C5): the DAG's person layer, and a Projection for C3 and the run.

One PersonParams per DK person (a Showdown CPT row and its FLEX row map to the same person). The
DAG per skater: opportunity (p_dress, TOI by strength) -> event rates per minute -> expected
events -> DK points, including the threshold bonuses as Poisson / negative-binomial tail
probabilities. Per goalie: p_start, shots against (opponent workload) and save skill -> saves,
goals against, win/OTL priors, shutout and 35+ save probabilities -> DK points. No simulation
(C6); these analytic moments are what C3's Projection reads.

Status per person: PRIOR when the person has no NHL id or no usable games; HISTORY when history
exposure is at least history_status_ratio x prior exposure (skaters: weighted minutes vs the
300 + 100 prior minutes; goalies: starts x league shots vs the 1,500-shot prior); MIXED between.
A PRIOR person's mean is prior dress (or start) probability x models.priors.prior_table (salary
band population prior blended with DK APPG where shown: the no-history fallback Ben allows).
That table is a per-game-played mean, so the probability puts PRIOR and history persons on one
scale; the parameter row still holds the config position priors for C6.
In a table where every person is PRIOR there is nothing to match, so the plain prior_table
means are kept (the C2 baseline, unchanged).
Run-level status: PRIOR if every person is PRIOR, HISTORY if every person is HISTORY, else MIXED.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
from scipy import stats

from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.contracts.ids import position_group
from nhl_dfs.contracts.statuses import ModelStatus
from nhl_dfs.intake.salary import SalaryPool
from nhl_dfs.models import goalies as goalie_mod
from nhl_dfs.models import opportunity as opp_mod
from nhl_dfs.models import rates as rates_mod
from nhl_dfs.models.priors import prior_table
from nhl_dfs.models.rates import STRENGTHS, load_model_config


@dataclass
class PersonParams:
    person_key: str
    name: str
    team: str  # DK code
    group: str  # F, D, G
    nhl_id: int | None
    opportunity: opp_mod.Opportunity | None
    rates: rates_mod.Rates | None
    goalie: goalie_mod.GoalieParams | None
    source: ModelStatus
    mean_tenths: int
    sd_tenths: int
    history_exposure: float = 0.0
    prior_exposure: float = 0.0


# -- analytic moments ------------------------------------------------------------------------------

def _nb_tail(k: int, mu: float, phi: float) -> float:
    """P(X >= k), X negative binomial with mean mu and var mu + phi mu^2 (Poisson when phi = 0)."""
    if mu <= 0:
        return 0.0
    if phi <= 1e-9:
        return float(stats.poisson.sf(k - 1, mu))
    r = 1.0 / phi
    return float(stats.nbinom.sf(k - 1, r, r / (r + mu)))


def skater_moments(o: opp_mod.Opportunity, r: rates_mod.Rates) -> tuple[float, float, dict]:
    """(mean tenths, sd tenths, expected events per dressed game)."""
    h = {s: getattr(o, f"toi_{s}_s") / 3600.0 for s in STRENGTHS}
    lg = sum(r.g60[s] * h[s] for s in STRENGTHS)
    la1 = sum(r.a1_60[s] * h[s] for s in STRENGTHS)
    la2 = sum(r.a2_60[s] * h[s] for s in STRENGTHS)
    lsog = sum(r.sog60[s] * h[s] for s in STRENGTHS)
    lblk = sum(r.blk60[s] * h[s] for s in STRENGTHS)
    lshp = r.pts60["sh"] * h["sh"]
    la = la1 + la2
    bonus_p = [float(stats.poisson.sf(2, lg)), _nb_tail(5, lsog, r.phi_sog), _nb_tail(3, lblk, r.phi_blk),
               float(stats.poisson.sf(2, lg + la))]
    base = 85 * lg + 50 * la + 15 * lsog + 13 * lblk + 20 * lshp + 30 * sum(bonus_p)
    var = (85 ** 2 * lg + 50 ** 2 * la + 15 ** 2 * (lsog + r.phi_sog * lsog ** 2)
           + 13 ** 2 * (lblk + r.phi_blk * lblk ** 2) + 20 ** 2 * lshp + sum(30 ** 2 * p * (1 - p) for p in bonus_p))
    p = o.p_dress
    mean = p * base
    total_var = p * (var + base ** 2) - mean ** 2
    return mean, math.sqrt(max(total_var, 0.0)), {"g": lg, "a1": la1, "a2": la2, "sog": lsog, "blk": lblk}


def goalie_moments(g: goalie_mod.GoalieParams, cfg: dict) -> tuple[float, float]:
    gc = cfg["goalies"]
    sa = g.shots_against
    lga = sa * (1.0 - g.sv_pct)
    saves = sa - lga
    p_so = math.exp(-lga) * (1.0 - min(0.5, g.pull_prob_per_ga))  # a pull before any goal is rare; bounded
    p35 = float(stats.poisson.sf(34, saves))
    pw, potl = float(gc["p_win"]), float(gc["p_otl"])
    base = 7 * saves - 35 * lga + 60 * pw + 20 * potl + 40 * p_so + 30 * p35
    var = 7 ** 2 * saves + 35 ** 2 * lga + 60 ** 2 * pw * (1 - pw) + 20 ** 2 * potl * (1 - potl) \
        + 40 ** 2 * p_so * (1 - p_so) + 30 ** 2 * p35 * (1 - p35)
    p = g.p_start
    mean = p * base
    return mean, math.sqrt(max(p * (var + base ** 2) - mean ** 2, 0.0))


# -- table -------------------------------------------------------------------------------------------

@dataclass
class ParamTable:
    persons: dict[str, PersonParams]
    role_map: dict[str, str]  # role_id -> person_key
    as_of: date
    notes: list[str] = field(default_factory=list)

    def mean_tenths(self, role_id: str) -> int:
        return self.persons[self.role_map[role_id]].mean_tenths

    def sd_tenths(self, role_id: str) -> int:
        return self.persons[self.role_map[role_id]].sd_tenths

    def counts(self) -> dict[str, int]:
        out = {s.value: 0 for s in (ModelStatus.PRIOR, ModelStatus.HISTORY, ModelStatus.MIXED)}
        for p in self.persons.values():
            out[p.source.value] += 1
        return out

    def source(self) -> ModelStatus:
        c = self.counts()
        n = sum(c.values())
        if n == 0 or c["PRIOR"] == n:
            return ModelStatus.PRIOR
        if c["HISTORY"] == n:
            return ModelStatus.HISTORY
        return ModelStatus.MIXED

    def to_frame(self) -> pd.DataFrame:
        rows = []
        for p in self.persons.values():
            o, r, g = p.opportunity, p.rates, p.goalie
            ev = skater_moments(o, r)[2] if (o is not None and r is not None) else {}
            rows.append({
                "person_key": p.person_key, "name": p.name, "team": p.team, "group": p.group,
                "nhl_id": int(p.nhl_id or 0), "nhl_id_missing": int(p.nhl_id is None), "source": p.source.value,
                "mean_tenths": p.mean_tenths, "sd_tenths": p.sd_tenths,
                "history_exposure": float(p.history_exposure), "prior_exposure": float(p.prior_exposure),
                "p_dress": float(o.p_dress) if o else 0.0,
                "toi_ev_s": float(o.toi_ev_s) if o else 0.0, "toi_pp_s": float(o.toi_pp_s) if o else 0.0,
                "toi_sh_s": float(o.toi_sh_s) if o else 0.0, "sd_toi_s": float(o.sd_toi_s) if o else 0.0,
                "pp_share": float(o.pp_share) if o else 0.0, "ev_scale": float(o.ev_scale) if o else 1.0,
                "unit_ev": o.unit_ev if o else "", "unit_pp": o.unit_pp if o else "",
                "units_missing": int(o.units_missing) if o else 1, "opportunity_source": o.source if o else "",
                **{f"{k}_per_game": float(ev.get(k, 0.0)) for k in ("g", "a1", "a2", "sog", "blk")},
                "shot_quality": float(r.shot_quality) if r else 0.0,
                "shot_quality_missing": int(r.shot_quality_missing) if r else 1,
                "a1_share": float(r.a1_share) if r else 0.0, "a1_share_missing": int(r.a1_share_missing) if r else 1,
                "phi_sog": float(r.phi_sog) if r else 0.0, "phi_blk": float(r.phi_blk) if r else 0.0,
                "n_games": int(r.n_games) if r else int(g.n_starts if g else 0),
                "p_start": float(g.p_start) if g else 0.0, "save_skill": float(g.save_skill) if g else 0.0,
                "save_skill_missing": int(g.save_skill_missing) if g else 1,
                "shots_against": float(g.shots_against) if g else 0.0, "workload_adj": float(g.workload_adj) if g else 0.0,
                "pull_prob_per_ga": float(g.pull_prob_per_ga) if g else 0.0, "b2b": int(g.b2b) if g else 0,
            })
        return pd.DataFrame(rows)


def _person_rows(pool: SalaryPool) -> dict:
    out = {}
    for pk, pr in pool.persons.items():
        r = pr.classic or pr.flex or pr.cpt
        if r is not None:
            out[pk] = r
    return out


def _status(history: float, prior: float, has: bool, ratio: float) -> ModelStatus:
    if not has or history <= 0:
        return ModelStatus.PRIOR
    return ModelStatus.HISTORY if history >= ratio * prior else ModelStatus.MIXED


def build(pool: SalaryPool, crosswalk: dict[str, int], as_of: date, cfg: dict | None = None, *, features=None,
          line_games: pd.DataFrame | None = None, roles: opp_mod.RoleState | None = None,
          team_map: dict[str, str] | None = None, store_root=None) -> ParamTable:
    """crosswalk: person_key -> nhl_id for resolved persons. features: an asof.FeatureFrame over all
    players (default: read from the store for as_of)."""
    from nhl_dfs.data.features import asof as asof_mod
    from nhl_dfs.data.history import store as store_mod

    cfg = cfg if cfg is not None else load_model_config()
    if features is None:
        features = asof_mod.frame(as_of, None, store_root=store_root)
    if line_games is None:
        line_games = store_mod.read("line_games", features.seasons, root=store_root)
    team_map = team_map or {}
    people = _person_rows(pool)
    groups = {pk: position_group(r.position) for pk, r in people.items()}
    ids = {pk: int(crosswalk[pk]) for pk in people if pk in crosswalk}
    skater_ids = [i for pk, i in ids.items() if groups[pk] != "G"]
    grp_by_id = {ids[pk]: groups[pk] for pk in ids}
    rts = rates_mod.estimate(features, cfg, nhl_ids=skater_ids, groups=grp_by_id)
    opps = opp_mod.estimate(features, roles, cfg, nhl_ids=skater_ids, groups=grp_by_id, line_games=line_games, as_of=as_of)

    # Skater opportunity per person (unmatched persons get the position prior), then reconcile per team.
    s_opp: dict[str, opp_mod.Opportunity] = {}
    for pk, r in people.items():
        if groups[pk] == "G":
            continue
        s_opp[pk] = opps[ids[pk]] if pk in ids else opp_mod.prior_opportunity(groups[pk], cfg, pk, roles)
    opp_mod.reconcile(s_opp, {pk: people[pk].team for pk in s_opp}, cfg)

    # Goalies: keyed by nhl_id when matched, else person_key; teams in NHL codes.
    nhl_team = {pk: team_map.get(r.team, r.team) for pk, r in people.items()}
    schedule = {}
    for gi in pool.games.values():
        home, away = team_map.get(gi.home, gi.home), team_map.get(gi.away, gi.away)
        day = gi.start_utc.date()
        schedule[home] = {"opponent": away, "game_date": day}
        schedule[away] = {"opponent": home, "game_date": day}
    team_goalies: dict[str, list] = {}
    for pk in people:
        if groups[pk] == "G":
            team_goalies.setdefault(nhl_team[pk], []).append(ids.get(pk, pk))
    gps = goalie_mod.estimate(features, schedule, cfg, team_goalies=team_goalies, as_of=as_of)

    pri = prior_table(pool)
    ratio = float(cfg.get("history_status_ratio", 1.0))
    persons: dict[str, PersonParams] = {}
    for pk, r in people.items():
        role_ids = [x.role_id for x in (pool.persons[pk].classic, pool.persons[pk].flex, pool.persons[pk].cpt) if x]
        prior_row = pri[next(x for x in role_ids if x in pri)]
        nid = ids.get(pk)
        if groups[pk] == "G":
            g = gps[ids.get(pk, pk)]
            hist = g.n_starts * float(cfg["goalies"]["shots_against"])
            prior_exp = float(cfg["goalies"]["prior_shots"])
            status = _status(hist, prior_exp, nid is not None and g.n_starts > 0, ratio)
            mean, sd = goalie_moments(g, cfg)
            pp = PersonParams(pk, r.name, r.team, "G", nid, None, None, g, status, 0, 0, hist, prior_exp)
        else:
            o = s_opp[pk]
            rt = rts.get(nid) if nid is not None else rates_mod.prior_rates(0, groups[pk], cfg)
            hist = rt.n_ev_min + rt.n_st_min
            status = _status(hist, rt.prior_exposure_min, nid is not None and rt.n_games > 0, ratio)
            mean, sd, _ = skater_moments(o, rt)
            pp = PersonParams(pk, r.name, r.team, groups[pk], nid, o, rt, None, status, 0, 0, hist, rt.prior_exposure_min)
        if status is ModelStatus.PRIOR:
            # No usable history: the population prior by salary band, blended with DK APPG where DK shows
            # one (models.priors, the fallback Ben allows), is a per-game-played mean; weight it by the
            # prior dress / start probability so PRIOR and history persons share one scale.
            p_play = g.p_start if groups[pk] == "G" else o.p_dress
            m, v = float(prior_row.mean_tenths), float(prior_row.sd_tenths) ** 2
            mean = p_play * m
            sd = math.sqrt(max(p_play * (v + m * m) - mean * mean, 0.0))
        pp.mean_tenths, pp.sd_tenths = int(round(mean)), max(1, int(round(sd)))
        persons[pk] = pp
    if all(p.source is ModelStatus.PRIOR for p in persons.values()):
        # Nothing to share a scale with: keep the plain priors, so an all-prior run is exactly the
        # C2 baseline (dress / start weighting exists only to mix PRIOR and history persons).
        for pk, pp in persons.items():
            rid = next(x.role_id for x in (pool.persons[pk].classic, pool.persons[pk].flex, pool.persons[pk].cpt) if x)
            pp.mean_tenths, pp.sd_tenths = pri[rid].mean_tenths, pri[rid].sd_tenths
    role_map = {row.role_id: row.person_key for row in pool.rows}
    return ParamTable(persons, role_map, as_of, list(features.notes))


def resolve_ids(pool: SalaryPool, *, store_root=None, accepted_path=None) -> tuple[dict[str, int], dict[str, str]]:
    """person_key -> nhl_id from accepted.csv plus exact matches against stored history. Never
    writes accepted.csv or proposals.json. Also returns the verified DK -> NHL team map."""
    from nhl_dfs.data.history import store as store_mod
    from nhl_dfs.data.identity import crosswalk as cw

    seasons = sorted({s for k in ("skater_games", "goalie_games") for s in store_mod.seasons_present(k, root=store_root)})
    directory = cw.directory_from_history(seasons, store_root=store_root)
    res = cw.seed(pool, directory=directory, accepted_path=accepted_path, write=False)
    return dict(res.accepted), cw.verified_team_map()


def projection_for(pool: SalaryPool, as_of: date, *, store_root=None, accepted_path=None) -> ParamTable:
    ids, team_map = resolve_ids(pool, store_root=store_root, accepted_path=accepted_path)
    return build(pool, ids, as_of, team_map=team_map, store_root=store_root)
