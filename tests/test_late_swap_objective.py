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


# -- late swap on the scenario objective (mini Classic: NYI@CAR 23:00Z, VAN@EDM 02:00Z) ------------------

from datetime import datetime, timezone  # noqa: E402

from nhl_dfs.build import late_swap, refresh  # noqa: E402
from nhl_dfs.intake.entries import cell_role_id, read_entries  # noqa: E402
from nhl_dfs.intake.salary import read_salary  # noqa: E402
from nhl_dfs.referee.check_file import check_file  # noqa: E402

BEFORE = datetime(2026, 9, 29, 18, 0, tzinfo=timezone.utc)
MID = datetime(2026, 9, 29, 23, 30, tzinfo=timezone.utc)  # NYI@CAR started, VAN@EDM open


def _v3(r):
    return r.run.version_file(r.manifest["scenario"]["version"])


def _roster(path, eid):
    e = next(x for x in read_entries(path).entries if x.entry_id == eid)
    return [cell_role_id(c) for c in e.cells]


def _fresh_with_out(src, dst, role_ids):
    """A re-downloaded salary file of the same slate with these rows' Status set to OUT."""
    out = []
    for line in src.read_bytes().split(b"\n"):
        body = line.rstrip(b"\r")
        if any(f"({rid})".encode() in body for rid in role_ids) and body.endswith(b",,"):
            line = body[:-2] + b",OUT," + line[len(body):]
        out.append(line)
    dst.write_bytes(b"\n".join(out))
    return dst


def _victim(r, eid, teams=("VAN", "EDM")):
    pool = read_salary(r.run.inputs / "DKSalaries.csv")
    return next(rid for rid in _roster(_v3(r), eid) if not pool.by_role_id[rid].is_goalie and pool.by_role_id[rid].team in teams)


def _swap(tmp, run_id, entries, *, as_of, salary=None, **kw):
    return late_swap.run(run_id, entries, offline=True, runs_root=tmp / "runs", outputs_root=tmp / "outputs",
                         salary_path=salary, as_of=as_of, **kw)


class _Count:
    def __init__(self):
        from nhl_dfs.models import roles

        self.n, self.fn = 0, roles.apply_state

    def __call__(self, *a, **k):
        self.n += 1
        return self.fn(*a, **k)


def _setup(full_classic, tmp_path):
    tmp, r = full_classic
    eid = read_entries(_v3(r)).entries[0].entry_id
    victim = _victim(r, eid)
    fresh = _fresh_with_out(r.run.inputs / "DKSalaries.csv", tmp_path / "fresh.csv", [victim])
    return tmp, r, eid, victim, fresh


def test_repair_uses_the_scenario_objective_and_resimulates_only_the_changed_game(full_classic, tmp_path):
    tmp, r, eid, victim, fresh = _setup(full_classic, tmp_path)
    count = _Count()
    s = _swap(tmp, r.run.run_id, _v3(r), as_of=BEFORE, salary=fresh, apply_state=count)
    assert s.ok, s.manifest["failed"]
    o = s.manifest["objective"]
    assert o["kind"] == "scenario" and o["requested"] == "auto" and o["fallbacks"] == []
    assert count.n == 1  # roles.apply_state exactly once per run (B11)
    assert o["scenario"]["games_resimulated"] == ["VAN@EDM"]  # the OUT changed one team's inputs only
    out = s.run.version_file(1)
    assert victim not in _roster(out, eid)
    pick = o["entries"][eid]
    assert pick["selection"]["objective"] and pick["selection"]["se"] >= 0 and pick["selection"]["candidates"] >= 1
    assert pick["referee"]["measured_on"] == "referee scenarios" and pick["referee"]["se"] >= 0
    for k in ("PAYOUT_SOURCE", "OUTCOME_CALIBRATION", "FIELD_CALIBRATION"):
        assert s.statuses[k] == o["evidence"][k]
    assert s.statuses["OUTCOME_CALIBRATION"] == "UNVALIDATED"
    notes = s.notes_path.read_text(encoding="utf-8")
    assert "OBJECTIVE=scenario" in notes and "LIVE_STATUS=NO_SNAPSHOT" in notes and "+/-" in notes
    holders = {e.entry_id for e in read_entries(_v3(r)).entries if victim in _roster(_v3(r), e.entry_id)}
    assert {c["entry_id"] for c in s.manifest["changed_cells"]} == holders  # fast: only entries that needed it
    assert set(o["entries"]) == holders


def test_pinned_cells_match_c2c_under_every_objective(full_classic, tmp_path):
    tmp, r, eid, victim, fresh = _setup(full_classic, tmp_path)
    pool = read_salary(fresh)
    assert any(pool.by_role_id[rid].team in ("NYI", "CAR") for rid in _roster(_v3(r), eid))
    files = {}
    for objective in ("scenario", "provisional", "baseline"):
        s = _swap(tmp, r.run.run_id, _v3(r), as_of=MID, salary=fresh, objective=objective)
        assert s.ok and s.manifest["objective"]["kind"] == objective, s.manifest.get("objective")
        files[objective] = s.run.version_file(1)
        rep = check_file(files[objective], s.run.inputs / "DKSalaries.csv", s.run.inputs / "DKEntries.csv")
        assert rep.ok, rep.reasons[:3]
    src = {e.entry_id: e.cells for e in read_entries(_v3(r)).entries}
    for objective, path in files.items():
        for e in read_entries(path).entries:
            for col, cell in enumerate(src[e.entry_id]):
                rid = cell_role_id(cell)
                if rid and pool.by_role_id[rid].team in ("NYI", "CAR"):  # started game: byte-identical, as in C2c
                    assert e.cells[col] == cell, (objective, e.entry_id, col)
        assert victim not in _roster(path, eid)


