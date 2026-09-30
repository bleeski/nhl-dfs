"""Backlog B25, B26, B28: the frozen scenario cache keeps per-draw indicators (dressed, started, one per DK bonus)
and each person's play probability; settle grades them, and older caches settle exactly as before (labeled)."""
import dataclasses
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from nhl_dfs.build import scenario_cache as sc
from nhl_dfs.contracts.geometry import Mode
from nhl_dfs.sim import outcomes as oc
from nhl_dfs.sim import score
from pool_builder import make_pool, row

FIRST = datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc)
SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}


# -- the cache format (C8) ------------------------------------------------------------------------------------------

@pytest.mark.c8
def test_indicators_round_trip_bit_for_bit_with_the_chunks(tmp_path):
    rng = np.random.default_rng(3)
    flags = rng.random((4500, 7, len(score.FLAG_NAMES))) < 0.3
    base = rng.integers(0, 400, (4500, 7)).astype(np.int32)
    kept = sc.save_base(tmp_path, "selection", base, keep=4500, chunk_size=2000, flags=flags)
    assert kept == {"n": 4000, "chunks": 2, "flags": True}  # the same whole-chunk truncation as the points
    small = sc.save_base(tmp_path, "referee", base, keep=1500, chunk_size=2000, flags=flags)
    assert small == {"n": 1500, "chunks": 1, "flags": True}
    meta = {"flags": {"names": list(score.FLAG_NAMES)}, "chunk_size": 2000, "person_keys": list("abcdefg"),
            "purposes": {"selection": kept, "referee": small}}
    root = tmp_path / "scenario"
    assert np.array_equal(sc.read_flags(root, meta, "selection", 4000), flags[:4000])
    assert np.array_equal(sc.read_flags(root, meta, "selection", 2000, start=1500), flags[1500:3500])
    assert np.array_equal(sc.read_flags(root, meta, "referee", 1500), flags[:1500])
    packed = np.load(root / "selection" / "flags_0000.npy")
    assert packed.dtype == np.uint8 and packed.shape == (len(score.FLAG_NAMES), 7, 250)  # 2,000 draws in 250 bytes
    notes = []
    assert sc.read_flags(root, meta, "selection", 4001, notes=notes) is None and "fewer" in notes[0]
    # a later write without indicators leaves none behind, and an older meta reads as "no indicators"
    assert sc.save_base(tmp_path, "selection", base, keep=4000, chunk_size=2000) == {"n": 4000, "chunks": 2}
    assert not list((root / "selection").glob("flags_*.npy"))
    assert sc.read_flags(root, {"chunk_size": 2000}, "selection", 10) is None


@pytest.mark.c8
def test_bonus_indicators_are_the_bonuses_base_tenths_pays():
    from nhl_dfs.contracts import scoring as s

    rng = np.random.default_rng(7)
    n, p = 3000, 6
    is_g = np.array([False] * 4 + [True] * 2)
    o = oc.empty([f"k{i}" for i in range(p)], is_g, n)
    o.dressed[:] = rng.random((n, p)) < 0.9
    for name, hi in (("goals", 5), ("assists", 5), ("sog", 9), ("blocks", 6), ("sh_pts", 2), ("saves", 45), ("ga", 6)):
        getattr(o, name)[:] = rng.integers(0, hi, (n, p)) * o.dressed
    o.shutout[:] = (o.ga == 0) & o.dressed
    o.started[:, 4:] = o.dressed[:, 4:] & (rng.random((n, 2)) < 0.8)
    f = score.bonus_flags(o)
    ix = {k: i for i, k in enumerate(score.FLAG_NAMES)}
    bonus = (f[..., ix["hat_trick"]] * s.HAT_TRICK_BONUS_TENTHS + f[..., ix["sog_5"]] * s.FIVE_PLUS_SOG_BONUS_TENTHS
             + f[..., ix["blocks_3"]] * s.THREE_PLUS_BLOCKS_BONUS_TENTHS + f[..., ix["points_3"]] * s.THREE_PLUS_POINTS_BONUS_TENTHS
             + f[..., ix["shutout"]] * s.GOALIE_SHUTOUT_TENTHS + f[..., ix["saves_35"]] * s.GOALIE_35_PLUS_SAVES_TENTHS)
    plain = (o.goals * s.GOAL_TENTHS + o.assists * s.ASSIST_TENTHS + o.sog * s.SOG_TENTHS + o.blocks * s.BLOCK_TENTHS
             + o.sh_pts * s.SH_POINT_BONUS_TENTHS + o.saves * s.GOALIE_SAVE_TENTHS + o.ga * s.GOALIE_GOAL_AGAINST_TENTHS)
    assert np.array_equal(score.base_tenths(o), (plain + bonus).astype(np.int32))  # decisions are all ND here
    assert np.array_equal(f[:, :4, ix["started"]], o.dressed[:, :4])  # a skater "starts" when he dresses
    assert np.array_equal(f[:, 4:, ix["started"]], o.started[:, 4:])  # a goalie: started in net, not relief


