"""C6: the calibration harness runs on stored history and writes an honest report."""

import json
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from nhl_dfs.data.history import store
from nhl_dfs.sim import market, validate
from test_nhl_reports import run as run_reports

pytestmark = pytest.mark.c6

SEASON = 20252026
TEAMS = ("DAL", "WPG")
N_GAMES = 40
FIRST = date(2025, 10, 10)


@pytest.fixture(scope="module")
def synthetic_store(tmp_path_factory):
    """A small history store with the real schema (from the one recorded game) and synthetic rows:
    two teams, 12 forwards, 6 defensemen and 2 goalies each, 40 games."""
    tmp = tmp_path_factory.mktemp("val")
    _, sk_t, gl_t, _ = run_reports(tmp, moneypuck=False, sub="tmpl")
    f_t = sk_t[sk_t["position"] != "D"].iloc[0].to_dict()
    d_t = sk_t[sk_t["position"] == "D"].iloc[0].to_dict()
    g_t = gl_t.iloc[0].to_dict()
    rng = np.random.default_rng(7)
    sk_rows, gl_rows = [], []
    for k in range(N_GAMES):
        day = FIRST + timedelta(days=k)
        gid = 2025020100 + k
        home = TEAMS[k % 2]
        winner = TEAMS[int(rng.random() < 0.5)]
        for ti, team in enumerate(TEAMS):
            opp = TEAMS[1 - ti]
            players = [(f"{team}F{i}", "C" if i < 4 else ("L" if i < 8 else "R"), f_t, 1000 + 100 * ti + i) for i in range(12)] + \
                      [(f"{team}D{i}", "D", d_t, 1050 + 100 * ti + i) for i in range(6)]
            for name, pos, tmpl, pid in players:
                row = dict(tmpl)
                is_d = pos == "D"
                goals = int(rng.poisson(0.08 if is_d else 0.3))
                assists = int(rng.poisson(0.3 if is_d else 0.45))
                sog = goals + int(rng.poisson(1.5 if is_d else 2.3))
                row.update(nhl_id=pid, name=name, position=pos, team=team, opponent=opp, home=team == home, game_id=gid,
                           game_date=day, goals=goals, assists=assists, sog=sog, blocks=int(rng.poisson(1.8 if is_d else 0.7)),
                           pp_points=0, sh_points=0, toi_ev_s=1000.0 if is_d else 800.0, toi_pp_s=50.0 if is_d else 100.0,
                           toi_sh_s=100.0 if is_d else 40.0, toi_s=1150.0 if is_d else 940.0, season=SEASON, game_type=2,
                           regime="regular")
                row["a1"], row["a2"] = float(min(assists, 1)), float(max(assists - 1, 0))
                sk_rows.append(row)
            starter = 0 if (k + ti) % 3 else 1
            for j in range(2):
                row = dict(g_t)
                started = j == starter
                ga = int(rng.poisson(2.7)) if started else 0
                saves = int(rng.poisson(26)) if started else 0
                row.update(nhl_id=5000 + 10 * ti + j, name=f"{team}G{j}", team=team, opponent=opp, home=team == home, game_id=gid,
                           game_date=day, started=started, toi_s=3600.0 if started else 0.0,
                           decision=("W" if team == winner else "L") if started else "none", saves=saves, shots_against=saves + ga,
                           goals_against=ga, shutout=bool(started and ga == 0), season=SEASON, game_type=2, regime="regular")
                gl_rows.append(row)
    root = tmp / "store"
    store.write("skater_games", SEASON, pd.DataFrame(sk_rows), root=root)
    store.write("goalie_games", SEASON, pd.DataFrame(gl_rows), root=root)
    return root


def test_pick_dates_skips_the_first_weeks_and_spreads_the_rest(synthetic_store):
    dates = validate.pick_dates(1, 4, store_root=synthetic_store, min_history_days=10)
    assert len(dates) == 4 and dates == sorted(dates) and dates[0] >= FIRST + timedelta(days=10)
    assert dates[-1] <= FIRST + timedelta(days=N_GAMES - 1)


def test_report_grades_conditional_on_dressing_and_says_what_is_unavailable(synthetic_store, tmp_path):
    cfg = market.load_sim_config()
    dates = [FIRST + timedelta(days=d) for d in (20, 30)]
    rep = validate.report(dates, cfg, store_root=synthetic_store, n_scenarios=400)
    assert rep.as_of_dates == [d.isoformat() for d in dates] and rep.n_scenarios == 400
    c = rep.coverage
    assert c["games"] == 2 and c["skaters_observed"] == 72 and c["skaters_matched"] == 72 and c["goalies_matched"] == 4
    for stat in ("goals", "assists", "points", "sog", "blocks", "saves", "ga", "team_goals"):
        assert rep.pit[stat]["n"] > 0 and len(rep.pit[stat]["hist"]) == cfg["calibrate"]["pit_bins"]
    assert {r["event"] for r in rep.rates} >= {"sog>=5", "blocks>=3", "points>=3", "goals>=3", "saves>=35", "goalie win"}
    assert rep.co_ceiling["pairs"] > 0
    # honest about what is not there
    assert "team totals vs market" in rep.unavailable and "goalie win vs implied" in rep.unavailable
    md = rep.to_markdown()
    assert "CONDITIONAL" in md and "UNAVAILABLE" in md and "MARKET" not in md.replace("vs market", "")
    md_path, js_path = validate.write_report(rep, tmp_path / "cal")
    assert md_path.exists() and md_path.suffix == ".md"
    data = json.loads(js_path.read_text(encoding="utf-8"))
    assert isinstance(data["deficiencies"], list) and data["unavailable"]


def test_report_is_reproducible_and_a_date_with_no_history_is_reported_not_guessed(synthetic_store):
    cfg = market.load_sim_config()
    d = [FIRST + timedelta(days=25)]
    a = validate.report(d, cfg, store_root=synthetic_store, n_scenarios=300)
    b = validate.report(d, cfg, store_root=synthetic_store, n_scenarios=300)
    assert a.pit == b.pit and a.rates == b.rates and a.coverage == b.coverage
    empty = validate.report([FIRST - timedelta(days=5)], cfg, store_root=synthetic_store, n_scenarios=300)
    assert empty.as_of_dates == [] and any("no games" in n for n in empty.notes)
