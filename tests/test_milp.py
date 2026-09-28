import sys
from types import SimpleNamespace

import numpy as np
import pytest

from conftest import mini_pair, real_pair
from nhl_dfs.build.milp import (
    GroupConstraint,
    LineupModel,
    overlap_units,
    solve_lineup,
    solver_available,
)
from nhl_dfs.contracts.geometry import Mode, check_lineup
from nhl_dfs.contracts.statuses import SearchStatus
from nhl_dfs.intake.salary import read_salary
from nhl_dfs.models.priors import prior_objective, prior_table
from pool_builder import classic_pool, make_pool, row, sd_person, showdown_pool

pytestmark = pytest.mark.c2a


def _rows(pool, lineup):
    return [pool.by_role_id[x] for x in lineup]


def _legal(pool, lineup):
    return check_lineup(_rows(pool, lineup), pool.mode).ok


def _load(pair):
    pool = read_salary(pair[0])
    return pool, prior_objective(pool, prior_table(pool))


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_mini_solution_is_legal(mode):
    pool, obj = _load(mini_pair(mode))
    res = solve_lineup(pool, pool.mode, obj)
    assert res.status is SearchStatus.FEASIBLE
    assert _legal(pool, res.lineup)
    assert res.objective_value == pytest.approx(sum(obj[x] for x in res.lineup))


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_real_solution_is_legal(mode):
    pool, obj = _load(real_pair(mode))
    res = solve_lineup(pool, pool.mode, obj)
    assert res.status is SearchStatus.FEASIBLE
    assert _legal(pool, res.lineup)


@pytest.mark.parametrize("pool", [classic_pool(teams=("AAA",)), classic_pool(teams=("AAA", "BBB")),
                                  showdown_pool(teams=("AAA",))], ids=["classic1", "classic2", "showdown1"])
def test_too_few_teams_is_infeasible(pool):
    res = solve_lineup(pool, pool.mode, {r.role_id: 1.0 for r in pool.rows})
    assert res.status is SearchStatus.INFEASIBLE
    assert res.solver_status_code == 2 and res.lineup is None


def test_two_rich_teams_force_the_third_team():
    pool = classic_pool(teams=("AAA", "BBB", "CCC"))
    obj = {r.role_id: (20.0 if r.team in ("AAA", "BBB") else 1.0) for r in pool.rows}
    res = solve_lineup(pool, pool.mode, obj)
    assert res.status is SearchStatus.FEASIBLE
    skaters = [r for r in _rows(pool, res.lineup) if not r.is_goalie]
    assert {r.team for r in skaters} == {"AAA", "BBB", "CCC"}
    assert sum(r.team == "CCC" for r in skaters) == 1


def test_locked_rows_count_toward_the_team_rule():
    pool = classic_pool(teams=("AAA", "BBB", "CCC"))
    obj = {r.role_id: (20.0 if r.team in ("AAA", "BBB") else 1.0) for r in pool.rows}
    ccc_d = next(r for r in pool.rows if r.team == "CCC" and r.position == "D")
    res = solve_lineup(pool, pool.mode, obj, locked={5: ccc_d.role_id})
    assert res.lineup[5] == ccc_d.role_id
    assert [r.team for r in _rows(pool, res.lineup) if not r.is_goalie].count("CCC") == 1


def test_showdown_locks_from_one_team_force_the_other_team():
    pool = showdown_pool(teams=("AAA", "BBB"), per_team=8)
    obj = {r.role_id: (10.0 if r.team == "AAA" else 1.0) for r in pool.rows}
    aaa = [p for p in pool.persons.values() if p.flex.team == "AAA"]
    locked = {0: aaa[0].cpt.role_id, 1: aaa[1].flex.role_id, 2: aaa[2].flex.role_id, 3: aaa[3].flex.role_id}
    res = solve_lineup(pool, pool.mode, obj, locked=locked)
    assert res.status is SearchStatus.FEASIBLE
    assert all(res.lineup[i] == rid for i, rid in locked.items())
    free_teams = [pool.by_role_id[res.lineup[i]].team for i in (4, 5)]
    assert free_teams.count("BBB") == 1  # the other team, exactly as much as the rule needs


