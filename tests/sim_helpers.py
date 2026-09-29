"""Test-only builders for simulator inputs: a synthetic ParamTable and a SlateSpec (no history)."""

from __future__ import annotations

from datetime import date

from nhl_dfs.contracts.statuses import ModelStatus
from nhl_dfs.models import goalies as goalie_mod
from nhl_dfs.models import opportunity as opp_mod
from nhl_dfs.models import rates as rates_mod
from nhl_dfs.models.params import ParamTable, PersonParams
from nhl_dfs.sim import market
from nhl_dfs.sim.game import GameSpec, SlateSpec

MODEL_CFG = rates_mod.load_model_config()


def synthetic_params(teams=("AAA", "BBB"), *, n_f=13, n_d=7, n_g=2, units=True, p_starts=(0.8, 0.2),
                     p_dress=None) -> ParamTable:
    """Prior-rate people with line structure: forwards in threes, defense in pairs, two PP units."""
    persons = {}
    for team in teams:
        opps = {}
        for i in range(n_f):
            o = opp_mod.prior_opportunity("F", MODEL_CFG)
            o.toi_ev_s = max(500.0, 960.0 - 85.0 * (i // 3))
            o.toi_pp_s = 150.0 if i < 5 else (60.0 if i < 10 else 0.0)
            if units:
                o.unit_ev = f"{team}-L{i // 3}"
                o.unit_pp = f"{team}-PP{1 if i < 3 else 2}" if i < 6 else ""
                o.units_missing = 0
            opps[(f"{team}-F{i:02d}", "F")] = o
        for i in range(n_d):
            o = opp_mod.prior_opportunity("D", MODEL_CFG)
            o.toi_ev_s = max(700.0, 1200.0 - 100.0 * (i // 2))
            o.toi_pp_s = 100.0 if i < 2 else 0.0
            if units:
                o.unit_ev = f"{team}-P{i // 2}"
                o.unit_pp = f"{team}-PP1" if i < 2 else ""
                o.units_missing = 0
            opps[(f"{team}-D{i:02d}", "D")] = o
        keyed = {f"{n}|{team}|{g}": o for (n, g), o in opps.items()}
        opp_mod.dress_budget(keyed, {k: team for k in keyed}, {k: k.split("|")[2] for k in keyed}, MODEL_CFG)
        for (n, g), o in opps.items():
            key = f"{n}|{team}|{g}"
            if p_dress is not None:
                keyed[key].p_dress = float(p_dress)
            persons[key] = PersonParams(key, n, team, g, None, keyed[key], rates_mod.prior_rates(0, g, MODEL_CFG), None,
                                        ModelStatus.PRIOR, 0, 0)
        for j in range(n_g):
            key = f"{team}-G{j}|{team}|G"
            gp = goalie_mod.GoalieParams(key, team, p_starts[j] if j < len(p_starts) else 0.0, 0.0,
                                         float(MODEL_CFG["goalies"]["sv_pct"]), float(MODEL_CFG["goalies"]["shots_against"]),
                                         1.0, float(MODEL_CFG["goalies"]["pull_per_ga"]))
            persons[key] = PersonParams(key, f"{team}-G{j}", team, "G", None, None, None, gp, ModelStatus.PRIOR, 0, 0)
    return ParamTable(persons, {}, date(2026, 10, 1))


def slate_for(params: ParamTable, games=(("AAA", "BBB"),), *, lam=(2.75, 2.75)) -> SlateSpec:
    """A model-source slate: home, away pairs, every game at the given intensities."""
    cfg = market.load_sim_config()
    specs = []
    for home, away in games:
        gr = market.fit_game(None, market.TeamStrength(lam[0], lam[1]), None, None, cfg=cfg)
        specs.append(GameSpec(f"{away}@{home}", home, away, gr))
    return SlateSpec(specs, "classic", cfg, MODEL_CFG)
