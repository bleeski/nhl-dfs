"""C25: the calibration grades the simulator with league constants that never saw the graded dates ([BEN] flag 114),
and the co-ceiling cause is tested against actual linemates (flag 115)."""

import argparse
import copy
import hashlib
import json
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from nhl_dfs.data.history import store
from nhl_dfs.models import rates as rates_mod
from nhl_dfs.sim import market, validate
from test_nhl_reports import run as run_reports

pytestmark = pytest.mark.c25

SEASON = 20252026
TEAMS = ("DAL", "WPG")
N_GAMES = 40
FIRST = date(2025, 10, 10)
HELD = [FIRST + timedelta(days=d) for d in (24, 33)]  # the held-out dates these tests grade
NO_LINE_ID = 1111  # WPG's last forward never appears in line_games: the join must count him UNMATCHED
GOLDEN = Path(__file__).parent / "fixtures" / "c25" / "shipped_report_golden.json"
REAL_STORE = Path(__file__).resolve().parents[1] / "data" / "features" / "history"  # conftest hides it otherwise
CONFIGS = (rates_mod.MODEL_YAML, market.SIM_YAML)


def build_store(base: Path, *, extreme_dates=(), seed: int = 7) -> Path:
    """A two-team history store with the real schema: 12 forwards on four lines, 6 defensemen, 2 goalies each, 40 games;
    ties go to overtime (an overtime goal two times in three, else a shootout), some leads end with an empty-net goal,
    a starter is sometimes pulled, and forward lines shuffle on odd games. extreme_dates get 12-goal nights."""
    _, sk_t, gl_t, _ = run_reports(base, moneypuck=False, sub="tmpl")
    f_t = sk_t[sk_t["position"] != "D"].iloc[0].to_dict()
    d_t = sk_t[sk_t["position"] == "D"].iloc[0].to_dict()
    g_t = gl_t.iloc[0].to_dict()
    rng = np.random.default_rng(seed)
    sk_rows, gl_rows, ln_rows = [], [], []
    for k in range(N_GAMES):
        day = FIRST + timedelta(days=k)
        gid = 2025020100 + k
        home = TEAMS[k % 2]
        extreme = day in set(extreme_dates)
        goals = {t: int(rng.poisson(12.0 if extreme else 2.8)) for t in TEAMS}
        if abs(goals[TEAMS[0]] - goals[TEAMS[1]]) == 1 and rng.random() < 0.4:
            goals[min(TEAMS, key=goals.get)] += 1  # a late equalizer with the goalie pulled
        past = goals[TEAMS[0]] == goals[TEAMS[1]]
        winner = TEAMS[int(rng.random() < 0.5)] if past else max(TEAMS, key=goals.get)
        if past and rng.random() < 0.67:
            goals[winner] += 1  # overtime goal
        en = {t: int(t == winner and not past and goals[t] > 0 and rng.random() < 0.3) for t in TEAMS}
        for ti, team in enumerate(TEAMS):
            opp = TEAMS[1 - ti]
            fwd = [1000 + 100 * ti + i for i in range(12)]
            dmen = [1050 + 100 * ti + i for i in range(6)]
            ids = fwd + dmen
            w = np.array([0.30] * 12 + [0.08] * 6)
            g, a1, a2 = np.zeros(18, int), np.zeros(18, int), np.zeros(18, int)
            for _ in range(goals[team]):
                s = int(rng.choice(18, p=w / w.sum()))
                g[s] += 1
                others = [j for j in range(18) if j != s]
                n_a = int(rng.choice(3, p=[0.06, 0.19, 0.75]))
                picks = rng.choice(others, size=n_a, replace=False)
                if n_a >= 1:
                    a1[picks[0]] += 1
                if n_a == 2:
                    a2[picks[1]] += 1
            lines = [fwd[0:3], fwd[3:6], fwd[6:9], fwd[9:12]]
            if k % 2:
                lines[0][2], lines[1][0] = lines[1][0], lines[0][2]
            for j, pid in enumerate(ids):
                is_d = j >= 12
                row = dict(d_t if is_d else f_t)
                sog = int(g[j] + rng.poisson(1.5 if is_d else 2.3))
                toi_ev = (2000.0 if extreme else 1000.0) if is_d else (1600.0 if extreme else 800.0)
                row.update(nhl_id=pid, name=f"{team}{'D' if is_d else 'F'}{j}", position="D" if is_d else ("C" if j < 4 else (
                           "L" if j < 8 else "R")), team=team, opponent=opp, home=team == home, game_id=gid, game_date=day,
                           goals=int(g[j]), assists=int(a1[j] + a2[j]), a1=float(a1[j]), a2=float(a2[j]), sog=sog,
                           blocks=int(rng.poisson(1.8 if is_d else 0.7)), pp_points=0, sh_points=0, toi_ev_s=toi_ev,
                           toi_pp_s=50.0 if is_d else 100.0, toi_sh_s=100.0 if is_d else 40.0,
                           toi_s=toi_ev + (150.0 if is_d else 140.0), ixg=0.1 * sog, tier="A", season=SEASON, game_type=2,
                           regime="regular")
                sk_rows.append(row)
            for u, members in enumerate(lines):
                kept = [m for m in members if m != NO_LINE_ID]
                ln_rows.append({"line_id": "".join(f"{m:07d}" for m in members), "player_ids": "|".join(str(m) for m in kept),
                                "unit": "line", "season": SEASON, "situation": "5on5", "toi_s": 700.0 - 100 * u, "xgf": 0.5,
                                "xga": 0.5, "game_id": gid, "team": team, "game_date": day})
            for u in range(3):
                pair = dmen[2 * u:2 * u + 2]
                ln_rows.append({"line_id": "".join(f"{m:07d}" for m in pair), "player_ids": "|".join(str(m) for m in pair),
                                "unit": "pairing", "season": SEASON, "situation": "5on5", "toi_s": 900.0 - 100 * u, "xgf": 0.5,
                                "xga": 0.5, "game_id": gid, "team": team, "game_date": day})
            starter = 0 if (k + ti) % 3 else 1
            pulled = rng.random() < 0.1
            ga = goals[opp] - en[opp]
            for j in range(2):
                row = dict(g_t)
                started = j == starter
                toi = (2400.0 if pulled else 3600.0) if started else (1200.0 if pulled else 0.0)
                my_ga = ga if started else 0
                saves = int(rng.poisson(26)) if started else (int(rng.poisson(8)) if pulled else 0)
                dec = ("W" if team == winner else ("O" if past else "L")) if started else "none"
                row.update(nhl_id=5000 + 10 * ti + j, name=f"{team}G{j}", team=team, opponent=opp, home=team == home, game_id=gid,
                           game_date=day, started=started, toi_s=toi, decision=dec, saves=saves, shots_against=saves + my_ga,
                           goals_against=my_ga, shutout=bool(started and my_ga == 0), season=SEASON, game_type=2, regime="regular")
                gl_rows.append(row)
    root = base / "store"
    store.write("skater_games", SEASON, pd.DataFrame(sk_rows), root=root)
    store.write("goalie_games", SEASON, pd.DataFrame(gl_rows), root=root)
    store.write("line_games", SEASON, pd.DataFrame(ln_rows), root=root)
    return root


