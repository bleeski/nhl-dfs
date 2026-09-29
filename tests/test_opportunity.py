from datetime import date, timedelta

import pandas as pd
import pytest

from nhl_dfs.models import opportunity as opp
from nhl_dfs.models.rates import load_model_config

pytestmark = pytest.mark.c5

AS_OF = date(2025, 12, 1)


@pytest.fixture(scope="module")
def cfg():
    return load_model_config()


def team_games(n, team="VAN", players=((1, "C", 900, 150), (2, "D", 1200, 60), (3, "L", 700, 0)), skip=None,
               start=date(2025, 10, 1)):
    """n team games; each player row: (nhl_id, position, EV s, PP s). skip: {nhl_id: set of game indexes}."""
    rows = []
    for i in range(n):
        for pid, pos, ev, pp in players:
            if skip and i in skip.get(pid, set()):
                continue
            rows.append({"nhl_id": pid, "game_id": 2025020000 + i, "game_date": start + timedelta(days=2 * i),
                         "team": team, "position": pos, "regime": "regular", "team_stint": 0,
                         "toi_ev_s": ev, "toi_pp_s": pp, "toi_sh_s": 30, "toi_s": ev + pp + 30})
    return pd.DataFrame(rows)


def test_zero_history_is_the_prior(cfg):
    o = opp.estimate(pd.DataFrame(), None, cfg, nhl_ids=[9], groups={9: "D"})[9]
    p = cfg["skaters"]["D"]
    assert (o.toi_ev_s, o.toi_pp_s, o.toi_sh_s, o.p_dress) == (p["toi_s"]["ev"], p["toi_s"]["pp"], p["toi_s"]["sh"], p["p_dress"])
    assert o.source == "prior" and o.units_missing == 1


def test_pp_share_denominator_is_team_pp_clock(cfg):
    unit = tuple((10 + i, "C", 800, 150) for i in range(5)) + ((20, "D", 1200, 60),)
    o = opp.estimate(team_games(400, players=unit), None, {**cfg, "decay_half_life_games": 1e9},
                     as_of=date(2030, 1, 1))
    clock = (5 * 150 + 60) / cfg["team"]["skaters_on_ice"]  # summed skater PP seconds / skaters on ice
    assert o[10].pp_share == pytest.approx(150 / clock, rel=0.02)
    assert o[20].pp_share == pytest.approx(60 / clock, rel=0.02)


def test_p_dress_counts_only_the_current_stint(cfg):
    sk = team_games(60, skip={3: set(range(0, 60, 2))})  # player 3 dresses in half of VAN's games
    four = team_games(60, players=((4, "C", 900, 0),))
    edm = four.iloc[:30].assign(team="EDM", game_id=four["game_id"].iloc[:30] + 9000)
    van = four.iloc[30:].assign(team_stint=1)  # joins VAN at its 31st game, same game ids as teammates
    edm_team = team_games(30, team="EDM", players=((50, "C", 900, 0),)).assign(game_id=edm["game_id"].to_numpy())
    sk = pd.concat([sk, edm, van, edm_team])
    o = opp.estimate(sk, None, {**cfg, "prior_games": {**cfg["prior_games"], "dress": 0.001}}, as_of=date(2026, 6, 1))
    assert o[3].p_dress == pytest.approx(0.5, abs=0.01)
    assert o[4].p_dress == pytest.approx(1.0, abs=0.01)  # not charged for VAN games before he arrived


def test_as_of_guards_features_and_line_games(cfg):
    sk = team_games(10)
    future = team_games(1, start=AS_OF, players=((1, "C", 3000, 900),))
    o = opp.estimate(pd.concat([sk, future]), None, cfg, as_of=AS_OF)
    assert o[1].n_games == 10
    lines = pd.DataFrame([
        {"line_id": "L1", "player_ids": "1|3|5", "situation": "5on5", "toi_s": 400, "game_id": 1, "game_date": date(2025, 11, 1)},
        {"line_id": "FUTURE", "player_ids": "1|3|5", "situation": "5on5", "toi_s": 9999, "game_id": 2, "game_date": AS_OF},
        {"line_id": "P1", "player_ids": "1|2|3|5|6", "situation": "5on4", "toi_s": 100, "game_id": 1, "game_date": date(2025, 11, 1)},
    ])
    o = opp.estimate(sk, None, cfg, line_games=lines, as_of=AS_OF)
    assert (o[1].unit_ev, o[1].unit_pp, o[1].units_missing) == ("L1", "P1", 0)
    assert o[2].unit_ev == "" and o[2].unit_pp == "P1"