@pytest.fixture(scope="module")
def mini_run(tmp_path_factory):
    from conftest import mini_pair
    from nhl_dfs.build.run import run_slate

    tmp = tmp_path_factory.mktemp("frozen_mini")
    r = run_slate(*mini_pair("classic"), offline=True, baseline_only=False, scenario=True, scenario_n=SMALL,
                  out_root=tmp / "runs", outputs_root=tmp / "outputs",
                  clock=lambda: datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))
    assert r.ok, r.manifest["failed"]
    return tmp, r


@pytest.mark.c8
def test_the_scenario_pass_freezes_indicators_and_play_probabilities(mini_run):
    tmp, r = mini_run
    got = sc.load(r.run.path, r.run.run_id)
    assert got.flag_names == list(score.FLAG_NAMES)
    ix = {k: i for i, k in enumerate(score.FLAG_NAMES)}
    n = got.n("selection")
    f, pts = got.flags("selection", n), got.base("selection", n)
    assert f.shape == (n, len(got.person_keys), len(ix)) and got.flags("referee", got.n("referee")) is not None
    part = got.meta["participation"]
    goalies = [j for j, k in enumerate(got.person_keys) if part["persons"][k]["group"] == "G"]
    skaters = [j for j in range(len(got.person_keys)) if j not in goalies]
    from nhl_dfs.contracts.scoring import SHOOTOUT_GOAL_TENTHS

    bad = (pts != 0) & ~f[..., ix["dressed"]]  # only shootout goals (the C6 defect below) score undressed
    assert not (pts[bad] % SHOOTOUT_GOAL_TENTHS).any() and bad.mean() < 0.001
    assert np.array_equal(f[:, skaters, ix["started"]], f[:, skaters, ix["dressed"]])
    assert not (f[:, goalies, ix["started"]] & ~f[:, goalies, ix["dressed"]]).any()  # started implies in net
    for j, k in enumerate(got.person_keys):  # the saved probability is what the draws show, within Monte Carlo error
        p = part["persons"][k]["p_sim"]
        share = f[:, j, ix["started"]].mean()
        assert abs(share - p) <= 4 * np.sqrt(p * (1 - p) / n) + 1.5 / n, (k, p, share)
        assert part["persons"][k]["p_play"] == pytest.approx(p * part["persons"][k]["mask"], abs=1e-5)
    assert set(part["goalies"]) == {got.person_keys[j] for j in goalies}
    assert all(v["source"] for v in part["goalies"].values())