def test_locked_wins_over_exclude_and_sits_at_its_index():
    pool = classic_pool()
    g = next(r for r in pool.rows if r.is_goalie)
    res = solve_lineup(pool, pool.mode, {}, exclude=frozenset({g.role_id}), locked={8: g.role_id})
    assert res.status is SearchStatus.FEASIBLE and res.lineup[8] == g.role_id


def test_impossible_locks_are_infeasible_and_unknown_locks_raise():
    pool = classic_pool()
    g = next(r for r in pool.rows if r.is_goalie)
    assert solve_lineup(pool, pool.mode, {}, locked={7: g.role_id}).status is SearchStatus.INFEASIBLE  # goalie in UTIL
    c = next(r for r in pool.rows if r.position == "C")
    assert solve_lineup(pool, pool.mode, {}, locked={0: c.role_id, 7: c.role_id}).status is SearchStatus.INFEASIBLE
    with pytest.raises(ValueError, match="not a selectable row"):
        solve_lineup(pool, pool.mode, {}, locked={0: "nope"})


def test_exclude_is_respected():
    pool, obj = _load(mini_pair("classic"))
    first = solve_lineup(pool, pool.mode, obj)
    again = solve_lineup(pool, pool.mode, obj, exclude=frozenset(first.lineup[:3]))
    assert again.status is SearchStatus.FEASIBLE
    assert not set(first.lineup[:3]) & set(again.lineup)


def test_group_min_and_max():
    pool = classic_pool(teams=("AAA", "BBB", "CCC", "DDD"))
    obj = {r.role_id: (5.0 if r.team == "AAA" else 1.0) for r in pool.rows}
    ddd = frozenset(r.role_id for r in pool.rows if r.team == "DDD" and not r.is_goalie)
    aaa = frozenset(r.role_id for r in pool.rows if r.team == "AAA")
    res = solve_lineup(pool, pool.mode, obj, groups=(GroupConstraint(ddd, min_count=3), GroupConstraint(aaa, max_count=2)))
    assert res.status is SearchStatus.FEASIBLE
    assert len(set(res.lineup) & ddd) >= 3 and len(set(res.lineup) & aaa) <= 2
    bad = solve_lineup(pool, pool.mode, obj, groups=(GroupConstraint(ddd, min_count=9),))
    assert bad.status is SearchStatus.INFEASIBLE


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_max_overlap_forces_a_real_difference(mode):
    pool, obj = _load(mini_pair(mode))
    first = solve_lineup(pool, pool.mode, obj)
    n = len(first.lineup)
    second = solve_lineup(pool, pool.mode, obj, max_overlap_with=[(first.lineup, n - 2)])
    assert second.status is SearchStatus.FEASIBLE and _legal(pool, second.lineup)
    shared = overlap_units(_rows(pool, first.lineup), pool.mode) & overlap_units(_rows(pool, second.lineup), pool.mode)
    assert len(shared) <= n - 2


def test_classic_overlap_counts_persons_so_a_slot_shuffle_is_not_a_difference():
    pool = classic_pool()
    res = solve_lineup(pool, pool.mode, {r.role_id: 1.0 for r in pool.rows})
    rows = _rows(pool, res.lineup)
    shuffled = [rows[i] for i in (0, 1, 2, 3, 7, 5, 6, 4, 8)]  # W and UTIL swap slots
    assert overlap_units(rows, Mode.CLASSIC) == overlap_units(shuffled, Mode.CLASSIC)


def test_showdown_captain_swap_counts_as_two_differences():
    a_cpt, a_flex = sd_person(1, "AAA", "C", 6000)
    b_cpt, b_flex = sd_person(2, "BBB", "C", 6000)
    one = [a_cpt, b_flex]
    two = [b_cpt, a_flex]
    assert len(overlap_units(one, Mode.SHOWDOWN) - overlap_units(two, Mode.SHOWDOWN)) == 2


def test_tiny_time_limit_never_returns_an_invalid_lineup():
    pool, obj = _load(mini_pair("classic"))
    res = solve_lineup(pool, pool.mode, obj, time_limit_s=0.2)
    assert res.status in (SearchStatus.FEASIBLE, SearchStatus.TIME_LIMIT_WITH_INCUMBENT)
    assert _legal(pool, res.lineup)