def test_call_up_with_pp1_role_gets_the_role_expectation(cfg):
    roles = opp.RoleState(ev_line={77: 1}, pp_unit={77: 1})
    o = opp.estimate(team_games(5), roles, cfg, nhl_ids=[77], groups={77: "F"}, as_of=AS_OF)[77]
    r = cfg["roles"]["F"]
    assert o.source == "role" and o.toi_ev_s == r["ev_line_toi_s"][0] and o.toi_pp_s == r["pp_unit_toi_s"][0]
    plain = opp.estimate(team_games(5), None, cfg, nhl_ids=[77], groups={77: "F"}, as_of=AS_OF)[77]
    assert plain.source == "prior" and plain.toi_pp_s == cfg["skaters"]["F"]["toi_s"]["pp"]


def test_reconcile_scales_depth_to_the_manpower_budget(cfg):
    t = cfg["team"]
    budget = t["skaters_on_ice"] * (3600 - t["pp_clock_s"] - t["pk_clock_s"])
    opps = {i: opp.Opportunity(1.0, 1100.0 if i < 9 else 900.0, 0, 0, 100, 0) for i in range(18)}
    opp.reconcile(opps, {i: "VAN" for i in opps}, cfg)
    total = sum(o.p_dress * o.toi_ev_s for o in opps.values())
    assert total <= budget + 1e-6 and opps[0].toi_ev_s == 1100.0 and opps[17].ev_scale < 1.0
    assert min(o.ev_scale for o in opps.values()) >= t["min_scale"]


def _o(p, source):
    o = opp.Opportunity(p, 800.0, 60.0, 30.0, 150.0, 0.2)
    o.source = source
    return o


def test_dress_budget_history_first_then_residual(cfg):
    opps = {f"h{i}": _o(0.9, "history") for i in range(8)} | {f"u{i}": _o(0.85, "prior") for i in range(10)}
    opps |= {f"d{i}": _o(0.9, "history") for i in range(9)}
    groups = {k: ("D" if k.startswith("d") else "F") for k in opps}
    opp.dress_budget(opps, {k: "VAN" for k in opps}, groups, cfg)
    fwd = sum(o.p_dress for k, o in opps.items() if groups[k] == "F")
    dmen = sum(o.p_dress for k, o in opps.items() if groups[k] == "D")
    assert fwd == pytest.approx(12.0) and dmen == pytest.approx(6.0)
    assert opps["h0"].p_dress == pytest.approx(0.9)  # history evidence kept: 7.2 < 12
    assert opps["u0"].p_dress == pytest.approx((12 - 7.2) / 10, rel=1e-6)  # equal unknowns share the residual
    assert opps["d0"].p_dress == pytest.approx(6 / 9, rel=1e-6)  # history alone over budget: cut (equal priors)


def test_dress_budget_cut_spares_regulars(cfg):
    ps = [0.97] * 6 + [0.9] * 6 + [0.5] * 6  # 15.3 expected forwards with history
    opps = {f"h{i}": _o(p, "history") for i, p in enumerate(ps)}
    opp.dress_budget(opps, {k: "BOS" for k in opps}, {k: "F" for k in opps}, cfg)
    new = [opps[f"h{i}"].p_dress for i in range(18)]
    assert sum(new) == pytest.approx(12.0, rel=1e-6)
    assert new[0] > 0.9 and new[0] / 0.97 > new[12] / 0.5  # the regular keeps far more of his probability


def test_dress_budget_floor_and_never_scales_up(cfg):
    over = {f"h{i}": _o(0.95, "history") for i in range(14)} | {"call_up": _o(0.85, "role")}
    opp.dress_budget(over, {k: "EDM" for k in over}, {k: "F" for k in over}, cfg)
    assert over["call_up"].p_dress == cfg["team"]["dress_budget"]["dress_floor"]
    small = {"a": _o(0.9, "history"), "b": _o(0.85, "prior")}
    opp.dress_budget(small, {k: "EDM" for k in small}, {k: "F" for k in small}, cfg)
    assert (small["a"].p_dress, small["b"].p_dress) == (0.9, 0.85)  # a short list is not inflated