@pytest.mark.c8
def test_a_goalie_source_names_override_daily_faceoff_or_the_rotation():
    from nhl_dfs.contracts.statuses import GoalieState

    def roles(state, *, report=None, named=None, notes=()):
        gr = SimpleNamespace(state=state, report=report, named=named, confirmed=None, notes=list(notes))
        return SimpleNamespace(goalies={"AAA": gr})

    assert sc.goalie_source(None, "AAA", "g")[0] == "rotation (no role state)"
    assert sc.goalie_source(roles(GoalieState.EXPECTED), "AAA", "g")[0] == "rotation"
    assert sc.goalie_source(roles(GoalieState.CONFIRMED, report=object(), named="g"), "AAA", "g")[0] == "Daily Faceoff CONFIRMED"
    assert sc.goalie_source(roles(GoalieState.EXPECTED, named="g", notes=["team page depth order (first listed goalie)"]),
                            "AAA", "g")[0] == "Daily Faceoff team page EXPECTED"
    assert sc.goalie_source(roles(GoalieState.CONFIRMED, notes=["override: g confirmed starter"]), "AAA", "g")[0] == "override"


# -- settle (C11) ----------------------------------------------------------------------------------------------------

def _pool(n_skaters):
    from nhl_dfs.intake.salary import GameInfo

    rows = [dataclasses.replace(row(i, "VGK", "C", 4000), name=f"Skater {i}", person_key=f"skater {i}|VGK|F")
            for i in range(1, n_skaters + 1)]
    rows += [dataclasses.replace(row(900 + i, "VGK", "G", 8000), name=f"Goalie {c}", person_key=f"goalie {c.lower()}|VGK|G")
             for i, c in enumerate("AB")]
    pool = make_pool(Mode.CLASSIC, rows)
    return rows, dataclasses.replace(pool, games={"CHI@VGK": GameInfo("VGK", "CHI", "", FIRST)},
                                     teams=frozenset({"VGK", "CHI"}))


