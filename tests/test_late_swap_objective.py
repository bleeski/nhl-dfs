"""C9: objective-aware late swap and refresh (scenario, then provisional, then baseline)."""

import copy

import numpy as np
import pytest

from nhl_dfs.sim import game, score
from sim_helpers import slate_for, synthetic_params

pytestmark = pytest.mark.c9

TEAMS = ("AAA", "BBB", "CCC", "DDD")
GAMES = (("AAA", "BBB"), ("CCC", "DDD"))


def _base(slate, params, n, games=None, seed=11, purpose="selection"):
    return np.concatenate([score.base_tenths(o) for o in game.iter_chunks(slate, params, n, seed, purpose, games)])


def _cols(params, teams):
    keys = sorted(params.persons)
    return [i for i, k in enumerate(keys) if params.persons[k].team in teams]


# -- per-game independence: the premise of re-simulating only changed games --------------------------

def test_a_filtered_game_draws_exactly_what_the_full_slate_draws():
    params = synthetic_params(TEAMS)
    slate = slate_for(params, GAMES)
    full = _base(slate, params, 2500)  # two chunks
    only = _base(slate, params, 2500, games={"DDD@CCC"})
    c = _cols(params, {"CCC", "DDD"})
    other = _cols(params, {"AAA", "BBB"})
    assert np.array_equal(full[:, c], only[:, c])
    assert not only[:, other].any()


def test_changing_one_team_leaves_the_other_games_columns_identical():
    params = synthetic_params(TEAMS)
    changed = copy.deepcopy(params)
    k = next(k for k, p in sorted(changed.persons.items()) if p.team == "AAA" and p.group == "F")
    changed.persons[k].opportunity.toi_ev_s *= 1.4
    a = _base(slate_for(params, GAMES), params, 2000)
    b = _base(slate_for(changed, GAMES), changed, 2000)
    c = _cols(params, {"CCC", "DDD"})
    assert np.array_equal(a[:, c], b[:, c])
    assert not np.array_equal(a[:, _cols(params, {"AAA", "BBB"})], b[:, _cols(params, {"AAA", "BBB"})])


# -- the persisted scenario cache ------------------------------------------------------------------

def test_cache_round_trip_keeps_whole_chunks_and_raw_scores(tmp_path):
    from nhl_dfs.build import scenario_cache as sc

    base = np.arange(5 * 3, dtype=np.int32).reshape(5, 3)
    meta = sc.save_base(tmp_path, "selection", base, keep=5, chunk_size=2)
    assert meta == {"n": 4, "chunks": 2}  # whole chunks only, so a reload never splits a chunk's draws
    meta1 = sc.save_base(tmp_path, "referee", base, keep=1, chunk_size=2)
    assert meta1 == {"n": 1, "chunks": 1}
    params = synthetic_params(("AAA", "BBB"))
    slate = slate_for(params)
    from nhl_dfs.contracts.statuses import PayoutSource
    from nhl_dfs.build import objectives as ob

    ct = ob.Contest("9", "x", "cash", 10, 100, np.array([180] * 5, np.int64), np.zeros(5, bool), None, PayoutSource.PRIOR, "d")
    sc.save(tmp_path, purposes={"selection": meta, "referee": meta1}, person_keys=["a", "b", "c"], slate=slate,
            params=params, seed=5, chunk_size=2, contests={"9": ct}, contest_family={"9": "cash"},
            fields={"cash": ([("r1", "r2")], ["k"])}, n_opponents={"9": 9}, own_by={"9": {"r1": 50.0}},
            dup_by={"9": {"k": 1.0}}, field_cal="PRIOR", model_status="PRIOR")
    got = sc.load(tmp_path, "run")
    assert np.array_equal(got.base("selection", 3), base[:3]) and got.n("referee") == 1
    assert got.contests["9"].prizes_cents.tolist() == [180] * 5 and got.contests["9"].payout_source is PayoutSource.PRIOR
    assert got.families["cash"] == ([("r1", "r2")], ["k"]) and got.n_opponents == {"9": 9}
    assert set(got.meta["game_sha256"]) == {"BBB@AAA"}
    with pytest.raises(ValueError):
        got.base("selection", 5)


def test_find_walks_the_parent_chain_and_reports_each_run(tmp_path):
    import json

    from nhl_dfs.build import scenario_cache as sc

    (tmp_path / "child").mkdir()
    (tmp_path / "child" / "manifest.json").write_text(json.dumps({"parent_run_id": "parent"}))
    (tmp_path / "parent" / "scenario").mkdir(parents=True)
    (tmp_path / "parent" / "scenario" / "meta.json").write_text("{not json")
    (tmp_path / "parent" / "manifest.json").write_text(json.dumps({}))
    got, notes = sc.find(tmp_path, "child")
    assert got is None
    assert notes[0] == "run child: no scenario cache" and "unreadable" in notes[1]


@pytest.fixture(scope="module")
def full_classic(tmp_path_factory):
    from datetime import datetime, timezone

    from conftest import mini_pair
    from nhl_dfs.build.run import run_slate

    tmp = tmp_path_factory.mktemp("c9_full")
    r = run_slate(*mini_pair("classic"), offline=True, baseline_only=False, scenario=True, scenario_n=SMALL,
                  out_root=tmp / "runs", outputs_root=tmp / "outputs",
                  clock=lambda: datetime(2026, 9, 28, 18, 0, tzinfo=timezone.utc))
    assert r.ok, r.manifest["failed"]
    return tmp, r


SMALL = {"design": 300, "selection": 800, "referee": 800, "field_target": 400}


def test_the_scenario_pass_writes_a_readable_cache(full_classic):
    from nhl_dfs.build import scenario_cache as sc

    tmp, r = full_classic
    got, notes = sc.find(tmp / "runs", r.run.run_id)
    assert got is not None, notes
    assert got.n("selection") == 800 and got.n("referee") == 800
    assert set(got.contests) == {str(e["contest_id"]) for e in r.manifest["scenario"]["entries"]}
    assert got.meta["model_status"] == r.manifest["scenario"]["evidence"]["MODEL_STATUS"]
    assert set(got.meta["game_sha256"]) == set(r.manifest["scenario"]["game_sources"])