def test_objective_falls_back_in_order_when_the_cache_is_absent(full_classic, tmp_path, monkeypatch):
    import shutil

    from nhl_dfs.models import params as params_mod

    tmp, r, eid, victim, fresh = _setup(full_classic, tmp_path)
    runs = tmp_path / "runs"
    shutil.copytree(r.run.path, runs / r.run.run_id)
    shutil.rmtree(runs / r.run.run_id / "scenario")
    s = _swap(tmp_path, r.run.run_id, _v3(r), as_of=BEFORE, salary=fresh)
    o = s.manifest["objective"]
    assert s.ok and o["kind"] == "provisional"
    assert [f["from"] for f in o["fallbacks"]] == ["scenario"] and "no scenario cache" in o["fallbacks"][0]["reason"]

    def boom(*a, **k):
        raise RuntimeError("history store unreadable")

    monkeypatch.setattr(params_mod, "projection_for", boom)
    s2 = _swap(tmp_path, r.run.run_id, _v3(r), as_of=BEFORE, salary=fresh)
    o2 = s2.manifest["objective"]
    assert s2.ok and o2["kind"] == "baseline"
    assert [f["from"] for f in o2["fallbacks"]] == ["scenario", "provisional"]
    assert "OBJECTIVE=baseline" in s2.notes_path.read_text(encoding="utf-8")
    assert any("provisional not used" in m for m in s2.messages)
    assert victim not in _roster(s2.run.version_file(1), eid)


def test_the_cache_survives_a_missing_param_table_without_resimulation(full_classic, tmp_path, monkeypatch):
    from nhl_dfs.models import params as params_mod

    tmp, r, eid, victim, fresh = _setup(full_classic, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(params_mod, "projection_for", boom)
    s = _swap(tmp, r.run.run_id, _v3(r), as_of=BEFORE, salary=fresh)
    o = s.manifest["objective"]
    assert s.ok and o["kind"] == "scenario" and o["scenario"]["games_resimulated"] == []
    assert any("without a role state" in n for n in o["notes"])


def test_optional_work_stop_steps_down_to_baseline_and_says_why(full_classic, tmp_path):
    tmp, r, eid, victim, fresh = _setup(full_classic, tmp_path)
    near = datetime(2026, 9, 30, 1, 54, 45, tzinfo=timezone.utc)  # VAN@EDM in 315 s: open (buffer 300 s), 15 s before T-5
    s = _swap(tmp, r.run.run_id, _v3(r), as_of=near, salary=fresh)
    o = s.manifest["objective"]
    assert s.ok and o["kind"] == "baseline"
    assert len(o["fallbacks"]) == 2 and all("optional work stop: T-5" in f["reason"] for f in o["fallbacks"])


def test_nothing_to_repair_evaluates_no_objective_and_returns_the_input_bytes(full_classic, tmp_path):
    tmp, r = full_classic
    count = _Count()
    s = _swap(tmp, r.run.run_id, _v3(r), as_of=BEFORE, apply_state=count)
    assert s.ok and s.manifest["changed_cells"] == []
    assert s.run.version_file(1).read_bytes() == _v3(r).read_bytes()
    assert s.manifest["objective"]["kind"] == "not needed" and count.n == 0


def test_refresh_resimulates_changed_games_and_persists_them_for_the_next_late_swap(full_classic, tmp_path):
    from nhl_dfs.build import scenario_cache as sc

    tmp, r, eid, victim, fresh = _setup(full_classic, tmp_path)
    count = _Count()
    f = refresh.run(r.run.run_id, offline=True, runs_root=tmp / "runs", outputs_root=tmp / "outputs", salary_path=fresh,
                    as_of=BEFORE, apply_state=count)
    assert f.ok and count.n == 1
    o = f.manifest["objective"]
    assert o["kind"] == "scenario" and o["scenario"]["games_resimulated"] == ["VAN@EDM"]
    got, _ = sc.find(tmp / "runs", f.run.run_id)
    assert got is not None and got.run_id == f.run.run_id  # the child carries the updated draws
    assert got.n("selection") == o["scenario"]["n"]["selection"]
    # from the refresh child, the same news changes no game any more
    s = _swap(tmp, f.run.run_id, f.run.version_file(1), as_of=BEFORE, salary=fresh, objective="scenario")
    assert s.ok
    if s.manifest["objective"]["kind"] == "scenario":
        assert s.manifest["objective"]["scenario"]["games_resimulated"] == []


def test_play_probability_is_priced_once():
    from types import SimpleNamespace as NS

    from nhl_dfs.build.swap_objective import play_probs

    def person(p_dress):
        return NS(goalie=None, opportunity=NS(p_dress=p_dress))

    t0 = NS(persons={"a": person(0.9), "b": person(0.9), "c": person(0.9)})
    t1 = NS(persons={"a": person(0.9), "b": person(0.3), "c": person(0.95)})  # roles lowered b (not on the lineup)
    roles = NS(persons={"a": NS(p_play=0.85), "b": NS(p_play=0.85), "c": NS(p_play=1.0)})
    got, notes = play_probs(t0, t1, roles)
    assert got == {"a": 0.85}  # b's absence is already in his dressing; c plays
    assert any("not stacked" in n for n in notes)