def _run(tmp_path, keys, draws, flags=None, participation=None):
    (tmp_path / "manifest.json").write_text(json.dumps({"mode": "classic"}), encoding="utf-8")
    kept = sc.save_base(tmp_path, "selection", draws, keep=len(draws), chunk_size=2000, flags=flags)
    meta = {"person_keys": keys, "chunk_size": 2000, "purposes": {"selection": kept}}
    if flags is not None:
        meta["flags"] = {"names": list(score.FLAG_NAMES)}
    if participation is not None:
        meta["participation"] = participation
    (tmp_path / "scenario" / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return SimpleNamespace(path=tmp_path, inputs=tmp_path / "inputs", run_id="fake")


def _box(skaters, goalies):
    from nhl_dfs.data.sources.nhl import BoxGoalie, BoxScore, BoxSkater

    return BoxScore(2026020005, "OFF", "VGK", "CHI", 3, 2,
                    [BoxSkater(nid, "VGK", "C", g, a, s, b, 0, 1000) for nid, g, a, s, b in skaters],
                    [BoxGoalie(nid, "VGK", dec, sv, sv + ga, ga, toi) for nid, dec, sv, ga, toi in goalies])


def _acc(rows):
    from nhl_dfs.data.identity.crosswalk import key_sha

    return {key_sha(r.name, r.team, "G" if r.is_goalie else "F"): int(r.role_id) for r in rows}


@pytest.mark.c11
def test_an_old_cache_settles_exactly_as_before_and_says_so(tmp_path):
    from nhl_dfs.learn import grade_forecasts as gf

    rows, pool = _pool(2)
    keys = [r.person_key for r in rows]
    n = 1000
    draws = np.stack([np.where(np.arange(n) < 500, 100, 0), np.full(n, 20), np.where(np.arange(n) < 700, 60, 0),
                      np.where(np.arange(n) < 300, 80, 0)], axis=1).astype(np.int32)
    run = _run(tmp_path, keys, draws)
    box = _box([(1, 1, 0, 3, 0)], [(901, "W", 30, 2, 3600)])
    g = gf.grade(run, [box], actual_points={keys[0]: 100, keys[3]: 100}, pool=pool, accepted=_acc(rows))
    assert g.by_group["F"]["mae"] == pytest.approx(5.0)  # mean 5.0 vs 10: unconditional on dressing, as before
    assert g.skater_conditioning.startswith("UNCONDITIONAL") and g.indicators.startswith("NONE")
    assert g.bonus_rate_calibration.startswith("NOT_AVAILABLE")
    assert g.goalie_decisions["status"].startswith("implied") and g.goalie_decisions["brier"] == pytest.approx(0.49)
    assert g.start_probability_check["status"].startswith("NOT_AVAILABLE")
    assert g.play_probability["status"].startswith("IMPLIED")


@pytest.mark.c11
def test_a_new_cache_grades_dressed_draws_saved_starts_and_reproduces_a_planted_bonus_rate(tmp_path):
    from nhl_dfs.learn import grade_forecasts as gf

    ns = 40
    rows, pool = _pool(ns)
    keys = [r.person_key for r in rows]
    n, P = 1000, ns + 2
    ix = {k: i for i, k in enumerate(score.FLAG_NAMES)}
    draws = np.zeros((n, P), np.int32)
    flags = np.zeros((n, P, len(ix)), bool)
    half = np.arange(n) < 500
    for j in range(ns):  # dressed in half the draws; 30% of the dressed draws have 5+ shots
        flags[:, j, ix["dressed"]] = flags[:, j, ix["started"]] = half
        flags[:, j, ix["sog_5"]] = np.arange(n) < 150
        draws[:, j] = np.where(half, 100, 0)
    ga, gb = ns, ns + 1
    flags[:, ga, ix["dressed"]] = flags[:, ga, ix["started"]] = np.arange(n) < 700
    flags[:, gb, ix["dressed"]] = np.arange(n) >= 300  # B starts in 30% and relieves in 10%
    flags[:, gb, ix["started"]] = np.arange(n) >= 700
    flags[:, gb, ix["saves_35"]] = np.arange(n) >= 900  # 35+ saves in a third of his started draws
    draws[:, ga] = np.where(np.arange(n) < 700, 60, 0)
    draws[:, gb] = np.where(np.arange(n) >= 700, 80, np.where(np.arange(n) >= 300, 10, 0))
    part = {"format": 1, "persons": {k: {"group": "G" if "goalie" in k else "F", "p_sim": 0.5, "mask": 1.0, "p_play": 0.5}
                                     for k in keys},
            "goalies": {keys[ga]: {"team": "VGK", "p_start": 0.7, "source": "Daily Faceoff EXPECTED"},
                        keys[gb]: {"team": "VGK", "p_start": 0.3, "source": "rotation"}}}
    part["persons"][keys[ga]]["p_play"] = 0.7
    part["persons"][keys[gb]]["p_play"] = 0.3
    run = _run(tmp_path, keys, draws, flags, part)
    sk = [(int(rows[j].role_id), 0, 0, 6 if j < 12 else 2, 0) for j in range(ns)]  # 12 of 40 had 5+ shots: 30%
    box = _box(sk, [(901, "W", 36, 2, 3600)])  # Goalie B (id 901) started alone: 36 saves, no shutout
    g = gf.grade(run, [box], actual_points={keys[0]: 100, keys[gb]: 80}, pool=pool, accepted=_acc(rows))
    assert g.skater_conditioning.startswith("CONDITIONAL") and g.indicators.startswith("per-draw indicators")
    assert g.by_group["F"]["mae"] == pytest.approx(0.0)  # graded on his dressed draws: 10.0 every time he dressed
    assert g.by_group["G"]["mae"] == pytest.approx(0.0)  # a starter on his started draws, not his relief draws
    b = g.bonus_rate_calibration
    assert b["sog_5"]["n"] == ns and b["sog_5"]["pred_rate"] == pytest.approx(0.3) and b["sog_5"]["obs_rate"] == pytest.approx(0.3)
    assert b["sog_5"]["expected"] == pytest.approx(12.0) and b["sog_5"]["observed"] == 12 and b["sog_5"]["z"] == 0.0
    assert b["saves_35"]["n"] == 1 and b["saves_35"]["pred_rate"] == pytest.approx(1 / 3, abs=1e-3) and b["saves_35"]["observed"] == 1
    assert b["shutout"]["observed"] == 0 and b["sh_point"]["status"].startswith("NOT_GRADED")
    d = g.goalie_decisions
    assert d["status"].startswith("saved at build") and d["accuracy"] == 0.0
    assert d["brier"] == pytest.approx((0.7 ** 2 + 0.7 ** 2) / 2) and d["sources"] == {"Daily Faceoff EXPECTED": 1, "rotation": 1}
    c = g.start_probability_check
    assert c["goalies"] == 2 and c["within_3_se"] and c["max_abs_z"] == pytest.approx(0.0)
    assert g.play_probability["status"].startswith("saved") and g.play_probability["F"]["n"] == ns
    assert g.play_probability["F"]["brier"] == pytest.approx(0.25)


@pytest.mark.c8
@pytest.mark.xfail(strict=True, reason="backlog: C6 shootout shooters can include undressed skaters (sim/game.py "
                   "_shootout Gumbel top-k falls through to -inf columns when a round needs more shooters than the "
                   "team has dressed forwards with weight); fixing it changes the draws, so it waits for a sim chunk")
def test_no_undressed_skater_scores_a_shootout_goal(mini_run):
    tmp, r = mini_run
    got = sc.load(r.run.path, r.run.run_id)
    n = got.n("selection")
    f, pts = got.flags("selection", n), got.base("selection", n)
    assert not ((pts != 0) & ~f[..., 0]).any()


@pytest.mark.c9
def test_refresh_carries_the_indicators_and_play_probabilities_into_its_child_cache(mini_run, tmp_path):
    from nhl_dfs.build import refresh
    from nhl_dfs.intake.salary import read_salary
    from test_late_swap_objective import _fresh_with_out, _victim, _v3
    from nhl_dfs.intake.entries import read_entries

    tmp, r = mini_run
    eid = read_entries(_v3(r)).entries[0].entry_id
    victim = _victim(r, eid)
    fresh = _fresh_with_out(r.run.inputs / "DKSalaries.csv", tmp_path / "fresh.csv", [victim])
    f = refresh.run(r.run.run_id, offline=True, runs_root=tmp / "runs", outputs_root=tmp / "outputs", salary_path=fresh,
                    as_of=datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc))
    assert f.ok and f.manifest["objective"]["scenario"]["flags"] == "carried to the child cache"
    parent, child = sc.load(r.run.path, r.run.run_id), sc.load(f.run.path, f.run.run_id)
    n = child.n("selection")
    pf, cf = parent.flags("selection", n), child.flags("selection", n)
    assert cf is not None and child.meta["participation"]["goalies"]
    pool = read_salary(fresh)
    vk = pool.by_role_id[victim].person_key
    resim = set(f.manifest["objective"]["scenario"]["games_resimulated"])
    teams = {t for g in resim for t in g.split("@")}
    for j, k in enumerate(child.person_keys):
        if k not in parent.person_keys:  # excluded when the parent ran: no frozen draws unless his game re-simulated
            assert k.split("|")[1] in teams or not cf[:, j].any()
            continue
        pj = parent.person_keys.index(k)
        if k == vk:
            assert not cf[:, j].any()  # ruled OUT since the draws were made
        elif k.split("|")[1] not in teams:
            assert np.array_equal(cf[:, j], pf[:, pj])  # untouched games keep the frozen indicators
    cb = child.base("selection", n)
    assert not ((cb != 0) & ~cf[..., 0] & (cb % 15 != 0)).any()  # re-simulated columns: points only when dressed
