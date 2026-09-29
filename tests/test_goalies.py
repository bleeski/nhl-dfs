from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from nhl_dfs.models import goalies
from nhl_dfs.models.rates import load_model_config

pytestmark = pytest.mark.c5

SLATE = date(2025, 12, 10)


@pytest.fixture(scope="module")
def cfg():
    return load_model_config()


def history(starter_pattern, *, last_day=SLATE - timedelta(days=1), tier="A"):
    """VAN goalie starts, most recent last; starter_pattern like [1, 1, 2, ...] (nhl ids)."""
    rows = []
    n = len(starter_pattern)
    for i, gid in enumerate(starter_pattern):
        d = last_day - timedelta(days=2 * (n - 1 - i))
        rows.append({"nhl_id": gid, "game_id": 2025020000 + i, "game_date": d, "team": "VAN", "regime": "regular",
                     "started": True, "toi_s": 3600, "shots_against": 30.0, "goals_against": 2.0,
                     "xga": 3.0 if tier == "A" else 2.85, "xga_missing": 0 if tier == "A" else 1})
    return pd.DataFrame(rows)


def feats(gl, sk=None):
    return SimpleNamespace(goalies=gl, skaters=sk if sk is not None else pd.DataFrame())


SCHED = {"VAN": {"opponent": "EDM", "game_date": SLATE}}


def test_team_goalies_sum_to_one_and_b2b_lowers_the_starter(cfg):
    gl = history([1] * 30 + [2] * 5 + [1])  # goalie 1 started last night
    out = goalies.estimate(feats(gl), SCHED, cfg, team_goalies={"VAN": [1, 2]}, as_of=SLATE)
    assert out[1].p_start + out[2].p_start == pytest.approx(1.0)
    assert out[1].b2b == 1 and out[2].b2b == 0
    rested = history([1] * 30 + [2] * 5 + [1], last_day=SLATE - timedelta(days=3))
    ref = goalies.estimate(feats(rested), SCHED, cfg, team_goalies={"VAN": [1, 2]}, as_of=SLATE)
    assert out[1].p_start < ref[1].p_start and ref[1].p_start + ref[2].p_start == pytest.approx(1.0)


def test_unknown_goalie_gets_prior_share_then_renormalized(cfg):
    out = goalies.estimate(feats(pd.DataFrame()), SCHED, cfg, team_goalies={"VAN": [1, "x|VAN|G"]}, as_of=SLATE)
    assert out[1].p_start == pytest.approx(0.5) and out["x|VAN|G"].source == "prior"


def test_save_skill_prior_and_tier_b(cfg):
    gl = history([1] * 50)
    r = goalies.estimate(feats(gl), SCHED, cfg, team_goalies={"VAN": [1]}, as_of=SLATE)[1]
    assert r.save_skill == pytest.approx((50 * 1.0) / (50 * 30 + cfg["goalies"]["prior_shots"]))
    assert r.save_skill_missing == 0 and r.p_start == 1.0
    b = goalies.estimate(feats(history([1] * 50, tier="B")), SCHED, cfg, team_goalies={"VAN": [1]}, as_of=SLATE)[1]
    assert b.save_skill == 0.0 and b.save_skill_missing == 1


def test_workload_uses_the_opponents_shots_for(cfg):
    def team_rows(team, sog_per_game, n=40):
        return pd.DataFrame([{"team": team, "game_id": 2025020000 + i, "game_date": SLATE - timedelta(days=i + 1),
                              "sog": sog_per_game, "regime": "regular"} for i in range(n)])
    sk = pd.concat([team_rows("EDM", 36.0), team_rows("VAN", 20.0)])
    r = goalies.estimate(feats(history([1] * 5), sk), SCHED, cfg, team_goalies={"VAN": [1]}, as_of=SLATE)[1]
    assert r.workload_adj > 1.0 and r.shots_against > cfg["goalies"]["shots_against"]  # EDM shoots a lot
    calm = goalies.estimate(feats(history([1] * 5), sk), {"VAN": {"opponent": "VAN", "game_date": SLATE}}, cfg,
                            team_goalies={"VAN": [1]}, as_of=SLATE)[1]
    assert calm.workload_adj < 1.0


def test_leakage_guard_ignores_games_on_or_after_as_of(cfg):
    gl = pd.concat([history([1] * 10, last_day=SLATE - timedelta(days=3)),
                    history([2] * 40, last_day=SLATE + timedelta(days=90))])
    out = goalies.estimate(feats(gl), SCHED, cfg, team_goalies={"VAN": [1, 2]}, as_of=SLATE)
    assert out[1].p_start > out[2].p_start and out[2].n_starts < 40