@pytest.mark.parametrize("mode", ["classic", "showdown"])
def test_real_pool_with_a_millisecond_limit_is_never_infeasible_or_illegal(mode):
    pool, obj = _load(real_pair(mode))
    res = solve_lineup(pool, pool.mode, obj, time_limit_s=0.001)
    assert res.status is not SearchStatus.INFEASIBLE
    if res.lineup is not None:
        assert _legal(pool, res.lineup)
    else:
        assert res.status is SearchStatus.ERROR


def _stub(monkeypatch, status, x):
    import scipy.optimize

    def fake(c, **kwargs):
        return SimpleNamespace(status=status, x=x(len(c)) if callable(x) else x, message="stub")

    monkeypatch.setattr(scipy.optimize, "milp", fake)


def _legal_x(pool):
    """A real solution vector for the stub to hand back."""
    model = LineupModel(pool, pool.mode)
    res = model.solve({r.role_id: 1.0 for r in pool.rows})
    ids = set(res.lineup)
    x = np.zeros(model.n_vars)
    placed = set()
    for i, r in enumerate(model.var_row):
        if r.role_id in ids and r.role_id not in placed:
            # pick the slot type the real solve used for this row
            k = res.lineup.index(r.role_id)
            if model.slots[k] == model.var_slot[i]:
                x[i] = 1.0
                placed.add(r.role_id)
    return x


def test_status_one_with_incumbent_is_time_limit_with_incumbent(monkeypatch):
    pool = classic_pool()
    x = _legal_x(pool)
    _stub(monkeypatch, 1, x)
    res = solve_lineup(pool, pool.mode, {})
    assert res.status is SearchStatus.TIME_LIMIT_WITH_INCUMBENT and res.solver_status_code == 1
    assert _legal(pool, res.lineup)


def test_status_one_without_incumbent_is_error_not_infeasible(monkeypatch):
    pool = classic_pool()
    _stub(monkeypatch, 1, None)
    res = solve_lineup(pool, pool.mode, {})
    assert res.status is SearchStatus.ERROR and res.lineup is None


@pytest.mark.parametrize("code", [3, 4])
def test_other_statuses_are_error(monkeypatch, code):
    pool = classic_pool()
    _stub(monkeypatch, code, None)
    assert solve_lineup(pool, pool.mode, {}).status is SearchStatus.ERROR


def test_an_illegal_solver_answer_is_never_returned(monkeypatch):
    pool = classic_pool()
    _stub(monkeypatch, 0, lambda n: np.ones(n))  # every variable on
    res = solve_lineup(pool, pool.mode, {})
    assert res.status is SearchStatus.ERROR and res.lineup is None


def test_solver_exception_is_error(monkeypatch):
    import scipy.optimize

    def boom(*a, **k):
        raise RuntimeError("highs crashed")

    monkeypatch.setattr(scipy.optimize, "milp", boom)
    res = solve_lineup(classic_pool(), Mode.CLASSIC, {})
    assert res.status is SearchStatus.ERROR and "highs crashed" in res.detail


def test_solver_available_false_when_import_fails(monkeypatch):
    assert solver_available() is True
    monkeypatch.setitem(sys.modules, "scipy.optimize", None)
    assert solver_available() is False


def test_arbitrary_roster_positions_are_respected():
    pool = classic_pool()
    util_only = row("u1", "AAA", "LW", 2500, roster={"UTIL"})
    pool = make_pool(Mode.CLASSIC, pool.rows + [util_only])
    res = solve_lineup(pool, pool.mode, {"u1": 100.0})
    assert res.lineup[7] == "u1"


def test_a_person_with_two_rows_is_used_once():
    base = classic_pool()
    twin_a = row("t1", "AAA", "C", 2500, roster={"C", "UTIL"}, person="twin|AAA|F")
    twin_b = row("t2", "AAA", "C", 2500, roster={"W", "UTIL"}, person="twin|AAA|F")
    pool = make_pool(Mode.CLASSIC, base.rows + [twin_a, twin_b])
    res = solve_lineup(pool, pool.mode, {"t1": 100.0, "t2": 100.0})
    assert res.status is SearchStatus.FEASIBLE
    assert len({"t1", "t2"} & set(res.lineup)) == 1