def _frames(root):
    return store.read("skater_games", [SEASON], root=root), store.read("goalie_games", [SEASON], root=root)


def _reproducing(root):
    """The shipped configs with every MEASURED and DERIVED value replaced by this store's own (all dates, printed
    precision), so the reproduction gate passes and the cleaning path itself is exercised."""
    sim, model = market.load_sim_config(), rates_mod.load_model_config()
    cfgs = {"sim": sim, "model": model}
    _, _, t = validate.clean_constants((), store_root=root, sim_cfg=sim, model_cfg=model)
    for r in t["rows"]:
        if r["kind"] == "MEASURED":
            validate._set(cfgs[r["file"]], tuple(r["path"]), round(r["all_dates"], r["decimals"]))
        elif r["kind"] == "DERIVED":
            validate._set(cfgs[r["file"]], tuple(r["path"]), round(r["solved_from_printed_inputs"], r["decimals"]))
    return sim, model


def _same(a, b, path="") -> None:
    """Integers and strings exact, floats to 1e-9 relative, NaN equal to NaN."""
    if isinstance(a, dict):
        assert set(a) == set(b), path
        for k in a:
            _same(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            _same(x, y, f"{path}[{i}]")
    elif isinstance(a, float) or isinstance(b, float):
        if math.isnan(float(a)) or math.isnan(float(b)):
            assert math.isnan(float(a)) and math.isnan(float(b)), path
        else:
            assert float(a) == pytest.approx(float(b), rel=1e-9, abs=1e-12), path
    else:
        assert a == b, path


OLD_CO_CEILING = ("pairs", "observed", "expected", "ratio", "flag", "control_pairs", "control_observed", "control_expected",
                  "control_ratio")


def old_keys(js: dict) -> dict:
    """The pre-C25 report content that must not move (flag 117), without the co-ceiling deficiency's detail text."""
    return {"as_of_dates": js["as_of_dates"], "n_scenarios": js["n_scenarios"], "coverage": js["coverage"], "pit": js["pit"],
            "rates": js["rates"], "team": js["team"], "unavailable": js["unavailable"],
            "co_ceiling": {k: js["co_ceiling"][k] for k in OLD_CO_CEILING},
            "deficiencies": [{k: d[k] for k in ("kind", "what", "value")} for d in js["deficiencies"] if d["kind"] != "constants"],
            "notes": js["notes"]}


@pytest.fixture(scope="module")
def store_root(tmp_path_factory):
    return build_store(tmp_path_factory.mktemp("c25"))


@pytest.fixture(scope="module")
def extreme_root(tmp_path_factory):
    return build_store(tmp_path_factory.mktemp("c25x"), extreme_dates=HELD)


@pytest.fixture(scope="module")
def clean_report(store_root):
    sim, model = _reproducing(store_root)
    sim_c, model_c, table = validate.clean_constants(HELD, store_root=store_root, sim_cfg=sim, model_cfg=model)
    rep = validate.report(HELD, sim_c, model_cfg=model_c, constants=table, store_root=store_root, n_scenarios=300)
    return rep, table, (sim_c, model_c)


# -- the constants used exclude the held-out dates ----------------------------------------------------------------------

def test_clean_constants_are_the_values_of_the_other_dates_alone(extreme_root):
    sk, gl = _frames(extreme_root)
    allv = validate.league_constants(sk, gl)
    dropped = validate.league_constants(sk[~sk["game_date"].isin(HELD)], gl[~gl["game_date"].isin(HELD)])
    clean = validate.league_constants(sk, gl, exclude_dates=HELD)
    names = [validate.constant_name(f, p) for f, p, _ in validate.MEASURED]
    for nm in names + ["in.reg_goals", "in.tie_rate", "in.en_goals", "pace.target_cv"]:
        assert clean[nm] == pytest.approx(dropped[nm], nan_ok=True), nm
    # the two 12-goal nights are out of the clean values and in the all-dates ones
    assert clean["sim.resolve.goals_league"] < allv["sim.resolve.goals_league"] - 0.2
    assert clean["model.skaters.F.toi_s.ev"] == pytest.approx(800.0) and allv["model.skaters.F.toi_s.ev"] > 800.0
    _, _, table = validate.clean_constants(HELD, store_root=extreme_root)
    by = {r["name"]: r for r in table["rows"]}
    for nm in names:
        assert by[nm]["clean"] == pytest.approx(clean[nm], nan_ok=True) and by[nm]["all_dates"] == pytest.approx(allv[nm], nan_ok=True)
    assert table["excluded_dates"] == [d.isoformat() for d in HELD] and table["games_all"] - table["games_used"] == len(HELD)
    # the derived values are re-solved from the clean inputs, not carried over
    d_all = validate.derive_resolve(allv["in.reg_goals"], allv["in.tie_rate"], allv["in.en_goals"], market.load_sim_config())
    d_cln = validate.derive_resolve(clean["in.reg_goals"], clean["in.tie_rate"], clean["in.en_goals"], market.load_sim_config())
    assert by["sim.resolve.lambda0_league"]["clean"] == pytest.approx(d_cln["lambda0_league"])
    assert d_cln["lambda0_league"] < d_all["lambda0_league"]


def test_only_reproduced_values_are_replaced_and_the_inputs_are_not_mutated(store_root):
    sim, model = _reproducing(store_root)
    before = copy.deepcopy((sim, model))
    sim_c, model_c, table = validate.clean_constants(HELD, store_root=store_root, sim_cfg=sim, model_cfg=model)
    assert (sim, model) == before
    cfgs = {"sim": sim_c, "model": model_c}
    given = {"sim": sim, "model": model}
    assert {n["name"] for n in table["not_cleaned"]} == {"sim.pace.var"}
    for r in table["rows"]:
        if r["kind"] in ("MEASURED", "DERIVED"):
            assert r["status"] == "CLEANED", r["name"]
            assert validate._get(cfgs[r["file"]], tuple(r["path"])) == pytest.approx(r["clean"])
    assert validate._get(sim_c, ("pace", "var")) == validate._get(given["sim"], ("pace", "var"))
    # against the real shipped files almost nothing on this synthetic store reproduces: a value is replaced if and only if
    # its group reproduced, everything else keeps the shipped value
    s2, m2, t2 = validate.clean_constants(HELD, store_root=store_root)
    shipped = {"sim": market.load_sim_config(), "model": rates_mod.load_model_config()}
    got = {"sim": s2, "model": m2}
    assert len(t2["cleaned"]) < 5
    for r in t2["rows"]:
        if r["kind"] in ("MEASURED", "DERIVED"):
            want = r["clean"] if r["status"] == "CLEANED" else validate._get(shipped[r["file"]], tuple(r["path"]))
            assert validate._get(got[r["file"]], tuple(r["path"])) == pytest.approx(want), r["name"]
            assert (r["status"] == "CLEANED") == all(x["reproduced"] for x in t2["rows"]
                                                     if x["kind"] == r["kind"] and (r["kind"] == "DERIVED" or
                                                     x["name"].split("[")[0] == r["name"].split("[")[0])), r["name"]


def test_report_refuses_constants_that_saw_a_graded_date(store_root):
    _, _, table = validate.clean_constants(HELD[:1], store_root=store_root)
    with pytest.raises(ValueError, match="does not exclude"):
        validate.report(HELD, constants=table, store_root=store_root, n_scenarios=50)


# -- reproduction of the shipped values ------------------------------------------------------------------------------

def test_the_solver_reproduces_the_shipped_resolve_values_from_the_printed_inputs():
    cfg = market.load_sim_config()
    d = validate.derive_resolve(2.970, 0.2275, 0.196, cfg)
    r = cfg["resolve"]
    assert d["lambda0_league"] == pytest.approx(r["lambda0_league"], abs=5e-5)
    assert d["q_eq"] == pytest.approx(r["q_eq"], abs=5e-5)
    assert d["q_en"] == pytest.approx(r["q_en"], abs=5e-5)


@pytest.mark.skipif(not (REAL_STORE / "skater_games" / "20252026.parquet").exists(),
                    reason="LOUD SKIP: the real history store (data/features/history, gitignored) is not on this machine")
def test_on_the_real_store_every_measured_value_reproduces_except_the_three_named():
    _, _, t = validate.clean_constants((), store_root=REAL_STORE)
    assert t["seasons"] == list(validate.CONSTANT_SEASONS) and t["games_all"] == 2624
    named = {"model.goalies.sv_pct", "sim.skater.assists_per_goal", "sim.pace.var"}
    assert {n["name"] for n in t["not_cleaned"]} == named
    for r in t["rows"]:
        if r["kind"] in ("MEASURED", "DERIVED", "INPUT", "TARGET") and r["name"].split("[")[0] not in named:
            assert r["reproduced"], r["name"]


# -- the shipped files are untouched and the shipped path is origin/master's ------------------------------------------

def _sha(paths):
    return {str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}


def test_calibrate_with_clean_constants_leaves_the_config_files_byte_identical(store_root, tmp_path):
    before = _sha(CONFIGS)
    args = argparse.Namespace(seasons=1, dates=2, scenarios=200, store_root=str(store_root), out_dir=str(tmp_path / "cal"),
                              clean_constants=True)
    assert validate.run_cli(args) == 0
    assert _sha(CONFIGS) == before
    js = json.loads(next((tmp_path / "cal").glob("*.json")).read_text(encoding="utf-8"))
    assert set(js["constants"]["excluded_dates"]) >= set(js["as_of_dates"]) and js["constants"]["not_cleaned"]


def test_report_with_the_shipped_files_matches_origin_master(store_root):
    """GOLDEN was written by validate.report(HELD, None, store_root=build_store(...), n_scenarios=300) on origin/master
    2091aad (before C25)."""
    rep = validate.report(HELD, store_root=store_root, n_scenarios=300)
    js = json.loads(rep.to_json())
    assert js["constants"] == {} and not any(d["kind"] == "constants" for d in js["deficiencies"])
    _same(old_keys(js), json.loads(GOLDEN.read_text(encoding="utf-8")))


# -- the report: machine-readable, named, deterministic, and the clean values reach the simulation --------------------

def test_deficiencies_are_machine_readable_and_the_md_lists_the_same(clean_report):
    rep, table, _ = clean_report
    js = json.loads(rep.to_json())
    assert js["deficiencies"]
    for d in js["deficiencies"]:
        assert {"kind", "what", "detail", "value"} <= set(d)
    md = rep.to_markdown()
    section = md.split("## Deficiencies")[1].split("\n## ")[0]
    assert [x for x in section.splitlines() if x.startswith("- ")] == [f"- {d['kind']} {d['what']}: {d['detail']}"
                                                                       for d in js["deficiencies"]]
    assert js["constants"]["cleaned"] and [n["name"] for n in js["constants"]["not_cleaned"]] == ["sim.pace.var"]
    assert any(d["kind"] == "constants" and "sim.pace.var" in d["detail"] for d in js["deficiencies"])
    assert "## League constants" in md and "sim.pace.var" in md and "NOT_CLEANED" in md and "CLEANED" in md
    assert any("recomputed without the 2 held-out dates" in n for n in js["notes"])


def test_the_same_seed_gives_the_same_report(clean_report, store_root):
    rep, table, (sim_c, model_c) = clean_report
    again = validate.report(HELD, sim_c, model_cfg=model_c, constants=table, store_root=store_root, n_scenarios=300)
    a, b = json.loads(rep.to_json()), json.loads(again.to_json())
    for x in (a, b):
        x.pop("created_utc"), x.pop("wall_s")
    _same(a, b)


def test_clean_configs_reach_the_simulation_without_reading_the_files(clean_report, store_root, monkeypatch):
    _, table, (sim_c, model_c) = clean_report
    import nhl_dfs.models.overrides as overrides
    import nhl_dfs.models.params as params
    import nhl_dfs.models.roles as roles
    import nhl_dfs.sim.game as game

    def boom(*a, **k):
        raise AssertionError("a shipped config file was read")

    for mod, name in ((rates_mod, "load_model_config"), (market, "load_sim_config"), (game, "load_model_config"),
                      (game, "load_sim_config"), (params, "load_model_config"), (roles, "load_model_config"),
                      (overrides, "load_model_config"), (validate, "load_model_config"), (validate, "load_sim_config")):
        monkeypatch.setattr(mod, name, boom)
    rep = validate.report(HELD[:1], sim_c, model_cfg=model_c, constants=table, store_root=store_root, n_scenarios=200)
    assert rep.as_of_dates == [HELD[0].isoformat()] and rep.coverage["skaters_matched"] > 0


def test_a_changed_league_value_moves_the_simulation(store_root):
    sim, model = market.load_sim_config(), rates_mod.load_model_config()
    hi = copy.deepcopy(sim)
    hi["resolve"]["lambda0_league"] *= 1.5
    rich = copy.deepcopy(model)  # league scoring rates are the shrinkage prior behind every skater's own rates
    for g in ("F", "D"):
        rich["skaters"][g]["pts60"] = {s: 3 * v for s, v in rich["skaters"][g]["pts60"].items()}
    base = validate.report(HELD[:1], sim, model_cfg=model, store_root=store_root, n_scenarios=200).team["sim_mean"]
    assert validate.report(HELD[:1], hi, model_cfg=model, store_root=store_root, n_scenarios=200).team["sim_mean"] > 1.2 * base
    assert validate.report(HELD[:1], sim, model_cfg=rich, store_root=store_root, n_scenarios=200).team["sim_mean"] > 1.2 * base
