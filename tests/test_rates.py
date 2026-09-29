from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from nhl_dfs.models import rates

pytestmark = pytest.mark.c5


@pytest.fixture(scope="module")
def cfg():
    return rates.load_model_config()


def games(n, *, pid=1, pos="C", ev=900, pp=120, sh=0, goals=1, assists=1, pp_points=1, sog=4, blocks=1,
          tier="A", start=date(2025, 10, 1)):
    rows = []
    for i in range(n):
        rows.append({"nhl_id": pid, "game_id": 2025020000 + i, "game_date": start + timedelta(days=2 * i),
                     "position": pos, "regime": "regular", "toi_ev_s": ev, "toi_pp_s": pp, "toi_sh_s": sh,
                     "toi_s": ev + pp + sh, "goals": goals, "assists": assists, "pp_points": pp_points,
                     "sh_points": 0, "sog": sog, "blocks": blocks, "a1": assists * 0.8 if tier == "A" else assists * 0.55,
                     "a1_missing": 0 if tier == "A" else 1, "ixg": sog * 0.2 if tier == "A" else sog * 0.1,
                     "ixg_missing": 0 if tier == "A" else 1})
    return pd.DataFrame(rows)


def test_zero_history_returns_the_prior_exactly(cfg):
    r = rates.estimate(pd.DataFrame(), cfg, nhl_ids=[7], groups={7: "D"})[7]
    p = cfg["skaters"]["D"]
    assert r.pts60 == p["pts60"] and r.goal_share == p["goal_share"] and r.a1_share == p["a1_share"]
    assert r.shot_quality == p["ixg_per_sog"] and r.shot_quality_missing == 1 and r.a1_share_missing == 1
    total = sum(p["toi_s"].values())
    implied = sum(r.sog60[s] * p["toi_s"][s] for s in rates.STRENGTHS) / total
    assert implied == pytest.approx(p["sog60"])  # the league all-situations rate is reproduced


def test_long_history_converges_to_the_sample_rate(cfg):
    c = {**cfg, "decay_half_life_games": 1e9}  # no decay: a clean sample mean
    r = rates.estimate(games(3000), c)[1]
    ev_pts_per60 = 1 / (900 / 3600)  # 1 EV point (goal + assist - PP point) per 15 EV minutes
    assert r.pts60["ev"] == pytest.approx(ev_pts_per60, rel=0.01)
    assert r.pts60["pp"] == pytest.approx(1 / (120 / 3600), rel=0.02)
    assert r.goal_share == pytest.approx(0.5, abs=0.01) and r.a1_share == pytest.approx(0.8, abs=0.01)
    assert r.shot_quality == pytest.approx(0.2, abs=0.005)
    m = cfg["strength_mult"]["sog"]
    per_game = r.sog60["ev"] * 900 / 3600 + r.sog60["pp"] * 120 / 3600
    assert per_game == pytest.approx(4, rel=0.01) and r.sog60["pp"] / r.sog60["ev"] == pytest.approx(m["pp"])


def test_short_history_sits_between_prior_and_sample(cfg):
    r = rates.estimate(games(5, goals=3, assists=3, pp_points=0), cfg)[1]
    assert cfg["skaters"]["F"]["pts60"]["ev"] < r.pts60["ev"] < 6 / 15 * 60


def test_decay_weights_favor_recent_games(cfg):
    w = rates.decay_weights(81, 40)
    assert w[0] == 1.0 and w[40] == pytest.approx(0.5) and w[80] == pytest.approx(0.25)
    old = games(20, goals=0, assists=0, pp_points=0, start=date(2024, 10, 1))
    new = games(20, goals=2, assists=2, pp_points=0, start=date(2025, 10, 1))
    new["game_id"] += 100
    both = pd.concat([old, new])
    weak = {**cfg, "ev_prior_min": 1, "st_prior_min": 1}  # isolate decay from prior shrinkage
    decayed = rates.estimate(both, {**weak, "decay_half_life_games": 5})[1].pts60["ev"]
    flat = rates.estimate(both, {**weak, "decay_half_life_games": 1e9})[1].pts60["ev"]
    assert decayed > flat


def test_tier_b_shares_stay_on_priors_with_indicators(cfg):
    r = rates.estimate(games(50, tier="B"), cfg)[1]
    assert r.a1_share == cfg["skaters"]["F"]["a1_share"] and r.a1_share_missing == 1
    assert r.shot_quality == cfg["skaters"]["F"]["ixg_per_sog"] and r.shot_quality_missing == 1


def test_dispersion_non_negative_and_leaks_nothing_from_other_regimes(cfg):
    g = games(30)
    g.loc[g.index[:5], "regime"] = "playoffs"
    g.loc[g.index[:5], "goals"] = 50
    r = rates.estimate(g, cfg)[1]
    assert r.n_games == 25 and r.phi_sog >= 0 and r.phi_blk >= 0
    assert np.isfinite([r.pts60[s] for s in rates.STRENGTHS]).all()
